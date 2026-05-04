"""Add agent JS to hermes_computer_use.py cockpit"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

filepath = r"C:\Users\PC\.openclaw-autoclaw\agents\os-perso\workspace\hermes_computer_use.py"
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

if 'sendAgent' in content:
    print('Agent JS already present')
    sys.exit(0)

# Add agent progress div before </body>
agent_div = '<div id="agent-progress" style="display:none;position:fixed;bottom:80px;right:20px;width:400px;max-height:300px;overflow-y:auto;background:#1a1a2e;border:1px solid #a3e635;border-radius:12px;padding:12px;z-index:999;font-size:13px"></div>\n'
content = content.replace('</body>', agent_div + '</body>', 1)

# Agent JS - using template to avoid escaping issues
agent_js = """
    // === AGENT MODE ===
    function isAgentTask(msg) {
        var kw = ['screenshot','capture','ecran','fenetres','clipboard','presse-papier',
                    'aide','help','infos','processus','reseau','fichiers','fichier',
                    'ouvrir','site','web','page','kill','tuer'];
        var m = msg.toLowerCase();
        for (var i = 0; i < kw.length; i++) { if (m.indexOf(kw[i]) !== -1) return false; }
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
        var box = document.getElementById('agent-progress');
        if (box) box.style.display = 'block';
        addMsg('bot', '\\u{1F916} Agent HERMES en cours...');
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
                var lines = buf.split('\\n');
                buf = lines.pop();
                for (var j = 0; j < lines.length; j++) {
                    var line = lines[j];
                    if (line.indexOf('data: ') !== 0) continue;
                    try {
                        var e = JSON.parse(line.slice(6));
                        if (e.type === 'step') { steps.push(e); updProg(steps); }
                        else if (e.type === 'result') { if (steps.length) steps[steps.length-1].result=e; updProg(steps); }
                        else if (e.type === 'done') { addMsg('bot', '\\u2705 ' + e.summary); if(box) box.style.display='none'; return; }
                        else if (e.type === 'error') { addMsg('bot', '\\u274C ' + e.message); if(box) box.style.display='none'; return; }
                    } catch(x) {}
                }
            }
            addMsg('bot', 'Agent termine.');
            if (box) box.style.display = 'none';
        } catch(err) { addMsg('bot', 'Erreur agent: ' + err.message); }
    }
    function updProg(steps) {
        var box = document.getElementById('agent-progress');
        if (!box) return;
        var html = '<div style="font-weight:700;margin-bottom:8px">Agent HERMES</div>';
        for (var i = 0; i < steps.length; i++) {
            var s = steps[i];
            var ic = s.result ? (s.result.success ? '[OK]' : '[ERR]') : '[...]';
            var pv = s.result ? (s.result.preview||'').substring(0,80) : 'en cours';
            html += '<div style="margin:4px 0;padding:4px 8px;background:rgba(163,230,53,0.1);border-radius:6px;font-size:13px">' + ic + ' Etape ' + (i+1) + ' [' + s.tool + '] ' + pv + '</div>';
        }
        box.innerHTML = html;
        box.scrollTop = box.scrollHeight;
    }
"""

content = content.replace('</script>', agent_js + '</script>', 1)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"OK - Agent JS added. File size: {len(content)} chars")
