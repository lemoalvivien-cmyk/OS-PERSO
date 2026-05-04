"""Full integration test - HERMES Agent v2 with a real task"""
import urllib.request, json, sys, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

task = "Create a simple calculator web app in C:\\Users\\PC\\Desktop\\calculator. It should be a single HTML file with a modern design, CSS included inline, that has buttons for digits 0-9, +, -, *, /, =, and C (clear). It must work correctly - when you click buttons, it should display the calculation and result."

print(f"Task: {task[:80]}...")
print("=" * 60)

data = json.dumps({"message": task}).encode()
req = urllib.request.Request("http://127.0.0.1:9307/api/agent", data=data, method="POST")
req.add_header("Content-Type", "application/json")

t0 = time.time()
resp = urllib.request.urlopen(req, timeout=600)
raw = resp.read().decode()
elapsed = time.time() - t0

print(f"\nStream received ({elapsed:.1f}s, {len(raw)} bytes)\n")

for line in raw.split("\n"):
    if not line.startswith("data: "):
        continue
    try:
        evt = json.loads(line[6:])
        t = evt.get("type", "?")
        if t == "start":
            print(f"[START] Model: {evt.get('model')}")
        elif t == "plan":
            print(f"[PLAN] {evt.get('content', '')[:200]}")
        elif t == "phase":
            print(f"[{evt.get('phase', '?').upper()}] {evt.get('message', '')}")
        elif t == "step":
            print(f"  Step {evt.get('step')}: {evt.get('tool')} | {evt.get('thought', '')[:80]}")
        elif t == "result":
            ok = "OK" if evt.get("success") else "FAIL"
            pv = evt.get("preview", "")[:120]
            print(f"    -> {ok}: {pv}")
        elif t == "done":
            print(f"\n[DONE] Step {evt.get('step')} | {evt.get('summary', '')[:300]}")
        elif t == "error":
            print(f"\n[ERROR] {evt.get('message', '')}")
    except json.JSONDecodeError:
        pass

# Verify output
import os
target = r"C:\Users\PC\Desktop\calculator\index.html"
if os.path.exists(target):
    size = os.path.getsize(target)
    with open(target) as f:
        content = f.read()
    print(f"\n[VERIFY] File created: {target} ({size} bytes)")
    print(f"  Contains buttons: {'button' in content.lower()}")
    print(f"  Contains CSS: {'style' in content.lower()}")
    print(f"  Contains JS: {'function' in content.lower() or 'onclick' in content.lower()}")
    print(f"  Has all digits: {all(str(d) in content for d in range(10))}")
    # Clean up
    os.remove(target)
    os.rmdir(r"C:\Users\PC\Desktop\calculator")
    print("[CLEANUP] Removed test files")
else:
    print(f"\n[VERIFY] File NOT created at {target}")
