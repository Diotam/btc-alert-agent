#!/usr/bin/env python3
"""
MULTI-TIMEFRAME SCALP SNIPER AGENT - tiered confluence, full lifecycle
-----------------------------------------------------------------------
Scalping playbook: 4H/1H/VWAP alignment -> 15m pullback -> 5m breakout.
Watches are opened BEFORE the breakout with resting-order levels, and
graded by setup quality.

SETUP CONFLUENCES (7 scored; mirrored for shorts):
  C1  4H trend agrees (price above EMA200)
  C2  1H EMA20 above EMA50
  C3  Price above daily VWAP
  C4  15m pullback into support / moving averages / VWAP
  C5  RSI(15m) in 50-65 (35-50 for shorts)
  C6  No major opposing level within 1R of the trigger
  C7  2R+ of clear air to the first major level (R:R >= 2:1 by design)

QUALITY TIERS:
  A+  all 7 met                  -> always alerted
  B   6 of 7 (one missing)       -> alerted if TIER_B_ALERTS = True
  Ignore  anything less          -> silent

TRIGGER: 5m break of the pullback's counter-swing. The watch alert
carries buy/sell-stop, stop, TP1 (2R), TP2 (3R) so a resting order can
sit on the exchange before the move. When the level breaks, the fill
alert reports whether 5m volume confirmed the breakout (above-average)
or flags a low-volume break as fade-prone.

RSI-extended gate: RSI > 72 (< 28 shorts) never opens a fresh watch -
the market is labeled EXTENDED and must retest first.

Lifecycle: TP1 (stop -> breakeven) / TP2 / STOPPED OUT, tracked on 5m.

Alerts are delivered to Telegram. Config from environment variables
(GitHub repo Secrets):
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Modes:
  python3 btc_alert_agent.py           single scan (workflow default)
  python3 btc_alert_agent.py --test    send a test message
  python3 btc_alert_agent.py --loop    run continuously (local PC -
                                       recommended for scalping: no lag)
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
MIN_DAY_VOLUME_USD = 10_000_000    # scalping needs liquidity: fees+slippage eat thin books
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
TIER_B_ALERTS = True         # also alert B setups (6 of 7); A+ always alerts
RSI_EXTENDED = 72            # RSI beyond this never opens a fresh watch
                             # (mirrored at 100-72=28 for shorts): the market is
                             # labeled EXTENDED and must retest before watching
EXTENDED_TTL_MIN = 360       # give an extended market up to 6h to retest
SETUP_REFRESH_MIN = 12       # re-scan the 15m setup roughly every 15m candle
WATCH_TTL_MIN = 120          # scalp watches go stale fast: 2h then cancel
PULLBACK_MIN_ATR = 0.60      # swing must sit at least this far above price
TRIGGER_PAD_ATR = 0.10       # trigger sits this far beyond the counter-swing
STOP_PAD_ATR = 0.15          # stop sits this far beyond the pullback extreme
VOL_CONFIRM_MULT = 1.30      # 5m breakout volume vs 20-avg to count as confirmed
R_TP1, R_TP2 = 2.0, 3.0      # targets in R multiples of entry-to-stop risk
BREAKEVEN_AFTER_TP1 = True
PIVOT_WING = 3               # candles each side to confirm a swing point
LEVEL_TOL_ATR = 0.30         # cluster pivots within this many ATRs into a level
PULLBACK_ZONE_ATR = 0.50     # "near" EMA20/VWAP/level = within this many ATRs

TIMEZONE = "America/New_York"
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
# ===========================================================================

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
LOOKBACK = {"5m": 300, "15m": 500, "30m": 500, "4h": 260}
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
    watching = sum(1 for s in RUN_STATUS if "WATCH" in s)
    open_t = sum(1 for s in RUN_STATUS if "IN_TRADE" in s)
    if RUN_ALERTS:
        headline = "ALERT SENT: " + " | ".join(RUN_ALERTS)
    else:
        extras = []
        if watching:
            extras.append(f"{watching} watching: " + "; ".join(
                s for s in RUN_STATUS if "WATCH" in s)[:120])
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


# ------------------------- scalp setup scoring ------------------------------
def scalp_setup(direction, c4h, c15):
    """Score the 7 scalp confluences on the last closed 15m candle.
    Returns (tier, checks, ctx) where tier is "A+", "B" or None.
    ctx carries trigger / stop / targets for the watch."""
    long = direction == "LONG"
    closes = [c["c"] for c in c15]
    a_arr = atr(c15)
    i = len(c15) - 2
    if i < 210 or a_arr[i] is None or len(c4h) < 205:
        return None, [], None
    a = a_arr[i]
    px = closes[i]
    checks = []

    # C1 - 4H trend agrees
    closes4 = [c["c"] for c in c4h]
    e200_4h = ema(closes4, 200)
    i4 = len(c4h) - 2
    c1 = (e200_4h[i4] is not None and
          (closes4[i4] > e200_4h[i4] if long else closes4[i4] < e200_4h[i4]))
    checks.append((f"4H trend agrees (price {'above' if long else 'below'} EMA200)", c1))

    # C2 - 1H EMA20 vs EMA50 (resampled from 15m)
    c1h = resample(c15, MS["1h"])
    cl1 = [c["c"] for c in c1h]
    e20_1h, e50_1h = ema(cl1, 20), ema(cl1, 50)
    i1 = len(c1h) - 2
    c2 = (i1 >= 0 and e20_1h[i1] is not None and e50_1h[i1] is not None and
          (e20_1h[i1] > e50_1h[i1] if long else e20_1h[i1] < e50_1h[i1]))
    checks.append((f"1H EMA20 {'above' if long else 'below'} EMA50", c2))

    # C3 - daily VWAP alignment
    w = vwap(c15)
    c3 = w[i] is not None and (px > w[i] if long else px < w[i])
    checks.append((f"Price {'above' if long else 'below'} daily VWAP"
                   + (f" (${fmt_px(w[i])})" if w[i] else ""), c3))

    # C4 - 15m pullback into support / MAs / VWAP
    e20_15 = ema(closes, 20)
    window = c15[i - 9:i + 1]
    highs, lows = find_pivots(c15, i)
    levels = build_levels([p for _, p in highs] + [p for _, p in lows],
                          LEVEL_TOL_ATR * a)
    if long:
        swing = max(c["h"] for c in window)
        pulled = swing - px >= PULLBACK_MIN_ATR * a
        pb_ext = min(c["l"] for c in c15[i - 5:i + 1])       # pullback low
        counter = max(c["h"] for c in c15[i - 3:i + 1])      # local counter-swing
    else:
        swing = min(c["l"] for c in window)
        pulled = px - swing >= PULLBACK_MIN_ATR * a
        pb_ext = max(c["h"] for c in c15[i - 5:i + 1])
        counter = min(c["l"] for c in c15[i - 3:i + 1])
    dists = [abs(px - e20_15[i])] if e20_15[i] is not None else []
    if w[i]:
        dists.append(abs(px - w[i]))
    zone_side = [lv for lv, _ in levels if (lv < px if long else lv > px)]
    if zone_side:
        dists.append(min(abs(px - lv) for lv in zone_side))
    c4 = pulled and dists and min(dists) <= PULLBACK_ZONE_ATR * a
    checks.append(("15m pullback into support / EMA20 / VWAP" if long
                   else "15m rally into resistance / EMA20 / VWAP", c4))

    # trigger / stop geometry (needed for C6, C7)
    sign = 1 if long else -1
    trigger = counter + sign * TRIGGER_PAD_ATR * a
    stop = pb_ext - sign * STOP_PAD_ATR * a
    risk = abs(trigger - stop)
    if risk <= 0 or (long and not stop < px < trigger + 2 * a) \
            or (not long and not stop > px > trigger - 2 * a):
        return None, checks, None

    # C5 - RSI in the scalp zone
    r = rsi(closes)
    zone = (50, 65) if long else (35, 50)
    c5 = r[i] is not None and zone[0] <= r[i] <= zone[1]
    checks.append((f"RSI {r[i]:.1f} in {zone[0]}-{zone[1]} zone"
                   if r[i] is not None else "RSI unavailable", c5))

    # C6 / C7 - room ahead, measured from the trigger
    if long:
        majors = sorted(lv for lv, n in levels if lv > trigger and n >= 2)
        near = majors[0] - trigger if majors else None
    else:
        majors = sorted((lv for lv, n in levels if lv < trigger and n >= 2),
                        reverse=True)
        near = trigger - majors[0] if majors else None
    c6 = near is None or near >= risk
    checks.append(("No major opposing level within 1R of the trigger", c6))
    c7 = near is None or near >= 2 * risk
    checks.append(("2R+ clear air to the first major level (R:R >= 2:1)", c7))

    passed = sum(1 for _, ok in checks if ok)
    tier = "A+" if passed == 7 else "B" if passed == 6 else None
    ctx = {"trigger": trigger, "stop": stop, "risk": risk,
           "tp1": trigger + sign * R_TP1 * risk,
           "tp2": trigger + sign * R_TP2 * risk,
           "px": px, "rsi": r[i], "atr": a, "tier": tier, "passed": passed,
           "checks": checks}
    return tier, checks, ctx


# --------------------- extended-market retest gate --------------------------
def is_extended(direction, rsi_val):
    if rsi_val is None:
        return False
    return rsi_val > RSI_EXTENDED if direction == "LONG" \
        else rsi_val < (100 - RSI_EXTENDED)


def retest_check(direction, candles):
    """After an EXTENDED reading: has price retested structure with
    contracting volume and returning confirmation? All three must hold."""
    long = direction == "LONG"
    closes = [c["c"] for c in candles]
    vols = [c["v"] for c in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    r = rsi(closes)
    a = atr(candles)
    w = vwap(candles)
    vol_avg = sma(vols, 20)
    i = len(candles) - 2
    if i < 200 or a[i] is None:
        return False, []
    px = closes[i]
    checks = []

    # 1 - price retested structure (EMA20 / VWAP / pivot level zone)
    highs, lows = find_pivots(candles, i)
    levels = build_levels([p for _, p in highs] + [p for _, p in lows],
                          LEVEL_TOL_ATR * a[i])
    dists = [abs(px - e20[i])]
    if w[i]:
        dists.append(abs(px - w[i]))
    if levels:
        dists.append(min(abs(px - lv) for lv, _ in levels))
    c1 = min(dists) <= PULLBACK_ZONE_ATR * a[i]
    checks.append(("Price retested structure (EMA20/VWAP/level)", c1))

    # 2 - volume contracted on the retest
    c2 = (vol_avg[i] is not None and vol_avg[i] > 0
          and sum(vols[i - 2:i + 1]) / 3 < vol_avg[i])
    checks.append(("Volume contracting", c2))

    # 3 - confirmation returned: RSI cooled + with-trend close + trend intact
    zone = (45, 68) if long else (32, 55)
    zone_ok = r[i] is not None and zone[0] <= r[i] <= zone[1]
    candle_ok = candles[i]["c"] > candles[i]["o"] if long else candles[i]["c"] < candles[i]["o"]
    trend_ok = e20[i] > e50[i] if long else e20[i] < e50[i]
    c3 = zone_ok and candle_ok and trend_ok
    checks.append((f"Confirmation returned (RSI "
                   f"{r[i]:.1f}, {'bullish' if long else 'bearish'} close, "
                   f"trend intact)" if r[i] is not None
                   else "Confirmation returned", c3))

    return all(ok for _, ok in checks), checks


# ----------------------------- watch monitor --------------------------------
def watch_check(direction, c5, watch):
    """While a breakout watch is live: did the trigger level get touched
    (= resting order fill), did the coil break down (cancel), or is heavy
    volume pressing into the trigger (surge nudge)?"""
    long = direction == "LONG"
    i = len(c5) - 2
    if i < 21:
        return None
    c = c5[i]
    # FILL: trigger level touched - matches a resting stop order.
    # Volume grading: above-average 5m volume = confirmed breakout.
    if (c["h"] >= watch["trigger"] if long else c["l"] <= watch["trigger"]):
        va = sma([x["v"] for x in c5], 20)[i]
        vol_ok = bool(va and c["v"] > VOL_CONFIRM_MULT * va)
        return ("FILL", watch["trigger"], c["t"], vol_ok)
    # CANCEL: closed through the protective side of the coil
    if (c["c"] < watch["stop"] if long else c["c"] > watch["stop"]):
        return ("CANCEL", c["c"], c["t"], None)
    # SURGE: one-time nudge - unusual volume pressing into the trigger
    if not watch.get("surged"):
        a5 = atr(c5)[i]
        vols = [x["v"] for x in c5]
        va = sma(vols, 20)[i]
        if a5 and va:
            press = ((watch["trigger"] - c["c"]) <= 0.3 * a5 if long
                     else (c["c"] - watch["trigger"]) <= 0.3 * a5)
            with_trend = c["c"] > c["o"] if long else c["c"] < c["o"]
            if press and with_trend and vols[i] > 2 * va:
                return ("SURGE", c["c"], c["t"], None)
    return None


# ----------------------------- telegram ------------------------------------
DISCLAIMER_TXT = ("Research signal - not financial advice. "
                  "Any single signal can fail; size accordingly.")


def esc(s):
    """Escape Telegram-HTML special characters in dynamic text."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send_telegram(text):
    resp = http_json(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True})
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram send failed: {resp.get('description')}")


