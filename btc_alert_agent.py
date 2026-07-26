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
import zlib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================= CONFIG ======================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")



# --- Asset universe -------------------------------------------------------
DISCOVER_ALL = True
DISCOVER_DEXES = False              # scan HIP-3 builder dexes too - but only
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
RANGE_MIN_ATR = 0.30         # range narrower than this x ATR = untradeable day
MIN_STOP_PCT = 0.25              # skip entries whose stop sits closer than
                                 # this % of price - sub-noise stops just churn

# Heikin Ashi entry confirmation:
#   SHORT wants HA_CONFIRM_CANDLES consecutive LARGE bearish HA candles with
#   no UPPER wicks; LONG wants large bullish HA candles with no LOWER wicks.
HA_MODE = "smoothed"             # "smoothed" = TradingView Smoothed HA
SHA_PRE, SHA_POST = 10, 10       # the two EMA lengths ("Smoothed Ha Candles 10 10")
HA_CONFIRM_CANDLES = 2
HA_BODY_MIN_ATR = 0.25           # "large" = HA body at least this x ATR.
                                 # NOTE: smoothed HA bodies are much smaller
                                 # than raw candles - 0.50 would qualify none
HA_WICK_MAX_BODY = 0.25          # "no wick" = wick at most this fraction of
                                 # the candle's own body (scale-free: works on
                                 # smoothed HA, where wicks track the smoothed
                                 # high/low rather than the body)
# second pathway: while price trades INSIDE the range, a doji arms the same
# HA confirmation in either direction
HA_REQUIRE_FULL_BODY = False      # True = reject ANY wick, either end
# --- the only pathway: smoothed-HA trend-change pullback -------------------
RANGE_ENTRY = False          # pathway A (4h-range failed breakout) retired
RANGE_GATE = False           # the trend pathway does not use the 4h range, so
                             # neither the session window nor the width guard
                             # applies - set True to trade only after the
                             # range window closes on a wide-enough range
TREND_ENTRY = True
TREND_RUN_MIN = 4            # candles of one colour before the flip counts
TREND_GROW_MIN = 2           # bodies must grow this many times during the run
TREND_FADE_MIN = 2           # then shrink this many times before the flip
ZONE_TTL = 24                # candles the HA support/resistance stays valid
RR_TREND = 1.5               # pathway B target (pathway A stays at RR)
RUNNER_HALF_AT_TP = True     # half off at 1.5R, stop to breakeven, rest exits
                             # when the smoothed HA flips against the trade
DOJI_ENTRY = False
HA_DOJI_BODY_ATR = 0.05          # HA body this small counts as a doji
                                 # (smoothed HA: the flat dashes at the turns)
DOJI_TTL = 12                    # candles a doji stays valid
DOJI_COUNT = 1                   # dojis needed to arm (then the two large
                                 # HA candles still have to confirm)
DOJI_SAME_COLOR = True           # the doji must match the confirmation
                                 # candles: red doji -> SHORT, green -> LONG

# stochastic gate - DOJI PATHWAY ONLY. Longs need oversold, shorts overbought,
# and the move into that extreme must be WEAK: never fade strong momentum.
STOCH_GATE = True
STOCH_PERIOD, STOCH_SMOOTH = 14, 3
STOCH_OVERSOLD, STOCH_OVERBOUGHT = 20.0, 80.0
# momentum strength, offset baseline: average HA body over the MOM_LOOKBACK
# candles BEFORE the dojis vs the MOM_LOOKBACK before those. Weak when the
# recent run is smaller than the prior one. The doji candles themselves and
# the confirmation candles are excluded from the measurement.
MOM_LOOKBACK = 20
MOM_WEAK_RATIO = 1.0

OVERRIDE_ONLY_OPPOSITE = True # only replace when the new signal REVERSES the
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


def momentum_weak(ha, i, skip=0):
    """Offset baseline on HA bodies: the MOM_LOOKBACK candles ending BEFORE
    the signal against the MOM_LOOKBACK before those. `skip` pushes the window
    further back so the signal candles themselves are excluded - for the doji
    pathway that means the run is measured before the dojis, not through them.
    Returns (weak, detail)."""
    e = max(0, i - skip)
    recent = [abs(x["c"] - x["o"]) for x in ha[max(0, e - MOM_LOOKBACK):e]]
    prior = [abs(x["c"] - x["o"])
             for x in ha[max(0, e - 2 * MOM_LOOKBACK):max(0, e - MOM_LOOKBACK)]]
    if not recent or not prior:
        return True, "not enough history to judge momentum"
    r = sum(recent) / len(recent)
    p = sum(prior) / len(prior)
    return r < p * MOM_WEAK_RATIO, f"HA body avg {r:.4f} vs prior {p:.4f}"


