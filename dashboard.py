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
TRADES_LOG = Path("/opt/btc-agent/trades.log")
DASH_KEY = os.environ.get("DASH_KEY", "")
PORT = int(os.environ.get("DASH_PORT", "8080"))

_price_cache = {"t": 0.0, "mids": {}}


def prices():
    if time.time() - _price_cache["t"] < 2:
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


def closed_trades(keep=200):
    try:
        lines = TRADES_LOG.read_text().splitlines()[-2000:]
    except OSError:
        return [], {"d": 0.0, "w": 0.0, "m": 0.0}
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    now_ms = time.time() * 1000
    def total(days):
        cut = now_ms - days * 86400_000
        return round(sum(r.get("pnl_pct", 0) for r in rows
                         if r.get("t", 0) >= cut), 2)
    pnl = {"d": total(1), "w": total(7), "m": total(30)}
    return rows[-keep:][::-1], pnl


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
    closed, pnl = closed_trades()
    return {"now": time.time(),
            "state_age_s": int(time.time() - mtime) if mtime else None,
            "scanned": scanned, "trades": trades, "zones": zones,
            "closed": closed, "pnl": pnl,
            "events": journal_events()}


PAGE = """<!DOCTYPE html><html><head>
<meta charset="utf-8">
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
.tabs{display:flex;gap:6px}
.tab{flex:1;text-align:center;padding:6px 0;border-radius:8px;font-size:12px;
     font-weight:600;color:#8b949e;background:#0d1117;border:1px solid #21262d}
.tab.active{color:#e6edf3;background:#21262d}
.total{font-size:30px;font-weight:800;font-family:Menlo,monospace;
       text-align:center;margin:8px 0 10px}
</style></head><body>
<h1>Signal Agent <span id=status class=badge></span>
<span id=meta class=muted style="font-weight:400;font-size:12px"></span></h1>
<div class=card>
  <div id=total class=total>-</div>
  <div class=tabs>
    <div class="tab active" data-p=d onclick="setP('d')">DAY</div>
    <div class=tab data-p=w onclick="setP('w')">WEEK</div>
    <div class=tab data-p=m onclick="setP('m')">MONTH</div>
  </div>
</div>
<div class=section>Open trades</div><div id=trades></div>
<div class=section>Active zones</div><div id=zones></div>
<div class=section>Closed trades <span id=csub class=muted style="float:right;text-transform:none;letter-spacing:0"></span></div><div id=closed></div>
<div class=section>Recent events</div><div id=events></div>
<script>
const KEY=new URLSearchParams(location.search).get('key')||'';
let PERIOD='d', LAST=null;
const DAYS={d:1,w:7,m:30}, LABEL={d:'last 24h',w:'last 7 days',m:'last 30 days'};
function setP(p){PERIOD=p;
 document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.p===p));
 if(LAST)render(LAST);}
function px(p){if(p==null)return '-';
 return p>=10000?p.toLocaleString(undefined,{maximumFractionDigits:0})
 :p>=1?p.toFixed(2):p.toFixed(6)}
function render(d){
  LAST=d;
  const tot=d.pnl?d.pnl[PERIOD]:0;
  const te=document.getElementById('total');
  te.textContent=(tot>=0?'+':'')+tot.toFixed(2)+'%';
  te.className='total '+(tot>=0?'pnl-pos':'pnl-neg');
  const st=document.getElementById('status');
  const fresh=d.state_age_s!=null&&d.state_age_s<480;
  st.textContent=fresh?'LIVE':'STALE '+(d.state_age_s==null?'':Math.round(d.state_age_s/60)+'m');
  const age=d.state_age_s==null?'':' · scan '+(d.state_age_s<60?d.state_age_s+'s':Math.round(d.state_age_s/60)+'m')+' ago';
  st.className='badge '+(fresh?'ok':'warn');
  document.getElementById('meta').textContent=d.scanned+' markets'+age;
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
  document.getElementById('csub').textContent=LABEL[PERIOD];
  const cut=Date.now()-DAYS[PERIOD]*86400000;
  const shown=d.closed.filter(c=>c.t>=cut).slice(0,20);
  const icons={TP2:'🏁',STOP:'❌',RUNNER:'🏃'};
  document.getElementById('closed').innerHTML=shown.length?shown.map(c=>{
   const cls=c.dir==='LONG'?'long':'short';
   const when=new Date(c.t).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
   return `<div class=card><div class=row>
    <span class=sym>${icons[c.kind]||''} ${c.sym} <span class=${cls}>${c.dir}</span>
    <span class=muted style="font-weight:400">${c.kind}</span></span>
    <span class="num ${c.pnl_pct>=0?'pnl-pos':'pnl-neg'}">${(c.pnl_pct>=0?'+':'')+c.pnl_pct.toFixed(2)}%</span></div>
    <div class=row><span class=muted>$${px(c.entry)} → $${px(c.exit)}</span>
    <span class=muted>${when}</span></div></div>`
  }).join(''):'<div class="card muted">none in this period</div>';
  document.getElementById('events').innerHTML=
   d.events.map(e=>`<div class=event>${e.replace(/</g,'&lt;')}</div>`).join('')||'<div class="card muted">none</div>';
}
function offline(){document.getElementById('status').textContent='OFFLINE';
 document.getElementById('status').className='badge warn'}
async function poll(){try{render(await (await fetch('/data'+(KEY?'?key='+KEY:''))).json())}
 catch(e){offline()}}
let ES=null, lastMsg=0;
function connect(){
 try{if(ES)ES.close()}catch(e){}
 try{
  ES=new EventSource('/stream'+(KEY?'?key='+KEY:''));
  ES.onmessage=e=>{lastMsg=Date.now();render(JSON.parse(e.data))};
  ES.onerror=()=>{offline()};
 }catch(e){offline()}
}
// watchdog: if the stream goes quiet (backgrounded tab, dropped
// connection), poll once and rebuild the stream
setInterval(()=>{if(Date.now()-lastMsg>12000){poll();connect()}},6000);
document.addEventListener('visibilitychange',()=>{
 if(!document.hidden){poll();if(Date.now()-lastMsg>6000)connect()}});
poll();connect();
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
        elif url.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(build_data())
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        elif url.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Dashboard on port {PORT}" + (" (key required)" if DASH_KEY else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
