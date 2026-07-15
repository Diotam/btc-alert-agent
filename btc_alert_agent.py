#!/usr/bin/env python3
"""
MULTI-TIMEFRAME SIGNAL ALERT AGENT - full trade lifecycle
----------------------------------------------------------
Pullback-continuation strategy across 4H / 1H / 30m / 5m.

HIGHER-TIMEFRAME BIAS (all four must hold; mirrored for shorts):
  B1  4H price above EMA200
  B2  1H EMA20 above EMA50
  B3  1H structure: higher highs & higher lows
  B4  Price above the daily (UTC) open

30M SETUP (at least SETUP_MIN of 8 must be true):
  S1  EMA20 above EMA50
  S2  Price above EMA200
  S3  Pullback toward EMA20, VWAP, or broken resistance
  S4  Pullback volume declining vs 20-period average
  S5  RSI cooled into ~48-62 (not pinned above 70)
  S6  MACD weakens on the pullback but hasn't collapsed (line still > 0)
  S7  Pullback holds above the previous structural low
  S8  At least 2R of unobstructed space before major resistance

5M TRIGGER (asset is ARMED; enter only on one of):
  T1  Bullish engulfing candle from support
  T2  Break and close above the pullback's lower high
  T3  Liquidity sweep below support followed by a reclaim
  T4  Strong bullish candle with rising volume after consolidation

Stop goes below the liquidity sweep or the structural low - never
arbitrarily beneath the entry candle. TP1 = 2R, TP2 = 3R, stop to
breakeven after TP1.

Phases per asset: IDLE -> ARMED -> IN_TRADE -> IDLE.
Run the workflow every 5 minutes; bias+setup re-checks every ~30
minutes, armed assets and open trades are watched on 5m candles.

Config from environment variables (GitHub repo Secrets):
  EMAIL_FROM / EMAIL_APP_PASSWORD / EMAIL_TO

Modes:
  python3 btc_alert_agent.py           single scan (workflow default)
  python3 btc_alert_agent.py --test    send a test email
  python3 btc_alert_agent.py --loop    run continuously (local PC)
"""

import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================= CONFIG ======================================
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# --- Asset universe -------------------------------------------------------
DISCOVER_ALL = True
DEXES = ["", "xyz"]                # "" = main crypto dex, "xyz" = TradeXYZ stocks
MIN_DAY_VOLUME_USD = 2_000_000     # skip illiquid markets below this 24h notional
MAX_ASSETS = 150                   # hard cap per run, highest-volume first
FETCH_DELAY_S = 0.12               # pause between per-asset API calls

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
ENABLE_SHORTS = True         # mirror the whole playbook for shorts
SETUP_MIN = 5                # of the 8 setup conditions
SETUP_REFRESH_MIN = 25       # re-check bias+setup roughly every 30m candle
ARM_TTL_MIN = 360            # an armed setup expires after 6h without a trigger
R_TP1, R_TP2 = 2.0, 3.0      # targets in R multiples of entry-to-stop risk
BREAKEVEN_AFTER_TP1 = True
PIVOT_WING = 3               # candles each side to confirm a swing point
LEVEL_TOL_ATR = 0.30         # cluster pivots within this many ATRs into a level
PULLBACK_ZONE_ATR = 0.50     # "near" EMA20/VWAP/level = within this many ATRs

TIMEZONE = "America/New_York"
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
# ===========================================================================

MS = {"5m": 300_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
LOOKBACK = {"5m": 300, "30m": 500, "4h": 260}
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
    armed = sum(1 for s in RUN_STATUS if "ARMED" in s)
    open_t = sum(1 for s in RUN_STATUS if "IN_TRADE" in s)
    if RUN_ALERTS:
        headline = "ALERT SENT: " + " | ".join(RUN_ALERTS)
    else:
        extras = []
        if armed:
            extras.append(f"{armed} armed: " + "; ".join(
                s for s in RUN_STATUS if "ARMED" in s)[:120])
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
               "User-Agent": "Mozilla/5.0 (signal-alert-agent/6.0)"}
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
    if interval == "4h":
        raise ValueError("yahoo has no 4h interval")
    yint = {"5m": "5m", "30m": "30m", "1h": "60m"}[interval]
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


