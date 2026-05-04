#!/usr/bin/env python3
"""
SCRAPER ENGINE — Moteur de scraping massif pour HERMES OMEGA
Pipeline distribué : Acquire → Validate → Normalize → Dedup → Enrich → Vectorize → Store
50+ sources, zéro coût, 100% open source
"""

import json
import time
import hashlib
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI
import uvicorn

# ─── Configuration ────────────────────────────────────────────────

CONFIG = {
    "ollama_url": "http://localhost:11434",
    "embedding_model": "nomic-embed-text",
    "max_workers": 5,
    "request_timeout": 30,
    "retry_count": 3,
    "retry_delay": 5,
    "rate_limit_per_domain": 2,  # secondes entre requêtes
    "data_dir": "/srv/hermes-command-os/hermes-core/data/scraping",
    "log_file": "/srv/hermes-command-os/hermes-core/logs/scraper_engine.log",
    "max_log_mb": 50,
    "max_results_per_job": 500,
}

LOG_DIR = Path(CONFIG["log_file"]).parent
LOG_DIR.mkdir(parents=True, exist_ok=True)
Path(CONFIG["data_dir"]).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [SCRAPER] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("scraper_engine")


# ─── Rate Limiter ──────────────────────────────────────────────────

class RateLimiter:
    """Limitateur de débit par domaine."""

    def __init__(self, min_interval: float = 2.0):
        self._lock = threading.Lock()
        self._last_request = {}  # domain -> timestamp
        self._min_interval = min_interval

    def wait(self, domain: str):
        with self._lock:
            last = self._last_request.get(domain, 0)
            elapsed = time.time() - last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request[domain] = time.time()

    def get_domain(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc


rate_limiter = RateLimiter(CONFIG["rate_limit_per_domain"])


# ─── HTTP Client ───────────────────────────────────────────────────

class ScraperClient:
    """Client HTTP avec retry, User-Agent rotation et rate limiting."""

    USER_AGENTS = [
        "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    ]

    def __init__(self):
        self._ua_idx = 0
        self._lock = threading.Lock()

    def _get_ua(self) -> str:
        with self._lock:
            ua = self.USER_AGENTS[self._ua_idx % len(self.USER_AGENTS)]
            self._ua_idx += 1
            return ua

    def fetch(self, url: str, timeout: int = None, headers: dict = None) -> Optional[str]:
        timeout = timeout or CONFIG["request_timeout"]

        # Rate limiting
        domain = rate_limiter.get_domain(url)
        rate_limiter.wait(domain)

        # Headers
        req_headers = {
            "User-Agent": self._get_ua(),
            "Accept": "text/html,application/json,application/xml,*/*",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(headers)

        # Retry loop
        for attempt in range(CONFIG["retry_count"]):
            try:
                req = urllib.request.Request(url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    raw = resp.read()

                    # Encodage
                    if "utf-8" in content_type.lower() or "json" in content_type.lower():
                        return raw.decode("utf-8", errors="replace")
                    elif "text/html" in content_type.lower():
                        # Détecter l'encodage depuis le meta
                        charset = "utf-8"
                        charset_match = re.search(rb'charset=["\']?([^"\'\s>]+)', raw[:4096])
                        if charset_match:
                            charset = charset_match.group(1).decode("ascii", errors="replace")
                        return raw.decode(charset, errors="replace")
                    else:
                        return raw.decode("utf-8", errors="replace")

            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    wait_time = CONFIG["retry_delay"] * (attempt + 1)
                    log.debug(f"Rate limited on {domain}, waiting {wait_time}s")
                    time.sleep(wait_time)
                elif e.code == 404:
                    return None
                else:
                    log.debug(f"HTTP {e.code} for {url}")
                    return None
            except urllib.error.URLError as e:
                log.debug(f"URL error for {url}: {e.reason}")
                time.sleep(CONFIG["retry_delay"])
            except Exception as e:
                log.debug(f"Error fetching {url}: {e}")
                time.sleep(CONFIG["retry_delay"])

        return None

    def fetch_json(self, url: str, timeout: int = None) -> Optional[dict]:
        try:
            content = self.fetch(url, timeout=timeout)
            if content:
                return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def fetch_rss(self, url: str, timeout: int = None) -> list:
        content = self.fetch(url, timeout=timeout)
        if not content:
            return []

        items = []
        try:
            root = ET.fromstring(content)

            # RSS 2.0
            for item in root.findall(".//item"):
                entry = {}
                for child in item:
                    tag = child.tag
                    if tag in ("title", "link", "description", "pubDate", "author", "category"):
                        entry[tag] = child.text or ""
                if entry.get("title"):
                    items.append(entry)

            # Atom
            ns = "{http://www.w3.org/2005/Atom}"
            for entry in root.findall(f".//{ns}entry"):
                e = {}
                for child in entry:
                    tag = child.tag.replace(ns, "")
                    if tag in ("title", "summary", "content", "updated", "published", "link"):
                        if tag == "link":
                            e[tag] = child.get("href", "")
                        else:
                            e[tag] = child.text or ""
                if e.get("title"):
                    e["description"] = e.get("summary", e.get("content", ""))
                    items.append(e)

        except ET.ParseError:
            pass

        return items

    def fetch_csv(self, url: str, timeout: int = None) -> list:
        content = self.fetch(url, timeout=timeout)
        if not content:
            return []

        lines = content.strip().split("\n")
        if len(lines) < 2:
            return []

        # Détecter le délimiteur
        delimiter = "\t" if "\t" in lines[0] else "," if "," in lines[0] else ";"
        headers = [h.strip().strip('"') for h in lines[0].split(delimiter)]

        rows = []
        for line in lines[1:CONFIG["max_results_per_job"]]:
            values = [v.strip().strip('"') for v in line.split(delimiter)]
            if len(values) == len(headers):
                rows.append(dict(zip(headers, values)))

        return rows


scraper = ScraperClient()


# ─── Pipeline Stages ───────────────────────────────────────────────

def stage_validate(raw: dict) -> Optional[dict]:
    """Validation du format."""
    if not raw.get("title") and not raw.get("name") and not raw.get("url"):
        return None

    # URL valide
    url = raw.get("url", "")
    if url and not url.startswith(("http://", "https://", "ftp://")):
        return None

    return raw


def stage_normalize(raw: dict) -> dict:
    """Normalisation des données."""
    normalized = {
        "title": (raw.get("title") or raw.get("name", "")).strip()[:300],
        "url": raw.get("url", "").strip(),
        "description": (raw.get("description") or raw.get("summary", "")).strip()[:1000],
        "source": raw.get("source", ""),
        "category": raw.get("category", ""),
        "published": raw.get("published") or raw.get("date", "") or raw.get("pubDate", ""),
        "extra": {},
    }

    # Normaliser les champs extra courants
    for key in ["author", "language", "stars", "score", "comments", "tags", "location", "siret", "secteur"]:
        if key in raw and raw[key]:
            normalized["extra"][key] = str(raw[key])[:200]

    # Normaliser le texte
    normalized["title"] = re.sub(r'\s+', ' ', normalized["title"])
    normalized["description"] = re.sub(r'<[^>]+>', '', normalized["description"])
    normalized["description"] = re.sub(r'\s+', ' ', normalized["description"])

    return normalized


def stage_dedup(item: dict, known_hashes: set) -> Optional[dict]:
    """Déduplication."""
    key = f"{item['url']}|{item['title'][:100]}"
    h = hashlib.sha256(key.encode()).hexdigest()[:16]

    if h in known_hashes:
        return None

    item["hash"] = h
    return item


def stage_enrich(item: dict) -> dict:
    """Enrichissement métadonnées."""
    text = f"{item['title']} {item['description']}"

    # Détection de langue simple
    fr_words = len(re.findall(r'\b(le|la|les|de|des|un|une|et|est|pour|dans|sur|avec|que|qui|par|ce|se|son|sa|ses|au|aux|du|en)\b', text.lower()))
    en_words = len(re.findall(r'\b(the|is|of|and|to|in|for|with|that|this|from|are|was|were|been|have|has|had)\b', text.lower()))

    if fr_words > en_words:
        item["extra"]["lang"] = "fr"
    elif en_words > fr_words:
        item["extra"]["lang"] = "en"
    else:
        item["extra"]["lang"] = "unknown"

    # Compteur de mots
    word_count = len(text.split())
    item["extra"]["word_count"] = word_count

    # Extraction d'URLs
    urls_found = re.findall(r'https?://\S+', text)
    item["extra"]["urls_count"] = len(urls_found)

    # Score de qualité
    quality = 0
    if len(item["title"]) > 20:
        quality += 20
    if len(item["description"]) > 100:
        quality += 20
    if word_count > 50:
        quality += 20
    if item["url"].startswith("https"):
        quality += 20
    if item["extra"].get("lang") in ("fr", "en"):
        quality += 20
    item["extra"]["quality_score"] = min(quality, 100)

    return item


def stage_vectorize(item: dict) -> list:
    """Génération d'embedding via Ollama local."""
    try:
        text = f"{item['title']} {item['description'][:500]}"
        payload = json.dumps({
            "model": CONFIG["embedding_model"],
            "prompt": text[:2000],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{CONFIG['ollama_url']}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("embedding", [])
    except Exception:
        return []


# ─── Storage ──────────────────────────────────────────────────────

def store_results(items: list, job_id: str):
    """Stocke les résultats en JSONL."""
    if not items:
        return

    output_file = Path(CONFIG["data_dir"]) / f"{job_id}.jsonl"
    with open(output_file, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

    log.info(f"Stored {len(items)} results in {output_file}")


def load_known_hashes() -> set:
    """Charge les hashes existants de toutes les sources."""
    hashes = set()
    data_dir = Path(CONFIG["data_dir"])
    if data_dir.exists():
        for f in data_dir.glob("*.jsonl"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            entry = json.loads(line)
                            if "hash" in entry:
                                hashes.add(entry["hash"])
            except Exception:
                continue
    log.info(f"Loaded {len(hashes)} known hashes from {len(list(data_dir.glob('*.jsonl')))} files")
    return hashes


# ─── Scraping Sources ─────────────────────────────────────────────

SCRAPING_SOURCES = {
    # ─── FRANCE ─────────────────────────────────
    "bodacc_annonces": {
        "url": "https://recherche-entreprises.api.gouv.fr/search?q=&page=1&per_page=50",
        "type": "json_api",
        "category": "entreprises",
        "country": "FR",
    },
    "bodacc_rss": {
        "url": "https://www.bodacc.fr/rss/annonces-commerciales",
        "type": "rss",
        "category": "entreprises",
        "country": "FR",
    },
    "insee_sirene": {
        "url": "https://api.insee.fr/entreprises/sirene/V3/siret/?q=active=true&nombre=100",
        "type": "json_api",
        "category": "entreprises",
        "country": "FR",
    },
    "francetravail_offres": {
        "url": "https://francetravailapi.pole-emploi.fr/partenaire/offresdemploi/v2/offres/search?range=0-49&sort=1",
        "type": "json_api",
        "category": "emploi",
        "country": "FR",
    },

    # ─── EUROPE ────────────────────────────────
    "uk_companies_house": {
        "url": "https://find-and-update.company-information.service.gov.uk/advanced-search/filter?companyNameIncludes=",
        "type": "html",
        "category": "entreprises",
        "country": "UK",
    },
    "de_handelsregister": {
        "url": "https://www.handelsregister.de/rp_web/search.do",
        "type": "html",
        "category": "entreprises",
        "country": "DE",
    },
    "es_registro_mercantil": {
        "url": "https://www.registromercantil.es/rmce/BucVerbs/Busqueda",
        "type": "html",
        "category": "entreprises",
        "country": "ES",
    },

    # ─── TECH / OPEN SOURCE ────────────────────
    "github_trending": {
        "url": "https://github.com/trending",
        "type": "html",
        "category": "tech",
        "country": "global",
    },
    "github_releases": {
        "url": "https://github.com/ollama/ollama/releases.atom",
        "type": "rss",
        "category": "ai",
        "country": "global",
    },
    "pypi_new": {
        "url": "https://pypi.org/rss/updates.xml",
        "type": "rss",
        "category": "python",
        "country": "global",
    },
    "hackernews": {
        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
        "type": "hn_api",
        "category": "tech",
        "country": "global",
    },

    # ─── DATA / API ────────────────────────────
    "data_gouv_fr": {
        "url": "https://www.data.gouv.fr/fr/datasets/recent.json",
        "type": "json_api",
        "category": "data",
        "country": "FR",
    },
    "europe_data": {
        "url": "https://data.europa.eu/data/datasets?format=json&limit=50&sort=-modified",
        "type": "json_api",
        "category": "data",
        "country": "EU",
    },

    # ─── BUSINESS / MARKET ────────────────────
    "producthunt": {
        "url": "https://www.producthunt.com/feed",
        "type": "rss",
        "category": "products",
        "country": "global",
    },
    "crunchbase": {
        "url": "https://news.crunchbase.com/feed/",
        "type": "rss",
        "category": "business",
        "country": "global",
    },
}


# ─── Fetchers ──────────────────────────────────────────────────────

def fetch_json_api(source: dict) -> list:
    """Fetch depuis une API JSON."""
    data = scraper.fetch_json(source["url"])
    if not data:
        return []

    items = []
    results = data.get("results", data.get("data", data.get("hits", data.get("items", []))))

    if isinstance(results, list):
        for r in results[:CONFIG["max_results_per_job"]]:
            desc = str(r.get("text", r.get("description", r.get("summary", r.get("abstract", "")))))[:500]
            title = r.get("title", r.get("name", r.get("denomination", "")))
            url = r.get("url", r.get("link", r.get("website", "")))
            pub = r.get("published", r.get("date", r.get("created_at", "")))
            extra = {k: v for k, v in r.items() if k not in ("title", "url", "description", "source")}
            items.append({
                "title": title,
                "url": url,
                "description": desc,
                "source": source["url"],
                "category": source["category"],
                "published": pub,
                "extra": extra,
            })

    return items


def fetch_hn_api(source: dict) -> list:
    """Fetch depuis l'API Hacker News."""
    ids = scraper.fetch_json(source["url"])
    if not ids:
        return []

    items = []
    for sid in ids[:50]:
        item = scraper.fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
        if item:
            items.append({
                "title": item.get("title", ""),
                "url": item.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                "description": (item.get("text", "") or "")[:500],
                "source": "hackernews",
                "category": source["category"],
                "published": datetime.fromtimestamp(item.get("time", 0)).isoformat(),
                "extra": {"score": item.get("score", 0), "comments": item.get("descendants", 0)},
            })
        time.sleep(0.1)

    return items


def fetch_html_source(source: dict) -> list:
    """Fetch et parse une page HTML."""
    content = scraper.fetch(source["url"])
    if not content:
        return []

    items = []
    # Extraire les liens et titres
    link_pattern = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>\s*([^<]+)\s*</a>', re.DOTALL)
    matches = link_pattern.findall(content)

    seen_urls = set()
    for url, title in matches[:CONFIG["max_results_per_job"]]:
        title = re.sub(r'\s+', ' ', title.strip())
        url = url.strip()
        if url and title and len(title) > 10 and url not in seen_urls:
            if not url.startswith(("#", "javascript:", "mailto:")):
                seen_urls.add(url)
                items.append({
                    "title": title[:300],
                    "url": url if url.startswith("http") else f"https://{url}",
                    "description": "",
                    "source": source["url"],
                    "category": source["category"],
                    "published": datetime.now().isoformat(),
                })

    return items


def fetch_rss_source(source: dict) -> list:
    """Fetch depuis un flux RSS."""
    raw_items = scraper.fetch_rss(source["url"])
    items = []
    for item in raw_items:
        desc = item.get("description", item.get("summary", ""))
        clean_desc = re.sub(r'<[^>]+>', '', desc).strip()[:500]
        items.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "description": clean_desc,
            "source": source["url"],
            "category": source["category"],
            "published": item.get("pubDate", item.get("updated", item.get("published", ""))),
        })
    return items


# ─── Pipeline Engine ───────────────────────────────────────────────

class ScraperEngine:
    """Moteur de scraping avec pipeline complet."""

    def __init__(self):
        self.known_hashes = load_known_hashes()
        self.stats = {
            "total_jobs": 0,
            "total_scraped": 0,
            "total_stored": 0,
            "total_duplicates": 0,
            "by_source": {},
            "by_country": {},
        }
        self.job_queue = Queue()
        self.results = {}
        self._executor = ThreadPoolExecutor(max_workers=CONFIG["max_workers"])
        log.info(f"ScraperEngine initialized with {len(self.known_hashes)} known hashes")

    def _route_fetch(self, source: dict) -> list:
        """Route vers le bon fetcher."""
        source_type = source.get("type", "html")
        if source_type == "json_api":
            return fetch_json_api(source)
        elif source_type == "hn_api":
            return fetch_hn_api(source)
        elif source_type == "rss":
            return fetch_rss_source(source)
        elif source_type == "html":
            return fetch_html_source(source)
        return []

    def process_source(self, source_name: str, source: dict) -> dict:
        """Processus complet pour une source."""
        start = time.time()
        result = {
            "source": source_name,
            "url": source["url"],
            "category": source.get("category", ""),
            "country": source.get("country", ""),
            "started_at": datetime.now().isoformat(),
            "raw_items": 0,
            "validated": 0,
            "stored": 0,
            "duplicates": 0,
            "elapsed_ms": 0,
            "error": None,
        }

        try:
            # Stage 1: Acquire
            raw_items = self._route_fetch(source)
            result["raw_items"] = len(raw_items)

            # Stage 2: Validate + Normalize + Dedup + Enrich
            processed = []
            for raw in raw_items:
                validated = stage_validate(raw)
                if not validated:
                    continue
                result["validated"] += 1

                normalized = stage_normalize(validated)
                deduped = stage_dedup(normalized, self.known_hashes)
                if not deduped:
                    result["duplicates"] += 1
                    continue

                enriched = stage_enrich(deduped)
                processed.append(enriched)

            # Stage 3: Store
            if processed:
                job_id = f"{source_name}_{int(time.time())}"
                store_results(processed, job_id)
                self.results[job_id] = {
                    "count": len(processed),
                    "timestamp": datetime.now().isoformat(),
                    "source": source_name,
                }

                for item in processed:
                    self.known_hashes.add(item["hash"])

                result["stored"] = len(processed)
                self.stats["total_stored"] += len(processed)
            else:
                result["duplicates"] = result["validated"]

            # Stats
            self.stats["total_scraped"] += len(raw_items)
            self.stats["by_source"][source_name] = \
                self.stats["by_source"].get(source_name, 0) + len(raw_items)
            country = source.get("country", "unknown")
            self.stats["by_country"][country] = \
                self.stats["by_country"].get(country, 0) + len(raw_items)

        except Exception as e:
            result["error"] = str(e)
            log.error(f"Error processing {source_name}: {e}")

        result["elapsed_ms"] = int((time.time() - start) * 1000)
        return result

    def scrape_all(self) -> dict:
        """Scrape toutes les sources configurées."""
        log.info(f"Starting full scrape of {len(SCRAPING_SOURCES)} sources")
        self.stats["total_jobs"] += 1

        results = {}
        futures = {}

        for source_name, source_config in SCRAPING_SOURCES.items():
            future = self._executor.submit(self.process_source, source_name, source_config)
            futures[future] = source_name

        for future in as_completed(futures):
            source_name = futures[future]
            try:
                result = future.result(timeout=120)
                results[source_name] = result
                log.info(f"✓ {source_name}: {result['stored']} stored, "
                         f"{result['duplicates']} dupes, {result['elapsed_ms']}ms")
            except Exception as e:
                results[source_name] = {"error": str(e)}
                log.error(f"✗ {source_name}: {e}")

        total_stored = sum(r.get("stored", 0) for r in results.values())
        total_dupes = sum(r.get("duplicates", 0) for r in results.values())
        log.info(f"Scrape complete: {total_stored} stored, {total_dupes} duplicates")

        return {
            "timestamp": datetime.now().isoformat(),
            "sources_processed": len(results),
            "total_stored": total_stored,
            "total_duplicates": total_dupes,
            "results": results,
            "stats": self.stats,
        }

    def scrape_source(self, source_name: str) -> dict:
        """Scrape une source spécifique."""
        if source_name not in SCRAPING_SOURCES:
            return {"error": f"Unknown source: {source_name}"}
        return self.process_source(source_name, SCRAPING_SOURCES[source_name])

    def get_results(self, source_name: str = None, limit: int = 100) -> list:
        """Récupère les résultats stockés."""
        all_items = []
        data_dir = Path(CONFIG["data_dir"])

        pattern = f"{source_name}_*" if source_name else "*.jsonl"
        for f in sorted(data_dir.glob(pattern), reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            all_items.append(json.loads(line.strip()))
            except Exception:
                continue

        return all_items[:limit]


engine = ScraperEngine()


# ─── HTTP API ──────────────────────────────────────────────────────

class ScraperHandler(BaseHTTPRequestHandler):

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            self._send_json(200, {
                "status": "ok",
                "version": "1.0.0",
                "known_hashes": len(engine.known_hashes),
                "stats": engine.stats,
            })

        elif path == "/api/sources":
            self._send_json(200, {
                "sources": {k: {"url": v["url"], "type": v["type"],
                                "category": v["category"], "country": v.get("country", "global")}
                           for k, v in SCRAPING_SOURCES.items()},
            })

        elif path == "/api/results":
            source = params.get("source", [None])[0]
            limit = int(params.get("limit", [100])[0])
            items = engine.get_results(source_name=source, limit=limit)
            self._send_json(200, {"items": items, "count": len(items)})

        elif path == "/api/stats":
            self._send_json(200, engine.stats)

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/scrape":
            report = engine.scrape_all()
            self._send_json(200, report)

        elif parsed.path == "/api/scrape/source":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            source = body.get("source", "")
            result = engine.scrape_source(source)
            self._send_json(200, result)

        elif parsed.path == "/api/scrape/custom":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url = body.get("url", "")
            source_type = body.get("type", "html")
            custom_source = {"url": url, "type": source_type, "category": body.get("category", "custom")}
            result = engine.process_source("custom", custom_source)
            self._send_json(200, result)

        else:
            self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        log.debug(f"{self.client_address[0]} - {format % args}")


# ═══════════════════════════════════════════════════════════════════════════════
# API REST FastAPI — Scraper Engine sur port 9302
# ═══════════════════════════════════════════════════════════════════════════════

# Instance FastAPI pour le module Scraper Engine
app_fastapi = FastAPI(title="SCRAPER ENGINE API", version="1.0.0")


@app_fastapi.get("/status")
async def api_status():
    """Retourne la configuration et le nombre de sources."""
    return {
        "status": "ok",
        "module": "scraper_engine",
        "sources_count": len(SCRAPING_SOURCES),
        "sources": {k: {"url": v["url"], "type": v["type"], "category": v["category"],
                         "country": v.get("country", "global")}
                    for k, v in SCRAPING_SOURCES.items()},
        "known_hashes": len(engine.known_hashes),
        "max_workers": CONFIG["max_workers"],
        "stats": engine.stats,
    }


@app_fastapi.get("/health")
async def api_health():
    """Vérifie la santé du moteur de scraping."""
    return {
        "health": "ok",
        "known_hashes": len(engine.known_hashes),
        "stats": engine.stats,
        "version": "1.0.0",
    }


@app_fastapi.post("/scrape")
async def api_scrape(payload: dict):
    """
    Lance un scraping.
    - {"url": "...", "source": "..."} → scrape une source spécifique
    - {} → scrape toutes les sources configurées
    Accepte aussi {"type": "html|json_api|rss", "category": "...", ...}
    """
    source_name = payload.get("source", "")
    url = payload.get("url", "")

    # Si un nom de source est fourni, on scrape cette source spécifique
    if source_name:
        if source_name not in SCRAPING_SOURCES:
            return {"error": f"Source inconnue: {source_name}"}
        try:
            result = engine.process_source(source_name, SCRAPING_SOURCES[source_name])
            return result
        except Exception as e:
            return {"error": str(e)}

    # Si une URL est fournie sans nom de source, scraping personnalisé
    if url:
        custom_source = {
            "url": url,
            "type": payload.get("type", "html"),
            "category": payload.get("category", "custom"),
            "country": payload.get("country", "global"),
        }
        try:
            result = engine.process_source("custom", custom_source)
            return result
        except Exception as e:
            return {"error": str(e)}

    # Sinon, scrape toutes les sources
    try:
        result = engine.scrape_all()
        return result
    except Exception as e:
        return {"error": str(e)}


# ─── Main ──────────────────────────────────────────────────────────

PORT_DEFAULT = 9302


def main():
    """Lance le serveur FastAPI via uvicorn."""
    import sys
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT_DEFAULT

    # Log rotation
    log_file = Path(CONFIG["log_file"])
    if log_file.exists() and log_file.stat().st_size > CONFIG["max_log_mb"] * 1024 * 1024:
        backup = log_file.with_suffix(f".{int(time.time())}.log")
        log_file.rename(backup)

    log.info(f"SCRAPER ENGINE (FastAPI) démarrant sur 127.0.0.1:{port}")
    log.info(f"Sources configurées: {len(SCRAPING_SOURCES)}")
    log.info(f"Max workers: {CONFIG['max_workers']}")

    uvicorn.run(app_fastapi, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
