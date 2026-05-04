"""
HERMES OMEGA - Module de Computer Use (Contrôle de l'ordinateur)
===============================================================
Donne à HERMES la capacité de contrôler la machine locale :
fichiers, commandes, navigation web, screenshots, fenêtres,
presse-papiers et raccourcis clavier.

Port API REST : 9307
Compatible Windows 11
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import asyncio
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from collections import defaultdict
from logging.handlers import RotatingFileHandler
import hashlib
import httpx
import json
import threading
import re

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SANDBOX_DIR: str = str(Path.home() / "hermes_sandbox")
DEFAULT_COMMAND_TIMEOUT: int = 30
DEFAULT_WEB_TIMEOUT: int = 60
API_PORT: int = 9307

# Authentification (vide = pas d'auth, sinon clé attendue dans header X-API-Key)
HERMES_API_KEY: str = os.environ.get("HERMES_API_KEY", "")

# Rate limiting (requêtes par fenêtre glissante)
RATE_LIMIT_MAX: int = 120
RATE_LIMIT_WINDOW: int = 60  # secondes

# Commandes nécessitant explicit=True (motifs)
DANGEROUS_PATTERNS: list[str] = [
    "rm -rf", "rmdir /s", "del /f", "format ", "diskpart",
    "reg delete", "bcdedit", "netsh", "schtasks /delete",
    "remove-item -recurse -force", "format-volume",
]

# Commandes toujours autorisées (sans restriction)
SAFE_COMMANDS: set[str] = {
    "echo", "type", "dir", "ls", "cd", "pwd", "date", "time",
    "whoami", "hostname", "ipconfig", "ping", "tracert", "nslookup",
    "python", "python3", "pip", "node", "npm", "npx", "git",
    "where", "which", "set", "printenv", "tree", "findstr", "grep",
}

logger = logging.getLogger("hermes.computer_use")

# ---------------------------------------------------------------------------
# Logging persistant (rotation 5 MB × 3 fichiers)
# ---------------------------------------------------------------------------

_LOG_DIR = Path.home() / ".hermes-omega" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    _LOG_DIR / "hermes_computer_use.log",
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
logger.addHandler(_file_handler)


# ---------------------------------------------------------------------------
# Codes d'erreur typés
# ---------------------------------------------------------------------------

class ErrorCode(Enum):
    """Codes d'erreur standardisés pour tous les endpoints."""
    SANDBOX_VIOLATION = "E001"
    DANGEROUS_COMMAND = "E002"
    COMMAND_TIMEOUT = "E003"
    FILE_NOT_FOUND = "E004"
    PERMISSION_DENIED = "E005"
    WINDOW_NOT_FOUND = "E006"
    CLIPBOARD_ERROR = "E007"
    SCREENSHOT_ERROR = "E008"
    WEB_NAVIGATION_ERROR = "E009"
    WEB_CLICK_ERROR = "E010"
    WEB_TYPE_ERROR = "E011"
    KEYBOARD_ERROR = "E012"
    BROWSER_NOT_READY = "E013"
    RATE_LIMITED = "E014"
    UNAUTHORIZED = "E015"
    INVALID_REQUEST = "E016"
    INTERNAL_ERROR = "E999"


def error_response(code: ErrorCode, message: str, **extra) -> dict:
    """Construit une réponse d'erreur typée."""
    return {"status": "error", "code": code.value, "message": message, **extra}


# ---------------------------------------------------------------------------
# Rate limiter (token bucket par clé)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Limiter de requêtes par fenêtre glissante."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str = "global") -> bool:
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            bucket = self._buckets[key]
            # Nettoyer les entrées expirées
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            return True

    def reset(self, key: str = "global"):
        with self._lock:
            self._buckets.pop(key, None)


rate_limiter = _RateLimiter(max_requests=RATE_LIMIT_MAX, window_seconds=RATE_LIMIT_WINDOW)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

class ComputerUseError(Exception):
    """Erreur générique du module computer use."""


def _is_dangerous(command: str) -> bool:
    """Vérifie si une commande contient un motif dangereux."""
    cmd_lower = command.lower().strip()
    return any(p in cmd_lower for p in DANGEROUS_PATTERNS)


def _resolve_sandbox(path: str, sandbox_root: str) -> Path:
    """Résout un chemin relatif dans le sandbox."""
    if os.path.isabs(path):
        resolved = Path(path)
        # Vérifier que le chemin est dans le sandbox
        try:
            resolved.relative_to(sandbox_root)
            return resolved
        except ValueError:
            raise ComputerUseError(f"Chemin hors du sandbox : {path}")
    return Path(sandbox_root) / path


def _ensure_dir(path: Path) -> None:
    """Crée le répertoire parent si nécessaire."""
    path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. FileSystemOps — Opérations sur les fichiers
# ---------------------------------------------------------------------------

class FileSystemOps:
    """Opérations sur les fichiers avec sandbox par défaut."""

    def __init__(
        self,
        sandbox_root: str = DEFAULT_SANDBOX_DIR,
        full_access: bool = False,
    ) -> None:
        self.sandbox_root = Path(sandbox_root).resolve()
        self.full_access = full_access
        if not full_access:
            self.sandbox_root.mkdir(parents=True, exist_ok=True)
            logger.info("Sandbox fichiers : %s", self.sandbox_root)

    def _safe_path(self, path: str) -> Path:
        """Valide et résout le chemin selon le mode."""
        if self.full_access:
            return Path(path).resolve()
        return _resolve_sandbox(path, str(self.sandbox_root))

    def read_file(self, path: str, encoding: str = "utf-8") -> str:
        """Lit un fichier texte et retourne son contenu."""
        target = self._safe_path(path)
        if not target.is_file():
            raise ComputerUseError(f"Fichier introuvable : {target}")
        # Limiter la lecture à 10 Mo pour la mémoire
        if target.stat().st_size > 10 * 1024 * 1024:
            raise ComputerUseError(f"Fichier trop volumineux (>10 Mo) : {target}")
        return target.read_text(encoding=encoding, errors="replace")

    def read_file_bytes(self, path: str) -> bytes:
        """Lit un fichier en mode binaire."""
        target = self._safe_path(path)
        if not target.is_file():
            raise ComputerUseError(f"Fichier introuvable : {target}")
        if target.stat().st_size > 50 * 1024 * 1024:
            raise ComputerUseError(f"Fichier trop volumineux (>50 Mo) : {target}")
        return target.read_bytes()

    def write_file(self, path: str, content: str, encoding: str = "utf-8") -> str:
        """Écrit du contenu dans un fichier. Crée les répertoires si besoin."""
        target = self._safe_path(path)
        _ensure_dir(target)
        target.write_text(content, encoding=encoding)
        return f"Fichier écrit : {target}"

    def write_file_bytes(self, path: str, data: bytes) -> str:
        """Écrit des données binaires dans un fichier."""
        target = self._safe_path(path)
        _ensure_dir(target)
        target.write_bytes(data)
        return f"Fichier écrit (binaire) : {target}"

    def list_dir(self, path: str = ".") -> list[dict[str, Any]]:
        """Liste le contenu d'un répertoire."""
        target = self._safe_path(path)
        if not target.is_dir():
            raise ComputerUseError(f"Répertoire introuvable : {target}")
        entries = []
        for item in target.iterdir():
            try:
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
            except PermissionError:
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "inaccessible",
                    "size": 0,
                    "modified": 0,
                })
        return entries

    def delete_file(self, path: str, explicit: bool = False) -> str:
        """Supprime un fichier. Nécessite explicit=True pour les chemins critiques."""
        target = self._safe_path(path)
        if not target.exists():
            raise ComputerUseError(f"Élément introuvable : {target}")
        if target.is_dir() and not explicit:
            raise ComputerUseError(
                "Suppression de répertoire : utiliser explicit=True pour confirmer"
            )
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return f"Supprimé : {target}"

    def rename(self, old_path: str, new_path: str) -> str:
        """Renomme ou déplace un fichier/répertoire."""
        src = self._safe_path(old_path)
        dst = self._safe_path(new_path)
        if not src.exists():
            raise ComputerUseError(f"Source introuvable : {src}")
        src.rename(dst)
        return f"Renommé : {src} -> {dst}"

    def mkdir(self, path: str, parents: bool = True) -> str:
        """Crée un répertoire."""
        target = self._safe_path(path)
        target.mkdir(parents=parents, exist_ok=True)
        return f"Répertoire créé : {target}"

    def stat(self, path: str) -> dict[str, Any]:
        """Retourne les métadonnées d'un fichier/répertoire."""
        target = self._safe_path(path)
        if not target.exists():
            raise ComputerUseError(f"Introuvable : {target}")
        s = target.stat()
        return {
            "name": target.name,
            "path": str(target),
            "type": "directory" if target.is_dir() else "file",
            "size": s.st_size,
            "created": s.st_ctime,
            "modified": s.st_mtime,
            "accessed": s.st_atime,
        }


# ---------------------------------------------------------------------------
# 2. CommandExecutor — Exécution de commandes système
# ---------------------------------------------------------------------------

