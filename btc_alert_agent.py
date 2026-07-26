#!/usr/bin/env python3
"""
4-HOUR RANGE AGENT (New York session false-breakout reversals)
---------------------------------------------------------------
Each day, the high/low of the FIRST 4-hour window of the New York day
(00:00-04:00 America/New_York) defines the range - marked only once the
window has fully closed, and valid until the end of that NY day.

On 5m candles, after 04:00 NY (mirrored for the low side):
  1. BREAKOUT  a 5m candle CLOSES above the range high (wicks alone
               never count). While price stays outside, the excursion
               extreme is tracked.
  2. REENTRY   a 5m candle CLOSES back inside the range
               -> SHORT at that close (the breakout failed)
               SL = the exact breakout extreme (no pad)
               TP = 2 x the stop distance
  Broke the LOW then reentered -> LONG, SL at the exact excursion low,
  TP 2R.

One live trade per asset; after it closes, a fresh breakout can arm the
same range again. At NY midnight everything resets and waits for the
new day's 04:00 range.

Exits: TP or SL, with intrabar detection. Closes recorded to trades.log.

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
DISCOVER_DEXES = True              # scan HIP-3 builder dexes too - but only
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
MAX_ZONES = 20                     # cap concurrently open reversal zones

ASSETS = [                         # used when DISCOVER_ALL = False / discovery fails
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- Strategy dials -------------------------------------------------------
TF = "5m"                    # execution timeframe (the spec is 5m closes)
RANGE_TZ = "America/New_York"
# session windows per asset class (NY h:m start -> h:m end):
#   crypto & commodities: the first 4h of the NY day (overnight range)
#   stocks: the cash-session OPENING RANGE (equities are closed overnight,
#           so the 00-04 window would be a meaningless flat line)
SESSIONS = {"crypto": (0, 0, 4, 0),
            "commodity": (0, 0, 4, 0),
            "stock": (9, 30, 10, 30)}
RR = 2.0                     # TP = 2 x the stop distance
RANGE_MIN_ATR = 0.30         # range narrower than this x ATR = untradeable day
# price-action confirmation on the reentry candle (Plan A):
#   * body must close AGAINST the breakout (bearish for a short, bullish long)
#   * the candle must still have wicked beyond the level (it came from outside)
#   * the close must land in the far REJECT_CLOSE_PCT of the candle's range
MIN_STOP_PCT = 0.25              # skip entries whose stop sits closer than
                                 # this % of price - sub-noise stops just churn
REQUIRE_REJECTION = True
REJECT_CLOSE_PCT = 0.40
REQUIRE_REENTRY_VOLUME = False   # optional extra: volume >= x 20-candle average
REENTRY_VOL_MULT = 1.0

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

MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}

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


# ----------------------------- 4h range engine ------------------------------
NY_TZ = ZoneInfo(RANGE_TZ)


def ny_dt(ms):
    return datetime.fromtimestamp(ms / 1000, NY_TZ)


def session_window(cls):
    h1, m1, h2, m2 = SESSIONS.get(cls, SESSIONS["crypto"])
    return h1 * 60 + m1, h2 * 60 + m2


def day_range(candles, d, cls):
    """High/low of 5m candles inside the class's session window on NY
    date d. Returns (hi, lo, ready)."""
    start, end = session_window(cls)
    step = MS[TF] // 60_000            # candle length in minutes (TF-agnostic)
    hi = lo = None
    count = 0
    end_seen = False
    for c in candles:
        dt = ny_dt(c["t"])
        mins = dt.hour * 60 + dt.minute
        if dt.date() == d and start <= mins and mins + step <= end:
            hi = c["h"] if hi is None else max(hi, c["h"])
            lo = c["l"] if lo is None else min(lo, c["l"])
            count += 1
            if mins + step == end:
                end_seen = True
    expected = max(1, (end - start) // step)
    ready = count >= expected - max(2, expected // 6) and end_seen
    return hi, lo, ready


def rejection_ok(c, level, short):
    """Price-action confirmation for a reentry candle. Returns (ok, why)."""
    rng = c["h"] - c["l"]
    if rng <= 0:
        return False, "flat candle"
    if short:
        if c["c"] >= c["o"]:
            return False, "body is not bearish"
        if c["h"] < level:
            return False, "no wick back above the level"
        pos = (c["c"] - c["l"]) / rng
        if pos > REJECT_CLOSE_PCT:
            return False, f"close sits {pos * 100:.0f}% up the candle"
    else:
        if c["c"] <= c["o"]:
            return False, "body is not bullish"
        if c["l"] > level:
            return False, "no wick back below the level"
        pos = (c["h"] - c["c"]) / rng
        if pos > REJECT_CLOSE_PCT:
            return False, f"close sits {pos * 100:.0f}% down the candle"
    return True, ""


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


def entry_message(asset, direction, plan, hi, lo, ext, source, t):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    broke = "high" if direction == "SHORT" else "low"
    h1, m1, h2, m2 = SESSIONS.get(asset.get("cls", "crypto"), SESSIONS["crypto"])
    win = f"NY {h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"
    kind = "opening-range" if asset.get("cls") == "stock" else "4h-range"
    lines = [
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 {kind} reversal \u00b7 {esc(fmt_ts(t))}</i>",
        "",
        f"\U0001F4CA <b>Setup</b>: {win} range ${fmt_px(lo)} - ${fmt_px(hi)}; "
        f"{TF} closed beyond the {broke}, then closed back INSIDE"
        + (" on a confirmed rejection candle" if REQUIRE_REJECTION else "")
        + " - failed breakout",
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>  (exact breakout extreme)",
        f"TP:    <code>${fmt_px(plan['tp'])}</code>  ({RR:.0f}x the stop distance)",
        f"<i>data: {esc(source)}</i>",
    ]
    return "\n".join(lines)


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    emoji, title, sub = {
        "TP": ("\u2705", "TAKE PROFIT HIT", f"{RR:.0f}R target reached"),
        "STOP": ("\u274C", "STOPPED OUT", "Stop level hit"),
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


def record_close(sym, trade, exit_px, kind, t_event=None):
    """Append a closed trade to the ledger (best-effort). t_event = the
    actual market time of the exit, so late reconciliations book to the
    day they truly happened."""
    try:
        with open(TRADES_LOG, "a") as f:
            f.write(json.dumps({"t": int(t_event or time.time() * 1000), "sym": sym,
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
        if tp_hit:
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
        stop_hit = live["l"] <= trade["stop"] if long else live["h"] >= trade["stop"]
        tp_hit = (live["h"] >= tp) if long else (live["l"] <= tp)
        if stop_hit:
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
        if tp_hit:
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


def process_candle(asset, ast, real, a, i, source, rng_cache):
    """Walk ONE newly closed 5m candle through the 4h-range engine."""
    sym = asset["symbol"]
    c = real[i]
    close_dt = ny_dt(c["t"] + MS[TF])
    d = close_dt.date()

    # NY-day rollover: everything resets
    if ast.get("day") != str(d):
        if ast.get("setup"):
            log(f"{sym}: NY day rolled over - pending breakout cleared")
        ast["day"], ast["setup"] = str(d), None

    # only trade after the class's range window has fully closed
    cls = asset.get("cls", "crypto")
    _, win_end = session_window(cls)
    if close_dt.hour * 60 + close_dt.minute <= win_end:
        return False

    if d not in rng_cache:
        rng_cache[d] = day_range(real, d, cls)
    hi, lo, ready = rng_cache[d]
    if not ready:
        return False
    # dead-flat window: a range narrower than 0.3 x ATR is noise, not a range
    if a[i] and (hi - lo) < RANGE_MIN_ATR * a[i]:
        return False

    brk = ast["setup"]

    # ---- closes OUTSIDE the range: register / extend the breakout ----------
    if c["c"] > hi:
        if brk and brk["side"] == "HIGH":
            brk["ext"] = max(brk["ext"], c["h"])
        else:
            ast["setup"] = {"side": "HIGH", "direction": "SHORT",
                            "level": hi, "ext": c["h"],
                            "note": f"closed above 4h-range high - a close "
                                    f"back inside triggers the SHORT"}
            log(f"{sym}: 5m closed ABOVE the 4h-range high ${fmt_px(hi)} - "
                "watching for reentry (SHORT on close back inside)")
            if ALERT_STAGES:
                send_telegram(stage_message(asset, "SHORT", hi, c["c"], c["t"]))
        return True
    if c["c"] < lo and ENABLE_SHORTS is not None:      # lows always tracked
        if brk and brk["side"] == "LOW":
            brk["ext"] = min(brk["ext"], c["l"])
        else:
            ast["setup"] = {"side": "LOW", "direction": "LONG",
                            "level": lo, "ext": c["l"],
                            "note": f"closed below 4h-range low - a close "
                                    f"back inside triggers the LONG"}
            log(f"{sym}: 5m closed BELOW the 4h-range low ${fmt_px(lo)} - "
                "watching for reentry (LONG on close back inside)")
            if ALERT_STAGES:
                send_telegram(stage_message(asset, "LONG", lo, c["c"], c["t"]))
        return True

    # ---- closes back INSIDE with a pending breakout: the reversal entry ----
    if brk and lo <= c["c"] <= hi:
        short = brk["side"] == "HIGH"
        lvl = brk["level"]
        if REQUIRE_REJECTION:
            ok, why = rejection_ok(c, lvl, short)
            if not ok:
                log(f"{sym}: reentry closed inside but price action unconfirmed "
                    f"({why}) - setup stays armed")
                return True
        if REQUIRE_REENTRY_VOLUME:
            window = [x["v"] for x in real[max(0, i - 20):i] if x.get("v")]
            avg = sum(window) / len(window) if window else 0
            if avg and c["v"] < REENTRY_VOL_MULT * avg:
                log(f"{sym}: reentry volume {c['v'] / avg:.1f}x average - "
                    "below the floor, setup stays armed")
                return True
        entry = c["c"]
        stop = brk["ext"]                       # the EXACT breakout extreme
        risk = (stop - entry) if short else (entry - stop)
        if risk <= 0:
            ast["setup"] = None
            return True
        if MIN_STOP_PCT and entry and risk / entry * 100 < MIN_STOP_PCT:
            log(f"{sym}: stop only {risk / entry * 100:.3f}% away "
                f"(min {MIN_STOP_PCT}%) - too tight to be worth fees, "
                "setup stays armed")
            return True
        tp = entry - RR * risk if short else entry + RR * risk
        direction = "SHORT" if short else "LONG"
        plan = {"entry": entry, "stop": stop, "tp": tp}
        # a fresh qualifying signal REPLACES any trade still open on this
        # symbol: book the incumbent at this candle's close, then take over
        old_trade = ast.get("trade")
        if old_trade and OVERRIDE_ONLY_OPPOSITE \
                and old_trade["verdict"] == direction:
            log(f"{sym}: {direction} signal matches the open trade's "
                "direction - not replacing it")
            ast["setup"] = None
            return True
        if old_trade:
            if ALERT_LIFECYCLE:
                send_telegram(lifecycle_message(
                    asset, "OVERRIDE", old_trade, entry, c["t"] + MS[TF],
                    f"replaced by a new {direction} range signal"))
            log(f"{sym}: trade REPLACED at ${fmt_px(entry)} by a fresh "
                f"{direction} signal")
            record_close(sym, old_trade, entry, "OVERRIDE", c["t"] + MS[TF])
            RUN_ALERTS.append(
                f"{sym} trade replaced ({pnl_pct(old_trade, entry):+.2f}%)")
            ast["trade"] = None
        if ALERT_ENTRIES:
            send_telegram(entry_message(asset, direction, plan, hi, lo,
                                        brk["ext"], source, c["t"] + MS[TF]))
        log(f"ALERT SENT -> telegram: {sym} {direction} ENTRY @ "
            f"${fmt_px(entry)} (failed break of the 4h-range "
            f"{'high' if short else 'low'})")
        RUN_ALERTS.append(f"{sym} {direction} entry @ ${fmt_px(entry)}")
        ast["trade"] = {"verdict": direction, "entry": entry, "stop": stop,
                        "tp": tp, "opened_t": c["t"], "checked_t": c["t"]}
        ast["phase"], ast["setup"] = "IN_TRADE", None
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

    stage = ast["phase"]
    if ast["setup"]:
        stage = f"BROKE-{ast['setup']['side']} ({ast['setup']['direction']} on reentry)"
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
                      f"Strategy: 4h range (NY 00:00-04:00) - 5m close "
                      "outside the range, then a close back INSIDE enters "
                      f"the reversal; SL at the exact breakout extreme, TP {RR:.0f}R.")
        print("Test message sent to Telegram.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
