"""
HERMES Agent v3 — Robust tool calling with JSON format.
Fixes: complex params, quoted values, nested JSON, edge cases.
"""
import sys, os, json, time, re, sqlite3, subprocess
from pathlib import Path
from datetime import datetime

_LOG = []
_DB_PATH = Path(r"C:\OS_INTERNE\data/osinterne.db")
_CFG = {}

def _load_cfg():
    global _CFG
    env_path = Path(r"C:\OS_INTERNE\config.env")
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                key, val = k.strip(), v.strip()
                if not val: val = os.environ.get(key, '')
                if val: _CFG[key] = val
_load_cfg()


class HermesAgent:
    SYSTEM = """Tu es Hermes, agent souverain autonome de cet OS interne.
Tu es concis, efficace, honnête. Tu parles français par défaut.

OUTILS DISPONIBLES:
Quand tu dois utiliser un outil, réponds avec un bloc JSON sur une seule ligne, préfixé par ACTION:
ACTION:{"tool":"nom_outil","params":{"cle":"valeur"}}

Outils:
- web_search(query) : recherche web
- calculate(expression) : calcul mathématique
- read_file(path) : lire un fichier (max 5000 chars)
- write_file(path, content) : écrire un fichier
- list_dir(path) : lister un répertoire
- create_task(description, priority) : créer une tâche (priority: 1-10)
- run_command(command) : exécuter une commande shell (timeout 30s)
- screenshot() : capture d'écran
- search_notes(query) : chercher dans les notes

RÈGLES:
- Un seul outil par réponse
- Après le résultat de l'outil, réponds naturellement à l'utilisateur
- Si aucun outil n'est nécessaire, réponds normalement sans bloc ACTION
- Ne JAMAIS inventer des résultats d'outil"""

    def __init__(self, agent_id="main"):
        self.agent_id = agent_id
        self.history = []
        self.max_rounds = 8
        self.tools = {}
        self._register_tools()

    def _log(self, action, **kw):
        _LOG.append({"ts": datetime.now().isoformat(), "action": action, "agent": self.agent_id, **kw})

    def _register_tools(self):
        # === web_search ===
        def web_search(query: str) -> dict:
            try:
                from ddgs import DDGS
                results = []
                with DDGS() as ddgs:
                    for r in ddgs.text(str(query), max_results=5):
                        results.append({"title": r.get("title",""), "url": r.get("href",""), "body": r.get("body","")[:200]})
                return {"ok": True, "results": results}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === calculate ===
        def calculate(expression: str) -> dict:
            try:
                # Only allow safe math
                allowed = set("0123456789+-*/.() %^")
                cleaned = expression.replace("^", "**")
                if not all(c in allowed for c in cleaned):
                    return {"ok": False, "error": "Caractères non autorisés"}
                result = eval(cleaned, {"__builtins__": {}}, {})
                return {"ok": True, "result": result}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # === read_file ===
        def read_file(path: str) -> dict:
            try:
                p = Path(str(path))
                if not p.exists():
                    return {"ok": False, "error": f"Introuvable: {path}"}
                content = p.read_text(encoding='utf-8', errors='replace')[:5000]
                lines = content.split("\n")
                return {"ok": True, "content": content, "lines": len(lines), "preview": lines[0][:200] if lines else ""}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === write_file ===
        def write_file(path: str, content: str) -> dict:
            try:
                p = Path(str(path))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(content), encoding='utf-8')
                return {"ok": True, "path": str(p.resolve()), "bytes": len(str(content))}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === list_dir ===
        def list_dir(path: str = ".") -> dict:
            try:
                p = Path(str(path))
                if not p.exists():
                    return {"ok": False, "error": f"Introuvable: {path}"}
                items = sorted([
                    {"name": i.name, "type": "dir" if i.is_dir() else "file",
                     "size": i.stat().st_size if i.is_file() else None}
                    for i in p.iterdir()
                ], key=lambda x: (x["type"] == "file", x["name"]))[:80]
                return {"ok": True, "path": str(p.resolve()), "count": len(items), "items": items}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === create_task ===
        def create_task(description: str, priority: int = 5) -> dict:
            try:
                import urllib.request
                payload = json.dumps({"description": str(description), "priority": int(priority)}).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:8766/api/task", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === run_command ===
        def run_command(command: str) -> dict:
            try:
                r = subprocess.run(str(command), shell=True, capture_output=True,
                                   text=True, timeout=30)
                return {"ok": True, "stdout": r.stdout[:3000], "stderr": r.stderr[:500], "code": r.returncode}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "Timeout 30s dépassé"}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === screenshot ===
        def screenshot() -> dict:
            try:
                import urllib.request
                payload = json.dumps({}).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:8766/api/screenshot", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        # === search_notes ===
        def search_notes(query: str) -> dict:
            try:
                import urllib.request
                resp = urllib.request.urlopen("http://127.0.0.1:8766/api/notes", timeout=5)
                data = json.loads(resp.read().decode())
                notes = data if isinstance(data, list) else data.get("items", data.get("notes", []))
                q = str(query).lower()
                matches = [n for n in notes if q in json.dumps(n, ensure_ascii=False).lower()]
                return {"ok": True, "matches": len(matches), "total": len(notes), "results": matches[:5]}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        self.tools = {
            "web_search": web_search, "calculate": calculate, "read_file": read_file,
            "write_file": write_file, "list_dir": list_dir, "create_task": create_task,
            "run_command": run_command, "screenshot": screenshot, "search_notes": search_notes,
        }

    def _call_llm(self, messages):
        """DeepSeek first, Ollama fallback."""
        import urllib.request, urllib.error
        api_key = _CFG.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return self._call_ollama(messages)

        payload = {"model": "deepseek-chat", "messages": messages,
                    "temperature": 0.2, "max_tokens": 2048}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

        try:
            req = urllib.request.Request("https://api.deepseek.com/chat/completions",
                data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())["choices"][0]["message"]["content"], None
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200] if hasattr(e, 'read') else ''
            return None, f"DeepSeek HTTP {e.code}: {body}"
        except Exception as e:
            return self._call_ollama(messages)

    def _call_ollama(self, messages):
        import urllib.request
        try:
            payload = {"model": "qwen2.5:7b", "messages": messages, "stream": False}
            req = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                r = json.loads(resp.read().decode())
                return r.get("message", {}).get("content", ""), None
        except Exception as e:
            return None, f"Aucun LLM: {e}"

    def _parse_action(self, text):
        """Parse ACTION:{"tool":"name","params":{...}}"""
        match = re.search(r'ACTION:\s*(\{.*\})', text, re.DOTALL)
        if not match:
            return None, None
        try:
            action = json.loads(match.group(1))
            return action.get("tool"), action.get("params", {})
        except json.JSONDecodeError:
            return None, None

    def _save(self, role, content):
        try:
            with sqlite3.connect(str(_DB_PATH), timeout=5) as conn:
                conn.execute("INSERT INTO messages (agent_id, role, content) VALUES (?,?,?)",
                           (self.agent_id, role, str(content)))
                conn.commit()
        except: pass

    def chat(self, message):
        self.history.append({"role": "user", "content": str(message)})

        sys_prompt = {"role": "system", "content": self.SYSTEM}

        for _ in range(self.max_rounds):
            msgs = [sys_prompt] + self.history[-24:]
            response, error = self._call_llm(msgs)

            if error:
                self.history.append({"role": "assistant", "content": f"[ERREUR LLM] {error}"})
                self._save("assistant", f"[ERREUR] {error}")
                return f"[ERREUR] {error}"

            tool_name, params = self._parse_action(response)

            if not tool_name or tool_name not in self.tools:
                # Final response — no tool call
                self.history.append({"role": "assistant", "content": response})
                self._save("user", str(message))
                self._save("assistant", response)
                return response

            # Execute tool
            self._log("tool_call", tool=tool_name, params=params)
            try:
                result = self.tools[tool_name](**params)
                result_str = json.dumps(result, ensure_ascii=False, default=str)[:3000]
            except TypeError as e:
                result_str = json.dumps({"ok": False, "error": f"Paramètres invalides: {e}"})

            self.history.append({"role": "assistant", "content": response})
            self.history.append({"role": "user",
                "content": f"[RÉSULTAT {tool_name}]: {result_str}\n\nMaintenant réponds à l'utilisateur."})

        return response or "[Agent] Rounds max atteintes"


_agent = None
def get_agent():
    global _agent
    if _agent is None:
        _agent = HermesAgent()
    return _agent
