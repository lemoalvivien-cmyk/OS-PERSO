"""Update cockpit with agent v3 UI and restart HERMES"""
import sys, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

fp = r"C:\Users\PC\.openclaw-autoclaw\agents\os-perso\workspace\hermes_computer_use.py"
with open(fp, 'r', encoding='utf-8') as f:
    c = f.read()

# Clean old agent JS
c = re.sub(r'\s*// === HERMES AGENT MODE.*', '\n', c, flags=re.DOTALL)

# Clean old agent div
c = c.replace('<div id="agent-progress" style="display:none;position:fixed;bottom:80px;right:20px;width:420px;max-height:350px;overflow-y:auto;background:#0a0a1a;border:1px solid #a3e635;border-radius:12px;padding:14px;z-index:999;font-size:13px;color:#e0e0e0;font-family:Inter,sans-serif"></div>', '')

# Ensure import
if "from hermes_agent import register_agent_routes" not in c:
    lines = c.split('\n')
    last_imp = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            last_imp = i
    lines.insert(last_imp + 1, "import sys, os as _imp_os; sys.path.insert(0, _imp_os.path.dirname(_imp_os.path.abspath(__file__))); from hermes_agent import register_agent_routes")
    c = '\n'.join(lines)

if "register_agent_routes(app)" not in c:
    c = c.replace("        return app", "        register_agent_routes(app)\n        return app", 1)

# Agent progress div
agent_div = '\n<div id="agent-panel" style="display:none;position:fixed;bottom:16px;right:16px;width:440px;max-height:420px;overflow-y:auto;background:#0d1117;border:1px solid #30363d;border-radius:16px;padding:16px;z-index:9999;font-size:13px;color:#c9d1d9;font-family:-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5)"></div>'
if 'id="agent-panel"' not in c:
    c = c.replace('</body>', agent_div + '</body>', 1)

# Agent JS v3
agent_js = r"""
    // === HERMES AGENT v3 ===
    function isAgentTask(msg) {
        var skip = ['screenshot','capture','ecran','fenetre','clipboard','presse-papier',
                    'aide','help','infos','processus','reseau','ouvrir','site','web','page',
                    'kill','tuer','tasklist','statut','status','fichiers','list'];
        var m = msg.toLowerCase();
        for (var i = 0; i < skip.length; i++) { if (m.indexOf(skip[i]) !== -1) return false; }
        return msg.length > 25;
    }
    var _origSend = window.sendMsg;
    window.sendMsg = async function(msg, files) {
        if (files && files.length > 0) return _origSend(msg, files);
        if (isAgentTask(msg)) return sendAgent(msg);
        return _origSend(msg, files);
    };
    async function sendAgent(msg) {
        addMsg('user', msg);
        var panel = document.getElementById('agent-panel');
        if (!panel) return;
        panel.style.display = 'block';
        panel.innerHTML = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><span style="font-size:20px">\u{1F916}</span><b style="color:#a3e635">HERMES Agent v3</b><span style="margin-left:auto;font-size:11px;color:#666">D\u00E9marrage...</span></div>';
        try {
            var r = await fetch('/api/agent', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
            var reader = r.body.getReader();
            var dec = new TextDecoder();
            var buf = '';
            var steps = [];
            var planText = '';
            while (true) {
                var res = await reader.read();
                if (res.done) break;
                buf += dec.decode(res.value, {stream: true});
                var lines = buf.split('\n');
                buf = lines.pop();
                for (var j = 0; j < lines.length; j++) {
                    var line = lines[j];
                    if (line.indexOf('data: ') !== 0) continue;
                    try { var e = JSON.parse(line.slice(6)); handleAgentEvent(e, steps, panel, planText); } catch(x) {}
                }
            }
        } catch(err) { addMsg('bot', '\u274C ' + err.message); }
    }
    function handleAgentEvent(e, steps, panel, planText) {
        if (e.type === 'plan') { planText = e.content; }
        else if (e.type === 'step') { steps.push(e); }
        else if (e.type === 'result') { if (steps.length) steps[steps.length-1].result = e; }
        else if (e.type === 'done') {
            var stats = e.stats || {};
            addMsg('bot', '\u2705 ' + e.summary + '\n(\u{1F3AF} ' + (stats.steps||'?') + ' \u00E9tapes, ' + (stats.time||'?') + 's)');
            if (stats.tools) { addMsg('bot', 'Outils: ' + stats.tools.join(', ')); }
            panel.style.display = 'none';
            return;
        }
        else if (e.type === 'error') { addMsg('bot', '\u274C ' + e.message); panel.style.display = 'none'; return; }
        else if (e.type === 'skill_hit') { addMsg('bot', '\u{1F4BE} Comp\u00E9tence trouv\u00E9e: ' + e.name); }
        else if (e.type === 'crystallized') { addMsg('bot', '\u{1F48E} Comp\u00E9tence cristallis\u00E9e: ' + e.skill); }
        // Render panel
        var h = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><span style="font-size:20px">\u{1F916}</span><b style="color:#a3e635">HERMES v3</b>';
        if (e.type === 'phase') h += '<span style="margin-left:auto;font-size:11px;color:#666">' + e.message + '</span>';
        h += '</div>';
        if (planText) h += '<details style="margin-bottom:10px"><summary style="color:#8b949e;cursor:pointer;font-size:12px">\u{1F4CB} Plan</summary><pre style="margin:6px 0;padding:8px;background:#161b22;border-radius:8px;font-size:11px;white-space:pre-wrap;max-height:150px;overflow-y:auto">' + planText.replace(/</g,'&lt;') + '</pre></details>';
        for (var i = 0; i < steps.length; i++) {
            var s = steps[i];
            var ic, clr;
            if (s.result) { ic = s.result.success ? '\u2705' : '\u274C'; clr = s.result.success ? '#3fb950' : '#f85149'; }
            else { ic = '\u23F3'; clr = '#d29922'; }
            var pv = s.result ? (s.result.preview||'').substring(0, 100) : 'en cours...';
            var th = s.thought ? s.thought.substring(0, 80) : '';
            h += '<div style="margin:3px 0;padding:8px 10px;background:#161b22;border-left:3px solid ' + clr + ';border-radius:0 8px 8px 0;font-size:12px">';
            h += ic + ' <b style="color:#e6edf3">' + s.tool + '</b> <span style="color:#484f58;font-size:11px">\u00E9tape ' + (i+1) + '</span>';
            if (th) h += '<div style="color:#8b949e;font-size:11px;margin-top:2px">' + th.replace(/</g,'&lt;') + '</div>';
            h += '<div style="color:#6e7681;font-size:11px;margin-top:3px;word-break:break-all">' + pv.replace(/</g,'&lt;') + '</div></div>';
        }
        panel.innerHTML = h;
        panel.scrollTop = panel.scrollHeight;
    }
"""
if 'sendAgent' not in c:
    c = c.replace('</script>', agent_js + '</script>', 1)

with open(fp, 'w', encoding='utf-8') as f:
    f.write(c)
print(f"OK - Updated ({len(c)} chars)")
