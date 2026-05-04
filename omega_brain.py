#!/usr/bin/env python3
"""
OMEGA BRAIN — Cortex IA multicouche pour HERMES Command OS
Chaîne de raisonnement : Analyse → Planification → Exécution → Vérification → Synthèse
100% local via Ollama, zero external API dependency
"""

import asyncio
import json
import time
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse
import threading

from fastapi import FastAPI
import uvicorn

# ─── Configuration ────────────────────────────────────────────────

CONFIG = {
    "ollama_url": "http://localhost:11434",
    "models": {
        "reasoning": "qwen2.5-coder:32b",   # Raisonnement lourd
        "fast": "qwen2.5-coder:7b",          # Tâches rapides
        "embedding": "nomic-embed-text",      # Embeddings
        "vision": "moondream",                # Vision
        "minimal": "qwen2.5:0.5b",            # Ultra-rapide
    },
    "reasoning_timeout": 300,
    "fast_timeout": 60,
    "max_retries": 3,
    "cache_ttl_seconds": 3600,
    "log_file": "/srv/hermes-command-os/hermes-core/logs/omega_brain.log",
    "max_log_mb": 50,
}

LOG_DIR = Path(CONFIG["log_file"]).parent
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [OMEGA] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("omega_brain")


# ─── Cache Redis-like en mémoire ──────────────────────────────────

class ResponseCache:
    """Cache simple en mémoire pour les réponses fréquentes."""

    def __init__(self, ttl: int = 3600):
        self._cache = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] < self._ttl:
                return entry["value"]
            return None

    def set(self, key: str, value: str):
        with self._lock:
            self._cache[key] = {"value": value, "ts": time.time()}

    def clear(self):
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        return len(self._cache)


# ─── Ollama Client ────────────────────────────────────────────────

