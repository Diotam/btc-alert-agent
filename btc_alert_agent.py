#!/usr/bin/env python3
"""
4-HOUR RANGE AGENT
------------------
One strategy, three steps:

  1. mark the high and low of the FIRST 4h CANDLE of the New York day, read
     straight off the exchange's own 4h candle, wicks included, once that
     candle has fully closed. Exchange 4h candles are UTC-aligned, so the
     candle that opens the NY day is 00:00-04:00 NY under EDT and
     03:00-07:00 NY under EST.
  2. on 5m, wait for a candle to CLOSE outside that range (wicks never
     count here), then for a candle to CLOSE back inside - both on the
     same NY day.
  3. broke the high -> SHORT, broke the low -> LONG.
     stop   = the exact extreme of the breakout excursion
     target = RANGE_RR x that distance, closed in full (no partials)

A huge breakout would put the stop far away, so when the excursion travels
more than RANGE_HUGE_FRACTION of the range width beyond the level, the stop
moves to the nearest swing pivot inside the excursion - or, failing that, to
the broken range level itself, which is now resistance (support for longs).

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
                                   # opening-range path stays wired up, so this
                                   # is one line to flip back
DEXES = [""]                       # fallback when dex discovery fails
COMMODITY_TICKERS = ("XAU", "GOLD", "XAG", "SILVER", "XPT", "PLAT",
                     "XPD", "PALLAD", "CL", "OIL", "WTI", "BRENT",
                     "NG", "NATGAS", "HG", "COPPER")
STOCK_DEXES = ("xyz",)             # TradeXYZ equities venue
MIN_DAY_VOLUME_USD = 2_000_000     # crypto floor, 24h notional
COMMODITY_MIN_VOLUME_USD = 5_000_000
STOCK_MIN_VOLUME_USD = 15_000_000
SESSIONS = {"stock": (9, 30, 10, 30)}   # equities use their cash-session
                                        # opening range, not a 4h candle
ONLY = []                          # trade ONLY these symbols ([] = whole universe)
EXCLUDE = ["PUMP"]                 # never trade these (matches the base name
                                   # on any venue)
MAX_ASSETS = 70

ASSETS = [                         # used when DISCOVER_ALL = False, or when
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",   # discovery fails
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- strategy dials -------------------------------------------------------
TF = "5m"                          # execution timeframe: the spec is 5m closes
RANGE_TZ = "America/New_York"
RANGE_RR = 2.0                     # take profit = 2x the stop distance
RANGE_MIN_ATR = 0.0                # minimum range width, x ATR. 0 = no filter:
                                   # whatever the two wicks are IS the range
RANGE_HUGE_FRACTION = 0.50         # an excursion travelling more than this much
                                   # of the range width beyond the broken level
                                   # counts as "huge" and retargets the stop
RANGE_MAX_STOP_ATR = 3.00          # absolute backstop on stop width, x ATR
RANGE_KEY_LOOKBACK = 40            # candles searched for the nearest key level
RANGE_KEY_BUFFER_ATR = 0.10        # fallback stop sits this far beyond the level
RANGE_ONE_PER_SIDE = False         # False = unlimited entries per side per day,
                                   # as long as the setup re-forms against the
                                   # same range. `done` still records which
                                   # sides traded today, for the state file
MIN_STOP_PCT = 0.25                # skip entries whose stop sits closer than
                                   # this % of price - sub-noise stops just churn
ATR_PERIOD = 14
OVERRIDE_ON_NEW_SIGNAL = False     # False: a symbol holding a trade is not
                                   # evaluated for new setups until it exits.
                                   # True replaced the open trade at market and
                                   # booked an OVERRIDE - but that path never
                                   # closed the live position or cancelled the
                                   # resting stop and TP, so it left the
                                   # exchange and the state file disagreeing

# --- alerts ---------------------------------------------------------------
ALERT_ENTRIES = True
ALERT_STAGES = False               # breakout-armed alerts (log-only when False)
ALERT_LIFECYCLE = True             # TP / stop / override alerts

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
NY_TZ = ZoneInfo(RANGE_TZ)

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
      "4h": 14_400_000}
_TF_ALIASES = {"5min": "5m", "15min": "15m", "30min": "30m",
               "60m": "1h", "60min": "1h", "1hr": "1h"}
TF = _TF_ALIASES.get(TF.strip().lower(), TF.strip().lower())
if TF not in MS:
    raise SystemExit(f"CONFIG ERROR: TF={TF!r} is not a known timeframe - "
                     f"use one of {sorted(MS)}")

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
               "User-Agent": "Mozilla/5.0 (range-agent/1.0)"}
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


# --------------------------- indicators ------------------------------------
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


def pivots(candles, wing=2):
    """(swing_high_indices, swing_low_indices), confirmed `wing` candles
    later. Used only by the huge-breakout fallback stop."""
    hs, ls = [], []
    for j in range(wing, len(candles) - wing):
        h, l = candles[j]["h"], candles[j]["l"]
        if all(h > candles[j + k]["h"] and h > candles[j - k]["h"]
               for k in range(1, wing + 1)):
            hs.append(j)
        if all(l < candles[j + k]["l"] and l < candles[j - k]["l"]
               for k in range(1, wing + 1)):
            ls.append(j)
    return hs, ls


# --------------------------- telegram --------------------------------------
def send_telegram(text):
    resp = http_json(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True})
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram send failed: {resp.get('description')}")


# --------------------------- the range -------------------------------------
def ny_dt(ms):
    return datetime.fromtimestamp(ms / 1000, NY_TZ)


def window_ms(d, cls="crypto"):
    """[start, end) epoch ms of the day's range window on NY date d.
    Equities: the 09:30-10:30 cash-session opening range. Everything else:
    the 4h CANDLE that opens the NY day, i.e. the first UTC-aligned 4h
    boundary at or after NY midnight, which is 00:00-04:00 NY under EDT and
    03:00-07:00 NY under EST."""
    if cls == "stock":
        h1, m1, h2, m2 = SESSIONS["stock"]
        a = datetime(d.year, d.month, d.day, h1, m1, tzinfo=NY_TZ)
        b = datetime(d.year, d.month, d.day, h2, m2, tzinfo=NY_TZ)
        return int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    midnight = int(datetime(d.year, d.month, d.day,
                            tzinfo=NY_TZ).timestamp() * 1000)
    step = MS["4h"]
    start = -(-midnight // step) * step          # first 4h boundary >= midnight
    return start, start + step


def stock_open_range(candles, d):
    """High/low of the TF candles inside the equities opening range on NY
    date d. 09:30-10:30 is not a 4h candle, so it has to be built from the
    execution-timeframe series. Returns (hi, lo, ready)."""
    start, end = window_ms(d, "stock")
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
    ready = end_seen and count >= expected - max(2, expected // 6)
    return hi, lo, ready


_R4_CACHE = {}


def range_4h(asset, d):
    """High/low of the native 4h candle that opens the NY day, wicks
    included, read straight off the exchange rather than rebuilt from the
    5m series. One fetch per symbol per NY day, cached on success.
    Returns (hi, lo, ready)."""
    key = (asset["symbol"], str(d))
    if key in _R4_CACHE:
        return _R4_CACHE[key]
    start, end = window_ms(d, asset.get("cls", "crypto"))
    if now_ms() < end:
        return None, None, False           # the candle has not closed yet
    _, cs = fetch(asset, "4h", 2)
    if not cs:
        return None, None, False           # fetch failed - retry next scan
    for c in cs:
        if c["t"] == start:
            out = (c["h"], c["l"], True)
            for k in [k for k in _R4_CACHE if k[1] != key[1]]:
                del _R4_CACHE[k]           # keep only the current NY day
            _R4_CACHE[key] = out
            return out
    log(f"{asset['symbol']}: no 4h candle opening {fmt_ts(start)} - "
        "no range today")
    return None, None, False


def day_range(asset, candles, d):
    """The day's range for this asset class. Returns (hi, lo, ready)."""
    if asset.get("cls") == "stock":
        return stock_open_range(candles, d)
    return range_4h(asset, d)


