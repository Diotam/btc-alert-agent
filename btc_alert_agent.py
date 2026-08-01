#!/usr/bin/env python3
"""
SMOOTHED HEIKIN ASHI AGENT (15m)
--------------------------------
One strategy, long side described; shorts mirror it exactly.

  1. TREND - a run of red HA candles with real momentum in it: at least
     HA_TREND_RUN consecutive bodies expanding somewhere in the run, and the
     biggest body at least HA_MIN_BODY_PCT of price so a flat series cannot
     qualify.
  2. DOJI - an HA body no more than HA_DOJI_FRACTION of the biggest body in
     that run. The smoothed HA has stalled, and that stall IS the turn.
  3. ENTER on the doji, in the direction opposite the trend that led into
     it. Price does NOT have to come back and retest anything.

  stop   = the doji's own level - its low for a long, its high for a short
  target = HA_RR x that distance. HA_PARTIAL of the position is booked there
           and the stop moves to entry; the remainder is held until the
           smoothed HA flips back against the trade.

The HA series is a derived band: EMA the OHLC, build Heikin Ashi on that,
then EMA the result. Its highs and lows are averages and need never have
printed, so the stop is a computed level rather than a price that traded.

Config comes from environment variables:
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / HL_API_KEY / HL_ACCOUNT_ADDRESS

Modes:
  python3 btc_alert_agent.py           single scan
  python3 btc_alert_agent.py --test    send a test message
  python3 btc_alert_agent.py --loop    run continuously
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================= CONFIG ======================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- asset universe -------------------------------------------------------
DISCOVER_ALL = True
DISCOVER_DEXES = True              # scan HIP-3 builder venues. Their symbols
                                   # are absent from the main perp meta the SDK
                                   # client is built against, so they ALERT
                                   # ONLY - they are placed by hand
ADMIT_COMMODITIES = True
ADMIT_STOCKS = False               # equities out of the universe. The
                                   # is one line to flip back
DEXES = [""]                       # fallback when dex discovery fails
COMMODITY_TICKERS = ("XAU", "GOLD", "XAG", "SILVER", "XPT", "PLAT",
                     "XPD", "PALLAD", "CL", "OIL", "WTI", "BRENT",
                     "NG", "NATGAS", "HG", "COPPER")
STOCK_DEXES = ("xyz",)             # TradeXYZ equities venue
MIN_DAY_VOLUME_USD = 2_000_000     # crypto floor, 24h notional
COMMODITY_MIN_VOLUME_USD = 5_000_000
STOCK_MIN_VOLUME_USD = 15_000_000
ONLY = []                          # trade ONLY these symbols ([] = whole universe)
EXCLUDE = ["PUMP"]                 # never trade these (matches the base name
                                   # on any venue)
MAX_ASSETS = 70

ASSETS = [                         # used when DISCOVER_ALL = False, or when
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",   # discovery fails
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- strategy dials -------------------------------------------------------
TF = "15m"                         # the spec is 15m closes
SCAN_EVERY = "5m"                  # how often the loop wakes. Aligning it to
                                   # TF means one scan per candle. A shorter
                                   # pulse costs API calls but reacts sooner:
                                   # symbols with no open trade are skipped
                                   # until their candle closes either way, so
                                   # the extra scans only ever serve OPEN
                                   # trades - intrabar stop/target detection
                                   # and moving the stop to entry after the
                                   # partial fills
HA_SMOOTH_IN = 6                   # EMA applied to OHLC before building HA
HA_SMOOTH_OUT = 3                  # EMA applied to the HA output
HA_TREND_RUN = 3                   # bodies that must expand, then shrink
HA_MIN_BODY_PCT = 0.05             # the trend run must contain at least one
                                   # HA body this big, as a % of price. Without
                                   # it a FLAT smoothed series satisfies
                                   # "strictly growing then strictly shrinking"
                                   # on noise in the fifth decimal and arms a
                                   # setup with no visible colour flip at all.
                                   # Measured: near-flat series produce bodies
                                   # of 0.005-0.034%, normal ones 0.081%+
HA_DOJI_FRACTION = 0.25            # a DOJI is an HA body this small relative
                                   # to the biggest body in the trend run that
                                   # led into it. Scale-free, so it adapts per
                                   # symbol instead of needing a fixed price
                                   # threshold. Entry happens ON the doji -
                                   # price does NOT have to come back and
                                   # retest anything
HA_RR = 2.0                        # first target = 2x the stop distance
HA_PARTIAL = 0.5                   # fraction booked there; the stop then moves
                                   # to entry and the remainder is held until
                                   # the HA flips against the trade
MIN_STOP_PCT = 0.25                # skip entries whose stop sits closer than
                                   # this % of price - sub-noise stops just churn

# --- alerts ---------------------------------------------------------------
ALERT_ENTRIES = True
ALERT_LIFECYCLE = True             # target, runner, stop and breakeven alerts

# --- execution ------------------------------------------------------------
EXEC_LIVE = True                   # place real orders
EXEC_LOG_ORDERS = True             # write every sized order to orders.log.
                                   # This is an audit trail only - it has never
                                   # gated execution. EXEC_LIVE alone decides
                                   # whether real orders are sent
EXEC_TESTNET = False               # False = MAINNET, real money
EXEC_HALT_FILE = "/opt/btc-agent/EXEC_HALT"   # touch this to stop new entries
EXEC_RISK_USD = 2.0                # fixed dollar risk per trade
EXEC_MAX_NOTIONAL_USD = 2500       # cap on position value
EXEC_MAX_POSITIONS = 3             # concurrent live positions
EXEC_DAILY_LOSS_LIMIT_USD = 40.0   # INERT: needs realised USD from the ledger,
                                   # which is not tracked yet

# --- plumbing -------------------------------------------------------------
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
TRADES_LOG = Path(__file__).parent / "trades.log"
ORDERS_LOG = Path(__file__).parent / "orders.log"
TIMEZONE = "America/Chicago"
LOCAL_TZ = ZoneInfo(TIMEZONE)

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
      "4h": 14_400_000}
_TF_ALIASES = {"5min": "5m", "15min": "15m", "30min": "30m",
               "60m": "1h", "60min": "1h", "1hr": "1h"}
TF = _TF_ALIASES.get(TF.strip().lower(), TF.strip().lower())
SCAN_EVERY = _TF_ALIASES.get(SCAN_EVERY.strip().lower(),
                             SCAN_EVERY.strip().lower())
for _n, _v in (("TF", TF), ("SCAN_EVERY", SCAN_EVERY)):
    if _v not in MS:
        raise SystemExit(f"CONFIG ERROR: {_n}={_v!r} is not a known "
                         f"timeframe - use one of {sorted(MS)}")

# the fetcher ignores the caller's count and uses this map - a value that is
# too small silently starves whatever depends on that interval
LOOKBACK = {"5m": 300, "15m": 400, "30m": 400, "1h": 500, "4h": 300}

REQUEST_TIMEOUT_S = 8              # fail fast: a throttled API must not burn 20s
FETCH_DELAY_S = 0.12
RETRY_ON_429 = 2                   # retries when the API throttles or 5xxs
RETRY_BACKOFF_S = 0.8              # doubles each attempt
DISCOVERY_TTL_S = 1800             # the tradable universe barely moves
                                   # intraday, so re-listing venues and
                                   # markets every 5 min is pure API burn
RUN_BUDGET_S = 480                 # hard per-run budget; the rest resume next
                                   # run via a rotating cursor
REPLAY_CANDLES = 3                 # candles replayed per run (covers a run gap)


# --------------------------- small helpers ---------------------------------
def fmt_ts(ms, fmt="%Y-%m-%d %I:%M %p %Z"):
    return datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime(fmt)


def log(msg):
    ts = datetime.now(LOCAL_TZ)
    print(f"[{ts.strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def fmt_px(p):
    return f"{p:,.0f}" if p >= 10000 else f"{p:,.2f}" if p >= 1 else f"{p:,.6f}"


def pnl_pct(trade, exit_px):
    sign = 1 if trade["verdict"] == "LONG" else -1
    return sign * (exit_px - trade["entry"]) / trade["entry"] * 100


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def now_ms():
    return int(time.time() * 1000)


# --------------------------- run summary -----------------------------------
RUN_ALERTS = []
RUN_STATUS = []
RUN_UNIVERSE = [0]                 # [universe size] for the run summary


def write_run_summary():
    n = len(RUN_STATUS)
    armed = [s for s in RUN_STATUS if "BROKE-" in s]
    open_t = sum(1 for s in RUN_STATUS if "IN_TRADE" in s)
    if RUN_ALERTS:
        headline = "ALERT SENT: " + " | ".join(RUN_ALERTS)
    else:
        extras = []
        if armed:
            extras.append("armed: " + "; ".join(armed)[:120])
        if open_t:
            extras.append(f"{open_t} in trade")
        headline = (f"No signal - {n} of {RUN_UNIVERSE[0] or n} markets scanned"
                    + (f" ({', '.join(extras)})" if extras else ""))
    log("SUMMARY: " + headline)
    try:
        (Path(__file__).parent / "run_summary.txt").write_text(headline + "\n")
    except OSError:
        pass


# --------------------------- data sources ----------------------------------
def http_json(url, payload=None, timeout=None, retries=RETRY_ON_429):
    """One HTTP call, retrying on throttling. A 429 that is simply swallowed
    is the worst outcome: the caller then behaves as if the data does not
    exist rather than as if it could not be reached."""
    timeout = timeout or REQUEST_TIMEOUT_S
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (ha-agent/1.0)"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries:
                wait = RETRY_BACKOFF_S * (2 ** attempt)
                log(f"HTTP {e.code} - backing off {wait:.1f}s and retrying")
                time.sleep(wait)
                continue
            raise


def fetch_hyperliquid(coin, interval, lookback):
    end = now_ms()
    start = end - lookback * MS[interval]
    data = http_json("https://api.hyperliquid.xyz/info", {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start, "endTime": end},
    })
    return [{"t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])}
            for c in data]


def fetch_binance(sym, interval, lookback):
    data = http_json(f"https://api.binance.com/api/v3/klines"
                     f"?symbol={sym}&interval={interval}&limit={lookback}")
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in data]


def fetch_kraken(pair, interval, lookback):
    mins = MS[interval] // 60000
    data = http_json(f"https://api.kraken.com/0/public/OHLC"
                     f"?pair={pair}&interval={mins}")
    key = next(k for k in data["result"] if k != "last")
    return [{"t": k[0] * 1000, "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[6])}
            for k in data["result"][key]]


def fetch_fallback(spec, interval, lookback):
    provider, _, ident = spec.partition(":")
    return {"binance": fetch_binance,
            "kraken": fetch_kraken}[provider](ident, interval, lookback)


def fetch(asset, interval, min_candles):
    lookback = LOOKBACK.get(interval, 400)
    sources = [(f"HL {asset['hl_coin']}",
                lambda: fetch_hyperliquid(asset["hl_coin"], interval, lookback))]
    for spec in asset.get("fallbacks", []):
        sources.append((spec,
                        lambda s=spec: fetch_fallback(s, interval, lookback)))
    for name, fn in sources:
        try:
            candles = fn()
            if candles and len(candles) >= min_candles:
                return name, candles
        except Exception as e:
            log(f"{asset['symbol']}: {name} {interval} failed: {e}")
    return None, None


# --------------------------- universe --------------------------------------
def base_name(name):
    """Strip any venue prefix Hyperliquid includes ('xyz:GOLD') and
    kGOLD-style multipliers, leaving the bare ticker."""
    return name.split(":")[-1].upper().lstrip("K")


def executable(symbol):
    """False for builder-venue markets. They are alert-only: the SDK client is
    built against the main perp meta, which does not contain them. Nothing
    that cannot execute may consume the live position budget."""
    return ":" not in symbol


def is_commodity(name):
    base = base_name(name)
    return any(base.startswith(t) for t in COMMODITY_TICKERS)


def list_dexes():
    """The main dex plus every HIP-3 builder dex."""
    if not DISCOVER_DEXES:
        return DEXES
    try:
        data = http_json("https://api.hyperliquid.xyz/info",
                         {"type": "perpDexs"})
        dexes = []
        for d in data:
            if d is None:
                dexes.append("")                  # the main dex slot
            elif isinstance(d, str):
                dexes.append(d)
            elif isinstance(d, dict) and d.get("name"):
                dexes.append(d["name"])
        if dexes:
            if "" not in dexes:
                dexes.insert(0, "")
            return dexes
    except Exception as e:
        log(f"Dex discovery failed ({e}) - using configured DEXES list")
    return DEXES


def discover_assets():
    found = []
    dexes = list_dexes()
    if len(dexes) > 2:
        log(f"Scanning {len(dexes)} dexes: "
            + ", ".join(d or "main" for d in dexes))
    excluded = {base_name(x) for x in EXCLUDE}
    for dex in dexes:
        payload = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        try:
            meta, ctxs = http_json("https://api.hyperliquid.xyz/info", payload)
        except Exception as e:
            log(f"Discovery failed for dex '{dex or 'main'}': {e}")
            continue
        for u, ctx in zip(meta.get("universe", []), ctxs):
            if u.get("isDelisted"):
                continue
            try:
                vol = float(ctx.get("dayNtlVlm") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            name = u["name"]
            if base_name(name) in excluded:
                continue
            if dex:
                if is_commodity(name):
                    if not ADMIT_COMMODITIES or vol < COMMODITY_MIN_VOLUME_USD:
                        continue
                    cls = "commodity"
                elif dex in STOCK_DEXES:
                    if not ADMIT_STOCKS or vol < STOCK_MIN_VOLUME_USD:
                        continue
                    cls = "stock"
                else:
                    continue                      # unknown venue class: skip
            else:
                if vol < MIN_DAY_VOLUME_USD:
                    continue
                cls = "crypto"
            coin = name if (":" in name or not dex) else f"{dex}:{name}"
            found.append({"symbol": coin, "hl_coin": coin, "vol": vol,
                          "cls": cls, "lev": u.get("maxLeverage"),
                          "label": f"{base_name(name)}-PERP"
                                   + (f" ({dex})" if dex else ""),
                          "fallbacks": []})
    found.sort(key=lambda a: a["vol"], reverse=True)
    return found[:MAX_ASSETS]


def _not_excluded(a):
    return base_name(a["symbol"]) not in {base_name(x) for x in EXCLUDE}


_UNIVERSE = {"t": 0.0, "assets": []}


def active_assets():
    if ONLY:
        picked = [a for a in ASSETS if a["symbol"] in ONLY] or ASSETS[:1]
        return [a for a in picked if _not_excluded(a)]
    if not DISCOVER_ALL:
        return [a for a in ASSETS if _not_excluded(a)]
    if _UNIVERSE["assets"] and \
            time.time() - _UNIVERSE["t"] < DISCOVERY_TTL_S:
        return _UNIVERSE["assets"]
    assets = discover_assets()
    if assets:
        _UNIVERSE.update(t=time.time(), assets=assets)
        auto = sum(1 for a in assets if executable(a["symbol"]))
        n_com = sum(1 for a in assets if a.get("cls") == "commodity")
        n_stk = sum(1 for a in assets if a.get("cls") == "stock")
        log(f"Discovered {len(assets)} markets: {auto} main-dex crypto "
            f"(auto-traded), {n_com} commodities + {n_stk} equities "
            f"(alert only, placed by hand)")
        return assets
    log("Discovery returned nothing - falling back to the manual ASSETS list.")
    return ASSETS


# --------------------------- smoothed heikin ashi --------------------------
def ema(vals, n):
    """Plain EMA, seeded on the first value so the series has no None gap."""
    k = 2.0 / (n + 1)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def smoothed_ha(candles, n_in=None, n_out=None):
    """EMA the OHLC, build Heikin Ashi on that, then EMA the result. The
    output is a derived band, NOT tradeable prices - its highs and lows are
    averages and may never have printed."""
    n_in, n_out = n_in or HA_SMOOTH_IN, n_out or HA_SMOOTH_OUT
    if not candles:
        return []
    o = ema([c["o"] for c in candles], n_in)
    h = ema([c["h"] for c in candles], n_in)
    lo = ema([c["l"] for c in candles], n_in)
    cl = ema([c["c"] for c in candles], n_in)
    ha, p_o, p_c = [], None, None
    for i in range(len(candles)):
        close = (o[i] + h[i] + lo[i] + cl[i]) / 4
        open_ = close if p_o is None else (p_o + p_c) / 2
        ha.append({"o": open_, "c": close})
        p_o, p_c = open_, close
    so = ema([x["o"] for x in ha], n_out)
    sc = ema([x["c"] for x in ha], n_out)
    sh = ema(h, n_out)
    sl = ema(lo, n_out)
    return [{"t": candles[i]["t"], "o": so[i], "c": sc[i],
             "h": max(sh[i], so[i], sc[i]), "l": min(sl[i], so[i], sc[i])}
            for i in range(len(candles))]


def ha_green(x):
    return x["c"] > x["o"]


def ha_body(x):
    return abs(x["c"] - x["o"])


def _strictly(bodies, growing):
    return all((bodies[k] > bodies[k - 1]) if growing else
               (bodies[k] < bodies[k - 1]) for k in range(1, len(bodies)))


def ha_doji(ha, i, want_long):
    """Is HA candle i a DOJI that turns a real trend?

    A doji is a body small relative to the trend that produced it - the
    smoothed HA stalling. That stall IS the signal: the trade is taken on
    this candle, in the direction OPPOSITE the trend that led in. Price does
    not have to come back and retest anything.

    Returns (doji_index, run_start) or None.
    """
    n = HA_TREND_RUN
    # the run of trend-coloured candles ending at i. want_long means the
    # trend into the doji was RED, so the doji turns us long.
    if ha_green(ha[i]) == want_long and ha_body(ha[i]) > 0:
        pass                                 # the doji may already have
        #                                      flipped colour - both are fine
    r = i - 1
    while r >= 0 and ha_green(ha[r]) != want_long:
        r -= 1
    r += 1
    run = ha[r:i]                            # the trend, excluding the doji
    if len(run) < n:
        return None
    bodies = [ha_body(x) for x in run]
    biggest = max(bodies)
    scale = abs(ha[i]["c"]) or 1.0

    # the trend has to be VISIBLE. A flat smoothed series is nothing BUT
    # dojis, so without this every quiet symbol would trade continuously.
    if HA_MIN_BODY_PCT and biggest < HA_MIN_BODY_PCT / 100.0 * scale:
        return None
    # ...and it has to have had momentum at some point
    if not any(_strictly(bodies[k:k + n], True)
               for k in range(0, len(bodies) - n + 1)):
        return None
    # the doji itself: small against what came before it
    if ha_body(ha[i]) > HA_DOJI_FRACTION * biggest:
        return None
    return i, r


# --------------------------- telegram --------------------------------------
def send_telegram(text):
    resp = http_json(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True})
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram send failed: {resp.get('description')}")


# --------------------------- messages --------------------------------------
def entry_message(asset, direction, plan, zhi, zlo, source, t, trigger):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    return "\n".join([
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 smoothed HA \u00b7 "
        f"{esc(fmt_ts(t))}</i>",
        "",
        "\U0001F4CA <b>Setup</b>: "
        + (f"HA zone ${fmt_px(zlo)} - ${fmt_px(zhi)}; " if zhi else "")
        + f"{esc(trigger)}",
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>  (HA zone low)",
        f"TP:    <code>${fmt_px(plan['tp'])}</code>  "
        f"({HA_RR}x the stop \u00b7 {HA_PARTIAL:.0%} booked there)",
        f"<i>data: {esc(source)}</i>",
    ])


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    emoji, title, sub = {
        "TP_HALF": ("\U0001F3AF", "TARGET HIT",
                    f"{HA_RR}R reached \u00b7 {HA_PARTIAL:.0%} booked, "
                    "stop moved to entry"),
        "RUNNER": ("\u2705", "RUNNER CLOSED",
                   "smoothed HA flipped against the trade"),
        "BE": ("\u27a1\ufe0f", "STOPPED AT ENTRY",
               "the runner came back to breakeven"),
        "STOP": ("\u274c", "STOPPED OUT", "stop level hit"),
        "TP": ("\u2705", "TAKE PROFIT HIT", "target reached"),
    }[kind]
    pnl = pnl_pct(trade, exit_px)
    return "\n".join([
        f"{emoji} <b>{title} \u00b7 {esc(asset['symbol'])} "
        f"{trade['verdict']}</b>  <code>{pnl:+.2f}%</code>",
        f"{sub} at ${fmt_px(exit_px)} (entry ${fmt_px(trade['entry'])})",
        esc(note) if note else "",
        f"<i>{esc(asset['label'])} \u00b7 {esc(fmt_ts(event_t))}</i>",
    ])


# --------------------------- trade ledger ----------------------------------
def already_closed(sym, trade, exit_px, kind):
    """True if an identical close is already in the ledger - makes duplicate
    close alerts structurally impossible."""
    try:
        lines = TRADES_LOG.read_text().splitlines()[-200:]
    except OSError:
        return False
    for ln in lines:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if (r.get("sym") == sym and r.get("dir") == trade["verdict"]
                and r.get("kind") == kind
                and abs(r.get("entry", 0) - trade["entry"]) < 1e-12
                and abs(r.get("exit", 0) - exit_px) < 1e-12):
            return True
    return False


def record_close(sym, trade, exit_px, kind, t_event=None, frac=1.0):
    """Append a closed trade to the ledger. t_event = the actual market time
    of the exit, so late reconciliations book to the day they happened."""
    try:
        with open(TRADES_LOG, "a") as f:
            f.write(json.dumps({
                "t": int(t_event or now_ms()), "sym": sym,
                "dir": trade["verdict"], "entry": trade["entry"],
                "exit": exit_px, "kind": kind, "frac": frac,
                "pnl_pct": round(pnl_pct(trade, exit_px) * frac, 3)}) + "\n")
    except OSError:
        pass


def log_order(rec):
    try:
        with open(ORDERS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log(f"orders.log write failed: {type(e).__name__}")


# --------------------------- state -----------------------------------------
def blank_asset_state():
    return {"phase": "SCAN", "last_candle_t": 0, "setup": None,
            "traded": None, "trade": None}


def load_state():
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


STATE_VIEW = {}


# --------------------------- open-trade management -------------------------
def _close_trade(asset, trade, px, kind, event_t, note="", frac=None):
    """Alert, log and book a close of `frac` of the position."""
    sym = asset["symbol"]
    frac = trade.get("left", 1.0) if frac is None else frac
    if already_closed(sym, trade, px, kind):
        log(f"{sym}: duplicate {kind} close suppressed")
        return None, True
    if ALERT_LIFECYCLE:
        try:
            send_telegram(lifecycle_message(asset, kind, trade, px,
                                            event_t, note))
        except Exception as e:
            log(f"{sym}: {kind} alert failed: {type(e).__name__}: {e}")
    log(f"{sym}: {kind} at ${fmt_px(px)}{' (intrabar)' if note else ''}")
    record_close(sym, trade, px, kind, event_t, frac=frac)
    plan_manage_orders(asset, kind, px)
    RUN_ALERTS.append(f"{sym} {kind} ({pnl_pct(trade, px) * frac:+.2f}%)")
    return None, True


def _book_partial(asset, trade, px, event_t):
    """HA_PARTIAL comes off at the target, the stop moves to entry, and the
    remainder runs until the HA flips. Booked as its own ledger row with
    frac set, so partial P&L stays partial."""
    sym = asset["symbol"]
    record_close(sym, trade, px, "TP_HALF", event_t, frac=HA_PARTIAL)
    trade["half"] = True
    trade["left"] = round(1.0 - HA_PARTIAL, 6)
    trade["stop"] = trade["entry"]
    if ALERT_LIFECYCLE:
        try:
            send_telegram(lifecycle_message(asset, "TP_HALF", trade, px,
                                            event_t, ""))
        except Exception as e:
            log(f"{sym}: TP_HALF alert failed: {type(e).__name__}")
    log(f"{sym}: target hit at ${fmt_px(px)} - {HA_PARTIAL:.0%} booked, "
        f"stop moved to entry ${fmt_px(trade['entry'])}, "
        f"{trade['left']:.0%} running")
    RUN_ALERTS.append(f"{sym} target hit, {HA_PARTIAL:.0%} booked")
    move_stop_live(asset, trade)


def process_open_trade(asset, trade, candles, ha, last_closed_t):
    """Stop / target watch. Before the partial the stop is the HA zone low;
    after it the stop is entry and the exit trigger is an HA flip against
    the trade. The stop is always checked first within a candle."""
    long = trade["verdict"] == "LONG"
    changed = False
    by_t = {h["t"]: h for h in ha}
    for c in candles:
        if c["t"] <= trade["checked_t"] or c["t"] > last_closed_t:
            continue
        changed = True
        trade["checked_t"] = c["t"]
        event_t = c["t"] + MS[TF]
        if (c["l"] <= trade["stop"]) if long else (c["h"] >= trade["stop"]):
            kind = "BE" if trade.get("half") else "STOP"
            return _close_trade(asset, trade, trade["stop"], kind, event_t)
        if not trade.get("half"):
            if (c["h"] >= trade["tp"]) if long else (c["l"] <= trade["tp"]):
                _book_partial(asset, trade, trade["tp"], event_t)
            continue
        h = by_t.get(c["t"])
        if h and ha_green(h) != long:
            return _close_trade(asset, trade, c["c"], "RUNNER", event_t,
                                "smoothed HA flipped against the trade")

    # ---- intrabar on the LIVE candle -------------------------------------
    # a fast move can blow through the stop mid-candle. checked_t is NOT
    # advanced here, and this block computes its own timestamp: the loop
    # above may not have run this pulse, so its variables must never be
    # referenced from here.
    live = candles[-1]
    if live["t"] > last_closed_t:
        t_now = now_ms()
        if (live["l"] <= trade["stop"]) if long else (live["h"] >= trade["stop"]):
            kind = "BE" if trade.get("half") else "STOP"
            return _close_trade(asset, trade, trade["stop"], kind, t_now,
                                "Intrabar - stop traded before the close.")
        if not trade.get("half"):
            if (live["h"] >= trade["tp"]) if long else (live["l"] <= trade["tp"]):
                _book_partial(asset, trade, trade["tp"], t_now)
    return trade, changed


# --------------------------- execution -------------------------------------
_EXEC = {"ex": None, "info": None, "addr": None, "meta": None, "err": None}


def exec_base_url():
    return ("https://api.hyperliquid-testnet.xyz" if EXEC_TESTNET
            else "https://api.hyperliquid.xyz")


def exec_client():
    """Lazily build the SDK client. Returns None (with a logged reason) if
    the SDK, the key or the network is unavailable - never raises."""
    if _EXEC["ex"] or _EXEC["err"]:
        return _EXEC["ex"]
    key = os.environ.get("HL_API_KEY", "").strip()
    if not key:
        _EXEC["err"] = "HL_API_KEY not set"
    else:
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            wallet = Account.from_key(key)
            addr = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip() \
                or wallet.address
            base = exec_base_url()
            _EXEC.update(ex=Exchange(wallet, base, account_address=addr),
                         info=Info(base, skip_ws=True), addr=addr)
            _EXEC["meta"] = _EXEC["info"].meta()
            log(f"execution client ready on "
                f"{'TESTNET' if EXEC_TESTNET else 'MAINNET'} for {addr[:10]}...")
        except Exception as e:
            _EXEC["err"] = f"{type(e).__name__}: {e}"
    if _EXEC["err"]:
        log(f"execution client unavailable ({_EXEC['err']}) - "
            "alerts and order logging only, nothing will be sent")
    return _EXEC["ex"]


def sz_decimals(sym):
    """Hyperliquid rejects sizes carrying too many decimals."""
    meta = _EXEC.get("meta") or {}
    for a in meta.get("universe", []):
        if a.get("name") in (base_name(sym), sym):
            return int(a.get("szDecimals", 4))
    return 4


def round_px(px):
    """Perp prices: max 5 significant figures."""
    if px <= 0:
        return px
    from decimal import Decimal
    d = Decimal(repr(float(px)))
    return float(round(d, -d.adjusted() + 4))


def exec_blocked(open_count, day_pnl_usd):
    """Reasons a live entry must not be sent."""
    if os.path.exists(EXEC_HALT_FILE):
        return "EXEC_HALT file present"
    if open_count >= EXEC_MAX_POSITIONS:
        return f"{open_count} positions already open (max {EXEC_MAX_POSITIONS})"
    if day_pnl_usd <= -abs(EXEC_DAILY_LOSS_LIMIT_USD):
        return f"daily loss limit hit ({day_pnl_usd:+.2f})"
    return None


def plan_entry_orders(asset, trade):
    """Size the trade by fixed dollar risk and describe the orders. Logged
    to orders.log. Returns the plan, or None."""
    sym = asset["symbol"]
    entry, stop, tp = trade["entry"], trade["stop"], trade["tp"]
    long_ = trade["verdict"] == "LONG"
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return None
    size = EXEC_RISK_USD / per_unit
    notional = size * entry
    capped = ""
    if notional > EXEC_MAX_NOTIONAL_USD:
        size = EXEC_MAX_NOTIONAL_USD / entry
        notional = EXEC_MAX_NOTIONAL_USD
        capped = f" [notional capped, risk now ${size * per_unit:.2f}]"
    rec = {"t": now_ms(), "sym": sym,
           "mode": "live" if EXEC_LIVE else "sim",
           "event": "ENTRY", "side": "buy" if long_ else "sell",
           "size": round(size, 8), "entry": entry, "stop": stop, "tp": tp,
           "notional_usd": round(notional, 2),
           "risk_usd": round(size * per_unit, 2),
           "stop_pct": round(per_unit / entry * 100, 3),
           "orders": [
               {"kind": "entry", "type": "market-IOC",
                "side": "buy" if long_ else "sell", "size": round(size, 8)},
               {"kind": "stop", "type": "stop-market", "reduce_only": True,
                "trigger": stop, "size": round(size, 8)},
               {"kind": "tp", "type": "limit", "reduce_only": True,
                "price": tp, "size": round(size, 8)}]}
    if EXEC_LOG_ORDERS:
        log_order(rec)
        log(f"{sym}: SIZED - {rec['side']} {size:.6g} @ ${fmt_px(entry)} = "
            f"${notional:,.0f} notional, ${rec['risk_usd']:.2f} risk "
            f"({rec['stop_pct']}% stop); stop ${fmt_px(stop)}, "
            f"TP ${fmt_px(tp)}{capped}")
    return rec


def plan_manage_orders(asset, event, price):
    if not EXEC_LOG_ORDERS:
        return
    action = {"STOP": "stop filled - flat",
              "TP": "target filled - flat",
              "TP_HALF": f"target filled - {HA_PARTIAL:.0%} booked, "
                         "stop to entry",
              "RUNNER": "HA flipped - runner closed at market",
              "BE": "runner stopped at entry - flat",
              }.get(event)
    if not action:
        return
    log_order({"t": now_ms(), "sym": asset["symbol"],
               "mode": "live" if EXEC_LIVE else "sim",
               "event": event, "price": price, "action": action})


def rebase_to_fill(sym, trade, fill, long_):
    """Re-derive the trade from the price actually paid. The stop is a
    structural level - the HA zone low - so it does NOT move; the
    target does, because 2R has to be measured from the real entry. Without
    this, market-IOC slippage silently changes the risk:reward: a fill worse
    than the candle close sits closer to the stop and further from the
    target than the plan assumed."""
    if not fill:
        return
    risk = (trade["stop"] - fill) if not long_ else (fill - trade["stop"])
    if risk <= 0:
        log(f"{sym}: filled at ${fmt_px(fill)}, through its own stop "
            f"${fmt_px(trade['stop'])} - protective orders will close it")
        return
    slip = (fill - trade["entry"]) / trade["entry"] * 100
    trade["entry"], trade["risk0"] = fill, risk
    trade["tp"] = fill + HA_RR * risk if long_ else fill - HA_RR * risk
    stop_pct = risk / fill * 100
    log(f"{sym}: filled ${fmt_px(fill)} ({slip:+.3f}% vs plan) - TP re-based "
        f"to ${fmt_px(trade['tp'])}, real stop {stop_pct:.3f}%")
    if MIN_STOP_PCT and stop_pct < MIN_STOP_PCT:
        log(f"{sym}: WARNING slippage left the stop {stop_pct:.3f}% away, "
            f"under the {MIN_STOP_PCT}% floor")


def fill_size(resp):
    """How much of a market order ACTUALLY filled. A slippage-bounded IOC on
    a thin book fills partially, and every protective order has to be sized
    against the real position, not the requested one."""
    try:
        return float(resp["response"]["data"]["statuses"][0]["filled"]["totalSz"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def order_oid(resp):
    """The exchange id of a resting order, so it can be cancelled later."""
    try:
        st = resp["response"]["data"]["statuses"][0]
        return st.get("resting", {}).get("oid")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def move_stop_live(asset, trade):
    """After the partial the stop belongs at entry. That means cancelling the
    resting stop and replacing it for what is left - without this the
    exchange still holds a stop at the HA zone low while the bot believes
    the runner is risk-free."""
    if not EXEC_LIVE or not executable(asset["symbol"]) or not trade.get("size"):
        return
    ex = exec_client()
    if not ex:
        return
    sym = base_name(asset["symbol"])
    long_ = trade["verdict"] == "LONG"
    left = round(trade["size"] * trade["left"], sz_decimals(asset["symbol"]))
    try:
        if trade.get("stop_oid"):
            ex.cancel(sym, trade["stop_oid"])
        r = ex.order(sym, not long_, left, round_px(trade["entry"]),
                     {"trigger": {"triggerPx": round_px(trade["entry"]),
                                  "isMarket": True, "tpsl": "sl"}},
                     reduce_only=True)
        trade["stop_oid"] = order_oid(r)
        log(f"{sym}: LIVE stop moved to entry ${fmt_px(trade['entry'])} "
            f"for the remaining {left}")
    except Exception as e:
        log(f"{sym}: could NOT move the live stop ({type(e).__name__}: {e}) - "
            "the exchange stop is still at its original level")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} partial booked but the "
                          "stop could not be moved to entry - check manually")
        except Exception:
            pass


def fill_price(resp):
    """The average price actually paid, from a market_open response."""
    try:
        return float(resp["response"]["data"]["statuses"][0]["filled"]["avgPx"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def place_entry_live(asset, trade, plan):
    """Entry, then the protective stop, then the TP. If the protective
    orders cannot be placed the position is closed immediately - never sit
    unprotected."""
    if not EXEC_LIVE:
        return None
    ex = exec_client()
    if not ex:
        return None
    # builder-venue markets (xyz:NBIS, ...) are not in the main perp meta the
    # SDK client was built against - it raises KeyError on the asset lookup.
    # Alert on them, but never try to trade them.
    if ":" in asset["symbol"]:
        log(f"{asset['symbol']}: live execution skipped - builder-venue "
            "market, not tradable through the main dex client (alert only)")
        return None
    sym = base_name(asset["symbol"])
    if not any(a.get("name") == sym
               for a in (_EXEC.get("meta") or {}).get("universe", [])):
        log(f"{sym}: live execution skipped - not in the perp universe")
        return None
    long_ = trade["verdict"] == "LONG"
    dec = sz_decimals(asset["symbol"])
    size = round(plan["size"], dec)
    if size <= 0:
        log(f"{sym}: size rounds to zero - not sent")
        return None
    try:
        r = ex.market_open(sym, long_, size)
        log(f"{sym}: LIVE entry sent {r}")
        got = fill_size(r)
        if got is not None and abs(got - size) > 10 ** -dec:
            log(f"{sym}: PARTIAL FILL - asked {size}, filled {got} "
                f"({got / size:.0%}); protective orders sized to the fill")
            size = got
        if not size:
            log(f"{sym}: nothing filled - no position to protect")
            return None
        rebase_to_fill(sym, trade, fill_price(r), long_)
    except Exception as e:
        log(f"{sym}: LIVE entry FAILED {type(e).__name__}: {e}")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} entry order failed - "
                          "no position")
        except Exception:
            pass
        return None
    trade["size"] = size
    per_unit = abs(trade["entry"] - trade["stop"])
    log(f"{sym}: position {size} units, real risk "
        f"${size * per_unit:.2f} (planned ${EXEC_RISK_USD:.2f})")
    try:
        r_stop = ex.order(sym, not long_, size, round_px(trade["stop"]),
                          {"trigger": {"triggerPx": round_px(trade["stop"]),
                                       "isMarket": True, "tpsl": "sl"}},
                          reduce_only=True)
        trade["stop_oid"] = order_oid(r_stop)
        # the target only takes HA_PARTIAL of the position - the rest runs
        # until the HA flips, so it must NOT be resting at the target
        part = round(size * HA_PARTIAL, dec)
        ex.order(sym, not long_, part, round_px(trade["tp"]),
                 {"limit": {"tif": "Gtc"}}, reduce_only=True)
        log(f"{sym}: LIVE stop ${fmt_px(trade['stop'])} (full size) and TP "
            f"${fmt_px(trade['tp'])} ({HA_PARTIAL:.0%}) placed")
    except Exception as e:
        log(f"{sym}: LIVE protective orders FAILED ({type(e).__name__}) - "
            "closing the position")
        try:
            ex.market_close(sym)
            send_telegram(f"\u26a0\ufe0f {esc(sym)} stop could not be placed - "
                          "position closed immediately")
        except Exception:
            try:
                send_telegram(f"\U0001F6A8 {esc(sym)} UNPROTECTED POSITION - "
                              "close it manually now")
            except Exception:
                pass
        return None
    return True


# --------------------------- entry -----------------------------------------
def fire_entry(asset, ast, direction, c, stop, hi, lo, source, trigger):
    """Risk checks, override handling, alert, trade creation and (when
    enabled) the live order. Returns True if a trade was opened."""
    sym = asset["symbol"]
    short = direction == "SHORT"
    entry = c["c"]
    risk = (stop - entry) if short else (entry - stop)
    if risk <= 0:
        return False
    if MIN_STOP_PCT and entry and risk / entry * 100 < MIN_STOP_PCT:
        log(f"{sym}: stop only {risk / entry * 100:.3f}% away "
            f"(min {MIN_STOP_PCT}%) - too tight to be worth fees, waiting")
        return False
    tp = entry - HA_RR * risk if short else entry + HA_RR * risk
    plan = {"entry": entry, "stop": stop, "tp": tp}
    event_t = c["t"] + MS[TF]

    # overrides are gone: check_asset never evaluates a symbol that already
    # holds a trade, so fire_entry is only ever reached flat
    if ast.get("trade"):
        log(f"{sym}: {direction} signal ignored - a trade is already open")
        ast["setup"] = None
        return False

    if ALERT_ENTRIES:
        try:
            send_telegram(entry_message(asset, direction, plan, hi, lo,
                                        source, event_t, trigger))
        except Exception as e:
            log(f"{sym}: entry alert failed: {type(e).__name__}: {e}")
    log(f"ALERT SENT -> telegram: {sym} {direction} ENTRY @ "
        f"${fmt_px(entry)} ({trigger})")
    RUN_ALERTS.append(f"{sym} {direction} entry @ ${fmt_px(entry)}")
    # count BEFORE recording the new trade: ast is the same object STATE_VIEW
    # holds, so counting afterwards makes the trade count itself and a cap of
    # 1 then blocks every live order that could ever be sent
    # only symbols that CAN execute count toward the live cap - builder-venue
    # trades are placed by hand and must not starve the automated ones
    open_now = sum(1 for k, v in STATE_VIEW.items()
                   if isinstance(v, dict) and v.get("trade") and executable(k))
    ast["trade"] = {"verdict": direction, "entry": entry, "stop": stop,
                    "tp": tp, "opened_t": c["t"], "checked_t": c["t"],
                    "rr": HA_RR, "risk0": risk, "half": False, "left": 1.0}
    # remember WHICH setup this came from so it cannot fire a second time
    if ast.get("setup"):
        ast["traded"] = {"ft": ast["setup"].get("ft"), "dir": direction}
    order_plan = plan_entry_orders(asset, ast["trade"])
    if EXEC_LIVE and order_plan:
        # the daily loss limit needs realised USD from the ledger, which is
        # not tracked yet - only the halt file and the position cap bite
        why = exec_blocked(open_now, 0.0)
        if why:
            log(f"{sym}: live order blocked - {why}")
        else:
            place_entry_live(asset, ast["trade"], order_plan)
    ast["phase"], ast["setup"] = "IN_TRADE", None
    return True


# --------------------------- the strategy ----------------------------------
def process_candle(asset, ast, candles, ha, i):
    """A visible trend, then a DOJI - an HA body small against that trend.
    The doji is the turn, and the trade is taken on it. No pullback, no
    retest, no confirming candle.

    Everything is re-derived from the series on every scan, so a restart
    cannot lose half a signal.
    """
    sym = asset["symbol"]
    c = candles[i]
    hd = ha[i]

    for want_long in (True, False):
        found = ha_doji(ha, i, want_long)
        if not found:
            continue
        direction = "LONG" if want_long else "SHORT"
        dt = hd["t"]                         # identify by TIMESTAMP, never by
        #                                      index - the fetch window rolls

        # ONE TRADE PER DOJI. The signal is re-derived every scan, so without
        # this the same doji fires again on the next pass at a slightly
        # different price into the identical stop. Seen live 31 Jul on AAVE,
        # CASHCAT, KAITO and SOL: -6.13% of a -14.53% book.
        done = ast.get("traded") or {}
        if done.get("ft") == dt and done.get("dir") == direction:
            continue

        # the stop is the doji's own level: its low for a long, high for a
        # short. That is the level the turn happened at.
        stop = hd["l"] if want_long else hd["h"]
        entry = c["c"]
        risk = (entry - stop) if want_long else (stop - entry)
        if risk <= 0:
            log(f"{sym}: {direction} doji but price closed through its own "
                f"level (${fmt_px(stop)}) - skipped")
            continue

        ast["setup"] = {"dir": direction, "zhi": hd["h"], "zlo": hd["l"],
                        "ft": dt, "departed": True, "touched": True,
                        "frozen": True, "t": c["t"]}
        log(f"{sym}: HA DOJI - turning {direction}, stop at the doji "
            f"{'low' if want_long else 'high'} ${fmt_px(stop)}")
        fire_entry(asset, ast, direction, c, stop, hd["h"], hd["l"], "HA",
                   f"HA doji after a {'down' if want_long else 'up'}trend - "
                   f"stop at the doji {'low' if want_long else 'high'}")
        return True

    if ast.get("setup"):
        ast["setup"] = None
    return False


# --------------------------- per-asset scan --------------------------------
def check_asset(asset, state):
    sym = asset["symbol"]
    ast = state.get(sym) or blank_asset_state()
    for k, v in blank_asset_state().items():
        ast.setdefault(k, v)
    if asset.get("lev"):
        ast["lev"] = asset["lev"]              # max leverage, for the dashboard
    changed = False
    cs = None
    source = None

    # ---- IN_TRADE: watch stop / TP first ---------------------------------
    if ast["trade"]:
        source, cs = fetch(asset, TF, 60)
        if cs:
            trade, ch = process_open_trade(asset, ast["trade"], cs,
                                           smoothed_ha(cs), cs[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        # exits win over overrides (the watch ran first). Fall through to the
        # candle walk when the trade just closed, or when overrides are on.
        if ast["trade"]:
            RUN_STATUS.append(f"{sym} IN_TRADE")
            state[sym] = ast
            return changed

    # ---- skip the fetch entirely when no new candle can exist ------------
    # ~60 markets re-fetched every pulse is what triggers HTTP 429. A symbol
    # with no open trade has nothing new to say until its next candle closes.
    if cs is None and not ast["trade"]:
        boundary = (now_ms() // MS[TF]) * MS[TF] - MS[TF]
        if ast["last_candle_t"] >= boundary:
            RUN_STATUS.append(f"{sym} up to date")
            state[sym] = ast
            return changed

    if not cs:
        source, cs = fetch(asset, TF, 300)
    if not cs:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    ha = smoothed_ha(cs)
    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        changed = process_candle(asset, ast, cs, ha, i) or changed
        ast["last_candle_t"] = cs[i]["t"]
        if ast["trade"] and ast["trade"].get("opened_t") == cs[i]["t"]:
            break                              # a trade opened on this candle

    # an open trade always reports IN_TRADE, even when a fresh setup is armed
    # on the same symbol - otherwise the run summary undercounts open trades
    setup = ast.get("setup")
    armed_dir = setup["dir"] if setup else None
    if ast["trade"]:
        stage = "IN_TRADE"
    elif setup:
        stage = ("HA-" + armed_dir
                 + (" pulled back" if setup.get("touched") else " waiting"))
    else:
        stage = ast["phase"]
    RUN_STATUS.append(f"{sym} {stage}")
    state[sym] = ast
    return changed


# --------------------------- the run --------------------------------------
def check_once():
    RUN_ALERTS.clear()
    RUN_STATUS.clear()
    state = load_state()
    STATE_VIEW.clear()
    STATE_VIEW.update(state)
    changed = False
    failures = 0
    start = time.time()
    assets = active_assets()
    RUN_UNIVERSE[0] = len(assets)
    meta = state.get("_meta") or {}
    cursor = meta.get("cursor", 0) % max(len(assets), 1)
    rotated = assets[cursor:] + assets[:cursor]        # rotate for fairness
    # symbols holding an open trade go FIRST every run - an exit check must
    # never wait for the cursor to come around
    held = {a["symbol"] for a in rotated
            if (state.get(a["symbol"]) or {}).get("trade")}
    order = [a for a in rotated if a["symbol"] in held] + \
            [a for a in rotated if a["symbol"] not in held]
    stopped_at = None
    rot_done = 0                                       # non-priority assets done
    try:
        for n, asset in enumerate(order):
            if time.time() - start > RUN_BUDGET_S:
                stopped_at = (cursor + rot_done) % len(assets)
                log(f"Run budget ({RUN_BUDGET_S}s) reached after {n} assets "
                    f"({len(held)} open trades checked first) - resuming from "
                    f"{assets[stopped_at]['symbol']} next run")
                break
            if asset["symbol"] not in held:
                rot_done += 1
            try:
                had = bool((state.get(asset["symbol"]) or {}).get("trade"))
                changed = check_asset(asset, state) or changed
                has = bool((state.get(asset["symbol"]) or {}).get("trade"))
                if had != has:
                    save_state(state)      # opened OR closed: persist NOW, so
                                           # a restart cannot resurrect it
            except Exception as e:
                failures += 1
                log(f"{asset['symbol']}: check failed: "
                    f"{type(e).__name__}: {e}")
                RUN_STATUS.append(f"{asset['symbol']} error")
            time.sleep(FETCH_DELAY_S)

        # zombie sweep: open trades on symbols that have dropped out of the
        # universe still get monitored - a trade must never go unwatched
        scanned = {a["symbol"] for a in assets}
        for sym, ast in list(state.items()):
            if sym.startswith("_") or not isinstance(ast, dict):
                continue
            if ast.get("trade") and sym not in scanned:
                ghost = {"symbol": sym, "hl_coin": sym,
                         "label": f"{sym}-PERP", "fallbacks": [],
                         "cls": ("commodity" if is_commodity(sym) else "stock")
                         if ":" in sym else "crypto"}
                try:
                    changed = check_asset(ghost, state) or changed
                    RUN_UNIVERSE[0] += 1   # it appends a RUN_STATUS row, so the
                                           # denominator has to count it too
                    if not (state.get(sym) or {}).get("trade"):
                        save_state(state)
                    log(f"{sym}: monitored outside the universe (open trade)")
                except Exception as e:
                    log(f"{sym}: zombie-trade check failed: "
                        f"{type(e).__name__}: {e}")

        new_cursor = stopped_at if stopped_at is not None else 0
        if meta.get("cursor", 0) != new_cursor:
            state["_meta"] = {"cursor": new_cursor}
            changed = True
        # the state file always gets written: its contents double as the
        # dashboard's liveness heartbeat
        state["_meta"] = dict(state.get("_meta") or {},
                              cursor=new_cursor,
                              scan_every_s=MS[SCAN_EVERY] // 1000,
                              last_scan_utc=datetime.now(timezone.utc)
                              .isoformat(timespec="seconds"))
        save_state(state)
        if failures:
            log(f"{failures} asset(s) failed this run - they retry next cycle.")
    finally:
        write_run_summary()


def seconds_to_next_close(buffer_s=15):
    period = MS[SCAN_EVERY] // 1000
    return period - (time.time() % period) + buffer_s


def run_loop():
    log("smoothed HA agent started (loop mode). Ctrl+C to stop.")
    check_once()
    while True:
        wait = seconds_to_next_close()
        log(f"Next scan in {wait / 60:.1f} min")
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            log("Stopped by user.")
            return
        check_once()


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        send_telegram("\u2705 <b>smoothed HA agent</b> - test message, "
                      "Telegram wiring is good.")
        log("test message sent")
        return
    if "--loop" in args:
        run_loop()
        return
    check_once()


if __name__ == "__main__":
    main()
