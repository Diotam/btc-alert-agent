#!/usr/bin/env python3
"""
BREAK & RETEST AGENT
---------------------
Continuation entries at broken structure levels, on one adjustable
timeframe (TF).

LEVELS: swing pivots (a high/low with PIVOT_WING lower highs / higher
lows on each side) from the last LEVEL_LOOKBACK candles.

LONG (mirrored for shorts):
  1. BREAK    a candle CLOSES above a pivot high by BREAK_MIN_ATR x ATR
              -> the level arms and is watched for RETEST_TTL candles
  2. RETEST   price trades back down into the level zone
              (level + RETEST_TOL_ATR x ATR)
  3. REJECT   the retest candle closes back ABOVE the level
              -> enter at that close
              stop  = level - STOP_PAD_ATR x ATR (old resistance failed)
              TP    = entry + RR x risk (RR = 1.5)
  FAIL: a close back below the level by FAIL_CLOSE_ATR x ATR cancels the
  setup (failed break). Expiry after RETEST_TTL candles also cancels.

Exits: single TP at 1.5R or the stop, with intrabar detection on the
live candle. Closes are recorded to trades.log.

Alerts are delivered to Telegram. Config from environment variables
(GitHub repo Secrets):
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Modes:
  python3 btc_alert_agent.py           single scan (workflow default)
  python3 btc_alert_agent.py --test    send a test message
  python3 btc_alert_agent.py --loop    run continuously (droplet/PC)
"""

import json
import os
import sys
import time
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================= CONFIG ======================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")



# --- Asset universe -------------------------------------------------------
DISCOVER_ALL = True
DISCOVER_DEXES = False             # main crypto dex only (no stock venues)
DEXES = [""]                       # "" = main crypto dex
ONLY = []                          # trade ONLY these symbols ([] = whole universe)
MIN_DAY_VOLUME_USD = 10_000_000    # skip markets below $10M 24h notional
MAX_ASSETS = 70
FETCH_DELAY_S = 0.12
REQUEST_TIMEOUT_S = 8              # fail fast: a throttled API must not burn 20s
RUN_BUDGET_S = 480                 # hard per-run budget; remaining assets resume
                                   # next run via a rotating cursor
MAX_ZONES = 20                     # cap concurrently open reversal zones

ASSETS = [                         # used when DISCOVER_ALL = False / discovery fails
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- Strategy dials -------------------------------------------------------
TF = "30m"                   # strategy timeframe - one knob: "5m"/"15m"/"30m"
PIVOT_WING = 5               # candles each side that define a swing pivot
LEVEL_LOOKBACK = 120         # candles scanned for pivot levels
BREAK_MIN_ATR = 0.10         # close must clear the level by this x ATR
RETEST_TOL_ATR = 0.15        # retest zone extends this x ATR beyond the level
RETEST_TTL = 4               # retest must come within 1-4 candles of the break
BREAK_VOL_MULT = 1.5         # breakout volume must be >= this x 20-candle avg
MAX_SWING_ATR = 0.40         # retest wick deeper than this below the level = skip
TREND_TF = "1h"              # higher-TF trend filter
TREND_FAST, TREND_SLOW = 20, 50   # 1h EMA pair that defines the trend
FAIL_CLOSE_ATR = 0.10        # close back through the level by this = failed break
STOP_MIN_ATR = 0.25          # stop sits 0.25-0.50 ATR beyond the level,
STOP_MAX_ATR = 0.50          # behind the retest swing
RR = 2.0                     # minimum reward-to-risk 2:1
TP_MAX_RR = 3.0              # structure targets are capped at this many R
FAILED_BREAK_REVERSAL = True # trade the squeeze when a break fails
REV_STOP_PAD_ATR = 0.15      # reversal stop pad beyond the failed excursion
ENABLE_SHORTS = True

ALERT_ENTRIES = True
ALERT_STAGES = False         # pullback-armed alerts (log-only when False)
ALERT_LIFECYCLE = True       # TP / stop alerts

STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
TIMEZONE = "America/Chicago"
LOCAL_TZ = ZoneInfo(TIMEZONE)

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}
LOOKBACK = {"5m": 300, "15m": 400, "30m": 400, "1h": 200}