def rsi(closes, period=14):
    out = [None] * len(closes)
    avg_gain = avg_loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain, loss = max(d, 0), max(-d, 0)
        if i <= period:
            avg_gain += gain / period
            avg_loss += loss / period
            if i == period:
                rs = 100 if avg_loss == 0 else avg_gain / avg_loss
                out[i] = 100 - 100 / (1 + rs)
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            rs = 100 if avg_loss == 0 else avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [ef[i] - es[i] if ef[i] is not None and es[i] is not None else None
            for i in range(len(closes))]
    sig_raw = ema([v if v is not None else 0 for v in line], signal)
    sig = [sig_raw[i] if line[i] is not None else None for i in range(len(line))]
    hist = [line[i] - sig[i] if line[i] is not None and sig[i] is not None else None
            for i in range(len(line))]
    return line, sig, hist


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


def vwap(candles):
    """Daily-anchored VWAP, reset each UTC day."""
    out = [None] * len(candles)
    cum_pv = cum_v = 0.0
    day = None
    for i, c in enumerate(candles):
        d = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).date()
        if d != day:
            day, cum_pv, cum_v = d, 0.0, 0.0
        tp = (c["h"] + c["l"] + c["c"]) / 3
        cum_pv += tp * c["v"]
        cum_v += c["v"]
        out[i] = cum_pv / cum_v if cum_v > 0 else None
    return out


def resample(candles, target_ms):
    """Aggregate candles into a larger timeframe aligned to target_ms."""
    buckets, order = {}, []
    for c in candles:
        b = c["t"] - (c["t"] % target_ms)
        if b not in buckets:
            buckets[b] = dict(t=b, o=c["o"], h=c["h"], l=c["l"], c=c["c"], v=c["v"])
            order.append(b)
        else:
            k = buckets[b]
            k["h"] = max(k["h"], c["h"])
            k["l"] = min(k["l"], c["l"])
            k["c"] = c["c"]
            k["v"] += c["v"]
    return [buckets[b] for b in order]


def find_pivots(candles, upto, wing=PIVOT_WING):
    highs, lows = [], []
    for i in range(wing, upto - wing + 1):
        h, l = candles[i]["h"], candles[i]["l"]
        if (all(h > candles[j]["h"] for j in range(i - wing, i)) and
                all(h >= candles[j]["h"] for j in range(i + 1, i + wing + 1))):
            highs.append((i, h))
        if (all(l < candles[j]["l"] for j in range(i - wing, i)) and
                all(l <= candles[j]["l"] for j in range(i + 1, i + wing + 1))):
            lows.append((i, l))
    return highs, lows


def build_levels(pivot_prices, tol):
    levels = []
    for p in sorted(pivot_prices):
        if levels and p - levels[-1][0] <= tol:
            price, n = levels[-1]
            levels[-1] = ((price * n + p) / (n + 1), n + 1)
        else:
            levels.append((p, 1))
    return levels


def daily_open(candles):
    """Open of the first candle of the current UTC day."""
    for c in reversed(candles):
        dt = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc)
        if dt.hour == 0 and dt.minute == 0:
            return c["o"]
    return None


