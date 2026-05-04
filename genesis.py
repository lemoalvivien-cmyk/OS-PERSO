#!/usr/bin/env python3
"""
GENESIS — Module de création et déploiement automatique pour HERMES OMEGA
Crée des agents, des workflows, des dashboards — tout en code, 100% local
"""

import json
import time
import hashlib
import logging
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse

from fastapi import FastAPI
import uvicorn

# ─── Configuration ────────────────────────────────────────────────

CONFIG = {
    "ollama_url": "http://localhost:11434",
    "hermes_core_path": "/srv/hermes-command-os/hermes-core",
    "data_dir": "/srv/hermes-command-os/hermes-core/data/genesis",
    "templates_dir": "/srv/hermes-command-os/hermes-core/templates",
    "agents_dir": "/srv/hermes-command-os/hermes-core/agents",
    "log_file": "/srv/hermes-command-os/hermes-core/logs/genesis.log",
    "max_log_mb": 50,
    "auto_restart_services": True,
    "backup_before_mod": True,
    "backup_dir": "/srv/hermes-command-os/backups",
}

LOG_DIR = Path(CONFIG["log_file"]).parent
LOG_DIR.mkdir(parents=True, exist_ok=True)
for d in [CONFIG["data_dir"], CONFIG["templates_dir"], CONFIG["agents_dir"], CONFIG["backup_dir"]]:
    Path(d).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [GENESIS] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("genesis")


# ─── Agent Templates ──────────────────────────────────────────────

AGENT_TEMPLATES = {
    "code_assistant": {
        "description": "Agent assistant de code — analyse, écrit, refactor",
        "system_prompt": """Tu es un assistant de code expert. Tu analyses le code, identifies les bugs,
proposes des améliorations, et génères du code propre et testé.
Tu travailles dans le cadre de HERMES Command OS.
Règles: jamais de code destructif sans confirmation, toujours documenter,
préférer les bibliothèques open-source, respecter les patterns existants.""",
        "tools": ["read_file", "write_file", "execute_shell", "search_code", "git_operations"],
        "model_preference": "reasoning",
    },
    "data_analyst": {
        "description": "Agent analyste de données — scraping, parsing, visualisation",
        "system_prompt": """Tu es un analyste de données. Tu scrapes, nettoies, analyses et visualise
les données. Tu travailles avec PostgreSQL, Redis, Qdrant et les fichiers locaux.
Règles: toujours valider les données, gérer les erreurs, respecter RGPD.""",
        "tools": ["scrape", "query_database", "process_data", "generate_report", "search"],
        "model_preference": "reasoning",
    },
    "sysadmin": {
        "description": "Agent sysadmin — monitoring, maintenance, déploiement",
        "system_prompt": """Tu es un administrateur système expert. Tu gères les serveurs,
conteneurs Docker, services systemd, et la sécurité.
Règles: jamais de commande destructrice sans backup, toujours vérifier l'impact,
préférer la stabilité à la nouveauté.""",
        "tools": ["execute_shell", "docker_manage", "service_control", "log_read", "backup"],
        "model_preference": "fast",
    },
    "researcher": {
        "description": "Agent chercheur — veille, analyse, synthèse",
        "system_prompt": """Tu es un chercheur et analyste tech. Tu surveilles les tendances,
analyses les technologies émergentes, et produis des rapports de synthèse.
Tu travailles en français par défaut.""",
        "tools": ["web_search", "rss_read", "analyze", "summarize", "compare"],
        "model_preference": "reasoning",
    },
    "security_auditor": {
        "description": "Agent sécurité — audit, scan, rapport",
        "system_prompt": """Tu es un auditeur sécurité. Tu scannes les vulnérabilités,
vérifies les configurations, et produis des rapports de sécurité.
Règles: signaler TOUT, jamais modifier sans confirmation explicite.""",
        "tools": ["scan_ports", "check_permissions", "audit_config", "verify_ssl", "report"],
        "model_preference": "reasoning",
    },
    "communicator": {
        "description": "Agent communication — email, notifications, rapports",
        "system_prompt": """Tu gères les communications sortantes de HERMES.
Tu rédiges des emails, des notifications, des rapports formatés.
Tu es concis, professionnel, et toujours en français sauf indication contraire.""",
        "tools": ["send_email", "send_notification", "format_report", "template_render"],
        "model_preference": "fast",
    },
    "orchestrator": {
        "description": "Agent orchestrateur — coordination entre agents",
        "system_prompt": """Tu es l'orchestrateur de HERMES OMEGA. Tu coordonnes les agents,
gères les workflows, et prends les décisions de routage.
Tu connais les capacités de chaque agent et délègues efficacement.
Priorité: sécurité > fiabilité > performance > nouveauté.""",
        "tools": ["delegate", "monitor", "schedule", "route", "escalate"],
        "model_preference": "reasoning",
    },
}


