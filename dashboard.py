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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

STATE_FILE = Path("/opt/btc-agent/btc_agent_state.json")
TRADES_LOG = Path("/opt/btc-agent/trades.log")
DASH_KEY = os.environ.get("DASH_KEY", "")
PORT = int(os.environ.get("DASH_PORT", "8080"))

_price_cache = {"t": 0.0, "mids": {}}


def _mids_for(dex=None):
    """allMids for one venue. Builder venues need an explicit dex arg;
    their keys may come back bare ('GOLD') or prefixed ('xyz:GOLD')."""
    payload = {"type": "allMids"}
    if dex:
        payload["dex"] = dex
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        raw = json.loads(r.read())
    out = {}
    for k, v in raw.items():
        try:
            px = float(v)
        except (TypeError, ValueError):
            continue
        out[k] = px
        if dex and ":" not in k:
            out[f"{dex}:{k}"] = px          # match how the agent names them
    return out


_btc = {"t": 0.0, "px": None, "chg": None, "prev": None}


def btc_prev_close():
    """Yesterday's BTC price. Changes once a day, so cache it for 10 minutes.
    The LIVE price comes from allMids, which the dashboard already fetches."""
    if _btc["prev"] is not None and time.time() - _btc["t"] < 600:
        return _btc["prev"]
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            meta, ctxs = json.loads(r.read())
        idx = next(i for i, a in enumerate(meta["universe"])
                   if a["name"] == "BTC")
        ctx = ctxs[idx]
        _btc.update(t=time.time(),
                    prev=float(ctx.get("prevDayPx") or 0) or None)
    except Exception:
        _btc["t"] = time.time()          # do not hammer on failure
    return _btc["prev"]


def _btc_now(mids):
    """Live BTC price straight from the mids we already fetched."""
    px = mids.get("BTC")
    prev = btc_prev_close()
    return {"px": px,
            "chg": ((px - prev) / prev * 100) if (px and prev) else None}


def prices():
    if time.time() - _price_cache["t"] < 0.8:
        return _price_cache["mids"]
    mids = {}
    try:
        mids.update(_mids_for())
    except Exception:
        pass
    # every venue that appears in state (xyz, km, ...) gets its own call
    state, _ = read_state()
    dexes = sorted({k.split(":")[0] for k in state
                    if isinstance(k, str) and ":" in k and not k.startswith("_")})
    for dex in dexes:
        try:
            mids.update(_mids_for(dex))
        except Exception:
            continue
    if mids:
        _price_cache.update(t=time.time(), mids=mids)
    return _price_cache["mids"]


BUILD = str(int(os.path.getmtime(__file__)))   # changes on every deploy


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
        if any(k in line for k in ("ALERT SENT", "ENTRY",
                                   "LIVE", "DRY RUN", "UNPROTECTED",
                                   "order blocked", "execution client",
                                   "huge breakout",
                                   "no 4h candle", "too tight",
                                   "REPLACED", "day rolled over",
                                   "TP HIT", "STOPPED OUT",
                                   "skipped", "SUMMARY")):
            events.append(line.strip())
    return events[-keep:][::-1]


def closed_trades(keep=200):
    try:
        lines = TRADES_LOG.read_text().splitlines()[-2000:]
    except OSError:
        # must match the shape window() returns below, per period - returning
        # bare floats here is what used to throw in the browser and show
        # OFFLINE whenever trades.log was missing
        empty = {"pnl": 0.0, "w": 0, "l": 0}
        return [], {"d": dict(empty), "w": dict(empty), "m": dict(empty)}
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    now_ms = time.time() * 1000
    def window(days):
        cut = now_ms - days * 86400_000
        sub = [r for r in rows if r.get("t", 0) >= cut]
        return {"pnl": round(sum(r.get("pnl_pct", 0) for r in sub), 2),
                "w": sum(1 for r in sub if r.get("pnl_pct", 0) > 0),
                "l": sum(1 for r in sub if r.get("pnl_pct", 0) < 0)}
    pnl = {"d": window(1), "w": window(7), "m": window(30)}
    return rows[-keep:][::-1], pnl


