#!/usr/bin/env python3
"""
4-HOUR RANGE AGENT
------------------
One strategy, three steps:

  1. mark the high and low of the FIRST 4h candle of the New York day
     (00:00-04:00 NY), once that candle has fully closed
  2. on 5m, wait for a candle to CLOSE outside that range (wicks never
     count), then for a candle to CLOSE back inside - both on the same day
  3. broke the high -> SHORT, broke the low -> LONG.
     stop  = the exact extreme of the breakout excursion
     target = RANGE_RR x that distance (2R)

A huge breakout would put the stop far away, so when the excursion travels
more than RANGE_HUGE_FRACTION of the range width beyond the level, the stop
moves to the nearest key level inside it - or, failing that, to the broken
range level itself, which is now resistance (support for longs).

Everything resets at New York midnight. Alerts go to Telegram; orders can be
placed on Hyperliquid (EXEC_LIVE) with fixed dollar risk per trade.

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
import urllib.request
import re
import zlib
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================= CONFIG ======================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")



# --- Asset universe -------------------------------------------------------
DISCOVER_ALL = True
DISCOVER_DEXES = True            # scan builder venues (stocks live there)
ADMIT_COMMODITIES = True         # crypto, stocks and commodities              # scan HIP-3 builder dexes too - but only
                                   # commodity markets are admitted from them
DEXES = [""]                       # fallback when dex discovery fails
COMMODITY_TICKERS = ("XAU", "GOLD", "XAG", "SILVER", "XPT", "PLAT",
                     "XPD", "PALLAD", "CL", "OIL", "WTI", "BRENT",
                     "NG", "NATGAS", "HG", "COPPER")
COMMODITY_MIN_VOLUME_USD = 5_000_000   # commodities trade thinner - lower floor
STOCK_DEXES = ("xyz",)                 # TradeXYZ equities venue
STOCK_MIN_VOLUME_USD = 5_000_000
ONLY = []                          # trade ONLY these symbols ([] = whole universe)
EXCLUDE = ["PUMP"]                 # never trade these symbols - add coins here
                                   # (matches the base name on any venue)
MIN_DAY_VOLUME_USD = 5_000_000     # skip markets below $10M 24h notional
MAX_ASSETS = 70
FETCH_DELAY_S = 0.12
REQUEST_TIMEOUT_S = 8              # fail fast: a throttled API must not burn 20s
MAX_ZONES = 20                     # cap concurrently open reversal zones

ASSETS = [                         # used when DISCOVER_ALL = False / discovery fails
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- Strategy dials -------------------------------------------------------
TF = "5m"                     # execution timeframe: the spec is 5m closes
RANGE_TZ = "America/New_York"
# session windows per asset class (NY h:m start -> h:m end):
#   crypto & commodities: the first 4h of the NY day (overnight range)
#   stocks: the cash-session OPENING RANGE (equities are closed overnight,
#           so the 00-04 window would be a meaningless flat line)
# crypto/commodities take the FIRST 4-HOUR CANDLE of the NY day. Exchange 4h
# candles are UTC-aligned, so that is the first 4h boundary at or after NY
# midnight: 00:00-04:00 NY in EDT, 03:00-07:00 NY in EST. Stocks keep the
# cash-session opening range, which is not a 4h candle.
SESSIONS = {"crypto": (0, 0, 4, 0),
            "commodity": (0, 0, 4, 0),
            "stock": (9, 30, 10, 30)}
RR = 2.0                     # TP = 2 x the stop distance
RANGE_MIN_ATR = 0.30         # range narrower than this x ATR = untradeable day
# stop placement: the raw stop is the swing extreme of the run that faded.
#   SL_PAD_ATR  pushes it that much further away (breathing room for wicks)
#   SL_MIN_PCT  widens it to at least this % of price instead of skipping the
#               trade - leave at 0 to keep skipping via MIN_STOP_PCT
# ===========================================================================
# 4-HOUR RANGE STRATEGY - the only strategy
#   step 1  mark the high/low of the FIRST 4h candle of the NY day, once it
#           has fully closed (00:00-04:00 New York)
#   step 2  on 5m: a candle must CLOSE outside that range (wicks never count),
#           then price must CLOSE back inside - both on the same NY day
#   step 3  broke the high -> SHORT, broke the low -> LONG.
#           stop = the exact extreme of the breakout excursion
#           target = RANGE_RR x the stop distance
# A huge breakout would put the stop far away, so when the excursion stop is
# wider than RANGE_MAX_STOP_ATR the nearest key level inside it is used
# instead; if there is no such level the trade is skipped.
# ===========================================================================
RANGE_STRATEGY = True
RANGE_RR = 2.0                   # take profit = 2x the stop distance
RANGE_MIN_ATR = 0.30             # skip days whose range is narrower than this
RANGE_HUGE_FRACTION = 0.50       # a breakout that travels more than this
                                 # much of the range width beyond the level
                                 # counts as "huge"
RANGE_MAX_STOP_ATR = 3.00        # absolute backstop on stop width, x ATR
RANGE_KEY_LOOKBACK = 40          # candles searched for the nearest key level
RANGE_KEY_BUFFER_ATR = 0.10      # placed just beyond that level
RANGE_ONE_PER_SIDE = True        # one trade per side per day

MIN_STOP_PCT = 0.25              # skip entries whose stop sits closer than
                                 # this % of price - sub-noise stops just churn

                                 # NOTE: smoothed HA bodies are much smaller
                                 # than raw candles - 0.50 would qualify none
                                 # the candle's own body (scale-free: works on
                                 # smoothed HA, where wicks track the smoothed
                                 # high/low rather than the body)
# second pathway: while price trades INSIDE the range, a doji arms the same
# HA confirmation in either direction
# --- the only pathway: smoothed-HA trend-change pullback -------------------
                             # neither the session window nor the width guard
                             # applies - set True to trade only after the
                             # range window closes on a wide-enough range
# --- multi-timeframe engine ------------------------------------------------
# --- headlines for stock alerts --------------------------------------------
NEWS_ENABLED = True
NEWS_MAX = 2                 # headlines shown per alert
NEWS_TTL_S = 900             # cache per ticker
NEWS_RECENT_H = 6            # a headline this fresh gets a caution line
NEWS_URL = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
            "?s={t}&region=US&lang=en-US")

# legacy volume knobs - only read by the pre-v2 HA pathway, kept so that
# STRATEGY_V2 = False still runs instead of raising NameError

# --- pathway C: trend continuation (pullback inside an established trend) ---
                             # not a pullback (the other pathway handles those)
                                   # the EMA20 (an exact touch is too strict)
                                   # the slow series still with the trend, or
                                   # plain HA turned back (momentum flipped)
                                   # flipped AGAINST the trade inside the
                                   # window - that is a reversal, not a pullback
                                   # risk fits, tighten to the trigger candle
                                   # only when it does not

# ===========================================================================
# STRATEGY V2 - two clean pathways, three conditions each
#   CONTINUATION: trend (EMA20>EMA50) -> pullback to the EMAs -> reclaim candle
#   REVERSAL:     price stretched from EMA50 -> liquidity sweep -> reclaim
# Both: stop beyond the swing + buffer, TP 1.5R, half off then a trailing
# runner. One light 1h bias check, no multi-stage state machine.
# ===========================================================================
# --- execution (stage 1: DRY RUN - nothing is ever sent) -------------------
EXEC_DRY_RUN = True          # log the orders that WOULD be placed
EXEC_LIVE = True             # stage 2: place real orders. Leave False until
                             # the testnet run has filled correctly.
EXEC_TESTNET = False         # MAINNET - real money
EXEC_HALT_FILE = "/opt/btc-agent/EXEC_HALT"   # touch this to stop new entries
EXEC_DAILY_LOSS_LIMIT_USD = 40.0             # no new entries past this
EXEC_RISK_USD = 2.0          # deliberately tiny for the first live fills;
                             # raise once orders have proven correct
EXEC_MAX_NOTIONAL_USD = 2500 # cap on position value
EXEC_MAX_POSITIONS = 1       # one live position at a time to start
# ORDERS_LOG is defined next to trades.log further down

                                 # entry too far from its own invalidation
# rule 3: the confirming candle must carry participation
# rule 1: the higher timeframe decides WHICH pathway is allowed
                                 # above which the market counts as trending

ATR_PERIOD = 14
                                     # the structure break (48 x 5m = 4h)
                                     # candle - median ignores one snap-back
STOP_BUFFER_ATR = 0.15               # buffer under the retest low
MAX_STOP_ATR = 1.25                  # skip if the stop is wider than this
                             # when the smoothed HA flips against the trade
                                 # (smoothed HA: the flat dashes at the turns)
                                 # HA candles still have to confirm)
                                 # candles: red doji -> SHORT, green -> LONG

# stochastic gate - DOJI PATHWAY ONLY. Longs need oversold, shorts overbought,
# and the move into that extreme must be WEAK: never fade strong momentum.
STOCH_PERIOD, STOCH_SMOOTH = 14, 3
STOCH_OVERSOLD, STOCH_OVERBOUGHT = 20.0, 80.0
# momentum strength, offset baseline: average HA body over the MOM_LOOKBACK
# candles BEFORE the dojis vs the MOM_LOOKBACK before those. Weak when the
# recent run is smaller than the prior one. The doji candles themselves and
# the confirmation candles are excluded from the measurement.

                              # open trade - a same-direction signal is churn
OVERRIDE_ON_NEW_SIGNAL = True # a fresh qualifying reversal REPLACES the open
                              # trade on that symbol (closed at the new entry
                              # price and booked as OVERRIDE)
ENABLE_SHORTS = True

ALERT_ENTRIES = True
ALERT_STAGES = False         # pullback-armed alerts (log-only when False)
ALERT_LIFECYCLE = True       # TP / stop alerts

STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
TIMEZONE = "America/Chicago"
LOCAL_TZ = ZoneInfo(TIMEZONE)

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
      "4h": 14_400_000}

# knob tolerance: accept "30min", "15M", "1H", "60m" etc.
_TF_ALIASES = {"5min": "5m", "15min": "15m", "30min": "30m",
               "60m": "1h", "60min": "1h", "1hr": "1h"}
TF = _TF_ALIASES.get(TF.strip().lower(), TF.strip().lower())
if TF not in MS:
    raise SystemExit(f"CONFIG ERROR: TF={TF!r} is not a known timeframe - "
                     f"use one of {sorted(MS)}")
LOOKBACK = {"5m": 300, "15m": 400, "30m": 400, "1h": 500, "4h": 300}
# 1h must cover the 200 EMA with room to spare, or the permission
# check can never pass

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
RUN_UNIVERSE = [0]                 # [universe size] for the run summary


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
        headline = (f"No signal - {n} of {RUN_UNIVERSE[0] or n} markets scanned"
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
    yint = {"5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "60m"}.get(interval)
    if not yint:
        return None            # e.g. 4h - no equivalent, never fake it
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


def base_name(name):
    """Strip any venue prefix Hyperliquid already includes ('xyz:GOLD')
    and kGOLD-style multipliers, leaving the bare ticker."""
    return name.split(":")[-1].upper().lstrip("K")


def is_commodity(name):
    base = base_name(name)
    return any(base.startswith(t) for t in COMMODITY_TICKERS)


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
            name = u["name"]
            if base_name(name) in {base_name(x) for x in EXCLUDE}:
                continue
            if dex:
                if is_commodity(name) and not ADMIT_COMMODITIES:
                    continue
                if is_commodity(name):
                    if vol < COMMODITY_MIN_VOLUME_USD:
                        continue
                    cls = "commodity"
                elif dex in STOCK_DEXES:
                    if vol < STOCK_MIN_VOLUME_USD:
                        continue
                    cls = "stock"
                else:
                    continue                    # unknown venue class: skip
            else:
                if vol < MIN_DAY_VOLUME_USD:
                    continue
                cls = "crypto"
            coin = name if (":" in name or not dex) else f"{dex}:{name}"
            found.append({"symbol": coin, "hl_coin": coin, "vol": vol,
                          "cls": cls,
                          "lev": u.get("maxLeverage"),
                          "label": f"{base_name(name)}-PERP"
                                   + (f" ({dex})" if dex else ""),
                          "fallbacks": []})
    found.sort(key=lambda a: a["vol"], reverse=True)
    return found[:MAX_ASSETS]


def _not_excluded(a):
    return base_name(a["symbol"]) not in {base_name(x) for x in EXCLUDE}


def active_assets():
    if ONLY:
        picked = [a for a in ASSETS if a["symbol"] in ONLY] or ASSETS[:1]
        return [a for a in picked if _not_excluded(a)]
    if not DISCOVER_ALL:
        return [a for a in ASSETS if _not_excluded(a)]
    assets = discover_assets()
    if assets:
        crypto = sum(1 for a in assets if ":" not in a["symbol"])
        n_com = sum(1 for a in assets if a.get("cls") == "commodity")
        n_stk = sum(1 for a in assets if a.get("cls") == "stock")
        log(f"Discovered {len(assets)} markets: {crypto} crypto "
            f"(>= ${MIN_DAY_VOLUME_USD:,.0f}), {n_com} commodities, "
            f"{n_stk} stocks")
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


def atr(candles, period=None):
    period = period or ATR_PERIOD
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


def _smooth(vals, n):
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i + 1 - n):i + 1]
        out.append(None if len(w) < n or any(v is None for v in w)
                   else sum(w) / n)
    return out


def stochastic(candles, period=None, smooth=None):
    """Slow %K, aligned 1:1 with the candles."""
    p = period or STOCH_PERIOD
    s = smooth or STOCH_SMOOTH
    raw = []
    for i, c in enumerate(candles):
        if i + 1 < p:
            raw.append(None)
            continue
        w = candles[i + 1 - p:i + 1]
        hh = max(x["h"] for x in w)
        ll = min(x["l"] for x in w)
        raw.append(50.0 if hh == ll else (c["c"] - ll) / (hh - ll) * 100)
    return _smooth(raw, s)


def avg_vol(candles, lo, hi):
    vals = [c.get("v") or 0 for c in candles[max(0, lo):max(0, hi)]]
    vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else 0.0


def pivots(candles, wing=2):
    """(swing_high_indices, swing_low_indices) confirmed `wing` candles later."""
    hs, ls = [], []
    for j in range(wing, len(candles) - wing):
        h = candles[j]["h"]
        l = candles[j]["l"]
        if all(h > candles[j + k]["h"] and h > candles[j - k]["h"]
               for k in range(1, wing + 1)):
            hs.append(j)
        if all(l < candles[j + k]["l"] and l < candles[j - k]["l"]
               for k in range(1, wing + 1)):
            ls.append(j)
    return hs, ls


def _ema_list(vals, n):
    """EMA that tolerates leading Nones."""
    out, prev, k = [], None, 2.0 / (n + 1)
    for v in vals:
        if v is None:
            out.append(None)
            continue
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


NY_TZ = ZoneInfo(RANGE_TZ)


def ny_dt(ms):
    return datetime.fromtimestamp(ms / 1000, NY_TZ)


def window_ms(d, cls):
    """[start, end) epoch ms of the day's range window on NY date d.
    Stocks: the cash-session opening range. Everything else: the first
    4-hour CANDLE of the NY day (UTC-aligned, like the exchange chart)."""
    if cls == "stock":
        h1, m1, h2, m2 = SESSIONS["stock"]
        s = datetime(d.year, d.month, d.day, h1, m1, tzinfo=NY_TZ)
        e = datetime(d.year, d.month, d.day, h2, m2, tzinfo=NY_TZ)
        return int(s.timestamp() * 1000), int(e.timestamp() * 1000)
    midnight = int(datetime(d.year, d.month, d.day,
                            tzinfo=NY_TZ).timestamp() * 1000)
    step = MS["4h"]
    start = -(-midnight // step) * step        # first 4h boundary >= midnight
    return start, start + step


def day_range(candles, d, cls):
    """High/low of 5m candles inside the class's session window on NY
    date d. Returns (hi, lo, ready)."""
    start, end = window_ms(d, cls)
    step = MS[TF]
    hi = lo = None
    count = 0
    end_seen = False
    for c in candles:
        if start <= c["t"] and c["t"] + step <= end:
            hi = c["h"] if hi is None else max(hi, c["h"])
            lo = c["l"] if lo is None else min(lo, c["l"])
            count += 1
            if c["t"] + step == end:
                end_seen = True
    expected = max(1, (end - start) // step)
    ready = count >= expected - max(2, expected // 6) and end_seen
    return hi, lo, ready


def stage_message(asset, direction, level, px, t):
    e = "\U0001F534" if direction == "SHORT" else "\U0001F7E2"
    side = "high" if direction == "SHORT" else "low"
    return "\n".join([
        f"{e} <b>RANGE BREAK \u00b7 {esc(asset['symbol'])}</b>",
        f"5m closed {'above' if direction == 'SHORT' else 'below'} the "
        f"4h-range {side} (${fmt_px(level)}) - a close back inside "
        f"triggers the {direction}",
        f"<i>{esc(asset['label'])} \u00b7 {esc(fmt_ts(t))}</i>",
    ])


_NEWS_CACHE = {}


def parse_news(xml, limit=NEWS_MAX):
    """Titles + publish dates from an RSS document. Returns [(title, dt)]."""
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S)[:limit * 2]:
        m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                      item, re.S)
        if not m:
            continue
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        when = None
        d = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        if d:
            try:
                when = parsedate_to_datetime(d.group(1).strip())
            except Exception:
                when = None
        out.append((title, when))
        if len(out) >= limit:
            break
    return out


def news_for(symbol):
    """Recent headlines for a stock ticker. Never raises, never blocks long."""
    if not NEWS_ENABLED:
        return []
    ticker = base_name(symbol)
    hit = _NEWS_CACHE.get(ticker)
    if hit and time.time() - hit[0] < NEWS_TTL_S:
        return hit[1]
    items = []
    try:
        req = urllib.request.Request(
            NEWS_URL.format(t=ticker),
            headers={"User-Agent": "Mozilla/5.0 (signal-agent)"})
        with urllib.request.urlopen(req, timeout=6) as r:
            items = parse_news(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log(f"{symbol}: news lookup failed ({type(e).__name__})")
    _NEWS_CACHE[ticker] = (time.time(), items)
    return items


def news_block(asset):
    """The headline section for a stock alert, or '' for anything else."""
    if asset.get("cls") != "stock":
        return ""
    items = news_for(asset["symbol"])
    if not items:
        return ""
    now = datetime.now(timezone.utc)
    lines, fresh = [], False
    for title, when in items:
        age = ""
        if when:
            hrs = (now - when.astimezone(timezone.utc)).total_seconds() / 3600
            fresh = fresh or hrs <= NEWS_RECENT_H
            age = f" \u00b7 {hrs:.0f}h ago" if hrs >= 1 else " \u00b7 just now"
        lines.append(f"\u2022 {esc(title[:110])}{age}")
    head = "\U0001F4F0 <b>Headlines</b> <i>(Yahoo Finance)</i>"
    if fresh:
        head += "\n\u26A0\ufe0f <i>news in the last few hours - this move may " \
                "be event-driven, not technical</i>"
    return "\n\n" + head + "\n" + "\n".join(lines)


def entry_message(asset, direction, plan, hi, lo, source, t, trigger):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    cls = asset.get("cls", "crypto")
    s_ms, e_ms = window_ms(ny_dt(t).date(), cls)
    win = f"NY {ny_dt(s_ms):%H:%M}-{ny_dt(e_ms):%H:%M}"
    kind = "opening-range" if cls == "stock" else "first 4h candle"
    return "\n".join([
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 {kind} \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        f"\U0001F4CA <b>Setup</b>: " +
        (f"{win} range ${fmt_px(lo)} - ${fmt_px(hi)}; "
         if (hi is not None and lo is not None) else "") + f"{esc(trigger)}",
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>",
        f"TP:    <code>${fmt_px(plan['tp'])}</code>  ({RR:.0f}x the stop distance)",
        f"<i>data: {esc(source)}</i>",
    ]) + news_block(asset)


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    emoji, title, sub = {
        "TP": ("\u2705", "TAKE PROFIT HIT", f"{RR:.0f}R target reached"),
        "STOP": ("\u274C", "STOPPED OUT", "Stop level hit"),
        "TP_HALF": ("\u2705", "HALF CLOSED AT TARGET",
                    "half booked, stop moved to entry, runner live"),
        "RUNNER": ("\U0001F3C1", "RUNNER CLOSED",
                   "smoothed HA flipped - remaining half out"),
        "BE": ("\u26AA", "RUNNER STOPPED AT BREAKEVEN",
               "price returned to entry after the half close"),
        "OVERRIDE": ("\U0001F504", "TRADE REPLACED",
                     "closed early - a fresh range signal took over"),
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
ORDERS_LOG = Path(__file__).parent / "orders.log"


def already_closed(sym, trade, exit_px, kind):
    """True if an identical close (sym+dir+entry+exit+kind) is already in
    the ledger - makes duplicate close alerts structurally impossible."""
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
    """Append a closed trade to the ledger (best-effort). t_event = the
    actual market time of the exit, so late reconciliations book to the
    day they truly happened."""
    try:
        with open(TRADES_LOG, "a") as f:
            f.write(json.dumps({"t": int(t_event or time.time() * 1000), "sym": sym,
                                "dir": trade["verdict"],
                                "entry": trade["entry"], "exit": exit_px,
                                "kind": kind,
                                "frac": frac,
                                "pnl_pct": round(
                                    pnl_pct(trade, exit_px) * frac, 3)})
                    + "\n")
    except OSError:
        pass


def blank_asset_state():
    return {"phase": "SCAN", "last_candle_t": 0, "setup": None, "trade": None,
            "doji": None, "zone": None, "watch": None}


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
            if trade.get("half"):
                if ALERT_LIFECYCLE:
                    send_telegram(lifecycle_message(
                        asset, "BE", trade, trade["stop"], c_close_t, ""))
                log(f"{sym}: RUNNER STOPPED AT BREAKEVEN "
                    f"${fmt_px(trade['stop'])}")
                record_close(sym, trade, trade["stop"], "BE", c_close_t,
                             frac=0.5)
                RUN_ALERTS.append(f"{sym} runner stopped at breakeven")
                return None, True
            if already_closed(sym, trade, trade["stop"], "STOP"):
                log(f"{sym}: duplicate STOP close suppressed")
                return None, True
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "STOP", trade, trade["stop"], c_close_t, ""))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])}")
            record_close(sym, trade, trade["stop"], "STOP", c_close_t)
            RUN_ALERTS.append(
                f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True
        if tp_hit and not trade.get("half"):
            if trade.get("runner"):
                trade["half"] = True
                trade["stop"] = trade["entry"]
                if ALERT_LIFECYCLE:
                    send_telegram(lifecycle_message(
                        asset, "TP_HALF", trade, tp, c_close_t,
                        f"stop is now breakeven at ${fmt_px(trade['entry'])}; "
                        "the rest exits when the smoothed HA flips"))
                log(f"{sym}: HALF CLOSED at ${fmt_px(tp)}, stop -> breakeven")
                plan_manage_orders(asset, trade, "TP_HALF", tp)
                record_close(sym, trade, tp, "TP_HALF", c_close_t, frac=0.5)
                RUN_ALERTS.append(
                    f"{sym} half closed ({pnl_pct(trade, tp) * 0.5:+.2f}%)")
                return trade, True
            if already_closed(sym, trade, tp, "TP"):
                log(f"{sym}: duplicate TP close suppressed")
                return None, True
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "TP", trade, tp, c_close_t, ""))
            log(f"{sym}: TP HIT at ${fmt_px(tp)}")
            record_close(sym, trade, tp, "TP", c_close_t)
            RUN_ALERTS.append(f"{sym} TP HIT ({pnl_pct(trade, tp):+.2f}%)")
            return None, True



    # ---- intrabar check on the LIVE (still forming) candle ------------------
    # A fast move can blow through the stop mid-candle; don't wait for the
    # close to say so. checked_t is NOT advanced for the live candle.
    live = candles[-1]
    if live["t"] > last_closed_t:
        # the closed-candle loop may not have run this pulse, so this
        # section must never rely on its loop variables
        now_t = int(time.time() * 1000)
        stop_hit = live["l"] <= trade["stop"] if long else live["h"] >= trade["stop"]
        tp_hit = (live["h"] >= tp) if long else (live["l"] <= tp)
        if stop_hit:
            if trade.get("half"):
                if ALERT_LIFECYCLE:
                    send_telegram(lifecycle_message(
                        asset, "BE", trade, trade["stop"], now_t, ""))
                log(f"{sym}: RUNNER STOPPED AT BREAKEVEN "
                    f"${fmt_px(trade['stop'])}")
                record_close(sym, trade, trade["stop"], "BE", now_t,
                             frac=0.5)
                RUN_ALERTS.append(f"{sym} runner stopped at breakeven")
                return None, True
            if already_closed(sym, trade, trade["stop"], "STOP"):
                log(f"{sym}: duplicate STOP close suppressed")
                return None, True
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "STOP", trade, trade["stop"], int(time.time() * 1000),
                    "Intrabar - stop level traded before the candle closed."))
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])} (intrabar)")
            record_close(sym, trade, trade["stop"], "STOP")
            RUN_ALERTS.append(
                f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True
        if tp_hit and not trade.get("half"):
            if trade.get("runner"):
                trade["half"] = True
                trade["stop"] = trade["entry"]
                if ALERT_LIFECYCLE:
                    send_telegram(lifecycle_message(
                        asset, "TP_HALF", trade, tp, now_t,
                        f"stop is now breakeven at ${fmt_px(trade['entry'])}; "
                        "the rest exits when the smoothed HA flips"))
                log(f"{sym}: HALF CLOSED at ${fmt_px(tp)}, stop -> breakeven")
                plan_manage_orders(asset, trade, "TP_HALF", tp)
                record_close(sym, trade, tp, "TP_HALF", now_t, frac=0.5)
                RUN_ALERTS.append(
                    f"{sym} half closed ({pnl_pct(trade, tp) * 0.5:+.2f}%)")
                return trade, True
            if already_closed(sym, trade, tp, "TP"):
                log(f"{sym}: duplicate TP close suppressed")
                return None, True
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "TP", trade, tp, int(time.time() * 1000),
                    "Intrabar - target traded before the candle closed."))
            log(f"{sym}: TP HIT at ${fmt_px(tp)} (intrabar)")
            record_close(sym, trade, tp, "TP")
            RUN_ALERTS.append(f"{sym} TP HIT ({pnl_pct(trade, tp):+.2f}%)")
            return None, True
    return trade, changed


def exec_base_url():
    return ("https://api.hyperliquid-testnet.xyz" if EXEC_TESTNET
            else "https://api.hyperliquid.xyz")


_EXEC = {"ex": None, "info": None, "addr": None, "meta": None, "err": None}


def exec_client():
    """Lazily build the SDK client. Returns None (with a logged reason) if the
    SDK, the key, or the network is unavailable - never raises."""
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
            addr = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip() or \
                wallet.address
            base = exec_base_url()
            _EXEC.update(
                ex=Exchange(wallet, base, account_address=addr),
                info=Info(base, skip_ws=True), addr=addr)
            _EXEC["meta"] = _EXEC["info"].meta()
            log(f"execution client ready on "
                f"{'TESTNET' if EXEC_TESTNET else 'MAINNET'} for {addr[:10]}...")
        except Exception as e:
            _EXEC["err"] = f"{type(e).__name__}: {e}"
    if _EXEC["err"]:
        log(f"execution client unavailable ({_EXEC['err']}) - dry run only")
    return _EXEC["ex"]


def sz_decimals(sym):
    """Hyperliquid rejects sizes with too many decimals."""
    meta = _EXEC.get("meta") or {}
    for a in meta.get("universe", []):
        if a.get("name") == base_name(sym) or a.get("name") == sym:
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
        return f"{open_count} positions already open"
    if day_pnl_usd <= -abs(EXEC_DAILY_LOSS_LIMIT_USD):
        return f"daily loss limit hit ({day_pnl_usd:+.2f})"
    return None


def place_entry_live(asset, trade, plan):
    """Entry, then the protective stop, then the half TP. If the stop cannot
    be placed the position is closed immediately - never sit unprotected."""
    if not EXEC_LIVE:
        return None
    ex = exec_client()
    if not ex:
        return None
    # builder-venue markets (xyz:NBIS, ...) are not in the main perp meta the
    # SDK client was built against - it raises KeyError on the asset lookup.
    # Alert on them, but do not try to trade them.
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
    size = round(plan["size"], sz_decimals(asset["symbol"]))
    if size <= 0:
        log(f"{sym}: size rounds to zero - not sent")
        return None
    try:
        r = ex.market_open(sym, long_, size)
        log(f"{sym}: LIVE entry sent {r}")
    except Exception as e:
        log(f"{sym}: LIVE entry FAILED {type(e).__name__}: {e}")
        send_telegram(f"\u26a0\ufe0f {esc(sym)} entry order failed - no position")
        return None
    try:
        ex.order(sym, not long_, size, round_px(trade["stop"]),
                 {"trigger": {"triggerPx": round_px(trade["stop"]),
                              "isMarket": True, "tpsl": "sl"}},
                 reduce_only=True)
        ex.order(sym, not long_, round(size / 2, sz_decimals(asset["symbol"])),
                 round_px(trade["tp"]),
                 {"limit": {"tif": "Gtc"}}, reduce_only=True)
        log(f"{sym}: LIVE stop ${fmt_px(trade['stop'])} and half TP "
            f"${fmt_px(trade['tp'])} placed")
    except Exception as e:
        log(f"{sym}: LIVE protective orders FAILED ({type(e).__name__}) - "
            "closing the position")
        try:
            ex.market_close(sym)
            send_telegram(f"\u26a0\ufe0f {esc(sym)} stop could not be placed - "
                          "position closed immediately")
        except Exception:
            send_telegram(f"\U0001F6A8 {esc(sym)} UNPROTECTED POSITION - "
                          "close it manually now")
        return None
    return True


def log_order(rec):
    try:
        with open(ORDERS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log(f"orders.log write failed: {type(e).__name__}")


def plan_entry_orders(asset, trade, open_count=0):
    """What a live version WOULD send, sized by fixed dollar risk.
    Dry run only - nothing leaves this process."""
    sym = asset["symbol"]
    entry, stop, tp = trade["entry"], trade["stop"], trade["tp"]
    long_ = trade["verdict"] == "LONG"
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return None
    if open_count >= EXEC_MAX_POSITIONS:
        log(f"{sym}: DRY RUN - would SKIP, {open_count} positions already "
            f"open (max {EXEC_MAX_POSITIONS})")
        return None
    size = EXEC_RISK_USD / per_unit
    notional = size * entry
    capped = ""
    if notional > EXEC_MAX_NOTIONAL_USD:
        size = EXEC_MAX_NOTIONAL_USD / entry
        notional = EXEC_MAX_NOTIONAL_USD
        capped = f" [notional capped, risk now ${size * per_unit:.2f}]"
    rec = {"t": int(time.time() * 1000), "sym": sym, "mode": "dry-run",
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
               {"kind": "tp_half", "type": "limit", "reduce_only": True,
                "price": tp, "size": round(size / 2, 8)}]}
    if not EXEC_DRY_RUN:
        return rec                        # sized, but nothing logged
    log_order(rec)
    log(f"{sym}: DRY RUN - {rec['side']} {size:.6g} @ ${fmt_px(entry)} = "
        f"${notional:,.0f} notional, ${rec['risk_usd']:.2f} risk "
        f"({rec['stop_pct']}% stop); stop ${fmt_px(stop)}, TP ${fmt_px(tp)} "
        f"on half{capped}")
    return rec


def plan_manage_orders(asset, trade, event, price):
    if not EXEC_DRY_RUN:
        return
    action = {"TP_HALF": "half filled at TP -> amend stop to entry, keep runner",
              "RUNNER": "close remaining size at market",
              "BE": "stop filled at entry - flat",
              "STOP": "stop filled - flat"}.get(event)
    if not action:
        return
    log_order({"t": int(time.time() * 1000), "sym": asset["symbol"],
               "mode": "dry-run", "event": event, "price": price,
               "action": action})
    log(f"{asset['symbol']}: DRY RUN - {action}")


def fire_entry(asset, ast, direction, c, stop, hi, lo, source, trigger,
               rr=None, runner=False, atr_i=None):
    """Risk checks, override handling, alert and trade creation. Returns True
    if a trade was opened."""
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
    rr = rr or RR
    tp = entry - rr * risk if short else entry + rr * risk
    plan = {"entry": entry, "stop": stop, "tp": tp}
    event_t = c["t"] + MS[TF]

    old_trade = ast.get("trade")
    if old_trade  \
            and old_trade["verdict"] == direction:
        log(f"{sym}: {direction} signal matches the open trade's direction "
            "- not replacing it")
        ast["setup"], ast["doji"], ast["zone"] = None, None, None
        return False
    if old_trade:
        if ALERT_LIFECYCLE:
            send_telegram(lifecycle_message(
                asset, "OVERRIDE", old_trade, entry, event_t,
                f"replaced by a new {direction} range signal"))
        log(f"{sym}: trade REPLACED at ${fmt_px(entry)} by a fresh "
            f"{direction} signal")
        record_close(sym, old_trade, entry, "OVERRIDE", event_t)
        RUN_ALERTS.append(
            f"{sym} trade replaced ({pnl_pct(old_trade, entry):+.2f}%)")
        ast["trade"] = None

    if ALERT_ENTRIES:
        send_telegram(entry_message(asset, direction, plan, hi, lo, source,
                                    event_t, trigger))
    log(f"ALERT SENT -> telegram: {sym} {direction} ENTRY @ "
        f"${fmt_px(entry)} ({trigger})")
    RUN_ALERTS.append(f"{sym} {direction} entry @ ${fmt_px(entry)}")
    ast["trade"] = {"verdict": direction, "entry": entry, "stop": stop,
                    "tp": tp, "opened_t": c["t"], "checked_t": c["t"],
                    "rr": rr, "runner": bool(runner), "half": False,
                    "risk0": risk}          # original risk, kept for R maths
                                            # after the stop moves to breakeven
    open_now = sum(1 for v in STATE_VIEW.values()
                   if isinstance(v, dict) and v.get("trade"))
    plan = plan_entry_orders(asset, ast["trade"], open_count=open_now)
    if EXEC_LIVE and plan:
        # NOTE: the daily loss limit needs realised USD from the ledger, which
        # is not tracked yet - the halt file and position cap are enforced.
        why = exec_blocked(open_now, 0.0)
        if why:
            log(f"{asset['symbol']}: live order blocked - {why}")
        else:
            place_entry_live(asset, ast["trade"], plan)
    ast["phase"], ast["setup"] = "IN_TRADE", None
    ast["doji"], ast["zone"] = None, None
    return True


_HTF_CACHE = {}


def nearest_key_level(real, i, extreme, entry, long_):
    """The nearest swing pivot between entry and the excursion extreme - used
    when a huge breakout would otherwise put the stop miles away."""
    lo_i = max(0, i - RANGE_KEY_LOOKBACK)
    hs, ls = pivots(real[lo_i:i + 1])
    levels = []
    for j in (ls if long_ else hs):
        px = real[lo_i + j]["l"] if long_ else real[lo_i + j]["h"]
        # must sit between the entry and the excursion extreme
        if (extreme < px < entry) if long_ else (entry < px < extreme):
            levels.append(px)
    if not levels:
        return None
    return max(levels) if long_ else min(levels)


def process_candle(asset, ast, real, a, i, source, rng_cache):
    return process_candle_range(asset, ast, real, a, i, source, rng_cache)


def process_candle_range(asset, ast, real, a, i, source, rng_cache):
    """4h range -> close outside -> close back inside -> entry."""
    sym = asset["symbol"]
    c = real[i]
    atr_i = a[i] or 0
    if not atr_i:
        return False
    close_dt = ny_dt(c["t"] + MS[TF])
    d = close_dt.date()

    # a new NY day wipes everything: the range, the breakout, the done flags
    if ast.get("day") != str(d):
        if ast.get("setup"):
            log(f"{sym}: NY day rolled over - pending breakout cleared")
        ast["day"] = str(d)
        ast["setup"] = None
        ast["done"] = []

    cls = asset.get("cls", "crypto")
    if d not in rng_cache:
        rng_cache[d] = day_range(real, d, cls)
    hi, lo, ready = rng_cache[d]

    # step 1: the first 4h candle must have FULLY closed
    _, win_end = window_ms(d, cls)
    if c["t"] + MS[TF] <= win_end or not ready or hi is None:
        return False
    if (hi - lo) < RANGE_MIN_ATR * atr_i:
        return False                       # dead-flat range, not a range

    brk = ast.get("setup")

    # ---- step 2a: a candle CLOSES outside the range ----------------------
    if c["c"] > hi:
        if not brk or brk["side"] != "above":
            if "SHORT" in (ast.get("done") or []) and RANGE_ONE_PER_SIDE:
                return False
            ast["setup"] = {"side": "above", "level": hi,
                            "extreme": c["h"], "t": c["t"]}
            log(f"{sym}: closed ABOVE the 4h range (${fmt_px(hi)}) - watching "
                "for a close back inside")
        else:
            brk["extreme"] = max(brk["extreme"], c["h"])
        return True
    if c["c"] < lo:
        if not brk or brk["side"] != "below":
            if "LONG" in (ast.get("done") or []) and RANGE_ONE_PER_SIDE:
                return False
            ast["setup"] = {"side": "below", "level": lo,
                            "extreme": c["l"], "t": c["t"]}
            log(f"{sym}: closed BELOW the 4h range (${fmt_px(lo)}) - watching "
                "for a close back inside")
        else:
            brk["extreme"] = min(brk["extreme"], c["l"])
        return True

    if not brk:
        return False

    # ---- step 2b: price CLOSES back inside the range ---------------------
    if not (lo <= c["c"] <= hi):
        return True
    short = brk["side"] == "above"
    direction = "SHORT" if short else "LONG"
    entry = c["c"]
    stop = brk["extreme"]
    note = "stop at the breakout extreme"

    # huge breakout: fall back to the nearest key level inside the excursion
    risk = (stop - entry) if short else (entry - stop)
    # "huge" is judged against the RANGE, not the entry: the excursion is how
    # far price travelled beyond the level it broke
    excursion = (brk["extreme"] - hi) if short else (lo - brk["extreme"])
    huge = excursion > RANGE_HUGE_FRACTION * (hi - lo) or \
        risk > RANGE_MAX_STOP_ATR * atr_i
    if huge:
        key = nearest_key_level(real, i, brk["extreme"], entry, not short)
        src_txt = "nearest key level"
        if key is None:
            # no swing pivot inside the excursion - fall back to the level the
            # breakout failed at, which is now resistance (support for longs)
            key = hi if short else lo
            src_txt = "the broken range level (now resistance)" if short \
                else "the broken range level (now support)"
        stop = key + RANGE_KEY_BUFFER_ATR * atr_i if short \
            else key - RANGE_KEY_BUFFER_ATR * atr_i
        risk = (stop - entry) if short else (entry - stop)
        if risk <= 0:
            log(f"{sym}: huge breakout but price closed beyond the fallback "
                "level - skipped")
            ast["setup"] = None
            return True
        note = (f"huge breakout (travelled {excursion / (hi - lo):.0%} of the "
                f"range beyond the level) - stop moved to {src_txt} "
                f"${fmt_px(key)}")
        log(f"{sym}: {note}")
    if risk <= 0:
        ast["setup"] = None
        return True

    fire_entry(asset, ast, direction, c, stop, hi, lo, source,
               f"closed outside the 4h range then back inside; {note}",
               rr=RANGE_RR)
    if ast.get("trade"):
        ast.setdefault("done", []).append(direction)
    ast["setup"] = None
    return True


def check_asset(asset, state):
    sym = asset["symbol"]
    ast = state.get(sym) or blank_asset_state()
    for k, v in blank_asset_state().items():
        ast.setdefault(k, v)
    if asset.get("lev"):
        ast["lev"] = asset["lev"]          # max leverage, for the dashboard
    changed = False
    cs = None

    # ---- IN_TRADE: watch TP / stop ----------------------------------------
    if ast["trade"]:
        source, cs = fetch(asset, TF, 30 if not OVERRIDE_ON_NEW_SIGNAL else 300)
        if cs:
            trade, ch = process_open_trade(asset, ast["trade"], cs, cs[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        # exits win over overrides (process_open_trade ran first). Fall through
        # to the candle walk when the trade closed just now, or when overrides
        # are enabled and it is still open.
        if ast["trade"] and not OVERRIDE_ON_NEW_SIGNAL:
            RUN_STATUS.append(f"{sym} IN_TRADE")
            state[sym] = ast
            return changed

    # ---- scan: skip the fetch entirely when no new candle can exist --------
    # 61 markets re-fetched every 5m while candles close every 15m is what
    # triggers HTTP 429. A symbol with no open trade has nothing new to say
    # until its next candle closes.
    if cs is None and not ast["trade"]:
        boundary = (int(time.time() * 1000) // MS[TF]) * MS[TF] - MS[TF]
        if ast["last_candle_t"] >= boundary:
            RUN_STATUS.append(f"{sym} up to date")
            state[sym] = ast
            return changed

    if not cs:                             # not already fetched above
        source, cs = fetch(asset, TF, 300)
    if not cs:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    a = atr(cs)
    stoch_k = stochastic(cs)
    rng_cache = {}

    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        ch = process_candle(asset, ast, cs, a, i, source, rng_cache)
        changed = changed or ch
        ast["last_candle_t"] = cs[i]["t"]
        if ast["trade"] and ast["trade"].get("opened_t") == cs[i]["t"]:
            break                          # a new trade opened on this candle

    # an open trade always reports IN_TRADE, even when a fresh setup is armed
    # on the same symbol (the override candidate) - otherwise the run summary
    # undercounts open trades
    if ast["trade"]:
        stage = "IN_TRADE"
        if ast["setup"]:
            stage += f" +armed({ast['setup']['direction']})"
    elif ast["setup"]:
        stage = f"BROKE-{ast['setup']['side']} ({ast['setup']['direction']} on reentry)"
    else:
        stage = ast["phase"]
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


STATE_VIEW = {}


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
    rotated = assets[cursor:] + assets[:cursor]    # rotate for fairness
    # symbols holding an open trade go FIRST every run - an exit check must
    # never wait for the cursor to come around
    held = {a["symbol"] for a in rotated
            if (state.get(a["symbol"]) or {}).get("trade")}
    order = [a for a in rotated if a["symbol"] in held] + \
        [a for a in rotated if a["symbol"] not in held]
    stopped_at = None
    rot_done = 0                                   # non-priority assets done
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
                had_trade = bool((state.get(asset["symbol"]) or {}).get("trade"))
                changed = check_asset(asset, state) or changed
                has_trade = bool((state.get(asset["symbol"]) or {}).get("trade"))
                if had_trade != has_trade:
                    save_state(state)      # trade opened OR closed: persist NOW
                                           # (a restart must never resurrect it)
            except Exception as e:
                failures += 1
                log(f"{asset['symbol']}: check failed: {e}")
                RUN_STATUS.append(f"{asset['symbol']} error")
            time.sleep(FETCH_DELAY_S)
        # zombie sweep: open trades on symbols no longer in the universe
        # still get monitored - a trade must never go unwatched
        scanned_syms = {a["symbol"] for a in assets}
        for sym, ast in list(state.items()):
            if sym.startswith("_") or not isinstance(ast, dict):
                continue
            if ast.get("trade") and sym not in scanned_syms:
                ghost = {"symbol": sym, "hl_coin": sym,
                         "label": f"{sym}-PERP", "fallbacks": []}
                try:
                    changed = check_asset(ghost, state) or changed
                    if not (state.get(sym) or {}).get("trade"):
                        save_state(state)          # closed: persist NOW
                    log(f"{sym}: monitored outside the universe (open trade)")
                except Exception as e:
                    log(f"{sym}: zombie-trade check failed: {e}")
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
