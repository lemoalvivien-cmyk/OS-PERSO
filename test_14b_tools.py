"""Test qwen2.5-coder:14b tool calling support"""
import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

tools = [{'type': 'function', 'function': {
    'name': 'create_file',
    'description': 'Create a file',
    'parameters': {'type': 'object', 'properties': {
        'path': {'type': 'string'}, 'content': {'type': 'string'}
    }, 'required': ['path', 'content']}
}}]

task = "Create a simple hello.txt on C:\\Users\\PC\\Desktop with content 'Hello World'"
req = urllib.request.Request(
    'http://127.0.0.1:11434/api/chat',
    data=json.dumps({
        'model': 'qwen2.5-coder:14b',
        'messages': [
            {'role': 'system', 'content': 'You are an autonomous agent. Use tools to accomplish tasks.'},
            {'role': 'user', 'content': task}
        ],
        'tools': tools,
        'stream': False,
        'options': {'num_predict': 500, 'temperature': 0.1}
    }).encode(),
    method='POST'
)
req.add_header('Content-Type', 'application/json')
print(f"Testing qwen2.5-coder:14b with tool calling...")
resp = urllib.request.urlopen(req, timeout=180)
d = json.loads(resp.read())
msg = d.get('message', {})
has_tc = bool(msg.get('tool_calls'))
print(f"tool_calls: {has_tc}")
if has_tc:
    for tc in msg['tool_calls']:
        func = tc.get('function', {})
        print(f"  -> {func.get('name')}: {func.get('arguments', '')[:200]}")
else:
    print(f"  text: {(msg.get('content', '') or '')[:300]}")