# ------------------------- higher-timeframe bias ---------------------------
def htf_bias(direction, c4h, c30):
    """All four bias checks must pass. direction: 'LONG' or 'SHORT'."""
    long = direction == "LONG"
    checks = []

    closes4 = [c["c"] for c in c4h]
    e200_4h = ema(closes4, 200)
    i4 = len(c4h) - 2
    ok1 = (e200_4h[i4] is not None and
           (closes4[i4] > e200_4h[i4] if long else closes4[i4] < e200_4h[i4]))
    checks.append((f"4H price {'above' if long else 'below'} EMA200", ok1))

    c1h = resample(c30, MS["1h"])
    closes1 = [c["c"] for c in c1h]
    e20_1h, e50_1h = ema(closes1, 20), ema(closes1, 50)
    i1 = len(c1h) - 2
    ok2 = (e20_1h[i1] is not None and e50_1h[i1] is not None and
           (e20_1h[i1] > e50_1h[i1] if long else e20_1h[i1] < e50_1h[i1]))
    checks.append((f"1H EMA20 {'above' if long else 'below'} EMA50", ok2))

    highs, lows = find_pivots(c1h, i1)
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
    else:
        # too few confirmed swings (steady trend) - compare recent halves
        seg = c1h[max(0, i1 - 23):i1 + 1]
        half = len(seg) // 2
        early, late = seg[:half], seg[half:]
        hh = max(x["h"] for x in late) > max(x["h"] for x in early)
        hl = min(x["l"] for x in late) > min(x["l"] for x in early)
    ok3 = (hh and hl) if long else (not hh and not hl)
    checks.append(("1H higher highs & higher lows" if long
                   else "1H lower highs & lower lows", ok3))

    d_open = daily_open(c30)
    px = c30[-2]["c"]
    ok4 = d_open is not None and (px > d_open if long else px < d_open)
    checks.append((f"Price {'above' if long else 'below'} daily open"
                   + (f" (${fmt_px(d_open)})" if d_open else ""), ok4))

    return all(ok for _, ok in checks), checks


# ----------------------------- 30m setup -----------------------------------
def setup_30m(direction, c30):
    """Returns (passed_count, checks, context) - context holds the levels the
    5m trigger and stop placement need. Mirrored for shorts."""
    long = direction == "LONG"
    closes = [c["c"] for c in c30]
    vols = [c["v"] for c in c30]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes)
    m_line, _, m_hist = macd(closes)
    a = atr(c30)
    w = vwap(c30)
    vol_avg = sma(vols, 20)
    i = len(c30) - 2
    if i < 200 or a[i] is None:
        return 0, [], None
    px = closes[i]
    checks = []

    # S1 / S2 - trend alignment
    s1 = e20[i] > e50[i] if long else e20[i] < e50[i]
    checks.append((f"EMA20 {'above' if long else 'below'} EMA50", s1))
    s2 = px > e200[i] if long else px < e200[i]
    checks.append((f"Price {'above' if long else 'below'} EMA200", s2))

    # pullback geometry: recent extreme and the retrace from it
    window = c30[i - 11:i + 1]
    if long:
        ext = max(range(len(window)), key=lambda j: window[j]["h"])
        ext_px = window[ext]["h"]
        pulled = ext_px - px >= 0.75 * a[i] and ext <= len(window) - 3
    else:
        ext = min(range(len(window)), key=lambda j: window[j]["l"])
        ext_px = window[ext]["l"]
        pulled = px - ext_px >= 0.75 * a[i] and ext <= len(window) - 3

    # S3 - pullback into a meaningful zone (EMA20 / VWAP / broken level)
    highs, lows = find_pivots(c30, i)
    levels = build_levels([p for _, p in highs] + [p for _, p in lows],
                          LEVEL_TOL_ATR * a[i])
    zone_dists = [abs(px - e20[i])]
    if w[i]:
        zone_dists.append(abs(px - w[i]))
    broken = [lv for lv, _ in levels
              if (lv < px if long else lv > px)]  # levels price has crossed
    if broken:
        zone_dists.append(min(abs(px - lv) for lv in broken))
    s3 = pulled and min(zone_dists) <= PULLBACK_ZONE_ATR * a[i]
    checks.append(("Pullback into EMA20 / VWAP / broken level", s3))

    # S4 - pullback volume declining
    s4 = (vol_avg[i] is not None and vol_avg[i] > 0 and
          sum(vols[i - 2:i + 1]) / 3 < vol_avg[i])
    checks.append(("Pullback volume below 20-period average", s4))

    # S5 - RSI cooled into the healthy zone
    zone = (48, 62) if long else (38, 52)
    s5 = r[i] is not None and zone[0] <= r[i] <= zone[1]
    checks.append((f"RSI {r[i]:.1f} in {zone[0]}-{zone[1]} zone"
                   if r[i] is not None else "RSI unavailable", s5))

    # S6 - MACD weakens without collapsing
    recent = [h for h in m_hist[i - 9:i] if h is not None]
    if long:
        s6 = (recent and m_hist[i] is not None and m_line[i] is not None
              and m_hist[i] < max(recent) and m_line[i] > 0)
    else:
        s6 = (recent and m_hist[i] is not None and m_line[i] is not None
              and m_hist[i] > min(recent) and m_line[i] < 0)
    checks.append(("MACD easing, trend momentum intact", bool(s6)))

    # S7 - pullback holds above the previous structural low (below for shorts)
    if long:
        pb_ext = min(c["l"] for c in c30[i - 7:i + 1])
        prior = [p for idx, p in lows if idx < i - 7]
        s7 = bool(prior) and pb_ext > prior[-1]
        struct_ref = prior[-1] if prior else pb_ext
    else:
        pb_ext = max(c["h"] for c in c30[i - 7:i + 1])
        prior = [p for idx, p in highs if idx < i - 7]
        s7 = bool(prior) and pb_ext < prior[-1]
        struct_ref = prior[-1] if prior else pb_ext
    checks.append(("Pullback holds above prior structural low" if long
                   else "Rally holds below prior structural high", s7))

    # S8 - at least 2R of clear air before major opposing level
    est_stop = (pb_ext - 0.25 * a[i]) if long else (pb_ext + 0.25 * a[i])
    risk = abs(px - est_stop)
    if long:
        opposing = [lv for lv, n in levels if lv > px and n >= 2]
        s8 = not opposing or (min(opposing) - px) >= 2 * risk
    else:
        opposing = [lv for lv, n in levels if lv < px and n >= 2]
        s8 = not opposing or (px - max(opposing)) >= 2 * risk
    checks.append(("2R+ clear before major resistance" if long
                   else "2R+ clear before major support", s8))

    passed = sum(1 for _, ok in checks if ok)

    # pullback's lower high (higher low for shorts) - the T2 breakout line
    if long:
        counter = max(c["h"] for c in c30[i - 4:i + 1])
    else:
        counter = min(c["l"] for c in c30[i - 4:i + 1])

    ctx = {"structural": pb_ext, "counter": counter, "atr30": a[i],
           "est_stop": est_stop, "px": px}
    return passed, checks, ctx


