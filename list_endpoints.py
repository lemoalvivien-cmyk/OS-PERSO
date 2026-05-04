import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
r = urllib.request.urlopen('http://127.0.0.1:9307/openapi.json')
data = json.loads(r.read())
paths = list(data.get('paths', {}).keys())
print(f'{len(paths)} endpoints:')
for p in sorted(paths):
    methods = list(data['paths'][p].keys())
    print(f'  {p} [{", ".join(m.upper() for m in methods)}]')