REQUEST_TIMEOUT_S = 8              # fail fast: a throttled API must not burn 20s
RUN_BUDGET_S = 480                 # hard per-run budget; remaining assets resume
                                   # next run via a rotating cursor
FETCH_DELAY_S = 0.12
REPLAY_CANDLES = 3                 # candles replayed per run (covers any run gap)

def fmt_ts(ms, fmt="%Y-%m-%d %I:%M %p %Z"):
    return datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime(fmt)


def log(msg):
    ts = datetime.now(ZoneInfo(TIMEZONE))
    print(f"[{ts.strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def fmt_px(p):
    return f"{p:,.0f}" if p >= 10000 else f"{p:,.2f}" if p >= 1 else f"{p:,.6f}"


def pnl_pct(trade, exit_px):
    sign = 1 if trade["verdict"] == "LONG" else -1
    return sign * (exit_px - trade["entry"]) / trade["entry"] * 100


# --------------------------- run summary -----------------------------------
RUN_ALERTS = []
RUN_STATUS = []


def write_run_summary():
    n = len(RUN_STATUS)
    staged = [s for s in RUN_STATUS if ("DOJI" in s or "CONFIRM" in s)]
    open_t = sum(1 for s in RUN_STATUS if "IN_TRADE" in s)
    if RUN_ALERTS:
        headline = "ALERT SENT: " + " | ".join(RUN_ALERTS)
    else:
        extras = []
        if staged:
            extras.append("staging: " + "; ".join(staged)[:120])
        if open_t:
            extras.append(f"{open_t} in trade")
        headline = (f"No signal - {n} markets scanned"
                    + (f" ({', '.join(extras)})" if extras else ""))
    log("SUMMARY: " + headline)
    print(f"::notice title={'ALERT SENT' if RUN_ALERTS else 'No signal'}::{headline}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a") as f:
                icon = "\U0001F514" if RUN_ALERTS else "\U0001F4A4"
                f.write(f"### {icon} {headline}\n")
        except OSError:
            pass
    try:
        (Path(__file__).parent / "run_summary.txt").write_text(headline + "\n")
    except OSError:
        pass


# --------------------------- data sources ---------------------------------
def http_json(url, payload=None, timeout=None):
    timeout = timeout or REQUEST_TIMEOUT_S
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (signal-alert-agent/7.0)"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_hyperliquid(coin, interval, lookback):
    end = int(time.time() * 1000)
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
    data = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={mins}")
    key = next(k for k in data["result"] if k != "last")
    return [{"t": k[0] * 1000, "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[6])}
            for k in data["result"][key]]


def fetch_yahoo(ticker, interval, lookback):
    yint = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m"}[interval]
    rng = "5d" if interval == "5m" else "1mo"
    from urllib.parse import quote
    data = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
                     f"?interval={yint}&range={rng}")
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    out = []
    for i in range(len(ts)):
        if q["close"][i] is None:
            continue
        out.append({"t": ts[i] * 1000, "o": q["open"][i], "h": q["high"][i],
                    "l": q["low"][i], "c": q["close"][i], "v": q["volume"][i] or 0})
    return out


def fetch_fallback(spec, interval, lookback):
    provider, _, ident = spec.partition(":")
    return {"binance": fetch_binance, "kraken": fetch_kraken,
            "yahoo": fetch_yahoo}[provider](ident, interval, lookback)


def fetch(asset, interval, min_candles):
    lookback = LOOKBACK.get(interval, 400)
    sources = [(f"HL {asset['hl_coin']}",
                lambda: fetch_hyperliquid(asset["hl_coin"], interval, lookback))]
    for spec in asset.get("fallbacks", []):
        sources.append((spec, lambda s=spec: fetch_fallback(s, interval, lookback)))
    for name, fn in sources:
        try:
            candles = fn()
            if len(candles) >= min_candles:
                return name, candles
        except Exception as e:
            log(f"{asset['symbol']}: {name} {interval} failed: {e}")
    return None, None


def list_dexes():
    """All perp dexes on Hyperliquid: the main dex plus every HIP-3
    builder dex (TradeXYZ stocks and any newer venues)."""
    if not DISCOVER_DEXES:
        return DEXES
    try:
        data = http_json("https://api.hyperliquid.xyz/info", {"type": "perpDexs"})
        dexes = []
        for d in data:
            if d is None:
                dexes.append("")                      # the main dex slot
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
            if vol < MIN_DAY_VOLUME_USD:
                continue
            name = u["name"]
            coin = f"{dex}:{name}" if dex else name
            found.append({"symbol": coin, "hl_coin": coin, "vol": vol,
                          "label": f"{name}-PERP" + (f" ({dex})" if dex else ""),
                          "fallbacks": []})
    found.sort(key=lambda a: a["vol"], reverse=True)
    return found[:MAX_ASSETS]


def active_assets():
    if ONLY:
        return [a for a in ASSETS if a["symbol"] in ONLY] or ASSETS[:1]
    if not DISCOVER_ALL:
        return ASSETS
    assets = discover_assets()
    if assets:
        crypto = sum(1 for a in assets if ":" not in a["symbol"])
        log(f"Discovered {len(assets)} markets above "
            f"${MIN_DAY_VOLUME_USD:,.0f} 24h volume "
            f"({crypto} crypto, {len(assets) - crypto} stocks/indices)")
        return assets
    log("Discovery returned nothing - falling back to manual ASSETS list.")
    return ASSETS


def sma(values, period):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def atr(candles, period=14):
    out = [None] * len(candles)
    prev = None
    for i in range(1, len(candles)):
        tr = max(candles[i]["h"] - candles[i]["l"],
                 abs(candles[i]["h"] - candles[i - 1]["c"]),
                 abs(candles[i]["l"] - candles[i - 1]["c"]))
        if i <= period:
            prev = (prev or 0) + tr / period
            if i == period:
                out[i] = prev
        else:
            prev = (prev * (period - 1) + tr) / period
            out[i] = prev
    return out


def ema(values, period):
    k = 2 / (period + 1)
    out = [None] * len(values)
    prev = None
    for i, v in enumerate(values):
        if i == period - 1:
            prev = sum(values[:period]) / period
            out[i] = prev
        elif i >= period:
            prev = v * k + prev * (1 - k)
            out[i] = prev
    return out


# ----------------------------- telegram ------------------------------------
DISCLAIMER_TXT = ("Research signal - not financial advice. "
                  "Any single signal can fail; size accordingly.")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send_telegram(text):
    resp = http_json(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True})
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram send failed: {resp.get('description')}")


