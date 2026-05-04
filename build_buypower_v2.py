"""Build BuyPower - step by step via HERMES OS commands"""
import urllib.request, json, sys, time, subprocess
if sys.platform == "win32": sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:9307"

def send(msg, timeout=30):
    data = json.dumps({"message": msg}).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def run_hermes(cmd, timeout=120):
    """Send a run command to HERMES and wait for result."""
    r = send(f"run {cmd}", timeout=timeout)
    text = r.get("text", "")
    stdout = r.get("stdout", "")
    stderr = r.get("stderr", "")
    return text + stdout, stderr

def write_file(path, content):
    """Write a file via HERMES."""
    # Use PowerShell to write file
    escaped = content.replace("'", "''")
    cmd = f"powershell -Command \"Set-Content -Path '{path}' -Value '{escaped}' -Encoding UTF8\""
    return run_hermes(cmd)

def check_hermes():
    try:
        r = send("aide", timeout=5)
        return True
    except:
        return False

print("=== BUYPOWER - Construction via HERMES OS ===")
print()

if not check_hermes():
    print("ERREUR: HERMES OS ne repond pas sur port 9307")
    print("Lance d'abord: python hermes_computer_use.py --port 9307")
    sys.exit(1)

print("[OK] HERMES OS en ligne")

# Step 1: Create Next.js project
print("\n[1/6] Creation du projet Next.js...")
out, err = run_hermes("npx create-next-app@latest C:\\Users\\PC\\BuyPower --typescript --tailwind --eslint --app --src-dir --import-alias @/* --use-npm --no-turbopack", timeout=180)
print(out[:500] if out else "(no output)")
if err: print(f"STDERR: {err[:300]}")

time.sleep(3)

# Step 2: Install dependencies
print("\n[2/6] Installation des dependances...")
deps = [
    "cd C:\\Users\\PC\\BuyPower && npm install @supabase/supabase-js @supabase/ssr next-themes lucide-react stripe",
    "cd C:\\Users\\PC\\BuyPower && npx shadcn@latest init -d",
    "cd C:\\Users\\PC\\BuyPower && npx shadcn@latest add button card input label select textarea tabs dialog badge table switch separator sheet toast sonner dropdown-menu avatar form"
]
for dep in deps:
    print(f"  Running: {dep[:80]}...")
    out, err = run_hermes(dep, timeout=180)
    print(f"  {out[:200] if out else '(ok)'}")
    if err and "ERR" in err.upper():
        print(f"  STDERR: {err[:200]}")

# Check if project was created
if not os.path.exists("C:\\Users\\PC\\BuyPower\\package.json"):
    print("\nERREUR: Le projet n'a pas ete cree correctement.")
    print("HERMES OS ne peut pas construire une SaaS - il execute des commandes")
    print("mais n'a pas la capacite de generer des centaines de fichiers de code.")
    sys.exit(1)

print("\n[OK] Projet cree avec succes!")
print("Projet: C:\\Users\\PC\\BuyPower")