def watch_message(asset, direction, ctx, source, t):
    sym = asset["symbol"]
    tier = ctx["tier"]
    badge = "\U0001F7E2 A+ SETUP" if tier == "A+" else "\U0001F7E1 B SETUP"
    order = "Buy-stop " if direction == "LONG" else "Sell-stop"
    lines = [
        f"{badge} \u00b7 <b>{esc(sym)} {direction}</b> \u2014 scalp watch",
        f"<i>{esc(asset['label'])} \u00b7 confluence {ctx['passed']}/7 "
        f"\u00b7 {esc(fmt_ts(t))}</i>",
        "",
        "\U0001F4CB <b>Sniper plan - place the resting order now:</b>",
        f"<code>{order}  ${fmt_px(ctx['trigger'])}</code>",
        f"<code>Stop      ${fmt_px(ctx['stop'])}</code>",
        f"<code>TP1       ${fmt_px(ctx['tp1'])}</code>  ({R_TP1:.0f}R)",
        f"<code>TP2       ${fmt_px(ctx['tp2'])}</code>  ({R_TP2:.0f}R)",
        f"<code>Risk      ${fmt_px(ctx['risk'])}</code>  (1R)",
        "",
        "\U0001F50D <b>Confluence</b>",
    ]
    lines += [f"{'\u2705' if ok else '\u274C'} {esc(d)}" for d, ok in ctx["checks"]]
    lines += ["",
              f"\u23F3 Watch valid ~{WATCH_TTL_MIN // 60}h. Fill alert will grade "
              "breakout volume. Cancel alert = pull the order.",
              f"<i>Source: {esc(source)}. \u26A0 {DISCLAIMER_TXT}</i>"]
    return "\n".join(lines)


