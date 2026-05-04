import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

tools = [{'type': 'function', 'function': {
    'name': 'run_command',
    'description': 'Execute a shell command and return the output',
    'parameters': {'type': 'object', 'properties': {
        'command': {'type': 'string', 'description': 'The shell command to execute'}
    }, 'required': ['command']}
}}]

prompt = "Create a folder called test123 on the desktop. The path is C:\\Users\\PC\\Desktop\\test123"

for model in ['llama3.1:8b', 'qwen2.5-coder:7b']:
    print(f"\n=== {model} ===")
    req = urllib.request.Request(
        'http://127.0.0.1:11434/api/chat',
        data=json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': 'You are a computer assistant. Use tools to accomplish tasks. Always respond with valid JSON for tool calls.'},
                {'role': 'user', 'content': prompt}
            ],
            'tools': tools,
            'stream': False,
            'options': {'num_predict': 500, 'temperature': 0.1}
        }).encode(),
        method='POST'
    )
    req.add_header('Content-Type', 'application/json')
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        d = json.loads(resp.read())
        msg = d.get('message', {})
        has_tc = bool(msg.get('tool_calls'))
        content = (msg.get('content', '') or '')[:300]
        print(f"  tool_calls: {has_tc}")
        if msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                func = tc.get('function', {})
                args = str(func.get('arguments', ''))[:200]
                print(f"  -> {func.get('name', '?')}: {args}")
        else:
            print(f"  text: {content}")
    except Exception as e:
        print(f"  ERROR: {e}")
