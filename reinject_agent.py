"""Clean and re-inject agent code into hermes_computer_use.py"""
import sys, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

fp = r"C:\Users\PC\.openclaw-autoclaw\agents\os-perso\workspace\hermes_computer_use.py"
with open(fp, 'r', encoding='utf-8') as f:
    c = f.read()

# Remove old agent progress div
c = c.replace(
    '<div id="agent-progress" style="display:none;position:fixed;bottom:80px;right:20px;width:400px;max-height:300px;overflow-y:auto;background:#1a1a2e;border:1px solid #a3e635;border-radius:12px;padding:12px;z-index:999;font-size:13px"></div>',
    ''
)

# Remove old agent JS (everything between AGENT MODE comment and close of </script>)
c = re.sub(r'\s*// === AGENT MODE ===.*', '\n', c, flags=re.DOTALL)

# Make sure import and register call exist
if "from hermes_agent import" not in c:
    # Add import after last import line
    lines = c.split('\n')
    last_import = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            last_import = i
    lines.insert(last_import + 1, "import sys, os as _imp_os; sys.path.insert(0, _imp_os.path.dirname(_imp_os.path.abspath(__file__))); from hermes_agent import register_agent_routes")
    c = '\n'.join(lines)

if "register_agent_routes(app)" not in c:
    c = c.replace("        return app", "        # Agent mode\n        register_agent_routes(app)\n\n        return app", 1)

# Add NEW agent JS and div to cockpit
agent_div = '\n<div id="agent-progress" style="display:none;position:fixed;bottom:80px;right:20px;width:420px;max-height:350px;overflow-y:auto;background:#0a0a1a;border:1px solid #a3e635;border-radius:12px;padding:14px;z-index:999;font-size:13px;color:#e0e0e0;font-family:Inter,sans-serif"></div>'
if 'id="agent-progress"' not in c:
    c = c.replace('</body>', agent_div + '\n</body>', 1)

# Agent JS
agent_js = r"""
    // === HERMES AGENT MODE v2 ===
    function isAgentTask(msg) {
        var skip = ['screenshot','capture','ecran','fenetres','clipboard','presse-papier',
                    'aide','help','infos','processus','reseau','ouvrir','site','web','page',
                    'kill','tuer','tasklist'];
        var m = msg.toLowerCase();
        for (var i = 0; i < skip.length; i++) { if (m.indexOf(skip[i]) !== -1) return false; }
        return msg.length > 30;
    }
    var _origSend = window.sendMsg;
    window.sendMsg = async function(msg, files) {
        if (files && files.length > 0) return _origSend(msg, files);
        if (isAgentTask(msg)) return sendAgent(msg);
        return _origSend(msg, files);
    };
    async function sendAgent(msg) {
        addMsg('user', msg);
        var box = document.getElementById('agent-progress');
        if (box) { box.style.display = 'block'; box.innerHTML = '<div style="font-weight:700;color:#a3e635;margin-bottom:8px">\u{1F916} HERMES Agent</div><div style="color:#888">D\u00E9marrage...</div>'; }
        try {
            var r = await fetch('/api/agent', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
            var reader = r.body.getReader();
            var dec = new TextDecoder();
            var buf = '';
            var steps = [];
            while (true) {
                var res = await reader.read();
                if (res.done) break;
                buf += dec.decode(res.value, {stream: true});
                var lines = buf.split('\n');
                buf = lines.pop();
                for (var j = 0; j < lines.length; j++) {
                    var line = lines[j];
                    if (line.indexOf('data: ') !== 0) continue;
                    try {
                        var e = JSON.parse(line.slice(6));
                        if (e.type === 'plan') {
                            addMsg('bot', '\u{1F4CB} Plan:\n' + e.content.substring(0, 500));
                        } else if (e.type === 'step') {
                            steps.push(e);
                            updSteps(steps);
                        } else if (e.type === 'result') {
                            if (steps.length) steps[steps.length-1].result = e;
                            updSteps(steps);
                        } else if (e.type === 'done') {
                            addMsg('bot', '\u2705 ' + e.summary);
                            if (box) box.style.display = 'none';
                            return;
                        } else if (e.type === 'error') {
                            addMsg('bot', '\u274C Agent: ' + e.message);
                            if (box) box.style.display = 'none';
                            return;
                        } else if (e.type === 'phase') {
                            if (box) box.innerHTML = '<div style="font-weight:700;color:#a3e635;margin-bottom:8px">\u{1F916} HERMES Agent</div><div style="color:#888">' + e.message + '</div>';
                        }
                    } catch(x) {}
                }
            }
            addMsg('bot', '\u26A0\uFE0F Agent termin\u00E9.');
            if (box) box.style.display = 'none';
        } catch(err) { addMsg('bot', '\u274C Erreur: ' + err.message); }
    }
    function updSteps(steps) {
        var box = document.getElementById('agent-progress');
        if (!box) return;
        var h = '<div style="font-weight:700;color:#a3e635;margin-bottom:8px">\u{1F916} HERMES Agent (' + steps.length + ' \u00E9tapes)</div>';
        for (var i = 0; i < steps.length; i++) {
            var s = steps[i];
            var ic, clr;
            if (s.result) {
                if (s.result.success) { ic = '\u2705'; clr = '#4ade80'; }
                else { ic = '\u274C'; clr = '#f87171'; }
            } else { ic = '\u23F3'; clr = '#facc15'; }
            var pv = s.result ? (s.result.preview || '').substring(0, 100) : 'en cours...';
            var th = s.thought ? s.thought.substring(0, 60) : '';
            h += '<div style="margin:3px 0;padding:6px 8px;background:rgba(163,230,53,0.05);border-left:3px solid ' + clr + ';border-radius:4px;font-size:12px">';
            h += ic + ' <b>' + s.tool + '</b>';
            if (th) h += ' <span style="color:#888">- ' + th + '</span>';
            h += '<br><span style="color:#aaa;font-size:11px">' + pv.replace(/</g,'&lt;') + '</span></div>';
        }
        box.innerHTML = h;
        box.scrollTop = box.scrollHeight;
    }
"""
if 'sendAgent' not in c:
    c = c.replace('</script>', agent_js + '\n</script>', 1)

with open(fp, 'w', encoding='utf-8') as f:
    f.write(c)
print(f"OK - hermes_computer_use.py updated ({len(c)} chars)")