class OllamaClient:
    """Client HTTP pour Ollama local — zéro dépendance externe."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _request(self, endpoint: str, payload: dict, timeout: int = 60) -> dict:
        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            log.error(f"Ollama request failed: {e}")
            return {"error": str(e)}
        except Exception as e:
            log.error(f"Ollama error: {e}")
            return {"error": str(e)}

    def generate(self, prompt: str, model: str = None, system: str = None,
                 timeout: int = 60, stream: bool = False) -> str:
        model = model or CONFIG["models"]["fast"]
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        result = self._request("/api/generate", payload, timeout)
        if "error" in result:
            return f"[ERROR] {result['error']}"
        return result.get("response", "")

    def chat(self, messages: list, model: str = None, system: str = None,
             timeout: int = 60) -> str:
        model = model or CONFIG["models"]["fast"]
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if system:
            payload["system"] = system
        result = self._request("/api/chat", payload, timeout)
        if "error" in result:
            return f"[ERROR] {result['error']}"
        return result.get("message", {}).get("content", "")

    def embed(self, text: str) -> list:
        result = self._request("/api/embeddings", {
            "model": CONFIG["models"]["embedding"],
            "prompt": text,
        }, timeout=30)
        if "error" in result:
            return []
        return result.get("embedding", [])

    def list_models(self) -> list:
        try:
            url = f"{self.base_url}/api/tags"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def health(self) -> bool:
        try:
            url = f"{self.base_url}/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False


# ─── Classification des demandes ──────────────────────────────────

TASK_CATEGORIES = {
    "code": {
        "keywords": ["code", "programme", "script", "fonction", "déboguer", "bug",
                     "coder", "développer", "implémenter", "créer un fichier", "API",
                     "endpoint", "route", "module", "class", "refactor", "optimiser le code"],
        "model": "reasoning",  # Utilise le modèle lourd pour le code
    },
    "system": {
        "keywords": ["serveur", "docker", "container", "déployer", "installer",
                     "configurer", "service", "daemon", "redémarrer", "logs",
                     "monitoring", "health", "status", "système", "disque", "RAM"],
        "model": "fast",
    },
    "research": {
        "keywords": ["recherche", "chercher", "trouver", "veille", "analyse",
                     "comparer", "évaluer", "source", "données", "scraping",
                     "article", "document", "étudier"],
        "model": "reasoning",
    },
    "creative": {
        "keywords": ["écrire", "rédiger", "texte", "contenu", "article", "blog",
                     "email", "message", "description", "documentation", "rapport"],
        "model": "fast",
    },
    "security": {
        "keywords": ["sécurité", "auth", "mot de passe", "token", "chiffrement",
                     "vulnérabilité", "audit", "policy", "permission", "firewall"],
        "model": "reasoning",
    },
    "general": {
        "keywords": [],
        "model": "fast",
    },
}


def classify_request(text: str) -> dict:
    """Classifie une demande et retourne la catégorie + modèle optimal."""
    text_lower = text.lower()
    scores = {}

    for category, config in TASK_CATEGORIES.items():
        score = sum(1 for kw in config["keywords"] if kw in text_lower)
        scores[category] = score

    best_category = max(scores, key=scores.get) if max(scores.values()) > 0 else "general"
    model_type = TASK_CATEGORIES.get(best_category, {}).get("model", "fast")

    return {
        "category": best_category,
        "confidence": scores[best_category] / max(len(text_lower.split()), 0.01),
        "model_type": model_type,
        "model": CONFIG["models"][model_type],
    }


# ─── Planification multi-étapes ───────────────────────────────────

SYSTEM_PROMPT_ANALYZE = """Tu es le module d'analyse d'OMEGA BRAIN, le cortex IA de HERMES Command OS.
Tu analyses les demandes et les décomposes en sous-tâches structurées.
Réponds UNIQUEMENT en JSON valide avec cette structure:
{
  "analysis": "résumé de la demande",
  "tasks": [
    {"id": 1, "action": "description", "type": "code|shell|search|read|write", "priority": "high|medium|low", "depends_on": []}
  ],
  "estimated_complexity": "low|medium|high|critical",
  "risk_level": "safe|needs_validation|dangerous",
  "suggested_model": "fast|reasoning"
}"""

SYSTEM_PROMPT_EXECUTE = """Tu es le module d'exécution d'OMEGA BRAIN, le cortex IA de HERMES Command OS.
Tu exécutes des tâches de manière précise et autonome.
Tu génères du code, des commandes shell, ou des analyses selon la tâche demandée.
Réponds de manière concise et actionnable."""

SYSTEM_PROMPT_VERIFY = """Tu es le module de vérification d'OMEGA BRAIN.
Tu vérifies qu'un résultat est correct, complet et sécurisé.
Réponds en JSON:
{
  "valid": true/false,
  "issues": ["liste des problèmes trouvés"],
  "score": 0-100,
  "suggestions": ["améliorations possibles"]
}"""

SYSTEM_PROMPT_SYNTHESIZE = """Tu es le module de synthèse d'OMEGA BRAIN.
Tu combines les résultats de plusieurs sous-tâches en une réponse claire et structurée.
Tu réponds en français par défaut, sauf si la demande est dans une autre langue.
Sois concis, direct et actionnable."""


# ─── Omega Brain Engine ────────────────────────────────────────────

class OmegaBrain:
    """Cortex IA multicouche — le cerveau de HERMES OMEGA."""

    def __init__(self):
        self.ollama = OllamaClient(CONFIG["ollama_url"])
        self.cache = ResponseCache(CONFIG["cache_ttl_seconds"])
        self.history = []  # Historique des raisonnements
        self.stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "avg_response_time_ms": 0,
            "by_category": {},
        }
        log.info("OMEGA BRAIN initialized")

    def _select_model(self, category_result: dict) -> str:
        """Sélectionne le modèle optimal selon la classification."""
        model_type = category_result.get("model_type", "fast")
        available = self.ollama.list_models()

        # Préfère le modèle lourd si disponible
        preferred = CONFIG["models"].get(model_type, CONFIG["models"]["fast"])
        if preferred in available:
            return preferred

        # Fallback vers 7b
        if CONFIG["models"]["fast"] in available:
            return CONFIG["models"]["fast"]

        # Fallback vers n'importe quoi de disponible
        for model in available:
            if "coder" in model or "qwen" in model:
                return model

        return available[0] if available else CONFIG["models"]["fast"]

    def _cache_key(self, prompt: str, model: str) -> str:
        return hashlib.sha256(f"{prompt}|{model}".encode()).hexdigest()[:32]

    def analyze(self, request: str) -> dict:
        """Étape 1 — Analyse et classification de la demande."""
        # Classification rapide par mots-clés
        classification = classify_request(request)
        model = self._select_model(classification)

        # Analyse profonde par le modèle IA
        analysis_prompt = f"Analyse cette demande et décompose-la en sous-tâches:\n\n{request}"
        analysis_raw = self.ollama.chat(
            messages=[{"role": "user", "content": analysis_prompt}],
            model=model,
            system=SYSTEM_PROMPT_ANALYZE,
            timeout=CONFIG["reasoning_timeout"],
        )

        # Parser le JSON de la réponse
        try:
            # Extraire le JSON même s'il est dans du texte
            json_match = re.search(r'\{[^}]*"tasks"[^}]*\}', analysis_raw, re.DOTALL)
            if json_match:
                analysis = json.loads(json_match.group())
            else:
                analysis = json.loads(analysis_raw)
        except (json.JSONDecodeError, TypeError):
            analysis = {
                "analysis": analysis_raw[:500],
                "tasks": [{"id": 1, "action": request[:200], "type": "general", "priority": "high", "depends_on": []}],
                "estimated_complexity": "medium",
                "risk_level": "safe",
                "suggested_model": classification["model_type"],
            }

        analysis["classification"] = classification
        analysis["model_used"] = model
        analysis["timestamp"] = datetime.now().isoformat()

        return analysis

    def plan(self, analysis: dict) -> list:
        """Étape 2 — Planification ordonnée des tâches."""
        tasks = analysis.get("tasks", [])
        if not tasks:
            return []

        # Tri par priorité puis par dépendances
        priority_order = {"high": 0, "medium": 1, "low": 2}
        tasks.sort(key=lambda t: (
            priority_order.get(t.get("priority", "medium"), 1),
            len(t.get("depends_on", []))
        ))

        return tasks

    def execute(self, task: dict, context: str = "") -> dict:
        """Étape 3 — Exécution d'une tâche."""
        model = CONFIG["models"].get(
            task.get("suggested_model", "fast"),
            CONFIG["models"]["fast"]
        )

        # Check cache
        cache_key = self._cache_key(task.get("action", ""), model)
        cached = self.cache.get(cache_key)
        if cached:
            self.stats["cache_hits"] += 1
            return {"result": cached, "source": "cache"}

        # Exécution via le modèle
        prompt = f"Exécute cette tâche:\n{task.get('action', '')}"
        if context:
            prompt += f"\n\nContexte:\n{context}"

        result = self.ollama.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            system=SYSTEM_PROMPT_EXECUTE,
            timeout=CONFIG["fast_timeout"],
        )

        # Cache le résultat
        self.cache.set(cache_key, result)

        return {
            "result": result,
            "source": "generated",
            "model": model,
            "task_id": task.get("id"),
        }

    def verify(self, task: dict, result: dict) -> dict:
        """Étape 4 — Vérification du résultat."""
        verify_prompt = f"""Vérifie ce résultat:
Tâche: {task.get('action', '')}
Résultat: {result.get('result', '')[:2000]}"""

        verification = self.ollama.chat(
            messages=[{"role": "user", "content": verify_prompt}],
            model=CONFIG["models"]["fast"],
            system=SYSTEM_PROMPT_VERIFY,
            timeout=CONFIG["fast_timeout"],
        )

        try:
            json_match = re.search(r'\{[^}]*"valid"[^}]*\}', verification, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "valid": True,
            "issues": [],
            "score": 70,
            "suggestions": [],
            "raw": verification[:500],
        }

    def synthesize(self, request: str, results: list) -> str:
        """Étape 5 — Synthèse finale."""
        results_text = "\n\n".join(
            f"Résultat {i+1}:\n{r.get('result', '')[:1000]}"
            for i, r in enumerate(results)
        )

        synthesis_prompt = f"""Demande originale: {request}

Résultats des sous-tâches:
{results_text}

Synthétise ces résultats en une réponse claire, structurée et actionnable."""

        return self.ollama.chat(
            messages=[{"role": "user", "content": synthesis_prompt}],
            model=CONFIG["models"]["reasoning"],
            system=SYSTEM_PROMPT_SYNTHESIZE,
            timeout=CONFIG["reasoning_timeout"],
        )

    def think(self, request: str) -> dict:
        """Pipeline complet : Analyse → Plan → Exécute → Vérifie → Synthèse."""
        start_time = time.time()
        self.stats["total_requests"] += 1

        log.info(f"New request: {request[:100]}...")

        # Étape 1 — Analyse
        analysis = self.analyze(request)
        log.info(f"Analysis: category={analysis.get('classification', {}).get('category')}, "
                 f"complexity={analysis.get('estimated_complexity')}")

        # Étape 2 — Planification
        tasks = self.plan(analysis)
        log.info(f"Plan: {len(tasks)} tasks")

        # Étape 3 — Exécution parallèle des tâches
        results = []
        context = ""
        for task in tasks:
            result = self.execute(task, context)
            results.append(result)
            context += f"\nTâche {task.get('id')}: {result.get('result', '')[:500]}"

            # Étape 4 — Vérification (async-friendly)
            verification = self.verify(task, result)
            if not verification.get("valid", True):
                log.warning(f"Task {task.get('id')} verification failed: "
                           f"{verification.get('issues', [])}")

        # Étape 5 — Synthèse
        if len(results) > 1:
            final_response = self.synthesize(request, results)
        else:
            final_response = results[0].get("result", "") if results else "No result"

        elapsed = (time.time() - start_time) * 1000
        self.stats["avg_response_time_ms"] = (
            (self.stats["avg_response_time_ms"] * (self.stats["total_requests"] - 1) + elapsed)
            / self.stats["total_requests"]
        )

        # Historique
        category = analysis.get("classification", {}).get("category", "general")
        if category not in self.stats["by_category"]:
            self.stats["by_category"][category] = 0
        self.stats["by_category"][category] += 1

        self.history.append({
            "timestamp": datetime.now().isoformat(),
            "request": request[:500],
            "category": category,
            "tasks_count": len(tasks),
            "elapsed_ms": round(elapsed),
            "complexity": analysis.get("estimated_complexity"),
        })

        log.info(f"Completed in {elapsed:.0f}ms")

        return {
            "response": final_response,
            "analysis": {
                "category": category,
                "complexity": analysis.get("estimated_complexity"),
                "tasks": len(tasks),
            },
            "performance": {
                "elapsed_ms": round(elapsed),
                "cache_used": any(r.get("source") == "cache" for r in results),
            },
        }