# ------------------------------ 5m trigger ----------------------------------
def trigger_5m(direction, c5, armed):
    """Returns (trigger_name, entry, stop) or None. Mirrored for shorts."""
    long = direction == "LONG"
    i = len(c5) - 2
    if i < 25:
        return None
    a5_arr = atr(c5)
    a5 = a5_arr[i]
    if not a5:
        return None
    vols = [c["v"] for c in c5]
    vol_avg = sma(vols, 20)
    c, p = c5[i], c5[i - 1]
    px = c["c"]
    support = armed["structural"]
    counter = armed["counter"]
    pad = 0.2 * a5

    def stop_from(level):
        return level - pad if long else level + pad

    # T3 - liquidity sweep below support then reclaim (checked first: it
    # defines the tightest, most meaningful stop)
    sweep = None
    for k in range(max(0, i - 2), i + 1):
        if long and c5[k]["l"] < support:
            sweep = min(sweep, c5[k]["l"]) if sweep is not None else c5[k]["l"]
        if not long and c5[k]["h"] > support:
            sweep = max(sweep, c5[k]["h"]) if sweep is not None else c5[k]["h"]
    if sweep is not None and (px > support if long else px < support):
        return ("Liquidity sweep & reclaim of "
                f"${fmt_px(support)}", px, stop_from(sweep))

    near_support = (c["l"] <= support + 0.6 * a5 if long
                    else c["h"] >= support - 0.6 * a5)

    # T1 - engulfing from support
    if long and near_support and c["c"] > c["o"] and p["c"] < p["o"] \
            and c["c"] >= p["o"] and c["o"] <= p["c"]:
        return ("Bullish engulfing from support", px, stop_from(support))
    if not long and near_support and c["c"] < c["o"] and p["c"] > p["o"] \
            and c["c"] <= p["o"] and c["o"] >= p["c"]:
        return ("Bearish engulfing from resistance", px, stop_from(support))

    # T2 - break & close beyond the pullback's counter-swing
    if (px > counter if long else px < counter):
        return (f"Break & close {'above lower high' if long else 'below higher low'} "
                f"${fmt_px(counter)}", px, stop_from(support))

    # T4 - strong candle with rising volume after consolidation
    rng = c["h"] - c["l"]
    prior = c5[i - 6:i]
    consolidated = (max(x["h"] for x in prior) - min(x["l"] for x in prior)) <= 2.5 * a5
    vol_ok = vol_avg[i] and vols[i] > 1.5 * vol_avg[i]
    if rng >= 1.2 * a5 and consolidated and vol_ok:
        if long and c["c"] > c["o"] and c["c"] >= c["h"] - 0.25 * rng:
            return ("Strong bullish candle on rising volume", px, stop_from(support))
        if not long and c["c"] < c["o"] and c["c"] <= c["l"] + 0.25 * rng:
            return ("Strong bearish candle on rising volume", px, stop_from(support))
    return None


