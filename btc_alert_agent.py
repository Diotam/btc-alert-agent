#!/usr/bin/env python3
"""
SMOOTHED-HA TREND-CHANGE AGENT
-------------------------------
One pathway. On smoothed Heikin Ashi (SHA_PRE/SHA_POST EMAs, TradingView
"Smoothed Ha Candles 10 10"):

  1. a strong run - same-colour HA candles whose bodies GROW, then FADE
  2. the HA flips colour: red -> green arms a LONG, green -> red a SHORT.
     No entry yet: those new HA candles become support (or resistance)
  3. price pulls back INTO the HA candles
  4. a candle closes in the trade's direction -> ENTER at that close
        SL = the swing extreme of the run that just ended
        TP = RR_TREND (1.5) x risk
  5. at TP: HALF is booked and the stop moves to entry; the runner exits
     when the smoothed HA flips against the trade

Setups are dropped if the HA turns back, price closes beyond the swing
extreme, or ZONE_TTL candles pass without a pullback. Entries also need a
stop at least MIN_STOP_PCT of price away.

The old 4h-range failed-breakout pathway is retired (RANGE_ENTRY = False);
its code is still present and can be switched back on, together with
RANGE_GATE if entries should be confined to the post-range session.

Exits carry intrabar detection. An opposite-direction signal replaces an
open trade; same-direction signals are ignored. Closes are recorded to
trades.log with a `frac` field so half-closes count as half.

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
MIN_DAY_VOLUME_USD = 10_000_000    # skip markets below $10M 24h notional
MAX_ASSETS = 70
FETCH_DELAY_S = 0.12
REQUEST_TIMEOUT_S = 8              # fail fast: a throttled API must not burn 20s

ASSETS = [                         # used when DISCOVER_ALL = False / discovery fails
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- Strategy dials -------------------------------------------------------
TF = "15m"                    # execution timeframe (the spec is 5m closes)
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
# stop placement: the raw stop is the swing extreme of the run that faded.
#   SL_PAD_ATR  pushes it that much further away (breathing room for wicks)
#   SL_MIN_PCT  widens it to at least this % of price instead of skipping the
#               trade - leave at 0 to keep skipping via MIN_STOP_PCT
SL_PAD_ATR = 0.25
SL_MIN_PCT = 0.0
MIN_STOP_PCT = 0.30              # skip entries whose stop sits closer than
                                 # this % of price - sub-noise stops just churn

# Heikin Ashi entry confirmation:
#   SHORT wants HA_CONFIRM_CANDLES consecutive LARGE bearish HA candles with
#   no UPPER wicks; LONG wants large bullish HA candles with no LOWER wicks.
HA_MODE = "smoothed"             # "smoothed" = TradingView Smoothed HA
SHA_PRE, SHA_POST = 6, 3         # smoothed HA settings
HA_CONFIRM_CANDLES = 2
                                 # NOTE: smoothed HA bodies are much smaller
                                 # than raw candles - 0.50 would qualify none
                                 # the candle's own body (scale-free: works on
                                 # smoothed HA, where wicks track the smoothed
                                 # high/low rather than the body)
# second pathway: while price trades INSIDE the range, a doji arms the same
# HA confirmation in either direction
# --- the only pathway: smoothed-HA trend-change pullback -------------------
RANGE_ENTRY = False          # pathway A (4h-range failed breakout) retired
RANGE_GATE = False           # the trend pathway does not use the 4h range, so
                             # neither the session window nor the width guard
                             # applies - set True to trade only after the
                             # range window closes on a wide-enough range
ZONE_TTL = 24                # candles the HA support/resistance stays valid
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
                             # the testnet run has filled correctly.
EXEC_RISK_USD = 2.0          # deliberately tiny for the first live fills;
                             # raise once orders have proven correct
EXEC_MAX_NOTIONAL_USD = 2500 # cap on position value
# ORDERS_LOG is defined next to trades.log further down

STRATEGY_V2 = True
V2_FAST, V2_SLOW = 20, 50        # 5m EMAs
V2_HTF, V2_HTF_EMA = "4h", 50    # higher-timeframe bias and regime
V2_HTF_REQUIRED = False          # True = 1h must agree, False = advisory only
V2_LOOKBACK = 8                  # candles a pullback / sweep may span
V2_PULLBACK_TOL = 0.35           # how close to the EMA counts as a pullback
V2_STRETCH_ATR = 2.0             # distance from EMA50 that counts as extended
V2_BODY_MIN = 0.45               # trigger candle body, x ATR
V2_BUFFER = 0.15                 # stop buffer, x ATR
V2_MAX_STOP = 2.50               # skip if the stop is wider, x ATR
V2_MAX_TRIGGER_RANGE = 1.30      # a trigger candle wider than this leaves the
                                 # entry too far from its own invalidation
V2_RR = 1.5
# rule 3: the confirming candle must carry participation
V2_VOL_GATE = True
V2_VOL_BASE = 20                 # bars in the volume average
V2_VOL_MULT = 1.20               # trigger candle volume vs that average
# rule 1: the higher timeframe decides WHICH pathway is allowed
V2_REGIME_ROUTING = True
V2_TREND_SLOPE = 0.40            # 1h EMA50 move over 6 bars, x 1h ATR,
                                 # above which the market counts as trending

HTF_TF, HTF_EMA = "1h", 200          # permission: price vs a flat/rising 200
MTF_FAST, MTF_SLOW = 20, 50
EMA5 = 20                            # 5m EMA used for breaks and the runner
ATR_PERIOD = 14
                                     # the structure break (48 x 5m = 4h)
                                     # candle - median ignores one snap-back
RR_TREND = 1.5               # pathway B target (pathway A stays at RR)
RUNNER_HALF_AT_TP = True     # half off at 1.5R, stop to breakeven, rest exits
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
MOM_LOOKBACK = 20

OVERRIDE_ONLY_OPPOSITE = True # only replace when the new signal REVERSES the
                              # open trade - a same-direction signal is churn
OVERRIDE_ON_NEW_SIGNAL = True # a fresh qualifying reversal REPLACES the open
                              # trade on that symbol (closed at the new entry
                              # price and booked as OVERRIDE)

ALERT_ENTRIES = True
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














# ------------------------------ heikin ashi --------------------------------
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


def smoothed_heikin_ashi(candles, pre=None, post=None):
    """TradingView-style Smoothed Heikin Ashi: EMA the OHLC, build HA from
    that, then EMA the HA series again. Warm-up candles are flagged so the
    gates ignore them."""
    p1 = pre or SHA_PRE
    p2 = post or SHA_POST
    eo, eh, el, ec = (_ema_list([c[key] for c in candles], p1)
                      for key in ("o", "h", "l", "c"))
    ho_l, hc_l, hh_l, hl_l = [], [], [], []
    prev_o = prev_c = None
    for i in range(len(candles)):
        if None in (eo[i], eh[i], el[i], ec[i]):
            ho_l.append(None), hc_l.append(None)
            hh_l.append(None), hl_l.append(None)
            continue
        hc = (eo[i] + eh[i] + el[i] + ec[i]) / 4
        ho = (eo[i] + ec[i]) / 2 if prev_o is None else (prev_o + prev_c) / 2
        ho_l.append(ho)
        hc_l.append(hc)
        hh_l.append(max(eh[i], ho, hc))
        hl_l.append(min(el[i], ho, hc))
        prev_o, prev_c = ho, hc
    so, sc, sh, sl = (_ema_list(x, p2) for x in (ho_l, hc_l, hh_l, hl_l))
    out = []
    warm = max(p1, p2) * 2
    for i, c in enumerate(candles):
        if None in (so[i], sc[i], sh[i], sl[i]) or i < warm:
            px = c["c"]
            out.append({"t": c["t"], "o": px, "c": px, "h": px, "l": px,
                        "warm": True})
            continue
        hi = max(sh[i], so[i], sc[i])
        lo = min(sl[i], so[i], sc[i])
        out.append({"t": c["t"], "o": so[i], "c": sc[i], "h": hi, "l": lo})
    return out


def ha_series(candles):
    """The HA series the strategy runs on."""
    return smoothed_heikin_ashi(candles) if HA_MODE == "smoothed" \
        else heikin_ashi(candles)


def heikin_ashi(candles):
    """Standard HA series, aligned 1:1 with the input candles."""
    out = []
    for c in candles:
        hc = (c["o"] + c["h"] + c["l"] + c["c"]) / 4
        ho = (c["o"] + c["c"]) / 2 if not out \
            else (out[-1]["o"] + out[-1]["c"]) / 2
        out.append({"t": c["t"], "o": ho, "c": hc,
                    "h": max(c["h"], ho, hc), "l": min(c["l"], ho, hc)})
    return out








# ----------------------------- 4h range engine ------------------------------
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
    kind = "smoothed HA" if not RANGE_ENTRY else \
        ("opening-range" if cls == "stock" else "first 4h candle")
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


def position_size_units(trade):
    """The unit size a live entry uses, reconstructed from the fixed-risk
    sizing so a closed trade can be booked in USD. Mirrors the old executor's
    exactly: risk / original-stop-distance, then the notional cap. Uses
    `risk0` (the risk at entry) so a stop later moved to breakeven does not
    corrupt the figure."""
    entry = trade.get("entry") or 0.0
    per_unit = trade.get("risk0")
    if not per_unit:                                 # legacy trade: fall back
        per_unit = abs(entry - trade.get("stop", entry))
    if not entry or per_unit <= 0:
        return 0.0
    size = EXEC_RISK_USD / per_unit
    if size * entry > EXEC_MAX_NOTIONAL_USD:
        size = EXEC_MAX_NOTIONAL_USD / entry
    return size


def realized_usd(trade, exit_px, frac=1.0):
    """Signed USD P&L of closing `frac` of the position at exit_px."""
    sign = 1 if trade["verdict"] == "LONG" else -1
    return sign * (exit_px - trade["entry"]) * position_size_units(trade) * frac




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
                                    pnl_pct(trade, exit_px) * frac, 3),
                                "pnl_usd": round(
                                    realized_usd(trade, exit_px, frac), 4)})
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
    ha_ex = ha_series(candles) if trade.get("runner") else None
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

        # MTF runner: trail under confirmed higher lows; exit on a close
        # through the last one, or two red HA candles plus a close past EMA20
        if trade.get("half") and trade.get("mtf"):
            n = next((x for x, cc in enumerate(candles) if cc["t"] == c["t"]),
                     None)
            if n is not None and n >= 3:
                long_ = long
                hs, ls = pivots(candles[:n + 1])
                swings = ls if long_ else hs
                if swings:
                    j = swings[-1]
                    lvl = candles[j]["l"] if long_ else candles[j]["h"]
                    better = (lvl > trade.get("hl", -1e18)) if long_ \
                        else (lvl < trade.get("hl", 1e18))
                    if better:
                        trade["hl"] = lvl
                        log(f"{sym}: trailing level -> ${fmt_px(lvl)}")
                broke = (c["c"] < trade.get("hl", -1e18)) if long_ \
                    else (c["c"] > trade.get("hl", 1e18))
                ha2 = ha_series(candles) if ha_ex is None else ha_ex
                e20l = _ema_list([x["c"] for x in candles], EMA5)
                two_red = False
                if n >= 1 and not ha2[n].get("warm"):
                    a1 = (ha2[n]["c"] < ha2[n]["o"]) if long_ \
                        else (ha2[n]["c"] > ha2[n]["o"])
                    a2 = (ha2[n - 1]["c"] < ha2[n - 1]["o"]) if long_ \
                        else (ha2[n - 1]["c"] > ha2[n - 1]["o"])
                    past = (c["c"] < e20l[n]) if (long_ and e20l[n]) else \
                        ((c["c"] > e20l[n]) if e20l[n] else False)
                    two_red = a1 and a2 and past
                if broke or two_red:
                    why = "closed below the trailing low" if broke else \
                        f"two red HA candles and a close past EMA{EMA5}"
                    if ALERT_LIFECYCLE:
                        send_telegram(lifecycle_message(
                            asset, "RUNNER", trade, c["c"], c_close_t, why))
                    log(f"{sym}: RUNNER OUT at ${fmt_px(c['c'])} ({why})")
                    record_close(sym, trade, c["c"], "RUNNER", c_close_t,
                                 frac=0.5)
                    RUN_ALERTS.append(
                        f"{sym} runner out "
                        f"({pnl_pct(trade, c['c']) * 0.5:+.2f}%)")
                    return None, True
                continue

        # runner: out when the smoothed HA flips against the trade
        if trade.get("half") and ha_ex is not None:
            n = next((x for x, cc in enumerate(candles) if cc["t"] == c["t"]),
                     None)
            if n is not None and not ha_ex[n].get("warm"):
                flipped = (ha_ex[n]["c"] < ha_ex[n]["o"]) if long \
                    else (ha_ex[n]["c"] > ha_ex[n]["o"])
                if flipped:
                    if ALERT_LIFECYCLE:
                        send_telegram(lifecycle_message(
                            asset, "RUNNER", trade, c["c"], c_close_t, ""))
                    log(f"{sym}: RUNNER OUT at ${fmt_px(c['c'])} (HA flipped)")
                    record_close(sym, trade, c["c"], "RUNNER", c_close_t,
                                 frac=0.5)
                    RUN_ALERTS.append(
                        f"{sym} runner out "
                        f"({pnl_pct(trade, c['c']) * 0.5:+.2f}%)")
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






















def fire_entry(asset, ast, direction, c, stop, hi, lo, source, trigger,
               rr=None, runner=False, atr_i=None):
    """Risk checks, override handling, alert and trade creation. Returns True
    if a trade was opened."""
    sym = asset["symbol"]
    short = direction == "SHORT"
    entry = c["c"]
    raw_stop = stop
    if SL_PAD_ATR and atr_i:                      # breathing room beyond the swing
        stop = stop + SL_PAD_ATR * atr_i if short else stop - SL_PAD_ATR * atr_i
    if SL_MIN_PCT and entry:                      # widen rather than skip
        need = entry * SL_MIN_PCT / 100
        stop = max(stop, entry + need) if short else min(stop, entry - need)
    if stop != raw_stop:
        log(f"{asset['symbol']}: stop widened ${fmt_px(raw_stop)} -> "
            f"${fmt_px(stop)} (pad {SL_PAD_ATR} ATR"
            + (f", min {SL_MIN_PCT}%" if SL_MIN_PCT else "") + ")")
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
    if old_trade and OVERRIDE_ONLY_OPPOSITE \
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
    ast["phase"], ast["setup"] = "IN_TRADE", None
    ast["doji"], ast["zone"] = None, None
    return True




HTF_CACHE_S = 900            # the 4h/1h read barely moves inside this window
HTF_RETRY_S = 120            # after a failure, retry sooner than that
HTF_MAX_REFRESH = 12         # cold-start guard: HTF reads allowed per run. The
                             # cache is in-memory, so every restart empties it
                             # and refetching all ~54 symbols at once is what
                             # trips the 429s
_HTF_CACHE = {}
_HTF_BUDGET = [0]            # refreshes used this run


def htf_context(asset):
    """Higher-timeframe read: (bias, regime). Cached per symbol - without
    this the extra fetches get rate-limited and routing silently turns off."""
    sym = asset["symbol"]
    hit = _HTF_CACHE.get(sym)
    good = hit[1] if (hit and hit[1][0]) else None
    if hit:
        age = time.time() - hit[0]
        ttl = HTF_CACHE_S if hit[1][0] else HTF_RETRY_S
        if age < ttl:
            return hit[1]
    # spread the refreshes out. Symbols that miss the budget keep whatever
    # they last had rather than dropping to "no routing"
    if _HTF_BUDGET[0] >= HTF_MAX_REFRESH:
        return good or (None, None)
    _HTF_BUDGET[0] += 1
    try:
        _, h = fetch(asset, V2_HTF, V2_HTF_EMA + 30)
        if not h or len(h) < V2_HTF_EMA + 12:
            if good:                       # a 429 is not a regime change
                log(f"{sym}: {V2_HTF} fetch failed - keeping the last "
                    f"read ({good[0]}/{good[1]})")
                _HTF_CACHE[sym] = (time.time(), good)
                return good
            log(f"{sym}: {V2_HTF} history too short "
                f"({0 if not h else len(h)} candles, need {V2_HTF_EMA + 12}) - "
                "bias and regime routing are OFF for this symbol")
            _HTF_CACHE[sym] = (time.time(), (None, None))
            return None, None
        e = _ema_list([c["c"] for c in h], V2_HTF_EMA)
        ah = atr(h)
        j = len(h) - 2
        if e[j] is None or e[j - 6] is None or not ah[j]:
            _HTF_CACHE[sym] = (time.time(), good or (None, None))
            return good or (None, None)
        bias = "LONG" if h[j]["c"] > e[j] else "SHORT"
        slope = abs(e[j] - e[j - 6]) / ah[j]
        out = bias, ("trend" if slope >= V2_TREND_SLOPE else "range")
        _HTF_CACHE[sym] = (time.time(), out)
        return out
    except Exception as e:
        if good:
            log(f"{sym}: {V2_HTF} context failed ({type(e).__name__}) - "
                "keeping the last read")
            _HTF_CACHE[sym] = (time.time(), good)
            return good
        log(f"{sym}: {V2_HTF} context failed ({type(e).__name__}) - "
            "routing off until the retry")
        _HTF_CACHE[sym] = (time.time(), (None, None))
        return None, None




def v2_continuation(real, a, ef, es, i, long_):
    """Trend -> pullback to the EMAs -> reclaim candle."""
    if i < V2_SLOW + V2_LOOKBACK or not a[i] or ef[i] is None or es[i] is None:
        return None
    c, prev, atr_i = real[i], real[i - 1], a[i]
    if (c["h"] - c["l"]) > V2_MAX_TRIGGER_RANGE * atr_i:
        return None
    trend = (ef[i] > es[i] and c["c"] > es[i]) if long_ \
        else (ef[i] < es[i] and c["c"] < es[i])
    if not trend:
        return None
    win = real[i - V2_LOOKBACK:i]
    tol = V2_PULLBACK_TOL * atr_i
    pulled = any((p["l"] <= ef[i - V2_LOOKBACK + k] + tol) if long_
                 else (p["h"] >= ef[i - V2_LOOKBACK + k] - tol)
                 for k, p in enumerate(win))
    if not pulled:
        return None
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    trig = ((c["c"] > c["o"]) if long_ else (c["c"] < c["o"])) and \
        ((c["c"] > ef[i]) if long_ else (c["c"] < ef[i])) and \
        ((c["c"] > prev["h"]) if long_ else (c["c"] < prev["l"])) and \
        body >= V2_BODY_MIN * atr_i and \
        (rng <= 0 or (((c["c"] - c["l"]) / rng if long_ else (c["h"] - c["c"]) / rng) >= 0.6))
    if not trig:
        return None
    near = real[max(0, i - 2):i + 1]          # the immediate swing only
    ext = min(p["l"] for p in near) if long_ else max(p["h"] for p in near)
    return ext, (f"pullback to the {TF} EMA{V2_FAST} in an EMA{V2_FAST}/"
                 f"{V2_SLOW} trend, reclaim candle {body / atr_i:.2f} ATR")


def v2_reversal(real, a, es, i, long_):
    """Stretched from the EMA50 -> sweep of the recent extreme -> reclaim."""
    if i < V2_SLOW + V2_LOOKBACK or not a[i] or es[i] is None:
        return None
    c, prev, atr_i = real[i], real[i - 1], a[i]
    if (c["h"] - c["l"]) > V2_MAX_TRIGGER_RANGE * atr_i:
        return None
    win = real[i - V2_LOOKBACK:i]
    # extended AWAY from value in the direction we are fading
    stretch = (es[i] - min(p["l"] for p in win + [c])) if long_ \
        else (max(p["h"] for p in win + [c]) - es[i])
    if stretch < V2_STRETCH_ATR * atr_i:
        return None
    # liquidity sweep: this candle took the window's extreme, then closed back
    prior = min(p["l"] for p in win) if long_ else max(p["h"] for p in win)
    swept = (c["l"] < prior) if long_ else (c["h"] > prior)
    if not swept:
        return None
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    reclaim = ((c["c"] > c["o"]) if long_ else (c["c"] < c["o"])) and \
        ((c["c"] > prior) if long_ else (c["c"] < prior)) and \
        body >= V2_BODY_MIN * atr_i and \
        (rng <= 0 or (((c["c"] - c["l"]) / rng if long_ else (c["h"] - c["c"]) / rng) >= 0.6))
    if not reclaim:
        return None
    ext = c["l"] if long_ else c["h"]
    return ext, (f"price {stretch / atr_i:.1f} ATR from the EMA{V2_SLOW}, swept "
                 f"the {V2_LOOKBACK}-candle {'low' if long_ else 'high'} and "
                 f"reclaimed it")


def v2_watch(real, a, ef, es, i, long_):
    """Context is in place but the trigger has not printed yet.
    Returns (pathway, note) or None."""
    if not a[i] or ef[i] is None or es[i] is None:
        return None
    c, atr_i = real[i], a[i]
    win = real[max(0, i - V2_LOOKBACK):i + 1]
    trend = (ef[i] > es[i] and c["c"] > es[i]) if long_ \
        else (ef[i] < es[i] and c["c"] < es[i])
    if trend:
        tol = V2_PULLBACK_TOL * atr_i
        near = (c["l"] <= ef[i] + tol) if long_ else (c["h"] >= ef[i] - tol)
        if near:
            # how close the close sits to the EMA - the reclaim fires from here
            gap = abs(c["c"] - ef[i]) / max(tol, 1e-12)
            return ("continuation",
                    f"trend intact, price back at the {TF} EMA{V2_FAST} - "
                    f"waiting for the reclaim candle",
                    max(10.0, min(95.0, 95.0 - 45.0 * gap)))
    stretch = (es[i] - min(p["l"] for p in win)) if long_ \
        else (max(p["h"] for p in win) - es[i])
    if stretch >= V2_STRETCH_ATR * atr_i:
        # the sweep is what is missing: how near price is to the extreme it
        # must take out
        prior = min(p["l"] for p in win[:-1]) if long_ \
            else max(p["h"] for p in win[:-1])
        away = (abs(c["c"] - prior) / atr_i) if atr_i else 9.0
        return ("reversal",
                f"{stretch / atr_i:.1f} ATR from the EMA{V2_SLOW} - waiting "
                f"for a sweep and reclaim",
                max(10.0, min(90.0, 90.0 - 60.0 * away)))
    return None


def process_candle_v2(asset, ast, real, a, i, source):
    before = ast.get("watch")
    def _touched(fired=False):
        """A changed watch must mark the state dirty, or it never reaches disk
        and the dashboard shows a frozen list."""
        now_w = ast.get("watch")
        key = lambda w: None if not w else (w.get("kind"), w.get("dir"),
                                            w.get("note"))
        return True if fired else key(before) != key(now_w)
    if ast.get("trade"):
        ast["watch"] = None          # a live trade is not a watch
        return _touched()
    sym = asset["symbol"]
    c, atr_i = real[i], a[i] or 0
    if not atr_i:
        return _touched()
    closes = [x["c"] for x in real]
    ef = _ema_list(closes, V2_FAST)
    es = _ema_list(closes, V2_SLOW)
    vols = [x.get("v") or 0 for x in real]
    vwin = [v for v in vols[max(0, i - V2_VOL_BASE):i] if v > 0]
    vavg = sum(vwin) / len(vwin) if vwin else 0.0
    # the mean is dragged up by a handful of panic bars, which are almost
    # always down candles - that punishes quiet rallies for no good reason.
    # the median is the fairer denominator for the participation test.
    _vs = sorted(vwin)
    vmed = 0.0 if not _vs else (
        _vs[len(_vs) // 2] if len(_vs) % 2
        else (_vs[len(_vs) // 2 - 1] + _vs[len(_vs) // 2]) / 2)
    bias, regime = htf_context(asset) if (V2_REGIME_ROUTING or V2_HTF_REQUIRED) \
        else (None, None)
    for long_ in (True, False):
        direction = "LONG" if long_ else "SHORT"
        for name, fn in (("continuation", v2_continuation),
                         ("reversal", v2_reversal)):
            # rule 1: trending markets get continuations (with the trend),
            # ranging / exhausted markets get reversals
            if V2_REGIME_ROUTING and regime:
                if regime == "trend" and name == "reversal":
                    continue
                if regime == "range" and name == "continuation":
                    continue
            hit = fn(real, a, es, i, long_) if name == "reversal" \
                else fn(real, a, ef, es, i, long_)
            if not hit:
                continue
            ext, detail = hit
            # rule 3: the confirming candle needs participation
            if V2_VOL_GATE and vmed and (c.get("v") or 0) < V2_VOL_MULT * vmed:
                log(f"{sym}: {direction} {name} but the confirming candle ran "
                    f"{(c.get('v') or 0) / vmed:.2f}x median volume "
                    f"(need {V2_VOL_MULT}x) - skipped")
                continue
            if regime:
                detail += f"; 1h {regime}"
            if bias and bias != direction:
                if V2_HTF_REQUIRED:
                    log(f"{sym}: {direction} {name} but the {V2_HTF} bias is "
                        f"{bias} - skipped")
                    continue
                detail += f" (against the {V2_HTF} bias)"
            stop = (ext - V2_BUFFER * atr_i) if long_ else (ext + V2_BUFFER * atr_i)
            risk = (c["c"] - stop) if long_ else (stop - c["c"])
            if risk <= 0:
                continue
            if risk > V2_MAX_STOP * atr_i:
                log(f"{sym}: {name} stop {risk / atr_i:.2f} ATR "
                    f"(max {V2_MAX_STOP}) - skipped")
                continue
            fire_entry(asset, ast, direction, c, stop, None, None, source,
                       f"{name} - {detail}", rr=V2_RR,
                       runner=RUNNER_HALF_AT_TP)
            if ast.get("trade"):
                ast["trade"]["mtf"] = True
                ast["trade"]["hl"] = ext
                ast["watch"] = None
            return True

    # nothing triggered - record what we are watching, for the dashboard
    ast["watch"] = None
    for long_ in (True, False):
        direction = "LONG" if long_ else "SHORT"
        w = v2_watch(real, a, ef, es, i, long_)
        if not w:
            continue
        kind, note, prox = w
        if V2_REGIME_ROUTING and regime:
            if regime == "trend" and kind == "reversal":
                continue
            if regime == "range" and kind == "continuation":
                continue
        ast["watch"] = {"kind": kind, "dir": direction, "note": note,
                        "regime": regime, "prox": round(prox), "t": c["t"]}
        break
    return _touched()






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

    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        ch = process_candle_v2(asset, ast, cs, a, i, source)
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
    _HTF_BUDGET[0] = 0
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
                # this symbol left the universe but still holds a trade, so it
                # gets scanned and appends a RUN_STATUS row. Count it in the
                # denominator too, or the summary reads "54 of 53 scanned".
                RUN_UNIVERSE[0] += 1
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


def debug_symbol(asset):
    """Print every v2 condition and its verdict for one symbol."""
    sym = asset["symbol"]
    tick = lambda ok: "\u2713" if ok else "\u2717"
    source, cs = fetch(asset, TF, 300)
    if not cs:
        print(f"\n=== {sym} === no {TF} candles (fetch failed)")
        return
    a = atr(cs)
    closes = [x["c"] for x in cs]
    ef = _ema_list(closes, V2_FAST)
    es = _ema_list(closes, V2_SLOW)
    i = len(cs) - 2                      # last CLOSED candle
    c, prev, atr_i = cs[i], cs[i - 1], a[i] or 0
    bias, regime = htf_context(asset)
    vols = [x.get("v") or 0 for x in cs]
    vwin = [v for v in vols[max(0, i - V2_VOL_BASE):i] if v > 0]
    vavg = sum(vwin) / len(vwin) if vwin else 0.0
    vr = (c.get("v") or 0) / vavg if vavg else 0.0
    rng = c["h"] - c["l"]
    body = abs(c["c"] - c["o"])

    print(f"\n=== {sym} === {source}  close ${fmt_px(c['c'])}  ATR {atr_i:.6g}")
    print(f"  {V2_HTF} bias {bias or '-'} / regime {regime or 'OFF'}"
          f"   EMA{V2_FAST} ${fmt_px(ef[i]) if ef[i] else '-'}"
          f"   EMA{V2_SLOW} ${fmt_px(es[i]) if es[i] else '-'}")
    print(f"  candle: body {body / atr_i if atr_i else 0:.2f} ATR "
          f"(need {V2_BODY_MIN}) | range {rng / atr_i if atr_i else 0:.2f} ATR "
          f"(max {V2_MAX_TRIGGER_RANGE}) | volume {vr:.2f}x (need {V2_VOL_MULT})")
    if not atr_i or ef[i] is None or es[i] is None:
        print("  not enough history for the EMAs / ATR")
        return

    win = cs[max(0, i - V2_LOOKBACK):i]
    tol = V2_PULLBACK_TOL * atr_i
    for long_ in (True, False):
        d = "LONG" if long_ else "SHORT"
        routed_out = ""
        if V2_REGIME_ROUTING and regime:
            if regime == "trend" and bias and bias != d:
                routed_out = f"(4h {regime}/{bias} blocks {d})"
        print(f"  -- {d} {routed_out}")

        trend = (ef[i] > es[i] and c["c"] > es[i]) if long_ \
            else (ef[i] < es[i] and c["c"] < es[i])
        pulled = any((p["l"] <= ef[i - V2_LOOKBACK + k] + tol) if long_
                     else (p["h"] >= ef[i - V2_LOOKBACK + k] - tol)
                     for k, p in enumerate(win))
        dirn = (c["c"] > c["o"]) if long_ else (c["c"] < c["o"])
        past = (c["c"] > ef[i]) if long_ else (c["c"] < ef[i])
        pastp = (c["c"] > prev["h"]) if long_ else (c["c"] < prev["l"])
        close_pos = ((c["c"] - c["l"]) / rng if long_ else (c["h"] - c["c"]) / rng) \
            if rng else 0
        allowed = not (V2_REGIME_ROUTING and regime == "range")
        print(f"     continuation: {tick(allowed)} routing "
              f"{tick(trend)} EMA stack  {tick(pulled)} pullback  "
              f"{tick(dirn)} direction  {tick(past)} past EMA{V2_FAST}  "
              f"{tick(pastp)} past prev  "
              f"{tick(body >= V2_BODY_MIN * atr_i)} body  "
              f"{tick(close_pos >= 0.6)} close {close_pos * 100:.0f}% of range  "
              f"{tick(vr >= V2_VOL_MULT or not vavg)} volume")

        stretch = (es[i] - min(p["l"] for p in win + [c])) if long_ \
            else (max(p["h"] for p in win + [c]) - es[i])
        prior = min(p["l"] for p in win) if long_ else max(p["h"] for p in win)
        swept = (c["l"] < prior) if long_ else (c["h"] > prior)
        reclaim = (c["c"] > prior) if long_ else (c["c"] < prior)
        allowed_r = not (V2_REGIME_ROUTING and regime == "trend")
        print(f"     reversal:     {tick(allowed_r)} routing "
              f"{tick(stretch >= V2_STRETCH_ATR * atr_i)} stretch "
              f"{stretch / atr_i:.1f} ATR (need {V2_STRETCH_ATR})  "
              f"{tick(swept)} swept ${fmt_px(prior)}  "
              f"{tick(reclaim)} reclaimed  "
              f"{tick(dirn)} direction  "
              f"{tick(body >= V2_BODY_MIN * atr_i)} body  "
              f"{tick(vr >= V2_VOL_MULT or not vavg)} volume")

    ast = {"phase": "SCAN", "last_candle_t": 0, "setup": None, "trade": None,
           "doji": None, "zone": None, "watch": None}
    for long_ in (True, False):
        w = v2_watch(cs, a, ef, es, i, long_)
        if w:
            print(f"  watch: {w[0]} {'LONG' if long_ else 'SHORT'} "
                  f"({w[2]:.0f}% proximity) - {w[1]}")


if __name__ == "__main__":
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Missing config: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "as environment variables (GitHub repo Secrets).")
        sys.exit(1)
    if "--debug" in sys.argv:
        want = [x.upper() for x in sys.argv[1:] if not x.startswith("--")]
        assets = active_assets()
        if want:
            assets = [a for a in assets
                      if a["symbol"].upper() in want
                      or base_name(a["symbol"]).upper() in want]
        else:
            assets = assets[:8]
        print(f"v2 debug: TF={TF} HTF={V2_HTF} routing={V2_REGIME_ROUTING} "
              f"vol={V2_VOL_MULT}x minstop={MIN_STOP_PCT}% maxstop={V2_MAX_STOP} ATR")
        for a in assets:
            try:
                debug_symbol(a)
            except Exception as e:
                print(f"\n=== {a['symbol']} === debug failed: {type(e).__name__}: {e}")
        sys.exit(0)

    if "--test" in sys.argv:
        if DISCOVER_ALL:
            watched = (f"all Hyperliquid markets above "
                       f"${MIN_DAY_VOLUME_USD:,.0f} 24h volume, max {MAX_ASSETS}")
        else:
            watched = ", ".join(a["symbol"] for a in ASSETS)
        send_telegram("\u2705 <b>Signal alert agent - test message</b>\n"
                      f"Your alert pipeline works. Watching: {esc(watched)}.\n"
                      f"Strategy: smoothed HA ({SHA_PRE}/{SHA_POST}) trend "
                      f"change on {TF} - fading run, colour flip, pullback to "
                      f"the HA candles, confirmation candle; SL at the swing "
                      f"extreme, TP {RR_TREND}R with half off and a runner.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
