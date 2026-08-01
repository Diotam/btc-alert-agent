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
# Paste the id from a saved TradingView layout that already has Smoothed
# Heiken Ashi applied, e.g. the "AbCd1234" in
# https://www.tradingview.com/chart/AbCd1234/ - cards then open THAT layout
# with the symbol swapped in, so the indicator comes with it. Indicators
# cannot be passed as URL parameters; a saved layout is the only free route.
# Left blank, cards open a plain chart instead.
TV_LAYOUT = os.environ.get("TV_LAYOUT", "").strip().strip("/")
CLOSE_REQ_DIR = STATE_FILE.parent / "close_requests"

# The dashboard closes positions ITSELF so a manual close is immediate rather
# than waiting up to SCAN_EVERY for the agent to notice. It reuses the
# agent's own execution code - same client, same rounding, same order
# sequence - instead of a second implementation that could drift.
try:
    import btc_alert_agent as agent          # same directory on the droplet
except BaseException as _e:                  # dashboard must still run alone
    # BaseException, not Exception: the agent calls raise SystemExit on a bad
    # config value, and SystemExit is NOT an Exception - catching only
    # Exception would let it kill the dashboard at import time
    agent = None
    print(f"agent module unavailable ({type(_e).__name__}: {_e}) - "
          "manual closes will be queued for the agent instead")


def _req_path(sym):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in sym)
    return CLOSE_REQ_DIR / (safe + ".req")


def pending_closes():
    """Symbols already closed here and waiting for the agent to clear state.
    They are hidden from the panels so a closed position does not linger."""
    out = set()
    try:
        for f in CLOSE_REQ_DIR.glob("*.req"):
            try:
                r = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if r.get("done") and r.get("sym"):
                out.add(r["sym"])
    except OSError:
        pass
    return out


def request_close(sym):
    """Close this position NOW. Returns (ok, message).

    Only symbols the agent currently holds a trade on are accepted, so a
    stray request cannot act on a position that does not exist.
    """
    state, _ = read_state()
    ast = state.get(sym)
    if not isinstance(ast, dict) or not ast.get("trade"):
        return False, "no open trade on that symbol"
    trade = dict(ast["trade"])
    px = (prices() or {}).get(sym) or trade.get("entry")

    done, note = False, "queued for the agent"
    if agent is not None:
        try:
            if agent.EXEC_LIVE and agent.executable(sym) and trade.get("size"):
                agent.close_position_live({"symbol": sym, "hl_coin": sym,
                                           "label": sym, "fallbacks": []},
                                          trade)
                note = "closed at market"
            else:
                note = ("alert-only symbol - nothing to close on the exchange"
                        if not agent.executable(sym) else "no live position")
            # book it here too, so P&L updates immediately. record_close
            # appends a single line in "a" mode, which is atomic enough for
            # two processes.
            agent.record_close(sym, trade, px, "MANUAL",
                               int(time.time() * 1000),
                               frac=trade.get("left", 1.0))
            done = True
        except Exception as e:
            return False, f"close failed ({type(e).__name__}: {e})"

    try:
        CLOSE_REQ_DIR.mkdir(parents=True, exist_ok=True)
        _req_path(sym).write_text(json.dumps(
            {"sym": sym, "asked": time.time(), "done": done, "exit": px}))
    except OSError as e:
        return False, f"closed, but the state marker failed ({type(e).__name__})"
    return True, note
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


MIDS_TTL_S = 3.0        # allMids is fetched once per venue, so the SSE tick
                        # rate used to multiply straight into API calls


def prices():
    if time.time() - _price_cache["t"] < MIDS_TTL_S:
        return _price_cache["mids"]
    mids = {}
    try:
        mids.update(_mids_for())
    except Exception:
        pass
    # every venue that appears in state (xyz, km, ...) gets its own call
    state, _ = read_state()
    # venue list changes only when the universe does - no need to re-derive
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


SSE_TICK_S = 2.0        # how often the stream pushes a fresh snapshot
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
                                   "LIVE", "SIZED", "UNPROTECTED",
                                   "order blocked", "execution client",
                                   "smoothed HA flipped", "target hit",
                                   "stop moved to entry", "too tight",
                                   "HA flipped against", "setup cleared",
                                   "TP_HALF", "RUNNER", "BE", "STOPPED OUT",
                                   "skipped", "SUMMARY")):
            events.append(line.strip())
    return events[-keep:][::-1]


