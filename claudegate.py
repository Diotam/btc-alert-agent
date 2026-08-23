#!/usr/bin/env python3
"""Claude as the ADJUDICATOR on impulse-MACD setups.

The deterministic engine stays the scanner: it computes the indicator, finds
crossovers and coils, and applies the band. That arithmetic is verified and
should not be handed to a model. What IS handed over is the judgement call
that thresholds kept failing at - whether a crossover is a real turn or a
sideways tangle - because every complaint on 20-21 Aug was that, and every
attempt to encode it as a number (IM_TURN_MIN 0.045 -> 0.10 -> 0) either
admitted the junk or switched the pathway off entirely.

Called AFTER a signal fires and BEFORE fire_entry. Returns a verdict the
caller can act on, never raises, and on any failure returns TAKE with the
engine's own numbers so a dead API degrades to yesterday's behaviour.

Every number the model returns is clamped in Python. The model advises; the
bounds are not negotiable.
"""
import json
import os
import time
import urllib.error
import urllib.request

# ----------------------------------------------------------------- config
CG_ON = True                       # False bypasses the model entirely
CG_MODEL = "claude-sonnet-4-5"     # override with CG_MODEL in env
CG_MAX_TOKENS = 700
CG_TIMEOUT_S = 25
CG_RETRIES = 1                     # one retry, then fail open
CG_BARS = 24                       # bars of history shown to the model

# ---- CLAMPS. The model can move within these and no further. A single bad
# response must not be able to size a position off a 0.01% stop.
CG_MIN_STOP_PCT = 0.30             # of price
CG_MAX_STOP_PCT = 3.00             # 21 Aug: stops over 3% were the worst losses
CG_MIN_RR = 1.0
CG_MAX_RR = 3.0
CG_MAX_QTY_MULT = 1.0              # the model may size DOWN, never up

SYSTEM = """You adjudicate trade setups from a deterministic Impulse MACD \
(LazyBear) scanner on 30m crypto and equity-synthetic perpetuals.

THE STRATEGY DELIBERATELY FADES EXTENDED MOVES. Pathway 1 shorts a downward \
crossover that happens ABOVE the overbought band and buys an upward crossover \
BELOW the oversold band. So md sitting far past the band, for many bars, is \
THE SETUP - not a defect. "Stale extension", "parked above the band", \
"momentum exhausting rather than initiating" and "the move already happened" \
are descriptions of the trade being taken, and are NOT reasons to skip. A \
crossover after a long extension is exactly what this strategy waits for.

Pathway 2 is different: it buys the first push out of a long flat range, in \
the direction of the push.

The scanner has ALREADY verified every mechanical rule - the crossover, the \
band, the slope, the flat run. Do not re-check them and do not second-guess \
the strategy. Assume the setup is valid unless you can point to something \
that makes the DATA itself untrustworthy.

SKIP only for these:
- a single-bar flash crash or liquidation wick, where the crossover is an \
  artifact of one anomalous bar rather than the price action around it
- md and the signal line overlapping and re-crossing repeatedly with no \
  separation between them, so the crossover is mechanical tangling
- a pathway-2 push that barely leaves zero - a flicker rather than an \
  expansion
- price bars that look broken: repeated identical values, impossible gaps

TAKE everything else. If in doubt, TAKE - the scanner's rules already \
filtered heavily, and skipping a valid setup costs more than taking a \
marginal one.

You may tighten the stop or reduce size. You may not loosen or increase them.

Reply with ONLY a JSON object, no prose and no code fences:
{"decision":"TAKE"|"SKIP","stop":number|null,"rr":number|null,
 "qty":number|null,"reasoning":"2-3 sentences","confidence":"LOW"|"MEDIUM"|"HIGH"}
stop is a PRICE. rr is the target as a multiple of risk. qty is units.
Use null to accept the scanner's value."""


def _key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _model():
    return os.environ.get("CG_MODEL", CG_MODEL).strip() or CG_MODEL


def _call(prompt, log):
    """One Messages API call. Returns the text body, or None."""
    body = json.dumps({
        "model": _model(),
        "max_tokens": CG_MAX_TOKENS,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json",
                 "anthropic-version": "2023-06-01",
                 "x-api-key": _key()})
    for attempt in range(CG_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=CG_TIMEOUT_S) as r:
                d = json.loads(r.read().decode())
            return "".join(b.get("text", "") for b in d.get("content", [])
                           if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:180]
            except Exception:
                pass
            log(f"claude: HTTP {e.code} {detail}")
            if e.code in (429, 500, 502, 503, 529) and attempt < CG_RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except Exception as e:
            log(f"claude: {type(e).__name__}: {e}")
            if attempt < CG_RETRIES:
                time.sleep(1.5)
                continue
            return None
    return None


