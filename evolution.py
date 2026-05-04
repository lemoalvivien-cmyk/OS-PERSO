# -*- coding: utf-8 -*-
"""
HERMES OMEGA - Module Évolution (Auto-Modification Sécurisée)
=============================================================
Système d'auto-modification avec kill switch, policy engine,
scoring de risque IA (Ollama), audit trail SQLite et API REST.

Port API : 9304 | 100% local — zéro API externe
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import textwrap
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

# ─── Constantes ─────────────────────────────────────────────────────────────

HERMES_DIR: Path = Path.home() / ".hermes-omega"
KILL_SWITCH_PATH: Path = HERMES_DIR / ".KILL_SWITCH"
AUDIT_DB_PATH: Path = HERMES_DIR / "audit.db"
SNAPSHOTS_DIR: Path = HERMES_DIR / "snapshots"
OLLAMA_URL: str = "http://localhost:11434/api/generate"
OLLAMA_MODEL: str = "llama3"
API_PORT: int = 9304

HERMES_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# ─── Types ───────────────────────────────────────────────────────────────────

class Zone(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class ChangeType(str, Enum):
    BUGFIX = "bugfix"
    OPTIMIZATION = "optimization"
    DOCS = "docs"
    TEST = "test"
    LOGGING = "logging"
    CONFIG = "config"
    NEW_DEP = "new_dependency"
    NEW_SOURCE = "new_source"
    PORT_CHANGE = "port_change"
    MODEL_CHANGE = "model_change"
    ARCHITECTURE = "architecture"
    AUTH = "auth"
    SECURITY = "security"
    ENV_VAR = "env_var"
    PAYMENT = "payment"
    USER_DATA = "user_data"
    SSH_KEY = "ssh_key"
    OTHER = "other"


@dataclass
class ClassificationResult:
    zone: Zone
    confidence: float
    matched_patterns: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class RiskScore:
    score: int
    zone_override: Optional[Zone] = None
    details: str = ""
    ai_used: bool = False


@dataclass
class ProposalResult:
    approved: bool
    change_id: str
    zone: Zone
    risk_score: RiskScore
    classification: ClassificationResult
    reason: str
    timestamp: str = ""


@dataclass
class ChangeRecord:
    id: str
    action: str
    file_path: str
    zone: str
    risk_score: int
    approved: bool
    description: str
    timestamp: str
    snapshot_path: Optional[str] = None
    content_hash: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# Kill Switch — Fichier-bascule d'arrêt d'urgence
# ═══════════════════════════════════════════════════════════════════════════════

class KillSwitch:
    """Si ~/.hermes-omega/.KILL_SWITCH existe -> TOUT est bloque."""

    def __init__(self, path: Path = KILL_SWITCH_PATH) -> None:
        self._path = path

    def check(self) -> bool:
        """True = safe, False = kill switch active."""
        return not self._path.exists()

    def activate(self) -> None:
        """Active le kill switch."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            f"KILL_SWITCH active le {datetime.now().isoformat()}\n",
            encoding="utf-8",
        )

    def deactivate(self) -> None:
        """Desactive le kill switch."""
        if self._path.exists():
            self._path.unlink()

    @property
    def is_active(self) -> bool:
        return not self.check()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.is_active,
            "path": str(self._path),
            "safe_to_proceed": self.check(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Policy Engine — Classification par zone de securite
# ═══════════════════════════════════════════════════════════════════════════════

class PolicyEngine:
    """
    Zone Green  (auto)    : mods sures, appliquees automatiquement
    Zone Yellow (notify)  : notification utilisateur requise
    Zone Red    (BLOQUE)  : interdites, aucun bypass possible
    """

    # Patterns Zone Green — 18 patterns de confiance
    GREEN_PATTERNS: list[tuple[str, str]] = [
        (r"(?i)\.md$", "Fichier Markdown"),
        (r"(?i)/docs/", "Repertoire docs/"),
        (r"(?i)/doc/", "Repertoire doc/"),
        (r"(?i)README", "Fichier README"),
        (r"(?i)CHANGELOG", "Fichier CHANGELOG"),
        (r"(?i)test_.*\.py$", "Fichier test Python (prefixe)"),
        (r"(?i)_test\.py$", "Fichier test Python (suffixe)"),
        (r"(?i)/tests/", "Repertoire tests/"),
        (r"(?i)/test/", "Repertoire test/"),
        (r"(?i)\.test\.", "Fichier de test"),
        (r"(?i)logger?\.py$", "Module logging"),
        (r"(?i)logging\.conf$", "Config logging"),
        (r"(?i)pyproject\.toml$", "Config projet"),
        (r"(?i)\.editorconfig$", "Config editeur"),
        (r"(?i)setup\.cfg$", "Config setup"),
        (r"(?i)TODO\.md$", "Fichier TODO"),
        (r"(?i)\.gitignore$", "Fichier .gitignore"),
        (r"(?i)requirements.*\.txt$", "Fichier requirements"),
    ]

    # Patterns Zone Yellow — 13 patterns de vigilance
    YELLOW_PATTERNS: list[tuple[str, str]] = [
        (r"(?i)package\.json$", "package.json (deps possibles)"),
        (r"(?i)Pipfile$", "Pipfile"),
        (r"(?i)poetry\.lock$", "poetry.lock"),
        (r"(?i)/migrations/", "Migrations BDD"),
        (r"(?i)\.env\.example$", ".env.example"),
        (r"(?i)/api/", "Code API"),
        (r"(?i)/endpoints/", "Endpoints API"),
        (r"(?i)/routes/", "Routes application"),
        (r"(?i)docker-compose", "Config Docker Compose"),
        (r"(?i)Dockerfile", "Dockerfile"),
        (r"(?i)/models/", "Modeles de donnees"),
        (r"(?i)/schemas/", "Schema de validation"),
        (r"(?i)/templates/", "Templates HTML"),
    ]

    # Patterns Zone Red — 26 patterns stricts, aucun bypass
    RED_PATTERNS: list[tuple[str, str]] = [
        # Auth & securite
        (r"(?i)/auth/", "Repertoire auth/ — BLOQUE"),
        (r"(?i)auth\.py$", "Module auth — BLOQUE"),
        (r"(?i)login", "Logique login — BLOQUE"),
        (r"(?i)password", "Mots de passe — BLOQUE"),
        (r"(?i)credential", "Credentials — BLOQUE"),
        (r"(?i)/security/", "Repertoire security/ — BLOQUE"),
        (r"(?i)jwt", "JWT — BLOQUE"),
        (r"(?i)token", "Tokens — BLOQUE"),
        (r"(?i)session", "Sessions — BLOQUE"),
        (r"(?i)oauth", "OAuth — BLOQUE"),
        # Secrets & env
        (r"(?i)\.env$", ".env — BLOQUE"),
        (r"(?i)\.secrets", "Secrets — BLOQUE"),
        (r"(?i)secret", "Secret — BLOQUE"),
        (r"(?i)API_KEY", "Cle API — BLOQUE"),
        (r"(?i)PRIVATE_KEY", "Cle privee — BLOQUE"),
        (r"(?i)\.pem$", "Fichier PEM — BLOQUE"),
        (r"(?i)\.key$", "Fichier cle — BLOQUE"),
        # SSH & acces
        (r"(?i)\.ssh/", "Repertoire SSH — BLOQUE"),
        (r"(?i)ssh_config", "Config SSH — BLOQUE"),
        (r"(?i)known_hosts", "Known hosts — BLOQUE"),
        (r"(?i)authorized_keys", "Cles SSH autorisees — BLOQUE"),
        # Donnees sensibles
        (r"(?i)/payments/", "Payments — BLOQUE"),
        (r"(?i)/billing/", "Billing — BLOQUE"),
        (r"(?i)/users/", "Repertoire users/ — BLOQUE"),
        (r"(?i)/personal/", "Donnees personnelles — BLOQUE"),
        # Systeme critique
        (r"(?i)/kernel/", "Kernel — BLOQUE"),
        (r"(?i)firewall", "Firewall — BLOQUE"),
        (r"(?i)sudoers", "Sudoers — BLOQUE"),
        (r"(?i)/etc/", "Config systeme — BLOQUE"),
    ]

    # Mapping type de changement -> zone forcee
    _TYPE_ZONE: dict[ChangeType, Zone] = {
        ChangeType.AUTH: Zone.RED, ChangeType.SECURITY: Zone.RED,
        ChangeType.ENV_VAR: Zone.RED, ChangeType.PAYMENT: Zone.RED,
        ChangeType.USER_DATA: Zone.RED, ChangeType.SSH_KEY: Zone.RED,
        ChangeType.ARCHITECTURE: Zone.RED,
        ChangeType.NEW_DEP: Zone.YELLOW, ChangeType.NEW_SOURCE: Zone.YELLOW,
        ChangeType.PORT_CHANGE: Zone.YELLOW, ChangeType.MODEL_CHANGE: Zone.YELLOW,
        ChangeType.BUGFIX: Zone.GREEN, ChangeType.OPTIMIZATION: Zone.GREEN,
        ChangeType.DOCS: Zone.GREEN, ChangeType.TEST: Zone.GREEN,
        ChangeType.LOGGING: Zone.GREEN, ChangeType.CONFIG: Zone.GREEN,
    }

    def classify(self, file_path: str, change_type: ChangeType = ChangeType.OTHER) -> ClassificationResult:
        """Classifie un changement. Ordre : type force -> Red -> Yellow -> Green -> default Yellow."""
        forced = self._TYPE_ZONE.get(change_type)
        if forced is Zone.RED:
            return ClassificationResult(Zone.RED, 1.0, [f"type:{change_type.value}"],
                                        f"Type '{change_type.value}' bloque par politique")
        if forced is Zone.YELLOW:
            return ClassificationResult(Zone.YELLOW, 1.0, [f"type:{change_type.value}"],
                                        f"Type '{change_type.value}' requiert notification")

        matched = self._match(file_path)
        if matched[Zone.RED]:
            return ClassificationResult(Zone.RED, 1.0, matched[Zone.RED], "Pattern Red detecte — BLOQUE")
        if matched[Zone.YELLOW]:
            return ClassificationResult(Zone.YELLOW, 0.9, matched[Zone.YELLOW], "Pattern Yellow detecte — notification requise")
        if matched[Zone.GREEN]:
            return ClassificationResult(Zone.GREEN, 0.85, matched[Zone.GREEN], "Pattern Green detecte — auto-approuve")

        return ClassificationResult(Zone.YELLOW, 0.5, [], "Pattern inconnu — default Yellow")

    def _match(self, file_path: str) -> dict[Zone, list[str]]:
        results: dict[Zone, list[str]] = {Zone.RED: [], Zone.YELLOW: [], Zone.GREEN: []}
        mapping = {Zone.RED: self.RED_PATTERNS, Zone.YELLOW: self.YELLOW_PATTERNS, Zone.GREEN: self.GREEN_PATTERNS}
        for zone, patterns in mapping.items():
            for regex, desc in patterns:
                if re.search(regex, file_path):
                    results[zone].append(desc)
        return results

    def get_rules_summary(self) -> dict[str, int]:
        return {"green": len(self.GREEN_PATTERNS), "yellow": len(self.YELLOW_PATTERNS), "red": len(self.RED_PATTERNS)}


# ═══════════════════════════════════════════════════════════════════════════════
# Risk Analyzer — Scoring IA via Ollama + fallback regex
# ═══════════════════════════════════════════════════════════════════════════════

class RiskAnalyzer:
    """
    Seuils : 0-70 = vert, 70-90 = Yellow, 90+ = BLOCAGE.
    Ollama en premier, regex en fallback.
    """

    _HIGH_RISK: list[tuple[str, int]] = [
        (r"(?i)rm\s+-rf", 95), (r"(?i)DROP\s+TABLE", 95), (r"(?i)DELETE\s+FROM", 85),
        (r"(?i)eval\s*\(", 90), (r"(?i)exec\s*\(", 85), (r"(?i)__import__", 75),
        (r"(?i)subprocess", 70), (r"(?i)os\.system", 80), (r"(?i)chmod\s+777", 90),
        (r"(?i)curl.*\|.*sh", 95), (r"(?i)wget.*\|.*sh", 95), (r"(?i)sudo\s", 75),
        (r"(?i)password\s*=", 85), (r"(?i)secret\s*=", 85), (r"(?i)api_key\s*=", 80),
        (r"(?i)token\s*=", 75), (r"(?i)private_key", 90), (r"(?i)credit_card", 95),
        (r"(?i)pickle\.loads", 80), (r"(?i)requests\.post.*auth", 75),
    ]

    def __init__(self, ollama_url: str = OLLAMA_URL, ollama_model: str = OLLAMA_MODEL) -> None:
        self._url = ollama_url
        self._model = ollama_model

    async def analyze_risk(self, file_path: str, diff_content: str) -> RiskScore:
        if aiohttp:
            ai_result = await self._ask_ollama(file_path, diff_content)
            if ai_result is not None:
                return ai_result
        return self._regex_scoring(file_path, diff_content)

    async def _ask_ollama(self, file_path: str, diff_content: str) -> Optional[RiskScore]:
        if not aiohttp:
            return None
        prompt = textwrap.dedent(f"""\
            Evalue ce changement de code et retourne UNIQUEMENT un nombre 0-100.
            Fichier : {file_path}
            Changement :
            ```
            {diff_content[:3000]}
            ```
            0 = inoffensif, 100 = critique. Juste le chiffre.""")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self._url, json={
                    "model": self._model, "prompt": prompt,
                    "stream": False, "options": {"temperature": 0.0},
                }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    match = re.search(r"\b(\d{1,3})\b", data.get("response", "").strip())
                    if match:
                        score = max(0, min(100, int(match.group(1))))
                        return RiskScore(score, self._zone_from_score(score),
                                        f"Ollama ({self._model}): {score}", ai_used=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            pass
        return None

    def _regex_scoring(self, file_path: str, diff_content: str) -> RiskScore:
        max_score = 0
        count = 0
        combined = f"{file_path}\n{diff_content}"
        for pattern, score in self._HIGH_RISK:
            if re.search(pattern, combined):
                max_score = max(max_score, score)
                count += 1
        final = min(max_score + min(count * 3, 15), 100)
        details = f"Regex fallback — {count} pattern(s) risque" + (f" detecte(s)" if count else " (aucun)")
        return RiskScore(final, self._zone_from_score(final), details, ai_used=False)

    @staticmethod
    def _zone_from_score(score: int) -> Optional[Zone]:
        if score >= 90:
            return Zone.RED
        if score >= 70:
            return Zone.YELLOW
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Trail — Journalisation SQLite persistante
# ═══════════════════════════════════════════════════════════════════════════════

class AuditTrail:
    """Journal d'audit SQLite avec index sur fichier, timestamp et zone."""

    _SQL_INIT = """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY, action TEXT NOT NULL, file_path TEXT NOT NULL,
            zone TEXT NOT NULL, risk_score INTEGER NOT NULL, approved INTEGER NOT NULL,
            description TEXT, snapshot_path TEXT, content_hash TEXT, timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_file ON audit_log(file_path);
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_zone ON audit_log(zone);
    """

    def __init__(self, db_path: Path = AUDIT_DB_PATH) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SQL_INIT)
        self._conn.commit()

    def log(self, action: str, file_path: str, zone: str, risk_score: int,
            approved: bool, description: str = "", snapshot_path: Optional[str] = None,
            content_hash: Optional[str] = None) -> str:
        entry_id = hashlib.sha256(
            f"{action}:{file_path}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]
        self._conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry_id, action, file_path, zone, risk_score,
             1 if approved else 0, description, snapshot_path, content_hash,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return entry_id

    def query(self, file_path: Optional[str] = None, zone: Optional[str] = None,
              approved: Optional[bool] = None, limit: int = 50, offset: int = 0) -> list[ChangeRecord]:
        conds, params = [], []
        if file_path:
            conds.append("file_path LIKE ?")
            params.append(f"%{file_path}%")
        if zone:
            conds.append("zone = ?")
            params.append(zone)
        if approved is not None:
            conds.append("approved = ?")
            params.append(1 if approved else 0)
        where = " AND ".join(conds) if conds else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM audit_log WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [ChangeRecord(**dict(r)) for r in rows]

    def get_entry(self, entry_id: str) -> Optional[ChangeRecord]:
        row = self._conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry_id,)).fetchone()
        return ChangeRecord(**dict(row)) if row else None

    def close(self) -> None:
        self._conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Evolution Manager — Orchestrateur principal
