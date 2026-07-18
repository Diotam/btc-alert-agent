#!/usr/bin/env python3
"""
HEIKIN ASHI + STOCHASTIC REVERSAL AGENT - 15m - full trade lifecycle
---------------------------------------------------------------------
Spots trend reversals using Heikin Ashi candle sequences confirmed by
stochastic exhaustion. Pattern logic reads HA candles; entries and stops
use REAL candle prices (HA values are averages and cannot be traded).

LONG sequence (all stages in order, on 15m closed candles):
  1. SETUP    stochastic %K crosses below the bottom line (20) and
              downward momentum is weakening
  2. DOJI     a GREEN Heikin Ashi doji appears (indecision -> turn)
  3. CONFIRM  two consecutive GREEN HA candles, LARGE bodies,
              wicks at the TOP ONLY (no lower wicks)
  4. ENTER    at the close of the 2nd confirmation candle
              stop below the pattern's real low, TP1 2R, TP2 3R

SHORT sequence (exact mirror):
  1. SETUP    %K crosses above the top line (80), upward momentum weak
  2. DOJI     a RED HA doji appears
  3. CONFIRM  two consecutive RED HA candles, LARGE bodies,
              wicks at the BOTTOM ONLY (no upper wicks)
  4. ENTER    at the close of the 2nd confirmation candle

Alerts: DOJI SPOTTED (heads-up), ENTRY (rule checklist + plan),
then TP1 (stop -> breakeven) / TP2 / STOPPED OUT tracked on 5m candles.

Alerts are delivered to Telegram. Config from environment variables
(GitHub repo Secrets):
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Modes:
  python3 btc_alert_agent.py           single scan (workflow default)
  python3 btc_alert_agent.py --test    send a test message
  python3 btc_alert_agent.py --loop    run continuously (local PC)
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================= CONFIG ======================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Asset universe -------------------------------------------------------
DISCOVER_ALL = True
DEXES = ["", "xyz"]                # "" = main crypto dex, "xyz" = TradeXYZ stocks
MIN_DAY_VOLUME_USD = 1_000_000       # watch everything above $1M 24h notional
MAX_ASSETS = 150
FETCH_DELAY_S = 0.12

ASSETS = [                         # used when DISCOVER_ALL = False / discovery fails
    {"symbol": "BTC",   "label": "BTC-PERP",        "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
    {"symbol": "xyz:TSLA",  "label": "TSLA-PERP (xyz)", "hl_coin": "xyz:TSLA",
     "fallbacks": ["yahoo:TSLA"]},
    {"symbol": "xyz:SP500", "label": "SP500-PERP (xyz)", "hl_coin": "xyz:SP500",
     "fallbacks": ["yahoo:^GSPC"]},
    {"symbol": "xyz:SKHX",  "label": "SKHX-PERP (xyz)", "hl_coin": "xyz:SKHX",
     "fallbacks": ["yahoo:SKHY"]},
]

# --- Strategy dials -------------------------------------------------------
ENABLE_SHORTS = True
STOCH_K = 14                 # stochastic lookback
STOCH_SMOOTH = 3             # %K smoothing (slow stochastic)
STOCH_D = 3                  # %D smoothing
STOCH_OVERSOLD = 15          # the "bottom line"
STOCH_OVERBOUGHT = 85        # the "top line"
CROSS_LOOKBACK = 6           # the cross must have happened within this many candles
DOJI_BODY_FRAC = 0.12        # HA body <= this fraction of range = doji
BIG_BODY_FRAC = 0.45         # HA body >= this fraction of range = large body
                             # (HA open starts at the doji midpoint, capping
                             #  the first confirmation candle near 0.5)
BODY_MIN_ATR = 0.50          # ...and >= this fraction of ATR (absolute size vs
                             #  current volatility, not the dead trend's bodies)
RANGE_MIN_ATR = 0.80         # candle range must be meaningful vs ATR (filters
                             #  micro flat candles whose ratios look strong)
WICK_TOL_FRAC = 0.15         # "no wick" tolerance: <= this fraction of range
WICK_TOL_BODY = 0.20         # ...or <= this fraction of the body (HA-open lag)
WAIT_DOJI_TTL = 10           # candles to wait for the doji after setup
CONFIRM_TTL = 6              # candles to complete both confirmations
STOP_PAD_ATR = 0.15          # stop pad beyond the pattern's real extreme
R_TP1, R_TP2 = 2.0, 3.0
BREAKEVEN_AFTER_TP1 = True
SETUP_REFRESH_MIN = 12       # scan cadence (new 15m candles processed as they close)

TIMEZONE = "America/New_York"
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
# ===========================================================================

MS = {"5m": 300_000, "15m": 900_000}
LOOKBACK = {"5m": 300, "15m": 400}
LOCAL_TZ = ZoneInfo(TIMEZONE)


def fmt_ts(ms, fmt="%Y-%m-%d %I:%M %p %Z"):
    return datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime(fmt)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}",
          flush=True)


def fmt_px(p):
    return f"{p:,.0f}" if p >= 10000 else f"{p:,.2f}" if p >= 1 else f"{p:,.4f}"


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
def http_json(url, payload=None, timeout=20):
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
    yint = {"5m": "5m", "15m": "15m"}[interval]
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


def discover_assets():
    found = []
    for dex in DEXES:
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


# ------------------------------ indicators --------------------------------
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


def heikin_ashi(candles):
    """HA candle series. HA prices are averages - never trade them directly."""
    out = []
    for i, c in enumerate(candles):
        hc = (c["o"] + c["h"] + c["l"] + c["c"]) / 4
        ho = (c["o"] + c["c"]) / 2 if i == 0 else (out[-1]["o"] + out[-1]["c"]) / 2
        out.append({"t": c["t"], "o": ho, "c": hc,
                    "h": max(c["h"], ho, hc), "l": min(c["l"], ho, hc)})
    return out


def stochastic(candles, k=STOCH_K, smooth=STOCH_SMOOTH, d=STOCH_D):
    """Slow stochastic on REAL candles. Returns (%K smoothed, %D)."""
    n = len(candles)
    raw = [None] * n
    for i in range(k - 1, n):
        hh = max(x["h"] for x in candles[i - k + 1:i + 1])
        ll = min(x["l"] for x in candles[i - k + 1:i + 1])
        raw[i] = 100 * (candles[i]["c"] - ll) / (hh - ll) if hh > ll else 50.0

    def smoo(arr, p):
        out = [None] * n
        for i in range(n):
            w = [arr[j] for j in range(max(0, i - p + 1), i + 1)
                 if arr[j] is not None]
            if len(w) == p:
                out[i] = sum(w) / p
        return out

    kline = smoo(raw, smooth)
    dline = smoo(kline, d)
    return kline, dline


# --------------------------- HA candle grammar -----------------------------
def ha_props(ha):
    body = abs(ha["c"] - ha["o"])
    rng = ha["h"] - ha["l"]
    up_w = ha["h"] - max(ha["o"], ha["c"])
    dn_w = min(ha["o"], ha["c"]) - ha["l"]
    return body, rng, up_w, dn_w


def is_green(ha):
    return ha["c"] > ha["o"]


def is_doji(ha, color_long):
    """Green doji (for longs) / red doji (for shorts): tiny HA body."""
    body, rng, _, _ = ha_props(ha)
    if rng <= 0:
        return False
    right_color = is_green(ha) if color_long else not is_green(ha)
    return right_color and body <= DOJI_BODY_FRAC * rng


def is_strong(ha, atr_now, long):
    """The three entry rules: right color, large body, wick on one side only.
    LONG: green, big body, no lower wick (top wick allowed).
    SHORT: red, big body, no upper wick (bottom wick allowed)."""
    body, rng, up_w, dn_w = ha_props(ha)
    if rng <= 0 or (atr_now and rng < RANGE_MIN_ATR * atr_now):
        return False, []
    color_ok = is_green(ha) if long else not is_green(ha)
    big = body >= BIG_BODY_FRAC * rng and (not atr_now or body >= BODY_MIN_ATR * atr_now)
    w = dn_w if long else up_w
    wick_ok = w <= WICK_TOL_FRAC * rng or w <= WICK_TOL_BODY * body
    rules = [("Green HA candle" if long else "Red HA candle", color_ok),
             ("Large body", big),
             ("No lower wick" if long else "No upper wick", wick_ok)]
    return color_ok and big and wick_ok, rules


def stoch_setup(kline, i, long):
    """LONG: %K crossed below the bottom line recently AND downward momentum
    is weakening. SHORT: mirrored at the top line."""
    if i < 3 or kline[i] is None or kline[i - 1] is None or kline[i - 2] is None:
        return False
    # a fresh cross through the line within the lookback, OR still pinned
    # in the exhaustion zone (deep trends keep %K below the line for long
    # stretches - the tradeable moment is when momentum fades while there)
    in_zone = (kline[i] < STOCH_OVERSOLD if long
               else kline[i] > STOCH_OVERBOUGHT)
    crossed = in_zone
    for j in range(max(3, i - CROSS_LOOKBACK + 1), i + 1):
        if kline[j] is None or kline[j - 1] is None:
            continue
        if long and kline[j - 1] >= STOCH_OVERSOLD > kline[j]:
            crossed = True
        if not long and kline[j - 1] <= STOCH_OVERBOUGHT < kline[j]:
            crossed = True
    if not crossed:
        return False
    d_now = kline[i] - kline[i - 1]
    d_prev = kline[i - 1] - kline[i - 2]
    if long:
        weak = d_now > d_prev or d_now > 0     # decline decelerating or turning up
        below = kline[i] < STOCH_OVERSOLD + 10
        return weak and below
    weak = d_now < d_prev or d_now < 0         # climb decelerating or turning down
    above = kline[i] > STOCH_OVERBOUGHT - 10
    return weak and above


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


def doji_message(asset, direction, kval, px, t):
    sym = asset["symbol"]
    color = "green" if direction == "LONG" else "red"
    line = STOCH_OVERSOLD if direction == "LONG" else STOCH_OVERBOUGHT
    return "\n".join([
        f"\U0001F56F <b>DOJI SPOTTED \u00b7 {esc(sym)}</b> \u2014 possible "
        f"{direction} reversal",
        f"<i>{esc(asset['label'])} \u00b7 price <code>${fmt_px(px)}</code> "
        f"\u00b7 {esc(fmt_ts(t))}</i>",
        "",
        f"Stochastic exhausted through {line} (%K {kval:.1f}, momentum fading) "
        f"and a {color} Heikin Ashi doji just printed.",
        "",
        f"\u23F3 Watching for two consecutive {color} large-body HA candles "
        f"with {'no lower wicks' if direction == 'LONG' else 'no upper wicks'}. "
        "Entry alert follows only if both confirm.",
    ])


def entry_message(asset, direction, plan, rules1, rules2, kval, source, t):
    sym = asset["symbol"]
    icon = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    lines = [
        f"{icon} <b>{direction} REVERSAL ENTRY \u00b7 {esc(sym)}</b> \u2014 "
        f"<code>${fmt_px(plan['entry'])}</code>",
        f"<i>{esc(asset['label'])} \u00b7 HA + stochastic \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        "\U0001F4CB <b>Trade plan (enter at market):</b>",
        f"<code>Entry  ${fmt_px(plan['entry'])}</code>",
        f"<code>Stop   ${fmt_px(plan['stop'])}</code>  (beyond pattern "
        f"{'low' if direction == 'LONG' else 'high'})",
        f"<code>TP1    ${fmt_px(plan['tp1'])}</code>  ({R_TP1:.0f}R)",
        f"<code>TP2    ${fmt_px(plan['tp2'])}</code>  ({R_TP2:.0f}R)",
        f"<code>Risk   ${fmt_px(plan['risk'])}</code>  (1R)",
        "",
        f"\U0001F50D <b>Sequence completed</b> (stoch %K {kval:.1f} at setup)",
        "\u2705 Exhaustion cross + weak momentum",
        f"\u2705 {'Green' if direction == 'LONG' else 'Red'} HA doji",
        "<b>Confirmation candle 1</b>",
    ]
    lines += [f"\u2705 {esc(d)}" for d, _ in rules1]
    lines += ["<b>Confirmation candle 2</b>"]
    lines += [f"\u2705 {esc(d)}" for d, _ in rules2]
    lines += ["", f"<i>Source: {esc(source)}. \u26A0 {DISCLAIMER_TXT}</i>"]
    return "\n".join(lines)


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    sym = asset["symbol"]
    v = trade["verdict"]
    pl = pnl_pct(trade, exit_px)
    pl_s = f"{pl:+.2f}%"
    meta = {
        "TP1":  ("\u2705", "TP1 HIT", "First target (2R) reached"),
        "TP2":  ("\U0001F3C1", "TP2 HIT - trade complete", "Final target (3R) reached"),
        "STOP": ("\u274C", "STOPPED OUT", "Pattern stop hit"),
    }[kind]
    icon, big, sub = meta
    return "\n".join([
        f"{icon} <b>{big} \u00b7 {esc(sym)} {v}</b>  <code>{pl_s}</code>",
        f"<i>{sub}</i>",
        "",
        f"<code>Entry  ${fmt_px(trade['entry'])}</code>",
        f"<code>Exit   ${fmt_px(exit_px)}</code>",
        f"Opened {esc(fmt_ts(trade['opened_t'], '%b %d %I:%M %p %Z'))}",
        f"\U0001F552 {esc(fmt_ts(event_t))}",
        "",
        f"{esc(note)}",
        f"<i>\u26A0 {DISCLAIMER_TXT}</i>",
    ])


# ------------------------------- state ------------------------------------
def load_state():
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def blank_asset_state():
    return {"phase": "SCAN", "last_setup_check": 0, "last_candle_t": 0,
            "seq": None, "trade": None}


# ------------------------------- agent ------------------------------------
def process_open_trade(asset, trade, candles, last_closed_t):
    sym = asset["symbol"]
    changed = False
    new_candles = [c for c in candles
                   if trade["checked_t"] < c["t"] <= last_closed_t]
    for c in new_candles:
        long = trade["verdict"] == "LONG"
        hit_stop = c["l"] <= trade["stop"] if long else c["h"] >= trade["stop"]
        hit_tp2 = c["h"] >= trade["tp2"] if long else c["l"] <= trade["tp2"]
        hit_tp1 = (not trade["tp1_hit"]
                   and (c["h"] >= trade["tp1"] if long else c["l"] <= trade["tp1"]))

        if hit_stop:
            note = ("Stop moved to breakeven after TP1, so this exit is at entry."
                    if trade["tp1_hit"] else
                    "Pattern stop was hit. Wait for the next full sequence.")
            send_telegram(lifecycle_message(asset, "STOP", trade,
                                            trade["stop"], c["t"], note))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])} -> telegram sent")
            RUN_ALERTS.append(f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True

        if hit_tp2:
            send_telegram(lifecycle_message(
                asset, "TP2", trade, trade["tp2"], c["t"],
                "Full 3R target reached. Trade closed."))
            log(f"{sym}: TP2 HIT at ${fmt_px(trade['tp2'])} -> telegram sent")
            RUN_ALERTS.append(f"{sym} TP2 HIT ({pnl_pct(trade, trade['tp2']):+.2f}%)")
            return None, True

        if hit_tp1:
            trade["tp1_hit"] = True
            note = "First target (2R) reached."
            if BREAKEVEN_AFTER_TP1:
                trade["stop"] = trade["entry"]
                note += " Stop moved to breakeven - remaining position is risk-free."
            send_telegram(lifecycle_message(asset, "TP1", trade,
                                            trade["tp1"], c["t"], note))
            log(f"{sym}: TP1 HIT at ${fmt_px(trade['tp1'])} -> telegram sent")
            RUN_ALERTS.append(f"{sym} TP1 HIT ({pnl_pct(trade, trade['tp1']):+.2f}%)")
            changed = True

        trade["checked_t"] = c["t"]

    if new_candles:
        trade["checked_t"] = last_closed_t
        changed = True
    return trade, changed


def process_candle(asset, ast, real, ha, kline, a15, i, source):
    """Walk ONE newly closed 15m candle (index i) through the sequence."""
    sym = asset["symbol"]
    ha_c = ha[i]

    def enter_confirm(direction, kval):
        ast["seq"] = {"direction": direction, "stage": "CONFIRM",
                      "ttl": CONFIRM_TTL, "kval": kval,
                      "pattern_ext": real[i]["l"] if direction == "LONG"
                      else real[i]["h"],
                      "confirms": 0, "rules": []}
        ast["phase"] = "CONFIRM"
        send_telegram(doji_message(asset, direction, kval,
                                   real[i]["c"], real[i]["t"]))
        log(f"{sym}: {'green' if direction == 'LONG' else 'red'} HA doji -> "
            "telegram sent, watching for 2 strong candles")
        RUN_ALERTS.append(f"{sym} {direction} doji spotted - watching")

    seq = ast["seq"]

    # expiry falls through to a fresh scan of this same candle
    if seq is not None:
        seq["ttl"] -= 1
        if seq["ttl"] <= 0:
            log(f"{sym}: {seq['stage']} {seq['direction']} expired - rescanning")
            ast["seq"], ast["phase"], seq = None, "SCAN", None

    # ---- SCAN: hunt a stochastic setup (and catch a same-candle doji) ------
    if seq is None:
        for direction in (["LONG"] + (["SHORT"] if ENABLE_SHORTS else [])):
            if stoch_setup(kline, i, direction == "LONG"):
                if is_doji(ha_c, direction == "LONG"):
                    enter_confirm(direction, kline[i])
                else:
                    ast["seq"] = {"direction": direction, "stage": "WAIT_DOJI",
                                  "ttl": WAIT_DOJI_TTL, "kval": kline[i],
                                  "pattern_ext": None, "confirms": 0,
                                  "rules": []}
                    ast["phase"] = "WAIT_DOJI"
                    log(f"{sym}: stoch setup {direction} (%K {kline[i]:.1f}) - "
                        "waiting for doji")
                return True
        return False

    direction = seq["direction"]
    long = direction == "LONG"

    # ---- WAIT_DOJI ----------------------------------------------------------
    if seq["stage"] == "WAIT_DOJI":
        if is_doji(ha_c, long):
            enter_confirm(direction, seq["kval"])
        return True

    # ---- CONFIRM: need two consecutive rule-perfect candles -----------------
    if seq["stage"] == "CONFIRM":
        ok, rules = is_strong(ha_c, a15[i], long)
        if long:
            seq["pattern_ext"] = min(seq["pattern_ext"], real[i]["l"])
        else:
            seq["pattern_ext"] = max(seq["pattern_ext"], real[i]["h"])
        if ok:
            seq["confirms"] += 1
            seq["rules"].append(rules)
            if seq["confirms"] >= 2:
                entry = real[i]["c"]
                pad = STOP_PAD_ATR * (a15[i] or 0)
                stop = (seq["pattern_ext"] - pad) if long                     else (seq["pattern_ext"] + pad)
                risk = abs(entry - stop)
                if risk > 0:
                    sign = 1 if long else -1
                    plan = {"entry": entry, "stop": stop, "risk": risk,
                            "tp1": entry + sign * R_TP1 * risk,
                            "tp2": entry + sign * R_TP2 * risk}
                    send_telegram(entry_message(asset, direction, plan,
                                                seq["rules"][0], seq["rules"][1],
                                                seq["kval"], source,
                                                real[i]["t"]))
                    log(f"ALERT SENT -> telegram: {sym} {direction} "
                        f"REVERSAL ENTRY @ ${fmt_px(entry)}")
                    RUN_ALERTS.append(f"{sym} {direction} reversal entry "
                                      f"@ ${fmt_px(entry)}")
                    ast["trade"] = {"verdict": direction, "entry": entry,
                                    "stop": stop, "tp1": plan["tp1"],
                                    "tp2": plan["tp2"], "tp1_hit": False,
                                    "opened_t": real[i]["t"],
                                    "checked_t": real[i]["t"]}
                    ast["phase"] = "IN_TRADE"
                ast["seq"] = None
        elif is_doji(ha_c, long):
            seq["confirms"] = 0        # extra doji: still waiting, streak resets
            seq["rules"] = []
        else:
            log(f"{sym}: confirmation broken ({direction}) - back to SCAN")
            ast["seq"], ast["phase"] = None, "SCAN"
        return True

    return False


def check_asset(asset, state):
    sym = asset["symbol"]
    now_ms = int(time.time() * 1000)
    ast = state.get(sym) or blank_asset_state()
    for k, v in blank_asset_state().items():
        ast.setdefault(k, v)
    changed = False

    # ---- IN_TRADE: watch the open position on 5m candles -----------------
    if ast["trade"]:
        source, c5 = fetch(asset, "5m", 30)
        if c5:
            trade, ch = process_open_trade(asset, ast["trade"], c5, c5[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        RUN_STATUS.append(f"{sym} IN_TRADE" if ast["trade"] else f"{sym} SCAN")
        state[sym] = ast
        return changed

    # ---- gate the 15m scan cadence ----------------------------------------
    if now_ms - ast["last_setup_check"] < SETUP_REFRESH_MIN * 60_000 \
            and ast["seq"] is None:
        RUN_STATUS.append(f"{sym} {ast['phase']}")
        state[sym] = ast
        return changed
    ast["last_setup_check"] = now_ms
    changed = True

    source, c15 = fetch(asset, "15m", 60)
    if not c15:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    ha = heikin_ashi(c15)
    kline, _ = stochastic(c15)
    a15 = atr(c15)

    # never replay history: on first contact (fresh state) or after downtime,
    # fast-forward to the recent past instead of walking days of old candles
    # through the sequence engine and alerting on long-dead patterns
    last_closed = len(c15) - 2
    cutoff = c15[last_closed]["t"] - 3 * MS["15m"]   # at most ~3 recent candles
    if ast["last_candle_t"] < cutoff:
        if ast["last_candle_t"]:
            log(f"{sym}: behind by more than 3 candles - fast-forwarding, "
                "stale patterns skipped")
        ast["last_candle_t"] = cutoff

    # process each newly CLOSED candle exactly once, in order
    for i in range(len(c15)):
        if i > last_closed or c15[i]["t"] <= ast["last_candle_t"]:
            continue
        ch = process_candle(asset, ast, c15, ha, kline, a15, i, source)
        changed = changed or ch
        ast["last_candle_t"] = c15[i]["t"]
        if ast["trade"]:
            break

    stage = ast["phase"]
    if ast["seq"]:
        stage += f" ({ast['seq']['direction']})"
    RUN_STATUS.append(f"{sym} {stage}")
    state[sym] = ast
    return changed


def check_once():
    RUN_ALERTS.clear()
    RUN_STATUS.clear()
    state = load_state()
    changed = False
    failures = 0
    assets = active_assets()
    try:
        for asset in assets:
            try:
                changed = check_asset(asset, state) or changed
            except Exception as e:
                failures += 1
                log(f"{asset['symbol']}: check failed: {e}")
                RUN_STATUS.append(f"{asset['symbol']} error")
            time.sleep(FETCH_DELAY_S)
        if changed or not STATE_FILE.exists():
            save_state(state)
        if failures:
            log(f"{failures} asset(s) failed this run - they retry next cycle.")
    finally:
        write_run_summary()


def seconds_to_next_close(buffer_s=15):
    period = MS["5m"] // 1000
    return period - (time.time() % period) + buffer_s


def run_loop():
    log("Heikin Ashi reversal agent started (loop mode). Ctrl+C to stop.")
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
                      "Strategy: 15m Heikin Ashi + stochastic reversals - "
                      "exhaustion cross \u2192 doji \u2192 two strong HA candles "
                      "\u2192 entry with pattern stop, 2R/3R targets, "
                      "breakeven after TP1.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
