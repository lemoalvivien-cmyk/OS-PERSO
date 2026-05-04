#!/usr/bin/env python3
"""
TECH WATCHER — Veille technologique permanente pour HERMES OMEGA
50+ sources open-source, scraping continu, auto-intégration
100% gratuit, 100% local, zéro dépendance SaaS
"""

import json
import time
import hashlib
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from typing import Optional

# --- Imports FastAPI / Uvicorn pour l'API REST ---
from fastapi import FastAPI
import uvicorn

# ─── Configuration ────────────────────────────────────────────────

CONFIG = {
    "ollama_url": "http://localhost:11434",
    "embedding_model": "nomic-embed-text",
    "check_interval_minutes": 30,
    "max_items_per_source": 50,
    "retention_days": 90,
    "data_file": "/srv/hermes-command-os/hermes-core/data/tech_signals.jsonl",
    "summary_file": "/srv/hermes-command-os/hermes-core/data/daily_summary.json",
    "log_file": "/srv/hermes-command-os/hermes-core/logs/tech_watcher.log",
    "max_log_mb": 50,
}

LOG_DIR = Path(CONFIG["log_file"]).parent
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TECH-WATCH] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("tech_watcher")


# ─── Sources de veille (toutes gratuites) ─────────────────────────

SOURCES = {
    # GitHub
    "github_trending": {
        "type": "html",
        "url": "https://github.com/trending",
        "interval_minutes": 60,
        "category": "github",
        "user_agent": True,
    },
    "github_releases_python": {
        "type": "rss",
        "url": "https://github.com/python/cpython/releases.atom",
        "interval_minutes": 120,
        "category": "python",
    },
    "github_releases_ollama": {
        "type": "rss",
        "url": "https://github.com/ollama/ollama/releases.atom",
        "interval_minutes": 60,
        "category": "ai",
    },
    "github_releases_nextjs": {
        "type": "rss",
        "url": "https://github.com/vercel/next.js/releases.atom",
        "interval_minutes": 120,
        "category": "web",
    },
    "github_releases_docker": {
        "type": "rss",
        "url": "https://github.com/docker/docker-ce/releases.atom",
        "interval_minutes": 120,
        "category": "devops",
    },
    "github_releases_node": {
        "type": "rss",
        "url": "https://github.com/nodejs/node/releases.atom",
        "interval_minutes": 120,
        "category": "javascript",
    },

    # News & Communities
    "hackernews_top": {
        "type": "api",
        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
        "api_base": "https://hacker-news.firebaseio.com/v0/item/",
        "interval_minutes": 30,
        "category": "news",
    },
    "hackernews_show": {
        "type": "api",
        "url": "https://hacker-news.firebaseio.com/v0/showstories.json",
        "api_base": "https://hacker-news.firebaseio.com/v0/item/",
        "interval_minutes": 60,
        "category": "projects",
    },
    "hackernews_newest": {
        "type": "api",
        "url": "https://hacker-news.firebaseio.com/v0/newstories.json",
        "api_base": "https://hacker-news.firebaseio.com/v0/item/",
        "interval_minutes": 15,
        "category": "news",
    },

    # Reddit
    "reddit_programming": {
        "type": "rss",
        "url": "https://www.reddit.com/r/programming/.rss",
        "interval_minutes": 30,
        "category": "programming",
    },
    "reddit_machinelearning": {
        "type": "rss",
        "url": "https://www.reddit.com/r/MachineLearning/.rss",
        "interval_minutes": 60,
        "category": "ai",
    },
    "reddit_selfhosted": {
        "type": "rss",
        "url": "https://www.reddit.com/r/selfhosted/.rss",
        "interval_minutes": 60,
        "category": "selfhosted",
    },
    "reddit_ollama": {
        "type": "rss",
        "url": "https://www.reddit.com/r/ollama/.rss",
        "interval_minutes": 60,
        "category": "ai",
    },
    "reddit_localllama": {
        "type": "rss",
        "url": "https://www.reddit.com/r/LocalLLaMA/.rss",
        "interval_minutes": 60,
        "category": "ai",
    },
    "reddit_docker": {
        "type": "rss",
        "url": "https://www.reddit.com/r/docker/.rss",
        "interval_minutes": 60,
        "category": "devops",
    },

    # Dev Blogs
    "devto_top": {
        "type": "rss",
        "url": "https://dev.to/feed",
        "interval_minutes": 60,
        "category": "web",
    },
    "producthunt": {
        "type": "rss",
        "url": "https://www.producthunt.com/feed",
        "interval_minutes": 120,
        "category": "products",
    },

    # Package Registries (new releases)
    "pypi_recent": {
        "type": "rss",
        "url": "https://pypi.org/rss/updates.xml",
        "interval_minutes": 60,
        "category": "python",
    },
    "npm_recent": {
        "type": "rss",
        "url": "https://github.com/npm/npm/releases.atom",
        "interval_minutes": 120,
        "category": "javascript",
    },

    # Tech News
    "techcrunch": {
        "type": "rss",
        "url": "https://techcrunch.com/feed/",
        "interval_minutes": 120,
        "category": "news",
    },
    "ars_technica": {
        "type": "rss",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "interval_minutes": 120,
        "category": "news",
    },
    "the_verge": {
        "type": "rss",
        "url": "https://www.theverge.com/rss/index.xml",
        "interval_minutes": 120,
        "category": "news",
    },
}


