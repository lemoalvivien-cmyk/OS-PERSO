import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Check models
resp = urllib.request.urlopen('http://127.0.0.1:11434/api/tags')
data = json.loads(resp.read())
for m in data.get('models', []):
    size_gb = m.get('size', 0) / 1e9
    print(f"{m['name']} - {size_gb:.1f}GB")

# Test tool calling with qwen2.5-coder
print('---')
req = urllib.request.Request(
    'http://127.0.0.1:11434/api/chat',
    data=json.dumps({
        'model': 'qwen2.5-coder:7b',
        'messages': [{'role': 'user', 'content': 'reply OK'}],
        'tools': [{'type': 'function', 'function': {
            'name': 'test_tool',
            'description': 'A test tool',
            'parameters': {'type': 'object', 'properties': {'x': {'type': 'string'}}, 'required': ['x']}
        }}],
        'stream': False
    }).encode(),
    method='POST'
)
req.add_header('Content-Type', 'application/json')
resp = urllib.request.urlopen(req, timeout=30)
d = json.loads(resp.read())
msg = d.get('message', {})
has_tc = bool(msg.get('tool_calls'))
content = msg.get('content', '') or ''
print(f"tool_calls present: {has_tc}")
print(f"content: {content[:300]}")
if msg.get('tool_calls'):
    for tc in msg['tool_calls']:
        func = tc.get('function', {})
        name = func.get('name', '?')
        args = str(func.get('arguments', '?'))[:200]
        print(f"  tool: {name} args: {args}")