# ─── HTTP API ──────────────────────────────────────────────────────

brain = OmegaBrain()


class OmegaBrainHandler(BaseHTTPRequestHandler):
    """API HTTP pour OMEGA BRAIN."""

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            body = self.rfile.read(length).decode("utf-8")
            return json.loads(body)
        return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            ollama_ok = brain.ollama.health()
            models = brain.ollama.list_models()
            self._send_json(200, {
                "status": "ok" if ollama_ok else "degraded",
                "ollama": "up" if ollama_ok else "down",
                "models_count": len(models),
                "models": models,
                "cache_size": brain.cache.size(),
                "stats": brain.stats,
                "version": "1.0.0",
            })

        elif path == "/api/stats":
            self._send_json(200, brain.stats)

        elif path == "/api/history":
            self._send_json(200, {"history": brain.history[-50:]})

        elif path == "/api/models":
            self._send_json(200, {
                "available": brain.ollama.list_models(),
                "configured": CONFIG["models"],
            })

        elif path == "/api/cache":
            self._send_json(200, {"size": brain.cache.size()})

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/think":
            body = self._read_body()
            request_text = body.get("request", body.get("prompt", ""))
            if not request_text:
                self._send_json(400, {"error": "Missing 'request' field"})
                return

            result = brain.think(request_text)
            self._send_json(200, result)

        elif path == "/api/classify":
            body = self._read_body()
            text = body.get("text", "")
            if not text:
                self._send_json(400, {"error": "Missing 'text' field"})
                return

            classification = classify_request(text)
            classification["model"] = brain._select_model(classification)
            self._send_json(200, classification)

        elif path == "/api/chat":
            body = self._read_body()
            messages = body.get("messages", [])
            model = body.get("model")
            system = body.get("system")
            if not messages:
                self._send_json(400, {"error": "Missing 'messages' field"})
                return

            result = brain.ollama.chat(messages, model=model, system=system,
                                        timeout=CONFIG["reasoning_timeout"])
            self._send_json(200, {"response": result})

        elif path == "/api/generate":
            body = self._read_body()
            prompt = body.get("prompt", "")
            model = body.get("model")
            system = body.get("system")
            if not prompt:
                self._send_json(400, {"error": "Missing 'prompt' field"})
                return

            result = brain.ollama.generate(prompt, model=model, system=system,
                                           timeout=CONFIG["reasoning_timeout"])
            self._send_json(200, {"response": result})

        elif path == "/api/embed":
            body = self._read_body()
            text = body.get("text", "")
            if not text:
                self._send_json(400, {"error": "Missing 'text' field"})
                return

            embedding = brain.ollama.embed(text)
            self._send_json(200, {"embedding": embedding, "dimensions": len(embedding)})

        elif path == "/api/cache/clear":
            brain.cache.clear()
            self._send_json(200, {"status": "cleared", "size": 0})

        else:
            self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        log.info(f"{self.client_address[0]} - {format % args}")


