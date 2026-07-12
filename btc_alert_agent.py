#!/usr/bin/env python3
"""
BTC SIGNAL ALERT AGENT - 30m swing - GitHub Actions edition
------------------------------------------------------------
Runs in the cloud on GitHub's free tier. A scheduled workflow calls this
script every 30 minutes; it checks the last closed candle and emails you
when the 5-factor confluence engine flips to LONG or SHORT.

Config comes from environment variables (set as GitHub repo Secrets):
  EMAIL_FROM           Gmail address that sends alerts
  EMAIL_APP_PASSWORD   16-char Gmail app password
  EMAIL_TO             where alerts are delivered

Modes:
  python3 btc_alert_agent.py --once    single check (what the workflow runs)
  python3 btc_alert_agent.py --test    send a test email
  python3 btc_alert_agent.py --loop    run continuously (local PC use)
"""

import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

# ============================= CONFIG ======================================
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

CANDLE_MINUTES = 30
LOOKBACK = 500                 # candles of history (~10.4 days)
SIGNAL_THRESHOLD = 3           # confluence score needed (out of +-5)
ATR_STOP_MULT = 1.5
ATR_TP1_MULT = 2.0
ATR_TP2_MULT = 3.0
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
# ===========================================================================

CANDLE_MS = CANDLE_MINUTES * 60 * 1000


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}",
          flush=True)


# --------------------------- data sources ---------------------------------
def http_json(url, payload=None, timeout=20):
    headers = {"Content-Type": "application/json", "User-Agent": "btc-alert-agent/1.0"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_hyperliquid():
    end = int(time.time() * 1000)
    start = end - LOOKBACK * CANDLE_MS
    data = http_json("https://api.hyperliquid.xyz/info", {
        "type": "candleSnapshot",
        "req": {"coin": "BTC", "interval": f"{CANDLE_MINUTES}m",
                "startTime": start, "endTime": end},
    })
    return [{"t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])}
            for c in data]


def fetch_binance():
    data = http_json(f"https://api.binance.com/api/v3/klines"
                     f"?symbol=BTCUSDT&interval={CANDLE_MINUTES}m&limit={LOOKBACK}")
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in data]


def fetch_kraken():
    data = http_json(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={CANDLE_MINUTES}")
    key = next(k for k in data["result"] if k != "last")
    return [{"t": k[0] * 1000, "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[6])}
            for k in data["result"][key]]


SOURCES = [("Hyperliquid BTC-PERP", fetch_hyperliquid),
           ("Binance BTCUSDT", fetch_binance),
           ("Kraken XBTUSD", fetch_kraken)]


def fetch_candles():
    for name, fn in SOURCES:
        try:
            candles = fn()
            if len(candles) >= 210:
                return name, candles
            log(f"{name}: only {len(candles)} candles, trying next source")
        except Exception as e:
            log(f"{name} failed: {e}")
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

    if vol_avg[i] is not None and vols[i] > vol_avg[i] * 1.2:
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
        plan = {"entry": px,
                "stop": px - sign * ATR_STOP_MULT * a,
                "tp1": px + sign * ATR_TP1_MULT * a,
                "tp2": px + sign * ATR_TP2_MULT * a,
                "atr": a}
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

    def ev(i):
        res = evaluate(i, closes, e20, e50, e200, r, hist, a, vols, vol_avg)
        if res:
            res["t"] = candles[i]["t"]
        return res

    last_closed = len(candles) - 2  # final candle in feed may still be forming
    return ev(last_closed)


# ------------------------------- email ------------------------------------
def send_email(subject, body):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD.replace(" ", ""))
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())


def alert_email(sig, source):
    v = sig["verdict"]
    icon = "[LONG]" if v == "LONG" else "[SHORT]"
    p = sig["plan"]
    ts = datetime.fromtimestamp(sig["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"{icon} BTC {v} signal @ ${sig['price']:,.0f} - 30m"
    lines = [
        f"BTC 30m confluence flipped to {v}",
        f"Candle close: {ts}",
        f"Source: {source}",
        f"Confluence score: {sig['score']:+d} / +-5",
        "",
        "TRADE PLAN (ATR-sized)",
        f"  Entry : ${p['entry']:,.0f}",
        f"  Stop  : ${p['stop']:,.0f}  ({ATR_STOP_MULT} x ATR)",
        f"  TP1   : ${p['tp1']:,.0f}  (R {ATR_TP1_MULT / ATR_STOP_MULT:.2f})",
        f"  TP2   : ${p['tp2']:,.0f}  (R {ATR_TP2_MULT / ATR_STOP_MULT:.2f})",
        f"  ATR14 : ${p['atr']:,.0f}",
        "",
        "FACTORS",
    ]
    for k, desc, _ in sig["factors"]:
        lines.append(f"  {k:<7}: {desc}")
    lines += ["", "-" * 50,
              "Automated technical signal for research - not financial advice.",
              "Any single signal can fail; size accordingly."]
    return subject, "\n".join(lines)


# ------------------------------- state ------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_verdict": None, "last_alert_candle": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ------------------------------- agent ------------------------------------
def check_once():
    source, candles = fetch_candles()
    if not candles:
        log("All data sources failed - will retry next scheduled run.")
        return

    sig = analyze(candles)
    if not sig:
        log("Not enough history to evaluate.")
        return

    state = load_state()
    log(f"{source} | ${sig['price']:,.0f} | verdict {sig['verdict']} "
        f"(score {sig['score']:+d}) | previous state: {state.get('last_verdict')}")

    flipped = (sig["verdict"] in ("LONG", "SHORT")
               and sig["verdict"] != state.get("last_verdict")
               and sig["t"] != state.get("last_alert_candle"))

    if flipped:
        subject, body = alert_email(sig, source)
        send_email(subject, body)  # let exceptions fail the run visibly
        log(f"ALERT SENT -> {EMAIL_TO}: {subject}")
        state["last_verdict"] = sig["verdict"]
        state["last_alert_candle"] = sig["t"]
        save_state(state)
    elif sig["verdict"] in ("LONG", "SHORT"):
        state["last_verdict"] = sig["verdict"]
        save_state(state)
    else:
        log("No flip - nothing to send.")


def seconds_to_next_close(buffer_s=20):
    period = CANDLE_MINUTES * 60
    return period - (time.time() % period) + buffer_s


def run_loop():
    log("BTC alert agent started (loop mode). Ctrl+C to stop.")
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
        send_email("BTC alert agent - test email",
                   "Your alert pipeline works. The agent will email you here "
                   "whenever the 30m confluence flips to LONG or SHORT.")
        print(f"Test email sent to {EMAIL_TO}.")
    elif "--loop" in sys.argv:
        run_loop()
    else:
        check_once()