# ─── Backup Manager ───────────────────────────────────────────────

class BackupManager:
    """Gestionnaire de snapshots avant modification."""

    def __init__(self, backup_dir: str):
        self.backup_dir = Path(backup_dir)
        self.lock = threading.Lock()

    def snapshot(self, target: str, label: str = "") -> dict:
        """Crée un snapshot d'un fichier ou répertoire."""
        target_path = Path(target)
        if not target_path.exists():
            return {"error": f"Target not found: {target}"}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label_suffix = f"_{label}" if label else ""
        snapshot_name = f"{target_path.name}_{timestamp}{label_suffix}"
        snapshot_path = self.backup_dir / snapshot_name

        with self.lock:
            try:
                if target_path.is_file():
                    import shutil
                    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target_path, snapshot_path)
                elif target_path.is_dir():
                    import shutil
                    shutil.make_archive(str(snapshot_path), 'gztar', target_path)
                    snapshot_path = Path(f"{snapshot_path}.tar.gz")
                else:
                    return {"error": f"Unsupported type: {target}"}

                size = snapshot_path.stat().st_size
                log.info(f"Snapshot created: {snapshot_path} ({size} bytes)")
                return {
                    "status": "ok",
                    "snapshot": str(snapshot_path),
                    "original": str(target_path),
                    "size": size,
                    "timestamp": timestamp,
                }
            except Exception as e:
                log.error(f"Snapshot failed for {target}: {e}")
                return {"error": str(e)}

    def list_snapshots(self) -> list:
        """Liste tous les snapshots."""
        snapshots = []
        for f in sorted(self.backup_dir.iterdir(), reverse=True):
            snapshots.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
        return snapshots

    def restore(self, snapshot_name: str, target: str) -> dict:
        """Restaure un snapshot."""
        snapshot_path = self.backup_dir / snapshot_name
        target_path = Path(target)

        if not snapshot_path.exists():
            return {"error": f"Snapshot not found: {snapshot_name}"}

        try:
            if snapshot_path.suffix == ".gz":
                import shutil
                shutil.unpack_archive(str(snapshot_path), str(target_path))
            else:
                import shutil
                shutil.copy2(snapshot_path, target_path)

            log.info(f"Restored {snapshot_name} -> {target}")
            return {"status": "ok", "restored": str(snapshot_name), "target": str(target_path)}
        except Exception as e:
            return {"error": str(e)}

    def cleanup(self, keep_days: int = 30):
        """Supprime les snapshots de plus de X jours."""
        cutoff = time.time() - (keep_days * 86400)
        removed = 0
        for f in self.backup_dir.iterdir():
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        if removed:
            log.info(f"Cleaned up {removed} old snapshots")
        return removed


backup_mgr = BackupManager(CONFIG["backup_dir"])


# ─── Service Manager ──────────────────────────────────────────────