# ------------------------------- email ------------------------------------
STYLES = {
    "LONG":  {"accent": "#0FB98C", "soft": "#E6F7F1", "mark": "&#9650;"},
    "SHORT": {"accent": "#E8524A", "soft": "#FCEAE8", "mark": "&#9660;"},
    "TP1":   {"accent": "#0FB98C", "soft": "#E6F7F1", "mark": "&#10003;"},
    "TP2":   {"accent": "#0FB98C", "soft": "#E6F7F1", "mark": "&#10003;&#10003;"},
    "STOP":  {"accent": "#E8524A", "soft": "#FCEAE8", "mark": "&#10007;"},
}


def send_email(subject, body, html=None):
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))
    else:
        msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD.replace(" ", ""))
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())


def _html_shell(kind, headline_small, headline_big, headline_sub, rows_html,
                footer_text, extra_section=""):
    s = STYLES[kind]
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F2F5F7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F5F7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="max-width:480px;background:#FFFFFF;border-radius:12px;overflow:hidden;
              font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;">
  <tr><td style="background:{s['accent']};padding:22px 24px;">
    <div style="font-size:11px;letter-spacing:2px;color:rgba(255,255,255,.75);
                text-transform:uppercase;">{headline_small}</div>
    <div style="font-size:30px;font-weight:800;color:#FFFFFF;line-height:1.15;">
      {s['mark']}&nbsp;{headline_big}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.85);margin-top:4px;">{headline_sub}</div>
  </td></tr>
  <tr><td style="padding:20px 24px 8px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows_html}</table>
  </td></tr>
  {extra_section}
  <tr><td style="padding:16px 24px 22px;">
    <div style="background:{s['soft']};border-radius:8px;padding:12px 14px;
                font-size:11px;line-height:1.6;color:#5B6C7A;">{footer_text}</div>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def _row(label, value, color="#1A2530", bold=False):
    w = "700" if bold else "500"
    return (f'<tr><td style="padding:9px 0;border-bottom:1px solid #EDF1F4;'
            f'font-size:12px;color:#7A8B99;">{label}</td>'
            f'<td style="padding:9px 0;border-bottom:1px solid #EDF1F4;'
            f'font-size:14px;color:{color};font-weight:{w};text-align:right;'
            f'font-family:Menlo,Consolas,monospace;">{value}</td></tr>')