# ─── HTTP Client (zéro dépendance) ────────────────────────────────

class HttpClient:
    """Client HTTP simple avec rotation User-Agent et retry."""

    USER_AGENTS = [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]

    def __init__(self):
        self._ua_index = 0
        self._lock = threading.Lock()

    def _get_ua(self) -> str:
        with self._lock:
            ua = self.USER_AGENTS[self._ua_index % len(self.USER_AGENTS)]
            self._ua_index += 1
            return ua

    def fetch(self, url: str, timeout: int = 30, use_ua: bool = False) -> Optional[str]:
        headers = {}
        if use_ua:
            headers["User-Agent"] = self._get_ua()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            log.debug(f"HTTP {e.code} for {url}")
            return None
        except urllib.error.URLError as e:
            log.debug(f"URL error for {url}: {e}")
            return None
        except Exception as e:
            log.debug(f"Error fetching {url}: {e}")
            return None

    def fetch_json(self, url: str, timeout: int = 30) -> Optional[dict]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None


http_client = HttpClient()


# ─── Fetchers par type ─────────────────────────────────────────────

def fetch_rss(source: dict) -> list:
    """Fetch et parse un flux RSS/Atom."""
    content = http_client.fetch(source["url"], timeout=30)
    if not content:
        return []

    items = []
    try:
        root = ET.fromstring(content)
        # RSS 2.0
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")

            # Nettoyer le HTML de la description
            clean_desc = re.sub(r'<[^>]+>', '', desc).strip()[:500]

            items.append({
                "title": title.strip(),
                "url": link.strip(),
                "description": clean_desc,
                "published": pub_date,
                "source": source["url"],
                "category": source["category"],
            })

        # Atom
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            desc = entry.findtext("{http://www.w3.org/2005/Atom}summary", "")
            updated = entry.findtext("{http://www.w3.org/2005/Atom}updated", "")

            clean_desc = re.sub(r'<[^>]+>', '', desc).strip()[:500]

            items.append({
                "title": title.strip(),
                "url": link.strip(),
                "description": clean_desc,
                "published": updated,
                "source": source["url"],
                "category": source["category"],
            })
    except ET.ParseError as e:
        log.warning(f"XML parse error for {source['url']}: {e}")
    except Exception as e:
        log.warning(f"Error parsing RSS {source['url']}: {e}")

    return items[:CONFIG["max_items_per_source"]]


def fetch_hackernews(source: dict) -> list:
    """Fetch les top stories depuis l'API HN."""
    story_ids = http_client.fetch_json(source["url"], timeout=15)
    if not story_ids:
        return []

    items = []
    for sid in story_ids[:30]:
        item = http_client.fetch_json(f"{source['api_base']}{sid}.json", timeout=10)
        if item:
            items.append({
                "title": item.get("title", ""),
                "url": item.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                "description": item.get("text", "")[:500] if item.get("text") else "",
                "published": datetime.fromtimestamp(item.get("time", 0)).isoformat(),
                "score": item.get("score", 0),
                "comments": item.get("descendants", 0),
                "source": "hackernews",
                "category": source["category"],
                "hn_id": sid,
            })
        time.sleep(0.1)  # Rate limit

    return items