def nearest_key_level(candles, i, extreme, entry, long_):
    """The nearest swing pivot between entry and the excursion extreme -
    used when a huge breakout would otherwise put the stop miles away."""
    lo_i = max(0, i - RANGE_KEY_LOOKBACK)
    hs, ls = pivots(candles[lo_i:i + 1])
    levels = []
    for j in (ls if long_ else hs):
        px = candles[lo_i + j]["l"] if long_ else candles[lo_i + j]["h"]
        if (extreme < px < entry) if long_ else (entry < px < extreme):
            levels.append(px)
    if not levels:
        return None
    return max(levels) if long_ else min(levels)


# --------------------------- messages --------------------------------------
def stage_message(asset, direction, level, t):
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
    return "\n".join([
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 "
        f"{'opening range' if cls == 'stock' else 'first 4h candle'} "
        f"\u00b7 {esc(fmt_ts(t))}</i>",
        "",
        "\U0001F4CA <b>Setup</b>: "
        + (f"{win} range ${fmt_px(lo)} - ${fmt_px(hi)}; "
           if (hi is not None and lo is not None) else "")
        + f"{esc(trigger)}",
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>",
        f"TP:    <code>${fmt_px(plan['tp'])}</code>  "
        f"({RANGE_RR:.0f}x the stop distance)",
        f"<i>data: {esc(source)}</i>",
    ])


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    emoji, title, sub = {
        "TP": ("\u2705", "TAKE PROFIT HIT",
               f"{RANGE_RR:.0f}R target reached, closed in full"),
        "STOP": ("\u274C", "STOPPED OUT", "Stop level hit"),
        "OVERRIDE": ("\U0001F504", "TRADE REPLACED",
                     "closed early - a fresh range signal took over"),
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
    return {"phase": "SCAN", "last_candle_t": 0, "day": None,
            "setup": None, "done": [], "trade": None}


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
def _close_trade(asset, trade, px, kind, event_t, note=""):
    """Alert, log and book a full close. Returns (None, True)."""
    sym = asset["symbol"]
    if already_closed(sym, trade, px, kind):
        log(f"{sym}: duplicate {kind} close suppressed")
        return None, True
    if ALERT_LIFECYCLE:
        try:
            send_telegram(lifecycle_message(asset, kind, trade, px,
                                            event_t, note))
        except Exception as e:
            log(f"{sym}: {kind} alert failed: {type(e).__name__}: {e}")
    log(f"{sym}: {'TP HIT' if kind == 'TP' else 'STOPPED OUT'} at "
        f"${fmt_px(px)}{' (intrabar)' if note else ''}")
    record_close(sym, trade, px, kind, event_t)
    plan_manage_orders(asset, kind, px)
    RUN_ALERTS.append(f"{sym} {'TP HIT' if kind == 'TP' else 'STOPPED OUT'} "
                      f"({pnl_pct(trade, px):+.2f}%)")
    return None, True


def process_open_trade(asset, trade, candles, last_closed_t):
    """Stop / TP watch. The stop is checked first within a candle
    (conservative). Full close at either level - no partials, no runner.
    Returns (trade or None, changed)."""
    long = trade["verdict"] == "LONG"
    tp = trade["tp"]
    changed = False
    for c in candles:
        if c["t"] <= trade["checked_t"] or c["t"] > last_closed_t:
            continue
        changed = True
        trade["checked_t"] = c["t"]
        event_t = c["t"] + MS[TF]             # label events with the close
        if c["l"] <= trade["stop"] if long else c["h"] >= trade["stop"]:
            return _close_trade(asset, trade, trade["stop"], "STOP", event_t)
        if c["h"] >= tp if long else c["l"] <= tp:
            return _close_trade(asset, trade, tp, "TP", event_t)

    # ---- intrabar check on the LIVE (still forming) candle ----------------
    # A fast move can blow through the stop mid-candle; don't wait for the
    # close to say so. checked_t is NOT advanced for the live candle, and
    # this block computes its own timestamp - the loop above may not have
    # run this pulse, so its variables must never be referenced here.
    live = candles[-1]
    if live["t"] > last_closed_t:
        t_now = now_ms()
        if live["l"] <= trade["stop"] if long else live["h"] >= trade["stop"]:
            return _close_trade(
                asset, trade, trade["stop"], "STOP", t_now,
                "Intrabar - stop level traded before the candle closed.")
        if live["h"] >= tp if long else live["l"] <= tp:
            return _close_trade(
                asset, trade, tp, "TP", t_now,
                "Intrabar - target traded before the candle closed.")
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
              "OVERRIDE": "closed at market, replaced by a fresh signal"
              }.get(event)
    if not action:
        return
    log_order({"t": now_ms(), "sym": asset["symbol"],
               "mode": "live" if EXEC_LIVE else "sim",
               "event": event, "price": price, "action": action})


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
    except Exception as e:
        log(f"{sym}: LIVE entry FAILED {type(e).__name__}: {e}")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} entry order failed - "
                          "no position")
        except Exception:
            pass
        return None
    try:
        ex.order(sym, not long_, size, round_px(trade["stop"]),
                 {"trigger": {"triggerPx": round_px(trade["stop"]),
                              "isMarket": True, "tpsl": "sl"}},
                 reduce_only=True)
        # full size at the target: this strategy closes the whole position
        # there, so a half-size TP would leave half the trade running with
        # only the stop attached
        ex.order(sym, not long_, size, round_px(trade["tp"]),
                 {"limit": {"tif": "Gtc"}}, reduce_only=True)
        log(f"{sym}: LIVE stop ${fmt_px(trade['stop'])} and TP "
            f"${fmt_px(trade['tp'])} placed")
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
    tp = entry - RANGE_RR * risk if short else entry + RANGE_RR * risk
    plan = {"entry": entry, "stop": stop, "tp": tp}
    event_t = c["t"] + MS[TF]

    old = ast.get("trade")
    if old and not OVERRIDE_ON_NEW_SIGNAL:
        # check_asset already returns early in this case; enforcing it here
        # too means the flag cannot be defeated by a reordering upstream
        log(f"{sym}: {direction} signal ignored - {old['verdict']} still open "
            "and overrides are off")
        ast["setup"] = None
        return False
    if old and old["verdict"] == direction:
        log(f"{sym}: {direction} signal matches the open trade's direction "
            "- not replacing it")
        ast["setup"] = None
        return False
    if old:
        if ALERT_LIFECYCLE:
            try:
                send_telegram(lifecycle_message(
                    asset, "OVERRIDE", old, entry, event_t,
                    f"replaced by a new {direction} range signal"))
            except Exception as e:
                log(f"{sym}: override alert failed: {type(e).__name__}")
        log(f"{sym}: trade REPLACED at ${fmt_px(entry)} by a fresh "
            f"{direction} signal")
        record_close(sym, old, entry, "OVERRIDE", event_t)
        plan_manage_orders(asset, "OVERRIDE", entry)
        RUN_ALERTS.append(f"{sym} trade replaced "
                          f"({pnl_pct(old, entry):+.2f}%)")
        ast["trade"] = None

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
                    "rr": RANGE_RR, "risk0": risk}
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
def process_candle(asset, ast, candles, a, i, source, rng_cache):
    """4h range -> a 5m close outside -> a 5m close back inside -> entry."""
    sym = asset["symbol"]
    c = candles[i]
    atr_i = a[i] or 0
    if not atr_i:
        return False
    d = ny_dt(c["t"] + MS[TF]).date()

    # a new NY day wipes the range, the pending breakout and the done flags
    if ast.get("day") != str(d):
        if ast.get("setup"):
            log(f"{sym}: NY day rolled over - pending breakout cleared")
        ast["day"] = str(d)
        ast["setup"] = None
        ast["done"] = []

    if d not in rng_cache:
        rng_cache[d] = day_range(asset, candles, d)
    hi, lo, ready = rng_cache[d]

    # step 1: the range candle must have FULLY closed, and this candle must
    # come after it
    _, win_end = window_ms(d, asset.get("cls", "crypto"))
    if c["t"] + MS[TF] <= win_end or not ready or hi is None:
        return False
    if (hi - lo) < RANGE_MIN_ATR * atr_i:
        return False

    brk = ast.get("setup")

    # ---- step 2a: a candle CLOSES outside the range ----------------------
    for above, level, extreme_key in ((True, hi, "h"), (False, lo, "l")):
        outside = c["c"] > hi if above else c["c"] < lo
        if not outside:
            continue
        side = "above" if above else "below"
        direction = "SHORT" if above else "LONG"
        if not brk or brk["side"] != side:
            if direction in (ast.get("done") or []) and RANGE_ONE_PER_SIDE:
                return False
            ast["setup"] = {"side": side, "level": level,
                            "extreme": c[extreme_key], "t": c["t"],
                            "hi": hi, "lo": lo}
            log(f"{sym}: closed {side.upper()} the 4h range "
                f"(${fmt_px(level)}) - watching for a close back inside")
            if ALERT_STAGES:
                try:
                    send_telegram(stage_message(asset, direction, level,
                                                c["t"] + MS[TF]))
                except Exception as e:
                    log(f"{sym}: stage alert failed: {type(e).__name__}")
        else:
            brk["extreme"] = (max(brk["extreme"], c["h"]) if above
                              else min(brk["extreme"], c["l"]))
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

    risk = (stop - entry) if short else (entry - stop)
    # "huge" is judged against the RANGE: the excursion is how far price
    # travelled beyond the level it broke
    excursion = (brk["extreme"] - hi) if short else (lo - brk["extreme"])
    if excursion > RANGE_HUGE_FRACTION * (hi - lo) \
            or risk > RANGE_MAX_STOP_ATR * atr_i:
        key = nearest_key_level(candles, i, brk["extreme"], entry, not short)
        src_txt = "nearest key level"
        if key is None:
            key = hi if short else lo
            src_txt = ("the broken range level (now resistance)" if short
                       else "the broken range level (now support)")
        stop = (key + RANGE_KEY_BUFFER_ATR * atr_i if short
                else key - RANGE_KEY_BUFFER_ATR * atr_i)
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

    # only a trade that actually opened burns the side - a setup rejected for
    # a too-tight stop leaves that direction free to try again today
    if fire_entry(asset, ast, direction, c, stop, hi, lo, source,
                  f"closed outside the 4h range then back inside; {note}"):
        ast.setdefault("done", []).append(direction)
    ast["setup"] = None
    return True


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
        source, cs = fetch(asset, TF, 30 if not OVERRIDE_ON_NEW_SIGNAL else 300)
        if cs:
            trade, ch = process_open_trade(asset, ast["trade"], cs,
                                           cs[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        # exits win over overrides (the watch ran first). Fall through to the
        # candle walk when the trade just closed, or when overrides are on.
        if ast["trade"] and not OVERRIDE_ON_NEW_SIGNAL:
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

    a = atr(cs)
    rng_cache = {}
    last_closed = len(cs) - 2
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_closed or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        changed = process_candle(asset, ast, cs, a, i, source, rng_cache) \
            or changed
        ast["last_candle_t"] = cs[i]["t"]
        if ast["trade"] and ast["trade"].get("opened_t") == cs[i]["t"]:
            break                              # a trade opened on this candle

    # an open trade always reports IN_TRADE, even when a fresh setup is armed
    # on the same symbol - otherwise the run summary undercounts open trades
    setup = ast.get("setup")
    armed_dir = ("SHORT" if setup["side"] == "above" else "LONG") \
        if setup else None
    if ast["trade"]:
        stage = "IN_TRADE" + (f" +armed({armed_dir})" if setup else "")
    elif setup:
        stage = f"BROKE-{setup['side']} ({armed_dir} on reentry)"
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
                              last_scan_utc=datetime.now(timezone.utc)
                              .isoformat(timespec="seconds"))
        save_state(state)
        if failures:
            log(f"{failures} asset(s) failed this run - they retry next cycle.")
    finally:
        write_run_summary()


def seconds_to_next_close(buffer_s=15):
    period = MS["5m"] // 1000     # 5m pulse regardless of TF: heartbeat and
    return period - (time.time() % period) + buffer_s      # prompt exits


def run_loop():
    log("4h range agent started (loop mode). Ctrl+C to stop.")
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
        send_telegram("\u2705 <b>4h range agent</b> - test message, "
                      "Telegram wiring is good.")
        log("test message sent")
        return
    if "--loop" in args:
        run_loop()
        return
    check_once()


if __name__ == "__main__":
    main()
