#!/usr/bin/env python3
"""
MULTI-ASSET SIGNAL ALERT AGENT - 30m swing - full trade lifecycle
------------------------------------------------------------------
Watches BTC, TSLA, SP500 and SKHX on Hyperliquid with a 5-factor
indicator confluence engine:

  1. Trend    EMA20 vs EMA50                    (+-1)
  2. Bias     price vs EMA200                   (+-1)
  3. MACD     histogram sign + slope            (+-1)
  4. RSI      bullish / bearish zone            (+-1)
  5. Volume   expansion vs 20-candle average    (+-1)

Score range -5..+5. Entry at |score| >= SIGNAL_THRESHOLD.

Lifecycle alerts: ENTRY, TP1 (stop -> breakeven), TP2, STOPPED OUT,
INVALIDATED. Each run surfaces a summary via GitHub annotations, the
job summary panel, and run_summary.txt (used as the commit message).

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
    {"symbol": "BTC",   "label": "BTC-PERP",        "hl_coin": "BTC",
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
    {"symbol": "TSLA",  "label": "TSLA-PERP (xyz)", "hl_coin": "xyz:TSLA",
     "fallbacks": ["yahoo:TSLA"]},
    {"symbol": "SP500", "label": "SP500-PERP (xyz)", "hl_coin": "xyz:SP500",
     "fallbacks": ["yahoo:^GSPC"]},
    {"symbol": "SKHX",  "label": "SKHX-PERP (xyz)", "hl_coin": "xyz:SKHX",
     "fallbacks": ["yahoo:SKHY"]},
]

CANDLE_MINUTES = 30
LOOKBACK = 500
# Timezone shown in alert emails (IANA name). Common options:
#   "America/New_York"  "America/Chicago"  "America/Denver"
#   "America/Los_Angeles"  "America/Port_of_Spain"  "Europe/London"
TIMEZONE = "America/New_York"

MAX_SCORE = 5
SIGNAL_THRESHOLD = 3           # |score| needed to enter (out of +-5)
INVALIDATION_SCORE = 1         # open LONG dies if score <= -1 (SHORT mirrored)
ATR_STOP_MULT = 1.5
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


# --------------------------- run summary -----------------------------------
RUN_ALERTS = []   # alert events fired during this run
RUN_STATUS = []   # one-liner per asset, e.g. "BTC WAIT (+1)"


def write_run_summary():
    """Surface the run result in GitHub's UI so logs never need opening:
    a ::notice annotation, the job summary panel, and run_summary.txt
    (which the workflow uses as the state-commit message)."""
    if RUN_ALERTS:
        headline = "ALERT SENT: " + " | ".join(RUN_ALERTS)
    else:
        headline = "No signal - " + (", ".join(RUN_STATUS) or "no assets checked")
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
               "User-Agent": "Mozilla/5.0 (signal-alert-agent/5.0)"}
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


# ---------------------------- signal engine --------------------------------
def evaluate(i, closes, e20, e50, e200, rsi_arr, hist, atr_arr, vols, vol_avg):
    if i < 200:
        return None
    factors, score = [], 0
    px = closes[i]

    if e20[i] > e50[i]:
        score += 1; factors.append(("Trend", "EMA20 above EMA50", 1))
    else:
        score -= 1; factors.append(("Trend", "EMA20 below EMA50", -1))

    if px > e200[i]:
        score += 1; factors.append(("Bias", "Price above EMA200", 1))
    else:
        score -= 1; factors.append(("Bias", "Price below EMA200", -1))

    h0, h1 = hist[i], hist[i - 1]
    if h0 is not None and h1 is not None:
        if h0 > 0 and h0 > h1:
            score += 1; factors.append(("MACD", "Histogram positive & rising", 1))
        elif h0 < 0 and h0 < h1:
            score -= 1; factors.append(("MACD", "Histogram negative & falling", -1))
        else:
            factors.append(("MACD", "Positive, fading" if h0 > 0 else "Negative, fading", 0))

    r = rsi_arr[i]
    if r is not None:
        if r > 70:
            factors.append(("RSI", f"{r:.1f} - overbought", 0))
        elif r < 30:
            factors.append(("RSI", f"{r:.1f} - oversold", 0))
        elif r > 55:
            score += 1; factors.append(("RSI", f"{r:.1f} - bullish zone", 1))
        elif r < 45:
            score -= 1; factors.append(("RSI", f"{r:.1f} - bearish zone", -1))
        else:
            factors.append(("RSI", f"{r:.1f} - neutral", 0))

    if vol_avg[i] is not None and vol_avg[i] > 0 and vols[i] > vol_avg[i] * 1.2:
        up = closes[i] > closes[i - 1]
        score += 1 if up else -1
        factors.append(("Volume", f"{vols[i] / vol_avg[i]:.1f}x avg on "
                                  f"{'up' if up else 'down'} candle", 1 if up else -1))
    else:
        factors.append(("Volume", "No expansion", 0))

    verdict = ("LONG" if score >= SIGNAL_THRESHOLD
               else "SHORT" if score <= -SIGNAL_THRESHOLD else "WAIT")
    a = atr_arr[i] or 0
    plan = None
    if verdict != "WAIT":
        sign = 1 if verdict == "LONG" else -1
        stop = px - sign * ATR_STOP_MULT * a
        tp1 = px + sign * ATR_TP1_MULT * a
        tp2 = px + sign * ATR_TP2_MULT * a
        risk = abs(px - stop)
        plan = {"entry": px, "stop": stop, "tp1": tp1, "tp2": tp2, "atr": a,
                "rr1": abs(tp1 - px) / risk if risk else 0,
                "rr2": abs(tp2 - px) / risk if risk else 0}
    return {"score": score, "factors": factors, "verdict": verdict,
            "plan": plan, "price": px, "t": None}


def analyze(candles):
    closes = [c["c"] for c in candles]
    vols = [c["v"] for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes)
    _, _, hist = macd(closes)
    a = atr(candles)
    vol_avg = sma(vols, 20)
    last_closed = len(candles) - 2
    res = evaluate(last_closed, closes, e20, e50, e200, r, hist, a, vols, vol_avg)
    if res:
        res["t"] = candles[last_closed]["t"]
    return res


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
            + _row(f"Stop &middot; {ATR_STOP_MULT} &times; ATR", "$" + fmt_px(p["stop"]), "#E8524A", True)
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
        "TRADE PLAN (ATR-sized)",
        f"  Entry : ${fmt_px(p['entry'])}",
        f"  Stop  : ${fmt_px(p['stop'])}  ({ATR_STOP_MULT} x ATR)",
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
                    "Initial ATR stop was hit. Wait for the next confluence flip.")
            subject, body, html = lifecycle_email(asset, "STOP", trade,
                                                  trade["stop"], c["t"], note)
            send_email(subject, body, html)
            log(f"{sym}: STOPPED OUT at ${fmt_px(trade['stop'])} -> email sent")
            RUN_ALERTS.append(f"{sym} STOPPED OUT ({pnl_pct(trade, trade['stop']):+.2f}%)")
            return None, True

        if hit_tp2:
            subject, body, html = lifecycle_email(
                asset, "TP2", trade, trade["tp2"], c["t"],
                "Full target reached. Trade closed.")
            send_email(subject, body, html)
            log(f"{sym}: TP2 HIT at ${fmt_px(trade['tp2'])} -> email sent")
            RUN_ALERTS.append(f"{sym} TP2 HIT ({pnl_pct(trade, trade['tp2']):+.2f}%)")
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
            RUN_ALERTS.append(f"{sym} TP1 HIT ({pnl_pct(trade, trade['tp1']):+.2f}%)")
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
            RUN_ALERTS.append(f"{sym} {trade['verdict']} INVALIDATED")
            return None, True

    return trade, changed


def check_asset(asset, state):
    sym = asset["symbol"]
    source, candles = fetch_candles(asset)
    if not candles:
        log(f"{sym}: all data sources failed - will retry next run.")
        RUN_STATUS.append(f"{sym} feed failed")
        return False

    sig = analyze(candles)
    if not sig:
        log(f"{sym}: not enough history to evaluate.")
        RUN_STATUS.append(f"{sym} insufficient history")
        return False

    ast = state.get(sym, {"last_verdict": None, "last_alert_candle": 0, "trade": None})
    ast.setdefault("trade", None)
    trade = ast["trade"]
    last_closed_t = candles[-2]["t"]
    changed = False

    log(f"{sym} | {source} | ${fmt_px(sig['price'])} | verdict {sig['verdict']} "
        f"(score {sig['score']:+d}/{MAX_SCORE}) | state: {ast.get('last_verdict')}"
        f"{' | open ' + trade['verdict'] + ' trade' if trade else ''}")
    RUN_STATUS.append(f"{sym} {sig['verdict']} ({sig['score']:+d})"
                      + (f", open {trade['verdict']}" if trade else ""))

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
        RUN_ALERTS.append(f"{sym} {sig['verdict']} entry @ ${fmt_px(sig['price'])}")
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
    RUN_ALERTS.clear()
    RUN_STATUS.clear()
    state = load_state()
    changed = False
    try:
        for asset in ASSETS:
            try:
                changed = check_asset(asset, state) or changed
            except Exception:
                save_state(state)
                raise
        if changed or not STATE_FILE.exists():
            save_state(state)
    finally:
        write_run_summary()


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
                   f"{CANDLE_MINUTES}m timeframe with the 5-factor indicator "
                   "confluence engine. You'll get emails for entries, TP1/TP2 "
                   "hits, stop-outs, and signal invalidations.")
        print(f"Test email sent to {EMAIL_TO}.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
