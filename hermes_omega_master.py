"""
HERMES OMEGA — Orchestrateur Principal
======================================
Chef d'orchestre qui lance, surveille et coordonne les 7 modules HERMES OMEGA.
Point d'entrée unique du système.

Modules orchestrés:
  1. omega_brain.py     (port 9300) — cortex de raisonnement
  2. tech_watcher.py    (port 9301) — veille technologique
  3. scraper_engine.py  (port 9302) — moteur de scraping
  4. genesis.py         (port 9303) — création d'agents
  5. evolution.py       (port 9304) — auto-modification
  6. nexus_bus.py       (port 9305) — bus de messages
  7. knowledge_graph.py (port 9306) — graphe de connaissances

Port API orchestrateur: 9399
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except ImportError:
    uvicorn = None  # type: ignore[assignment]
    FastAPI = None  # type: ignore[assignment, misc]
    HTTPException = None  # type: ignore[assignment, misc]
    BaseModel = None  # type: ignore[assignment, misc]

# ─── Configuration ────────────────────────────────────────────────────────────

WORKSPACE = Path(__file__).parent.resolve()
HERMES_DIR = Path.home() / ".hermes-omega"
LOGS_DIR = HERMES_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

ORCHESTRATOR_PORT: int = 9399

# Dépendances externes (host:port)
DEPS: dict[str, tuple[str, int]] = {
    "redis": ("127.0.0.1", 6379),
    "qdrant": ("127.0.0.1", 6333),
    "ollama": ("127.0.0.1", 11434),
}

# Définition des modules (ordre de démarrage inverse = ordre d'arrêt)
MODULE_DEFS: list[dict[str, Any]] = [
    {"name": "nexus_bus",        "script": "nexus_bus.py",        "port": 9305, "critical": True},
    {"name": "omega_brain",      "script": "omega_brain.py",      "port": 9300, "critical": True},
    {"name": "tech_watcher",     "script": "tech_watcher.py",     "port": 9301, "critical": False},
    {"name": "scraper_engine",   "script": "scraper_engine.py",   "port": 9302, "critical": False},
    {"name": "genesis",          "script": "genesis.py",          "port": 9303, "critical": False},
    {"name": "evolution",        "script": "evolution.py",        "port": 9304, "critical": False},
    {"name": "knowledge_graph",  "script": "knowledge_graph.py",  "port": 9306, "critical": False},
]

HEALTH_INTERVAL: int = 30          # secondes entre les health checks
MAX_RESTARTS: int = 3              # max redémarrages par fenêtre
RESTART_WINDOW: int = 300          # fenêtre en secondes
SHUTDOWN_TIMEOUT: int = 5          # secondes avant SIGKILL

# ─── Logging ──────────────────────────────────────────────────────────────────

log_file = LOGS_DIR / f"orchestrator_{datetime.now():%Y%m%d}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hermes.omega")


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ModuleState:
    """État d'un module orchestré."""
    name: str
    script: str
    port: int
    critical: bool
    process: Optional[subprocess.Popen] = None
    status: str = "stopped"  # stopped | starting | running | error
    last_health: Optional[datetime] = None
    restart_times: list[float] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ─── Orchestrateur principal ─────────────────────────────────────────────────

