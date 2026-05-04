"""HERMES OS - Agent Mode Upgrade
Adds autonomous agent capabilities to HERMES OS.
The agent can plan, execute tools, observe results, and self-correct.

Agent loop:
1. User sends a complex task
2. Ollama analyzes and plans steps
3. Each step: think → choose tool → execute → observe
4. Feed result back to Ollama
5. Ollama decides next step or reports done
"""

# This file will be injected into hermes_computer_use.py
# It replaces the agent section (after COCKPIT_HTML) up to "return app"

AGENT_SYSTEM_PROMPT = """You are HERMES, an autonomous AI assistant embedded in a local OS. You receive tasks and accomplish them step by step using available tools.

AVAILABLE TOOLS:
- create_file: Create or overwrite a file. Params: {"path": "C:\\path\\to\\file.ext", "content": "full file content here"}
- run_command: Execute a shell command. Params: {"command": "the command"}
- read_file: Read a file's content. Params: {"path": "C:\\path\\to\\file"}
- list_files: List directory contents. Params: {"path": "C:\\path\\to\\dir"}
- web_search: Search the web for information. Params: {"query": "search terms"}

RESPONSE FORMAT - You MUST respond with valid JSON:
For each step:
{"thought": "what you plan to do and why", "action": "tool_name", "params": {"key": "value"}}

When the task is complete:
{"thought": "task accomplished", "action": "done", "summary": "description of what was built/done"}

CRITICAL RULES:
1. ALWAYS respond with valid JSON - no markdown, no extra text outside the JSON
2. Break complex tasks into small, sequential steps
3. Create files with COMPLETE content - never use placeholders or "..." 
4. After each tool result, verify it succeeded before proceeding
5. If a tool fails, analyze the error and try a different approach
6. Combine related actions when possible to be efficient
7. For coding tasks: create files one at a time, verify with read_file
8. For project setup: use run_command with npm/npx/pip commands
9. Always use full Windows paths (C:\\Users\\...) for file operations
10. Keep file content under 8000 characters per step to stay within limits
"""

AGENT_MODEL_PROMPT = """You are a senior full-stack developer. You build complete, working applications from specifications.

TECHNOLOGIES: Next.js 14+, TypeScript, Supabase, Tailwind CSS, shadcn/ui
DESIGN: Dark theme, anthracite bg (#0d1117), lime accent (#a3e635), Inter font

When building a project:
1. First create the project scaffold (npx create-next-app)
2. Install dependencies
3. Create files one by one, starting with core infrastructure
4. Test when possible (npm run build)
5. Fix any errors before proceeding

For React/Next.js components:
- Use TypeScript
- Use Tailwind CSS for styling
- Use shadcn/ui components when available
- Make components responsive
- Use proper TypeScript types

IMPORTANT: Always output valid JSON with your action."""