def _check_rows(title, checks):
    rows = "".join(
        f'<tr><td style="padding:6px 0;border-bottom:1px solid #EDF1F4;'
        f'font-size:12px;color:{"#0FB98C" if ok else "#C4CED6"};width:18px;">'
        f'{"&#10003;" if ok else "&#10007;"}</td>'
        f'<td style="padding:6px 0;border-bottom:1px solid #EDF1F4;font-size:12.5px;'
        f'color:{"#1A2530" if ok else "#9AAAB6"};">{desc}</td></tr>'
        for desc, ok in checks)
    return (f'<tr><td style="padding:12px 24px 4px;">'
            f'<div style="font-size:11px;letter-spacing:2px;color:#7A8B99;'
            f'text-transform:uppercase;margin-bottom:4px;">{title}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{rows}</table></td></tr>')


DISCLAIMER = ("Automated technical signal for research &mdash; not financial advice. "
              "Any single signal can fail; size accordingly.")
DISCLAIMER_TXT = ("Automated technical signal for research - not financial advice. "
                  "Any single signal can fail; size accordingly.")


def entry_email(asset, direction, trigger_name, plan, bias_checks, setup_checks,
                setup_count, source, t):
    sym = asset["symbol"]
    ts = fmt_ts(t)
    subject = (f"[{direction}] {sym} entry @ ${fmt_px(plan['entry'])} "
               f"- 5m trigger")
    rows = (_row("Trigger", trigger_name, bold=True)
            + _row("Entry", "$" + fmt_px(plan["entry"]), bold=True)
            + _row("Stop &middot; structure", "$" + fmt_px(plan["stop"]), "#E8524A", True)
            + _row(f"TP1 &middot; {R_TP1:.0f}R", "$" + fmt_px(plan["tp1"]), "#0FB98C", True)
            + _row(f"TP2 &middot; {R_TP2:.0f}R", "$" + fmt_px(plan["tp2"]), "#0FB98C", True)
            + _row("Risk (1R)", "$" + fmt_px(plan["r"])))
    extra = (_check_rows("Higher-timeframe bias (4/4)", bias_checks)
             + _check_rows(f"30m setup ({setup_count}/8)", setup_checks))
    html = _html_shell(direction,
                       f"{asset['label']} &middot; 4H/1H/30m/5m confluence",
                       f"{direction} &middot; ${fmt_px(plan['entry'])}",
                       f"{trigger_name} &nbsp;&middot;&nbsp; {ts}",
                       rows, f"Source: {source}. {DISCLAIMER}", extra)
    body = "\n".join([
        f"{sym} {direction} entry - multi-timeframe confluence",
        f"Trigger: {trigger_name}", f"Time: {ts}", f"Source: {source}", "",
        "TRADE PLAN (structure stop, R-based targets)",
        f"  Entry : ${fmt_px(plan['entry'])}",
        f"  Stop  : ${fmt_px(plan['stop'])}",
        f"  TP1   : ${fmt_px(plan['tp1'])}  ({R_TP1:.0f}R)",
        f"  TP2   : ${fmt_px(plan['tp2'])}  ({R_TP2:.0f}R)", "",
        "HTF BIAS (all passed)"] +
        [f"  [x] {d}" for d, _ in bias_checks] +
        ["", f"30M SETUP ({setup_count}/8)"] +
        [f"  [{'x' if ok else ' '}] {d}" for d, ok in setup_checks] +
        ["", DISCLAIMER_TXT])
    return subject, body, html