class ServiceManager:
    """Gestionnaire de services systemd."""

    def _run(self, cmd: str, timeout: int = 30) -> dict:
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return {
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "returncode": -1}
        except Exception as e:
            return {"error": str(e), "returncode": -1}

    def status(self, service: str) -> dict:
        return self._run(f"systemctl status {service} --no-pager -l")

    def start(self, service: str) -> dict:
        result = self._run(f"systemctl start {service}")
        if result.get("returncode") == 0:
            log.info(f"Service started: {service}")
        else:
            log.error(f"Failed to start {service}: {result.get('stderr')}")
        return result

    def stop(self, service: str) -> dict:
        return self._run(f"systemctl stop {service}")

    def restart(self, service: str) -> dict:
        return self._run(f"systemctl restart {service}")

    def is_active(self, service: str) -> bool:
        result = self._run(f"systemctl is-active {service}")
        return result.get("stdout", "").strip() == "active"

    def create_service(self, name: str, exec_path: str, description: str = "",
                        user: str = "root", working_dir: str = None,
                        env_vars: dict = None, auto_restart: bool = True) -> dict:
        """Crée un fichier service systemd."""
        content = f"""[Unit]
Description={description or f'HERMES {name}'}
After=network.target docker.service ollama.service
Wants=ollama.service

[Service]
Type=simple
User={user}
"""
        if working_dir:
            content += f"WorkingDirectory={working_dir}\n"
        if env_vars:
            for k, v in env_vars.items():
                content += f"Environment={k}={v}\n"

        content += f"""ExecStart={exec_path}
Restart={'always' if auto_restart else 'no'}
RestartSec=5
StandardOutput=append:/srv/hermes-command-os/hermes-core/logs/{name}.log
StandardError=append:/srv/hermes-command-os/hermes-core/logs/{name}.log

[Install]
WantedBy=multi-user.target
"""
        service_path = f"/etc/systemd/system/hermes-{name}.service"
        try:
            with open(service_path, "w") as f:
                f.write(content)
            self._run("systemctl daemon-reload")
            log.info(f"Service created: {service_path}")
            return {"status": "ok", "service": service_path}
        except Exception as e:
            return {"error": str(e)}

    def enable_service(self, name: str) -> dict:
        return self._run(f"systemctl enable hermes-{name}.service")

    def list_hermes_services(self) -> list:
        result = self._run("systemctl list-units --type=service | grep hermes")
        lines = result.get("stdout", "").strip().split("\n")
        return [l.strip() for l in lines if l.strip()]


svc_mgr = ServiceManager()


# ─── Agent Factory ────────────────────────────────────────────────

class AgentFactory:
    """Factory pour créer et gérer des agents HERMES."""

    def __init__(self):
        self.agents = {}
        self._load_agents()

    def _load_agents(self):
        """Charge les agents existants."""
        agents_dir = Path(CONFIG["agents_dir"])
        if agents_dir.exists():
            for f in agents_dir.glob("*.json"):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        agent = json.loads(fh.read())
                        self.agents[agent["id"]] = agent
                except Exception:
                    continue
        log.info(f"Loaded {len(self.agents)} agents")

    def create_agent(self, agent_id: str, template: str = None,
                      system_prompt: str = None, tools: list = None,
                      model_preference: str = "fast",
                      metadata: dict = None) -> dict:
        """Crée un nouvel agent."""
        if agent_id in self.agents:
            return {"error": f"Agent '{agent_id}' already exists"}

        # Charger le template si spécifié
        if template and template in AGENT_TEMPLATES:
            tmpl = AGENT_TEMPLATES[template]
            system_prompt = system_prompt or tmpl["system_prompt"]
            tools = tools or tmpl["tools"]
            model_preference = model_preference or tmpl["model_preference"]
            description = tmpl["description"]
        else:
            description = metadata.get("description", "") if metadata else ""

        if not system_prompt:
            return {"error": "system_prompt is required"}

        agent = {
            "id": agent_id,
            "system_prompt": system_prompt,
            "tools": tools or [],
            "model_preference": model_preference,
            "description": description,
            "metadata": metadata or {},
            "created_at": datetime.now().isoformat(),
            "status": "active",
            "stats": {"tasks_completed": 0, "errors": 0},
        }

        # Sauvegarder
        agent_file = Path(CONFIG["agents_dir"]) / f"{agent_id}.json"
        with open(agent_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(agent, indent=2, ensure_ascii=False))

        self.agents[agent_id] = agent
        log.info(f"Agent created: {agent_id} (template={template})")
        return {"status": "ok", "agent": agent}

    def update_agent(self, agent_id: str, **kwargs) -> dict:
        """Met à jour un agent existant."""
        if agent_id not in self.agents:
            return {"error": f"Agent '{agent_id}' not found"}

        agent = self.agents[agent_id]
        for key, value in kwargs.items():
            if key in agent:
                agent[key] = value

        agent["updated_at"] = datetime.now().isoformat()

        agent_file = Path(CONFIG["agents_dir"]) / f"{agent_id}.json"
        with open(agent_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(agent, indent=2, ensure_ascii=False))

        log.info(f"Agent updated: {agent_id}")
        return {"status": "ok", "agent": agent}

    def delete_agent(self, agent_id: str) -> dict:
        """Supprime un agent."""
        if agent_id not in self.agents:
            return {"error": f"Agent '{agent_id}' not found"}

        # Backup avant suppression
        backup_mgr.snapshot(str(Path(CONFIG["agents_dir"]) / f"{agent_id}.json"), f"agent_{agent_id}")

        agent_file = Path(CONFIG["agents_dir"]) / f"{agent_id}.json"
        agent_file.unlink()

        del self.agents[agent_id]
        log.info(f"Agent deleted: {agent_id}")
        return {"status": "ok"}

    def list_agents(self) -> list:
        return list(self.agents.values())

    def get_agent(self, agent_id: str) -> dict:
        return self.agents.get(agent_id, {"error": "not found"})