# --- Tool implementations ---
async def agent_execute_tool(name: str, params: dict) -> dict:
    """Execute an agent tool and return the result."""
    import subprocess, os as _os, json as _json

    try:
        if name == "create_file":
            path = params.get("path", "")
            content = params.get("content", "")
            if not path:
                return {"success": False, "error": "path is required"}
            # Ensure directory exists
            dir_path = _os.path.dirname(path)
            if dir_path:
                _os.makedirs(dir_path, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "message": f"File created: {path}", "size": len(content)}

        elif name == "run_command":
            cmd = params.get("command", "")
            if not cmd:
                return {"success": False, "error": "command is required"}
            proc = await subprocess.create_subprocess_shell(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="C:\\Users\\PC",
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            except asyncio.TimeoutError:
                proc.kill()
                return {"success": False, "error": "Command timed out (180s)", "exit_code": -1}
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
                return {"success": True, "content": content[:6000], "total_lines": content.count("\n")}
            except FileNotFoundError:
                return {"success": False, "error": f"File not found: {path}"}

        elif name == "list_files":
            path = params.get("path", "C:\\Users\\PC")
            if not _os.path.exists(path):
                return {"success": False, "error": f"Path not found: {path}"}
            items = []
            for item in _os.listdir(path):
                full = _os.path.join(path, item)
                items.append({
                    "name": item,
                    "type": "dir" if _os.path.isdir(full) else "file",
                    "size": _os.path.getsize(full) if _os.path.isfile(full) else 0,
                })
            return {"success": True, "items": items[:100]}

        elif name == "web_search":
            query = params.get("query", "")
            if not query:
                return {"success": False, "error": "query is required"}
            try:
                from urllib.parse import quote as _quote
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.get(
                        f"https://html.duckduckgo.com/html/?q={_quote(query)}",
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    # Extract result snippets
                    text = resp.text
                    results = []
                    for line in text.split("\n"):
                        line = line.strip()
                        if line.startswith("result__a") and "href" in line.lower():
                            results.append(line[:300])
                    return {"success": True, "results": results[:10], "raw_length": len(text)}
            except Exception as e:
                return {"success": False, "error": str(e)}

        else:
            return {"success": False, "error": f"Unknown tool: {name}. Available: create_file, run_command, read_file, list_files, web_search"}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def agent_stream(task: str, max_steps: int = 50):
    """Agent loop - yields SSE events as the agent works."""
    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "system", "content": AGENT_MODEL_PROMPT},
        {"role": "user", "content": task},
    ]

    yield f"data: {_json.dumps({'type': 'start', 'task': task[:200]})}\n\n"

    async with httpx.AsyncClient(timeout=300.0) as client:
        # Check Ollama availability
        try:
            resp = await client.get("http://127.0.0.1:11434/api/tags", timeout=5)
            if resp.status_code != 200:
                yield f"data: {_json.dumps({'type': 'error', 'message': 'Ollama non disponible - verifie que Ollama tourne sur le port 11434'})}\n\n"
                return
            models = resp.json().get("models", [])
            model = None
            for pref in ["qwen2.5-coder:7b", "deepseek-r1:7b", "phi3:mini"]:
                if any(pref in m.get("name", "") for m in models):
                    model = pref
                    break
            if not model and models:
                model = models[0]["name"]
            if not model:
                yield f"data: {_json.dumps({'type': 'error', 'message': 'Aucun modele Ollama disponible'})}\n\n"
                return
            yield f"data: {_json.dumps({'type': 'model', 'model': model})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type': 'error', 'message': f'Connexion Ollama echouee: {e}'})}\n\n"
            return

        for step in range(max_steps):
            # Generate from Ollama
            try:
                resp = await client.post(
                    "http://127.0.0.1:11434/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {"num_predict": 4000, "temperature": 0.2},
                    },
                    timeout=180,
                )
                ai_text = resp.json().get("message", {}).get("content", "").strip()
            except Exception as e:
                yield f"data: {_json.dumps({'type': 'error', 'message': f'Ollama erreur (etape {step+1}): {e}'})}\n\n"
                break

            if not ai_text:
                yield f"data: {_json.dumps({'type': 'error', 'message': f'Ollama reponse vide (etape {step+1})'})}\n\n"
                break

            messages.append({"role": "assistant", "content": ai_text})

            # Extract JSON from response (handle markdown code blocks too)
            json_str = None
            # Try direct parse first
            try:
                parsed = _json.loads(ai_text)
                json_str = ai_text
            except:
                pass

            if not json_str:
                # Try to find JSON in code blocks
                import re
                json_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", ai_text)
                if not json_match:
                    json_match = re.search(r"\{[\s\S]*\}", ai_text)
                if json_match:
                    try:
                        _json.loads(json_match.group(1) if json_match.lastindex else json_match.group())
                        json_str = json_match.group(1) if json_match.lastindex else json_match.group()
                    except:
                        pass

            if not json_str:
                # Not JSON - treat as text response
                yield f"data: {_json.dumps({'type': 'message', 'step': step+1, 'text': ai_text[:1000]})}\n\n"
                # Ask model to use proper format
                messages.append({"role": "user", "content": "You must respond with valid JSON only. Use the format: {\"thought\": \"...\", \"action\": \"tool_name\", \"params\": {...}}. Try again."})
                continue

            try:
                action = _json.loads(json_str)
            except:
                yield f"data: {_json.dumps({'type': 'error', 'message': 'JSON parsing failed'})}\n\n"
                messages.append({"role": "user", "content": "Invalid JSON. Respond with valid JSON only: {\"thought\": \"...\", \"action\": \"tool_name\", \"params\": {...}}"})
                continue

            # Check if done
            if action.get("action") == "done":
                summary = action.get("summary", "Task completed")
                yield f"data: {_json.dumps({'type': 'done', 'step': step+1, 'summary': summary})}\n\n"
                break

            # Emit step info
            thought = action.get("thought", "")
            tool_name = action.get("action", "")
            tool_params = action.get("params", {})

            yield f"data: {_json.dumps({'type': 'step', 'step': step+1, 'thought': thought[:300], 'tool': tool_name})}\n\n"

            # Execute tool
            result = await agent_execute_tool(tool_name, tool_params)

            # Emit result
            result_display = dict(result)
            if result_display.get("stdout"):
                result_display["stdout"] = result_display["stdout"][:1500]
            if result_display.get("content"):
                result_display["content"] = result_display["content"][:1000]
            yield f"data: {_json.dumps({'type': 'result', 'step': step+1, 'success': result.get('success', False), 'result': result_display})}\n\n"

            # Feed result back to model (truncated for context)
            result_for_model = _json.dumps(result, ensure_ascii=False, indent=2)
            if len(result_for_model) > 3000:
                result_for_model = result_for_model[:3000] + "\n... (truncated)"

            messages.append({"role": "user", "content": f"Tool '{tool_name}' result:\n```\n{result_for_model}\n```\n\nContinue with the next step or report done when finished."})
        else:
            yield f"data: {_json.dumps({'type': 'error', 'message': f'Max steps reached ({max_steps})'})}\n\n"