def lifecycle_email(asset, kind, trade, exit_px, event_t, note):
    sym = asset["symbol"]
    v = trade["verdict"]
    pl = pnl_pct(trade, exit_px)
    pl_s = f"{pl:+.2f}%"
    ts = fmt_ts(event_t)
    titles = {
        "TP1":  (f"[TP1 HIT] {sym} {v} {pl_s}", "TP1 HIT", "First target (2R) reached"),
        "TP2":  (f"[TP2 HIT] {sym} {v} {pl_s} - trade complete", "TP2 HIT",
                 "Final target (3R) reached &mdash; trade complete"),
        "STOP": (f"[STOPPED] {sym} {v} {pl_s}", "STOPPED OUT",
                 "Structure stop hit &mdash; trade closed"),
    }
    subject, big, sub = titles[kind]
    pl_color = "#0FB98C" if pl >= 0 else "#E8524A"
    rows = (_row("Direction", v)
            + _row("Entry", "$" + fmt_px(trade["entry"]))
            + _row("Exit level", "$" + fmt_px(exit_px), bold=True)
            + _row("P&amp;L (approx)", pl_s, pl_color, True)
            + _row("Opened", fmt_ts(trade["opened_t"], "%b %d %I:%M %p %Z")))
    html = _html_shell(kind, f"{asset['label']} &middot; trade update",
                       f"{big} &middot; {sym}", f"{sub} &nbsp;&middot;&nbsp; {ts}",
                       rows, f"{note} {DISCLAIMER}")
    body = "\n".join([
        f"{sym} {v} trade update: {big}", f"Time: {ts}",
        f"Entry: ${fmt_px(trade['entry'])}",
        f"Exit level: ${fmt_px(exit_px)}",
        f"P&L (approx): {pl_s}", "", note, "", DISCLAIMER_TXT])
    return subject, body, html


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
    return {"phase": "IDLE", "last_setup_check": 0, "armed": None, "trade": None}