def _parse(text):
    """Pull the JSON object out of the reply, fences or not."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def _prompt(sym, path, side, entry, stop, rr, qty, md, sb, sh, band, bars):
    long_ = side == "LONG"
    rows = []
    for c, m, s, h in bars:
        rows.append(f"  {c['o']:.6g} {c['h']:.6g} {c['l']:.6g} {c['c']:.6g}"
                    f"   md {m:+.4e}  sb {s:+.4e}  sh {h:+.4e}")
    return (
        f"SYMBOL {sym}   {side}   pathway: {path}\n"
        f"entry {entry:.6g}\n"
        f"scanner stop {stop:.6g} "
        f"({abs(entry - stop) / entry * 100:.2f}% of price), "
        f"target {rr}R, qty {qty:.6g}\n\n"
        f"impulse MACD now: md {md:+.4e}  signal {sb:+.4e}  hist {sh:+.4e}\n"
        f"band +/-{band:.4e}  -> md is {md / band:+.2f}x the band\n\n"
        f"last {len(bars)} closed bars (O H L C, then the indicator):\n"
        + "\n".join(rows) + "\n\n"
        f"Take this {side}, or skip it?")


def adjudicate(sym, path, side, entry, stop, rr, qty, candles, md, sb, sh,
               band, log=print):
    """(take, stop, rr, qty, note).

    NEVER raises. On any failure returns the scanner's own numbers with
    take=True, so a dead API degrades to the deterministic engine.
    """
    if not CG_ON:
        return True, stop, rr, qty, "adjudicator off"
    if not _key():
        log("claude: no ANTHROPIC_API_KEY - passing the signal through")
        return True, stop, rr, qty, "no api key - not adjudicated"
    try:
        n = min(CG_BARS, len(candles) - 1, len(md) - 1)
        bars = [(candles[k], md[k], sb[k], sh[k])
                for k in range(len(md) - n, len(md))]
        text = _call(_prompt(sym, path, side, entry, stop, rr, qty,
                             md[-1], sb[-1], sh[-1], band, bars), log)
        d = _parse(text)
        if not d:
            log(f"{sym}: claude returned nothing usable - passing through")
            return True, stop, rr, qty, "adjudicator unreachable"

        why = str(d.get("reasoning", ""))[:500]
        conf = str(d.get("confidence", "")).upper()
        if str(d.get("decision", "")).upper() == "SKIP":
            return False, stop, rr, qty, f"{why} [{conf}]"

        long_ = side == "LONG"
        # ---- STOP: only ever tightened, and only inside the bounds
        ns = d.get("stop")
        if isinstance(ns, (int, float)) and ns > 0:
            ns = float(ns)
            pct = abs(entry - ns) / entry * 100
            ok_side = (ns < entry) if long_ else (ns > entry)
            tighter = abs(entry - ns) <= abs(entry - stop)
            if ok_side and tighter and CG_MIN_STOP_PCT <= pct <= CG_MAX_STOP_PCT:
                stop = ns
            else:
                log(f"{sym}: claude stop {ns:.6g} ({pct:.2f}%) rejected - "
                    f"keeping {stop:.6g}")
        # ---- RR
        nr = d.get("rr")
        if isinstance(nr, (int, float)):
            nr = float(nr)
            if CG_MIN_RR <= nr <= CG_MAX_RR:
                rr = nr
            else:
                log(f"{sym}: claude rr {nr} outside "
                    f"{CG_MIN_RR}-{CG_MAX_RR} - keeping {rr}")
        # ---- QTY: down only
        nq = d.get("qty")
        if isinstance(nq, (int, float)) and nq > 0:
            nq = float(nq)
            if nq <= qty * CG_MAX_QTY_MULT:
                qty = nq
            else:
                log(f"{sym}: claude qty {nq:.6g} above the cap - "
                    f"keeping {qty:.6g}")
        return True, stop, rr, qty, f"{why} [{conf}]"
    except Exception as e:
        log(f"{sym}: adjudicator failed {type(e).__name__}: {e} - "
            f"passing through")
        return True, stop, rr, qty, "adjudicator errored"