# --------------------------- break & retest engine --------------------------
def pivot_high(candles, j, wing=PIVOT_WING):
    if j < wing or j + wing >= len(candles):
        return False
    h = candles[j]["h"]
    return all(h > candles[j + k]["h"] for k in range(1, wing + 1)) and \
        all(h > candles[j - k]["h"] for k in range(1, wing + 1))


def pivot_low(candles, j, wing=PIVOT_WING):
    if j < wing or j + wing >= len(candles):
        return False
    l = candles[j]["l"]
    return all(l < candles[j + k]["l"] for k in range(1, wing + 1)) and \
        all(l < candles[j - k]["l"] for k in range(1, wing + 1))


def pivot_levels(candles, upto_i):
    """Confirmed pivot highs/lows strictly before candle upto_i."""
    highs, lows = [], []
    start = max(PIVOT_WING, upto_i - LEVEL_LOOKBACK)
    for j in range(start, upto_i - PIVOT_WING):
        if pivot_high(candles, j):
            highs.append(candles[j]["h"])
        if pivot_low(candles, j):
            lows.append(candles[j]["l"])
    return highs, lows


def stage_message(asset, direction, level, px, t):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    return "\n".join([
        f"{e} <b>LEVEL BROKEN \u00b7 {esc(asset['symbol'])} {direction}</b>",
        f"Closed {'above' if direction == 'LONG' else 'below'} "
        f"${fmt_px(level)} - watching for the retest",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 {esc(fmt_ts(t))}</i>",
    ])