def _lvl(v):
    """Level as a display string, tolerant of sub-dollar markets."""
    if v is None:
        return "?"
    return f"{v:,.6f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"


def scan_age_s(state, mtime):
    """Seconds since the agent last completed a scan. Prefers the explicit
    heartbeat the agent writes into _meta; falls back to the file mtime."""
    last = (state.get("_meta") or {}).get("last_scan_utc")
    if last:
        try:
            return int(time.time() - datetime.fromisoformat(last).timestamp())
        except (ValueError, TypeError):
            pass
    return int(time.time() - mtime) if mtime else None


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
        setup = ast.get("setup")
        above = (setup or {}).get("side") == "above"
        armed_dir = None
        if setup:
            armed_dir = "SHORT" if above else "LONG"

        if tr:
            sign = 1 if tr["verdict"] == "LONG" else -1
            tp = tr.get("tp")
            risk = tr.get("risk0") or abs(tr["entry"] - tr["stop"]) or 1
            pnl = r_now = None
            if mid:
                pnl = sign * (mid - tr["entry"]) / tr["entry"] * 100
                r_now = sign * (mid - tr["entry"]) / risk
            trades.append({"sym": sym, "dir": tr["verdict"],
                           "lev": ast.get("lev"), "risk": risk,
                           "entry": tr["entry"], "stop": tr["stop"],
                           "tp": tp, "mid": mid, "pnl": pnl, "r": r_now,
                           "rr": tr.get("rr"),
                           "armed": armed_dir if armed_dir != tr["verdict"]
                           else None,
                           "opened_t": tr.get("opened_t", 0)})

        # an armed breakout: price has CLOSED outside the range and the agent
        # is waiting for a close back inside. dist_pct is how far price still
        # is from the level it has to close back through; the bar fills as
        # that gap closes, scaled against the width of the range itself so
        # the reference never moves.
        if setup and not tr:
            lvl = setup.get("level")
            ext = setup.get("extreme")
            width = None
            if setup.get("hi") is not None and setup.get("lo") is not None:
                width = setup["hi"] - setup["lo"]
            dist = dist_pct = prog = None
            if mid and lvl:
                dist = mid - lvl if above else lvl - mid
                dist_pct = dist / mid * 100
                if width:
                    prog = max(0.0, min(100.0, (1 - dist / width) * 100))
            zones.append({"sym": sym, "dir": armed_dir,
                          "lev": ast.get("lev"),
                          "level": lvl, "extreme": ext,
                          "dist": dist, "dist_pct": dist_pct,
                          "stage": f"closed {'above' if above else 'below'} "
                                   f"${_lvl(lvl)} \u00b7 waiting for a close "
                                   "back inside",
                          "mid": mid, "prog": prog})

    trades.sort(key=lambda t: t["sym"])
    zones.sort(key=lambda z: z["sym"])
    closed, pnl = closed_trades()
    return {"now": time.time(),
            "state_age_s": scan_age_s(state, mtime),
            "scanned": scanned, "trades": trades, "zones": zones,
            "closed": closed, "pnl": pnl, "build": BUILD,
            "btc": _btc_now(mids),
            "events": journal_events()}


