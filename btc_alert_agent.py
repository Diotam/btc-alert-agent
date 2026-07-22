#!/usr/bin/env python3
"""
THREE MOVING AVERAGES + WILLIAMS FRACTAL AGENT (Advent Trading style)
----------------------------------------------------------------------
Trend-following pullback entries on a single adjustable timeframe (TF).

REGIME (the trend filter):
  LONG  regime: MA20 > MA50 > MA100, cleanly stacked (held for
                STACK_STABLE_BARS candles). Entangled / crossing MAs
                mean NO regime and NO trades.
  SHORT regime: MA100 > MA50 > MA20, same stability rule.

LONG ENTRY (mirrored for shorts):
  1. Price pulls back UNDER MA20 (shallow) or UNDER MA50 (deep)
  2. A Williams fractal GREEN arrow (bullish swing-low fractal,
     confirmed FRACTAL_PERIOD candles later) fires inside the pullback
  3. Enter at the confirming candle close.
       shallow pullback -> stop just below MA50
       deep pullback    -> stop just below MA100
     TP = entry + 1.5 x risk (RR = 1.5)
  4. If price CLOSES below MA100, the setup is cancelled - no entry on
     any green arrow until a fresh pullback forms above MA100.

SHORT ENTRY: price pulls back ABOVE MA20 (shallow: stop above MA50) or
ABOVE MA50 (deep: stop above MA100); red arrow (bearish fractal) enters;
a close above MA100 cancels. TP = 1.5R.

Exits: single TP at 1.5R or the stop - no partials, no trailing.
Closes are recorded to trades.log.

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
MIN_DAY_VOLUME_USD = 1_000_000     # skip markets below $1M 24h notional
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
TF = "15m"                   # strategy timeframe - one knob: "5m"/"15m"/"30m"
MA_TYPE = "ema"              # "ema" or "sma"
MA_LEN1, MA_LEN2, MA_LEN3 = 20, 50, 100
STACK_STABLE_BARS = 3        # MA ordering must hold this many candles
                             # (crossing / entangled MAs = no trades)
FRACTAL_PERIOD = 2           # Williams fractal wing size (confirmed 2 bars later)
RR = 1.5                     # take-profit at 1.5 x risk
SL_PAD_ATR = 0.10            # stop sits this far beyond the reference MA
ENABLE_SHORTS = True

ALERT_ENTRIES = True
ALERT_STAGES = False         # pullback-armed alerts (log-only when False)
ALERT_LIFECYCLE = True       # TP / stop alerts

CTX_TF = "30m"               # context timeframe: the trade thesis lives here
EXEC_TF = "5m"               # execution timeframe: entries, stops, TPs, exits
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
TIMEZONE = "America/Chicago"
LOCAL_TZ = ZoneInfo(TIMEZONE)

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000}
LOOKBACK = {"5m": 300, "15m": 400, "30m": 400}

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
    yint = {"5m": "5m", "15m": "15m", "30m": "30m"}[interval]
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


# ------------------------------ ma engine ----------------------------------
def moving_average(values, period):
    return ema(values, period) if MA_TYPE == "ema" else sma(values, period)


def bull_fractal(candles, j, n=FRACTAL_PERIOD):
    """Williams bullish (green-arrow) fractal at index j: a swing LOW.
    Needs n candles each side, so it is confirmed at candle j+n."""
    if j < n or j + n >= len(candles):
        return False
    low = candles[j]["l"]
    return all(low < candles[j + k]["l"] for k in range(1, n + 1)) and \
        all(low < candles[j - k]["l"] for k in range(1, n + 1))


def bear_fractal(candles, j, n=FRACTAL_PERIOD):
    """Williams bearish (red-arrow) fractal at index j: a swing HIGH."""
    if j < n or j + n >= len(candles):
        return False
    high = candles[j]["h"]
    return all(high > candles[j + k]["h"] for k in range(1, n + 1)) and \
        all(high > candles[j - k]["h"] for k in range(1, n + 1))


def regime(m1, m2, m3, i):
    """LONG when MA20 > MA50 > MA100 for STACK_STABLE_BARS candles,
    SHORT when MA100 > MA50 > MA20 likewise, else None (MAs crossing)."""
    if i < STACK_STABLE_BARS or m3[i] is None:
        return None
    long_ok = short_ok = True
    for k in range(STACK_STABLE_BARS):
        a, b, c = m1[i - k], m2[i - k], m3[i - k]
        if a is None or b is None or c is None:
            return None
        if not (a > b > c):
            long_ok = False
        if not (c > b > a):
            short_ok = False
    if long_ok:
        return "LONG"
    if short_ok:
        return "SHORT"
    return None


# ----------------------------- telegram copy --------------------------------
def stage_message(asset, direction, depth, px, t):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    return "\n".join([
        f"{e} <b>PULLBACK ARMED \u00b7 {esc(asset['symbol'])} {direction}</b>",
        f"Price pulled {'under' if direction == 'LONG' else 'over'} "
        f"MA{MA_LEN2 if depth == 'deep' else MA_LEN1} - waiting for the "
        f"{'green' if direction == 'LONG' else 'red'} fractal arrow",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 {esc(fmt_ts(t))}</i>",
    ])


def entry_message(asset, direction, plan, depth, frac_px, source, t):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    sl_ma = MA_LEN3 if depth == "deep" else MA_LEN2
    lines = [
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} zone \u00b7 3MA + fractal \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        f"\U0001F4CA <b>Setup</b>: MA{MA_LEN1}/{MA_LEN2}/{MA_LEN3} stacked "
        f"{direction}; {depth} pullback; "
        f"{'green' if direction == 'LONG' else 'red'} fractal at ${fmt_px(frac_px)}",
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>  (beyond MA{sl_ma})",
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
        if stop_hit:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "STOP", trade, trade["stop"], c["t"], ""))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])}")
            record_close(sym, trade, trade["stop"], "STOP")
            RUN_ALERTS.append(
                f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True
        if tp_hit:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "TP", trade, tp, c["t"], ""))
            log(f"{sym}: TP HIT at ${fmt_px(tp)}")
            record_close(sym, trade, tp, "TP")
            RUN_ALERTS.append(f"{sym} TP HIT ({pnl_pct(trade, tp):+.2f}%)")
            return None, True
    return trade, changed


def process_candle(asset, ast, real, m1, m2, m3, a, i, source):
    """Walk ONE newly closed candle through the 3MA + fractal engine."""
    sym = asset["symbol"]
    reg = regime(m1, m2, m3, i)
    setup = ast["setup"]

    # no clean stack -> stand down entirely
    if reg is None:
        if setup:
            log(f"{sym}: MAs crossing - setup cancelled")
        ast["setup"], ast["phase"] = None, "SCAN"
        return True
    if setup and setup["direction"] != reg:
        setup = None
        ast["setup"] = None

    long = reg == "LONG"
    c = real[i]

    # trend-break invalidation: close beyond MA100 cancels everything
    broke = c["c"] < m3[i] if long else c["c"] > m3[i]
    if broke:
        if setup:
            log(f"{sym}: price closed {'below' if long else 'above'} "
                f"MA{MA_LEN3} - setup cancelled (no fractal entries)")
        ast["setup"], ast["phase"] = None, "SCAN"
        return True

    # pullback arming / deepening
    pulled_shallow = c["l"] <= m1[i] if long else c["h"] >= m1[i]
    pulled_deep = c["l"] <= m2[i] if long else c["h"] >= m2[i]
    if pulled_deep or pulled_shallow:
        depth = "deep" if pulled_deep else "shallow"
        if setup is None or (setup["depth"] == "shallow" and depth == "deep"):
            fresh = setup is None
            setup = {"direction": reg, "depth": depth, "armed_t": c["t"]} \
                if fresh else {**setup, "depth": depth}
            ast["setup"] = setup
            ast["phase"] = "ARMED"
            if fresh:
                log(f"{sym}: {reg} pullback armed ({depth}) - waiting for "
                    f"{'green' if long else 'red'} fractal")
                if ALERT_STAGES:
                    send_telegram(stage_message(asset, reg, depth, c["c"], c["t"]))
            elif depth == "deep":
                log(f"{sym}: pullback deepened past MA{MA_LEN2} - "
                    f"stop reference now MA{MA_LEN3}")

    # fractal entry: the arrow confirms FRACTAL_PERIOD candles after its low
    if ast["setup"]:
        setup = ast["setup"]
        j = i - FRACTAL_PERIOD
        if j >= 0 and real[j]["t"] >= setup["armed_t"] - FRACTAL_PERIOD * (real[1]["t"] - real[0]["t"]):
            arrow = bull_fractal(real, j) if long else bear_fractal(real, j)
            if arrow:
                sl_ref = m3[i] if setup["depth"] == "deep" else m2[i]
                pad = SL_PAD_ATR * (a[i] or 0)
                stop = sl_ref - pad if long else sl_ref + pad
                entry = c["c"]
                risk = (entry - stop) if long else (stop - entry)
                if risk <= 0:
                    log(f"{sym}: fractal fired but entry is beyond the stop "
                        "reference - skipped")
                    return True
                tp = entry + RR * risk if long else entry - RR * risk
                plan = {"entry": entry, "stop": stop, "tp": tp}
                frac_px = real[j]["l"] if long else real[j]["h"]
                if ALERT_ENTRIES:
                    send_telegram(entry_message(asset, reg, plan,
                                                setup["depth"], frac_px,
                                                source, c["t"]))
                log(f"ALERT SENT -> telegram: {sym} {reg} ENTRY @ "
                    f"${fmt_px(entry)} ({setup['depth']} pullback, "
                    f"{TF} fractal)")
                RUN_ALERTS.append(f"{sym} {reg} entry @ ${fmt_px(entry)}")
                ast["trade"] = {"verdict": reg, "entry": entry, "stop": stop,
                                "tp": tp, "opened_t": c["t"],
                                "checked_t": c["t"]}
                ast["phase"], ast["setup"] = "IN_TRADE", None
                return True
    return True


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
    source, cs = fetch(asset, TF, MA_LEN3 + 40)
    if not cs:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    closes = [c["c"] for c in cs]
    m1 = moving_average(closes, MA_LEN1)
    m2 = moving_average(closes, MA_LEN2)
    m3 = moving_average(closes, MA_LEN3)
    a = atr(cs)

    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        ch = process_candle(asset, ast, cs, m1, m2, m3, a, i, source)
        changed = changed or ch
        ast["last_candle_t"] = cs[i]["t"]
        if ast["trade"]:
            break

    stage = ast["phase"]
    if ast["setup"]:
        stage = f"ARMED-{ast['setup']['depth'].upper()} ({ast['setup']['direction']})"
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
                      f"Strategy: {CTX_TF} stochastic reversal zones \u2192 {EXEC_TF} HA "
                      "sequence (doji + two strong candles + volume/PA "
                      "confirmation) \u2192 entry; exits on 5m: 2R/3R targets, "
                      "breakeven after TP1, smoothed-HA runner exit.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