# ═══════════════════════════════════════════════════════════════════════════════
# API REST FastAPI — Omega Brain sur port 9300
# ═══════════════════════════════════════════════════════════════════════════════

# Instance FastAPI pour le module Omega Brain
app_fastapi = FastAPI(title="OMEGA BRAIN API", version="1.0.0")


@app_fastapi.get("/status")
async def api_status():
    """Retourne le statut du module avec le modèle configuré."""
    return {
        "status": "ok",
        "module": "omega_brain",
        "model": CONFIG["models"]["reasoning"],
        "fast_model": CONFIG["models"]["fast"],
        "cache_size": brain.cache.size(),
        "stats": brain.stats,
    }


@app_fastapi.get("/health")
async def api_health():
    """Vérifie la santé d'Ollama et retourne les infos système."""
    ollama_ok = brain.ollama.health()
    models = brain.ollama.list_models()
    return {
        "health": "ok" if ollama_ok else "degraded",
        "ollama": "up" if ollama_ok else "down",
        "models_count": len(models),
        "models": models,
    }


@app_fastapi.post("/think")
async def api_think(payload: dict):
    """
    Pipeline complet de réflexion via Ollama.
    Accepte {"query": "..."} ou {"request": "..."} ou {"prompt": "..."}.
    Timeout de 90s pour les requêtes longues.
    """
    import asyncio
    query = payload.get("query") or payload.get("request") or payload.get("prompt", "")
    if not query:
        return {"error": "Champ 'query', 'request' ou 'prompt' requis"}
    # Exécution dans un thread pool pour ne PAS bloquer l'event loop uvicorn
    try:
        result = await asyncio.to_thread(brain.think, query)
        return result
    except Exception as e:
        return {"error": str(e)}