class CommandExecutor:
    """Exécuteur de commandes PowerShell/cmd avec sécurité."""

    def __init__(
        self,
        default_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        allowed_commands: set[str] | None = None,
    ) -> None:
        self.default_timeout = default_timeout
        self.allowed_commands = allowed_commands or SAFE_COMMANDS

    def _check_safety(self, command: str, explicit: bool) -> None:
        """Vérifie si la commande est sûre à exécuter."""
        if _is_dangerous(command) and not explicit:
            raise ComputerUseError(
                f"Commande potentiellement dangereuse détectée. "
                f"Utiliser explicit=True pour confirmer : {command[:80]}"
            )

    def execute(
        self,
        command: str,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shell: bool = True,
        explicit: bool = False,
    ) -> dict[str, Any]:
        """
        Exécute une commande et retourne {stdout, stderr, exit_code}.

        Parameters:
            command: La commande à exécuter
            timeout: Délai max en secondes (défaut: 30s)
            cwd: Répertoire de travail
            env: Variables d'environnement supplémentaires
            shell: Utiliser le shell (PowerShell sur Windows)
            explicit: Autoriser les commandes dangereuses
        """
        self._check_safety(command, explicit)
        timeout = timeout or self.default_timeout

        # Construire l'environnement
        proc_env = None
        if env:
            proc_env = {**os.environ, **env}

        logger.info("Exécution commande (timeout=%ds) : %s", timeout, command[:120])

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=proc_env,
                shell=shell,
                # Encodage UTF-8 pour PowerShell
                encoding="utf-8",
                errors="replace",
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "command": command,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Timeout après {timeout}s",
                "exit_code": -1,
                "command": command,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
                "command": command,
            }

    async def execute_async(
        self,
        command: str,
        timeout: int | None = None,
        cwd: str | None = None,
        explicit: bool = False,
    ) -> dict[str, Any]:
        """Exécution asynchrone avec streaming stdout/stderr."""
        self._check_safety(command, explicit)
        timeout = timeout or self.default_timeout

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode or -1,
                "command": command,
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "stdout": "",
                "stderr": f"Timeout asynchrone après {timeout}s",
                "exit_code": -1,
                "command": command,
            }


# ---------------------------------------------------------------------------
# 3. WebController — Navigation web via Playwright
# ---------------------------------------------------------------------------

class WebController:
    """Contrôle de navigateur web via Playwright (fallback: Selenium)."""

    def __init__(self, headless: bool = True, timeout: int = DEFAULT_WEB_TIMEOUT):
        self.headless = headless
        self.timeout = timeout
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._pages: list = []

    PLAYWRIGHT_MAX_RETRIES: int = 3
    PLAYWRIGHT_RETRY_DELAY: float = 2.0  # secondes (exponentiel)

    async def start(self) -> None:
        """Démarre le navigateur Playwright avec retry exponentiel."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "Playwright non installé. Installer avec : "
                "pip install playwright && playwright install chromium"
            )
            raise ComputerUseError("Playwright non disponible")

        last_err = None
        for attempt in range(1, self.PLAYWRIGHT_MAX_RETRIES + 1):
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )
                self._page = await self._context.new_page()
                self._page.set_default_timeout(self.timeout * 1000)
                self._pages.append(self._page)
                logger.info("Navigateur Playwright démarré (headless=%s, attempt=%d)", self.headless, attempt)
                return
            except Exception as e:
                last_err = e
                logger.warning("Playwright start attempt %d/%d failed: %s", attempt, self.PLAYWRIGHT_MAX_RETRIES, e)
                # Nettoyer les ressources partielles
                await self.close()
                if attempt < self.PLAYWRIGHT_MAX_RETRIES:
                    delay = self.PLAYWRIGHT_RETRY_DELAY * (2 ** (attempt - 1))
                    logger.info("Retrying Playwright start in %.1fs...", delay)
                    await asyncio.sleep(delay)

        raise ComputerUseError(f"Playwright échoué après {self.PLAYWRIGHT_MAX_RETRIES} tentatives: {last_err}")

    async def close(self) -> None:
        """Ferme le navigateur."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._context = None
        self._page = None
        self._pages = []

    async def navigate(self, url: str) -> dict[str, str]:
        """Navigue vers une URL."""
        if not self._page:
            await self.start()
        await self._page.goto(url, wait_until="domcontentloaded")
        title = await self._page.title()
        current_url = self._page.url
        return {"url": current_url, "title": title, "status": "ok"}

    async def screenshot(self, full_page: bool = False) -> str:
        """Capture d'écran de la page. Retourne du base64 PNG."""
        if not self._page:
            await self.start()
        png_bytes = await self._page.screenshot(full_page=full_page)
        return base64.b64encode(png_bytes).decode("utf-8")

    async def click(self, selector: str) -> dict[str, str]:
        """Clique sur un élément."""
        if not self._page:
            await self.start()
        await self._page.click(selector)
        return {"action": "click", "selector": selector, "status": "ok"}

    async def type_text(self, selector: str, text: str, clear: bool = True) -> dict[str, str]:
        """Tape du texte dans un champ."""
        if not self._page:
            await self.start()
        if clear:
            await self._page.fill(selector, text)
        else:
            await self._page.type(selector, text)
        return {"action": "type", "selector": selector, "status": "ok"}

    async def get_page_content(self) -> str:
        """Retourne le contenu texte de la page."""
        if not self._page:
            await self.start()
        return await self._page.inner_text("body")

    async def get_links(self) -> list[dict[str, str]]:
        """Extrait tous les liens de la page."""
        if not self._page:
            await self.start()
        links = await self._page.eval_on_selector_all(
            "a[href]", """els => els.map(e => ({text: e.textContent.trim(), href: e.href}))"""
        )
        return links or []

    async def fill_form(self, fields: dict[str, str]) -> dict[str, str]:
        """Remplit un formulaire (champ -> valeur)."""
        if not self._page:
            await self.start()
        for selector, value in fields.items():
            await self._page.fill(selector, value)
        return {"action": "fill_form", "fields_count": len(fields), "status": "ok"}

    async def new_tab(self, url: str | None = None) -> dict[str, str]:
        """Ouvre un nouvel onglet."""
        if not self._context:
            await self.start()
        page = await self._context.new_page()
        self._pages.append(page)
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        title = await page.title()
        return {"tab_index": len(self._pages) - 1, "url": url or "", "title": title}

    async def close_tab(self, index: int | None = None) -> str:
        """Ferme un onglet."""
        if index is None:
            index = len(self._pages) - 1
        if 0 <= index < len(self._pages):
            await self._pages[index].close()
            self._pages.pop(index)
            return f"Onglet {index} fermé"
        raise ComputerUseError(f"Index d'onglet invalide : {index}")

    async def list_tabs(self) -> list[dict[str, Any]]:
        """Liste les onglets ouverts."""
        tabs = []
        for i, page in enumerate(self._pages):
            try:
                title = await page.title()
                url = page.url
            except Exception:
                title = "inaccessible"
                url = ""
            tabs.append({"index": i, "title": title, "url": url})
        return tabs


# ---------------------------------------------------------------------------
# 4. ScreenCapture — Captures d'écran via PowerShell + System.Drawing
# ---------------------------------------------------------------------------