def fetch_github_trending(source: dict) -> list:
    """Parse la page GitHub Trending."""
    content = http_client.fetch(source["url"], timeout=30, use_ua=True)
    if not content:
        return []

    items = []
    # Parser le HTML — patterns simples pour GitHub
    repo_pattern = re.compile(
        r'<h2[^>]*class="h3 lh-condensed"[^>]*>.*?<a[^>]*href="(/[^"]+)"[^>]*>([^<]+)</a>',
        re.DOTALL
    )
    desc_pattern = re.compile(
        r'<p[^>]*class="col-9[^"]*"[^>]*>([^<]+)</p>',
        re.DOTALL
    )
    lang_pattern = re.compile(
        r'<span[^>]*itemprop="programmingLanguage"[^>]*>([^<]+)</span>'
    )
    star_pattern = re.compile(
        r'<a[^>]*href="[^"]+/stargazers"[^>]*>\s*([\d,.]+)\s*</a>',
        re.DOTALL
    )

    repos = repo_pattern.findall(content)
    descs = desc_pattern.findall(content)
    langs = lang_pattern.findall(content)
    stars = star_pattern.findall(content)

    for i, (path, name) in enumerate(repos[:25]):
        full_path = path.strip().lstrip("/")
        items.append({
            "title": f" trending: {full_path}",
            "url": f"https://github.com{path.strip()}",
            "description": descs[i].strip() if i < len(descs) else "",
            "published": datetime.now().isoformat(),
            "language": langs[i].strip() if i < len(langs) else "",
            "stars": stars[i].strip() if i < len(stars) else "",
            "source": "github_trending",
            "category": "github",
        })

    return items


def fetch_generic(source: dict) -> list:
    """Fetch générique pour les sources HTML non-RSS."""
    content = http_client.fetch(source["url"], timeout=30, use_ua=True)
    if not content:
        return []

    # Simple extraction de titres et liens
    items = []
    link_pattern = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', re.DOTALL)
    matches = link_pattern.findall(content)

    for url, title in matches[:CONFIG["max_items_per_source"]]:
        title = title.strip()
        url = url.strip()
        if title and len(title) > 15 and not url.startswith("#") and "javascript:" not in url:
            items.append({
                "title": title[:200],
                "url": url if url.startswith("http") else f"https://github.com{url}",
                "description": "",
                "published": datetime.now().isoformat(),
                "source": source["url"],
                "category": source["category"],
            })

    return items


# ─── Ollama Embeddings ─────────────────────────────────────────────

def get_embedding(text: str) -> list:
    """Génère un embedding via Ollama local."""
    try:
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
    except Exception as e:
        log.debug(f"Embedding failed: {e}")
        return []


# ─── Scoring & Classification ─────────────────────────────────────

HERMES_KEYWORDS = [
    "ollama", "llama", "qwen", "mistral", "deepseek", "gemma", "phi",
    "docker", "kubernetes", "container", "compose",
    "next.js", "react", "typescript", "node", "python",
    "postgres", "redis", "qdrant", "vector", "embedding",
    "playwright", "puppeteer", "selenium", "scraping",
    "self-hosted", "selfhosted", "open-source",
    "ai agent", "autonomous", "llm", "rag", "fine-tuning",
    "n8n", "workflow", "automation",
    "fastapi", "flask", "express", "nginx",
    "supabase", "sqlite", "minio",
    "whisper", "tts", "speech", "vision",
    "pytorch", "tensorflow", "huggingface",
    "linux", "ubuntu", "hetzner", "vps",
]

def score_signal(item: dict) -> dict:
    """Score un signal tech selon sa pertinence pour HERMES."""
    title_lower = (item.get("title", "") + " " + item.get("description", "")).lower()
    text_lower = title_lower.lower()

    # Pertinence HERMES (0-50)
    hermes_score = sum(5 for kw in HERMES_KEYWORDS if kw in text_lower)
    hermes_score = min(hermes_score, 50)

    # Fraîcheur (0-30)
    published = item.get("published", "")
    freshness = 0
    if published:
        try:
            if isinstance(published, str):
                # Parser différents formats de date
                for fmt in ["%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S", "%Y-%m-%d"]:
                    try:
                        dt = datetime.strptime(published[:25], fmt)
                        age_hours = (datetime.now() - dt).total_seconds() / 3600
                        freshness = max(0, 30 - int(age_hours / 8))
                        break
                    except ValueError:
                        continue
        except Exception:
            pass

    # Popularité (0-20)
    popularity = 0
    score_hn = item.get("score", 0)
    if score_hn:
        popularity = min(20, int(score_hn / 50))
    stars = item.get("stars", "")
    if stars:
        try:
            star_count = int(stars.replace(",", "").replace(".", ""))
            popularity = min(20, int(star_count / 500))
        except ValueError:
            pass

    total = hermes_score + freshness + popularity

    # Classification
    if total >= 40:
        level = "critical"
    elif total >= 25:
        level = "important"
    elif total >= 10:
        level = "notable"
    else:
        level = "info"

    return {
        "total": total,
        "hermes_relevance": hermes_score,
        "freshness": freshness,
        "popularity": popularity,
        "level": level,
    }