# ═══════════════════════════════════════════════════════════════════════════════

class EvolutionManager:
    """Combine KillSwitch + PolicyEngine + RiskAnalyzer + AuditTrail."""

    def __init__(self, kill_switch: Optional[KillSwitch] = None,
                 policy: Optional[PolicyEngine] = None,
                 risk_analyzer: Optional[RiskAnalyzer] = None,
                 audit: Optional[AuditTrail] = None) -> None:
        self.kill_switch = kill_switch or KillSwitch()
        self.policy = policy or PolicyEngine()
        self.risk_analyzer = risk_analyzer or RiskAnalyzer()
        self.audit = audit or AuditTrail()
        self._pending: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _gen_id(file_path: str) -> str:
        return hashlib.sha256(f"{file_path}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _blocked_result(self, reason: str) -> ProposalResult:
        return ProposalResult(
            approved=False, change_id="", zone=Zone.RED,
            risk_score=RiskScore(100, Zone.RED, reason),
            classification=ClassificationResult(Zone.RED, 1.0, [], reason),
            reason=reason, timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def propose_change(self, file_path: str, content: str,
                             change_type: ChangeType = ChangeType.OTHER,
                             description: str = "") -> ProposalResult:
        """Propose un changement. Kill switch verifie EN PREMIER."""
        # 0) Kill Switch
        if not self.kill_switch.check():
            return self._blocked_result("KILL SWITCH ACTIVE — operations suspendues")

        # 1) Classification
        cls = self.policy.classify(file_path, change_type)

        # 2) Red = blocage immediat
        if cls.zone == Zone.RED:
            cid = self._gen_id(file_path)
            self.audit.log("propose", file_path, Zone.RED.value, 100, False, description, content_hash=self._hash(content))
            return ProposalResult(False, cid, Zone.RED, RiskScore(100, Zone.RED, cls.reason),
                                  cls, f"BLOQUE par Policy Engine : {cls.reason}",
                                  datetime.now(timezone.utc).isoformat())

        # 3) Analyse de risque
        risk = await self.risk_analyzer.analyze_risk(file_path, content)
        effective_zone = cls.zone
        if risk.zone_override == Zone.RED:
            effective_zone = Zone.RED
        elif risk.zone_override == Zone.YELLOW and cls.zone == Zone.GREEN:
            effective_zone = Zone.YELLOW

        # 4) Score >= 90 -> blocage
        if effective_zone == Zone.RED or risk.score >= 90:
            cid = self._gen_id(file_path)
            self.audit.log("propose", file_path, Zone.RED.value, risk.score, False, description, content_hash=self._hash(content))
            return ProposalResult(False, cid, Zone.RED, risk, cls,
                                  f"BLOQUE — risque {risk.score}/100 : {risk.details}",
                                  datetime.now(timezone.utc).isoformat())

        # 5) Approbation
        approved = effective_zone == Zone.GREEN
        cid = self._gen_id(file_path)
        self.audit.log("propose", file_path, effective_zone.value, risk.score, approved, description, content_hash=self._hash(content))

        if approved:
            self._pending[cid] = {
                "file_path": file_path, "content": content,
                "description": description, "zone": effective_zone.value,
                "risk_score": risk.score,
            }

        reasons = {Zone.GREEN: "Auto-approuve (Zone Green)", Zone.YELLOW: "Zone Yellow — notification requise"}
        return ProposalResult(approved, cid, effective_zone, risk, cls, reasons.get(effective_zone, ""),
                              datetime.now(timezone.utc).isoformat())

    def snapshot_before_change(self, file_path: str) -> Optional[str]:
        """Copie timestampee du fichier avant modification."""
        src = Path(file_path)
        if not src.exists():
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = SNAPSHOTS_DIR / f"{src.stem}_{ts}{src.suffix}"
        shutil.copy2(str(src), str(dest))
        return str(dest)

    def apply_change(self, change_id: str) -> dict[str, Any]:
        """Applique un changement approuve. Kill switch verifie AVANT ecriture."""
        if not self.kill_switch.check():
            self.audit.log("apply_blocked", "", "red", 100, False,
                           f"Tentative apply {change_id} avec kill switch")
            return {"success": False, "error": "Kill switch active"}

        change = self._pending.pop(change_id, None)
        if change is None:
            return {"success": False, "error": f"Changement {change_id} introuvable"}

        file_path, content = change["file_path"], change["content"]
        target = Path(file_path)

        # Snapshot avant modification
        snapshot = self.snapshot_before_change(file_path)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            chash = self._hash(content)
            self.audit.log("apply", file_path, change["zone"], change["risk_score"],
                           True, change["description"], snapshot, chash)
            return {"success": True, "change_id": change_id, "file_path": file_path,
                    "snapshot": snapshot, "content_hash": chash}
        except OSError as e:
            return {"success": False, "error": f"Erreur ecriture : {e}"}

    def rollback(self, change_id: str) -> dict[str, Any]:
        """Annule un changement via restoration du snapshot."""
        entries = self.audit.query(limit=200)
        target_entry = None
        for e in entries:
            if e.id == change_id:
                target_entry = e
                break
        if target_entry is None:
            return {"success": False, "error": f"Entree {change_id} introuvable"}
        if not target_entry.snapshot_path:
            return {"success": False, "error": "Aucun snapshot disponible"}

        snap = Path(target_entry.snapshot_path)
        if not snap.exists():
            return {"success": False, "error": f"Snapshot introuvable : {snap}"}

        try:
            shutil.copy2(str(snap), str(Path(target_entry.file_path)))
            self.audit.log("rollback", target_entry.file_path, target_entry.zone,
                           target_entry.risk_score, True, f"Rollback {change_id}")
            return {"success": True, "file_path": target_entry.file_path, "restored_from": str(snap)}
        except OSError as e:
            return {"success": False, "error": f"Erreur rollback : {e}"}

    def get_pending(self) -> list[dict[str, Any]]:
        return [{**v, "change_id": k} for k, v in self._pending.items()]

    def status(self) -> dict[str, Any]:
        return {
            "kill_switch": self.kill_switch.status(),
            "policy_rules": self.policy.get_rules_summary(),
            "pending_changes": len(self._pending),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Evolution API — Serveur FastAPI sur port 9304
# ═══════════════════════════════════════════════════════════════════════════════

class EvolutionAPI:
    """
    API REST du module Evolution.
    POST /propose  |  POST /apply  |  POST /rollback
    GET  /audit    |  POST /kill-switch  |  GET /status  |  GET /pending
    """

    def __init__(self, manager: Optional[EvolutionManager] = None) -> None:
        self.manager = manager or EvolutionManager()
        self._app: Any = None

    def _ensure_app(self) -> Any:
        if self._app is not None:
            return self._app
        from fastapi import FastAPI
        app = FastAPI(title="HERMES OMEGA — Module Evolution", version="1.0.0")
        mgr = self.manager

        @app.post("/propose")
        async def propose(payload: dict) -> Any:
            fp, ct_str = payload.get("file_path", ""), payload.get("change_type", "other")
            content, desc = payload.get("content", ""), payload.get("description", "")
            if not fp or not content:
                return {"error": "file_path et content requis"}
            try:
                ct = ChangeType(ct_str)
            except ValueError:
                ct = ChangeType.OTHER
            return asdict(await mgr.propose_change(fp, content, ct, desc))

        @app.post("/apply")
        async def apply(payload: dict) -> Any:
            cid = payload.get("change_id", "")
            return mgr.apply_change(cid) if cid else {"error": "change_id requis"}

        @app.post("/rollback")
        async def rollback(payload: dict) -> Any:
            cid = payload.get("change_id", "")
            return mgr.rollback(cid) if cid else {"error": "change_id requis"}

        @app.get("/audit")
        async def audit_query(file_path: Optional[str] = None, zone: Optional[str] = None,
                              approved: Optional[bool] = None, limit: int = 50, offset: int = 0) -> Any:
            return {"entries": [asdict(e) for e in mgr.audit.query(file_path, zone, approved, limit, offset)]}

        @app.post("/kill-switch")
        async def kill_switch(payload: dict) -> Any:
            action = payload.get("action", "")
            if action == "activate":
                mgr.kill_switch.activate()
                return {"status": "activated", "message": "Kill switch ACTIVE — operations suspendues"}
            elif action == "deactivate":
                mgr.kill_switch.deactivate()
                return {"status": "deactivated", "message": "Kill switch desactive"}
            return {"error": "Utiliser 'activate' ou 'deactivate'"}

        @app.get("/status")
        async def status() -> Any:
            return mgr.status()

        @app.get("/pending")
        async def pending() -> Any:
            return {"changes": mgr.get_pending()}

        self._app = app
        return app

    @property
    def app(self) -> Any:
        return self._ensure_app()

    def run(self, host: str = "127.0.0.1", port: int = API_PORT) -> None:
        import uvicorn
        uvicorn.run(self._ensure_app(), host=host, port=port, log_level="info")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI & Demo
# ═══════════════════════════════════════════════════════════════════════════════

async def _demo() -> None:
    print("=" * 60)
    print("  HERMES OMEGA — Module Evolution (demo)")
    print("=" * 60)
    mgr = EvolutionManager()
    print(f"\nKill switch : {'OK' if mgr.kill_switch.check() else 'ACTIVE'}")
    print(f"Regles : {mgr.policy.get_rules_summary()}")

    # Classification
    tests = [
        ("docs/README.md", ChangeType.DOCS), ("tests/test_x.py", ChangeType.TEST),
        ("auth/login.py", ChangeType.AUTH), ("src/payment/checkout.py", ChangeType.PAYMENT),
        (".env", ChangeType.ENV_VAR), ("~/.ssh/id_rsa", ChangeType.SSH_KEY),
        ("config/logging.yaml", ChangeType.LOGGING), ("src/api/endpoints.py", ChangeType.NEW_SOURCE),
        ("pyproject.toml", ChangeType.CONFIG), ("src/core/main.py", ChangeType.OTHER),
    ]
    print("\n-- Classification --")
    for path, ct in tests:
        r = mgr.policy.classify(path, ct)
        icon = {"green": "GREEN", "yellow": "YELLOW", "red": "RED"}.get(r.zone.value, "?")
        print(f"  [{icon:6s}] {path:35s} ({ct.value})")

    # Risk scoring
    print("\n-- Risk Scoring --")
    diffs = [
        ("src/hello.py", "print('hello')"),
        ("src/danger.py", "os.system('rm -rf /')"),
        ("src/db.py", "DROP TABLE users"),
        ("src/ok.py", "x += 1  # fix"),
    ]
    for path, d in diffs:
        risk = await mgr.risk_analyzer.analyze_risk(path, d)
        tag = "OK" if risk.score < 70 else "WARN" if risk.score < 90 else "BLOCK"
        print(f"  [{tag:5s}] {risk.score:3d}/100 {path:20s} ({'IA' if risk.ai_used else 'regex'})")

    # Proposal
    print("\n-- Proposal --")
    p = await mgr.propose_change("docs/README.md", "# HERMES\n\nAuto-mod.", ChangeType.DOCS, "MAJ README")
    print(f"  approved={p.approved} zone={p.zone.value} id={p.change_id} reason={p.reason}")

    mgr.audit.close()
    print("\nDemo terminee.")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="HERMES OMEGA — Module Evolution")
    parser.add_argument("--serve", action="store_true", help="Lancer API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=API_PORT)
    parser.add_argument("--demo", action="store_true", help="Demo")
    parser.add_argument("--kill", action="store_true", help="Activer kill switch")
    parser.add_argument("--unkill", action="store_true", help="Desactiver kill switch")
    args = parser.parse_args()

    if args.kill:
        KillSwitch().activate()
        print("Kill switch active.")
    elif args.unkill:
        KillSwitch().deactivate()
        print("Kill switch desactive.")
    elif args.demo:
        asyncio.run(_demo())
    elif args.serve:
        EvolutionAPI().run(args.host, args.port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