class ScreenCapture:
    """Capture d'écran via PowerShell System.Drawing (zéro ctypes)."""

    def __init__(self) -> None:
        self._pil_available = True
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self._pil_available = False
            logger.warning("Pillow non installé. pip install Pillow")

    def capture_screen(self) -> bytes:
        """Capture l'écran complet. Retourne des octets PNG."""
        return self._ps_capture()

    def capture_region(self, x: int, y: int, w: int, h: int) -> bytes:
        """Capture une région spécifique de l'écran."""
        return self._ps_capture(x, y, w, h)

    def capture_window(self, title: str | None = None) -> bytes:
        """Capture la fenêtre active (ou par titre partiel)."""
        if not title:
            return self._ps_capture()
        # Trouver la fenêtre et capturer sa zone
        rect = self._ps_find_window_rect(title)
        if not rect:
            raise ComputerUseError(f"Fenêtre non trouvée : {title}")
        return self._ps_capture(rect["x"], rect["y"], rect["w"], rect["h"])

    def _ps_capture(self, x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> bytes:
        """Capture via PowerShell System.Drawing.Bitmap + PNG stream."""
        if w > 0 and h > 0:
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap({w}, {h})
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen({x}, {y}, 0, 0, (New-Object System.Drawing.Size({w}, {h})))
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
[Convert]::ToBase64String($ms.ToArray())
$ms.Close(); $g.Dispose(); $bmp.Dispose()
"""
        else:
            ps_script = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
[Convert]::ToBase64String($ms.ToArray())
$ms.Close(); $g.Dispose(); $bmp.Dispose()
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                raise ComputerUseError(f"ScreenCapture PS error: {result.stderr[:300]}")
            import base64
            return base64.b64decode(result.stdout.strip())
        except subprocess.TimeoutExpired:
            raise ComputerUseError("ScreenCapture timeout (15s)")

    @staticmethod
    def _ps_find_window_rect(title: str) -> dict | None:
        """Trouve la position/dimensions d'une fenêtre par titre partiel."""
        ps_script = f"""
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;using System.Text;
public static class WH{{
    [DllImport("user32.dll")]static extern bool EnumWindows(EP e,IntPtr l);
    [DllImport("user32.dll")]static extern int GetWindowText(IntPtr h,StringBuilder b,int m);
    [DllImport("user32.dll")]static extern bool GetWindowRect(IntPtr h,out R r);
    [DllImport("user32.dll")]static extern bool IsWindowVisible(IntPtr h);
    delegate bool EP(IntPtr h,IntPtr l);
    [StructLayout(LayoutKind.Sequential)]public struct R{{public int L,T,Ri,B;}}
    public static string F(string t){{
        string res=null;EnumWindows((h,lp)=>{{if(!IsWindowVisible(h))return true;
        var s=new StringBuilder(256);GetWindowText(h,s,256);
        if(s.ToString().IndexOf(t,StringComparison.OrdinalIgnoreCase)>=0){{
        R r;GetWindowRect(h,out r);
        res=r.L+","+r.T+","+(r.Ri-r.L)+","+(r.B-r.T);return false;}}
        return true;}},IntPtr.Zero);return res;
    }}
}}' -Language CSharp
[WH]::F("{title}")
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(",")
                return {"x": int(parts[0]), "y": int(parts[1]), "w": int(parts[2]), "h": int(parts[3])}
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# 5. WindowManagement — Gestion des fenêtres via PowerShell/C#
# ---------------------------------------------------------------------------

class WindowManagement:
    """Gestion des fenêtres via PowerShell/C# (zéro ctypes dans le process FastAPI)."""

    def list_windows(self) -> list[dict[str, Any]]:
        """Liste toutes les fenêtres visibles avec leurs informations."""
        # Use TAB delimiter (\t) to avoid conflicts with | in window titles
        ps_script = r"""
Add-Type -TypeDefinition 'using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public static class W{
    [DllImport("user32.dll")]static extern bool EnumWindows(WndEnumProc e,IntPtr l);
    [DllImport("user32.dll")]static extern int GetWindowText(IntPtr h,StringBuilder b,int m);
    [DllImport("user32.dll")]static extern int GetClassName(IntPtr h,StringBuilder b,int m);
    [DllImport("user32.dll")]static extern bool GetWindowRect(IntPtr h,out R r);
    [DllImport("user32.dll")]static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")]static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")]static extern IntPtr GetForegroundWindow();
    delegate bool WndEnumProc(IntPtr h,IntPtr l);
    [StructLayout(LayoutKind.Sequential)]public struct R{public int L,T,Ri,B;}
    public static string L(){
        var w=new List<string>();var fg=GetForegroundWindow();
        EnumWindows((h,lp)=>{if(!IsWindowVisible(h))return true;
        var t=new StringBuilder(256);GetWindowText(h,t,256);string s=t.ToString().Trim();
        if(string.IsNullOrEmpty(s))return true;
        var c=new StringBuilder(256);GetClassName(h,c,256);
        R r;GetWindowRect(h,out r);
        w.Add(((long)h)+"\t"+s+"\t"+c+"\t"+r.L+"\t"+r.T+"\t"+(r.Ri-r.L)+"\t"+(r.B-r.T)+"\t"+(IsIconic(h)?"1":"0")+"\t"+(h==fg?"1":"0"));
        return true;},IntPtr.Zero);
        return string.Join("\n",w);
    }
}' -Language CSharp
[W]::L()
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                logger.error("WindowManagement.list_windows error: %s", result.stderr[:300])
                return []
            windows = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 9:
                    try:
                        windows.append({
                            "hwnd": int(parts[0]),
                            "title": parts[1],
                            "class": parts[2],
                            "x": int(parts[3]),
                            "y": int(parts[4]),
                            "width": int(parts[5]),
                            "height": int(parts[6]),
                            "minimized": parts[7] == "1",
                            "foreground": parts[8] == "1",
                        })
                    except (ValueError, IndexError):
                        continue
            return windows
        except subprocess.TimeoutExpired:
            logger.error("WindowManagement.list_windows timeout")
            return []

    @staticmethod
    def _ps_find_hwnd(title: str) -> int | None:
        """Trouve un handle par titre partiel via subprocess PS."""
        escaped = title.replace("'", "''").replace('"', '\\"')
        ps_script = f"""
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;using System.Text;
public static class WF{{
    [DllImport("user32.dll")]static extern bool EnumWindows(EP e,IntPtr l);
    [DllImport("user32.dll")]static extern int GetWindowText(IntPtr h,StringBuilder b,int m);
    delegate bool EP(IntPtr h,IntPtr l);
    public static long F(string t){{
        long f=0;EnumWindows((h,lp)=>{{var s=new StringBuilder(256);GetWindowText(h,s,256);
        if(s.ToString().IndexOf(t,StringComparison.OrdinalIgnoreCase)>=0){{f=(long)h;return false;}}
        return true;}},IntPtr.Zero);return f;
    }}
}}' -Language CSharp
[WF]::F("{escaped}")
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                val = int(result.stdout.strip())
                return val if val > 0 else None
        except Exception:
            pass
        return None

    @staticmethod
    def _ps_window_action(hwnd: int, action: str) -> str:
        """Exécute une action fenêtre via PowerShell C# P/Invoke."""
        ps_script = f"""
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;
public static class WA{{
    [DllImport("user32.dll")]static extern bool ShowWindow(IntPtr h,int c);
    [DllImport("user32.dll")]static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")]static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")]static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")]static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l);
    public static string A(long hwnd,string act){{
        IntPtr h=new IntPtr(hwnd);
        if(act=="restore"){{if(IsIconic(h))ShowWindow(h,9);SetForegroundWindow(h);BringWindowToTop(h);return "ok";}}
        if(act=="minimize"){{ShowWindow(h,6);return "ok";}}
        if(act=="maximize"){{ShowWindow(h,3);return "ok";}}
        if(act=="close"){{PostMessage(h,0x0010,IntPtr.Zero,IntPtr.Zero);return "ok";}}
        return "unknown";
    }}
}}' -Language CSharp
[WA]::A({hwnd}, "{action}")
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
            )
            return "ok" if result.returncode == 0 and "ok" in result.stdout.strip().lower() else "error"
        except Exception as e:
            return f"error: {e}"

    def focus_window(self, title: str) -> str:
        hwnd = self._ps_find_hwnd(title)
        if not hwnd:
            raise ComputerUseError(f"Fenêtre non trouvée : {title}")
        self._ps_window_action(hwnd, "restore")
        return f"Focus appliqué sur : {title}"

    def minimize_window(self, title: str) -> str:
        hwnd = self._ps_find_hwnd(title)
        if not hwnd:
            raise ComputerUseError(f"Fenêtre non trouvée : {title}")
        self._ps_window_action(hwnd, "minimize")
        return f"Minimisé : {title}"

    def maximize_window(self, title: str) -> str:
        hwnd = self._ps_find_hwnd(title)
        if not hwnd:
            raise ComputerUseError(f"Fenêtre non trouvée : {title}")
        self._ps_window_action(hwnd, "maximize")
        return f"Maximisé : {title}"

    def close_window(self, title: str) -> str:
        hwnd = self._ps_find_hwnd(title)
        if not hwnd:
            raise ComputerUseError(f"Fenêtre non trouvée : {title}")
        self._ps_window_action(hwnd, "close")
        return f"Fermé (signal) : {title}"


# ---------------------------------------------------------------------------
# 6. ClipboardOps — Presse-papiers via PowerShell (zéro ctypes)
# ---------------------------------------------------------------------------

class ClipboardOps:
    """Lecture/écriture du presse-papiers via PowerShell subprocess."""

    def read_clipboard(self) -> str:
        """Lit le contenu texte du presse-papiers."""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return (result.stdout or "").rstrip("\r\n")
            return ""
        except Exception:
            return ""

    def write_clipboard(self, text: str) -> str:
        """Écrit du texte dans le presse-papiers."""
        try:
            # Écrire via un fichier temporaire pour éviter les problèmes d'escaping
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(text)
                tmppath = f.name
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-Command", f"Set-Clipboard -Path '{tmppath}'"],
                    capture_output=True, text=True, timeout=5,
                )
            finally:
                os.unlink(tmppath)
            if result.returncode == 0:
                return f"Presse-papiers mis à jour ({len(text)} caractères)"
            raise ComputerUseError(f"Clipboard write error: {result.stderr[:200]}")
        except ComputerUseError:
            raise
        except Exception as e:
            raise ComputerUseError(f"Clipboard write failed: {e}")


# ---------------------------------------------------------------------------
# 7. KeyboardMouse — Simulation clavier/souris via PowerShell/SendKeys
# ---------------------------------------------------------------------------

class KeyboardMouse:
    """Simulation de frappes clavier via PowerShell (zéro ctypes SendInput)."""

    # Mapping spécial pour SendKeys
    _SENDKEYS_MAP: dict[str, str] = {
        "ctrl": "^", "alt": "%", "shift": "+",
        "win": "#", "tab": "{TAB}", "enter": "~", "escape": "{ESC}", "esc": "{ESC}",
        "space": " ", "backspace": "{BACKSPACE}", "delete": "{DELETE}", "del": "{DELETE}",
        "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
        "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
        "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
        "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}",
        "f9": "{F9}", "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
        "printscreen": "{PRTSC}", "insert": "{INSERT}", "capslock": "{CAPSLOCK}",
        "numlock": "{NUMLOCK}", "scrolllock": "{SCROLLLOCK}",
    }

    def send_keys(self, keys: str) -> str:
        """
        Envoie une séquence de touches via PowerShell System.Windows.Forms.SendKeys.

        Formats supportés :
        - "ctrl+c"     → combinaison
        - "alt+tab"    → combinaison
        - "hello"      → frappe caractère par caractère
        - "ctrl+a,ctrl+c" → séquence de combinaisons
        """
        steps = [s.strip() for s in keys.split(",")]
        all_sendkeys = []

        for step in steps:
            if "+" in step:
                # Combinaison : ctrl+shift+esc → ^+{ESC}
                parts = step.lower().split("+")
                combo = ""
                for p in parts:
                    p = p.strip()
                    if p in self._SENDKEYS_MAP:
                        combo += self._SENDKEYS_MAP[p]
                    elif len(p) == 1:
                        combo += p
                    else:
                        combo += "{" + p.upper() + "}"
                all_sendkeys.append(combo)
            else:
                # Frappe simple caractère par caractère
                for char in step:
                    cl = char.lower()
                    if cl in self._SENDKEYS_MAP:
                        all_sendkeys.append(self._SENDKEYS_MAP[cl])
                    else:
                        # Caractères spéciaux SendKeys : { } + ^ % ~ ( ) [ ]
                        if char in "{}+^%~()[]":
                            all_sendkeys.append("{" + char + "}")
                        else:
                            all_sendkeys.append(char)

        full_keys = "".join(all_sendkeys)
        # Escape pour PowerShell
        escaped = full_keys.replace("'", "''")

        ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait('{escaped}')
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise ComputerUseError(f"SendKeys error: {result.stderr[:200]}")
            return f"Touche(s) envoyée(s) : {keys}"
        except subprocess.TimeoutExpired:
            raise ComputerUseError("SendKeys timeout (10s)")
        except Exception as e:
            raise ComputerUseError(f"SendKeys failed: {e}")


# ---------------------------------------------------------------------------
# 8. ComputerUseAPI — API REST FastAPI
# ---------------------------------------------------------------------------

class ComputerUseAPI:
    """
    API REST pour le contrôle de l'ordinateur via FastAPI.
    Regroupe toutes les capacités en endpoints HTTP.
    Port : 9307
    """

    def __init__(
        self,
        sandbox_root: str = DEFAULT_SANDBOX_DIR,
        full_access: bool = False,
        headless: bool = True,
    ) -> None:
        self.fs = FileSystemOps(sandbox_root=sandbox_root, full_access=full_access)
        self.cmd = CommandExecutor()
        self.web = WebController(headless=headless)
        self.screen = ScreenCapture()
        self.windows = WindowManagement()
        self.clipboard = ClipboardOps()
        self.keyboard = KeyboardMouse()

    def _register_routes(self, app) -> None:
        """Enregistre tous les endpoints sur l'application FastAPI."""

        # --- Santé ---
        @app.get("/health")
        async def health_check():
            return {"health": "ok", "module": "hermes_computer_use"}

        @app.get("/status")
        async def status_check():
            return {"status": "ok", "module": "hermes_computer_use", "sandbox": DEFAULT_SANDBOX_DIR}

        # --- Fichiers ---
        @app.post("/file/read")
        async def api_file_read(body: dict):
            try:
                content = await asyncio.to_thread(self.fs.read_file, body["path"], body.get("encoding", "utf-8"))
                return {"status": "ok", "content": content}
            except ComputerUseError as e:
                return error_response(ErrorCode.FILE_NOT_FOUND, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        @app.post("/file/write")
        async def api_file_write(body: dict):
            try:
                result = await asyncio.to_thread(self.fs.write_file, body["path"], body["content"])
                return {"status": "ok", "message": result}
            except ComputerUseError as e:
                return error_response(ErrorCode.PERMISSION_DENIED, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        @app.get("/file/list")
        async def api_file_list(path: str = "."):
            try:
                entries = await asyncio.to_thread(self.fs.list_dir, path)
                return {"status": "ok", "entries": entries}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.post("/file/delete")
        async def api_file_delete(body: dict):
            try:
                result = await asyncio.to_thread(self.fs.delete_file, body["path"], explicit=body.get("explicit", False))
                return {"status": "ok", "message": result}
            except ComputerUseError as e:
                return error_response(ErrorCode.PERMISSION_DENIED, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        @app.post("/file/stat")
        async def api_file_stat(body: dict):
            try:
                info = await asyncio.to_thread(self.fs.stat, body["path"])
                return {"status": "ok", "info": info}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        # --- Commandes ---
        @app.post("/command/execute")
        async def api_command_execute(body: dict):
            try:
                result = await asyncio.to_thread(
                    self.cmd.execute,
                    command=body["command"],
                    timeout=body.get("timeout"),
                    cwd=body.get("cwd"),
                    explicit=body.get("explicit", False),
                )
                return result
            except ComputerUseError as e:
                return error_response(ErrorCode.DANGEROUS_COMMAND, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        # --- Web ---
        @app.post("/web/navigate")
        async def api_web_navigate(body: dict):
            try:
                result = await self.web.navigate(body["url"])
                return {"status": "ok", **result}
            except ComputerUseError as e:
                return error_response(ErrorCode.BROWSER_NOT_READY, str(e))
            except Exception as e:
                return error_response(ErrorCode.WEB_NAVIGATION_ERROR, str(e))

        @app.post("/web/screenshot")
        async def api_web_screenshot(body: dict = None):
            try:
                b64 = await self.web.screenshot(full_page=body.get("full_page", False) if body else False)
                return {"status": "ok", "image_b64": b64, "format": "png"}
            except ComputerUseError as e:
                return error_response(ErrorCode.BROWSER_NOT_READY, str(e))
            except Exception as e:
                return error_response(ErrorCode.WEB_NAVIGATION_ERROR, str(e))

        @app.post("/web/click")
        async def api_web_click(body: dict):
            try:
                result = await self.web.click(body["selector"])
                return {"status": "ok", **result}
            except Exception as e:
                return error_response(ErrorCode.WEB_CLICK_ERROR, str(e))

        @app.post("/web/type")
        async def api_web_type(body: dict):
            try:
                result = await self.web.type_text(body["selector"], body["text"])
                return {"status": "ok", **result}
            except Exception as e:
                return error_response(ErrorCode.WEB_TYPE_ERROR, str(e))

        @app.get("/web/content")
        async def api_web_content():
            try:
                content = await self.web.get_page_content()
                return {"status": "ok", "content": content[:50000]}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.get("/web/links")
        async def api_web_links():
            try:
                links = await self.web.get_links()
                return {"status": "ok", "links": links}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.get("/web/tabs")
        async def api_web_tabs():
            try:
                tabs = await self.web.list_tabs()
                return {"status": "ok", "tabs": tabs}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        # --- Écran ---
        @app.post("/screen/capture")
        async def api_screen_capture(body: dict = None):
            try:
                if body and body.get("window"):
                    png = await asyncio.to_thread(self.screen.capture_window, body["window"])
                elif body and body.get("region"):
                    r = body["region"]
                    png = await asyncio.to_thread(self.screen.capture_region, r["x"], r["y"], r["w"], r["h"])
                else:
                    png = await asyncio.to_thread(self.screen.capture_screen)
                fmt = (body or {}).get("format", "png").lower()
                if fmt == "jpeg" or fmt == "jpg":
                    # Convertir PNG → JPEG pour réduire la taille
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(png))
                    if img.mode == "RGBA":
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    quality = (body or {}).get("quality", 80)
                    img.save(buf, format="JPEG", quality=quality)
                    jpg_data = buf.getvalue()
                    return {"status": "ok", "image_b64": base64.b64encode(jpg_data).decode(), "format": "jpeg", "size_bytes": len(jpg_data)}
                return {"status": "ok", "image_b64": base64.b64encode(png).decode(), "format": "png", "size_bytes": len(png)}
            except ComputerUseError as e:
                return error_response(ErrorCode.SCREENSHOT_ERROR, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        # --- Fenêtres ---
        @app.get("/windows")
        async def api_list_windows():
            try:
                wins = await asyncio.to_thread(self.windows.list_windows)
                safe = [
                    {k: (str(v) if k == "hwnd" else v) for k, v in w.items()}
                    for w in wins
                ]
                return {"status": "ok", "windows": safe}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.post("/windows/focus")
        async def api_focus_window(body: dict):
            try:
                result = await asyncio.to_thread(self.windows.focus_window, body["title"])
                return {"status": "ok", "message": result}
            except ComputerUseError as e:
                return error_response(ErrorCode.WINDOW_NOT_FOUND, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        @app.post("/windows/close")
        async def api_close_window(body: dict):
            try:
                result = await asyncio.to_thread(self.windows.close_window, body["title"])
                return {"status": "ok", "message": result}
            except ComputerUseError as e:
                return error_response(ErrorCode.WINDOW_NOT_FOUND, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        # --- Presse-papiers (PowerShell-based to avoid ctypes segfault in Python 3.13) ---
        @app.get("/clipboard")
        async def api_read_clipboard():
            try:
                import subprocess
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return {"status": "ok", "content": result.stdout.rstrip()}
                return {"status": "ok", "content": ""}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @app.post("/clipboard/write")
        async def api_write_clipboard(body: dict):
            try:
                import subprocess
                text = body.get("text", "")
                # Escape text for PowerShell
                escaped = text.replace("'", "''")
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{escaped}'"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return {"status": "ok", "message": f"Clipboard updated ({len(text)} chars)"}
                return error_response(ErrorCode.CLIPBOARD_ERROR, result.stderr[:200])
            except Exception as e:
                return error_response(ErrorCode.CLIPBOARD_ERROR, str(e))

        # --- Clavier ---
        @app.post("/keys/send")
        async def api_send_keys(body: dict):
            try:
                result = await asyncio.to_thread(self.keyboard.send_keys, body["keys"])
                return {"status": "ok", "message": result}
            except ComputerUseError as e:
                return error_response(ErrorCode.KEYBOARD_ERROR, str(e))
            except Exception as e:
                return error_response(ErrorCode.INTERNAL_ERROR, str(e))

        # --- Commande haut niveau ---
        @app.post("/computer/ask")
        async def api_computer_ask(body: dict):
            """
            Endpoint haut niveau qui interprète une question en langage naturel
            et délègue au bon sous-système. En mode simplifié : analyse les
            mots-clés pour choisir l'action appropriée.
            """
            query = body.get("query", "").lower().strip()
            params = body.get("params", {})

            try:
                if any(w in query for w in ["lire fichier", "read file", "ouvrir fichier"]):
                    path = params.get("path", "")
                    content = await asyncio.to_thread(self.fs.read_file, path)
                    return {"status": "ok", "action": "file_read", "content": content}

                elif any(w in query for w in ["écrire fichier", "write file", "sauvegarder"]):
                    result = await asyncio.to_thread(self.fs.write_file, params["path"], params["content"])
                    return {"status": "ok", "action": "file_write", "message": result}

                elif any(w in query for w in ["exécuter", "execute", "commande", "run"]):
                    result = await asyncio.to_thread(self.cmd.execute, params.get("command", query), explicit=params.get("explicit", False))
                    return {"status": "ok", "action": "command", **result}

                elif any(w in query for w in ["naviguer", "navigate", "ouvrir url", "go to"]):
                    result = await self.web.navigate(params.get("url", ""))
                    return {"status": "ok", "action": "web_navigate", **result}

                elif any(w in query for w in ["screenshot", "capture", "capturer écran"]):
                    png = await asyncio.to_thread(self.screen.capture_screen)
                    return {"status": "ok", "action": "screen_capture", "image_b64": base64.b64encode(png).decode()}

                elif any(w in query for w in ["liste fenêtre", "list window", "fenêtres"]):
                    wins = await asyncio.to_thread(self.windows.list_windows)
                    return {"status": "ok", "action": "list_windows", "windows": wins}

                elif any(w in query for w in ["presse-papier", "clipboard", "copier"]):
                    text = await asyncio.to_thread(self.clipboard.read_clipboard)
                    return {"status": "ok", "action": "clipboard_read", "content": text}

                else:
                    return {
                        "status": "unknown",
                        "message": f"Impossible d'interpréter : {query}. "
                                   "Utilisez les endpoints spécifiques.",
                    }
            except Exception as e:
                return {"status": "error", "message": str(e)}

    def create_app(self):
        """Crée l'application FastAPI avec auth, rate limiting et tous les routes."""
        try:
            from fastapi import FastAPI, Request
            from fastapi.middleware.cors import CORSMiddleware
            from fastapi.responses import JSONResponse
        except ImportError:
            raise ComputerUseError(
                "FastAPI non installé. Installer avec : pip install fastapi uvicorn"
            )

        app = FastAPI(
            title="HERMES Computer Use API",
            description="API de contrôle de l'ordinateur pour HERMES OMEGA",
            version="2.0.0",
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # --- Middleware: Authentification API Key ---
        @app.middleware("http")
        async def auth_and_rate_limit(request: Request, call_next):
            # Skip auth/rate-limit pour les endpoints de santé
            if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
                return await call_next(request)

            # Authentification
            if HERMES_API_KEY:
                api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
                if api_key != HERMES_API_KEY:
                    return JSONResponse(
                        status_code=401,
                        content=error_response(ErrorCode.UNAUTHORIZED, "Clé API invalide ou manquante"),
                    )

            # Rate limiting (par IP source)
            client_key = request.client.host if request.client else "unknown"
            if not rate_limiter.allow(client_key):
                logger.warning("Rate limit atteint pour %s", client_key)
                return JSONResponse(
                    status_code=429,
                    content=error_response(ErrorCode.RATE_LIMITED, f"Trop de requêtes. Limite: {RATE_LIMIT_MAX}/{RATE_LIMIT_WINDOW}s"),
                )

            return await call_next(request)

        self._register_routes(app)

        # --- Lifespan (startup + shutdown) ---
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(app_instance):
            logger.info("HERMES Computer Use API v2.0 démarrage")
            logger.info("Sandbox: %s | Auth: %s | Rate limit: %d/%ds",
                        DEFAULT_SANDBOX_DIR,
                        "activé" if HERMES_API_KEY else "désactivé",
                        RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)
            try:
                await self.web.start()
                logger.info("Navigateur Playwright prêt")
            except Exception as e:
                logger.warning("Navigateur non démarré au startup (les endpoints web renverront une erreur) : %s", e)
            yield
            logger.info("HERMES Computer Use API arrêt")
            await self.web.close()

        app.router.lifespan_context = lifespan

        # -----------------------------------------------------------------
        # -----------------------------------------------------------------
        # HERMES OS - Cockpit Simplifié
        # -----------------------------------------------------------------
        from fastapi.responses import HTMLResponse
        from fastapi import UploadFile, File, Form

        COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HERMES - Mon OS</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--surface:#161b22;--surface2:#1c2333;--border:#30363d;--text:#e6edf3;--text2:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--orange:#d29922;--user-bg:#1f3a5f;--bot-bg:#1c2333}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.header-left{display:flex;align-items:center;gap:12px}
.header-left h1{font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.badge{font-size:11px;color:var(--green);padding:2px 8px;background:rgba(63,185,80,.1);border-radius:10px}
.header-right{display:flex;gap:6px}
.hbtn{background:none;border:1px solid var(--border);color:var(--text2);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;transition:all .15s}
.hbtn:hover{border-color:var(--blue);color:var(--blue)}
.main{display:flex;flex:1;overflow:hidden}
.sidebar{width:200px;background:var(--surface);border-right:1px solid var(--border);padding:10px;overflow-y:auto;flex-shrink:0}
.stitle{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:1px;margin:12px 0 6px 4px}
.sbtn{display:flex;align-items:center;gap:8px;width:100%;text-align:left;background:none;border:1px solid transparent;color:var(--text);padding:9px 10px;border-radius:8px;margin-bottom:2px;cursor:pointer;font-size:13px;transition:all .12s}
.sbtn:hover{background:rgba(88,166,255,.06);border-color:var(--border)}
.sbtn .si{font-size:16px;width:22px;text-align:center}
.chat{flex:1;display:flex;flex-direction:column;min-width:0}
.msgs{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:12px;scroll-behavior:smooth}
.msg{display:flex;gap:8px;max-width:88%;animation:fi .2s}
@keyframes fi{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.msg.u{align-self:flex-end;flex-direction:row-reverse}
.msg.b{align-self:flex-start}
.av{width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}
.msg.u .av{background:var(--user-bg);border:1px solid rgba(88,166,255,.3)}
.msg.b .av{background:var(--bot-bg);border:1px solid var(--border)}
.bub{padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.5;word-break:break-word}
.msg.u .bub{background:var(--user-bg);border:1px solid rgba(88,166,255,.15);border-bottom-right-radius:4px}
.msg.b .bub{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:4px}
.bub img{max-width:100%;border-radius:8px;margin-top:8px;cursor:pointer;max-height:350px}
.bub pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;margin-top:8px;font-size:12px;overflow-x:auto;white-space:pre-wrap;max-height:300px;overflow-y:auto}
.bub code{font-family:'Cascadia Code','Fira Code',monospace;font-size:12px}
.bub .lbl{font-size:10px;color:var(--text2);margin-bottom:3px}
.bub .file-tag{display:inline-flex;align-items:center;gap:6px;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;margin-top:6px;font-size:12px}
.file-tag .fi{font-size:16px}
.typing{display:flex;gap:4px;padding:8px 14px}
.typing span{width:5px;height:5px;border-radius:50%;background:var(--text2);animation:bk 1.4s infinite both}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes bk{0%,80%,100%{opacity:.3}40%{opacity:1}}
.inp{padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;flex-shrink:0;background:var(--surface)}
.inp-main{flex:1;position:relative;display:flex;align-items:flex-end;gap:6px}
#inp{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:10px;font-size:14px;font-family:inherit;resize:none;outline:none;max-height:100px;line-height:1.4}
#inp:focus{border-color:var(--blue)}
#inp::placeholder{color:var(--text2)}
.attach-btn{background:none;border:none;color:var(--text2);font-size:20px;cursor:pointer;padding:4px 2px;transition:color .15s}
.attach-btn:hover{color:var(--blue)}
.file-chip{display:inline-flex;align-items:center;gap:4px;background:rgba(88,166,255,.1);border:1px solid rgba(88,166,255,.2);color:var(--blue);padding:2px 8px;border-radius:12px;font-size:11px;max-width:150px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.file-chip button{background:none;border:none;color:var(--text2);cursor:pointer;font-size:14px;padding:0 2px}
#send{background:var(--blue);border:none;color:#fff;padding:10px 18px;border-radius:10px;cursor:pointer;font-size:14px;font-weight:500;transition:all .12s;white-space:nowrap}
#send:hover{opacity:.85}
#send:disabled{opacity:.4;cursor:not-allowed}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;max-width:420px;width:90%}
.modal h2{font-size:16px;margin-bottom:12px}
.modal p{font-size:13px;color:var(--text2);line-height:1.5;margin-bottom:8px}
.modal code{background:var(--bg);padding:1px 5px;border-radius:3px;font-size:12px}
.modal .close{float:right;background:none;border:none;color:var(--text2);font-size:18px;cursor:pointer}
.example{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px;margin:8px 0;font-size:12px;color:var(--text2);line-height:1.6}
.example strong{color:var(--text)}
.mob-tog{display:none;background:none;border:none;color:var(--text2);font-size:18px;cursor:pointer}
@media(max-width:700px){.sidebar{display:none}.sidebar.open{display:block;position:fixed;left:0;top:44px;bottom:0;z-index:50}.mob-tog{display:block}}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <button class="mob-tog" onclick="document.querySelector('.sidebar').classList.toggle('open')">&#9776;</button>
    <h1><span class="dot"></span>HERMES</h1>
    <span class="badge" id="st">En ligne</span>
  </div>
  <div class="header-right">
    <button class="hbtn" onclick="showHelp()">Aide</button>
    <button class="hbtn" onclick="clearChat()">Effacer</button>
  </div>
</div>
<div class="main">
  <div class="sidebar">
    <div class="stitle">Voir</div>
    <button class="sbtn" onclick="q('screenshot')"><span class="si">&#128247;</span>Mon ecran</button>
    <button class="sbtn" onclick="q('windows')"><span class="si">&#128467;</span>Mes fenetres</button>
    <button class="sbtn" onclick="q('clipboard')"><span class="si">&#128203;</span>Presse-papier</button>
    <div class="stitle">Agir</div>
    <button class="sbtn" onclick="q('open https://google.com')"><span class="si">&#127760;</span>Ouvrir un site</button>
    <button class="sbtn" onclick="q('taper ')" id="typeBtn"><span class="si">&#9000;</span>Taper du texte</button>
    <button class="sbtn" onclick="q('copier ')" id="copyBtn"><span class="si">&#128221;</span>Copier du texte</button>
    <div class="stitle">Systeme</div>
    <button class="sbtn" onclick="q('infos')"><span class="si">&#128187;</span>Infos PC</button>
    <button class="sbtn" onclick="q('processus')"><span class="si">&#9881;</span>Processus</button>
    <button class="sbtn" onclick="q('reseau')"><span class="si">&#128225;</span>Reseau</button>
    <button class="sbtn" onclick="q('fichiers')"><span class="si">&#128193;</span>Fichiers</button>
  </div>
  <div class="chat">
    <div class="msgs" id="msgs">
      <div class="msg b"><div class="av">&#9881;</div><div><div class="bub">
        <div class="lbl">HERMES</div>
        Salut ! Je suis HERMES, ton assistant.<br><br>
        Dis-moi ce que tu veux faire, ou utilise les boutons sur la gauche.<br><br>
        Tu peux aussi m'envoyer des fichiers (PDF, images, documents...).<br><br>
        <strong>Exemples :</strong><br>
        &#8226; "Prends une photo de mon ecran"<br>
        &#8226; "Ouvre YouTube"<br>
        &#8226; "Quels sont mes processus ?"<br>
        &#8226; Envoie un PDF et demande-moi de le lire
      </div></div></div>
    </div>
    <div class="inp">
      <div class="inp-main">
        <button class="attach-btn" onclick="document.getElementById('fileIn').click()" title="Joindre un fichier">&#128206;</button>
        <input type="file" id="fileIn" multiple style="display:none" onchange="onFiles(this)">
        <div id="chips" style="display:flex;gap:4px;flex-wrap:wrap"></div>
        <textarea id="inp" rows="1" placeholder="Dis-moi ce que tu veux..." onkeydown="onKey(event)"></textarea>
      </div>
      <button id="send" onclick="send()">Envoyer</button>
    </div>
  </div>
</div>

<div class="overlay" id="helpOv" onclick="if(event.target===this)hideHelp()">
  <div class="modal">
    <button class="close" onclick="hideHelp()">&times;</button>
    <h2>Comment ca marche ?</h2>
    <p>Tape ce que tu veux en francais, HERMES s'occupe du reste.</p>
    <div class="example">
      <strong>Ecran :</strong> "prends une photo" ou "screenshot"<br>
      <strong>Fenetres :</strong> "montre mes fenetres" ou "ferme Chrome"<br>
      <strong>Web :</strong> "ouvre youtube.com" ou "que voit la page ?"<br>
      <strong>Texte :</strong> "taper bonjour" ou "copier ceci"<br>
      <strong>Systeme :</strong> "infos PC" ou "mes processus" ou "reseau"<br>
      <strong>Fichiers :</strong> "lire C:\test.txt" ou "fichiers sur C:\"<br>
      <strong>Pieces jointes :</strong> Clique sur &#128206; pour envoyer un PDF ou une image
    </div>
    <p>HERMES utilise une IA locale (Ollama) pour comprendre le francais.</p>
  </div>
</div>

<script>
const M=document.getElementById('msgs'),I=document.getElementById('inp'),S=document.getElementById('send');
let busy=false,files=[];
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
function now(){return new Date().toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'})}
function scroll(){M.scrollTop=M.scrollHeight}
function add(role,html,img){
  const d=document.createElement('div');d.className='msg '+(role==='user'?'u':'b');
  d.innerHTML='<div class="av">'+(role==='user'?'&#128100;':'&#9881;')+'</div><div><div class="bub"><div class="lbl">'+(role==='user'?'Toi':'HERMES')+'</div>'+html+'</div></div>';
  M.appendChild(d);scroll();return d;
}
function typing(){const d=document.createElement('div');d.className='msg b';d.id='tp';d.innerHTML='<div class="av">&#9881;</div><div><div class="bub"><div class="typing"><span></span><span></span><span></span></div></div></div>';M.appendChild(d);scroll()}
function untyp(){const t=document.getElementById('tp');if(t)t.remove()}
function fmt(d){
  let h='';
  if(d.text)h+=d.text.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/```(.+?)```/gs,'<pre><code>$1</code></pre>').replace(/\n/g,'<br>');
  if(d.image)h+='<img src="data:image/png;base64,'+d.image+'" onclick="window.open(this.src)">';
  if(d.file_info)h+='<div class="file-tag"><span class="fi">&#128196;</span>'+esc(d.file_info)+'</div>';
  if(d.stdout)h+='<pre><code>'+esc(d.stdout)+'</code></pre>';
  if(d.stderr&&d.stderr.trim())h+='<pre><code style="color:var(--red)">'+esc(d.stderr)+'</code></pre>';
  return h||'<em style="color:var(--text2)">(aucun resultat)</em>';
}
function readFileAsBase64(file){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(file)})}
async function sendToApi(msg,files){
  if(files.length>0){
    const fileData=await Promise.all(files.map(async f=>({name:f.name,data:await readFileAsBase64(f)})));
    const r=await fetch('/api/chat/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,files:fileData})});
    return r.json();
  }
  const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  return r.json();
}
async function send(){
  const t=I.value.trim();if(!t&&files.length===0||busy)return;
  const fileNames=files.map(f=>f.name);
  let userHtml=esc(t);
  if(fileNames.length)userHtml+='<div class="file-tag"><span class="si">&#128206;</span>'+esc(fileNames.join(', '))+'</div>';
  add('user',userHtml);
  I.value='';I.style.height='auto';document.getElementById('chips').innerHTML='';files=[];
  busy=true;S.disabled=true;typing();
  try{
    const r=await sendToApi(t,files);
    untyp();add('bot',fmt(r));
  }catch(e){untyp();add('bot','<span style="color:var(--red)">Erreur : '+esc(e.message)+'</span>');}
  busy=false;S.disabled=false;I.focus();
}
function onFiles(inp){files=Array.from(inp.files);const c=document.getElementById('chips');c.innerHTML='';
  files.forEach((f,i)=>{const s=document.createElement('span');s.className='file-chip';s.textContent=f.name;
    const b=document.createElement('button');b.textContent='x';b.onclick=()=>{files.splice(i,1);onFiles({files:files})};s.appendChild(b);c.appendChild(s);});
  if(files.length)I.focus();
}
function q(cmd){I.value=cmd;if(cmd.endsWith(' '))I.focus();else send()}
function onKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
function showHelp(){document.getElementById('helpOv').classList.add('show')}
function hideHelp(){document.getElementById('helpOv').classList.remove('show')}
function clearChat(){M.innerHTML='';add('bot','Conversation effacee. Je suis pret.')}
I.addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,100)+'px'});
I.focus();
</script>
</body>
</html>"""

        @app.get("/", response_class=HTMLResponse)
        async def cockpit_ui():
            """Page du cockpit simplifie."""
            return COCKPIT_HTML

        # --- Traitement des messages + fichiers ---
        @app.post("/api/chat")
        async def chat_endpoint(body: dict):
            """Recoit un message JSON."""
            message = str(body.get("message", "")).strip()
            if not message:
                return {"text": "Envoie-moi quelque chose !"}
            logger.info("Chat: %s", message[:100])
            result = await _interpret_simple(message, [], self)
            return result

        @app.post("/api/chat/upload")
        async def chat_upload(body: dict):
            """Recoit un message + fichiers en base64 (JSON)."""
            import PyPDF2
            import io as _io

            message = str(body.get("message", "")).strip()
            raw_files = body.get("files", [])
            file_contents = []

            for rf in raw_files:
                fname = rf.get("name", "unknown")
                fdata = rf.get("data", "")
                fname_lower = fname.lower()
                fsize = len(fdata) * 3 // 4 if fdata else 0  # approximate base64 size

                if fname_lower.endswith(".pdf") and fdata:
                    try:
                        raw = base64.b64decode(fdata)
                        reader = PyPDF2.PdfReader(_io.BytesIO(raw))
                        text_parts = [page.extract_text() or "" for page in reader.pages[:20]]
                        pdf_text = "\n".join(t for t in text_parts if t.strip())
                        file_contents.append({"name": fname, "type": "pdf", "size": len(raw), "text": pdf_text, "pages": len(reader.pages)})
                    except Exception as e:
                        file_contents.append({"name": fname, "type": "pdf", "size": fsize, "error": str(e)})
                elif fname_lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")) and fdata:
                    file_contents.append({"name": fname, "type": "image", "size": fsize, "b64": fdata})
                elif fdata:
                    try:
                        text = base64.b64decode(fdata).decode("utf-8", errors="replace")
                        file_contents.append({"name": fname, "type": "text", "size": len(text), "text": text})
                    except Exception:
                        file_contents.append({"name": fname, "type": "binary", "size": fsize})
                else:
                    file_contents.append({"name": fname, "type": "unknown", "size": fsize})

            if not message and file_contents:
                names = [fc["name"] for fc in file_contents]
                message = "Voici le(s) fichier(s) : " + ", ".join(names)
            if not message:
                return {"text": "Envoie-moi quelque chose !"}
            logger.info("Chat upload: %s | %d fichier(s)", message[:100], len(file_contents))
            result = await _interpret_simple(message, file_contents, self)
            return result

            # Si pas de message texte mais des fichiers, generer un message par defaut
            if not message and file_contents:
                names = [fc["name"] for fc in file_contents]
                message = "Voici le(s) fichier(s) : " + ", ".join(names)

            if not message:
                return {"text": "Envoie-moi quelque chose !"}

            logger.info("Chat: %s | %d fichier(s)", message[:100], len(file_contents))

            # Interpreter et executer
            result = await _interpret_simple(message, file_contents, self)
            return result

        async def _interpret_simple(message: str, file_contents: list[dict], api: "ComputerUseAPI") -> dict:
            """Interprete un message simple et execute l'action."""
            msg = message.lower().strip()

            # --- Fichiers joints en priorite ---
            if file_contents:
                # Si on a un PDF avec du texte, on peut le lire et le resumer via Ollama
                pdf_texts = [fc["text"] for fc in file_contents if fc.get("type") == "pdf" and fc.get("text")]
                txt_texts = [fc["text"] for fc in file_contents if fc.get("type") == "text" and fc.get("text")]
                images = [fc["b64"] for fc in file_contents if fc.get("type") == "image" and fc.get("b64")]

                result_parts = []

                # Afficher les fichiers recus
                for fc in file_contents:
                    sz = fc["size"]
                    if sz > 1024*1024: sz_s = f"{sz/(1024*1024):.1f} Mo"
                    elif sz > 1024: sz_s = f"{sz/1024:.1f} Ko"
                    else: sz_s = f"{sz} octets"

                    if fc["type"] == "pdf":
                        pages = fc.get("pages", "?")
                        text_len = len(fc.get("text", ""))
                        result_parts.append(f"**📄 {fc['name']}** ({sz_s}, {pages} pages)")
                        if text_len > 0:
                            # Montrer un apercu du contenu
                            preview = fc["text"][:3000].replace("\n", "\n")
                            result_parts.append(f"\n```\n{preview}\n```")
                        else:
                            result_parts.append("(Impossible d'extraire le texte)")
                    elif fc["type"] == "image":
                        result_parts.append(f"**🖼 {fc['name']}** ({sz_s})")
                        return {"text": "\n".join(result_parts), "image": fc["b64"]}
                    elif fc["type"] == "text":
                        result_parts.append(f"**📝 {fc['name']}** ({sz_s})")
                        text = fc["text"]
                        if len(text) > 5000:
                            result_parts.append(f"\n```\n{text[:5000]}\n```\n\n(_... tronque a 5000 caracteres sur {len(text)}_)")
                        else:
                            result_parts.append(f"\n```\n{text}\n```")
                    else:
                        result_parts.append(f"**📎 {fc['name']}** ({sz_s})")

                # Si on a du texte extrait + un message de l'utilisateur, utiliser Ollama pour analyser
                user_wants_analysis = any(w in msg for w in ["analyse", "resume", "résumé", "lit", "lis", "lis ", "code", "explique", "trouve", "cherch", "que dit", "contien", "contient"])
                all_text = "\n\n".join(pdf_texts + txt_texts)

                if all_text and user_wants_analysis and len(all_text) > 50:
                    try:
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            oc = await client.get("http://127.0.0.1:11434/api/tags")
                            if oc.status_code == 200:
                                models = oc.json().get("models", [])
                                mn = None
                                for pref in ["qwen2.5-coder:7b", "deepseek-r1:7b", "phi3:mini"]:
                                    if any(pref in m.get("name","") for m in models):
                                        mn = pref; break
                                if not mn and models: mn = models[0]["name"]

                                if mn:
                                    ai_prompt = (
                                        "Tu es HERMES, un assistant. Le user t'envoie un document.\n"
                                        "Reponds en francais, de maniere simple et claire.\n"
                                        "Document:\n" + all_text[:8000] + "\n\n"
                                        "Demande du user: " + message
                                    )
                                    _or = await client.post(
                                        "http://127.0.0.1:11434/api/generate",
                                        json={"model": mn, "prompt": ai_prompt, "stream": False, "options": {"num_predict": 1500}}
                                    )
                                    ai_text = _or.json().get("response", "").strip()
                                    if ai_text:
                                        result_parts.append(f"\n---\n**Analyse IA :**\n{ai_text}")
                    except Exception:
                        pass

                return {"text": "\n".join(result_parts)}

            # === COMMANDES SIMPLES ===

            # Screenshot
            if msg in ("screenshot", "capture", "ecran", "screen", "photo", "prendre une photo", "photo ecran", "mon ecran"):
                try:
                    png = api.screen.capture_screen()
                    return {"text": "Voici ton ecran :", "image": base64.b64encode(png).decode()}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Windows
            if msg in ("windows", "fenetres", "fenêtres", "mes fenetres", "mes fenêtres"):
                try:
                    wins = api.windows.list_windows()
                    if not wins:
                        return {"text": "Aucune fenetre ouverte."}
                    lines = []
                    for w in wins:
                        fg = " &#9733;" if w.get("foreground") else ""
                        mini = " [reduite]" if w.get("minimized") else ""
                        lines.append(f"&#8226; {w['title'][:55]}{fg}{mini}")
                    return {"text": f"**{len(wins)} fenetres ouvertes :**\n" + "\n".join(lines)}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Clipboard
            if msg in ("clipboard", "presse-papier", "copie"):
                try:
                    text = api.clipboard.read_clipboard()
                    if text:
                        return {"text": f"**Presse-papier :**\n```\n{text[:2000]}\n```"}
                    return {"text": "Le presse-papier est vide."}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Browser content
            if msg in ("browser content", "page", "contenu", "page web", "contenu page", "que voit la page"):
                try:
                    content = await api.web.get_content()
                    return {"text": content[:5000] if content else "Page vide ou inaccessible."}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Links
            if msg in ("links", "liens", "page links"):
                try:
                    links = await api.web.get_links()
                    if not links:
                        return {"text": "Aucun lien sur cette page."}
                    lines = [f"&#8226; [{l.get('text','')[:40]}]({l.get('href','')})" for l in links[:30]]
                    return {"text": f"**{len(links)} liens :**\n" + "\n".join(lines)}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Infos PC
            if msg in ("infos", "info pc", "infos pc", "systeminfo", "systeme", "configuration"):
                try:
                    r = api.cmd.execute("systeminfo", timeout=15)
                    return {"text": f"**Infos PC :**\n```\n{r.get('stdout','')[:3000]}\n```", "stdout": r.get("stdout","")[:3000], "stderr": r.get("stderr","")}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Processus
            if msg in ("processus", "process", "taches"):
                try:
                    r = api.cmd.execute('tasklist /FI "STATUS eq running" /FO TABLE', timeout=15)
                    return {"text": "**Processus actifs :**\n", "stdout": r.get("stdout","")[:3000]}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Reseau
            if msg in ("reseau", "network", "ip", "wifi", "internet"):
                try:
                    r = api.cmd.execute("ipconfig", timeout=10)
                    return {"text": "**Reseau :**\n", "stdout": r.get("stdout","")[:3000]}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Fichiers racine
            if msg in ("fichiers", "files", "mes fichiers"):
                try:
                    r = api.cmd.execute("dir C:\\ /B", timeout=10)
                    return {"text": "**Fichiers C:\\ :**\n", "stdout": r.get("stdout","")[:2000]}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Help
            if msg in ("help", "aide", "?", "commandes"):
                return {"text": (
                    "Voici ce que tu peux me demander :\n\n"
                    "&#8226; **Prendre une photo** de mon ecran\n"
                    "&#8226; **Montrer mes fenetres** ouvertes\n"
                    "&#8226; **Presse-papier** (lire / copier)\n"
                    "&#8226; **Ouvrir un site** (ex: ouvre youtube.com)\n"
                    "&#8226; **Taper du texte** au clavier\n"
                    "&#8226; **Lire un fichier** (ex: lire C:\\test.txt)\n"
                    "&#8226; **Executer une commande** (ex: run dir)\n"
                    "&#8226; **Infos PC** / processus / reseau\n"
                    "&#8226; **Envoyer des fichiers** (PDF, images, textes)\n\n"
                    "Tu peux aussi parler en francais, l'IA locale comprend !"
                )}

            # === COMMANDES AVEC PARAMETRES ===

            # Focus window
            m = re.match(r"(?:focus|mettre|active|affiche)\s+(?:la\s+fenetre\s+)?(.+)", message, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                try:
                    result = api.windows.focus_window(title)
                    return {"text": result}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Close window
            m = re.match(r"(?:close|ferme|fermer)\s+(?:la\s+fenetre\s+)?(.+)", message, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                try:
                    result = api.windows.close_window(title)
                    return {"text": result}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Copy
            m = re.match(r"(?:copy|copier|copie)\s+(.+)", message, re.IGNORECASE)
            if m:
                text = m.group(1).strip()
                try:
                    api.clipboard.write_clipboard(text)
                    return {"text": f"**Copie !**\n```\n{text[:500]}\n```"}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Type
            m = re.match(r"(?:type|taper|ecrire|ecrire)\s+(.+)", message, re.IGNORECASE)
            if m:
                text = m.group(1).strip()
                try:
                    api.keyboard.send_keys(text)
                    return {"text": f"**Texte tape :** {text[:100]}"}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Keys
            m = re.match(r"(?:keys?|touches?)\s+(.+)", message, re.IGNORECASE)
            if m:
                keys = m.group(1).strip()
                try:
                    api.keyboard.send_keys(keys)
                    return {"text": f"**Touches envoyees :** {keys}"}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Run
            m = re.match(r"(?:run|exec)\s+(.+)", message, re.IGNORECASE)
            if m:
                cmd = m.group(1).strip()
                try:
                    r = api.cmd.execute(cmd, timeout=30)
                    text = f"**Commande :** `{cmd}`\n**Code :** {r.get('exit_code', '?')}"
                    out = r.get("stdout", "")
                    err = r.get("stderr", "")
                    return {"text": text, "stdout": out[:3000], "stderr": err[:1000]}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Open URL
            m = re.match(r"(?:open|ouvre|ouvrir|aller sur|va sur)\s+(https?://\S+|www\.\S+)", message, re.IGNORECASE)
            if m:
                url = m.group(1)
                if url.startswith("www."): url = "https://" + url
                try:
                    result = await api.web.navigate(url)
                    return {"text": f"**Ouvert :** {url}\n" + (result.get("message","") or "")}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Read file
            m = re.match(r"(?:read|lire|voir)\s+(.+)", message, re.IGNORECASE)
            if m:
                path = m.group(1).strip()
                try:
                    content = api.fs.read_file(path)
                    if len(content) > 5000:
                        return {"text": f"**{path} :**\n```\n{content[:5000]}\n```\n\n(_... tronque_)"}
                    return {"text": f"**{path} :**\n```\n{content}\n```"}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # Write file
            m = re.match(r"(?:write|ecrire|ecri)\s+(\S+)\s+(.+)", message, re.IGNORECASE)
            if m:
                path = m.group(1).strip()
                content = m.group(2).strip()
                try:
                    api.fs.write_file(path, content)
                    return {"text": f"**Fichier ecrit :** {path}"}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # List directory
            m = re.match(r"(?:ls|dir|list|lister)\s*(.*)", message, re.IGNORECASE)
            if m:
                path = m.group(1).strip() or "."
                try:
                    items = api.fs.list_directory(path)
                    if not items:
                        return {"text": f"Dossier vide : {path}"}
                    lines = [f"{'📁' if i['type']=='directory' else '📄'} {i['name']}" for i in items[:50]]
                    return {"text": f"**{path} :**\n" + "\n".join(lines)}
                except Exception as e:
                    return {"text": f"Erreur : {e}"}

            # === IA LOCALE (OLLAMA) POUR TOUT LE RESTE ===
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    oc = await client.get("http://127.0.0.1:11434/api/tags")
                    if oc.status_code == 200:
                        models = oc.json().get("models", [])
                        mn = None
                        for pref in ["qwen2.5-coder:7b", "deepseek-r1:7b", "phi3:mini"]:
                            if any(pref in md.get("name","") for md in models):
                                mn = pref; break
                        if not mn and models: mn = models[0]["name"]

                        if mn:
                            tools = json.dumps({
                                "screenshot": "Prendre une capture d'ecran",
                                "list_windows": "Lister les fenetres ouvertes",
                                "focus_window": "Mettre une fenetre au premier plan. Param: title",
                                "close_window": "Fermer une fenetre. Param: title",
                                "clipboard_read": "Lire le presse-papier",
                                "clipboard_write": "Ecrire dans le presse-papier. Param: text",
                                "send_keys": "Taper du texte ou envoyer des touches. Param: keys",
                                "execute_command": "Executer une commande systeme. Param: command",
                                "navigate": "Ouvrir une URL dans le navigateur. Param: url",
                                "web_content": "Lire le contenu de la page web actuelle",
                                "read_file": "Lire un fichier. Param: path",
                                "write_file": "Ecrire dans un fichier. Param: path, content",
                                "list_directory": "Lister un dossier. Param: path",
                            })

                            ai_prompt = (
                                "Tu es HERMES, un assistant local qui controle l'ordinateur.\n"
                                "Reponds UNIQUEMENT en JSON:\n"
                                "- Pour parler: {\"text\": \"ta reponse\"}\n"
                                "- Pour agir: {\"tool\": \"nom\", \"params\": {\"param\": \"valeur\"}, \"text\": \"ce que tu fais\"}\n\n"
                                "Outils:\n" + tools + "\n\n"
                                "Reponds en francais simplement. Demande: " + message
                            )

                            _or = await client.post(
                                "http://127.0.0.1:11434/api/generate",
                                json={"model": mn, "prompt": ai_prompt, "stream": False, "options": {"num_predict": 300}}
                            )
                            ai_text = _or.json().get("response", "").strip()
                            jm = re.search(r'\{[\s\S]*\}', ai_text)
                            if jm:
                                aj = json.loads(jm.group())
                            else:
                                return {"text": ai_text or "Desole, je n'ai pas compris."}

                            if "tool" in aj:
                                tool = aj["tool"]
                                params = aj.get("params", {})
                                desc = aj.get("text", "")

                                try:
                                    if tool == "screenshot":
                                        png = api.screen.capture_screen()
                                        return {"text": desc, "image": base64.b64encode(png).decode()}
                                    elif tool == "list_windows":
                                        wins = api.windows.list_windows()
                                        lines = [f"&#8226; {w['title'][:55]}" for w in wins[:20]]
                                        return {"text": f"{desc}\n\n" + "\n".join(lines)}
                                    elif tool == "focus_window":
                                        r = api.windows.focus_window(params.get("title",""))
                                        return {"text": f"{desc}\n{r}"}
                                    elif tool == "close_window":
                                        r = api.windows.close_window(params.get("title",""))
                                        return {"text": f"{desc}\n{r}"}
                                    elif tool == "clipboard_read":
                                        t = api.clipboard.read_clipboard()
                                        return {"text": f"{desc}\n\n```\n{t or '(vide)'}\n```"}
                                    elif tool == "clipboard_write":
                                        api.clipboard.write_clipboard(params.get("text",""))
                                        return {"text": desc}
                                    elif tool == "send_keys":
                                        api.keyboard.send_keys(params.get("keys",""))
                                        return {"text": desc}
                                    elif tool == "execute_command":
                                        r = api.cmd.execute(params.get("command",""), timeout=30)
                                        return {"text": desc, "stdout": r.get("stdout",""), "stderr": r.get("stderr","")}
                                    elif tool == "navigate":
                                        r = await api.web.navigate(params.get("url",""))
                                        return {"text": desc}
                                    elif tool == "web_content":
                                        c = await api.web.get_content()
                                        return {"text": (desc + "\n\n" + (c[:3000] if c else "(vide)")) if c else desc}
                                    elif tool == "read_file":
                                        c = api.fs.read_file(params.get("path",""))
                                        return {"text": f"{desc}\n\n```\n{c[:3000]}\n```"}
                                    elif tool == "write_file":
                                        api.fs.write_file(params.get("path",""), params.get("content",""))
                                        return {"text": desc}
                                    elif tool == "list_directory":
                                        items = api.fs.list_directory(params.get("path","."))
                                        lines = [f"{'📁' if i['type']=='directory' else '📄'} {i['name']}" for i in items[:50]]
                                        return {"text": desc + "\n\n" + "\n".join(lines)}
                                    else:
                                        return {"text": f"Outil inconnu : {tool}"}
                                except Exception as e:
                                    return {"text": f"Erreur : {e}"}
                            else:
                                return {"text": aj.get("text", "Je n'ai pas compris.")}
            except Exception:
                pass

            return {"text": (
                "Je n'ai pas compris. Voici ce que je sais faire :\n\n"
                "&#8226; Prendre une photo de l'ecran\n"
                "&#8226; Montrer les fenetres\n"
                "&#8226; Lire / copier le presse-papier\n"
                "&#8226; Ouvrir un site web\n"
                "&#8226; Executer une commande\n"
                "&#8226; Lire / ecrire des fichiers\n"
                "&#8226; Envoyer des fichiers (PDF, images...)\n"
                "&#8226; Repondre a tes questions (IA locale)\n\n"
                "Clique sur **Aide** pour plus de details."
            )}

        return app


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main():
    """Lance l'API HERMES Computer Use sur le port 9307."""
    import argparse

    parser = argparse.ArgumentParser(description="HERMES OMEGA - Computer Use API")
    parser.add_argument("--port", type=int, default=API_PORT, help=f"Port d'écoute (défaut: {API_PORT})")
    parser.add_argument("--sandbox", type=str, default=DEFAULT_SANDBOX_DIR, help="Répertoire sandbox")
    parser.add_argument("--full-access", action="store_true", help="Désactiver le sandbox fichiers")
    parser.add_argument("--no-headless", action="store_true", help="Navigateur visible (non headless)")
    args = parser.parse_args()

    api = ComputerUseAPI(
        sandbox_root=args.sandbox,
        full_access=args.full_access,
        headless=not args.no_headless,
    )
    app = api.create_app()

    try:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    except ImportError:
        raise ComputerUseError(
            "uvicorn non installé. Installer avec : pip install uvicorn"
        )


if __name__ == "__main__":
    main()
