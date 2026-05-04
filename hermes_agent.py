"""
HERMES Agent v3 — Autonomous Task Execution Engine
Based on: Agent OS Microkernel, Skill Crystallization, Memory Layers, Multi-Model Routing

Architecture inspired by:
- ArbiterOS (Governance-First, Reliability Budget)
- GenericAgent (Skill Crystallization, Memory Layering)
- ReflexiCoder (Internal Self-Correction)
- The Kitchen Loop (Verification Gates)
- Agent OS Kernel (Budget, HITL, Checkpoint/Replay)

Hardware: CPU-only, Ollama local inference
Models: qwen2.5-coder:14b (planning), llama3.1:8b (execution)
"""
import asyncio
import json as _json
import os as _os
import subprocess as _subprocess
import re
import time
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as _url_quote

try:
    import httpx
except ImportError:
    import urllib.request as _urllib
    import urllib.parse as _uparse
    # Minimal httpx-like wrapper using urllib
    class _FakeAsyncClient:
        def __init__(self, timeout=30):
            self.timeout = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw):
            r = _urllib.urlopen(url, timeout=kw.get('timeout', self.timeout))
            return type('R', (), {'json': lambda: _json.loads(r.read()), 'text': r.read().decode()})()
        async def post(self, url, json=None, **kw):
            data = _json.dumps(json).encode()
            req = _urllib.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            r = _urllib.urlopen(req, timeout=kw.get('timeout', self.timeout))
            return type('R', (), {'json': lambda: _json.loads(r.read())})()
    httpx = type('httpx', (), {'AsyncClient': _FakeAsyncClient})()


# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════
OLLAMA_URL = "http://127.0.0.1:11434"
PLAN_MODEL = "qwen2.5-coder:14b"      # Slow but smart — for planning & reflection
EXEC_MODEL = "llama3.1:8b"            # Fast, native tool calling — for execution
MAX_STEPS = 80
MAX_BUDGET_TOKENS = 200000            # Approximate token budget
CONSECUTIVE_FAILURE_LIMIT = 3
MEMORY_DIR = Path(__file__).parent / ".hermes_memory"
SKILLS_DIR = MEMORY_DIR / "skills"
TASKS_DIR = MEMORY_DIR / "tasks"

