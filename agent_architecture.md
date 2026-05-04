# HERMES OS Agent Mode - Architecture

## What was done:
1. Pulled `llama3.1:8b` (4.9GB) - supports native tool calling via Ollama
2. Tested all models:
   - llama3.1:8b → native tool_calls ✓ (best for agent planning)
   - qwen2.5-coder:7b → JSON output but no native tool_calls
   - deepseek-r1:7b → 400 error with tools
   - phi3:mini → 400 error with tools

## Architecture:
- **Planner model**: llama3.1:8b (tool calling, reasoning, multi-step)
- **Code model**: qwen2.5-coder:7b (for code generation tasks specifically)
- **Agent loop**: ReAct pattern (Reason → Act → Observe → Repeat)
- **Tools**: create_file, run_command, read_file, list_files, web_search
- **Endpoint**: POST /api/agent (SSE streaming)
- **Max steps**: 50 per task
- **UI**: Real-time progress in cockpit

## Files to create:
1. `hermes_agent.py` - standalone agent server (port 9308) OR
2. Modify `hermes_computer_use.py` - add agent endpoint to existing server

## Decision: Modify existing hermes_computer_use.py
- Add /api/agent endpoint
- Use llama3.1:8b for planning
- Use qwen2.5-coder:7b for code content
- SSE streaming for real-time progress
- Update cockpit HTML for agent progress UI
