"""Test the agent endpoint with a simple task"""
import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Send a simple task to the agent
task = "Create a file called C:\\Users\\PC\\Desktop\\agent_test.txt with content 'Hello from HERMES Agent!'"
data = json.dumps({"message": task}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:9307/api/agent",
    data=data,
    method="POST"
)
req.add_header("Content-Type", "application/json")

print(f"Sending task: {task[:80]}...")
print("Waiting for agent stream...\n")

resp = urllib.request.urlopen(req, timeout=120)
raw = resp.read().decode()

# Parse SSE events
for line in raw.split("\n"):
    if not line.startswith("data: "):
        continue
    try:
        evt = json.loads(line[6:])
        t = evt.get("type", "?")
        if t == "start":
            print(f"[START] Model: {evt.get('model')}")
        elif t == "step":
            print(f"[STEP {evt.get('step')}] {evt.get('tool')} - {evt.get('thought', '')[:80]}")
        elif t == "result":
            ok = "OK" if evt.get("success") else "FAIL"
            pv = evt.get("preview", "")[:100]
            print(f"  -> {ok}: {pv}")
        elif t == "done":
            print(f"\n[DONE] {evt.get('summary', '')}")
        elif t == "error":
            print(f"\n[ERROR] {evt.get('message', '')}")
    except json.JSONDecodeError:
        pass

# Check if file was created
import os
path = r"C:\Users\PC\Desktop\agent_test.txt"
if os.path.exists(path):
    with open(path) as f:
        print(f"\nFile content: {f.read()}")
    os.remove(path)
    print("File deleted (cleanup)")
else:
    print(f"\nFile NOT created at {path}")
