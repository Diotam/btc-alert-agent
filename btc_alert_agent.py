#!/usr/bin/env python3
"""
MULTI-ASSET SIGNAL ALERT AGENT - 30m swing - full trade lifecycle
------------------------------------------------------------------
Confluence engine built on three pillars:

  1. TREND DIRECTION   EMA20/50 alignment + price vs EMA200      (-2..+2)
  2. SUPPORT/RESISTANCE pivot-based levels: breakouts, bounces,
                        rejections, breakdowns                    (-2..+2)
  3. PRICE ACTION      swing structure (HH/HL vs LH/LL) +
                        candle signals (engulfing, pin bars,
                        strong closes)                            (-2..+2)

Total score range -6..+6. Entry requires |score| >= SIGNAL_THRESHOLD,
plus a room-to-move check: a LONG is vetoed if resistance sits before
TP1 (mirrored for SHORTs against support).

Stops are structure-aware: placed just beyond the nearest S/R level
when one is close, otherwise 1.5 x ATR.

Lifecycle alerts: ENTRY, TP1 (stop -> breakeven), TP2, STOPPED OUT,
INVALIDATED.

Config from environment variables (GitHub repo Secrets):
  EMAIL_FROM / EMAIL_APP_PASSWORD / EMAIL_TO

Modes:
  python3 btc_alert_agent.py           single check (workflow default)
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

ASSETS = [
    {"symbol": "BTC",  "label": "BTC-PERP",        "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
    {"symbol": "TSLA", "label": "TSLA-PERP (xyz)", "hl_coin": "xyz:TSLA",
     "fallbacks": ["yahoo:TSLA"]},
    {"symbol": "SP500", "label": "SP500-PERP (xyz)", "hl_coin": "xyz:SP500",
     "fallbacks": ["yahoo:^GSPC"]},
]

CANDLE_MINUTES = 30
LOOKBACK = 500
# Timezone shown in alert emails (IANA name). Common options:
#   "America/New_York"  "America/Chicago"  "America/Denver"
#   "America/Los_Angeles"  "America/Port_of_Spain"  "Europe/London"
TIMEZONE = "America/New_York"

MAX_SCORE = 6
SIGNAL_THRESHOLD = 4           # |score| needed to enter (out of +-6)
INVALIDATION_SCORE = 2         # open LONG dies if score <= -2 (SHORT mirrored)

# Support/resistance detection
PIVOT_WING = 3                 # candles on each side to confirm a swing point
LEVEL_TOL_ATR = 0.30           # cluster pivots within this many ATRs into one level
MIN_TOUCHES_BREAKOUT = 2       # touches a level needs to count for breakout/breakdown
SR_STOP_MAX_ATR = 2.5          # use structure stop only if level within this many ATRs
SR_STOP_PAD_ATR = 0.30         # stop placed this far beyond the level

ATR_STOP_MULT = 1.5            # fallback stop when no nearby structure
ATR_TP1_MULT = 2.0
ATR_TP2_MULT = 3.0
BREAKEVEN_AFTER_TP1 = True
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
# ===========================================================================

CANDLE_MS = CANDLE_MINUTES * 60 * 1000
LOCAL_TZ = ZoneInfo(TIMEZONE)


def fmt_ts(ms, fmt="%Y-%m-%d %I:%M %p %Z"):
    return datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime(fmt)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}",
          flush=True)


def fmt_px(p):
    return f"{p:,.0f}" if p >= 10000 else f"{p:,.2f}"


def pnl_pct(trade, exit_px):
    sign = 1 if trade["verdict"] == "LONG" else -1
    return sign * (exit_px - trade["entry"]) / trade["entry"] * 100


# --------------------------- data sources ---------------------------------
def http_json(url, payload=None, timeout=20):
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (signal-alert-agent/4.0)"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_hyperliquid(coin):
    end = int(time.time() * 1000)
    start = end - LOOKBACK * CANDLE_MS
    data = http_json("https://api.hyperliquid.xyz/info", {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": f"{CANDLE_MINUTES}m",
                "startTime": start, "endTime": end},
    })
    return [{"t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])}
            for c in data]


def fetch_binance(sym):
    data = http_json(f"https://api.binance.com/api/v3/klines"
                     f"?symbol={sym}&interval={CANDLE_MINUTES}m&limit={LOOKBACK}")
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in data]


def fetch_kraken(pair):
    data = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={CANDLE_MINUTES}")
    key = next(k for k in data["result"] if k != "last")
    return [{"t": k[0] * 1000, "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[6])}
            for k in data["result"][key]]


def fetch_yahoo(ticker):
    from urllib.parse import quote
    data = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
                     f"?interval={CANDLE_MINUTES}m&range=1mo")
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


def fetch_fallback(spec):
    provider, _, ident = spec.partition(":")
    return {"binance": fetch_binance, "kraken": fetch_kraken,
            "yahoo": fetch_yahoo}[provider](ident)


def fetch_candles(asset):
    sources = [(f"Hyperliquid {asset['hl_coin']}",
                lambda: fetch_hyperliquid(asset["hl_coin"]))]
    for spec in asset.get("fallbacks", []):
        sources.append((spec, lambda s=spec: fetch_fallback(s)))
    for name, fn in sources:
        try:
            candles = fn()
            if len(candles) >= 210:
                return name, candles
            log(f"{asset['symbol']}: {name} returned only {len(candles)} candles")
        except Exception as e:
            log(f"{asset['symbol']}: {name} failed: {e}")
    return None, None


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


# ----------------------- support / resistance ------------------------------
def find_pivots(candles, upto, wing=PIVOT_WING):
    """Confirmed swing highs/lows in candles[:upto+1]. Returns (highs, lows)
    as lists of (index, price). A pivot needs `wing` candles on both sides."""
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
    """Cluster pivot prices within `tol` into levels: [(price, touches)]."""
    levels = []
    for p in sorted(pivot_prices):
        if levels and p - levels[-1][0] <= tol:
            price, n = levels[-1]
            levels[-1] = ((price * n + p) / (n + 1), n + 1)
        else:
            levels.append((p, 1))
    return levels


def sr_context(candles, i, a):
    """Everything the engine needs to know about levels around candle i."""
    highs, lows = find_pivots(candles, i)
    tol = LEVEL_TOL_ATR * a
    levels = build_levels([p for _, p in highs] + [p for _, p in lows], tol)
    px = candles[i]["c"]
    support = max((lv for lv in levels if lv[0] < px), key=lambda x: x[0], default=None)
    resistance = min((lv for lv in levels if lv[0] > px), key=lambda x: x[0], default=None)
    return {"levels": levels, "support": support, "resistance": resistance,
            "highs": highs, "lows": lows, "tol": tol}


def sr_signal(candles, i, ctx):
    """Score S/R interaction on candle i: (score -2..+2, description)."""
    c, prev_close = candles[i], candles[i - 1]["c"]
    px = c["c"]
    tol = ctx["tol"]

    # breakout / breakdown through an established level
    for price, touches in ctx["levels"]:
        if touches < MIN_TOUCHES_BREAKOUT:
            continue
        if prev_close < price <= px:
            return 2, f"Breakout above ${fmt_px(price)} ({touches} touches)"
        if prev_close > price >= px:
            return -2, f"Breakdown below ${fmt_px(price)} ({touches} touches)"

    # bounce off support / rejection at resistance
    if ctx["support"]:
        s_price, s_touch = ctx["support"]
        if c["l"] <= s_price + tol and px > s_price and px > c["o"]:
            return 1, f"Bounce off support ${fmt_px(s_price)} ({s_touch} touches)"
    if ctx["resistance"]:
        r_price, r_touch = ctx["resistance"]
        if c["h"] >= r_price - tol and px < r_price and px < c["o"]:
            return -1, f"Rejected at resistance ${fmt_px(r_price)} ({r_touch} touches)"

    return 0, "Mid-range - no level interaction"


# ----------------------------- price action --------------------------------
def structure_signal(ctx):
    """Swing structure from the last two confirmed highs and lows."""
    highs, lows = ctx["highs"], ctx["lows"]
    if len(highs) < 2 or len(lows) < 2:
        return 0, "Structure unclear"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    if hh and hl:
        return 1, "Higher highs & higher lows"
    if not hh and not hl:
        return -1, "Lower highs & lower lows"
    return 0, "Mixed structure"


def candle_signal(candles, i, a):
    """Single-candle / two-candle price action on the closed candle i."""
    c, p = candles[i], candles[i - 1]
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    if rng <= 0:
        return 0, "No candle signal"
    up_wick = c["h"] - max(c["c"], c["o"])
    dn_wick = min(c["c"], c["o"]) - c["l"]
    bull = c["c"] > c["o"]

    # engulfing
    if bull and p["c"] < p["o"] and c["c"] >= p["o"] and c["o"] <= p["c"]:
        return 1, "Bullish engulfing"
    if not bull and p["c"] > p["o"] and c["c"] <= p["o"] and c["o"] >= p["c"]:
        return -1, "Bearish engulfing"
    # pin bars (rejection wicks)
    if dn_wick >= 2 * body and c["c"] >= c["l"] + 0.6 * rng:
        return 1, "Hammer / bullish pin bar"
    if up_wick >= 2 * body and c["c"] <= c["h"] - 0.6 * rng:
        return -1, "Shooting star / bearish pin bar"
    # strong directional close on an expanded candle
    if rng >= a and bull and c["c"] >= c["h"] - 0.2 * rng:
        return 1, "Strong bullish close, expanded range"
    if rng >= a and not bull and c["c"] <= c["l"] + 0.2 * rng:
        return -1, "Strong bearish close, expanded range"
    return 0, "No candle signal"


# ---------------------------- signal engine --------------------------------
def evaluate(candles, i, e20, e50, e200, atr_arr):
    if i < 200 or atr_arr[i] is None or e200[i] is None:
        return None
    a = atr_arr[i]
    px = candles[i]["c"]
    factors, score = [], 0

    # 1 - TREND DIRECTION (-2..+2)
    t = (1 if e20[i] > e50[i] else -1) + (1 if px > e200[i] else -1)
    parts = [("EMA20>EMA50" if e20[i] > e50[i] else "EMA20<EMA50"),
             ("above EMA200" if px > e200[i] else "below EMA200")]
    score += t
    factors.append(("Trend", f"{parts[0]}, price {parts[1]} ({t:+d})",
                    (t > 0) - (t < 0)))

    # 2 - SUPPORT / RESISTANCE (-2..+2)
    ctx = sr_context(candles, i, a)
    s, desc = sr_signal(candles, i, ctx)
    score += s
    factors.append(("S/R", f"{desc} ({s:+d})", (s > 0) - (s < 0)))

    # 3 - PRICE ACTION (-2..+2): structure + candle signal
    st, st_desc = structure_signal(ctx)
    ca, ca_desc = candle_signal(candles, i, a)
    pa = st + ca
    score += pa
    factors.append(("Price action", f"{st_desc}; {ca_desc} ({pa:+d})",
                    (pa > 0) - (pa < 0)))

    verdict = ("LONG" if score >= SIGNAL_THRESHOLD
               else "SHORT" if score <= -SIGNAL_THRESHOLD else "WAIT")

    plan = None
    if verdict != "WAIT":
        sign = 1 if verdict == "LONG" else -1
        # structure-aware stop: beyond the nearest level if one is close
        stop = px - sign * ATR_STOP_MULT * a
        if verdict == "LONG" and ctx["support"]:
            s_price = ctx["support"][0]
            if px - s_price <= SR_STOP_MAX_ATR * a:
                stop = s_price - SR_STOP_PAD_ATR * a
        elif verdict == "SHORT" and ctx["resistance"]:
            r_price = ctx["resistance"][0]
            if r_price - px <= SR_STOP_MAX_ATR * a:
                stop = r_price + SR_STOP_PAD_ATR * a
        tp1 = px + sign * ATR_TP1_MULT * a
        tp2 = px + sign * ATR_TP2_MULT * a

        # room-to-move veto: a level sitting before TP1 kills the trade
        blocker = None
        if verdict == "LONG" and ctx["resistance"] and ctx["resistance"][0] < tp1:
            blocker = ("Resistance", ctx["resistance"][0])
        if verdict == "SHORT" and ctx["support"] and ctx["support"][0] > tp1:
            blocker = ("Support", ctx["support"][0])
        if blocker:
            factors.append(("Room", f"{blocker[0]} ${fmt_px(blocker[1])} sits "
                                    f"before TP1 - entry vetoed", 0))
            verdict = "WAIT"
        elif abs(px - stop) > 0 and abs(tp1 - px) / abs(px - stop) < 1.0:
            factors.append(("Room", "Structure stop is wider than TP1 reward "
                                    "(R < 1) - entry vetoed", 0))
            verdict = "WAIT"
        else:
            risk = abs(px - stop)
            plan = {"entry": px, "stop": stop, "tp1": tp1, "tp2": tp2, "atr": a,
                    "rr1": abs(tp1 - px) / risk if risk else 0,
                    "rr2": abs(tp2 - px) / risk if risk else 0}
            if ctx["support"]:
                factors.append(("Levels", f"Support ${fmt_px(ctx['support'][0])} / "
                                          f"resistance " +
                                (f"${fmt_px(ctx['resistance'][0])}"
                                 if ctx["resistance"] else "none above"), 0))

    return {"score": score, "factors": factors, "verdict": verdict,
            "plan": plan, "price": px, "t": candles[i]["t"]}


def analyze(candles):
    closes = [c["c"] for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    a = atr(candles)
    return evaluate(candles, len(candles) - 2, e20, e50, e200, a)


# ------------------------------- email ------------------------------------
STYLES = {
    "LONG":        {"accent": "#0FB98C", "soft": "#E6F7F1", "mark": "&#9650;"},
    "SHORT":       {"accent": "#E8524A", "soft": "#FCEAE8", "mark": "&#9660;"},
    "TP1":         {"accent": "#0FB98C", "soft": "#E6F7F1", "mark": "&#10003;"},
    "TP2":         {"accent": "#0FB98C", "soft": "#E6F7F1", "mark": "&#10003;&#10003;"},
    "STOP":        {"accent": "#E8524A", "soft": "#FCEAE8", "mark": "&#10007;"},
    "INVALIDATED": {"accent": "#D99A2B", "soft": "#FBF2DF", "mark": "&#9888;"},
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


DISCLAIMER = ("Automated technical signal for research &mdash; not financial advice. "
              "Any single signal can fail; size accordingly.")
DISCLAIMER_TXT = ("Automated technical signal for research - not financial advice. "
                  "Any single signal can fail; size accordingly.")


def entry_email(asset, sig, source):
    v = sig["verdict"]
    sym = asset["symbol"]
    p = sig["plan"]
    ts = fmt_ts(sig["t"])
    subject = f"[{v}] {sym} entry @ ${fmt_px(sig['price'])} - {CANDLE_MINUTES}m"

    dot = {1: "#0FB98C", -1: "#E8524A", 0: "#C4CED6"}
    factor_rows = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid #EDF1F4;'
        f'font-size:12px;color:#7A8B99;white-space:nowrap;">'
        f'<span style="display:inline-block;width:8px;height:8px;border-radius:4px;'
        f'background:{dot[d]};margin-right:8px;"></span>{k}</td>'
        f'<td style="padding:7px 0;border-bottom:1px solid #EDF1F4;font-size:13px;'
        f'color:#1A2530;text-align:right;">{desc}</td></tr>'
        for k, desc, d in sig["factors"])
    extra = (f'<tr><td style="padding:14px 24px 8px;">'
             f'<div style="font-size:11px;letter-spacing:2px;color:#7A8B99;'
             f'text-transform:uppercase;margin-bottom:4px;">Why it fired</div>'
             f'<table role="presentation" width="100%" cellpadding="0" '
             f'cellspacing="0">{factor_rows}</table></td></tr>')

    rows = (_row("Entry", "$" + fmt_px(p["entry"]), bold=True)
            + _row("Stop &middot; structure-aware", "$" + fmt_px(p["stop"]), "#E8524A", True)
            + _row(f"TP1 &middot; R {p['rr1']:.2f}", "$" + fmt_px(p["tp1"]), "#0FB98C", True)
            + _row(f"TP2 &middot; R {p['rr2']:.2f}", "$" + fmt_px(p["tp2"]), "#0FB98C", True)
            + _row("ATR14", "$" + fmt_px(p["atr"])))
    html = _html_shell(v, f"{asset['label']} &middot; {CANDLE_MINUTES}m entry signal",
                       f"{v} &middot; ${fmt_px(sig['price'])}",
                       f"Confluence {sig['score']:+d} / &plusmn;{MAX_SCORE} &nbsp;&middot;&nbsp; {ts}",
                       rows, f"Source: {source}. {DISCLAIMER}", extra)

    body = "\n".join([
        f"{sym} {CANDLE_MINUTES}m confluence flipped to {v}",
        f"Candle close: {ts}", f"Source: {source}",
        f"Confluence score: {sig['score']:+d} / +-{MAX_SCORE}", "",
        "TRADE PLAN (structure-aware)",
        f"  Entry : ${fmt_px(p['entry'])}",
        f"  Stop  : ${fmt_px(p['stop'])}",
        f"  TP1   : ${fmt_px(p['tp1'])}  (R {p['rr1']:.2f})",
        f"  TP2   : ${fmt_px(p['tp2'])}  (R {p['rr2']:.2f})", "",
        "FACTORS"] +
        [f"  {k}: {d}" for k, d, _ in sig["factors"]] +
        ["", DISCLAIMER_TXT])
    return subject, body, html


def lifecycle_email(asset, kind, trade, exit_px, event_t, note):
    sym = asset["symbol"]
    v = trade["verdict"]
    pl = pnl_pct(trade, exit_px)
    pl_s = f"{pl:+.2f}%"
    ts = fmt_ts(event_t)
    titles = {
        "TP1":  (f"[TP1 HIT] {sym} {v} {pl_s}", "TP1 HIT", "First target reached"),
        "TP2":  (f"[TP2 HIT] {sym} {v} {pl_s} - trade complete", "TP2 HIT", "Final target reached &mdash; trade complete"),
        "STOP": (f"[STOPPED] {sym} {v} {pl_s}", "STOPPED OUT", "Stop level hit &mdash; trade closed"),
        "INVALIDATED": (f"[INVALIDATED] {sym} {v} signal - consider exit",
                        "INVALIDATED", "Confluence collapsed against the trade"),
    }
    subject, big, sub = titles[kind]
    pl_color = "#0FB98C" if pl >= 0 else "#E8524A"
    rows = (_row("Direction", v)
            + _row("Entry", "$" + fmt_px(trade["entry"]))
            + _row("Exit level" if kind != "INVALIDATED" else "Current price",
                   "$" + fmt_px(exit_px), bold=True)
            + _row("P&amp;L (approx)", pl_s, pl_color, True)
            + _row("Opened", fmt_ts(trade["opened_t"], "%b %d %I:%M %p %Z")))
    html = _html_shell(kind, f"{asset['label']} &middot; {CANDLE_MINUTES}m trade update",
                       f"{big} &middot; {sym}", f"{sub} &nbsp;&middot;&nbsp; {ts}",
                       rows, f"{note} {DISCLAIMER}")
    body = "\n".join([
        f"{sym} {v} trade update: {big}",
        f"Time: {ts}",
        f"Entry: ${fmt_px(trade['entry'])}",
        f"{'Current price' if kind == 'INVALIDATED' else 'Exit level'}: ${fmt_px(exit_px)}",
        f"P&L (approx): {pl_s}", "",
        note, "", DISCLAIMER_TXT])
    return subject, body, html


# ------------------------------- state ------------------------------------
def load_state():
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if "last_verdict" in raw:
        return {"BTC": raw}
    return raw


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ------------------------------- agent ------------------------------------
def process_open_trade(asset, trade, candles, last_closed_t, sig):
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
                    "Structure stop was hit. Wait for the next confluence flip.")
            subject, body, html = lifecycle_email(asset, "STOP", trade,
                                                  trade["stop"], c["t"], note)
            send_email(subject, body, html)
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])} -> email sent")
            return None, True

        if hit_tp2:
            subject, body, html = lifecycle_email(
                asset, "TP2", trade, trade["tp2"], c["t"],
                "Full target reached. Trade closed.")
            send_email(subject, body, html)
            log(f"{sym}: TP2 HIT at ${fmt_px(trade['tp2'])} -> email sent")
            return None, True

        if hit_tp1:
            trade["tp1_hit"] = True
            note = "First target reached."
            if BREAKEVEN_AFTER_TP1:
                trade["stop"] = trade["entry"]
                note += " Stop moved to breakeven - remaining position is risk-free."
            subject, body, html = lifecycle_email(asset, "TP1", trade,
                                                  trade["tp1"], c["t"], note)
            send_email(subject, body, html)
            log(f"{sym}: TP1 HIT at ${fmt_px(trade['tp1'])} -> email sent")
            changed = True

        trade["checked_t"] = c["t"]

    if new_candles:
        trade["checked_t"] = last_closed_t
        changed = True

    if sig:
        long = trade["verdict"] == "LONG"
        collapsed = (sig["score"] <= -INVALIDATION_SCORE if long
                     else sig["score"] >= INVALIDATION_SCORE)
        if collapsed:
            subject, body, html = lifecycle_email(
                asset, "INVALIDATED", trade, sig["price"], sig["t"],
                f"Confluence is now {sig['score']:+d} against the position before "
                "any level was hit. The setup that justified this trade is gone - "
                "consider exiting.")
            send_email(subject, body, html)
            log(f"{sym}: INVALIDATED (score {sig['score']:+d}) -> email sent")
            return None, True

    return trade, changed


def check_asset(asset, state):
    sym = asset["symbol"]
    source, candles = fetch_candles(asset)
    if not candles:
        log(f"{sym}: all data sources failed - will retry next run.")
        return False

    sig = analyze(candles)
    if not sig:
        log(f"{sym}: not enough history to evaluate.")
        return False

    ast = state.get(sym, {"last_verdict": None, "last_alert_candle": 0, "trade": None})
    ast.setdefault("trade", None)
    trade = ast["trade"]
    last_closed_t = candles[-2]["t"]
    changed = False

    log(f"{sym} | {source} | ${fmt_px(sig['price'])} | verdict {sig['verdict']} "
        f"(score {sig['score']:+d}/{MAX_SCORE}) | state: {ast.get('last_verdict')}"
        f"{' | open ' + trade['verdict'] + ' trade' if trade else ''}")

    if trade:
        trade, ch = process_open_trade(asset, trade, candles, last_closed_t, sig)
        changed = changed or ch
        if trade is None and ch:
            ast["last_verdict"] = sig["verdict"]
        ast["trade"] = trade

    flipped = (sig["verdict"] in ("LONG", "SHORT")
               and sig["verdict"] != ast.get("last_verdict")
               and sig["t"] != ast.get("last_alert_candle"))
    if flipped and ast["trade"] is None and sig["plan"]:
        subject, body, html = entry_email(asset, sig, source)
        send_email(subject, body, html)
        log(f"ALERT SENT -> {EMAIL_TO}: {subject}")
        p = sig["plan"]
        ast["trade"] = {"verdict": sig["verdict"], "entry": p["entry"],
                        "stop": p["stop"], "tp1": p["tp1"], "tp2": p["tp2"],
                        "tp1_hit": False, "opened_t": sig["t"],
                        "checked_t": sig["t"]}
        ast["last_verdict"] = sig["verdict"]
        ast["last_alert_candle"] = sig["t"]
        changed = True
    elif sig["verdict"] in ("LONG", "SHORT") and ast.get("last_verdict") != sig["verdict"]:
        ast["last_verdict"] = sig["verdict"]
        changed = True

    state[sym] = ast
    return changed


def check_once():
    state = load_state()
    changed = False
    for asset in ASSETS:
        try:
            changed = check_asset(asset, state) or changed
        except Exception:
            save_state(state)
            raise
    if changed or not STATE_FILE.exists():
        save_state(state)


def seconds_to_next_close(buffer_s=20):
    period = CANDLE_MINUTES * 60
    return period - (time.time() % period) + buffer_s


def run_loop():
    log("Signal alert agent started (loop mode). Ctrl+C to stop.")
    check_once()
    while True:
        wait = seconds_to_next_close()
        log(f"Next check in {wait / 60:.1f} min")
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
        watched = ", ".join(a["symbol"] for a in ASSETS)
        send_email("Signal alert agent - test email",
                   f"Your alert pipeline works. Watching: {watched} on the "
                   f"{CANDLE_MINUTES}m timeframe with S/R + trend + price action "
                   "confluence. You'll get emails for entries, TP1/TP2 hits, "
                   "stop-outs, and signal invalidations.")
        print(f"Test email sent to {EMAIL_TO}.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