def merge_partials(raw):
    """One TRADE, one row. A partial books TP_HALF and the remainder books
    RUNNER or BE later, so a single trade writes two ledger lines - and
    counting them separately double-counts the wins and the trade total.

    There is no trade id in the ledger, so rows are paired on
    (symbol, direction, entry). The pnl_pct values are already weighted by
    `frac`, so summing them gives the whole trade's return. The merged row
    is dated and priced by the FINAL close.
    """
    out, index = [], {}
    for r in raw:
        key = (r.get("sym"), r.get("dir"), round(r.get("entry", 0), 10))
        prev = index.get(key)
        # only merge into a trade that is still incomplete - once the frac
        # reaches 1.0 the next TP_HALF on the same symbol and price is a
        # genuinely new trade
        if prev is not None and prev["frac"] < 0.999:
            prev["pnl_pct"] = round(prev["pnl_pct"] + r.get("pnl_pct", 0), 3)
            prev["frac"] = round(prev["frac"] + r.get("frac", 1.0), 6)
            prev["exit"] = r.get("exit", prev["exit"])
            prev["t"] = max(prev.get("t", 0), r.get("t", 0))
            prev["kind"] = ("TP_RUNNER" if r.get("kind") == "RUNNER"
                            else "TP_BE" if r.get("kind") == "BE"
                            else r.get("kind", prev["kind"]))
            continue
        row = dict(r)
        row["frac"] = r.get("frac", 1.0)
        row["pnl_pct"] = r.get("pnl_pct", 0)
        out.append(row)
        index[key] = row
    return out