agent_factory = AgentFactory()


# ─── Workflow Engine ──────────────────────────────────────────────

class WorkflowEngine:
    """Moteur de workflows pour orchestrer les agents."""

    def __init__(self):
        self.workflows = {}
        self._load_workflows()

    def _load_workflows(self):
        wf_dir = Path(CONFIG["data_dir"])
        if wf_dir.exists():
            for f in wf_dir.glob("workflow_*.json"):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        wf = json.loads(fh.read())
                        self.workflows[wf["id"]] = wf
                except Exception:
                    continue
        log.info(f"Loaded {len(self.workflows)} workflows")

    def create_workflow(self, wf_id: str, steps: list, trigger: str = "manual",
                         description: str = "") -> dict:
        """Crée un workflow."""
        if wf_id in self.workflows:
            return {"error": f"Workflow '{wf_id}' already exists"}

        workflow = {
            "id": wf_id,
            "steps": steps,
            "trigger": trigger,
            "description": description,
            "created_at": datetime.now().isoformat(),
            "status": "active",
            "runs": 0,
            "last_run": None,
        }

        wf_file = Path(CONFIG["data_dir"]) / f"workflow_{wf_id}.json"
        with open(wf_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(workflow, indent=2, ensure_ascii=False))

        self.workflows[wf_id] = workflow
        log.info(f"Workflow created: {wf_id} ({len(steps)} steps)")
        return {"status": "ok", "workflow": workflow}

    def list_workflows(self) -> list:
        return list(self.workflows.values())


workflow_engine = WorkflowEngine()


# ─── HTTP API ──────────────────────────────────────────────────────

