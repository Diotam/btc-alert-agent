#!/usr/bin/env python3
"""
HEIKIN ASHI + STOCHASTIC REVERSAL AGENT - 15m zones, 1m entries
----------------------------------------------------------------
The 15m chart supplies the trade thesis; the 1m chart supplies the
timing. Pattern logic reads HA candles; entries and stops use REAL
candle prices (HA values are averages and cannot be traded).

15M REVERSAL ZONE (the thesis):
  %K crosses / pins beyond the exhaustion line (20 / 80) with weakening
  momentum -> a directional zone opens for up to 8 x 15m candles, and
  closes early if %K recovers past 50.

1M ENTRY SEQUENCE (the timing - hunted while the zone is live):
  LONG:  green 1m HA doji -> two consecutive GREEN large-body 1m HA
         candles with no lower wicks -> volume spike or price-action
         confirmation -> enter at the 2nd candle's close
  SHORT: exact mirror (red doji, red candles, no upper wicks)

  Stop: beyond the 1m pattern extreme (0.30 x 1m ATR pad), floored at
  0.20 x 15m ATR so targets clear 15m noise. TP1 2R (stop -> breakeven),
  TP2 3R, and after TP1 a smoothed-HA color flip on 15m closes the
  runner. Trades are tracked on 1m candles.

Alerts are delivered to Telegram. Config from environment variables
(GitHub repo Secrets):
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Modes:
  python3 btc_alert_agent.py           single scan (workflow default)
  python3 btc_alert_agent.py --test    send a test message
  python3 btc_alert_agent.py --loop    run continuously (local PC -
                                       strongly recommended for 1m timing)
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
ENABLE_SHORTS = True
ALERT_ENTRIES = True         # entry alerts (the signal itself)
ALERT_STAGES = False         # "DOJI SPOTTED" heads-up messages
ALERT_LIFECYCLE = True       # TP1 / TP2 / STOPPED OUT trade updates
STOCH_K = 14                 # stochastic lookback
STOCH_SMOOTH = 3             # %K smoothing (slow stochastic)
STOCH_D = 3                  # %D smoothing
STOCH_OVERSOLD = 20          # the "bottom line"
STOCH_OVERBOUGHT = 80        # the "top line"
CROSS_LOOKBACK = 6           # the cross must have happened within this many candles
ZONE_TTL_15M = 8             # a reversal zone stays live for this many 15m candles
ZONE_EXIT_K = 50             # zone closes early once 15m %K recovers past this
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
CONFIRM_TTL = 6              # 1m candles to complete both confirmations
STOP_PAD_ATR = 0.30          # stop pad beyond the pattern's real extreme (1m ATR)
MIN_RISK_ATR15 = 0.20        # risk floor vs 15m ATR so 1m stops aren't microscopic
REQUIRE_ENTRY_CONFIRM = True # entry needs a volume spike OR a PA pattern
VOL_SPIKE_MULT = 1.50        # confirmation-candle volume vs 20-candle average
BASE_WINDOW = 20             # candles before the doji defining the reversal base
R_TP1, R_TP2 = 2.0, 3.0
BREAKEVEN_AFTER_TP1 = True
SHA_EXIT = True              # after TP1, close the runner when smoothed HA flips
SHA_LEN1 = 5                 # pre-smoothing EMA on OHLC
SHA_LEN2 = 5                 # post-smoothing EMA on the HA values
SETUP_REFRESH_MIN = 12       # scan cadence (new 15m candles processed as they close)

TIMEZONE = "America/Chicago"
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
# ===========================================================================

MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
LOOKBACK = {"1m": 240, "5m": 300, "15m": 400}
LOCAL_TZ = ZoneInfo(TIMEZONE)


def fmt_ts(ms, fmt="%Y-%m-%d %I:%M %p %Z"):
    return datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime(fmt)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}",
          flush=True)


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
    yint = {"1m": "1m", "5m": "5m", "15m": "15m"}[interval]
    rng = "1d" if interval == "1m" else "5d" if interval == "5m" else "1mo"
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


def smoothed_heikin_ashi(candles, len1=SHA_LEN1, len2=SHA_LEN2):
    """Doubly-smoothed HA: EMA the OHLC first, HA on the smoothed values,
    then EMA the HA open/close. Returns [{"t","o","c"} or None] - color only,
    which is all the runner exit needs."""
    n = len(candles)
    eo = ema([c["o"] for c in candles], len1)
    eh = ema([c["h"] for c in candles], len1)
    el = ema([c["l"] for c in candles], len1)
    ec = ema([c["c"] for c in candles], len1)
    ho_arr, hc_arr = [None] * n, [None] * n
    prev_o = prev_c = None
    for i in range(n):
        if eo[i] is None:
            continue
        hc = (eo[i] + eh[i] + el[i] + ec[i]) / 4
        ho = (eo[i] + ec[i]) / 2 if prev_o is None else (prev_o + prev_c) / 2
        ho_arr[i], hc_arr[i] = ho, hc
        prev_o, prev_c = ho, hc
    # post-smooth over the valid region
    start = next((i for i in range(n) if ho_arr[i] is not None), n)
    so = ema(ho_arr[start:], len2)
    sc = ema(hc_arr[start:], len2)
    out = [None] * n
    for i in range(start, n):
        if so[i - start] is not None:
            out[i] = {"t": candles[i]["t"], "o": so[i - start],
                      "c": sc[i - start]}
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


# ----------------------- entry confirmation layer ---------------------------
def entry_confirmations(real, vols_avg, doji_i, c1_i, c2_i, long):
    """Volume spikes and price-action patterns on REAL candles around the
    completed sequence. Returns a list of human-readable confirmations."""
    out = []
    # volume spike on either confirmation candle
    best = 0.0
    for j in (c1_i, c2_i):
        if vols_avg[j]:
            best = max(best, real[j]["v"] / vols_avg[j])
    if best >= VOL_SPIKE_MULT:
        out.append(f"Volume spike {best:.1f}x average")

    # engulfing on either confirmation candle
    for j in (c1_i, c2_i):
        c, p = real[j], real[j - 1]
        if long and c["c"] > c["o"] and p["c"] < p["o"] \
                and c["c"] >= p["o"] and c["o"] <= p["c"]:
            out.append("Bullish engulfing")
            break
        if not long and c["c"] < c["o"] and p["c"] > p["o"] \
                and c["c"] <= p["o"] and c["o"] >= p["c"]:
            out.append("Bearish engulfing")
            break

    # liquidity sweep of the pre-doji extreme, then reclaim
    lo_w = max(0, doji_i - 8)
    if lo_w < doji_i:
        if long:
            prior_low = min(c["l"] for c in real[lo_w:doji_i])
            swept = min(c["l"] for c in real[doji_i:c2_i + 1]) < prior_low
            if swept and real[c2_i]["c"] > prior_low:
                out.append("Liquidity sweep & reclaim")
        else:
            prior_high = max(c["h"] for c in real[lo_w:doji_i])
            swept = max(c["h"] for c in real[doji_i:c2_i + 1]) > prior_high
            if swept and real[c2_i]["c"] < prior_high:
                out.append("Liquidity sweep & reclaim")

    # close through the broader reversal base
    b_w = max(0, doji_i - BASE_WINDOW)
    if b_w < doji_i:
        if long:
            base_high = max(c["h"] for c in real[b_w:doji_i])
            if real[c2_i]["c"] > base_high:
                out.append(f"Break above {BASE_WINDOW}-candle base")
        else:
            base_low = min(c["l"] for c in real[b_w:doji_i])
            if real[c2_i]["c"] < base_low:
                out.append(f"Break below {BASE_WINDOW}-candle base")
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


def entry_message(asset, direction, plan, rules1, rules2, confirms,
                  kval, source, t):
    sym = asset["symbol"]
    icon = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    lines = [
        f"{icon} <b>{direction} REVERSAL ENTRY \u00b7 {esc(sym)}</b> \u2014 "
        f"<code>${fmt_px(plan['entry'])}</code>",
        f"<i>{esc(asset['label'])} \u00b7 15m zone \u00b7 1m sequence \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        "\U0001F4CB <b>Trade plan (enter at market):</b>",
        f"<code>Entry  ${fmt_px(plan['entry'])}</code>",
        f"<code>Stop   ${fmt_px(plan['stop'])}</code>  (beyond pattern "
        f"{'low' if direction == 'LONG' else 'high'})",
        f"<code>TP1    ${fmt_px(plan['tp1'])}</code>  ({R_TP1:.0f}R)",
        f"<code>TP2    ${fmt_px(plan['tp2'])}</code>  ({R_TP2:.0f}R)",
        f"<code>Risk   ${fmt_px(plan['risk'])}</code>  (1R)",
        "",
        f"\U0001F50D <b>Sequence completed</b> (15m %K {kval:.1f} at zone open)",
        "\u2705 Exhaustion cross + weak momentum",
        f"\u2705 {'Green' if direction == 'LONG' else 'Red'} HA doji",
        "<b>Confirmation candle 1</b>",
    ]
    lines += [f"\u2705 {esc(d)}" for d, _ in rules1]
    lines += ["<b>Confirmation candle 2</b>"]
    lines += [f"\u2705 {esc(d)}" for d, _ in rules2]
    lines += ["<b>Entry confirmation</b>"]
    lines += ([f"\u2705 {esc(c)}" for c in confirms] if confirms
              else ["\u26A0 None (unconfirmed entry)"])
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
        "RUNNER": ("\U0001F3C1", "RUNNER CLOSED",
                   "Smoothed HA flipped - remaining position closed"),
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
    return {"phase": "SCAN", "last_setup_check": 0, "zone": None, "trade": None}


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
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(asset, "STOP", trade,
                                                trade["stop"], c["t"], note))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])}")
            RUN_ALERTS.append(f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True

        if hit_tp2:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "TP2", trade, trade["tp2"], c["t"],
                    "Full 3R target reached. Trade closed."))
            log(f"{sym}: TP2 HIT at ${fmt_px(trade['tp2'])}")
            RUN_ALERTS.append(f"{sym} TP2 HIT ({pnl_pct(trade, trade['tp2']):+.2f}%)")
            return None, True

        if hit_tp1:
            trade["tp1_hit"] = True
            note = "First target (2R) reached."
            if BREAKEVEN_AFTER_TP1:
                trade["stop"] = trade["entry"]
                note += " Stop moved to breakeven - remaining position is risk-free."
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(asset, "TP1", trade,
                                                trade["tp1"], c["t"], note))
            log(f"{sym}: TP1 HIT at ${fmt_px(trade['tp1'])}")
            RUN_ALERTS.append(f"{sym} TP1 HIT ({pnl_pct(trade, trade['tp1']):+.2f}%)")
            changed = True

        trade["checked_t"] = c["t"]

    if new_candles:
        trade["checked_t"] = last_closed_t
        changed = True
    return trade, changed


def process_1m_candle(asset, ast, real, ha, a1, vol_avg, i, source):
    """Walk ONE newly closed 1m candle through the entry sequence while a
    15m reversal zone is active."""
    sym = asset["symbol"]
    zone = ast["zone"]
    direction = zone["direction"]
    long = direction == "LONG"
    ha_c = ha[i]
    seq = zone.get("seq")

    # ---- hunting the 1m doji (the whole zone lifetime) ----------------------
    if seq is None:
        if is_doji(ha_c, long):
            zone["seq"] = {"ttl": CONFIRM_TTL,
                           "pattern_ext": real[i]["l"] if long else real[i]["h"],
                           "doji_t": real[i]["t"], "c1_t": None,
                           "confirms": 0, "rules": []}
            if ALERT_STAGES:
                send_telegram(doji_message(asset, direction, zone["kval"],
                                           real[i]["c"], real[i]["t"]))
            log(f"{sym}: 1m {'green' if long else 'red'} HA doji in 15m zone - "
                "watching for 2 strong 1m candles")
        return True

    # ---- confirming: two consecutive rule-perfect 1m candles ----------------
    seq["ttl"] -= 1
    ok, rules = is_strong(ha_c, a1[i], long)
    if long:
        seq["pattern_ext"] = min(seq["pattern_ext"], real[i]["l"])
    else:
        seq["pattern_ext"] = max(seq["pattern_ext"], real[i]["h"])

    if ok:
        seq["confirms"] += 1
        seq["rules"].append(rules)
        if seq["confirms"] == 1:
            seq["c1_t"] = real[i]["t"]
        if seq["confirms"] >= 2:
            t_index = {c["t"]: idx for idx, c in enumerate(real)}
            doji_i = t_index.get(seq["doji_t"])
            c1_i = t_index.get(seq["c1_t"], i)
            confirms = []
            if doji_i is not None:
                confirms = entry_confirmations(real, vol_avg, doji_i,
                                               c1_i, i, long)
            if REQUIRE_ENTRY_CONFIRM and not confirms:
                log(f"{sym}: 1m sequence complete but no volume/PA "
                    "confirmation - back to doji hunt")
                zone["seq"] = None
                return True
            entry = real[i]["c"]
            sign = 1 if long else -1
            stop = seq["pattern_ext"] - sign * STOP_PAD_ATR * (a1[i] or 0)
            risk = abs(entry - stop)
            floor = MIN_RISK_ATR15 * zone.get("atr15", 0)
            if floor and risk < floor:
                stop = entry - sign * floor      # widen to a meaningful stop
                risk = floor
            if risk > 0:
                plan = {"entry": entry, "stop": stop, "risk": risk,
                        "tp1": entry + sign * R_TP1 * risk,
                        "tp2": entry + sign * R_TP2 * risk}
                if ALERT_ENTRIES:
                    send_telegram(entry_message(
                        asset, direction, plan,
                        seq["rules"][0], seq["rules"][1],
                        confirms, zone["kval"], source, real[i]["t"]))
                log(f"ALERT SENT -> telegram: {sym} {direction} "
                    f"REVERSAL ENTRY @ ${fmt_px(entry)} (1m sequence)")
                RUN_ALERTS.append(f"{sym} {direction} reversal entry "
                                  f"@ ${fmt_px(entry)}")
                ast["trade"] = {"verdict": direction, "entry": entry,
                                "stop": stop, "tp1": plan["tp1"],
                                "tp2": plan["tp2"], "tp1_hit": False,
                                "opened_t": real[i]["t"],
                                "checked_t": real[i]["t"]}
                ast["phase"] = "IN_TRADE"
                ast["zone"] = None
            else:
                zone["seq"] = None
        return True

    if is_doji(ha_c, long):
        seq["confirms"], seq["rules"] = 0, []    # fresh doji: streak resets
        seq["doji_t"] = real[i]["t"]
        if long:
            seq["pattern_ext"] = min(seq["pattern_ext"], real[i]["l"])
        else:
            seq["pattern_ext"] = max(seq["pattern_ext"], real[i]["h"])
        seq["ttl"] = CONFIRM_TTL
        return True

    if seq["ttl"] <= 0 or seq["confirms"] > 0:
        # window exhausted, or a started streak broke: back to doji hunting
        zone["seq"] = None
        log(f"{sym}: 1m confirmation broken - hunting the next doji")
    return True


def check_asset(asset, state):
    sym = asset["symbol"]
    now_ms = int(time.time() * 1000)
    ast = state.get(sym) or blank_asset_state()
    for k, v in blank_asset_state().items():
        ast.setdefault(k, v)
    changed = False

    # ---- IN_TRADE: watch the open position on 1m candles ------------------
    if ast["trade"]:
        source, c1 = fetch(asset, "1m", 30)
        if c1:
            trade, ch = process_open_trade(asset, ast["trade"], c1, c1[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        # runner exit: after TP1, a smoothed-HA color flip (15m) closes the rest
        if ast["trade"] and ast["trade"].get("tp1_hit") and SHA_EXIT:
            trade = ast["trade"]
            _, c15 = fetch(asset, "15m", SHA_LEN1 + SHA_LEN2 + 20)
            if c15:
                sha = smoothed_heikin_ashi(c15)
                long = trade["verdict"] == "LONG"
                seen = trade.get("sha_checked_t", trade["opened_t"])
                for i in range(len(c15) - 1):
                    if c15[i]["t"] <= seen or sha[i] is None:
                        continue
                    flipped = (sha[i]["c"] < sha[i]["o"] if long
                               else sha[i]["c"] > sha[i]["o"])
                    if flipped:
                        exit_px = c15[i]["c"]
                        if ALERT_LIFECYCLE:
                            send_telegram(lifecycle_message(
                                asset, "RUNNER", trade, exit_px, c15[i]["t"],
                                "Smoothed HA flipped against the trade - "
                                "runner closed at the 15m close."))
                        log(f"{sym}: RUNNER CLOSED at ${fmt_px(exit_px)} "
                            "(smoothed HA flip)")
                        RUN_ALERTS.append(
                            f"{sym} runner closed ({pnl_pct(trade, exit_px):+.2f}%)")
                        ast["trade"], ast["phase"] = None, "SCAN"
                        break
                    trade["sha_checked_t"] = c15[i]["t"]
                changed = True
        RUN_STATUS.append(f"{sym} IN_TRADE" if ast["trade"] else f"{sym} SCAN")
        state[sym] = ast
        return changed

    # stagger each asset's context cadence so scans don't herd into one run
    if ast["last_setup_check"] == 0:
        ast["last_setup_check"] = now_ms - (
            zlib.crc32(sym.encode()) % (SETUP_REFRESH_MIN * 60_000))
        changed = True

    # ---- 15m context on its cadence: open / close the reversal zone --------
    if now_ms - ast["last_setup_check"] >= SETUP_REFRESH_MIN * 60_000:
        ast["last_setup_check"] = now_ms
        changed = True
        source15, c15 = fetch(asset, "15m", 60)
        if c15:
            kline, _ = stochastic(c15)
            a15 = atr(c15)
            i = len(c15) - 2
            if ast["zone"]:
                long = ast["zone"]["direction"] == "LONG"
                recovered = (kline[i] is not None and
                             (kline[i] > ZONE_EXIT_K if long
                              else kline[i] < ZONE_EXIT_K))
                if now_ms > ast["zone"]["expires_t"] or recovered:
                    why = "expired" if now_ms > ast["zone"]["expires_t"] \
                        else f"%K recovered past {ZONE_EXIT_K}"
                    log(f"{sym}: 15m zone {ast['zone']['direction']} closed - {why}")
                    ast["zone"], ast["phase"] = None, "SCAN"
            open_zones = sum(1 for v in state.values()
                             if isinstance(v, dict) and v.get("zone"))
            if not ast["zone"] and open_zones < MAX_ZONES:
                for direction in (["LONG"] + (["SHORT"] if ENABLE_SHORTS else [])):
                    if stoch_setup(kline, i, direction == "LONG"):
                        ast["zone"] = {"direction": direction,
                                       "kval": kline[i],
                                       "atr15": a15[i] or 0,
                                       "expires_t": now_ms + ZONE_TTL_15M * MS["15m"],
                                       "seq": None, "last_1m_t": 0}
                        ast["phase"] = "ZONE"
                        log(f"{sym}: 15m reversal zone {direction} open "
                            f"(%K {kline[i]:.1f}) - hunting 1m sequences")
                        break

    # ---- zone active: walk new 1m candles through the sequence -------------
    if ast["zone"]:
        source, c1 = fetch(asset, "1m", 40)
        if c1:
            last_closed = len(c1) - 2
            cutoff = c1[last_closed]["t"] - 5 * MS["1m"]
            if ast["zone"]["last_1m_t"] < cutoff:
                ast["zone"]["last_1m_t"] = cutoff
            ha = heikin_ashi(c1)
            a1 = atr(c1)
            vol_avg = sma([c["v"] for c in c1], 20)
            for i in range(len(c1)):
                if i > last_closed or c1[i]["t"] <= ast["zone"]["last_1m_t"]:
                    continue
                ch = process_1m_candle(asset, ast, c1, ha, a1, vol_avg, i, source)
                changed = changed or ch
                if ast["zone"]:
                    ast["zone"]["last_1m_t"] = c1[i]["t"]
                if ast["trade"]:
                    break

    stage = ast["phase"]
    if ast["zone"]:
        stage = "ZONE-CONFIRM" if ast["zone"].get("seq") else "ZONE"
        stage += f" ({ast['zone']['direction']})"
    RUN_STATUS.append(f"{sym} {stage}")
    state[sym] = ast
    return changed


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
                changed = check_asset(asset, state) or changed
            except Exception as e:
                failures += 1
                log(f"{asset['symbol']}: check failed: {e}")
                RUN_STATUS.append(f"{asset['symbol']} error")
            time.sleep(FETCH_DELAY_S)
        new_cursor = stopped_at if stopped_at is not None else 0
        if meta.get("cursor", 0) != new_cursor:
            state["_meta"] = {"cursor": new_cursor}
            changed = True
        if changed or not STATE_FILE.exists():
            save_state(state)
        if failures:
            log(f"{failures} asset(s) failed this run - they retry next cycle.")
    finally:
        write_run_summary()


def seconds_to_next_close(buffer_s=5):
    period = MS["1m"] // 1000
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
                      "Strategy: 15m stochastic reversal zones \u2192 1m HA "
                      "sequence (doji + two strong candles + volume/PA "
                      "confirmation) \u2192 entry with pattern stop, 2R/3R "
                      "targets, breakeven after TP1, smoothed-HA runner exit.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