class HermesOmega:
    """Orchestrateur central — le cerveau qui coordonne tous les modules."""

    def __init__(self) -> None:
        # Registre des modules name -> ModuleState
        self.modules: dict[str, ModuleState] = {}
        for mdef in MODULE_DEFS:
            self.modules[mdef["name"]] = ModuleState(
                name=mdef["name"],
                script=mdef["script"],
                port=mdef["port"],
                critical=mdef["critical"],
            )

        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False
        self._start_time: float = 0.0
        self._lock = asyncio.Lock()

    # ── Démarrage ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Démarre tous les modules dans l'ordre approprié."""
        if self._running:
            logger.warning("L'orchestrateur est déjà en cours d'exécution.")
            return

        self._running = True
        self._start_time = time.time()
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║       HERMES OMEGA — Démarrage          ║")
        logger.info("╚══════════════════════════════════════════╝")

        # 1. Vérification des dépendances externes
        await self._check_dependencies()

        # 2. Lancement du nexus_bus en premier (les autres en dépendent)
        nexus = self.modules["nexus_bus"]
        logger.info("▶ Démarrage du bus de messages (nexus_bus)…")
        await self._launch_module(nexus)
        await self._wait_ready(nexus, timeout=20)
        logger.info("✔ nexus_bus est prêt.")

        # 3. Lancement des 6 autres modules en parallèle
        others = [m for name, m in self.modules.items() if name != "nexus_bus"]
        logger.info(f"▶ Démarrage de {len(others)} modules en parallèle…")
        await asyncio.gather(*(self._launch_module(m) for m in others))

        # 4. Attente que tous soient prêts
        logger.info("▶ Vérification de la disponibilité des modules…")
        results = await asyncio.gather(
            *(self._wait_ready(m, timeout=30) for m in others),
            return_exceptions=True,
        )
        for m, r in zip(others, results):
            if isinstance(r, Exception):
                logger.error(f"✖ {m.name} n'a pas pu démarrer: {r}")
                m.status = "error"
            else:
                logger.info(f"✔ {m.name} est prêt.")

        # 5. Enregistrement des modules sur le nexus bus
        await self._register_modules_on_nexus()

        # 6. Lancement du heartbeat système
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 7. Affichage du statut complet
        self._print_status()
        logger.info("━━━ HERMES OMEGA — Opérationnel ━━━")

    # ── Arrêt ────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Arrête tous les modules proprement (ordre inverse du démarrage)."""
        if not self._running:
            return

        logger.info("━━━ HERMES OMEGA — Arrêt en cours ━━━")
        self._running = False

        # Annule le heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Arrêt dans l'ordre inverse (nexus_bus en dernier)
        reverse_order = list(reversed(MODULE_DEFS))
        for mdef in reverse_order:
            m = self.modules[mdef["name"]]
            if m.process and m.process.poll() is None:
                logger.info(f"■ Arrêt de {m.name}…")
                try:
                    m.process.terminate()
                    try:
                        m.process.wait(timeout=SHUTDOWN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"⚠ {m.name} ne répond pas — kill.")
                        m.process.kill()
                        m.process.wait(timeout=3)
                except ProcessLookupError:
                    pass
                m.status = "stopped"
                logger.info(f"✔ {m.name} arrêté.")

        self._print_status()
        logger.info("━━━ HERMES OMEGA — Arrêté ━━━")

    # ── Health check ─────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, dict[str, Any]]:
        """Vérifie tous les modules, retourne un dictionnaire de statuts."""
        results: dict[str, dict[str, Any]] = {}
        for name, m in self.modules.items():
            try:
                if aiohttp:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{m.base_url}/health", timeout=aiohttp.ClientTimeout(total=3)
                        ) as resp:
                            alive = resp.status == 200
                else:
                    alive = m.process is not None and m.process.poll() is None

                m.status = "running" if alive else "error"
                m.last_health = datetime.now()
                results[name] = {
                    "status": m.status,
                    "port": m.port,
                    "critical": m.critical,
                    "last_health": m.last_health.isoformat() if m.last_health else None,
                }
            except Exception as exc:
                m.status = "error"
                results[name] = {
                    "status": "error",
                    "port": m.port,
                    "critical": m.critical,
                    "error": str(exc),
                    "last_health": m.last_health.isoformat() if m.last_health else None,
                }
        return results

    # ── Commandes ────────────────────────────────────────────────────────

    async def process_command(self, command: str, **kwargs: Any) -> Any:
        """Interface de commande centralisée — route vers le bon module."""
        command = command.lower().strip()
        logger.info(f"Commande reçue: {command}")

        if command == "status":
            return await self.health_check()

        if command == "think":
            return await self._route_to_module("omega_brain", "/think", kwargs)

        if command == "search":
            return await self._route_to_module("knowledge_graph", "/search", kwargs)

        if command == "watch":
            return await self._route_to_module("tech_watcher", "/watch", kwargs)

        if command == "scrape":
            return await self._route_to_module("scraper_engine", "/scrape", kwargs)

        if command == "create_agent":
            return await self._route_to_module("genesis", "/create", kwargs)

        if command == "evolve":
            return await self._route_to_module("evolution", "/evolve", kwargs)

        if command == "ask":
            return await self._intelligent_ask(kwargs.get("question", ""))

        return {"error": f"Commande inconnue: {command}"}

    # ── Mode "ask" intelligent ───────────────────────────────────────────

    async def _intelligent_ask(self, question: str) -> dict[str, Any]:
        """Combine brain reasoning + knowledge search + web search."""
        if not question:
            return {"error": "Question vide."}

        logger.info(f"🧠 Ask intelligent: {question[:80]}…")

        # 1. Interroge le graphe de connaissances
        knowledge_result: Any = {}
        try:
            knowledge_result = await self._route_to_module(
                "knowledge_graph", "/search", {"query": question}
            )
        except Exception as exc:
            logger.warning(f"knowledge_graph indisponible: {exc}")

        # 2. Interroge le cortex de raisonnement avec le contexte
        brain_result: Any = {}
        try:
            brain_result = await self._route_to_module(
                "omega_brain", "/think", {
                    "task": question,
                    "context": knowledge_result,
                }
            )
        except Exception as exc:
            logger.warning(f"omega_brain indisponible: {exc}")

        return {
            "question": question,
            "knowledge": knowledge_result,
            "reasoning": brain_result,
            "timestamp": datetime.now().isoformat(),
        }

    # ── Redémarrage d'un module ──────────────────────────────────────────

    async def restart_module(self, name: str) -> dict[str, str]:
        """Redémarre un module spécifique."""
        if name not in self.modules:
            return {"error": f"Module inconnu: {name}"}

        m = self.modules[name]

        # Arrêt
        if m.process and m.process.poll() is None:
            m.process.terminate()
            try:
                m.process.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                m.process.kill()
                m.process.wait(timeout=3)

        # Redémarrage
        await self._launch_module(m)
        await self._wait_ready(m, timeout=20)

        return {"status": m.status, "module": name}

    # ── Méthodes internes ────────────────────────────────────────────────

    async def _check_dependencies(self) -> None:
        """Vérifie que Redis, Qdrant et Ollama sont joignables."""
        for dep_name, (host, port) in DEPS.items():
            try:
                if aiohttp:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"http://{host}:{port}/",
                            timeout=aiohttp.ClientTimeout(total=3),
                        ) as resp:
                            logger.info(f"✔ {dep_name} joignable ({host}:{port})")
                else:
                    logger.info(f"⚠ {dep_name}: vérification HTTP non disponible (aiohttp manquant)")
            except Exception:
                logger.warning(f"⚠ {dep_name} ({host}:{port}) injoignable — le système peut fonctionner en mode dégradé.")

    async def _launch_module(self, m: ModuleState) -> None:
        """Lance un module comme subprocess Python séparé."""
        script_path = WORKSPACE / m.script
        if not script_path.exists():
            logger.error(f"Script introuvable: {script_path}")
            m.status = "error"
            return

        m.status = "starting"
        try:
            # Flags de création Windows : cacher la fenêtre console
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

            m.process = subprocess.Popen(
                [sys.executable, str(script_path)],
                cwd=str(WORKSPACE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            logger.info(f"▶ {m.name} lancé (PID={m.process.pid}, port={m.port})")
        except Exception as exc:
            logger.error(f"✖ Impossible de lancer {m.name}: {exc}")
            m.status = "error"

    async def _wait_ready(self, m: ModuleState, timeout: int = 30) -> None:
        """Attend qu'un module soit prêt (health check)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if m.process and m.process.poll() is not None:
                raise RuntimeError(f"{m.name} a crashé (code={m.process.returncode})")
            try:
                if aiohttp:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{m.base_url}/health",
                            timeout=aiohttp.ClientTimeout(total=2),
                        ) as resp:
                            if resp.status == 200:
                                m.status = "running"
                                m.last_health = datetime.now()
                                return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(f"{m.name} n'est pas prêt après {timeout}s")

    async def _register_modules_on_nexus(self) -> None:
        """Enregistre chaque module sur le nexus bus."""
        nexus = self.modules["nexus_bus"]
        if nexus.status != "running" or not aiohttp:
            return

        try:
            async with aiohttp.ClientSession() as session:
                for name, m in self.modules.items():
                    if m.status == "running":
                        payload = {
                            "name": m.name,
                            "port": m.port,
                            "url": m.base_url,
                            "critical": m.critical,
                        }
                        async with session.post(
                            f"{nexus.base_url}/register", json=payload
                        ) as resp:
                            if resp.status in (200, 201):
                                logger.info(f"📡 {m.name} enregistré sur le nexus bus")
                            else:
                                logger.warning(f"⚠ Échec d'enregistrement de {m.name} sur nexus")
        except Exception as exc:
            logger.warning(f"⚠ Impossible d'enregistrer les modules sur nexus: {exc}")

    async def _route_to_module(self, module_name: str, endpoint: str, data: dict) -> Any:
        """Route une commande vers un module spécifique via HTTP."""
        m = self.modules[module_name]
        if m.status != "running":
            raise RuntimeError(f"Module {module_name} n'est pas en cours d'exécution")

        if not aiohttp:
            raise RuntimeError("aiohttp est requis pour communiquer avec les modules")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{m.base_url}{endpoint}", json=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                return await resp.json()

    async def _heartbeat_loop(self) -> None:
        """Boucle de health check périodique + auto-restart."""
        logger.info("💓 Heartbeat système démarré (intervalle=%ds)", HEALTH_INTERVAL)
        while self._running:
            await asyncio.sleep(HEALTH_INTERVAL)
            if not self._running:
                break

            statuses = await self.health_check()
            now = time.time()

            for name, m in self.modules.items():
                if statuses[name]["status"] == "error" and m.process and m.process.poll() is not None:
                    # Nettoyage des anciens restarts hors fenêtre
                    m.restart_times = [t for t in m.restart_times if now - t < RESTART_WINDOW]

                    if len(m.restart_times) >= MAX_RESTARTS:
                        logger.error(f"🚨 {name} a crashé {MAX_RESTARTS} fois en {RESTART_WINDOW}s — pas de redémarrage auto")
                        continue

                    logger.warning(f"🔄 Redémarrage automatique de {name}…")
                    m.restart_times.append(now)
                    try:
                        await self._launch_module(m)
                        await self._wait_ready(m, timeout=15)
                        await self._register_modules_on_nexus()
                        logger.info(f"✔ {name} redémarré avec succès")
                    except Exception as exc:
                        logger.error(f"✖ Échec du redémarrage de {name}: {exc}")

            self._print_status()

    # ── Affichage ────────────────────────────────────────────────────────

    def _print_status(self) -> None:
        """Affiche le statut de tous les modules en tableau ASCII."""
        sep = "─" * 62
        header = f"{'Module':<18} {'Port':>5} {'PID':>6} {'Statut':<10} {'Critique'}"
        uptime = time.time() - self._start_time if self._start_time else 0

        lines = [
            "",
            sep,
            f"  HERMES OMEGA — Uptime: {uptime:.0f}s",
            sep,
            header,
            "─" * 62,
        ]
        for mdef in MODULE_DEFS:
            m = self.modules[mdef["name"]]
            pid = str(m.process.pid) if m.process and m.process.poll() is None else "—"
            icon = "🟢" if m.status == "running" else ("🟡" if m.status == "starting" else "🔴")
            crit = "oui" if m.critical else "non"
            lines.append(f"  {icon} {m.name:<16} {m.port:>5} {pid:>6} {m.status:<10} {crit}")
        lines.append(sep)

        table = "\n".join(lines)
        logger.info(table)

    def get_status_dict(self) -> dict[str, Any]:
        """Retourne le statut sous forme de dictionnaire (pour l'API)."""
        return {
            "orchestrator": "HERMES OMEGA",
            "running": self._running,
            "uptime": round(time.time() - self._start_time, 1) if self._start_time else 0,
            "modules": {
                name: {
                    "port": m.port,
                    "status": m.status,
                    "critical": m.critical,
                    "pid": m.process.pid if m.process and m.process.poll() is None else None,
                    "last_health": m.last_health.isoformat() if m.last_health else None,
                }
                for name, m in self.modules.items()
            },
        }


# ─── CLI ──────────────────────────────────────────────────────────────────────

class OmegaCLI:
    """Interface CLI pour contrôler l'orchestrateur."""

    def __init__(self) -> None:
        self.omega = HermesOmega()

    async def run(self, args: list[str]) -> None:
        """Point d'entrée principal du CLI."""
        if not args:
            self._usage()
            return

        cmd = args[0].lower()

        if cmd == "--start" or cmd == "start":
            await self.omega.start()
            # Garde le process actif
            try:
                while self.omega._running:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                logger.info("\n⌨ Interruption clavier — arrêt en cours…")
                await self.omega.stop()

        elif cmd == "--stop" or cmd == "stop":
            # Essaie d'arrêter via l'API d'abord
            stopped = await self._try_api_stop()
            if not stopped:
                logger.info("Aucun orchestrateur distant trouvé — rien à faire.")

        elif cmd == "--status" or cmd == "status":
            await self._show_status()

        elif cmd == "--ask":
            if len(args) < 2:
                logger.error("Usage: hermes_omega_master.py --ask \"votre question\"")
                return
            question = " ".join(args[1:])
            await self._remote_command("ask", {"question": question})

        elif cmd == "--think":
            if len(args) < 2:
                logger.error("Usage: hermes_omega_master.py --think \"description de la tâche\"")
                return
            task = " ".join(args[1:])
            await self._remote_command("think", {"task": task})

        elif cmd == "--demo":
            await self._run_demo()

        else:
            self._usage()

    def _usage(self) -> None:
        print("""
HERMES OMEGA — Orchestrateur Principal
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Usage:
  python hermes_omega_master.py --start              Démarrer le système
  python hermes_omega_master.py --stop               Arrêter le système
  python hermes_omega_master.py --status             Voir le statut
  python hermes_omega_master.py --ask "question"     Question intelligente
  python hermes_omega_master.py --think "tâche"      Raisonnement
  python hermes_omega_master.py --demo               Mode démo
""")

    async def _show_status(self) -> None:
        """Affiche le statut (local ou distant via API)."""
        try:
            if aiohttp:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{ORCHESTRATOR_PORT}/status",
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            print(json.dumps(data, indent=2, ensure_ascii=False))
                            return
        except Exception:
            pass

        # Fallback: vérifie les processus localement
        print("L'orchestrateur ne semble pas être en cours d'exécution.")
        for mdef in MODULE_DEFS:
            print(f"  {mdef['name']:<18} port={mdef['port']}")

    async def _remote_command(self, command: str, data: dict) -> None:
        """Envoie une commande à l'orchestrateur via API."""
        if not aiohttp:
            print("Erreur: aiohttp est requis pour les commandes distantes")
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{ORCHESTRATOR_PORT}/command",
                    json={"command": command, **data},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    result = await resp.json()
                    print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as exc:
            print(f"Erreur: impossible de contacter l'orchestrateur ({exc})")
            print("Assurez-vous que le système est démarré avec --start")

    async def _try_api_stop(self) -> bool:
        """Tente d'arrêter l'orchestrateur via son API."""
        if not aiohttp:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{ORCHESTRATOR_PORT}/command",
                    json={"command": "stop"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def _run_demo(self) -> None:
        """Mode démo avec exemples de commandes."""
        print("""
╔══════════════════════════════════════════════════╗
║         HERMES OMEGA — Mode Démonstration        ║
╚══════════════════════════════════════════════════╝

Ce mode illustre les capacités du système.

→ Exemple de workflow:

  1. Démarrage:     python hermes_omega_master.py --start
  2. Veille tech:   python hermes_omega_master.py --think "Quelles sont les dernières tendances IA?"
  3. Question:      python hermes_omega_master.py --ask "Comment fonctionne RAG?"
  4. Scraping:      python hermes_omega_master.py --think "Scrape les 10 derniers articles de HN"
  5. Statut:        python hermes_omega_master.py --status
  6. Arrêt:         python hermes_omega_master.py --stop

→ Modules actifs:
""")
        for mdef in MODULE_DEFS:
            crit = "⚡ critique" if mdef["critical"] else "  optionnel"
            print(f"  • {mdef['name']:<18} port {mdef['port']}  {crit}")
        print()


# ─── API REST ─────────────────────────────────────────────────────────────────

def create_api(omega: HermesOmega) -> Any:
    """Crée l'application FastAPI pour l'orchestrateur."""
    if FastAPI is None:
        logger.error("FastAPI non installé — l'API REST sera indisponible")
        return None  # type: ignore[return-value]

    app = FastAPI(title="HERMES OMEGA", version="1.0.0")

    @app.get("/health")
    async def health() -> dict:
        """Health check global de l'orchestrateur."""
        return {"status": "ok" if omega._running else "stopped"}

    @app.get("/status")
    async def status() -> dict:
        """Statut détaillé de tous les modules."""
        return omega.get_status_dict()

    @app.get("/modules")
    async def modules_list() -> dict:
        """Liste des modules avec leur configuration."""
        return {
            name: {"port": m.port, "script": m.script, "critical": m.critical}
            for name, m in omega.modules.items()
        }

    @app.post("/command")
    async def command(payload: dict) -> Any:
        """Exécute une commande (think, ask, search, etc.)."""
        cmd = payload.get("command", "")
        kwargs = {k: v for k, v in payload.items() if k != "command"}
        return await omega.process_command(cmd, **kwargs)

    @app.post("/ask")
    async def ask(payload: dict) -> Any:
        """Question intelligente (brain + knowledge + search)."""
        question = payload.get("question", "")
        return await omega._intelligent_ask(question)

    @app.post("/module/{name}/restart")
    async def restart_module(name: str) -> dict:
        """Redémarre un module spécifique."""
        return await omega.restart_module(name)

    return app


async def run_api_server(omega: HermesOmega) -> None:
    """Lance le serveur API de l'orchestrateur."""
    app = create_api(omega)
    if app is None or uvicorn is None:
        logger.error("Impossible de démarrer l'API (uvicorn/FastAPI manquants)")
        return

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=ORCHESTRATOR_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


# ─── Point d'entrée principal ─────────────────────────────────────────────────

async def main() -> None:
    """Fonction principale — lance l'orchestrateur complet."""
    cli = OmegaCLI()
    await cli.run(sys.argv[1:])


def run() -> None:
    """Point d'entrée synchrone pour `python hermes_omega_master.py`."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n⌨ Interruption — HERMES OMEGA s'arrête.")


if __name__ == "__main__":
    run()