def closed_trades(keep=200):
    try:
        lines = TRADES_LOG.read_text().splitlines()[-2000:]
    except OSError:
        # must match the shape window() returns below, per period - returning
        # bare floats here is what used to throw in the browser and show
        # OFFLINE whenever trades.log was missing
        empty = {"pnl": 0.0, "w": 0, "l": 0}
        return [], {"d": dict(empty), "w": dict(empty), "m": dict(empty)}
    raw = []
    for ln in lines:
        try:
            raw.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    rows = merge_partials(raw)
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
    trades, runners = [], []
    closing = pending_closes()
    scanned = 0
    for sym, ast in state.items():
        if sym.startswith("_") or not isinstance(ast, dict):
            continue
        scanned += 1
        mid = mids.get(sym)
        tr = ast.get("trade")
        if tr and sym in closing:
            continue          # closed from here, waiting for the agent

        if tr:
            sign = 1 if tr["verdict"] == "LONG" else -1
            tp = tr.get("tp")
            risk = tr.get("risk0") or abs(tr["entry"] - tr["stop"]) or 1
            pnl = r_now = None
            if mid:
                pnl = sign * (mid - tr["entry"]) / tr["entry"] * 100
                r_now = sign * (mid - tr["entry"]) / risk
            # a trade that has booked its partial is a RUNNER: risk-free,
            # stop at entry, closing only when the HA flips. It gets its own
            # list so the open-trades panel stays "still at risk"
            (runners if tr.get("half") else trades).append(
                          {"sym": sym, "dir": tr["verdict"],
                           "half": bool(tr.get("half")),
                           "left": tr.get("left", 1.0),
                           "lev": ast.get("lev"), "risk": risk,
                           "entry": tr["entry"], "stop": tr["stop"],
                           "tp": tp, "mid": mid, "pnl": pnl, "r": r_now,
                           "rr": tr.get("rr"),
                           "opened_t": tr.get("opened_t", 0)})

    # fullest bar at the top. The open-trade bar is one scale - stop at 0%,
    # entry at 40%, target at 100% - so sorting by R puts the trade closest
    # to its target first and the one closest to its stop last. Rebuilt on
    # every SSE tick, so the order re-shuffles live as prices move.
    trades.sort(key=lambda t: (t["r"] is None,
                               -(t["r"] if t["r"] is not None else 0),
                               t["sym"]))
    # best-performing runner first - it is the one closest to being given
    # back if the HA turns
    runners.sort(key=lambda t: -(t["r"] if t["r"] is not None else -99))
    closed, pnl = closed_trades()
    return {"now": time.time(),
            "state_age_s": scan_age_s(state, mtime),
            # TradingView's interval code for whatever TF the agent runs, so
            # a card never opens a different timeframe from the one traded
            # render every timestamp in the AGENT's timezone, not the
            # browser's - otherwise a phone in another zone shows closed
            # trades at times that do not match the agent's own logs
            "tz": (state.get("_meta") or {}).get("tz", "America/Chicago"),
            "tv_interval": {"5m": "5", "15m": "15", "30m": "30",
                            "1h": "60", "4h": "240"}.get(
                (state.get("_meta") or {}).get("tf", "15m"), "15"),
            # the agent publishes its own pulse, so the staleness threshold
            # follows SCAN_EVERY instead of assuming the old 5m loop. Two
            # missed scans plus a minute of slack before anything is wrong.
            "stale_after_s": 2 * int((state.get("_meta") or {})
                                     .get("scan_every_s", 300)) + 60,
            "scanned": scanned, "trades": trades, "runners": runners,
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
.closebtn{background:transparent;border:0.5px solid #6e7681;color:#c9d1d9;
  font:11.5px inherit;padding:3px 10px;border-radius:10px;cursor:pointer}
.closebtn:hover:enabled{border-color:#f85149;color:#f85149}
.closebtn:disabled{opacity:.55;cursor:default}
.card.tv{cursor:pointer}
.card.tv:hover{border-color:#3d444d}
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
<div class="section shead" onclick="toggle('runners')"><span class=chev id=c-runners>\u25be</span>Runners<span class=cnt id=n-runners>0</span></div><div id=runners></div>
<div class="section shead" onclick="toggle('closed')"><span class=chev id=c-closed>\u25be</span>Closed trades<span class=cnt id=n-closed>0</span> <span id=csub class=muted style="float:right;text-transform:none;letter-spacing:0"></span></div><div id=closed></div>
<div class="section shead" onclick="toggle('events')"><span class=chev id=c-events>\u25be</span>Recent events<span class=cnt id=n-events>0</span></div><div id=events></div>
<script>
let TVINT='15';   // replaced from _meta.tf on the first poll
let TZ='America/Chicago';   // replaced from _meta.tz on the first poll
const TVLAYOUT=__TV_LAYOUT__;
// a saved layout carries its indicators; a bare /chart/ does not
const TVBASE='https://www.tradingview.com/chart/'+(TVLAYOUT?TVLAYOUT+'/':'')+'?symbol=';
// inline onclick handlers run in GLOBAL scope, so these must live at the top
// level - defined inside a render function they are invisible to the cards.
// Main-dex perps are HYPERLIQUID:<TICKER>USDC.P; builder-venue symbols
// (xyz:ARM, xyz:CL) are equities and commodities TradingView carries under
// their own tickers, so the bare name resolves better.
function tvSym(sym){
  if(sym.indexOf(':')>=0) return encodeURIComponent(sym.split(':').pop().toUpperCase());
  return encodeURIComponent('HYPERLIQUID:'+sym.toUpperCase()+'USDC.P');
}
// iPadOS and iOS have no popup windows - every browser there, Brave included,
// runs on WebKit. window.open with a feature string is treated as a popup and
// blocked, and a _blank retry is blocked too because the click gesture is
// already spent. A synthetic anchor click is ordinary link navigation, which
// is never blocked, and a named target still reuses a single tab.
const TOUCH = (navigator.maxTouchPoints || 0) > 1 ||
              !window.matchMedia('(hover: hover)').matches;

function closeRunner(ev, sym){
  ev.stopPropagation();          // the card itself opens TradingView
  // NO \n escapes here: PAGE is a non-raw triple-quoted Python string, so a
  // backslash-n in this file becomes a REAL newline in the served HTML and
  // breaks the JS string literal. Keep confirm text on one line.
  if(!confirm('Close '+sym+' at market NOW? This sends the order immediately '
      +'and cancels the resting stop and target.')) return;
  const b=ev.currentTarget; b.disabled=true; b.textContent='closing...';
  fetch('/close?sym='+encodeURIComponent(sym)+(KEY?'&key='+KEY:''),
        {method:'POST'})
    .then(r=>r.json())
    .then(j=>{ b.textContent = j.ok ? 'closed' : 'failed';
               if(!j.ok){ b.disabled=false; alert(j.msg||'close failed'); } })
    .catch(()=>{ b.textContent='failed'; b.disabled=false; });
}

function tvOpen(sym){
  const url = TVBASE + tvSym(sym) + '&interval=' + TVINT;
  if (TOUCH) {
    const a = document.createElement('a');
    a.href = url; a.target = 'tvchart'; a.rel = 'noopener';
    document.body.appendChild(a); a.click(); a.remove();
    return;
  }
  // desktop: a NAMED, sized window, so a second card replaces the chart in
  // the same popup instead of stacking tabs
  const w = Math.min(1400, Math.round(screen.availWidth * 0.72));
  const h = Math.min(900,  Math.round(screen.availHeight * 0.8));
  const l = Math.round((screen.availWidth - w) / 2);
  const t = Math.round((screen.availHeight - h) / 2);
  const win = window.open(url, 'tvchart',
    `popup=yes,width=${w},height=${h},left=${l},top=${t},` +
    'toolbar=no,menubar=no,location=no,status=no');
  if (win) { win.focus(); return; }
  const a = document.createElement('a');       // popup blocked - fall back to
  a.href = url; a.target = 'tvchart';          // plain link navigation
  document.body.appendChild(a); a.click(); a.remove();
}
const KEY=new URLSearchParams(location.search).get('key')||'';
let PERIOD='d', LAST=null;
let COLLAPSED={};
try{COLLAPSED=JSON.parse(localStorage.getItem('dashCollapsed')||'{}')}catch(e){}
function applyCollapse(){['trades','runners','closed','events'].forEach(id=>{
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
  if(d.tv_interval) TVINT=d.tv_interval;
  if(d.tz) TZ=d.tz;
  const limit=d.stale_after_s||480;
  const fresh=d.state_age_s!=null&&d.state_age_s<limit;
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
   // There is NO target freeze any more. Reaching the target never closes
   // the position under this engine - it books HA_PARTIAL and the rest
   // runs - so latching the card at the target price stopped the live
   // price and P&L updating for the whole life of the runner. Only the
   // STOP still fully closes a trade, and only before the partial: after
   // it the stop sits at entry, so hitting it is a breakeven close.
   if(window.__froz[fkey]==='tp') delete window.__froz[fkey];   // stale latch
   if(t.r!=null && t.r<=-1) window.__froz[fkey]='sl';
   const slDone=window.__froz[fkey]==='sl';
   const showR=slDone?-1:t.r;
   const showPnl=slDone?sgn*(t.stop-t.entry)/t.entry*100:t.pnl;
   // "target reached" is now computed LIVE, never latched
   // this panel only holds trades still at full risk - once the partial
   // books they move to the Runners list
   const atTarget=t.r!=null && t.r>=RRT;
   const badge=atTarget?'<span class="badge ok">target reached · booking half</span>'
     :slDone?'<span class="badge warn">stop hit · closing</span>':'';
   // one scale: stop = 0%, entry = 40%, TP = 100% - the fill IS closeness to TP
   const rp=showR==null?0:Math.max(0,Math.min(100,(showR+1)/(1+RRT)*100));
   const rc=showR==null?'#8b949e':showR>=0?'#3fb950':'#f85149';
   const rlbl=showR==null?''
     :slDone?'Stop traded - waiting for the close confirmation'
     :atTarget?`${showR.toFixed(2)}R \u00b7 target reached, booking half`
     :showR>=0
     ?`${showR.toFixed(2)}R · ${Math.round(Math.min(100,showR/RRT*100))}% of the way to TP`
     :`${showR.toFixed(2)}R · ${Math.round(Math.min(100,-showR*100))}% of the way to stop`;
   return `<div class="card tv" onclick="tvOpen('${t.sym}')" title="open ${t.sym} on TradingView">
    <div class=row><span class=sym>${t.sym} <span class=${cls}>${t.dir}</span>${t.lev?` <span class=lev>${t.lev}x</span>`:''} ${badge}</span>
    <span class="num ${showPnl>=0?'pnl-pos':'pnl-neg'}">${showPnl==null?'-':(showPnl>=0?'+':'')+showPnl.toFixed(2)+'%'}</span></div>
    <div class=row><span class=muted>entry <span class=num>$${px(t.entry)}</span></span>
    <span class=muted>${slDone?'exit':'now'} <span class=num>$${px(slDone?t.stop:t.mid)}</span></span></div>
    <div class=row><span class=muted>stop <span class=num>$${px(t.stop)}</span></span>
    <span class=muted>TP <span class=num>$${px(t.tp)}</span></span></div>
    <div class=bar><div class=fill style="width:${rp}%;background:${rc}"></div></div>
    <div class=muted>${rlbl}</div></div>`
  }).join(''):'<div class="card muted">none</div>';
  // RUNNERS: partial booked, stop at entry, riding until the HA flips.
  // Everything is measured from the ORIGINAL entry, and nothing here ever
  // freezes - these are live positions with no target left to hit.
  document.getElementById('runners').innerHTML=d.runners.length?d.runners.map(t=>{
   const cls=t.dir==='LONG'?'long':'short';
   const R=t.r==null?null:t.r;
   // the bar shows how far PAST the target the runner has travelled, so a
   // trade that has doubled its target reads full
   const rp=R==null?0:Math.max(0,Math.min(100,(R/(t.rr*2))*100));
   const peak=R!=null&&R>=t.rr*2;
   return `<div class="card tv" onclick="tvOpen('${t.sym}')" title="open ${t.sym} on TradingView">
    <div class=row><span class=sym>${t.sym} <span class=${cls}>${t.dir}</span>${t.lev?` <span class=lev>${t.lev}x</span>`:''} <span class="badge ok">${Math.round(t.left*100)}% running</span></span>
    <span class="num ${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl==null?'-':(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'%'}</span></div>
    <div class=row><span class=muted>entry <span class=num>$${px(t.entry)}</span></span>
    <span class=muted>now <span class=num>$${px(t.mid)}</span></span></div>
    <div class=row><span class=muted>stop <span class=num>$${px(t.stop)}</span> · risk-free</span>
    <span class=muted>booked at <span class=num>$${px(t.tp)}</span></span></div>
    <div class=bar><div class=fill style="width:${rp}%;background:${peak?'#3fb950':'#58a6ff'}"></div></div>
    <div class=row style="align-items:center">
      <span class=muted>${R==null?'':R.toFixed(2)+'R from entry'} · closes when the HA flips</span>
      <button class=closebtn onclick="closeRunner(event,'${t.sym}')">close now</button>
    </div></div>`
  }).join(''):'<div class="card muted">none</div>';
  document.getElementById('csub').textContent=LABEL[PERIOD];
  const cut=Date.now()-DAYS[PERIOD]*86400000;
  const shown=d.closed.filter(c=>c.t>=cut).slice(0,20);
  // a partial and its runner are merged server-side into ONE trade, so
  // these compound kinds appear in place of the raw ledger events
  const KINDS={TP_RUNNER:'target + runner', TP_BE:'target, runner to BE',
               TP_HALF:'target hit', RUNNER:'runner', BE:'breakeven',
               STOP:'stopped'};
  // outcome, not event type: anything closed in profit gets a checkmark.
  // BE keeps its own mark because breakeven is neither.
  const iconFor=c=>c.kind==='BE'?'➡️':(c.pnl_pct>=0?'✅':'❌');
  document.getElementById('closed').innerHTML=shown.length?shown.map(c=>{
   const cls=c.dir==='LONG'?'long':'short';
   const when=new Date(c.t).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:TZ});
   return `<div class="card tv" onclick="tvOpen('${c.sym}')" title="open ${c.sym} on TradingView"><div class=row>
    <span class=sym>${iconFor(c)} ${c.sym} <span class=${cls}>${c.dir}</span>
    <span class=muted style="font-weight:400">${KINDS[c.kind]||c.kind}</span>${c.frac&&c.frac<1?` <span class=muted style="font-size:10.5px">${Math.round(c.frac*100)}% closed</span>`:''}</span>
    <span class="num ${c.pnl_pct>=0?'pnl-pos':'pnl-neg'}">${(c.pnl_pct>=0?'+':'')+c.pnl_pct.toFixed(2)}%</span></div>
    <div class=row><span class=muted>$${px(c.entry)} → $${px(c.exit)}</span>
    <span class=muted>${when}</span></div></div>`
  }).join(''):'<div class="card muted">none in this period</div>';
  document.getElementById('events').innerHTML=
   d.events.map(e=>`<div class=event>${e.replace(/</g,'&lt;')}</div>`).join('')||'<div class="card muted">none</div>';
  document.getElementById('n-trades').textContent=d.trades.length;
  document.getElementById('n-runners').textContent=d.runners.length;
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
        # iOS WebKit (Safari, and Brave/Chrome which are WebKit underneath)
        # will happily re-serve a cached document on no-store alone, which
        # meant a deploy that fixed broken page JS still showed the broken
        # page. Belt and braces: the full no-cache set, an explicitly stale
        # Expires, and an ETag that changes on every deploy so a conditional
        # request can never be answered from cache.
        self.send_header("Cache-Control",
                         "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("ETag", f'"{BUILD}"')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()          # same routing and the same key check

    def do_GET(self):
        url = urlparse(self.path)
        if DASH_KEY:
            key = (parse_qs(url.query).get("key") or [""])[0]
            if key != DASH_KEY:
                self._send(403, b"forbidden", "text/plain")
                return
        if url.path == "/close":
            sym = (parse_qs(url.query).get("sym") or [""])[0]
            ok, msg = request_close(sym) if sym else (False, "no symbol")
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok, "msg": msg}).encode(),
                       "application/json")
        elif url.path == "/data":
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
                    time.sleep(SSE_TICK_S)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        elif url.path == "/":
            page = PAGE.replace("__TV_LAYOUT__", json.dumps(TV_LAYOUT))
            self._send(200, page.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Dashboard on port {PORT}" + (" (key required)" if DASH_KEY else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
