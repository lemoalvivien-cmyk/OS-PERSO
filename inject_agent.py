"""Inject agent mode into hermes_computer_use.py"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

AGENT_CODE = r'''
# ============================================================
# AGENT MODE - Autonomous task execution with ReAct loop
# ============================================================
import subprocess as _subprocess
import os as _os

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create or overwrite a file at the given path with the given content. Creates parent directories automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path (e.g. C:\\Users\\PC\\project\\file.tsx)"},
                    "content": {"type": "string", "description": "Complete file content. Never use placeholders like '...' or '// rest of code'. Write everything."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command (PowerShell/cmd) and capture stdout and stderr. Use for npm, npx, pip, git, mkdir, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "cwd": {"type": "string", "description": "Working directory (optional, defaults to C:\\Users\\PC)"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the content of a file. Returns first 6000 characters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in a given path. Shows name, type (file/dir), and size.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo. Returns result snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    }
]

AGENT_SYSTEM = """You are HERMES, an autonomous AI assistant that runs on a local Windows computer. You receive tasks and accomplish them step by step using tools.

CRITICAL RULES:
1. When creating files, ALWAYS write COMPLETE content. Never use "...", "// rest of code", "TODO", or any placeholder. Every file must be fully functional.
2. Break complex tasks into small, sequential steps.
3. After each tool result, verify success before proceeding.
4. If a step fails, analyze the error and try a different approach.
5. Use run_command for npm/npx/pip/git/mkdir commands.
6. Use create_file for all code files - write complete, working code.
7. For large projects: scaffold first, then create files one by one.
8. Always use Windows paths with backslashes: C:\\Users\\PC\\...
9. After creating files, verify with read_file or run_command to check for errors.
10. When done, report what was accomplished with action "done"."""


async def _agent_execute_tool(name: str, params: dict) -> dict:
    """Execute an agent tool and return the result."""
    try:
        if name == "create_file":
            path = params.get("path", "")
            content = params.get("content", "")
            if not path:
                return {"success": False, "error": "path is required"}
            dir_path = _os.path.dirname(path)
            if dir_path:
                _os.makedirs(dir_path, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "message": f"Created {path} ({len(content)} chars)"}

        elif name == "run_command":
            cmd = params.get("command", "")
            cwd = params.get("cwd", "C:\\Users\\PC")
            if not cmd:
                return {"success": False, "error": "command is required"}
            proc = await _subprocess.create_subprocess_shell(
                cmd, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, cwd=cwd
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                return {"success": False, "error": "Timeout (300s)"}
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": out[:4000],
                "stderr": err[:2000],
            }

        elif name == "read_file":
            path = params.get("path", "")
            if not path:
                return {"success": False, "error": "path is required"}
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                return {"success": True, "content": content[:6000], "lines": content.count("\n")}
            except FileNotFoundError:
                return {"success": False, "error": f"Not found: {path}"}

        elif name == "list_files":
            path = params.get("path", "C:\\Users\\PC")
            if not _os.path.exists(path):
                return {"success": False, "error": f"Not found: {path}"}
            items = []
            try:
                for item in _os.listdir(path):
                    full = _os.path.join(path, item)
                    items.append({"name": item, "type": "dir" if _os.path.isdir(full) else "file"})
            except PermissionError:
                return {"success": False, "error": "Permission denied"}
            return {"success": True, "items": items[:100]}

        elif name == "web_search":
            query = params.get("query", "")
            if not query:
                return {"success": False, "error": "query is required"}
            try:
                from urllib.parse import quote as _quote
                async with httpx.AsyncClient(timeout=20.0) as c:
                    r = await c.get(
                        f"https://html.duckduckgo.com/html/?q={_quote(query)}",
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                    )
                    text = r.text
                    results = []
                    for line in text.split("\n"):
                        line = line.strip()
                        if "result__a" in line and "href" in line.lower():
                            results.append(line[:300])
                    return {"success": True, "results": results[:10]}
            except Exception as e:
                return {"success": False, "error": str(e)}

        else:
            return {"success": False, "error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def _agent_loop(task: str):
    """Agent ReAct loop - yields SSE events."""
    import json as _json

    yield _json.dumps({"type": "start", "model": "llama3.1:8b"})

    messages = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": task},
    ]

    async with httpx.AsyncClient(timeout=300.0) as client:
        for step in range(50):
            try:
                resp = await client.post(
                    "http://127.0.0.1:11434/api/chat",
                    json={
                        "model": "llama3.1:8b",
                        "messages": messages,
                        "tools": AGENT_TOOLS,
                        "stream": False,
                        "options": {"num_predict": 4000, "temperature": 0.1},
                    },
                    timeout=180,
                )
                d = resp.json()
                ai_msg = d.get("message", {})
                ai_content = ai_msg.get("content", "") or ""
                tool_calls = ai_msg.get("tool_calls", [])
            except Exception as e:
                yield _json.dumps({"type": "error", "message": f"Ollama error (step {step+1}): {e}"})
                break

            if not ai_content and not tool_calls:
                yield _json.dumps({"type": "error", "message": f"Empty response (step {step+1})"})
                break

            # Append assistant message
            messages.append(ai_msg)

            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    args = func.get("arguments", {})

                    if tool_name == "done":
                        yield _json.dumps({"type": "done", "step": step+1, "summary": args.get("summary", "Task completed")})
                        return

                    if not tool_name:
                        continue

                    yield _json.dumps({"type": "step", "step": step+1, "thought": ai_content[:300], "tool": tool_name})

                    # Execute tool
                    result = await _agent_execute_tool(tool_name, args)

                    # Send result
                    yield _json.dumps({"type": "result", "step": step+1, "success": result.get("success", False), "preview": str(result)[:500]})

                    # Add tool result to messages
                    messages.append({"role": "tool", "content": _json.dumps(result, ensure_ascii=False)[:4000]})
            else:
                # No tool calls - model is just talking, ask it to use tools
                if "done" in ai_content.lower() or "complete" in ai_content.lower() or "finished" in ai_content.lower():
                    yield _json.dumps({"type": "done", "step": step+1, "summary": ai_content[:500]})
                    return
                messages.append({"role": "user", "content": "Use a tool to accomplish the task. Choose from: create_file, run_command, read_file, list_files, web_search. When finished, call done()."})

        else:
            yield _json.dumps({"type": "error", "message": "Max steps reached (50)"})


@app.post("/api/agent")
async def agent_endpoint(body: dict):
    """Agent mode - autonomous task execution with SSE streaming."""
    from starlette.responses import StreamingResponse
    task = body.get("message", "") or body.get("task", "")
    if not task:
        return {"text": "Veuillez decrire une tache.", "error": "empty_task"}

    async def event_stream():
        try:
            async for event in _agent_loop(task):
                yield f"data: {event}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

'''

# Read the original file
filepath = r"C:\Users\PC\.openclaw-autoclaw\agents\os-perso\workspace\hermes_computer_use.py"
with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# Find "return app" and insert agent code before it
marker = "        return app\n"
if marker not in content:
    print("ERROR: Could not find 'return app' marker")
    sys.exit(1)

# Also need to add httpx import check and json import
# Check if httpx is already imported
if "import httpx" not in content:
    print("Note: httpx not found in imports - checking...")
    # Add after existing imports
    import_line = "import httpx\n"
    if "import asyncio" in content:
        content = content.replace("import asyncio\n", "import asyncio\n" + import_line, 1)
    else:
        content = "import httpx\n" + content

# Insert agent code before "return app"
content = content.replace(marker, AGENT_CODE + marker, 1)

# Add agent JS to cockpit - find the send function
agent_js = '''
    // Agent mode detection
    function isAgentTask(msg) {
        const keywords = ['screenshot','capture','ecran','fenetres','clipboard','presse-papier',
                         'aide','help','infos','processus','reseau','fichiers','fichier',
                         'ouvrir','site','web','page','processus','kill','tuer'];
        const msgLow = msg.toLowerCase();
        return !keywords.some(k => msgLow.includes(k)) && msg.length > 20;
    }

    // Override send to route complex tasks to agent
    const _origSend = window.sendMsg;
    window.sendMsg = async function(msg, files) {
        if (files && files.length > 0) {
            return _origSend(msg, files);
        }
        if (isAgentTask(msg)) {
            return sendAgent(msg);
        }
        return _origSend(msg, files);
    };

    async function sendAgent(msg) {
        addMsg('user', msg);
        const box = document.getElementById('agent-progress');
        if (box) box.style.display = 'block';
        addMsg('bot', '🤖 Agent HERMES en cours...');

        try {
            const resp = await fetch('/api/agent', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: msg})
            });
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = '', steps = [];

            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                buf += decoder.decode(value, {stream: true});
                const lines = buf.split('\\n');
                buf = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    try {
                        const evt = JSON.parse(line.slice(6));
                        if (evt.type === 'step') {
                            steps.push(evt);
                            updateAgentProgress(steps);
                        } else if (evt.type === 'result') {
                            const last = steps[steps.length-1];
                            if (last) last.result = evt;
                            updateAgentProgress(steps);
                        } else if (evt.type === 'done') {
                            addMsg('bot', '✅ ' + evt.summary);
                            if (box) box.style.display = 'none';
                            return;
                        } else if (evt.type === 'error') {
                            addMsg('bot', '❌ Erreur: ' + evt.message);
                            if (box) box.style.display = 'none';
                            return;
                        }
                    } catch(e) {}
                }
            }
            addMsg('bot', '⚠️ Agent termine sans confirmation.');
            if (box) box.style.display = 'none';
        } catch(e) {
            addMsg('bot', '❌ Erreur connexion agent: ' + e.message);
        }
    }

    function updateAgentProgress(steps) {
        const box = document.getElementById('agent-progress');
        if (!box) return;
        box.innerHTML = '<div style="font-weight:700;margin-bottom:8px">🤖 Agent HERMES</div>' +
            steps.map((s,i) => {
                const icon = s.result ? (s.result.success ? '✅' : '❌') : '⏳';
                const tool = s.tool || '?';
                const preview = s.result ? (s.result.preview||'').substring(0,80) : '...';
                return '<div style="margin:4px 0;padding:4px 8px;background:rgba(163,230,53,0.1);border-radius:6px;font-size:13px">' +
                    icon + ' Étape ' + (i+1) + ' [' + tool + '] ' + preview + '</div>';
            }).join('');
        box.scrollTop = box.scrollHeight;
    }
'''

# Add agent progress div and JS before </script> in COCKPIT_HTML
if '</script>' in content:
    # Add the agent progress div to the HTML
    agent_div = '<div id="agent-progress" style="display:none;position:fixed;bottom:80px;right:20px;width:400px;max-height:300px;overflow-y:auto;background:#1a1a2e;border:1px solid #a3e635;border-radius:12px;padding:12px;z-index:999;font-size:13px"></div>'
    # Insert before closing </body>
    content = content.replace('</body>', agent_div + '\n</body>', 1)
    # Insert JS before </script>
    content = content.replace('</script>', agent_js + '\n</script>', 1)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"OK - Agent mode injected into hermes_computer_use.py")
print(f"New file size: {len(content)} chars ({content.count(chr(10))} lines)")