def fill_message(asset, direction, watch, t, vol_ok):
    sym = asset["symbol"]
    grade = ("\u2705 Volume-confirmed breakout (above-average 5m volume)"
             if vol_ok else
             "\u26A0 <b>Low-volume break</b> - fade-prone, consider "
             "tightening or skipping")
    lines = [
        f"\U0001F680 <b>TRIGGERED \u00b7 {esc(sym)} {direction}</b> \u2014 "
        f"broke <code>${fmt_px(watch['trigger'])}</code>",
        f"<i>{esc(fmt_ts(t))}</i>",
        "",
        grade,
        "",
        "If your resting order filled, the plan is live:",
        f"<code>Entry  ${fmt_px(watch['trigger'])}</code>",
        f"<code>Stop   ${fmt_px(watch['stop'])}</code>",
        f"<code>TP1    ${fmt_px(watch['tp1'])}</code>  ({R_TP1:.0f}R)",
        f"<code>TP2    ${fmt_px(watch['tp2'])}</code>  ({R_TP2:.0f}R)",
        "",
        f"<i>\u26A0 {DISCLAIMER_TXT}</i>",
    ]
    return "\n".join(lines)


def surge_message(asset, direction, watch, px, t):
    sym = asset["symbol"]
    return "\n".join([
        f"\u26A1 <b>Pressure building \u00b7 {esc(sym)}</b>",
        f"Heavy 5m volume pressing into <code>${fmt_px(watch['trigger'])}</code> "
        f"(now <code>${fmt_px(px)}</code>). {esc(fmt_ts(t))}",
        "Breakout attempt may be imminent - resting order should be in place.",
    ])