class GenesisHandler(BaseHTTPRequestHandler):

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
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

        if parsed.path == "/api/health":
            self._send_json(200, {"status": "ok", "version": "1.0.0"})

        elif parsed.path == "/api/agents":
            self._send_json(200, {"agents": agent_factory.list_agents()})

        elif parsed.path == "/api/templates":
            self._send_json(200, {"templates": list(AGENT_TEMPLATES.keys())})

        elif parsed.path == "/api/workflows":
            self._send_json(200, {"workflows": workflow_engine.list_workflows()})

        elif parsed.path == "/api/backups":
            self._send_json(200, {"backups": backup_mgr.list_snapshots()})

        elif parsed.path == "/api/services":
            self._send_json(200, {"services": svc_mgr.list_hermes_services()})

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_body()

        if parsed.path == "/api/agents/create":
            result = agent_factory.create_agent(**body)
            self._send_json(200, result)

        elif parsed.path == "/api/agents/update":
            agent_id = body.get("agent_id", "")
            result = agent_factory.update_agent(agent_id, **{k: v for k, v in body.items() if k != "agent_id"})
            self._send_json(200, result)

        elif parsed.path == "/api/agents/delete":
            result = agent_factory.delete_agent(body.get("agent_id", ""))
            self._send_json(200, result)

        elif parsed.path == "/api/workflows/create":
            result = workflow_engine.create_workflow(**body)
            self._send_json(200, result)

        elif parsed.path == "/api/backup":
            result = backup_mgr.snapshot(body.get("target", ""), body.get("label", ""))
            self._send_json(200, result)

        elif parsed.path == "/api/backup/restore":
            result = backup_mgr.restore(body.get("snapshot", ""), body.get("target", ""))
            self._send_json(200, result)

        elif parsed.path == "/api/services/create":
            result = svc_mgr.create_service(**body)
            self._send_json(200, result)

        elif parsed.path == "/api/services/restart":
            result = svc_mgr.restart(body.get("service", ""))
            self._send_json(200, result)

        else:
            self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        log.debug(f"{self.client_address[0]} - {format % args}")


# ═══════════════════════════════════════════════════════════════════════════════
# API REST FastAPI — Genesis sur port 9303
# ═══════════════════════════════════════════════════════════════════════════════

# Instance FastAPI pour le module Genesis
app_fastapi = FastAPI(title="GENESIS API", version="1.0.0")


@app_fastapi.get("/status")
async def api_status():
    """Retourne le statut du module Genesis."""
    return {
        "status": "ok",
        "module": "genesis",
        "agents_count": len(agent_factory.agents),
        "workflows_count": len(workflow_engine.workflows),
        "templates_count": len(AGENT_TEMPLATES),
    }


@app_fastapi.get("/health")
async def api_health():
    """Vérifie la santé du module Genesis."""
    return {
        "health": "ok",
        "agents": len(agent_factory.agents),
        "workflows": len(workflow_engine.workflows),
        "version": "1.0.0",
    }


@app_fastapi.get("/agents")
async def api_list_agents():
    """Liste tous les agents enregistrés."""
    return {"agents": agent_factory.list_agents(), "count": len(agent_factory.agents)}


@app_fastapi.post("/agent/create")
async def api_create_agent(payload: dict):
    """
    Crée un nouvel agent.
    Accepte {"agent_id": "...", "template": "...", "system_prompt": "...", "tools": [...], ...}
    Si un template est spécifié, les champs manquants sont remplis depuis le template.
    """
    agent_id = payload.get("agent_id", "")
    if not agent_id:
        return {"error": "Champ 'agent_id' requis"}

    # Paramètres optionnels avec fallback vers le payload
    template = payload.get("template")
    system_prompt = payload.get("system_prompt")
    tools = payload.get("tools")
    model_preference = payload.get("model_preference", "fast")
    metadata = payload.get("metadata")

    try:
        result = agent_factory.create_agent(
            agent_id=agent_id,
            template=template,
            system_prompt=system_prompt,
            tools=tools,
            model_preference=model_preference,
            metadata=metadata,
        )
        return result
    except Exception as e:
        return {"error": str(e)}


# ─── Main ──────────────────────────────────────────────────────────

PORT_DEFAULT = 9303


def main():
    """Lance le serveur FastAPI via uvicorn."""
    import sys
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT_DEFAULT

    # Log rotation
    log_file = Path(CONFIG["log_file"])
    if log_file.exists() and log_file.stat().st_size > CONFIG["max_log_mb"] * 1024 * 1024:
        backup = log_file.with_suffix(f".{int(time.time())}.log")
        log_file.rename(backup)

    # Créer les agents par défaut si aucun n'existe
    if not agent_factory.agents:
        log.info("Création des agents par défaut...")
        for name, template in AGENT_TEMPLATES.items():
            agent_factory.create_agent(
                agent_id=name,
                template=name,
            )

    log.info(f"GENESIS (FastAPI) démarrant sur 127.0.0.1:{port}")
    log.info(f"Agents chargés: {len(agent_factory.agents)}")
    uvicorn.run(app_fastapi, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