PAGE = """<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Agent</title><style>
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,sans-serif;
     margin:0;padding:12px;font-size:14px}
h1{font-size:17px;margin:4px 0 12px}
.lev{font-size:10.5px;font-weight:800;color:#8b949e;background:#21262d;
     padding:2px 7px;border-radius:999px;vertical-align:middle}
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
.shead{cursor:pointer;-webkit-user-select:none;user-select:none}
.chev{display:inline-block;width:13px}
.cnt{color:#e6edf3;background:#21262d;border-radius:8px;padding:0 7px;
     margin-left:6px;font-size:11px}
</style></head><body>
<h1>Signal Agent <span id=status class=badge></span>
<span id=meta class=muted style="font-weight:400;font-size:12px"></span></h1>
<div id=btcbar class=card style="display:flex;justify-content:space-between;
     align-items:center;padding:9px 13px;position:sticky;top:0;z-index:20;
     backdrop-filter:blur(8px);background:rgba(22,27,34,.92)">
  <span style="font-weight:800;font-size:14px">BTC</span>
  <span id=btcpx style="font-family:ui-monospace,monospace;font-size:15px">-</span>
  <span id=btcchg style="font-weight:800;font-size:13px">-</span>
</div>
<div class=card>
  <div id=total class=total>-</div>
  <div id=wl class=muted style="text-align:center;margin:-6px 0 10px"></div>
  <div class=tabs>
    <div class="tab active" data-p=d onclick="setP('d')">DAY</div>
    <div class=tab data-p=w onclick="setP('w')">WEEK</div>
    <div class=tab data-p=m onclick="setP('m')">MONTH</div>
  </div>
</div>
<div class="section shead" onclick="toggle('trades')"><span class=chev id=c-trades>\u25be</span>Open trades<span class=cnt id=n-trades>0</span></div><div id=trades></div>
<div class="section shead" onclick="toggle('zones')"><span class=chev id=c-zones>\u25be</span>Watching<span class=cnt id=n-zones>0</span></div><div id=zones></div>
<div class="section shead" onclick="toggle('closed')"><span class=chev id=c-closed>\u25be</span>Closed trades<span class=cnt id=n-closed>0</span> <span id=csub class=muted style="float:right;text-transform:none;letter-spacing:0"></span></div><div id=closed></div>
<div class="section shead" onclick="toggle('events')"><span class=chev id=c-events>\u25be</span>Recent events<span class=cnt id=n-events>0</span></div><div id=events></div>
<script>
const KEY=new URLSearchParams(location.search).get('key')||'';
let PERIOD='d', LAST=null;
let COLLAPSED={};
try{COLLAPSED=JSON.parse(localStorage.getItem('dashCollapsed')||'{}')}catch(e){}
function applyCollapse(){['trades','zones','closed','events'].forEach(id=>{
 const el=document.getElementById(id), ch=document.getElementById('c-'+id);
 if(el)el.style.display=COLLAPSED[id]?'none':'';
 if(ch)ch.textContent=COLLAPSED[id]?'\u25b8':'\u25be'})}
function toggle(id){COLLAPSED[id]=!COLLAPSED[id];
 try{localStorage.setItem('dashCollapsed',JSON.stringify(COLLAPSED))}catch(e){}
 applyCollapse()}
const DAYS={d:1,w:7,m:30}, LABEL={d:'last 24h',w:'last 7 days',m:'last 30 days'};
function setP(p){PERIOD=p;
 document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.p===p));
 if(LAST)render(LAST);}
function px(p){if(p==null)return '-';
 return p>=10000?p.toLocaleString(undefined,{maximumFractionDigits:0})
 :p>=1?p.toFixed(2):p.toFixed(6)}
function render(d){
 if(d.btc&&d.btc.px!=null){
   document.getElementById('btcpx').textContent='$'+d.btc.px.toLocaleString(
     undefined,{minimumFractionDigits:0,maximumFractionDigits:0});
   const e=document.getElementById('btcchg');
   if(d.btc.chg!=null){e.textContent=(d.btc.chg>=0?'+':'')+d.btc.chg.toFixed(2)+'% 24h';
     e.style.color=d.btc.chg>=0?'#3fb950':'#f85149';}
   else{e.textContent='';}
 }
 // the page never reloads on its own - if the server was redeployed, the tab
 // is running stale JavaScript, so refresh it once
 if(d.build){ if(window.__build===undefined){window.__build=d.build;}
              else if(d.build!==window.__build){location.reload();return;} }
  LAST=d;
  const pw=d.pnl?d.pnl[PERIOD]:{pnl:0,w:0,l:0};
  const tot=pw.pnl;
  const te=document.getElementById('total');
  te.textContent=(tot>=0?'+':'')+tot.toFixed(2)+'%';
  te.className='total '+(tot>=0?'pnl-pos':'pnl-neg');
  const n=pw.w+pw.l;
  document.getElementById('wl').innerHTML=n?
   `<span class=pnl-pos>${pw.w}W</span> · <span class=pnl-neg>${pw.l}L</span> · ${Math.round(pw.w/n*100)}% win rate`
   :'no closed trades yet';
  const st=document.getElementById('status');
  const fresh=d.state_age_s!=null&&d.state_age_s<480;
  st.textContent=fresh?'LIVE':'STALE '+(d.state_age_s==null?'':Math.round(d.state_age_s/60)+'m');
  const age=d.state_age_s==null?'':' · scan '+(d.state_age_s<60?d.state_age_s+'s':Math.round(d.state_age_s/60)+'m')+' ago';
  st.className='badge '+(fresh?'ok':'warn');
  document.getElementById('meta').textContent=d.scanned+' markets'+age;
  document.getElementById('trades').innerHTML=d.trades.length?d.trades.map(t=>{
   const cls=t.dir==='LONG'?'long':'short';
   const sgn=t.dir==='LONG'?1:-1;
   // each trade's true RR from its own prices (targets vary: 2R..3R/structure)
   const RRT=(t.tp!=null&&t.risk)?Math.abs((t.tp-t.entry)/t.risk)
     :((t.tp!=null&&t.entry!==t.stop)?Math.abs((t.tp-t.entry)/(t.entry-t.stop)):2);
   // freeze the card once TP or stop has traded - the agent confirms the
   // close on its next scan (<=5 min) and the card moves to Closed trades
   // latch the freeze per trade: once TP or the stop trades, this card stops
   // updating even if price wanders back through the level
   const fkey=`${t.sym}:${t.opened_t}`;
   window.__froz=window.__froz||{};
   if(t.r!=null&&t.r>=RRT)window.__froz[fkey]='tp';
   if(t.r!=null&&t.r<=-1)window.__froz[fkey]='sl';
   const tpDone=window.__froz[fkey]==='tp', slDone=window.__froz[fkey]==='sl';
   const showR=tpDone?RRT:slDone?-1:t.r;
   const showPnl=tpDone?sgn*(t.tp-t.entry)/t.entry*100
     :slDone?sgn*(t.stop-t.entry)/t.entry*100:t.pnl;
   const badge=tpDone?'<span class="badge ok">TP hit · closing</span>'
     :slDone?'<span class="badge warn">stop hit · closing</span>'
     :t.armed?`<span class="badge warn">${t.armed} armed · would override</span>`:'';
   // one scale: stop = 0%, entry = 40%, TP = 100% - the fill IS closeness to TP
   const rp=showR==null?0:Math.max(0,Math.min(100,(showR+1)/(1+RRT)*100));
   const rc=showR==null?'#8b949e':showR>=0?'#3fb950':'#f85149';
   const rlbl=showR==null?''
     :tpDone?'TP reached - waiting for the close confirmation'
     :slDone?'Stop traded - waiting for the close confirmation'
     :showR>=0
     ?`${showR.toFixed(2)}R · ${Math.round(Math.min(100,showR/RRT*100))}% of the way to TP`
     :`${showR.toFixed(2)}R · ${Math.round(Math.min(100,-showR*100))}% of the way to stop`;
   return `<div class=card>
    <div class=row><span class=sym>${t.sym} <span class=${cls}>${t.dir}</span>${t.lev?` <span class=lev>${t.lev}x</span>`:''} ${badge}</span>
    <span class="num ${showPnl>=0?'pnl-pos':'pnl-neg'}">${showPnl==null?'-':(showPnl>=0?'+':'')+showPnl.toFixed(2)+'%'}</span></div>
    <div class=row><span class=muted>entry <span class=num>$${px(t.entry)}</span></span>
    <span class=muted>${tpDone||slDone?'exit':'now'} <span class=num>$${px(tpDone?t.tp:slDone?t.stop:t.mid)}</span></span></div>
    <div class=row><span class=muted>stop <span class=num>$${px(t.stop)}</span></span>
    <span class=muted>TP <span class=num>$${px(t.tp)}</span></span></div>
    <div class=bar><div class=fill style="width:${rp}%;background:${rc}"></div></div>
    <div class=muted>${rlbl}</div></div>`
  }).join(''):'<div class="card muted">none</div>';
  document.getElementById('zones').innerHTML=d.zones.length?d.zones.map(z=>{
   const cls=z.dir==='LONG'?'long':'short';
   const near=z.dist_pct!=null&&z.dist_pct<=0;
   const gap=z.dist_pct==null?''
    :near?`back inside the range - entry triggers on this candle's close`
    :`<span class=num>${z.dist_pct.toFixed(2)}%</span> from the range ${z.dir==='SHORT'?'high':'low'} ($${px(z.level)})`;
   const pbar=z.prog==null?(gap?`<div class=muted style="margin-top:6px">${gap}</div>`:'')
    :`<div class=bar><div class=fill style="width:${z.prog}%;background:${near?'#3fb950':'#58a6ff'}"></div></div>
      <div class=muted>${gap}</div>`;
   return `<div class=card><div class=row>
    <span class=sym>${z.sym} <span class=${cls}>${z.dir}</span>${z.lev?` <span class=lev>${z.lev}x</span>`:''}</span>
    <span class=muted>now <span class=num>$${px(z.mid)}</span></span></div>
    <div class=row><span class=muted>${z.stage}</span></div>${pbar}</div>`
  }).join(''):'<div class="card muted">none</div>';
  document.getElementById('csub').textContent=LABEL[PERIOD];
  const cut=Date.now()-DAYS[PERIOD]*86400000;
  const shown=d.closed.filter(c=>c.t>=cut).slice(0,20);
  const icons={TP:'✅',STOP:'❌',OVERRIDE:'🔄'};
  // OVERRIDE closes have no fixed outcome - the P&L decides the icon
  const iconFor=c=>icons[c.kind]||(c.pnl_pct>=0?'✅':'❌');
  document.getElementById('closed').innerHTML=shown.length?shown.map(c=>{
   const cls=c.dir==='LONG'?'long':'short';
   const when=new Date(c.t).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
   return `<div class=card><div class=row>
    <span class=sym>${iconFor(c)} ${c.sym} <span class=${cls}>${c.dir}</span>
    <span class=muted style="font-weight:400">${c.kind}</span></span>
    <span class="num ${c.pnl_pct>=0?'pnl-pos':'pnl-neg'}">${(c.pnl_pct>=0?'+':'')+c.pnl_pct.toFixed(2)}%</span></div>
    <div class=row><span class=muted>$${px(c.entry)} → $${px(c.exit)}</span>
    <span class=muted>${when}</span></div></div>`
  }).join(''):'<div class="card muted">none in this period</div>';
  document.getElementById('events').innerHTML=
   d.events.map(e=>`<div class=event>${e.replace(/</g,'&lt;')}</div>`).join('')||'<div class="card muted">none</div>';
  document.getElementById('n-trades').textContent=d.trades.length;
  document.getElementById('n-zones').textContent=d.zones.length;
  document.getElementById('n-closed').textContent=shown.length;
  document.getElementById('n-events').textContent=d.events.length;
  applyCollapse();
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
                    time.sleep(1)
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