def stoch_ok(k, ha, i, short, skip=0):
    """Doji-pathway gate: stochastic at the right extreme AND a weak run into
    it, measured before the signal candles. Returns (ok, why-not)."""
    if not STOCH_GATE:
        return True, ""
    kv = k[i] if i < len(k) else None
    if kv is None:
        return False, "no stochastic reading yet"
    if short and kv < STOCH_OVERBOUGHT:
        return False, f"%K {kv:.0f} is not above {STOCH_OVERBOUGHT:.0f}"
    if not short and kv > STOCH_OVERSOLD:
        return False, f"%K {kv:.0f} is not below {STOCH_OVERSOLD:.0f}"
    weak, detail = momentum_weak(ha, i, skip)
    if not weak:
        return False, (f"strong {'uptrend' if short else 'downtrend'} "
                       f"momentum ({detail})")
    return True, ""


def doji_label():
    return f"{DOJI_COUNT} doji" + ("s" if DOJI_COUNT != 1 else "")


def trend_flip(ha, a, i):
    """Did the smoothed HA just flip colour at candle i, after a run that grew
    then faded? Returns (direction, run_start) or (None, None).
    LONG = red run that faded, then turned green."""
    if i < TREND_RUN_MIN + 2 or ha[i].get("warm"):
        return None, None
    bull = ha[i]["c"] > ha[i]["o"]
    if (ha[i - 1]["c"] > ha[i - 1]["o"]) == bull:
        return None, None                      # no colour change here
    j = i - 1
    while j > 0 and not ha[j].get("warm") and \
            (ha[j]["c"] > ha[j]["o"]) != bull:
        j -= 1
    run = list(range(j + 1, i))                # the run that just ended
    if len(run) < TREND_RUN_MIN:
        return None, None
    bodies = [abs(ha[x]["c"] - ha[x]["o"]) for x in run]
    peak = bodies.index(max(bodies))
    grew = sum(1 for n in range(1, peak + 1) if bodies[n] > bodies[n - 1])
    faded = sum(1 for n in range(peak + 1, len(bodies))
                if bodies[n] < bodies[n - 1])
    if grew < TREND_GROW_MIN or faded < TREND_FADE_MIN:
        return None, None
    return ("LONG" if bull else "SHORT"), run[0]


def doji_run(ha, a, i, bear):
    """DOJI_COUNT consecutive dojis of the same colour, ending at i."""
    if i + 1 < DOJI_COUNT:
        return False
    for kk in range(DOJI_COUNT):
        h = ha[i - kk]
        if not ha_doji(h, a[i - kk]):
            return False
        if (h["c"] < h["o"]) != bear:
            return False
    return True


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


def ha_strong(hac, atr_i, short):
    if hac.get("warm"):
        return False
    """A LARGE HA candle with no wick on the trade side: shorts want a big
    bearish body with no upper wick, longs a big bullish body with no lower
    wick."""
    if not atr_i:
        return False
    if abs(hac["c"] - hac["o"]) < HA_BODY_MIN_ATR * atr_i:
        return False
    if short and hac["c"] >= hac["o"]:
        return False
    if not short and hac["c"] <= hac["o"]:
        return False
    body = abs(hac["c"] - hac["o"])
    tol = HA_WICK_MAX_BODY * body
    up = hac["h"] - max(hac["o"], hac["c"])
    dn = min(hac["o"], hac["c"]) - hac["l"]
    if up > tol and dn > tol:
        return False                  # wicks at BOTH ends: indecisive, skip it
    if HA_REQUIRE_FULL_BODY and (up > tol or dn > tol):
        return False
    return (up <= tol) if short else (dn <= tol)


def ha_confirmed(ha, a, i, short):
    """HA_CONFIRM_CANDLES consecutive strong candles ending at index i."""
    if i + 1 < HA_CONFIRM_CANDLES:
        return False
    return all(ha_strong(ha[i - k], a[i - k], short)
               for k in range(HA_CONFIRM_CANDLES))