# Ensure dirs exist
MEMORY_DIR.mkdir(exist_ok=True)
SKILLS_DIR.mkdir(exist_ok=True)
TASKS_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════
# MEMORY LAYERS (L0-L3 as per GenericAgent)
# ═══════════════════════════════════════════════════════════
class MemoryStore:
    """Layered memory: L0=safety, L1=patterns, L2=facts, L3=crystallized skills"""

    def __init__(self):
        self.l0_path = MEMORY_DIR / "l0_safety.json"
        self.l1_path = MEMORY_DIR / "l1_patterns.json"
        self.l2_path = MEMORY_DIR / "l2_facts.json"
        self.l3_path = MEMORY_DIR / "l3_skills.json"
        self._load()

    def _load(self):
        self.l0 = self._read_json(self.l0_path, [
            "Always write COMPLETE files - never use placeholders",
            "Verify each step before proceeding",
            "If a step fails 3 times, stop and report",
            "Never delete user files without explicit permission",
            "Use Windows paths with backslashes",
        ])
        self.l1 = self._read_json(self.l1_path, [])
        self.l2 = self._read_json(self.l2_path, {})
        self.l3 = self._read_json(self.l3_path, {})

    def _read_json(self, path, default):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            return default

    def _write_json(self, path, data):
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)

    def add_pattern(self, task_hash, model_used, steps_count, success, tools_used):
        """L1: Store task execution patterns for routing decisions"""
        self.l1.append({
            "hash": task_hash[:8],
            "model": model_used,
            "steps": steps_count,
            "success": success,
            "tools": list(set(tools_used)),
            "time": datetime.now().isoformat()
        })
        if len(self.l1) > 200:
            self.l1 = self.l1[-100:]
        self._write_json(self.l1_path, self.l1)

    def set_fact(self, key, value):
        """L2: Store persistent facts"""
        self.l2[key] = {"value": value, "time": datetime.now().isoformat()}
        self._write_json(self.l2_path, self.l2)

    def get_fact(self, key):
        return self.l2.get(key, {}).get("value")

    def crystallize_skill(self, name, plan, tools_sequence, notes):
        """L3: Save a successful execution pattern as a reusable skill"""
        self.l3[name] = {
            "plan": plan,
            "tools_sequence": tools_sequence,
            "notes": notes,
            "created": datetime.now().isoformat(),
            "uses": 0
        }
        self._write_json(self.l3_path, self.l3)
        # Also save as individual file for readability
        skill_file = SKILLS_DIR / f"{name.replace(' ', '_').replace('/', '_')}.json"
        skill_file.write_text(_json.dumps(self.l3[name], ensure_ascii=False, indent=2), encoding='utf-8')

    def find_similar_skill(self, task_desc):
        """L1/L3: Find a crystallized skill matching the task"""
        task_low = task_desc.lower()
        best_match = None
        best_score = 0
        for name, skill in self.l3.items():
            score = sum(1 for w in name.lower().split('_') if w in task_low and len(w) > 2)
            plan_words = skill.get('plan', '').lower()
            for w in task_low.split():
                if len(w) > 3 and w in plan_words:
                    score += 2
            if score > best_score:
                best_score = score
                best_match = (name, skill)
        return best_match if best_score >= 3 else None

    def get_context_string(self):
        """Build a context string from all memory layers for the system prompt"""
        parts = []
        parts.append("## Memory Rules (L0)")
        for rule in self.l0:
            parts.append(f"- {rule}")
        if self.l3:
            parts.append("\n## Known Skills (L3)")
            for name, skill in self.l3.items():
                parts.append(f"- {name}: {skill.get('notes', '')[:100]}")
        facts_str = "\n".join(f"- {k}: {v['value']}" for k, v in self.l2.items())
        if facts_str:
            parts.append(f"\n## Known Facts (L2)\n{facts_str}")
        return "\n".join(parts) if parts else ""


# ═══════════════════════════════════════════════════════════
# AGENT OS MICROKERNEL (Budget, HITL, Checkpoint)
# ═══════════════════════════════════════════════════════════
class AgentKernel:
    """Minimal OS kernel for the agent — budgeting, security gates, checkpointing"""

    DANGEROUS_PATTERNS = [
        r'\brm\s+-rf\b', r'\bdel\s+/[sf]', r'\brd\s+/[sf]', r'\bformat\b',
        r'\bdrop\s+table\b', r'\bDROP\s+DATABASE\b', r'\bshutdown\b',
        r'\breg\s+delete\b', r'\bdiskpart\b', r'\bnetsh\b.*delete',
    ]

    def __init__(self):
        self.budget_tokens = MAX_BUDGET_TOKENS
        self.spent_tokens = 0
        self.steps_executed = 0
        self.consecutive_failures = 0
        self.checkpoint_log = []
        self.hitl_pending = False

    def check_budget(self):
        """Returns True if budget is ok"""
        return self.spent_tokens < self.budget_tokens and self.steps_executed < MAX_STEPS

    def record_token_usage(self, approx_tokens):
        self.spent_tokens += approx_tokens

    def record_step(self, tool, success):
        self.steps_executed += 1
        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

    def save_checkpoint(self, step, tool, args, result):
        self.checkpoint_log.append({
            "step": step, "tool": tool,
            "args": str(args)[:200], "result": str(result)[:300],
        })

    def check_dangerous(self, command):
        """Check if a command matches dangerous patterns — HITL gate"""
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        return False

    def should_stop(self):
        return (
            not self.check_budget() or
            self.consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT
        )