def entry_message(asset, direction, plan, level, source, t, note=None):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    lines = [
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 break &amp; retest \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        f"\U0001F4CA <b>Setup</b>: " + (esc(note) if note else
        f"broke ${fmt_px(level)}, retested it as "
        f"{'support' if direction == 'LONG' else 'resistance'}, and rejected"),
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>  (beyond the level)",
        f"TP:    <code>${fmt_px(plan['tp'])}</code>  ({RR}R)",
        f"<i>data: {esc(source)}</i>",
    ]
    return "\n".join(lines)


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    emoji, title, sub = {
        "TP": ("\U0001F3C1", "TAKE PROFIT HIT", f"{RR}R target reached"),
        "STOP": ("\u274C", "STOPPED OUT", "Stop level hit"),
    }[kind]
    pnl = pnl_pct(trade, exit_px)
    return "\n".join([
        f"{emoji} <b>{title} \u00b7 {esc(asset['symbol'])} {trade['verdict']}</b>"
        f"  <code>{pnl:+.2f}%</code>",
        f"{sub} at ${fmt_px(exit_px)} (entry ${fmt_px(trade['entry'])})",
        esc(note) if note else "",
        f"<i>{esc(asset['label'])} \u00b7 {esc(fmt_ts(event_t))}</i>",
    ])


# ---------------------------- trade ledger ---------------------------------
TRADES_LOG = Path(__file__).parent / "trades.log"


def record_close(sym, trade, exit_px, kind):
    """Append a closed trade to the ledger (best-effort)."""
    try:
        with open(TRADES_LOG, "a") as f:
            f.write(json.dumps({"t": int(time.time() * 1000), "sym": sym,
                                "dir": trade["verdict"],
                                "entry": trade["entry"], "exit": exit_px,
                                "kind": kind,
                                "pnl_pct": round(pnl_pct(trade, exit_px), 3)})
                    + "\n")
    except OSError:
        pass


def blank_asset_state():
    return {"phase": "SCAN", "last_candle_t": 0, "setup": None, "trade": None}