def cancel_message(asset, direction, watch, px, reason, t):
    sym = asset["symbol"]
    return "\n".join([
        f"\U0001F6D1 <b>WATCH CANCELLED \u00b7 {esc(sym)} {direction}</b>",
        f"{esc(reason)} (price <code>${fmt_px(px)}</code>). {esc(fmt_ts(t))}",
        "",
        f"<b>Pull any resting {'buy' if direction == 'LONG' else 'sell'}-stop "
        f"at <code>${fmt_px(watch['trigger'])}</code>.</b>",
    ])


def extended_message(asset, direction, rsi_val, px, t):
    sym = asset["symbol"]
    lines = [
        f"\U0001F536 <b>EXTENDED \u00b7 {esc(sym)}</b> \u2014 RSI <code>{rsi_val:.1f}</code>",
        f"<i>{esc(asset['label'])} \u00b7 price <code>${fmt_px(px)}</code> \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        f"HTF bias and trend favor a {direction}, but momentum is too hot "
        f"(RSI &gt; {RSI_EXTENDED if direction == 'LONG' else 100 - RSI_EXTENDED}). "
        "Not chasing.",
        "",
        "\u23F3 <b>Waiting for retest:</b> price back into structure, "
        "volume contraction, and confirmation returning. "
        "You'll get a second alert if it sets up.",
    ]
    return "\n".join(lines)