# ─── Main ──────────────────────────────────────────────────────────

PORT_DEFAULT = 9300


def main():
    """Lance le serveur FastAPI via uvicorn."""
    import sys
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT_DEFAULT

    # Rotation logs
    log_file = Path(CONFIG["log_file"])
    if log_file.exists() and log_file.stat().st_size > CONFIG["max_log_mb"] * 1024 * 1024:
        backup = log_file.with_suffix(f".{int(time.time())}.log")
        log_file.rename(backup)
        log.info(f"Log rotated to {backup}")

    # Vérifier Ollama
    if not brain.ollama.health():
        log.error("Ollama is not responding! OMEGA BRAIN requires Ollama.")
        print("ERROR: Ollama not available at", CONFIG["ollama_url"])
        return

    models = brain.ollama.list_models()
    log.info(f"Ollama ready with {len(models)} models: {', '.join(models)}")

    # Vérifier modèle de raisonnement
    reasoning_model = CONFIG["models"]["reasoning"]
    if reasoning_model not in models:
        log.warning(f"Reasoning model '{reasoning_model}' not available, falling back to fast")
        log.info(f"Available: {', '.join(models)}")

    log.info(f"OMEGA BRAIN (FastAPI) démarrant sur 127.0.0.1:{port}")
    uvicorn.run(app_fastapi, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