# ------------------------------- agent ------------------------------------
def process_open_trade(asset, trade, candles, last_closed_t):
    """TP / stop watch on closed candles. Stop is checked first within a
    candle (conservative). Returns (trade or None, changed)."""
    sym = asset["symbol"]
    long = trade["verdict"] == "LONG"
    tp = trade.get("tp") or trade.get("tp2")     # legacy trades keep working
    changed = False
    for c in candles:
        if c["t"] <= trade["checked_t"] or c["t"] > last_closed_t:
            continue
        changed = True
        trade["checked_t"] = c["t"]
        stop_hit = c["l"] <= trade["stop"] if long else c["h"] >= trade["stop"]
        tp_hit = (c["h"] >= tp) if long else (c["l"] <= tp)
        c_close_t = c["t"] + MS[TF]              # label events with the close
        if stop_hit:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "STOP", trade, trade["stop"], c_close_t, ""))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])}")
            record_close(sym, trade, trade["stop"], "STOP")
            RUN_ALERTS.append(
                f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True
        if tp_hit:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "TP", trade, tp, c_close_t, ""))
            log(f"{sym}: TP HIT at ${fmt_px(tp)}")
            record_close(sym, trade, tp, "TP")
            RUN_ALERTS.append(f"{sym} TP HIT ({pnl_pct(trade, tp):+.2f}%)")
            return None, True

    # ---- intrabar check on the LIVE (still forming) candle ------------------
    # A fast move can blow through the stop mid-candle; don't wait for the
    # close to say so. checked_t is NOT advanced for the live candle.
    live = candles[-1]
    if live["t"] > last_closed_t:
        stop_hit = live["l"] <= trade["stop"] if long else live["h"] >= trade["stop"]
        tp_hit = (live["h"] >= tp) if long else (live["l"] <= tp)
        if stop_hit:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "STOP", trade, trade["stop"], int(time.time() * 1000),
                    "Intrabar - stop level traded before the candle closed."))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])} (intrabar)")
            record_close(sym, trade, trade["stop"], "STOP")
            RUN_ALERTS.append(
                f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True
        if tp_hit:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "TP", trade, tp, int(time.time() * 1000),
                    "Intrabar - target traded before the candle closed."))
            log(f"{sym}: TP HIT at ${fmt_px(tp)} (intrabar)")
            record_close(sym, trade, tp, "TP")
            RUN_ALERTS.append(f"{sym} TP HIT ({pnl_pct(trade, tp):+.2f}%)")
            return None, True
    return trade, changed


def structure_target(entry, risk, direction, highs, lows):
    """TP at the nearest prior opposite pivot if it offers RR >= RR,
    capped at TP_MAX_RR. No structure -> plain RR target.
    Returns (tp, rr) or (None, rr) when structure is too close."""
    if direction == "LONG":
        cands = [p for p in highs if p > entry]
        struct = min(cands) if cands else None
    else:
        cands = [p for p in lows if p < entry]
        struct = max(cands) if cands else None
    if struct is None:
        return entry + RR * risk if direction == "LONG" \
            else entry - RR * risk, RR
    rr = abs(struct - entry) / risk
    if rr < RR:
        return None, rr                      # target too close - skip
    if rr > TP_MAX_RR:
        return (entry + TP_MAX_RR * risk if direction == "LONG"
                else entry - TP_MAX_RR * risk), TP_MAX_RR
    return struct, rr


def trend_agrees(asset, direction):
    """1h trend filter: EMA20 vs EMA50 on the higher timeframe must point
    the same way as the trade. Feed failure = no trade (conservative)."""
    try:
        _, ch = fetch(asset, TREND_TF, TREND_SLOW + 10)
        if not ch or len(ch) < TREND_SLOW + 2:
            return False
        closes = [c["c"] for c in ch]
        f = ema(closes, TREND_FAST)[len(ch) - 2]
        s = ema(closes, TREND_SLOW)[len(ch) - 2]
        if f is None or s is None:
            return False
        return f > s if direction == "LONG" else f < s
    except Exception:
        return False