def retest_armed_message(asset, direction, checks, px, structural, counter, t):
    sym = asset["symbol"]
    icon = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    lines = [
        f"\u2705 <b>RETEST CONFIRMED \u00b7 {esc(sym)}</b> \u2014 breakout watch {direction}",
        f"<i>{esc(asset['label'])} \u00b7 price <code>${fmt_px(px)}</code> \u00b7 {esc(fmt_ts(t))}</i>",
        "",
    ]
    lines += [f"\u2705 {esc(d)}" for d, _ in checks]
    lines += [
        "",
        f"{icon} Structure <code>${fmt_px(structural)}</code> \u00b7 "
        f"breakout line <code>${fmt_px(counter)}</code>",
        "Scanning for a coil - a breakout watch follows if one forms.",
    ]
    return "\n".join(lines)


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    sym = asset["symbol"]
    v = trade["verdict"]
    pl = pnl_pct(trade, exit_px)
    pl_s = f"{pl:+.2f}%"
    meta = {
        "TP1":  ("\u2705", "TP1 HIT", "First target (2R) reached"),
        "TP2":  ("\U0001F3C1", "TP2 HIT - trade complete", "Final target (3R) reached"),
        "STOP": ("\u274C", "STOPPED OUT", "Structure stop hit"),
    }[kind]
    icon, big, sub = meta
    lines = [
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
    ]
    return "\n".join(lines)


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
    return {"phase": "IDLE", "last_setup_check": 0, "watch": None,
            "trade": None, "extended": None}


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
                    "Structure stop was hit. Wait for the next breakout watch.")
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

    # ---- WATCH: monitor the coil for a fill / cancel / surge ---------------
    if ast.get("watch"):
        watch = ast["watch"]
        direction = watch["direction"]
        if now_ms > watch["expires_t"]:
            log(f"{sym}: breakout watch expired untriggered")
            send_telegram(cancel_message(asset, direction, watch, watch["px"],
                                         "Watch expired without a breakout",
                                         now_ms))
            RUN_ALERTS.append(f"{sym} watch expired - pull orders")
            ast["watch"], ast["phase"] = None, "IDLE"
            changed = True
        else:
            source, c5 = fetch(asset, "5m", 30)
            if c5:
                ev = watch_check(direction, c5, watch)
                if ev:
                    kind, px, t, vol_ok = ev
                    if kind == "FILL":
                        send_telegram(fill_message(asset, direction, watch, t, vol_ok))
                        log(f"ALERT SENT -> telegram: {sym} {direction} "
                            f"TRIGGERED @ ${fmt_px(watch['trigger'])}")
                        RUN_ALERTS.append(
                            f"{sym} {direction} TRIGGERED @ ${fmt_px(watch['trigger'])}"
                            + ("" if vol_ok else " (low-vol)"))
                        ast["trade"] = {"verdict": direction,
                                        "entry": watch["trigger"],
                                        "stop": watch["stop"],
                                        "tp1": watch["tp1"],
                                        "tp2": watch["tp2"],
                                        "tp1_hit": False,
                                        "opened_t": t, "checked_t": t}
                        ast["watch"], ast["phase"] = None, "IN_TRADE"
                    elif kind == "CANCEL":
                        send_telegram(cancel_message(
                            asset, direction, watch, px,
                            "Coil broke down through the stop side", t))
                        log(f"{sym}: watch cancelled - coil broke down")
                        RUN_ALERTS.append(f"{sym} watch cancelled - pull orders")
                        ast["watch"], ast["phase"] = None, "IDLE"
                    elif kind == "SURGE":
                        watch["surged"] = True
                        send_telegram(surge_message(asset, direction, watch, px, t))
                        log(f"{sym}: surge nudge sent")
                        RUN_ALERTS.append(f"{sym} pressure surge into trigger")
                    changed = True
        RUN_STATUS.append(f"{sym} {ast['phase']}"
                          + (f" (WATCH {direction})" if ast.get("watch") else ""))
        state[sym] = ast
        return changed

    # ---- EXTENDED / IDLE both refresh on the ~30m cadence -------------------
    if now_ms - ast["last_setup_check"] < SETUP_REFRESH_MIN * 60_000:
        RUN_STATUS.append(f"{sym} {ast['phase']}")
        state[sym] = ast
        return changed

    ast["last_setup_check"] = now_ms
    changed = True
    src15, c15 = fetch(asset, "15m", 215)
    src4h, c4h = fetch(asset, "4h", 205)
    if not c15 or not c4h:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    def open_watch(direction, ctx, source, t):
        ast["watch"] = {"direction": direction,
                        "trigger": ctx["trigger"], "stop": ctx["stop"],
                        "tp1": ctx["tp1"], "tp2": ctx["tp2"],
                        "px": ctx["px"], "risk": ctx["risk"],
                        "tier": ctx["tier"],
                        "expires_t": now_ms + WATCH_TTL_MIN * 60_000}
        ast["phase"] = "WATCH"
        send_telegram(watch_message(asset, direction, ctx, source, t))
        log(f"{sym}: {ctx['tier']} SCALP WATCH {direction} - "
            f"trigger ${fmt_px(ctx['trigger'])}, stop ${fmt_px(ctx['stop'])} "
            f"({ctx['passed']}/7)")
        RUN_ALERTS.append(f"{sym} {ctx['tier']} watch {direction} "
                          f"@ ${fmt_px(ctx['trigger'])}")

    # ---- EXTENDED: wait for the retest before opening a watch ---------------
    if ast["extended"]:
        ext = ast["extended"]
        direction = ext["direction"]
        if now_ms > ext["expires_t"]:
            log(f"{sym}: EXTENDED {direction} expired - back to IDLE")
            ast["extended"], ast["phase"] = None, "IDLE"
        else:
            ok, retest_checks = retest_check(direction, c15)
            tier, checks, ctx = scalp_setup(direction, c4h, c15)
            if ok and ctx is not None and tier is not None \
                    and not is_extended(direction, ctx["rsi"]) \
                    and (tier == "A+" or TIER_B_ALERTS):
                send_telegram(retest_armed_message(
                    asset, direction, retest_checks, ctx["px"],
                    ctx["stop"], ctx["trigger"], c15[-2]["t"]))
                log(f"{sym}: RETEST CONFIRMED -> scalp watch {direction}")
                RUN_ALERTS.append(f"{sym} retest confirmed - {tier} watch {direction}")
                ast["extended"] = None
                open_watch(direction, ctx, src15, c15[-2]["t"])
        RUN_STATUS.append(f"{sym} {ast['phase']}"
                          + (f" ({direction})" if ast["phase"] != "IDLE" else ""))
        state[sym] = ast
        return True

    # ---- IDLE: fresh tiered scan --------------------------------------------
    directions = ["LONG"] + (["SHORT"] if ENABLE_SHORTS else [])
    for direction in directions:
        tier, checks, ctx = scalp_setup(direction, c4h, c15)
        if ctx is None or tier is None:
            continue
        if is_extended(direction, ctx["rsi"]):
            ast["extended"] = {"direction": direction, "since": now_ms,
                               "expires_t": now_ms + EXTENDED_TTL_MIN * 60_000,
                               "rsi": ctx["rsi"]}
            ast["phase"] = "EXTENDED"
            send_telegram(extended_message(asset, direction, ctx["rsi"],
                                           ctx["px"], c15[-2]["t"]))
            log(f"{sym}: EXTENDED {direction} (RSI {ctx['rsi']:.1f})")
            RUN_ALERTS.append(f"{sym} EXTENDED (RSI {ctx['rsi']:.0f}) - wait for retest")
            break
        if tier == "B" and not TIER_B_ALERTS:
            continue
        open_watch(direction, ctx, src15, c15[-2]["t"])
        break

    RUN_STATUS.append(f"{sym} {ast['phase']}"
                      + (f" ({ast['watch']['direction']})" if ast.get("watch")
                         else f" ({ast['extended']['direction']})" if ast.get("extended")
                         else ""))
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
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Missing config: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "as environment variables (GitHub repo Secrets).")
        sys.exit(1)
    if "--test" in sys.argv:
        if DISCOVER_ALL:
            watched = (f"all Hyperliquid markets (crypto + xyz stocks) above "
                       f"${MIN_DAY_VOLUME_USD:,.0f} 24h volume, max {MAX_ASSETS}")
        else:
            watched = ", ".join(a["symbol"] for a in ASSETS)
        send_telegram("\u2705 <b>Signal alert agent - test message</b>\n"
                      f"Your alert pipeline works. Watching: {esc(watched)}.\n"
                      "Strategy: 4H/1H/VWAP alignment \u2192 15m pullback "
                      "\u2192 5m breakout, tiered \U0001F7E2 A+ / \U0001F7E1 B "
                      "watches with resting-order plans, volume-graded fills, "
                      "2R/3R targets, breakeven after TP1.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