# ------------------------------- agent ------------------------------------
def process_open_trade(asset, trade, candles, last_closed_t):
    """Stop/TP tracking on 5m candles."""
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
                    "Structure stop was hit. Wait for the next armed setup.")
            subject, body, html = lifecycle_email(asset, "STOP", trade,
                                                  trade["stop"], c["t"], note)
            send_email(subject, body, html)
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])} -> email sent")
            RUN_ALERTS.append(f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True

        if hit_tp2:
            subject, body, html = lifecycle_email(
                asset, "TP2", trade, trade["tp2"], c["t"],
                "Full 3R target reached. Trade closed.")
            send_email(subject, body, html)
            log(f"{sym}: TP2 HIT at ${fmt_px(trade['tp2'])} -> email sent")
            RUN_ALERTS.append(f"{sym} TP2 HIT ({pnl_pct(trade, trade['tp2']):+.2f}%)")
            return None, True

        if hit_tp1:
            trade["tp1_hit"] = True
            note = "First target (2R) reached."
            if BREAKEVEN_AFTER_TP1:
                trade["stop"] = trade["entry"]
                note += " Stop moved to breakeven - remaining position is risk-free."
            subject, body, html = lifecycle_email(asset, "TP1", trade,
                                                  trade["tp1"], c["t"], note)
            send_email(subject, body, html)
            log(f"{sym}: TP1 HIT at ${fmt_px(trade['tp1'])} -> email sent")
            RUN_ALERTS.append(f"{sym} TP1 HIT ({pnl_pct(trade, trade['tp1']):+.2f}%)")
            changed = True

        trade["checked_t"] = c["t"]

    if new_candles:
        trade["checked_t"] = last_closed_t
        changed = True
    return trade, changed


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
                ast["phase"] = "IDLE"
        RUN_STATUS.append(f"{sym} IN_TRADE" if ast["trade"] else f"{sym} IDLE")
        state[sym] = ast
        return changed

    # ---- ARMED: hunt the 5m trigger ---------------------------------------
    if ast["armed"]:
        armed = ast["armed"]
        direction = armed["direction"]
        long = direction == "LONG"
        expired = now_ms > armed["expires_t"]
        source, c5 = fetch(asset, "5m", 30)
        if c5 and not expired:
            px = c5[-2]["c"]
            broken = px < armed["structural"] if long else px > armed["structural"]
            if broken:
                log(f"{sym}: armed {direction} setup broke structure - disarmed")
                ast["armed"], ast["phase"] = None, "IDLE"
                changed = True
            else:
                trig = trigger_5m(direction, c5, armed)
                if trig:
                    name, entry, stop = trig
                    r = abs(entry - stop)
                    if r > 0:
                        sign = 1 if long else -1
                        plan = {"entry": entry, "stop": stop, "r": r,
                                "tp1": entry + sign * R_TP1 * r,
                                "tp2": entry + sign * R_TP2 * r}
                        subject, body, html = entry_email(
                            asset, direction, name, plan,
                            armed["bias_checks"], armed["setup_checks"],
                            armed["setup_count"], source, c5[-2]["t"])
                        send_email(subject, body, html)
                        log(f"ALERT SENT -> {EMAIL_TO}: {subject}")
                        RUN_ALERTS.append(f"{sym} {direction} entry @ ${fmt_px(entry)} ({name})")
                        ast["trade"] = {"verdict": direction, "entry": entry,
                                        "stop": stop, "tp1": plan["tp1"],
                                        "tp2": plan["tp2"], "tp1_hit": False,
                                        "opened_t": c5[-2]["t"],
                                        "checked_t": c5[-2]["t"]}
                        ast["armed"], ast["phase"] = None, "IN_TRADE"
                        changed = True
        elif expired:
            log(f"{sym}: armed {direction} setup expired without a trigger")
            ast["armed"], ast["phase"] = None, "IDLE"
            changed = True
        RUN_STATUS.append(f"{sym} {ast['phase']}"
                          + (f" ({direction})" if ast["phase"] == "ARMED" else ""))
        state[sym] = ast
        return changed

    # ---- IDLE: refresh bias + setup roughly every 30m ----------------------
    if now_ms - ast["last_setup_check"] < SETUP_REFRESH_MIN * 60_000:
        RUN_STATUS.append(f"{sym} IDLE")
        state[sym] = ast
        return changed

    ast["last_setup_check"] = now_ms
    changed = True
    src30, c30 = fetch(asset, "30m", 210)
    src4h, c4h = fetch(asset, "4h", 205)
    if not c30 or not c4h:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    directions = ["LONG"] + (["SHORT"] if ENABLE_SHORTS else [])
    for direction in directions:
        bias_ok, bias_checks = htf_bias(direction, c4h, c30)
        if not bias_ok:
            continue
        count, setup_checks, ctx = setup_30m(direction, c30)
        if ctx is None or count < SETUP_MIN:
            continue
        ast["armed"] = {"direction": direction,
                        "structural": ctx["structural"],
                        "counter": ctx["counter"],
                        "armed_t": now_ms,
                        "expires_t": now_ms + ARM_TTL_MIN * 60_000,
                        "setup_count": count,
                        "bias_checks": bias_checks,
                        "setup_checks": setup_checks}
        ast["phase"] = "ARMED"
        log(f"{sym}: ARMED {direction} - bias 4/4, setup {count}/8, "
            f"structure ${fmt_px(ctx['structural'])}, "
            f"breakout line ${fmt_px(ctx['counter'])}")
        break

    RUN_STATUS.append(f"{sym} {ast['phase']}"
                      + (f" ({ast['armed']['direction']})" if ast["armed"] else ""))
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
    log("Multi-timeframe alert agent started (loop mode). Ctrl+C to stop.")
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
    if not (EMAIL_FROM and EMAIL_APP_PASSWORD and EMAIL_TO):
        print("Missing config: set EMAIL_FROM, EMAIL_APP_PASSWORD and EMAIL_TO "
              "as environment variables (GitHub repo Secrets).")
        sys.exit(1)
    if "--test" in sys.argv:
        if DISCOVER_ALL:
            watched = (f"all Hyperliquid markets (crypto + xyz stocks) above "
                       f"${MIN_DAY_VOLUME_USD:,.0f} 24h volume, max {MAX_ASSETS}")
        else:
            watched = ", ".join(a["symbol"] for a in ASSETS)
        send_email("Signal alert agent - test email",
                   f"Your alert pipeline works. Watching: {watched}. "
                   "Strategy: 4H/1H bias -> 30m pullback setup (5 of 8) -> "
                   "5m trigger entries with structure stops, 2R/3R targets, "
                   "breakeven after TP1.")
        print(f"Test email sent to {EMAIL_TO}.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
