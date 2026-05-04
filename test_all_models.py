import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

tools = [{'type': 'function', 'function': {
    'name': 'run_command',
    'description': 'Execute a shell command and return the output',
    'parameters': {'type': 'object', 'properties': {
        'command': {'type': 'string', 'description': 'The shell command to execute'}
    }, 'required': ['command']}
}}]

prompt = """You are an autonomous assistant. The user wants you to run a command.

User request: "Create a folder called test123 on the desktop"

Choose a tool to use. Respond with JSON."""

for model in ['deepseek-r1:7b', 'phi3:mini', 'qwen2.5-coder:7b']:
    print(f"\n=== Testing {model} ===")
    req = urllib.request.Request(
        'http://127.0.0.1:11434/api/chat',
        data=json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': 'Create a folder called test123 on the desktop at C:\\Users\\PC\\Desktop\\test123'}
            ],
            'tools': tools,
            'stream': False,
            'options': {'num_predict': 500, 'temperature': 0.2}
        }).encode(),
        method='POST'
    )
    req.add_header('Content-Type', 'application/json')
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        d = json.loads(resp.read())
        msg = d.get('message', {})
        has_tc = bool(msg.get('tool_calls'))
        content = (msg.get('content', '') or '')[:500]
        print(f"  tool_calls: {has_tc}")
        if msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                func = tc.get('function', {})
                print(f"  -> tool: {func.get('name', '?')}")
                print(f"     args: {str(func.get('arguments', ''))[:300]}")
        else:
            print(f"  content: {content}")
    except Exception as e:
        print(f"  ERROR: {e}")