def ha_doji(hac, atr_i):
    """Indecision: an HA body small relative to ATR."""
    if hac.get("warm"):
        return False
    return bool(atr_i) and abs(hac["c"] - hac["o"]) <= HA_DOJI_BODY_ATR * atr_i


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
    ])


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
            "doji": None, "zone": None}


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
        stop_hit = live["l"] <= trade["stop"] if long else live["h"] >= trade["stop"]
        tp_hit = (live["h"] >= tp) if long else (live["l"] <= tp)
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
                    asset, "TP", trade, tp, int(time.time() * 1000),
                    "Intrabar - target traded before the candle closed."))
            log(f"{sym}: TP HIT at ${fmt_px(tp)} (intrabar)")
            record_close(sym, trade, tp, "TP")
            RUN_ALERTS.append(f"{sym} TP HIT ({pnl_pct(trade, tp):+.2f}%)")
            return None, True
    return trade, changed


def fire_entry(asset, ast, direction, c, stop, hi, lo, source, trigger,
               rr=None, runner=False):
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


def process_candle(asset, ast, real, ha, a, k, i, source, rng_cache):
    """Walk ONE newly closed 5m candle through the 4h-range engine."""
    sym = asset["symbol"]
    c = real[i]
    close_dt = ny_dt(c["t"] + MS[TF])
    d = close_dt.date()

    # NY-day rollover: everything resets
    if ast.get("day") != str(d):
        if ast.get("setup"):
            log(f"{sym}: NY day rolled over - pending breakout cleared")
        ast["day"], ast["setup"], ast["doji"] = str(d), None, None

    cls = asset.get("cls", "crypto")
    if d not in rng_cache:
        rng_cache[d] = day_range(real, d, cls)
    hi, lo, ready = rng_cache[d]
    if RANGE_GATE:
        # only trade after the class's range window has fully closed
        _, win_end_ms = window_ms(d, cls)
        if c["t"] + MS[TF] <= win_end_ms:
            return False
        if not ready:
            return False
        # a range narrower than RANGE_MIN_ATR x ATR is noise, not a range
        if a[i] and (hi - lo) < RANGE_MIN_ATR * a[i]:
            return False
    if not RANGE_ENTRY or not ready:
        hi = lo = None                     # nothing to quote in the alert

    brk = ast["setup"] if RANGE_ENTRY else None
    if not RANGE_ENTRY and ast.get("setup"):
        ast["setup"] = None                # drop any setup left by pathway A

    # ---- closes OUTSIDE the range: register / extend the breakout ----------
    if RANGE_ENTRY and hi is not None and c["c"] > hi:
        if brk and brk["side"] == "HIGH":
            brk["ext"] = max(brk["ext"], c["h"])
            brk["reentered"] = False       # left the range again: reconfirm
        else:
            ast["setup"] = {"side": "HIGH", "direction": "SHORT",
                            "level": hi, "ext": c["h"], "reentered": False,
                            "note": f"closed above 4h-range high - a close "
                                    f"back inside triggers the SHORT"}
            log(f"{sym}: 5m closed ABOVE the 4h-range high ${fmt_px(hi)} - "
                "watching for reentry (SHORT on close back inside)")
            if ALERT_STAGES:
                send_telegram(stage_message(asset, "SHORT", hi, c["c"], c["t"]))
        return True
    if RANGE_ENTRY and lo is not None and c["c"] < lo:
        if brk and brk["side"] == "LOW":
            brk["ext"] = min(brk["ext"], c["l"])
            brk["reentered"] = False       # left the range again: reconfirm
        else:
            ast["setup"] = {"side": "LOW", "direction": "LONG",
                            "level": lo, "ext": c["l"], "reentered": False,
                            "note": f"closed below 4h-range low - a close "
                                    f"back inside triggers the LONG"}
            log(f"{sym}: 5m closed BELOW the 4h-range low ${fmt_px(lo)} - "
                "watching for reentry (LONG on close back inside)")
            if ALERT_STAGES:
                send_telegram(stage_message(asset, "LONG", lo, c["c"], c["t"]))
        return True

    # ---- pending breakout: reentry, then HA confirmation ------------------
    if RANGE_ENTRY and brk and lo <= c["c"] <= hi:
        short = brk["side"] == "HIGH"
        if not brk.get("reentered"):
            brk["reentered"] = True
            log(f"{sym}: closed back INSIDE the range - waiting for "
                f"{HA_CONFIRM_CANDLES} large HA candles with no "
                f"{'upper' if short else 'lower'} wicks")
        if ha_confirmed(ha, a, i, short):
            fire_entry(asset, ast, "SHORT" if short else "LONG", c,
                       brk["ext"], hi, lo, source,
                       f"{TF} closed beyond the "
                       f"{'high' if short else 'low'}, then back INSIDE - "
                       f"{HA_CONFIRM_CANDLES} large HA candles with no "
                       f"{'upper' if short else 'lower'} wicks confirmed it")
        return True

    # ---- pathway B: smoothed-HA trend change, pull back, then confirm -----
    if TREND_ENTRY and not brk:
        z = ast.get("zone")

        # a fresh flip arms (or re-arms) the zone
        direction, run_start = trend_flip(ha, a, i)
        if direction:
            long_ = direction == "LONG"
            swing = min(x["l"] for x in real[run_start:i + 1]) if long_ \
                else max(x["h"] for x in real[run_start:i + 1])
            ast["zone"] = {
                "dir": direction,
                "top": max(ha[i]["o"], ha[i]["c"]),
                "bot": min(ha[i]["o"], ha[i]["c"]),
                "swing": swing,
                "touched": False,
                "expires_t": c["t"] + ZONE_TTL * MS[TF],
            }
            log(f"{sym}: smoothed HA flipped "
                f"{'red->green' if long_ else 'green->red'} after a fading "
                f"{'down' if long_ else 'up'}trend - HA "
                f"{'support' if long_ else 'resistance'} armed at "
                f"${fmt_px(ha[i]['c'])}, waiting for a pullback")
            return True

        if not z:
            return False
        long_ = z["dir"] == "LONG"

        # the flip must still hold: HA turning back cancels it
        if (ha[i]["c"] < ha[i]["o"]) if long_ else (ha[i]["c"] > ha[i]["o"]):
            log(f"{sym}: HA turned back before the pullback traded - zone dropped")
            ast["zone"] = None
            return True
        if c["t"] > z["expires_t"]:
            log(f"{sym}: HA {'support' if long_ else 'resistance'} expired "
                "without a pullback")
            ast["zone"] = None
            return True
        # the zone rides with the HA candles
        z["top"] = max(ha[i]["o"], ha[i]["c"])
        z["bot"] = min(ha[i]["o"], ha[i]["c"])
        if (c["c"] < z["swing"]) if long_ else (c["c"] > z["swing"]):
            log(f"{sym}: closed beyond the swing "
                f"{'low' if long_ else 'high'} - zone invalidated")
            ast["zone"] = None
            return True

        # step 1: price pulls back INTO the HA candles
        reached = (c["l"] <= z["top"]) if long_ else (c["h"] >= z["bot"])
        if reached and not z["touched"]:
            z["touched"] = True
            log(f"{sym}: pulled back into the HA "
                f"{'support' if long_ else 'resistance'} - waiting for a "
                f"{'green' if long_ else 'red'} candle to confirm")
            return True

        # step 2: a candle in the trade's direction confirms the area held
        if z["touched"]:
            confirmed = (c["c"] > c["o"]) if long_ else (c["c"] < c["o"])
            if confirmed:
                fire_entry(asset, ast, z["dir"], c, z["swing"], hi, lo, source,
                           f"smoothed HA flipped, price pulled back to the HA "
                           f"{'support' if long_ else 'resistance'} and printed "
                           f"a {'green' if long_ else 'red'} candle",
                           rr=RR_TREND, runner=RUNNER_HALF_AT_TP)
                return True
        return True

    return False


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

    # ---- scan / armed: process each newly closed candle --------------------
    if not cs:                             # not already fetched above
        source, cs = fetch(asset, TF, 300) # ~25h of 5m: covers the NY day
    if not cs:
        RUN_STATUS.append(f"{sym} feed failed")
        state[sym] = ast
        return changed

    a = atr(cs)
    ha = ha_series(cs)
    stoch_k = stochastic(cs)
    rng_cache = {}

    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        ch = process_candle(asset, ast, cs, ha, a, stoch_k, i, source,
                            rng_cache)
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


def check_once():
    RUN_ALERTS.clear()
    RUN_STATUS.clear()
    state = load_state()
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
                      f"Strategy: smoothed HA ({SHA_PRE}/{SHA_POST}) trend "
                      f"change on {TF} - fading run, colour flip, pullback to "
                      f"the HA candles, confirmation candle; SL at the swing "
                      f"extreme, TP {RR_TREND}R with half off and a runner.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
