#!/usr/bin/env python3
"""
Live dashboard for the signal agent. Reads the agent's state file, pulls
live mid prices from Hyperliquid, and tails the agent's journal for recent
events. Serves a phone-friendly page on port 8080. Stdlib only.

Optional: set DASH_KEY in the environment to require ?key=... on every
request (light protection - the page is read-only either way).
"""
import json
import os
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

STATE_FILE = Path("/opt/btc-agent/btc_agent_state.json")
DASH_KEY = os.environ.get("DASH_KEY", "")
PORT = int(os.environ.get("DASH_PORT", "8080"))

_price_cache = {"t": 0.0, "mids": {}}


def prices():
    if time.time() - _price_cache["t"] < 5:
        return _price_cache["mids"]
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "allMids"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            mids = {k: float(v) for k, v in json.loads(r.read()).items()}
        _price_cache.update(t=time.time(), mids=mids)
    except Exception:
        pass
    return _price_cache["mids"]


def read_state():
    try:
        return json.loads(STATE_FILE.read_text()), STATE_FILE.stat().st_mtime
    except Exception:
        return {}, 0


def journal_events(n=400, keep=25):
    try:
        out = subprocess.run(
            ["journalctl", "-u", "btc-agent", "-n", str(n), "--no-pager",
             "-o", "cat"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    events = []
    for line in out.splitlines():
        if any(k in line for k in ("ALERT SENT", "zone", "doji", "TP1 HIT",
                                   "TP2 HIT", "STOPPED OUT", "RUNNER",
                                   "SUMMARY")):
            events.append(line.strip())
    return events[-keep:][::-1]


def build_data():
    state, mtime = read_state()
    mids = prices()
    trades, zones = [], []
    scanned = 0
    for sym, ast in state.items():
        if sym.startswith("_") or not isinstance(ast, dict):
            continue
        scanned += 1
        mid = mids.get(sym)
        tr = ast.get("trade")
        if tr:
            sign = 1 if tr["verdict"] == "LONG" else -1
            r1 = abs(tr["tp1"] - tr["entry"]) / 2 or 1
            pnl = r_now = None
            if mid:
                pnl = sign * (mid - tr["entry"]) / tr["entry"] * 100
                r_now = sign * (mid - tr["entry"]) / r1
            trades.append({"sym": sym, "dir": tr["verdict"],
                           "entry": tr["entry"], "stop": tr["stop"],
                           "tp1": tr["tp1"], "tp2": tr["tp2"],
                           "tp1_hit": tr.get("tp1_hit", False),
                           "mid": mid, "pnl": pnl, "r": r_now,
                           "opened_t": tr.get("opened_t", 0)})
        z = ast.get("zone")
        if z:
            seq = z.get("seq")
            stage = "hunting doji"
            if seq:
                stage = f"confirming ({seq.get('confirms', 0)}/2)"
            zones.append({"sym": sym, "dir": z["direction"],
                          "k": z.get("kval"), "stage": stage,
                          "mid": mid,
                          "mins_left": max(0, int((z.get("expires_t", 0)
                                          - time.time() * 1000) / 60000))})
    trades.sort(key=lambda t: t["sym"])
    zones.sort(key=lambda z: z["mins_left"])
    return {"now": time.time(),
            "state_age_s": int(time.time() - mtime) if mtime else None,
            "scanned": scanned, "trades": trades, "zones": zones,
            "events": journal_events()}


PAGE = """<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Agent</title><style>
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,sans-serif;
     margin:0;padding:12px;font-size:14px}
h1{font-size:17px;margin:4px 0 12px}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:12px;
       font-weight:600;margin-left:8px}
.ok{background:#12351f;color:#3fb950}.warn{background:#3a2b12;color:#d29922}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;
      padding:11px 13px;margin-bottom:9px}
.sym{font-weight:700;font-size:15px}
.long{color:#3fb950}.short{color:#f85149}
.num{font-family:Menlo,monospace}
.row{display:flex;justify-content:space-between;margin:3px 0}
.muted{color:#8b949e;font-size:12px}
.bar{height:6px;background:#21262d;border-radius:3px;margin:7px 0 2px;overflow:hidden}
.fill{height:100%;border-radius:3px}
.section{margin:16px 0 8px;font-size:12px;letter-spacing:1.5px;color:#8b949e;
         text-transform:uppercase}
.event{font-family:Menlo,monospace;font-size:11px;color:#8b949e;
       padding:3px 0;border-bottom:1px solid #161b22;word-break:break-all}
.pnl-pos{color:#3fb950;font-weight:700}.pnl-neg{color:#f85149;font-weight:700}
</style></head><body>
<h1>Signal Agent <span id=status class=badge></span>
<span id=meta class=muted style="font-weight:400;font-size:12px"></span></h1>
<div class=section>Open trades</div><div id=trades></div>
<div class=section>Active zones</div><div id=zones></div>
<div class=section>Recent events</div><div id=events></div>
<script>
const KEY=new URLSearchParams(location.search).get('key')||'';
function px(p){if(p==null)return '-';
 return p>=10000?p.toLocaleString(undefined,{maximumFractionDigits:0})
 :p>=1?p.toFixed(2):p.toFixed(6)}
async function tick(){
 try{
  const d=await (await fetch('/data'+(KEY?'?key='+KEY:''))).json();
  const st=document.getElementById('status');
  const fresh=d.state_age_s!=null&&d.state_age_s<180;
  st.textContent=fresh?'LIVE':'STALE '+(d.state_age_s==null?'':Math.round(d.state_age_s/60)+'m');
  st.className='badge '+(fresh?'ok':'warn');
  document.getElementById('meta').textContent=d.scanned+' markets';
  document.getElementById('trades').innerHTML=d.trades.length?d.trades.map(t=>{
   const cls=t.dir==='LONG'?'long':'short';
   const rp=t.r==null?0:Math.max(0,Math.min(100,(t.r+1)/4*100));
   const rc=t.r==null?'#8b949e':t.r>=0?'#3fb950':'#f85149';
   return `<div class=card>
    <div class=row><span class=sym>${t.sym} <span class=${cls}>${t.dir}</span>
    ${t.tp1_hit?'<span class="badge ok">runner</span>':''}</span>
    <span class="num ${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl==null?'-':(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'%'}</span></div>
    <div class=row><span class=muted>entry <span class=num>$${px(t.entry)}</span></span>
    <span class=muted>now <span class=num>$${px(t.mid)}</span></span></div>
    <div class=row><span class=muted>stop <span class=num>$${px(t.stop)}</span></span>
    <span class=muted>TP1 <span class=num>$${px(t.tp1)}</span> · TP2 <span class=num>$${px(t.tp2)}</span></span></div>
    <div class=bar><div class=fill style="width:${rp}%;background:${rc}"></div></div>
    <div class=muted>${t.r==null?'':t.r.toFixed(2)+'R'} (stop -1R → TP2 +3R)</div></div>`
  }).join(''):'<div class="card muted">none</div>';
  document.getElementById('zones').innerHTML=d.zones.length?d.zones.map(z=>{
   const cls=z.dir==='LONG'?'long':'short';
   return `<div class=card><div class=row>
    <span class=sym>${z.sym} <span class=${cls}>${z.dir}</span></span>
    <span class=muted>${z.mins_left}m left</span></div>
    <div class=row><span class=muted>${z.stage}</span>
    <span class=muted>%K ${z.k==null?'-':z.k.toFixed(1)} · now <span class=num>$${px(z.mid)}</span></span></div></div>`
  }).join(''):'<div class="card muted">none</div>';
  document.getElementById('events').innerHTML=
   d.events.map(e=>`<div class=event>${e.replace(/</g,'&lt;')}</div>`).join('')||'<div class="card muted">none</div>';
 }catch(e){document.getElementById('status').textContent='OFFLINE';
  document.getElementById('status').className='badge warn'}
}
tick();setInterval(tick,10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if DASH_KEY:
            key = (parse_qs(url.query).get("key") or [""])[0]
            if key != DASH_KEY:
                self._send(403, b"forbidden", "text/plain")
                return
        if url.path == "/data":
            self._send(200, json.dumps(build_data()).encode(),
                       "application/json")
        elif url.path == "/":
            self._send(200, PAGE.encode(), "text/html")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Dashboard on port {PORT}" + (" (key required)" if DASH_KEY else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