def process_candle(asset, ast, real, vols_avg, a, i, source):
    """Walk ONE newly closed candle through the break & retest engine.
    All eight checklist rules are enforced here + trend_agrees()."""
    sym = asset["symbol"]
    c = real[i]
    atr_i = a[i] or 0
    setup = ast["setup"]

    # ---- active broken level ------------------------------------------------
    if setup:
        long = setup["direction"] == "LONG"
        lvl = setup["level"]
        # track BOTH post-break extremes (retest side + excursion side)
        if setup.get("lo") is None:
            setup["lo"], setup["hi"] = c["l"], c["h"]
        else:
            setup["lo"] = min(setup["lo"], c["l"])
            setup["hi"] = max(setup["hi"], c["h"])
        setup["swing"] = setup["lo"] if long else setup["hi"]
        if c["t"] > setup["expires_t"]:
            log(f"{sym}: no retest within {RETEST_TTL} candles - setup expired")
            ast["setup"], ast["phase"], setup = None, "SCAN", None
        elif (c["c"] < lvl if long else c["c"] > lvl):
            log(f"{sym}: failed break - closed back through ${fmt_px(lvl)}")
            if FAILED_BREAK_REVERSAL and setup.get("lo") is not None:
                rev = "SHORT" if long else "LONG"
                rlong = rev == "LONG"
                stop = (setup["lo"] - REV_STOP_PAD_ATR * atr_i) if not rlong \
                    else 0  # placeholder, set below
                if rlong:
                    stop = setup["lo"] - REV_STOP_PAD_ATR * atr_i
                else:
                    stop = setup["hi"] + REV_STOP_PAD_ATR * atr_i
                entry = c["c"]
                risk = (entry - stop) if rlong else (stop - entry)
                if risk > 0:
                    highs, lows = pivot_levels(real, i)
                    tp, rr = structure_target(entry, risk, rev, highs, lows)
                    if tp is None:
                        log(f"{sym}: reversal skipped - structure target "
                            f"only {rr:.1f}R away")
                    else:
                        plan = {"entry": entry, "stop": stop, "tp": tp}
                        if ALERT_ENTRIES:
                            send_telegram(entry_message(
                                asset, rev, plan, lvl, source,
                                c["t"] + MS[TF],
                                note=f"FAILED BREAK reversal - the "
                                     f"{'breakdown' if rlong else 'breakout'} "
                                     f"was reclaimed; squeeze entry "
                                     f"({rr:.1f}R target)"))
                        log(f"ALERT SENT -> telegram: {sym} {rev} REVERSAL "
                            f"ENTRY @ ${fmt_px(entry)} (failed break of "
                            f"${fmt_px(lvl)})")
                        RUN_ALERTS.append(
                            f"{sym} {rev} failed-break entry @ ${fmt_px(entry)}")
                        ast["trade"] = {"verdict": rev, "entry": entry,
                                        "stop": stop, "tp": tp,
                                        "opened_t": c["t"],
                                        "checked_t": c["t"]}
                        ast["phase"], ast["setup"] = "IN_TRADE", None
                        return True
            ast["setup"], ast["phase"], setup = None, "SCAN", None
        else:
            touched = c["l"] <= lvl + RETEST_TOL_ATR * atr_i if long \
                else c["h"] >= lvl - RETEST_TOL_ATR * atr_i
            if touched and not setup.get("touched"):
                setup["touched"] = True
                log(f"{sym}: retesting ${fmt_px(lvl)}")
            rejected = touched and (c["c"] > lvl if long else c["c"] < lvl)
            if rejected:
                # rule: retest volume must be LOWER than breakout volume
                if c["v"] >= setup["break_vol"]:
                    log(f"{sym}: retest volume >= breakout volume - not a "
                        "quiet retest, waiting")
                    return True
                # rule: retest swing must not have pierced too deep
                swing = setup["swing"]
                too_deep = swing < lvl - MAX_SWING_ATR * atr_i if long \
                    else swing > lvl + MAX_SWING_ATR * atr_i
                if too_deep:
                    log(f"{sym}: retest wick too deep past the level - "
                        "setup discarded")
                    ast["setup"], ast["phase"] = None, "SCAN"
                    return True
                # rule: 1h trend must agree
                if not trend_agrees(asset, setup["direction"]):
                    log(f"{sym}: {TREND_TF} trend disagrees - no trade")
                    ast["setup"], ast["phase"] = None, "SCAN"
                    return True
                # stop behind the retest swing, 0.25-0.50 ATR beyond the level
                if long:
                    stop = min(swing, lvl - STOP_MIN_ATR * atr_i)
                    stop = max(stop, lvl - STOP_MAX_ATR * atr_i)
                else:
                    stop = max(swing, lvl + STOP_MIN_ATR * atr_i)
                    stop = min(stop, lvl + STOP_MAX_ATR * atr_i)
                entry = c["c"]
                risk = (entry - stop) if long else (stop - entry)
                if risk <= 0:
                    ast["setup"], ast["phase"] = None, "SCAN"
                    return True
                highs_l, lows_l = pivot_levels(real, i)
                tp, rr_used = structure_target(entry, risk,
                                               setup["direction"],
                                               highs_l, lows_l)
                if tp is None:
                    log(f"{sym}: entry skipped - structure target only "
                        f"{rr_used:.1f}R away (min {RR}R)")
                    ast["setup"], ast["phase"] = None, "SCAN"
                    return True
                plan = {"entry": entry, "stop": stop, "tp": tp}
                if ALERT_ENTRIES:
                    send_telegram(entry_message(
                        asset, setup["direction"], plan, lvl, source,
                        c["t"] + MS[TF]))
                log(f"ALERT SENT -> telegram: {sym} {setup['direction']} "
                    f"ENTRY @ ${fmt_px(entry)} (retest of ${fmt_px(lvl)}, "
                    f"{TREND_TF} trend aligned)")
                RUN_ALERTS.append(
                    f"{sym} {setup['direction']} entry @ ${fmt_px(entry)}")
                ast["trade"] = {"verdict": setup["direction"],
                                "entry": entry, "stop": stop, "tp": tp,
                                "opened_t": c["t"], "checked_t": c["t"]}
                ast["phase"], ast["setup"] = "IN_TRADE", None
                return True
        if ast["setup"] or ast["trade"]:
            return True

    # ---- hunt a fresh break: full close beyond + volume >= 1.5x average ----
    if i < 1:
        return False
    vol_ok = (vols_avg[i - 1] or 0) > 0 and \
        c["v"] >= BREAK_VOL_MULT * vols_avg[i - 1]
    highs, lows = pivot_levels(real, i)
    prev_close = real[i - 1]["c"]
    broken_high = [p for p in highs
                   if prev_close <= p and c["c"] > p + BREAK_MIN_ATR * atr_i]
    broken_low = [p for p in lows
                  if prev_close >= p and c["c"] < p - BREAK_MIN_ATR * atr_i]
    if broken_high or (broken_low and ENABLE_SHORTS):
        if not vol_ok:
            log(f"{sym}: level broken but volume below "
                f"{BREAK_VOL_MULT}x average - ignored")
            return True
    if broken_high:
        lvl = max(broken_high)
        ast["setup"] = {"direction": "LONG", "level": lvl, "touched": False,
                        "break_vol": c["v"], "swing": None,
                        "expires_t": c["t"] + RETEST_TTL * MS[TF]}
        ast["phase"] = "ARMED"
        log(f"{sym}: LONG level broken at ${fmt_px(lvl)} on "
            f"{c['v'] / (vols_avg[i - 1] or 1):.1f}x volume - "
            "watching for retest")
        if ALERT_STAGES:
            send_telegram(stage_message(asset, "LONG", lvl, c["c"], c["t"]))
        return True
    if broken_low and ENABLE_SHORTS:
        lvl = min(broken_low)
        ast["setup"] = {"direction": "SHORT", "level": lvl, "touched": False,
                        "break_vol": c["v"], "swing": None,
                        "expires_t": c["t"] + RETEST_TTL * MS[TF]}
        ast["phase"] = "ARMED"
        log(f"{sym}: SHORT level broken at ${fmt_px(lvl)} on "
            f"{c['v'] / (vols_avg[i - 1] or 1):.1f}x volume - "
            "watching for retest")
        if ALERT_STAGES:
            send_telegram(stage_message(asset, "SHORT", lvl, c["c"], c["t"]))
        return True
    return False