# ═══════════════════════════════════════════════════════════
# TOOL EXECUTION
# ═══════════════════════════════════════════════════════════
async def execute_tool(tool_name, args, kernel):
    """Execute a tool with kernel security checks"""
    try:
        # HITL gate for dangerous commands
        if tool_name == "run_command":
            cmd = args.get("command", "")
            if kernel.check_dangerous(cmd):
                return {"success": False, "error": f"BLOCKED by security kernel: dangerous command pattern detected. Requires human approval."}

        if tool_name == "create_file":
            path = args.get("path", "")
            content = args.get("content", "")
            if not path:
                return {"success": False, "error": "path required"}
            d = _os.path.dirname(path)
            if d:
                _os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "message": f"Created {path} ({len(content)} chars)"}

        elif tool_name == "run_command":
            cmd = args.get("command", "")
            cwd = args.get("cwd", "C:\\Users\\PC")
            proc = await _subprocess.create_subprocess_shell(
                cmd, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, cwd=cwd
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                return {"success": False, "error": "Timeout (300s)"}
            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace")[:5000],
                "stderr": stderr.decode("utf-8", errors="replace")[:3000],
            }

        elif tool_name == "read_file":
            path = args.get("path", "")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    c = f.read()
                return {"success": True, "content": c[:8000], "lines": c.count("\n")}
            except FileNotFoundError:
                return {"success": False, "error": f"Not found: {path}"}

        elif tool_name == "list_files":
            path = args.get("path", "C:\\Users\\PC")
            if not _os.path.exists(path):
                return {"success": False, "error": f"Not found: {path}"}
            items = []
            try:
                for item in _os.listdir(path)[:100]:
                    full = _os.path.join(path, item)
                    items.append({"name": item, "type": "dir" if _os.path.isdir(full) else "file"})
            except PermissionError:
                return {"success": False, "error": "Permission denied"}
            return {"success": True, "items": items}

        elif tool_name == "web_search":
            query = args.get("query", "")
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(
                    f"https://html.duckduckgo.com/html/?q={_url_quote(query)}",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                results = []
                for m in re.finditer(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL):
                    results.append(re.sub(r'<[^>]+>', '', m.group(1)).strip()[:200])
                return {"success": True, "results": results[:12]}

        elif tool_name == "done":
            return {"success": True, "done": True, "summary": args.get("summary", "Done")}

        return {"success": False, "error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# MODEL CALLING
# ═══════════════════════════════════════════════════════════
async def call_model(messages, model=PLAN_MODEL, timeout=300, num_predict=6000):
    """Call Ollama model and return text"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": model, "messages": messages, "stream": False,
                  "options": {"num_predict": num_predict, "temperature": 0.1}},
        )
        data = resp.json()
        return data.get("message", {}).get("content", "")


def parse_tool_call(text):
    """Extract JSON tool call from model output"""
    # Try ```json block first
    m = re.search(r'```json\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        try:
            p = _json.loads(m.group(1))
            if "tool" in p and "args" in p:
                return p
        except _json.JSONDecodeError:
            pass
    # Try bare JSON
    m = re.search(r'\{[^{}]*"tool"\s*:\s*"[^"]+[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            p = _json.loads(m.group(0))
            if "tool" in p and "args" in p:
                return p
        except _json.JSONDecodeError:
            pass
    return None


# ═══════════════════════════════════════════════════════════
# TOOL SCHEMA FOR PROMPTS
# ═══════════════════════════════════════════════════════════
TOOL_SCHEMA = """
Available tools (call via JSON):
1. create_file: {"tool": "create_file", "args": {"path": "C:\\path\\file.ext", "content": "COMPLETE content"}}
2. run_command: {"tool": "run_command", "args": {"command": "...", "cwd": "C:\\Users\\PC"}}
3. read_file:   {"tool": "read_file", "args": {"path": "C:\\path\\file"}}
4. list_files:  {"tool": "list_files", "args": {"path": "C:\\path\\dir"}}
5. web_search:  {"tool": "web_search", "args": {"query": "search terms"}}
6. done:        {"tool": "done", "args": {"summary": "What was accomplished"}}

Output format for each step:
Thought: [reasoning]
```json
{"tool": "tool_name", "args": {...}}
```
"""


def build_system_prompt(memory: MemoryStore):
    """Build the full system prompt with memory context"""
    mem_ctx = memory.get_context_string()
    return f"""You are HERMES, a fully autonomous AI agent on a Windows PC. You execute tasks completely on your own.

## CRITICAL RULES
1. Write COMPLETE files — NEVER "..." or "// rest of code" or placeholders
2. Break complex tasks into sequential steps
3. Verify each step before proceeding
4. If something fails, analyze the error and fix it — don't repeat the same command
5. Use Windows paths: C:\\Users\\PC\\...
6. After creating files, read them back to verify
7. When truly done, call done() with a summary

{TOOL_SCHEMA}

{mem_ctx}
"""


# ═══════════════════════════════════════════════════════════
# MAIN AGENT LOOP
# ═══════════════════════════════════════════════════════════
async def agent_stream(task: str):
    """Main agent entry point. Yields SSE event strings."""

    memory = MemoryStore()
    kernel = AgentKernel()
    task_hash = hashlib.md5(task.encode()).hexdigest()
    start_time = time.time()

    yield _json.dumps({"type": "start", "models": {"plan": PLAN_MODEL, "exec": EXEC_MODEL}, "time": datetime.now().isoformat()})

    # Verify Ollama
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            plan_ok = any(PLAN_MODEL.split(":")[0] in m for m in models)
            exec_ok = any(EXEC_MODEL.split(":")[0] in m for m in models)
            if not plan_ok or not exec_ok:
                yield _json.dumps({"type": "error", "message": f"Models missing. plan={plan_ok}, exec={exec_ok}"})
                return
    except Exception as e:
        yield _json.dumps({"type": "error", "message": f"Ollama unreachable: {e}"})
        return

    # ── PHASE 1: Check for crystallized skill (L3) ──
    yield _json.dumps({"type": "phase", "phase": "memory", "message": "Checking memory for similar tasks..."})
    skill_match = memory.find_similar_skill(task)
    skill_hint = ""
    if skill_match:
        name, skill = skill_match
        skill_hint = f"\n\nA similar task was done before: '{name}'\nPrevious plan: {skill.get('plan', '')[:500]}\nAdapt this plan to the current task."
        yield _json.dumps({"type": "skill_hit", "name": name, "notes": skill.get("notes", "")[:200]})

    # ── PHASE 2: Planning (slow model) ──
    yield _json.dumps({"type": "phase", "phase": "planning", "message": f"Planning with {PLAN_MODEL}..."})
    plan_prompt = f"""Analyze this task and create a detailed step-by-step execution plan. Be specific about files, commands, and order.

Task: {task}
{skill_hint}

Respond with a numbered plan."""
    try:
        plan = await call_model([
            {"role": "system", "content": "You are a project planner. Create detailed execution plans."},
            {"role": "user", "content": plan_prompt}
        ], model=PLAN_MODEL, timeout=180)
        yield _json.dumps({"type": "plan", "content": plan[:2000]})
    except Exception as e:
        plan = "Direct execution"
        yield _json.dumps({"type": "plan", "content": plan})

    # ── PHASE 3: Execution (fast model with native tool calling) ──
    yield _json.dumps({"type": "phase", "phase": "executing", "message": f"Executing with {EXEC_MODEL}..."})

    system_prompt = build_system_prompt(memory)
    conversation = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"## Task\n{task}\n\n## Plan\n{plan}\n\nExecute step by step. Start now."},
    ]

    tools_used = []
    exec_model = EXEC_MODEL

    while not kernel.should_stop():
        kernel.steps_executed += 1
        step_num = kernel.steps_executed

        try:
            response = await call_model(conversation, model=exec_model, timeout=300)
        except Exception as e:
            yield _json.dumps({"type": "error", "message": f"Model error step {step_num}: {e}"})
            break

        if not response:
            yield _json.dumps({"type": "error", "message": f"Empty response step {step_num}"})
            break

        # Estimate tokens (rough: 1 token ≈ 4 chars)
        kernel.record_token_usage(len(response) // 4)

        # Parse tool call
        tool_call = parse_tool_call(response)
        if not tool_call:
            conversation.append({"role": "assistant", "content": response})
            conversation.append({"role": "user", "content": "Output a JSON tool call: ```json\n{\"tool\": \"...\", \"args\": {...}}\n```\nAvailable: create_file, run_command, read_file, list_files, web_search, done"})
            # Trim if too long
            total_chars = sum(len(m.get("content", "")) for m in conversation)
            if total_chars > 35000:
                conversation = [conversation[0]] + conversation[-6:]
            continue

        tool_name = tool_call["tool"]
        tool_args = tool_call["args"]

        # Extract thought
        thought = response.split("```json")[0].strip()[-300:] if "```json" in response else ""
        yield _json.dumps({"type": "step", "step": step_num, "thought": thought, "tool": tool_name})

        # Execute
        result = await execute_tool(tool_name, tool_args, kernel)
        success = result.get("success", False)
        preview = _json.dumps(result, ensure_ascii=False)[:600]

        kernel.record_step(tool_name, success)
        kernel.save_checkpoint(step_num, tool_name, tool_args, result)
        tools_used.append(tool_name)

        yield _json.dumps({"type": "result", "step": step_num, "success": success, "preview": preview})

        # Check done
        if result.get("done"):
            elapsed = time.time() - start_time
            yield _json.dumps({"type": "done", "step": step_num, "summary": result.get("summary", ""),
                               "stats": {"steps": step_num, "time": round(elapsed), "tools": list(set(tools_used))}})

            # ── PHASE 4: Skill Crystallization ──
            if step_num >= 3:
                skill_name = task[:60].strip().replace(" ", "_").replace("/", "_")
                memory.crystallize_skill(
                    skill_name,
                    plan=plan[:1000],
                    tools_sequence=tools_used,
                    notes=f"{task[:100]} — {step_num} steps — {round(elapsed)}s"
                )
                yield _json.dumps({"type": "crystallized", "skill": skill_name})

            # Save task pattern
            memory.add_pattern(task_hash, exec_model, step_num, True, tools_used)
            return

        # Build feedback
        if success:
            feedback = f"Tool `{tool_name}` succeeded.\nResult: {preview}\nContinue. If done, call done()."
        else:
            error_msg = result.get("error", result.get("stderr", "Unknown error"))
            if kernel.consecutive_failures >= 3:
                feedback = f"FAILED 3 times! Error: {error_msg}\nSTOP and call done() with summary."
            else:
                feedback = f"FAILED. Error: {error_msg}\nAnalyze the error, fix approach, retry. Don't repeat same command."

        conversation.append({"role": "assistant", "content": response})
        conversation.append({"role": "user", "content": feedback})

        # Trim conversation
        total_chars = sum(len(m.get("content", "")) for m in conversation)
        if total_chars > 35000:
            conversation = [conversation[0]] + conversation[-6:]

    # Budget exhausted
    elapsed = time.time() - start_time
    reason = "budget" if not kernel.check_budget() else "consecutive_failures"
    yield _json.dumps({"type": "error", "message": f"Stopped: {reason} ({step_num} steps, {round(elapsed)}s)"})

    # Save failed pattern
    memory.add_pattern(task_hash, exec_model, kernel.steps_executed, False, tools_used)


# ═══════════════════════════════════════════════════════════
# FASTAPI REGISTRATION
# ═══════════════════════════════════════════════════════════
def register_agent_routes(app):
    from starlette.responses import StreamingResponse

    @app.post("/api/agent")
    async def agent_endpoint(body: dict):
        task = body.get("message", "") or body.get("task", "")
        if not task:
            return {"text": "Décrivez une tâche.", "error": "empty_task"}

        async def event_stream():
            try:
                async for event in agent_stream(task):
                    yield f"data: {event}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps({'type': 'error', 'message': str(e)})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/agent/status")
    async def agent_status():
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{OLLAMA_URL}/api/tags")
                models = [m["name"] for m in r.json().get("models", [])]
                mem = MemoryStore()
                return {
                    "ollama": True, "models": models,
                    "plan_model": PLAN_MODEL, "exec_model": EXEC_MODEL,
                    "ready": all(any(m.split(":")[0] in mo for mo in models) for m in [PLAN_MODEL, EXEC_MODEL]),
                    "memory": {"skills": len(mem.l3), "patterns": len(mem.l1), "facts": len(mem.l2)},
                }
        except Exception:
            return {"ollama": False, "ready": False}

    @app.get("/api/agent/memory")
    async def agent_memory():
        mem = MemoryStore()
        return {"l0_rules": mem.l0, "l3_skills": list(mem.l3.keys()), "l1_patterns_count": len(mem.l1), "l2_facts": mem.l2}