# ─── Data Storage ──────────────────────────────────────────────────

def item_hash(item: dict) -> str:
    """Hash unique pour déduplication."""
    key = f"{item.get('url', '')}{item.get('title', '')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def load_existing_hashes() -> set:
    """Charge les hashes existants pour déduplication."""
    data_file = Path(CONFIG["data_file"])
    hashes = set()
    if data_file.exists():
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            hashes.add(entry.get("hash", ""))
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            log.warning(f"Error loading existing data: {e}")
    return hashes


def save_signal(item: dict, score: dict):
    """Sauvegarde un signal dans le fichier JSONL."""
    entry = {
        "hash": item_hash(item),
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "description": item.get("description", "")[:500],
        "category": item.get("category", ""),
        "source": item.get("source", ""),
        "published": item.get("published", ""),
        "score": score,
        "detected_at": datetime.now().isoformat(),
    }

    data_file = Path(CONFIG["data_file"])
    data_file.parent.mkdir(parents=True, exist_ok=True)

    with open(data_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def rotate_data():
    """Supprime les entrées de plus de retention_days."""
    data_file = Path(CONFIG["data_file"])
    if not data_file.exists():
        return

    cutoff = (datetime.now() - timedelta(days=CONFIG["retention_days"])).isoformat()
    kept = []

    try:
        with open(data_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        if entry.get("detected_at", "") > cutoff:
                            kept.append(line)
                    except json.JSONDecodeError:
                        continue
    except Exception:
        return

    if len(kept) < sum(1 for _ in open(data_file) if _.strip()):
        with open(data_file, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        log.info(f"Data rotated, kept {len(kept)} entries")


# ─── Fetcher Router ───────────────────────────────────────────────

def fetch_source(source: dict) -> list:
    """Route vers le bon fetcher selon le type."""
    source_type = source.get("type", "rss")

    if source_type == "rss":
        return fetch_rss(source)
    elif source_type == "api" and "hacker-news" in source.get("url", ""):
        return fetch_hackernews(source)
    elif source_type == "html":
        return fetch_github_trending(source)
    else:
        return fetch_generic(source)


# ─── Scanner Loop ─────────────────────────────────────────────────

class TechWatcher:
    """Scanner principal de veille technologique."""

    def __init__(self):
        self.running = False
        self.stats = {
            "total_scanned": 0,
            "total_new": 0,
            "by_source": {},
            "by_level": {"critical": 0, "important": 0, "notable": 0, "info": 0},
            "last_scan": None,
        }
        self._last_fetch = {}  # source_name -> timestamp
        self.known_hashes = load_existing_hashes()
        log.info(f"TechWatcher initialized with {len(self.known_hashes)} known signals")

    def scan_all(self):
        """Scan complet de toutes les sources."""
        self.stats["last_scan"] = datetime.now().isoformat()
        new_count = 0
        total_count = 0

        for source_name, source_config in SOURCES.items():
            # Vérifier l'intervalle
            last = self._last_fetch.get(source_name, 0)
            interval = source_config.get("interval_minutes", 60) * 60
            if time.time() - last < interval:
                continue

            try:
                items = fetch_source(source_config)
                self._last_fetch[source_name] = time.time()

                for item in items:
                    total_count += 1
                    h = item_hash(item)

                    if h not in self.known_hashes:
                        score = score_signal(item)
                        save_signal(item, score)
                        self.known_hashes.add(h)
                        new_count += 1

                        self.stats["by_level"][score["level"]] = \
                            self.stats["by_level"].get(score["level"], 0) + 1

                self.stats["by_source"][source_name] = \
                    self.stats["by_source"].get(source_name, 0) + len(items)

                log.info(f"Scanned {source_name}: {len(items)} items, "
                         f"{sum(1 for i in items if item_hash(i) not in self.known_hashes)} new")

            except Exception as e:
                log.error(f"Error scanning {source_name}: {e}")

        self.stats["total_scanned"] += total_count
        self.stats["total_new"] += new_count

        log.info(f"Scan complete: {total_count} items, {new_count} new signals")

        # Rotation quotidienne
        if datetime.now().hour == 3:  # 3h du matin
            rotate_data()

        return new_count

    def get_recent(self, hours: int = 24, level: str = None) -> list:
        """Récupère les signaux récents."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        results = []

        data_file = Path(CONFIG["data_file"])
        if not data_file.exists():
            return results

        with open(data_file, "r", encoding="utf-8") as f:
            for line in reversed(list(f)):
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        if entry.get("detected_at", "") >= cutoff:
                            if level is None or entry.get("score", {}).get("level") == level:
                                results.append(entry)
                    except json.JSONDecodeError:
                        continue

        return results[:100]

    def get_summary(self) -> dict:
        """Génère un résumé de la veille."""
        recent = self.get_recent(hours=24)
        return {
            "timestamp": datetime.now().isoformat(),
            "total_sources": len(SOURCES),
            "active_sources": len(self._last_fetch),
            "signals_last_24h": len(recent),
            "by_level": {
                level: len([r for r in recent if r.get("score", {}).get("level") == level])
                for level in ["critical", "important", "notable", "info"]
            },
            "top_signals": sorted(recent, key=lambda r: r.get("score", {}).get("total", 0), reverse=True)[:10],
            "stats": self.stats,
        }


watcher = TechWatcher()


# ─── API REST FastAPI ─────────────────────────────────────────────

# Application FastAPI pour les endpoints REST
app_fastapi = FastAPI(
    title="HERMES OMEGA — Tech Watcher",
    description="API de veille technologique",
    version="1.0.0",
)


@app_fastapi.get("/health")
async def api_health():
    """Vérification de santé du service."""
    return {"health": "ok"}


@app_fastapi.get("/status")
async def api_status():
    """Statut du watcher et nombre de sources configurées."""
    return {
        "status": "ok",
        "sources": len(SOURCES),
        "active_sources": len(watcher._last_fetch),
        "running": watcher.running,
    }


@app_fastapi.get("/results")
async def api_results(hours: int = 24, level: Optional[str] = None):
    """Retourne les derniers résultats du scan."""
    results = watcher.get_recent(hours=hours, level=level)
    return {
        "count": len(results),
        "results": results,
    }


# ─── HTTP API ──────────────────────────────────────────────────────

class TechWatcherHandler(BaseHTTPRequestHandler):

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
            self._send_json(200, {"status": "ok", "version": "1.0.0"})

        elif path == "/api/signals":
            hours = int(params.get("hours", [24])[0])
            level = params.get("level", [None])[0]
            signals = watcher.get_recent(hours=hours, level=level)
            self._send_json(200, {"signals": signals, "count": len(signals)})

        elif path == "/api/summary":
            self._send_json(200, watcher.get_summary())

        elif path == "/api/stats":
            self._send_json(200, watcher.stats)

        elif path == "/api/sources":
            self._send_json(200, {
                "sources": {k: {"type": v["type"], "category": v["category"],
                                "interval_minutes": v["interval_minutes"]}
                           for k, v in SOURCES.items()},
                "last_fetch": {k: datetime.fromtimestamp(v).isoformat()
                              for k, v in watcher._last_fetch.items()},
            })

        elif path == "/api/scan":
            new = watcher.scan_all()
            self._send_json(200, {"new_signals": new})

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/scan":
            new = watcher.scan_all()
            self._send_json(200, {"new_signals": new, "stats": watcher.stats})
        else:
            self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        log.debug(f"{self.client_address[0]} - {format % args}")


# ─── Background Scanner Thread ─────────────────────────────────────

def background_scanner():
    """Thread de scan en arrière-plan."""
    while watcher.running:
        try:
            watcher.scan_all()
        except Exception as e:
            log.error(f"Background scan error: {e}")
        time.sleep(CONFIG["check_interval_minutes"] * 60)


# ─── Main ──────────────────────────────────────────────────────────

def main():
    """Point d'entrée principal — lance le scanner en arrière-plan + API FastAPI."""
    host = "127.0.0.1"
    port = 9301

    # Log rotation
    log_file = Path(CONFIG["log_file"])
    if log_file.exists() and log_file.stat().st_size > CONFIG["max_log_mb"] * 1024 * 1024:
        backup = log_file.with_suffix(f".{int(time.time())}.log")
        log_file.rename(backup)

    # Démarrage du scanner en arrière-plan
    watcher.running = True
    scanner_thread = threading.Thread(target=background_scanner, daemon=True)
    scanner_thread.start()
    log.info("Background scanner started")

    # Scan initial — commenté : le background_scanner s'en occupe au démarrage
    # watcher.scan_all()
    log.info("Initial scan deferred to background scanner")

    # Serveur FastAPI via uvicorn (port 9301 par défaut)
    log.info(f"TECH WATCHER (FastAPI) listening on {host}:{port}")
    try:
        uvicorn.run(app_fastapi, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        watcher.running = False
        log.info("TECH WATCHER shutting down")


if __name__ == "__main__":
    main()