def check_asset(asset, state):
    sym = asset["symbol"]
    ast = state.get(sym) or blank_asset_state()
    for k, v in blank_asset_state().items():
        ast.setdefault(k, v)
    changed = False

    # ---- IN_TRADE: watch TP / stop ----------------------------------------
    if ast["trade"]:
        source, cs = fetch(asset, TF, 30)
        if cs:
            trade, ch = process_open_trade(asset, ast["trade"], cs, cs[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        RUN_STATUS.append(f"{sym} IN_TRADE" if ast["trade"] else f"{sym} SCAN")
        state[sym] = ast
        return changed

    # ---- scan / armed: process each newly closed candle --------------------
    source, cs = fetch(asset, TF, LEVEL_LOOKBACK + 2 * PIVOT_WING + 20)
    if not cs:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    a = atr(cs)
    vols_avg = sma([c["v"] for c in cs], 20)

    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        ch = process_candle(asset, ast, cs, vols_avg, a, i, source)
        changed = changed or ch
        ast["last_candle_t"] = cs[i]["t"]
        if ast["trade"]:
            break

    stage = ast["phase"]
    if ast["setup"]:
        stage = f"ARMED ({ast['setup']['direction']} ${fmt_px(ast['setup']['level'])})"
    RUN_STATUS.append(f"{sym} {stage}")
    state[sym] = ast
    return changed


# ------------------------------- state ------------------------------------
def load_state():
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def check_once():
    RUN_ALERTS.clear()
    RUN_STATUS.clear()
    state = load_state()
    changed = False
    failures = 0
    start = time.time()
    assets = active_assets()
    meta = state.get("_meta") or {}
    cursor = meta.get("cursor", 0) % max(len(assets), 1)
    order = assets[cursor:] + assets[:cursor]      # rotate for fairness
    stopped_at = None
    try:
        for n, asset in enumerate(order):
            if time.time() - start > RUN_BUDGET_S:
                stopped_at = (cursor + n) % len(assets)
                log(f"Run budget ({RUN_BUDGET_S}s) reached after {n} assets - "
                    f"resuming from {assets[stopped_at]['symbol']} next run")
                break
            try:
                had_trade = bool((state.get(asset["symbol"]) or {}).get("trade"))
                changed = check_asset(asset, state) or changed
                if not had_trade and (state.get(asset["symbol"]) or {}).get("trade"):
                    save_state(state)      # a trade just opened: persist NOW
            except Exception as e:
                failures += 1
                log(f"{asset['symbol']}: check failed: {e}")
                RUN_STATUS.append(f"{asset['symbol']} error")
            time.sleep(FETCH_DELAY_S)
        new_cursor = stopped_at if stopped_at is not None else 0
        if meta.get("cursor", 0) != new_cursor:
            state["_meta"] = {"cursor": new_cursor}
            changed = True
        # always save: the state file's mtime doubles as the liveness
        # heartbeat for the dashboard
        save_state(state)
        if failures:
            log(f"{failures} asset(s) failed this run - they retry next cycle.")
    finally:
        write_run_summary()


def seconds_to_next_close(buffer_s=15):
    period = MS["5m"] // 1000   # 5m pulse regardless of TF: heartbeat + prompt exits
    return period - (time.time() % period) + buffer_s


def run_loop():
    log("3MA + fractal agent started (loop mode). Ctrl+C to stop.")
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


if __name__ == "__main__":
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Missing config: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "as environment variables (GitHub repo Secrets).")
        sys.exit(1)
    if "--test" in sys.argv:
        if DISCOVER_ALL:
            watched = (f"all Hyperliquid markets above "
                       f"${MIN_DAY_VOLUME_USD:,.0f} 24h volume, max {MAX_ASSETS}")
        else:
            watched = ", ".join(a["symbol"] for a in ASSETS)
        send_telegram("\u2705 <b>Signal alert agent - test message</b>\n"
                      f"Your alert pipeline works. Watching: {esc(watched)}.\n"
                      f"Strategy: break &amp; retest ({TF}) - pivot level "
                      "breaks, retest-and-reject entries, stop beyond the "
                      f"level, TP {RR}R, intrabar exit detection.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
