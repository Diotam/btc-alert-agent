#!/usr/bin/env python3
"""
SMOOTHED HEIKIN ASHI DOJI AGENT
--------------------------------
One strategy, long side described; shorts mirror it exactly.

  1. TREND - a run of red HA candles, any length, whose biggest body is at
     least HA_MIN_BODY_PCT of price so a flat series cannot qualify. There
     is no minimum run length and no requirement that the bodies expanded:
     both were removed 3 Aug.
  2. DOJI - an HA body no more than HA_DOJI_FRACTION of the biggest body in
     that run. The smoothed HA has stalled, and that stall IS the turn.
  3. ENTER on the doji, in the direction opposite the trend that led into
     it. Price does NOT have to come back and retest anything.

  stop   = the REAL candle's extreme at the doji - its low for a long, its
           high for a short. Not the HA level: HA highs and lows are EMA
           averages that need never have printed
  target = HA_RR x that distance. HA_PARTIAL of the position is booked there
           and the stop moves to entry; the remainder is held until the
           smoothed HA flips back against the trade.

The HA series is a derived band: EMA the OHLC, build Heikin Ashi on that,
then EMA the result. It decides WHEN to trade; the real candles decide at
what price, so every level the agent sends to the exchange is one the market
actually traded.

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
DISCOVER_DEXES = False             # scan HIP-3 builder venues. OFF as of
                                   # 2 Aug: with EXEC_BUILDER_DEXES empty
                                   # they could only ever alert, never trade,
                                   # so every xyz signal was noise that also
                                   # booked paper rows into the ledger. False
                                   # falls back to DEXES = [""], the main dex
                                   # alone. Turn both back on together if the
                                   # xyz pool is ever funded again
ADMIT_COMMODITIES = True
ADMIT_STOCKS = True                # equities IN the universe. They are
                                   # is one line to flip back
DEXES = [""]                       # fallback when dex discovery fails
# EXACT names, never prefixes. This used to be a startswith() match, which
# on an equities venue swallowed every ticker beginning with CL, NG or HG -
# CLF, CLX, CLSK, NGD, HGV. Those were then classified as commodities, let
# in at the lower commodity volume floor, and - worse - they bypassed
# ADMIT_STOCKS entirely, so equities that were switched off still entered
# the universe through this door.
COMMODITY_TICKERS = ("XAU", "GOLD", "XAG", "SILVER", "XPT", "PLAT",
                     "XPD", "PALLADIUM", "CL", "WTI", "OIL", "BRENT",
                     "BRENTOIL", "NG", "NATGAS", "HG", "COPPER")
STOCK_DEXES = ("xyz",)             # TradeXYZ equities venue
EXEC_BUILDER_DEXES = ()            # builder dexes to trade AUTOMATICALLY,
                                   # e.g. ("xyz",). OFF as of 2 Aug: the xyz
                                   # dex has its OWN collateral pool and it
                                   # holds $0, so every order there was
                                   # rejected for insufficient margin no
                                   # matter what the main account held. Turn
                                   # back on once xyz is funded, or once the
                                   # account is switched to Unified mode so
                                   # the main USDC balance collateralizes it.
                                   # The "xyz:GOLD" naming is this agent's
                                   # own - on Hyperliquid that market is just
                                   # GOLD, living on the xyz dex rather than
                                   # the main perp dex. The SDK reaches it
                                   # when the Exchange is built with
                                   # perp_dexs=[...]
MIN_DAY_VOLUME_USD = 2_000_000     # crypto floor, 24h notional
COMMODITY_MIN_VOLUME_USD = 5_000_000
STOCK_MIN_VOLUME_USD = 5_000_000
ONLY = []                          # trade ONLY these symbols ([] = whole universe)
ONLY_SYMBOLS = ()                  # if non-empty, the universe is EXACTLY
                                   # these and nothing else - volume floors,
                                   # MAX_ASSETS and dex discovery no longer
                                   # decide anything. BTC-only as of 4 Aug,
                                   # his call. Empty tuple restores the
                                   # discovered universe
EXCLUDE = []                       # never trade these (matches the base name
                                   # on any venue). PUMP was removed from
                                   # this list 3 Aug - it trades again
MAX_ASSETS = 100

ASSETS = [                         # used when DISCOVER_ALL = False, or when
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",   # discovery fails
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- strategy dials -------------------------------------------------------
TF = "15m"                         # execution timeframe
SCAN_EVERY = "5m"                  # how often the loop wakes. Aligning it to
                                   # TF means one scan per candle. A shorter
                                   # pulse costs API calls but reacts sooner:
                                   # symbols with no open trade are skipped
                                   # until their candle closes either way, so
                                   # the extra scans only ever serve OPEN
                                   # trades - intrabar stop/target detection
                                   # and moving the stop to entry after the
                                   # partial fills
HA_SMOOTH_IN = 1                   # EMA applied to OHLC before building HA
HA_SMOOTH_OUT = 1                  # EMA applied to the HA output.
                                   # 1,1 = REGULAR Heikin Ashi, no smoothing
                                   # at all - the 4 Aug engine works on raw
                                   # HA, where wicks are meaningful. Any
                                   # smoothing averages wicks away and the
                                   # no-wick test stops meaning anything
BTC_TREND_SMOOTH = (5, 5)          # smoothing for the BTC CONTEXT line only,
                                   # deliberately lighter than the signal's
                                   # 10,10 so it turns sooner and reports
                                   # where BTC is now
BTC_TREND_TTL_S = 120              # one BTC fetch per scan, not per symbol
_BTC_CACHE = {"t": 0.0, "v": None}
HA_MIN_BODY_PCT = 0.05             # the trend run must contain at least one
                                   # HA body this big, as a % of price. Without
                                   # it a FLAT smoothed series satisfies
                                   # "strictly growing then strictly shrinking"
                                   # on noise in the fifth decimal and arms a
                                   # setup with no visible colour flip at all.
                                   # Measured: near-flat series produce bodies
                                   # of 0.005-0.034%, normal ones 0.081%+
HA_MIN_FLIP_BODY_PCT = 0.002       # the DOJI/flip candle must itself be a real
                                   # candle, this % of price or bigger. 0 = off.
                                   # HA_DOJI_FRACTION puts a CEILING on that
                                   # body; this is the FLOOR, so the two form a
                                   # band. Added after FARTCOIN entered on a
                                   # colour flip whose body was 0.006% - under
                                   # a tick, invisible on a chart, a rounding
                                   # artefact rather than a reversal.
HA_DOJI_COLOUR = "flip"            # which colour the doji must be, relative
                                   # to the trend that led into it.
                                   #   "same" - a RED doji ends a downtrend
                                   #            and turns us LONG; a GREEN
                                   #            doji ends an uptrend and
                                   #            turns us SHORT. The stall is
                                   #            read BEFORE the colour turns,
                                   #            so entries come earlier and
                                   #            the trend is still nominally
                                   #            intact when we take them.
                                   #   "flip" - the doji must have CHANGED
                                   #            colour first. Later, more
                                   #            confirmation, fewer trades.
                                   #   "any"  - either colour counts.
HA_MODE = "reversal"               # what the doji MEANS.
                                   #   "reversal"     - a doji ending a red
                                   #                    run turns us LONG.
                                   #                    Every version before
                                   #                    3 Aug worked this way.
                                   #   "continuation" - the same doji turns
                                   #                    us SHORT: the stall is
                                   #                    a pause in the move,
                                   #                    not the end of it.
                                   # Detection is IDENTICAL either way - only
                                   # the resulting side flips
EMA_FILTER_TF = "1h"               # TIMEFRAME the filter's EMA is measured
                                   # on, which need not be TF. A 50 EMA on
                                   # 15m spans about 12 hours, so an ordinary
                                   # pullback inside a two-day uptrend
                                   # crosses it and the filter flips - it was
                                   # reading the local swing, not the trend.
                                   # On 1h the same 50 EMA spans ~2 days.
                                   # Built by RESAMPLING the candles already
                                   # fetched, so it costs no extra request;
                                   # must be a whole multiple of TF, and
                                   # LOOKBACK has to leave enough bars after
                                   # the resample. "4h" is the slower option
EMA_RETEST_PCT = 0.75              # entry must be within this % of the EMA -
                                   # a RETEST, not just the right side of it.
                                   # Turns the filter from "shorts anywhere
                                   # below the EMA" into "shorts where price
                                   # has rallied back INTO the EMA and
                                   # failed". The existing pattern already
                                   # fits: that rally is a green run, and its
                                   # flip at the EMA is the short trigger.
                                   # 0 disables, restoring side-only
EMA_FILTER_LEN = 50                # trend filter on the REAL closes. Only
                                   # SHORT while price is BELOW this EMA and
                                   # only LONG while it is above, so the
                                   # reversal is never taken against the
                                   # bigger trend. Measured on the last
                                   # CLOSED candle - the no-wick bar - since
                                   # the entry bar is still forming when the
                                   # signal is read. 0 disables the filter
HA_FADE_BARS = 2                   # trailing HA bodies that must SHRINK
                                   # into the turn - "as the candles start
                                   # getting smaller". 0 disables
HA_NOWICK_TOL_PCT = 5.0            # a candle counts as NO-WICK when the wick
                                   # on the trade's side is at most this % of
                                   # its own body. Exact zeros are rare even
                                   # on raw HA, so 0 makes the engine almost
                                   # silent. The side that matters is the one
                                   # the trade runs AGAINST: upper wick for a
                                   # short, lower wick for a long - that is
                                   # the classic HA conviction candle
ENTRY_AT_OPEN = True               # enter at the OPEN of the candle AFTER
                                   # the no-wick bar, not at a close. Every
                                   # engine before 4 Aug entered on a close
HA_MIN_RUN_PCT = 1.0               # MINIMUM MOVE across the run, start to
                                   # flip, as a % of price. HA_MIN_RUN counts
                                   # CANDLES, so eight bars drifting sideways
                                   # scored the same as eight falling hard -
                                   # and only the second is a trend worth
                                   # fading. HA_MIN_BODY_PCT does not cover
                                   # this: it only asks that ONE body in the
                                   # run clears a floor, not that the run
                                   # went anywhere. 0 disables
HA_MIN_RUN = 4                     # MINIMUM trend-coloured HA candles before
                                   # the flip counts. Back on 3 Aug at 5,
                                   # after LIT showed a ONE-candle red run
                                   # inside an uptrend being read as a trend
                                   # and its next green candle as a flip.
                                   # 15 silenced the engine; 1 lets noise
                                   # through. 0 disables the check
HA_CONFIRM_BARS = 0                # HA candles that must follow the doji
                                   # before the trade is taken: each in the
                                   # doji's direction, each with a body
                                   # BIGGER than the one before, and on the
                                   # LAST of them the REAL candle must close
                                   # that way too. The doji says a turn is
                                   # happening; these say it took. 0 restores
                                   # the old fire-on-the-doji behaviour.
                                   # Entry moves to the last bar's close, so
                                   # price has usually left the 7-candle
                                   # stop behind - stops widen accordingly
HA_DOJI_FRACTION = 0.25            # a DOJI is an HA body this small relative
                                   # to the biggest body in the trend run that
                                   # led into it. Scale-free, so it adapts per
                                   # symbol instead of needing a fixed price
                                   # threshold. Entry happens ON the doji -
                                   # price does NOT have to come back and
                                   # retest anything
HA_RR = 1.5                        # first target = 3x the stop distance
HA_PARTIAL = 0.5                   # fraction booked there; the stop then moves
                                   # to entry and the remainder is held until
                                   # the HA flips against the trade
ADOPT_ORPHANS = "universe"         # a position on the exchange with NO
                                   # tracked trade at all - opened by hand,
                                   # or left behind when the agent booked a
                                   # close the venue never made.
                                   #   "universe" - adopt only symbols the
                                   #                agent already scans, so
                                   #                a deliberate hand trade
                                   #                on anything else is left
                                   #                alone
                                   #   "any"      - adopt everything held
                                   #   "off"      - report only, never adopt
REVERSE_ALERTS = True              # when the smoothed HA flips against an
                                   # open trade, the runner closes. That
                                   # same flip is a setup the OTHER way, so
                                   # send a REVERSE alert with levels ready
                                   # for Trust Wallet's flip button. ALERT
                                   # ONLY - the agent does not place it,
                                   # because a reverse skips the doji, the
                                   # confirmation bars and the run-length
                                   # floor that every other entry must clear
STOP_SOURCE = "run"                # WHERE the stop level comes from.
                                   #   "turn"  - the extreme of the FLIP bar
                                   #             and the no-wick bar, i.e.
                                   #             the two candles that ARE the
                                   #             reversal. Tightest, and the
                                   #             level that fails the moment
                                   #             the turn does.
                                   #   "run"   - the extreme of the whole
                                   #             fading run behind the flip.
                                   #             Structurally safer, but the
                                   #             entry can sit far above it
                                   #             after a fast bounce: SKR on
                                   #             5 Aug entered 5% above its
                                   #             own run low.
                                   #   "fixed" - STOP_LOOKBACK bars before
                                   #             the flip, ignoring the run.
                                   # "turn" is measured on TF, not STOP_TF -
                                   # these are two specific candles and
                                   # resampling would blur them into the hour
                                   # around them
STOP_TF = "1h"                     # TIMEFRAME the stop extreme is measured
                                   # on. The engine runs on TF, but a 15m
                                   # high is a local wick - the level a
                                   # reversal is really risking against is
                                   # the SWING high, which is what the same
                                   # lookback finds on 1h. Resampled from
                                   # the candles already fetched, and the
                                   # groups are ALIGNED so the last one ends
                                   # at the flip. Set to TF for the old
                                   # behaviour
STOP_LOOKBACK = 12                 # FLOOR on the stop window, COUNTED IN
                                   # STOP_TF BARS - so 1 means one hour, not
                                   # one 15m candle. It was 5 when the window
                                   # was measured on TF; left at 5 after the
                                   # move to 1h it meant FIVE HOURS, which on
                                   # SKR reached back past the setup and
                                   # found an unrelated low three hours
                                   # earlier: a 5.386% stop on a 15m trade.
                                   # At 1 the RUN decides the stop whenever
                                   # it spans more than an hour.
                                   # (original note) the stop is the extreme
                                   # of the LAST N
                                   # REAL candles ending at the doji - low
                                   # for a long, high for a short. 1 restores
                                   # the old behaviour (the doji candle's own
                                   # extreme). Wider stops are not a bug: the
                                   # 1 Aug ledger showed WINNERS carried the
                                   # wider stops (median 0.537% vs the
                                   # losers' 0.471%) and every max-width cap
                                   # tested made the book worse
MIN_TARGET_PCT = 1.5               # the TARGET must sit at least this far
                                   # from entry, as a % of price. 2.0 as of
                                   # 4 Aug, his call: "each setup needs to be
                                   # at least a 2% move". Expressed on the
                                   # target rather than the stop on purpose -
                                   # at HA_RR 3.0 it implies a stop of at
                                   # least 0.667%, but it stays correct if
                                   # HA_RR changes, and it is the move he
                                   # actually cares about. Round-trip fees
                                   # are ~0.083%, so 2% keeps them near 4%
                                   # of the gross rather than half of it.
                                   # 0 disables
MIN_STOP_PCT = 0.25                # skip entries whose stop sits closer than
                                   # this % of price - sub-noise stops just churn
TRACK_UNPLACED = False             # keep tracking a trade whose order never
                                   # reached the exchange. FALSE as of 5 Aug:
                                   # the alert still fires and says NOT
                                   # PLACED, but nothing is tracked, so the
                                   # dashboard shows ONLY what he actually
                                   # holds. Those paper rows could not be
                                   # cleared by closing anything by hand -
                                   # there was no position to close, and the
                                   # GONE reconciler skips sizeless trades on
                                   # purpose. True restores paper tracking,
                                   # which measures the strategy rather than
                                   # the account
STOP_WIDEN_PCT = 0                 # FLOOR that WIDENS. MIN_STOP_PCT skips a
                                   # setup whose stop is too tight; this one
                                   # pushes the stop OUT to at least this %
                                   # instead, so every stop lands in a band
                                   # between STOP_WIDEN_PCT and MAX_STOP_PCT.
                                   # 0 disables. Note the window can still
                                   # hand back something wider - this is a
                                   # floor, not a target
MAX_STOP_PCT = 10.0                # CLAMP the stop to at most this % from
                                   # entry. The window can reach back hours
                                   # and find a level 20%+ away; that is
                                   # beyond the LIQUIDATION point on any
                                   # market above ~5x, so the stop would
                                   # never trigger and the position would be
                                   # liquidated instead. Clamping keeps a
                                   # working stop. 0 disables the clamp

# --- alerts ---------------------------------------------------------------
ALERT_ENTRIES = True
ALERT_LIFECYCLE = True             # target, runner, stop and breakeven alerts

# --- execution ------------------------------------------------------------
EXEC_LIVE = True                   # place real orders. Back ON 4 Aug after a
                                   # night tracked-only. When False: every
                                   # entry alert carries "NOT PLACED on
                                   # Hyperliquid - live execution OFF"; the
                                   # ledger keeps booking outcomes, so it is
                                   # a PAPER record from here.
                                   # This also silences the exchange side of
                                   # reconciliation: POS_CACHE is only filled
                                   # when EXEC_LIVE, and protect_position,
                                   # cancel_stale_orders, close_position_live,
                                   # move_stop_live and ensure_flat all return
                                   # early. The agent stops touching the
                                   # account in any way
EXEC_LOG_ORDERS = True             # write every sized order to orders.log.
                                   # This is an audit trail only - it has never
                                   # gated execution. EXEC_LIVE alone decides
                                   # whether real orders are sent
EXEC_TESTNET = False               # False = MAINNET, real money
EXEC_MARGIN_MODE = "cross"         # "isolated" or "cross". CROSS as of
                                   # 3 Aug, reverting the isolated switch he
                                   # made earlier the same day. Cross lets
                                   # one bad position draw on the whole
                                   # account, but it keeps every position in
                                   # the SAME pool that free_collateral()
                                   # reads, which is the reporting the guard
                                   # was rebuilt around. The other
                                   # mode is still tried as a fallback,
                                   # since some markets refuse one of them.
                                   # ONLY AFFECTS NEW ENTRIES - positions
                                   # already open stay as they were opened
EXEC_HALT_FILE = "/opt/btc-agent/EXEC_HALT"
CLOSE_REQ_DIR = Path(__file__).parent / "close_requests"   # the dashboard
#   drops one marker file per symbol here; the agent closes that position on
#   its next scan and removes the file. A directory of markers rather than a
#   shared JSON file, so the two processes never write the same bytes   # touch this to stop new entries
EXEC_SIZING = "margin"             # "margin"   = a FIXED DOLLAR AMOUNT of
                                   #   COLLATERAL per trade. Position size is
                                   #   margin x leverage, so the agent must
                                   #   also SET the leverage or the figure is
                                   #   a guess - see EXEC_LEVERAGE below
                                   # "notional" = a fixed dollar POSITION
                                   #   size, whatever collateral that needs
                                   # "risk"     = fixed dollar LOSS at the
                                   #   stop; the position size then varies
EXEC_MARGIN_USD = 30.0             # collateral per trade in "margin" mode
EXEC_LEVERAGE = 999                # MAX leverage: eff_leverage() clamps this
                                   # to each market's own maximum, so 999
                                   # simply means "whatever this market
                                   # allows". Position size then varies a lot
                                   # by venue - a 40x market gets 4x the
                                   # position of a 10x one for the same
                                   # collateral. The leverage the agent SETS
                                   # before entering, clamped to that market's
                                   # maximum. Sizing uses the same number, so
                                   # the margin actually posted is the figure
                                   # above rather than an assumption about
                                   # whatever the account happened to be set
                                   # to. A symbol left at 3x would otherwise
                                   # post 3x the intended collateral.
EXEC_NOTIONAL_USD = 30.0           # size of each position in "notional" mode
EXEC_RISK_USD = 2.0                # dollar loss at the stop in "risk" mode
EXEC_MAX_NOTIONAL_USD = 12000      # cap on position value. Raised from 8000
                                   # on 4 Aug so it stops binding: in margin
                                   # mode the position is EXEC_MARGIN_USD x
                                   # the market max, and $250 x 40 = $10,000
                                   # was being clamped to $8,000, quietly
                                   # making the real collateral $200. KEEP
                                   # THIS ABOVE EXEC_MARGIN_USD x 40 or the
                                   # cap trims the position and the risk
                                   # with it, without saying so
EXEC_MAX_POSITIONS = 0             # concurrent live positions. 0 = NO CAP:
                                   # the only remaining limits are the halt
                                   # file, EXEC_MAX_NOTIONAL_USD per trade,
                                   # and whatever margin the account has
EXEC_DAILY_LOSS_LIMIT_USD = 40.0   # INERT: needs realised USD from the ledger,
                                   # which is not tracked yet

# --- plumbing -------------------------------------------------------------
STATE_FILE = Path(__file__).parent / "btc_agent_state.json"
TRADES_LOG = Path(__file__).parent / "trades.log"
ORDERS_LOG = Path(__file__).parent / "orders.log"
TIMEZONE = "America/Chicago"
LOCAL_TZ = ZoneInfo(TIMEZONE)

MS = {"5m": 300_000, "10m": 600_000, "15m": 900_000, "30m": 1_800_000,
      "1h": 3_600_000, "4h": 14_400_000}
_TF_ALIASES = {"5min": "5m", "10min": "10m", "15min": "15m", "30min": "30m",
               "60m": "1h", "60min": "1h", "1hr": "1h"}
TF = _TF_ALIASES.get(TF.strip().lower(), TF.strip().lower())
SCAN_EVERY = _TF_ALIASES.get(SCAN_EVERY.strip().lower(),
                             SCAN_EVERY.strip().lower())
for _n, _v in (("TF", TF), ("SCAN_EVERY", SCAN_EVERY)):
    if _v not in MS:
        raise SystemExit(f"CONFIG ERROR: {_n}={_v!r} is not a known "
                         f"timeframe - use one of {sorted(MS)}")

# the fetcher ignores the caller's count and uses this map - a value that is
# too small silently starves whatever depends on that interval
LOOKBACK = {"5m": 300, "10m": 300, "15m": 400, "30m": 400, "1h": 500,
            "4h": 300}

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
    # two more decimals than the original bands (0/2/6), so AVAX prints
    # 6.5676 rather than 6.57 - the doji levels are often separated by less
    # than a cent and the rounded form made distinct prices look identical
    return f"{p:,.2f}" if p >= 10000 else f"{p:,.4f}" if p >= 1 else f"{p:,.8f}"


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
               "User-Agent": "Mozilla/5.0 (ha-agent/1.0)"}
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


def ha_label():
    """What the alerts should call the series. At 1,1 there is no smoothing
    at all, and calling it "smoothed HA" misdescribes the engine."""
    return ("Heikin Ashi" if HA_SMOOTH_IN <= 1 and HA_SMOOTH_OUT <= 1
            else f"smoothed HA {HA_SMOOTH_IN},{HA_SMOOTH_OUT}")


def btc_trend():
    """BTC's smoothed-HA colour, run length and % move over that run.

    Read with BTC_TREND_SMOOTH (5,5), NOT the signal smoothing (10,10).
    Lighter smoothing turns sooner, so this reports where BTC is NOW rather
    than confirming it several candles late - which is what you want from
    context that is only ever displayed, never traded on.

    Cached for BTC_TREND_TTL_S so a scan of 35 symbols costs one fetch.
    Returns (up, candles_in_run, pct_move) or None if it cannot be read."""
    now = time.time()
    if _BTC_CACHE["t"] and now - _BTC_CACHE["t"] < BTC_TREND_TTL_S:
        return _BTC_CACHE["v"]
    try:
        c = fetch_hyperliquid("BTC", TF, LOOKBACK.get(TF, 400))
        ha = smoothed_ha(c, *BTC_TREND_SMOOTH)
        if len(ha) < 3:
            return None
        up = ha_green(ha[-1])
        n = 1
        while n < len(ha) and ha_green(ha[-1 - n]) == up:
            n += 1
        move = (c[-1]["c"] - c[-n]["o"]) / c[-n]["o"] * 100 if n <= len(c) \
            else 0.0
        # IS BITCOIN STALLING? A SAME-COLOUR doji - a small trend-coloured
        # body at the end of the run - is the earliest read on exhaustion,
        # printing BEFORE the colour turns. The entry detector waits for the
        # flip; the gate does not, because BTC leads the alts and by the
        # time BTC has flipped the alt move is already under way.
        # want_long=True means "a red run ending", so it detects a stalling
        # DOWNtrend; that is the one to look for when BTC is currently red.
        stalling = bool(ha_doji(ha, len(ha) - 1, not up, colour="same"))
        val = (up, n, move, stalling)
    except Exception as e:
        log(f"btc_trend() failed: {type(e).__name__}: {e}")
        val = None
    _BTC_CACHE.update({"t": now, "v": val})
    return val


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
    """Strip only the venue prefix Hyperliquid includes ('xyz:GOLD').

    It used to also .upper().lstrip("K"), meant to undo kPEPE-style
    multipliers - but "kPEPE" IS the perp's real name on Hyperliquid, so
    stripping the k produced "PEPE", which is not in the universe. Every
    k-prefixed market and every ticker simply starting with K (KAITO ->
    AITO) silently failed its universe lookup and never executed. Case is
    preserved too: the exchange matches names exactly.
    """
    return name.split(":")[-1]


def executable(symbol):
    """Can the agent place orders on this market itself?

    Builder-venue markets ("xyz:GOLD") are alert-only UNLESS their dex is
    listed in EXEC_BUILDER_DEXES, in which case the SDK client was built
    with perp_dexs=[...] and can reach them. Nothing that cannot execute may
    consume the live position budget.
    """
    if ":" not in symbol:
        return True
    if symbol.split(":")[0] not in EXEC_BUILDER_DEXES:
        return False
    # the client may have fallen back to main-dex-only at build time
    return _EXEC["ex"] is None or bool(_EXEC.get("dexes"))


def is_commodity(name):
    """Exact match only - see the note on COMMODITY_TICKERS."""
    return base_name(name).upper() in COMMODITY_TICKERS


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
    excluded = {base_name(x).upper() for x in EXCLUDE}
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
            if base_name(name).upper() in excluded:
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
    if ONLY_SYMBOLS:
        want = {base_name(x).upper() for x in ONLY_SYMBOLS}
        found = [a for a in found
                 if base_name(a["symbol"]).upper() in want]
        missing = want - {base_name(a["symbol"]).upper() for a in found}
        if missing:
            log(f"ONLY_SYMBOLS names markets that are not tradable: "
                f"{sorted(missing)}")
        log(f"ONLY_SYMBOLS active - universe restricted to "
            f"{[a['symbol'] for a in found]}")
        return found
    found.sort(key=lambda a: a["vol"], reverse=True)
    return found[:MAX_ASSETS]


def _not_excluded(a):
    return (base_name(a["symbol"]).upper()
            not in {base_name(x).upper() for x in EXCLUDE})


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
        hand = len(assets) - auto
        n_com = sum(1 for a in assets if a.get("cls") == "commodity")
        n_stk = sum(1 for a in assets if a.get("cls") == "stock")
        # say what is ACTUALLY tradable rather than assuming every builder
        # market is alert-only - EXEC_BUILDER_DEXES can now make them live
        log(f"Discovered {len(assets)} markets: {auto} auto-traded"
            + (f", {hand} alert-only (placed by hand)" if hand else "")
            + f" - {n_com} commodities, {n_stk} equities"
            + (f", builder dexes live: {', '.join(EXEC_BUILDER_DEXES)}"
               if EXEC_BUILDER_DEXES else ""))
        return assets
    log("Discovery returned nothing - falling back to the manual ASSETS list.")
    return ASSETS


# --------------------------- smoothed heikin ashi --------------------------
def ema(vals, n):
    """Plain EMA, seeded on the first value so the series has no None gap."""
    k = 2.0 / (n + 1)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def smoothed_ha(candles, n_in=None, n_out=None):
    """EMA the OHLC, build Heikin Ashi on that, then EMA the result. The
    output is a derived band, NOT tradeable prices - its highs and lows are
    averages and may never have printed."""
    n_in, n_out = n_in or HA_SMOOTH_IN, n_out or HA_SMOOTH_OUT
    if not candles:
        return []
    o = ema([c["o"] for c in candles], n_in)
    h = ema([c["h"] for c in candles], n_in)
    lo = ema([c["l"] for c in candles], n_in)
    cl = ema([c["c"] for c in candles], n_in)
    ha, p_o, p_c = [], None, None
    for i in range(len(candles)):
        close = (o[i] + h[i] + lo[i] + cl[i]) / 4
        open_ = close if p_o is None else (p_o + p_c) / 2
        ha.append({"o": open_, "c": close})
        p_o, p_c = open_, close
    so = ema([x["o"] for x in ha], n_out)
    sc = ema([x["c"] for x in ha], n_out)
    sh = ema(h, n_out)
    sl = ema(lo, n_out)
    return [{"t": candles[i]["t"], "o": so[i], "c": sc[i],
             "h": max(sh[i], so[i], sc[i]), "l": min(sl[i], so[i], sc[i])}
            for i in range(len(candles))]


def ha_green(x):
    return x["c"] > x["o"]


def ha_body(x):
    return abs(x["c"] - x["o"])


def ha_wick(c, upper):
    """Wick length on one side of an HA candle."""
    body_hi = max(c["o"], c["c"])
    body_lo = min(c["o"], c["c"])
    return (c["h"] - body_hi) if upper else (body_lo - c["l"])


def no_wick(c, want_long):
    """Is this the CONVICTION candle - no wick on the side the trade runs
    against? For a LONG that is the LOWER wick, for a SHORT the UPPER one.

    Tolerance is a fraction of the candle's OWN BODY, not of price, so it
    scales with the candle instead of with the market. A zero-body candle
    can never qualify: it is a doji, the opposite of conviction."""
    body = ha_body(c)
    if body <= 0:
        return False
    wick = ha_wick(c, upper=not want_long)
    return wick <= (HA_NOWICK_TOL_PCT / 100.0) * body


def resample(candles, factor):
    """Group base candles into larger ones. Only WHOLE groups are returned,
    so the still-forming higher-timeframe bar is left out and the EMA never
    reads a partial candle."""
    if factor <= 1:
        return list(candles)
    out = []
    for k in range(0, len(candles) - factor + 1, factor):
        g = candles[k:k + factor]
        out.append({"t": g[0]["t"], "o": g[0]["o"], "c": g[-1]["c"],
                    "h": max(x["h"] for x in g),
                    "l": min(x["l"] for x in g),
                    "v": sum(x.get("v", 0) for x in g)})
    return out


def ema_side(candles, i, want_long):
    """Is price on the right side of the trend EMA?

    Returns (ok, ema_value, close). The price tested is the LAST CLOSED base
    candle, i-1 at signal time - the no-wick bar. Testing the entry bar would
    read one that has barely opened. The EMA itself is measured on
    EMA_FILTER_TF, resampled from the same candles.

    Fails OPEN: too few bars for a meaningful EMA allows the trade rather
    than blocking every symbol on a short history."""
    if not EMA_FILTER_LEN or i < 1:
        return True, None, None
    k = max(0, i - 1)
    base = candles[:k + 1]
    factor = max(1, MS.get(EMA_FILTER_TF, MS[TF]) // MS[TF])
    higher = resample(base, factor)
    if len(higher) < EMA_FILTER_LEN:
        return True, None, None
    e, px = ema([c["c"] for c in higher], EMA_FILTER_LEN)[-1], base[-1]["c"]
    side_ok = (px > e) if want_long else (px < e)
    if not side_ok or not EMA_RETEST_PCT:
        return side_ok, e, px
    # RETEST: price has to be back AT the EMA, not merely on its side. A
    # short 6% under the average is chasing a move that already happened;
    # a short 0.3% under it is selling the failed pullback.
    near = abs(px - e) / e * 100 if e else 999
    return near <= EMA_RETEST_PCT, e, px


def ha_nowick_signal(ha, i, want_long):
    """The 4 Aug engine. Entry is at the OPEN of candle i, so everything
    below happened at i-1 or earlier.

    Reading the sequence backwards from the entry bar:
      i-1   the NO-WICK candle - new colour, no wick on the trade's side
      i-2   the COLOUR FLIP - first candle of the new colour
      ...   before that, a run of the OLD colour whose bodies were SHRINKING
            into the turn ("as the candles start getting smaller")

    want_long=True means the old run was RED and it flipped GREEN.
    Returns (flip_index, run_start) or None."""
    nw, fl = i - 1, i - 2
    if fl < 1:
        return None
    # the no-wick bar and the flip bar must both be the NEW colour
    if ha_green(ha[nw]) != want_long or ha_green(ha[fl]) != want_long:
        return None
    if not no_wick(ha[nw], want_long):
        return None
    # ...and the flip bar must be the FIRST of that colour
    if ha_green(ha[fl - 1]) == want_long:
        return None
    # the run it ended, walked back while the colour holds
    r = fl - 1
    while r >= 0 and ha_green(ha[r]) != want_long:
        r -= 1
    r += 1
    run = ha[r:fl]
    if len(run) < max(1, HA_MIN_RUN):
        return None
    # the run has to have GONE somewhere, not merely lasted
    if HA_MIN_RUN_PCT:
        span = abs(run[0]["o"] - run[-1]["c"])
        px = abs(ha[fl]["c"]) or 1.0
        if span / px * 100 < HA_MIN_RUN_PCT:
            return None
    bodies = [ha_body(x) for x in run]
    biggest = max(bodies)
    scale = abs(ha[fl]["c"]) or 1.0
    if HA_MIN_BODY_PCT and biggest < HA_MIN_BODY_PCT / 100.0 * scale:
        return None
    # "as the candles start getting smaller" - the last HA_FADE_BARS bodies
    # of the run shrink monotonically into the flip. This is what makes it a
    # move running out of steam rather than one cut off mid-stride.
    if HA_FADE_BARS and len(bodies) >= HA_FADE_BARS:
        tail = bodies[-HA_FADE_BARS:]
        if not all(tail[k] < tail[k - 1] for k in range(1, len(tail))):
            return None
    return fl, r


def confirmed(ha, candles, d, i, want_long, final=True):
    """Did the doji at d actually take, by candle i?

    Every HA candle after the doji must run the trade's way with a body
    LARGER than the one before it - a stall that resumes the old trend, or
    one that peters out, never reaches here. On the final bar the REAL
    candle must close that way as well, which is the part the HA cannot
    fake: HA closes are averages of four prices and drift green while the
    market prints red."""
    prev = ha_body(ha[d])
    for k in range(d + 1, i + 1):
        if ha_green(ha[k]) != want_long:
            return False, f"bar {k - d} turned back"
        b = ha_body(ha[k])
        if b <= prev:
            return False, f"bar {k - d} did not expand"
        prev = b
    if not final:
        # mid-flight: the expansion has held so far, but the real-close test
        # belongs to the LAST bar only. Applying it early would report a
        # perfectly healthy sequence as failed.
        return True, ""
    c = candles[i]
    real_up = c["c"] > c["o"]
    if real_up != want_long:
        return False, "real candle closed the wrong way"
    return True, ""


def gate_status(ha, candles, i, sym=None):
    """Where a symbol sits in the FLIP + NO-WICK sequence, for the panel.

    Returns None for anything that has not reached a flip - those are just
    trends running, and listing every one of them buries the handful that
    matter. ha_nowick_signal returns None with no reason, so this mirrors
    its checks and names the first that fails. REPORT ONLY: fire_entry
    still asks ha_nowick_signal, and if the two ever disagree that one is
    right.

    The sequence is strict about position - the no-wick bar must be the
    candle IMMEDIATELY after the flip - so a row ages out in two bars."""
    if i < 3:
        return None
    # the flip is the first candle of the colour currently running
    f = i
    while f > 0 and ha_green(ha[f - 1]) == ha_green(ha[i]):
        f -= 1
    age = i - f
    if f < 1 or age > 2:
        return None                      # no flip, or the chance has passed
    want_long = ha_green(ha[f])
    d = {"dir": "LONG" if want_long else "SHORT",
         "trend": "down" if want_long else "up",
         "age": age, "need": 1}

    r = f - 1
    while r >= 0 and ha_green(ha[r]) != want_long:
        r -= 1
    r += 1
    run = ha[r:f]
    d["run"] = len(run)
    if not run:
        return None
    if len(run) < max(1, HA_MIN_RUN):
        # ha_nowick_signal refuses this outright, so listing it as "flipped,
        # needs a no-wick bar next" promises a trade that can never fire
        d.update(stage="run too short",
                 detail=f"{len(run)}/{HA_MIN_RUN} candles")
        return d
    if HA_MIN_RUN_PCT:
        span = abs(run[0]["o"] - run[-1]["c"])
        pxr = abs(ha[f]["c"]) or 1.0
        if span / pxr * 100 < HA_MIN_RUN_PCT:
            d.update(stage="run went nowhere",
                     detail=f"{span / pxr * 100:.2f}% move, "
                            f"needs {HA_MIN_RUN_PCT}%")
            return d
    bodies = [ha_body(x) for x in run]
    biggest = max(bodies)
    scale = abs(ha[f]["c"]) or 1.0
    if HA_MIN_BODY_PCT and biggest < HA_MIN_BODY_PCT / 100.0 * scale:
        d.update(stage="run too flat",
                 detail=f"{biggest / scale * 100:.3f}% vs {HA_MIN_BODY_PCT}%")
        return d
    if HA_FADE_BARS and len(bodies) >= HA_FADE_BARS:
        tail = bodies[-HA_FADE_BARS:]
        if not all(tail[k] < tail[k - 1] for k in range(1, len(tail))):
            d.update(stage="run was expanding",
                     detail="bodies grew into the flip")
            return d
    if age == 0:
        d.update(stage="flipped", detail="needs a no-wick bar next")
        return d
    if age == 1:
        if no_wick(ha[i], want_long):
            ok_ema, ev, epx = ema_side(candles, i + 1, want_long)
            if not ok_ema:
                right_side = (epx > ev) == want_long if (ev and epx) else False
                gap = (abs(epx - ev) / ev * 100) if ev else 0
                if right_side:
                    d.update(stage="too far from EMA",
                             detail=f"{gap:.2f}% away, needs "
                                    f"<= {EMA_RETEST_PCT}%")
                else:
                    d.update(stage="wrong side of EMA",
                             detail=f"close {fmt_px(epx)} vs "
                                    f"{EMA_FILTER_LEN} EMA {fmt_px(ev)}")
            else:
                d.update(stage="ready",
                         detail="no-wick bar - entry at next open")
        else:
            side = "upper" if not want_long else "lower"
            w, b = ha_wick(ha[i], upper=not want_long), ha_body(ha[i])
            pct = (w / b * 100) if b else 999
            d.update(stage="wick too long",
                     detail=f"{side} wick {pct:.0f}% of body "
                            f"(max {HA_NOWICK_TOL_PCT:.0f}%)")
        return d
    d.update(stage="missed", detail="no-wick bar did not follow the flip")
    return d


def ha_doji(ha, i, want_long, colour=None):
    """Is HA candle i a DOJI that turns a real trend?

    A doji is a body small relative to the trend that produced it - the
    smoothed HA stalling. That stall IS the signal: the trade is taken on
    this candle, in the direction OPPOSITE the trend that led in. Price does
    not have to come back and retest anything.

    Returns (doji_index, run_start) or None.
    """
    # want_long means the trend into the doji was RED, so the doji turns us
    # long. HA_DOJI_COLOUR decides which colour that doji has to be: "same"
    # takes the trend-coloured stall (a red doji ending a downtrend), "flip"
    # waits for the colour to actually turn first.
    if HA_MIN_FLIP_BODY_PCT:
        # a colour change of essentially zero is not a turn
        if abs(ha[i]["c"] - ha[i]["o"]) / ha[i]["o"] * 100 < HA_MIN_FLIP_BODY_PCT:
            return None
    mode = colour or HA_DOJI_COLOUR
    if mode == "flip" and ha_green(ha[i]) != want_long:
        return None
    if mode == "same" and ha_green(ha[i]) == want_long:
        return None
    r = i - 1
    while r >= 0 and ha_green(ha[r]) != want_long:
        r -= 1
    r += 1
    run = ha[r:i]                            # the trend, excluding the doji
    # a run has to be long enough to BE a trend. Without this a single
    # counter-coloured candle inside a move counts as one, and the candle
    # after it reads as a flip - LIT, 3 Aug, "1 candle down" turning LONG
    # in the middle of an uptrend.
    if len(run) < max(1, HA_MIN_RUN):
        return None
    bodies = [ha_body(x) for x in run]
    biggest = max(bodies)
    scale = abs(ha[i]["c"]) or 1.0

    # the trend has to be VISIBLE. A flat smoothed series is nothing BUT
    # dojis, so without this every quiet symbol would trade continuously.
    if HA_MIN_BODY_PCT and biggest < HA_MIN_BODY_PCT / 100.0 * scale:
        return None
    # the doji itself: small against what came before it
    if ha_body(ha[i]) > HA_DOJI_FRACTION * biggest:
        return None
    return i, r


# --------------------------- telegram --------------------------------------
def send_telegram(text):
    resp = http_json(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True})
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram send failed: {resp.get('description')}")


# --------------------------- messages --------------------------------------
def entry_message(asset, direction, plan, zhi, zlo, source, t, trigger):
    e = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
    return "\n".join([
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 {ha_label()} \u00b7 "
        f"{esc(fmt_ts(t))}</i>",
        "",
        "\U0001F4CA <b>Setup</b>: "
        + (f"HA zone ${fmt_px(zlo)} - ${fmt_px(zhi)}; " if zhi else "")
        + f"{esc(trigger)}",
        "",
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(plan['entry'])}</code>",
        f"Stop:  <code>${fmt_px(plan['stop'])}</code>  (HA zone low)",
        f"TP:    <code>${fmt_px(plan['tp'])}</code>  "
        f"({HA_RR}x the stop \u00b7 {HA_PARTIAL:.0%} booked there)",
        f"<i>data: {esc(source)}</i>",
    ])


def lifecycle_message(asset, kind, trade, exit_px, event_t, note):
    emoji, title, sub = {
        "TP_HALF": ("\U0001F3AF", "TARGET HIT",
                    f"{HA_RR}R reached \u00b7 {HA_PARTIAL:.0%} booked, "
                    "stop moved to entry"),
        "RUNNER": ("\u2705", "RUNNER CLOSED",
                   "smoothed HA flipped against the trade"),
        "BE": ("\u27a1\ufe0f", "STOPPED AT ENTRY",
               "the runner came back to breakeven"),
        "STOP": ("\u274c", "STOPPED OUT", "stop level hit"),
        "MANUAL": ("\u270b", "CLOSED BY HAND",
                   "closed from the dashboard at market"),
        "TP": ("\u2705", "TAKE PROFIT HIT", "target reached"),
        "GONE": ("\u26a0\ufe0f", "POSITION GONE",
                 "tracked here but flat on the exchange"),
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
    return {"phase": "SCAN", "last_candle_t": 0, "setup": None,
            "traded": None, "trade": None}


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
def ensure_flat(asset, trade, kind):
    """The ledger just booked a close - make the exchange agree.

    Two ways they drift apart. A STOP is detected from CANDLE data, which is
    last-trade prints, while the exchange triggers its resting stop on MARK
    price: a wick that moves the last trade but not the mark closes the
    trade here and leaves the position open there. A RUNNER is worse - there
    is no resting order for it at all, because the take-profit covered only
    the partial, so nothing on the exchange ever closes it.

    So: read the real position, and if it is not flat, close it at market.
    """
    if not EXEC_LIVE or not executable(asset["symbol"]) or not trade.get("size"):
        return
    ex = exec_client()
    if not ex:
        return
    sym = exec_symbol(asset["symbol"])
    try:
        state = _EXEC["info"].user_state(_EXEC["addr"]) or {}
        szi = 0.0
        for p in state.get("assetPositions", []):
            pos = p.get("position") or {}
            if pos.get("coin") == sym:
                szi = float(pos.get("szi") or 0)
                break
    except Exception as e:
        log(f"{sym}: could not read the exchange position after {kind} "
            f"({type(e).__name__}: {e}) - check it by hand")
        return
    if abs(szi) < 1e-12:
        return                      # the exchange agrees, nothing to do
    log(f"{sym}: ledger booked {kind} but the exchange still shows {szi} "
        "open - closing at market")
    try:
        ex.market_close(sym)
        CLOSED_THIS_SCAN.add(sym)   # sym is already the exchange name here
        log(f"{sym}: reconciling market close sent")
    except Exception as e:
        log(f"{sym}: reconciling close FAILED ({type(e).__name__}: {e})")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} booked {kind} but is "
                          f"STILL OPEN on Hyperliquid ({szi}) and the "
                          "market close failed - close it by hand")
        except Exception:
            pass
        return
    for oid in (trade.get("stop_oid"), trade.get("tp_oid")):
        if oid:
            try:
                ex.cancel(sym, oid)
            except Exception:
                pass


def _close_trade(asset, trade, px, kind, event_t, note="", frac=None):
    """Alert, log and book a close of `frac` of the position."""
    sym = asset["symbol"]
    frac = trade.get("left", 1.0) if frac is None else frac
    if already_closed(sym, trade, px, kind):
        log(f"{sym}: duplicate {kind} close suppressed")
        return None, True
    if ALERT_LIFECYCLE:
        try:
            send_telegram(lifecycle_message(asset, kind, trade, px,
                                            event_t, note))
        except Exception as e:
            log(f"{sym}: {kind} alert failed: {type(e).__name__}: {e}")
    log(f"{sym}: {kind} at ${fmt_px(px)}{' (intrabar)' if note else ''}")
    record_close(sym, trade, px, kind, event_t, frac=frac)
    CLOSED_THIS_SCAN.add(exec_symbol(sym))
    plan_manage_orders(asset, kind, px)
    # EVERY _close_trade call ends the position - the partial books through
    # record_close directly, never here. The old `frac >= 0.999` guard was
    # therefore wrong: a RUNNER closing its remaining half passes frac 0.5,
    # so reconciliation was skipped on exactly the case that has NO resting
    # order and most needs it. Seen live on HYPE, closed by hand.
    ensure_flat(asset, trade, kind)
    RUN_ALERTS.append(f"{sym} {kind} ({pnl_pct(trade, px) * frac:+.2f}%)")
    return None, True


def _book_partial(asset, trade, px, event_t):
    """HA_PARTIAL comes off at the target, the stop moves to entry, and the
    remainder runs until the HA flips. Booked as its own ledger row with
    frac set, so partial P&L stays partial.

    AT HA_PARTIAL = 1.0 THERE IS NO REMAINDER: the target takes the whole
    position and the trade is DONE. Falling through to the partial path
    would leave a trade marked half-booked with 0% running, which the
    runner logic would then watch forever."""
    sym = asset["symbol"]
    if HA_PARTIAL >= 1.0:
        log(f"{sym}: target hit at ${fmt_px(px)} - full position booked "
            f"at {HA_RR:.1f}R, no runner")
        return _close_trade(asset, trade, px, "TP", event_t)
    # returns None on the PARTIAL path so the caller knows the trade lives on
    record_close(sym, trade, px, "TP_HALF", event_t, frac=HA_PARTIAL)
    trade["half"] = True
    trade["left"] = round(1.0 - HA_PARTIAL, 6)
    trade["stop"] = trade["entry"]
    if ALERT_LIFECYCLE:
        try:
            send_telegram(lifecycle_message(asset, "TP_HALF", trade, px,
                                            event_t, ""))
        except Exception as e:
            log(f"{sym}: TP_HALF alert failed: {type(e).__name__}")
    log(f"{sym}: target hit at ${fmt_px(px)} - {HA_PARTIAL:.0%} booked, "
        f"stop moved to entry ${fmt_px(trade['entry'])}, "
        f"{trade['left']:.0%} running")
    RUN_ALERTS.append(f"{sym} target hit, {HA_PARTIAL:.0%} booked")
    move_stop_live(asset, trade)


def reverse_alert(asset, trade, candles, c, was_long):
    """The flip that closes a runner is a setup the OTHER way. Send the
    levels so the position can be flipped in one action.

    ALERT ONLY. A reverse has cleared none of the entry chain - no doji, no
    confirmation bars, no run-length floor - so the agent must not place it
    on its own. The levels use the same rules an entry would: the stop is
    the STOP_LOOKBACK real-candle extreme ending at this candle, and the
    target is HA_RR from there."""
    sym = asset["symbol"]
    try:
        idx = next(k for k in range(len(candles) - 1, -1, -1)
                   if candles[k]["t"] == c["t"])
        lo = max(0, idx - stop_bars() + 1)
        window = candles[lo:idx + 1]
        if not window:
            return
        now_long = not was_long
        stop = (min(x["l"] for x in window) if now_long
                else max(x["h"] for x in window))
        entry = c["c"]
        risk = (entry - stop) if now_long else (stop - entry)
        if risk <= 0:
            log(f"{sym}: reverse to {'LONG' if now_long else 'SHORT'} has no "
                "risk distance - no alert")
            return
        if MIN_STOP_PCT and risk / entry * 100 < MIN_STOP_PCT:
            log(f"{sym}: reverse stop {risk / entry * 100:.3f}% under the "
                f"{MIN_STOP_PCT}% floor - no alert")
            return
        tp = entry + HA_RR * risk if now_long else entry - HA_RR * risk
        side = "LONG" if now_long else "SHORT"
        send_telegram(
            f"\U0001f504 <b>REVERSE \u00b7 {esc(sym)}</b>\n"
            f"<i>the smoothed HA flipped against your "
            f"{'long' if was_long else 'short'}</i>\n\n"
            f"Flip to <b>{side}</b>\n"
            f"Entry: <code>${fmt_px(entry)}</code>\n"
            f"Stop:  <code>${fmt_px(stop)}</code> "
            f"({len(window)}-candle {'low' if now_long else 'high'}, "
            f"{risk / entry * 100:.2f}%)\n"
            f"TP:    <code>${fmt_px(tp)}</code> ({HA_RR:.1f}x)\n\n"
            f"\u26a0\ufe0f Not placed - a reverse skips the doji and the "
            f"confirmation bars. Yours to take or ignore.")
        log(f"{sym}: REVERSE alert - flip to {side} @ ${fmt_px(entry)}, "
            f"stop ${fmt_px(stop)}")
    except Exception as e:
        log(f"{sym}: reverse_alert failed: {type(e).__name__}: {e}")


def process_open_trade(asset, trade, candles, ha, last_closed_t):
    """Stop / target watch. Before the partial the stop is the HA zone low;
    after it the stop is entry and the exit trigger is an HA flip against
    the trade. The stop is always checked first within a candle."""
    long = trade["verdict"] == "LONG"
    changed = False
    by_t = {h["t"]: h for h in ha}
    for c in candles:
        if c["t"] <= trade["checked_t"] or c["t"] > last_closed_t:
            continue
        changed = True
        trade["checked_t"] = c["t"]
        event_t = c["t"] + MS[TF]
        if (c["l"] <= trade["stop"]) if long else (c["h"] >= trade["stop"]):
            kind = "BE" if trade.get("half") else "STOP"
            return _close_trade(asset, trade, trade["stop"], kind, event_t)
        if not trade.get("half"):
            if (c["h"] >= trade["tp"]) if long else (c["l"] <= trade["tp"]):
                # AT HA_PARTIAL = 1.0 _book_partial CLOSES the trade and
                # returns _close_trade's (None, True). Discarding that left
                # the ledger with a TP row while state still tracked the
                # position as open - PUMP, 5 Aug, in both panels at once.
                done = _book_partial(asset, trade, trade["tp"], event_t)
                if done is not None:
                    return done
            continue
        h = by_t.get(c["t"])
        if h and ha_green(h) != long:
            if REVERSE_ALERTS:
                reverse_alert(asset, trade, candles, c, long)
            return _close_trade(asset, trade, c["c"], "RUNNER", event_t,
                                "smoothed HA flipped against the trade")

    # ---- intrabar on the LIVE candle -------------------------------------
    # a fast move can blow through the stop mid-candle. checked_t is NOT
    # advanced here, and this block computes its own timestamp: the loop
    # above may not have run this pulse, so its variables must never be
    # referenced from here.
    live = candles[-1]
    if live["t"] > last_closed_t:
        t_now = now_ms()
        if (live["l"] <= trade["stop"]) if long else (live["h"] >= trade["stop"]):
            kind = "BE" if trade.get("half") else "STOP"
            return _close_trade(asset, trade, trade["stop"], kind, t_now,
                                "Intrabar - stop traded before the close.")
        if not trade.get("half"):
            if (live["h"] >= trade["tp"]) if long else (live["l"] <= trade["tp"]):
                done = _book_partial(asset, trade, trade["tp"], t_now)
                if done is not None:
                    return done
    return trade, changed


# --------------------------- execution -------------------------------------
_EXEC = {"ex": None, "info": None, "addr": None, "meta": None, "err": None,
         "dexes": []}


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
            dexes = [d for d in EXEC_BUILDER_DEXES if d]
            ex = None
            if dexes:
                # The MAIN dex must be listed too. Passing only the builder
                # names REPLACES the SDK's asset map instead of extending
                # it, and every main-dex coin then raises KeyError - seen
                # live as "could NOT set leverage to 3x (KeyError:
                # 'CASHCAT')" on a plain main-dex market.
                for attempt in ([""] + dexes, [None] + dexes):
                    try:
                        ex = Exchange(wallet, base, account_address=addr,
                                      perp_dexs=attempt)
                        log(f"execution client built with perp_dexs="
                            f"{attempt}")
                        break
                    except Exception as e:
                        log(f"perp_dexs={attempt} rejected "
                            f"({type(e).__name__}: {e})")
            if ex is None:
                # Builder dexes are a bonus; main-dex trading is the job.
                # Never let the extra feature take execution down with it.
                ex = Exchange(wallet, base, account_address=addr)
                if dexes:
                    log("falling back to a MAIN-DEX-ONLY client - builder "
                        "markets will be refused, main-dex trading is fine")
                    dexes = []
            _EXEC.update(ex=ex, info=Info(base, skip_ws=True), addr=addr)
            _EXEC["dexes"] = dexes
            # the universe check must know every market the client can
            # reach, or builder symbols are refused as "not in the perp
            # universe" even though the client could trade them
            universe = list((_EXEC["info"].meta() or {}).get("universe", []))
            for d in dexes:
                try:
                    extra = _EXEC["info"].meta(dex=d) or {}
                    for u in extra.get("universe", []):
                        # the dex meta may return names bare ("GOLD") or
                        # ALREADY prefixed ("xyz:GOLD") - prefixing blindly
                        # gives "xyz:xyz:GOLD", which matches nothing
                        nm = u["name"]
                        universe.append({**u,
                                         "name": nm if ":" in nm
                                         else f"{d}:{nm}"})
                    log(f"builder dex '{d}': "
                        f"{len(extra.get('universe', []))} markets tradable")
                except Exception as e:
                    log(f"builder dex '{d}' meta failed ({type(e).__name__}: "
                        f"{e}) - its markets stay alert-only")
            _EXEC["meta"] = {"universe": universe}
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
    if EXEC_MAX_POSITIONS and open_count >= EXEC_MAX_POSITIONS:
        return f"{open_count} positions already open (max {EXEC_MAX_POSITIONS})"
    if day_pnl_usd <= -abs(EXEC_DAILY_LOSS_LIMIT_USD):
        return f"daily loss limit hit ({day_pnl_usd:+.2f})"
    return None


def exec_symbol(symbol):
    """The name the EXCHANGE expects. Builder markets keep their full
    "xyz:GOLD" form now that the client is built with perp_dexs; only a
    main-dex symbol is passed bare. place_entry_live and ensure_flat had
    this inline while close_position_live and move_stop_live still used
    base_name - so a manual close or a stop move on a commodity would have
    been sent as "GOLD", which the client does not know."""
    return symbol if ":" in symbol else base_name(symbol)


def only_isolated(symbol):
    """Does the VENUE forbid cross margin on this market?

    Hyperliquid marks such assets in the perp meta - CASHCAT carries
    onlyIsolated: True and marginMode: "strictIsolated". Reading the flag
    beats discovering it by sending a cross request and being refused: one
    less round trip, and the log stops implying something went wrong when
    nothing did."""
    sym = exec_symbol(symbol)
    for a in (_EXEC.get("meta") or {}).get("universe", []):
        if a.get("name") == sym:
            return bool(a.get("onlyIsolated")) or \
                a.get("marginMode") == "strictIsolated"
    return False


def eff_leverage(asset):
    """The leverage the agent will actually use: the configured value, never
    above what the market allows."""
    cap = asset.get("lev") or EXEC_LEVERAGE
    try:
        return max(1, min(int(EXEC_LEVERAGE), int(cap)))
    except (TypeError, ValueError):
        return max(1, int(EXEC_LEVERAGE))


def free_collateral():
    """FREE INITIAL MARGIN: account value minus margin already committed.
    None if the read fails.

    NOT "withdrawable". That field answers a different question - how much
    cash could leave the account right now - and it also nets off margin
    reserved by resting orders. Measured live 3 Aug with 8 cross positions
    and 15 resting stops/TPs: accountValue $372.53, totalMarginUsed
    $225.02, so $147.51 of headroom, while withdrawable read 0.0. The guard
    was refusing every entry on an account with room for four more.

    Returning None rather than 0.0 on failure matters: the caller treats
    None as "unknown, proceed" so a bad read cannot silently block every
    entry. A real zero still blocks, and the venue is the final backstop -
    if Hyperliquid disagrees it rejects the order and the NOT PLACED path
    reports it honestly."""
    try:
        st = _EXEC["info"].user_state(_EXEC["addr"]) or {}
        ms = st.get("crossMarginSummary") or st.get("marginSummary") or {}
        return max(0.0, float(ms["accountValue"])
                   - float(ms["totalMarginUsed"]))
    except Exception as e:
        log(f"free_collateral() failed: {type(e).__name__}: {e}")
        return None


def plan_entry_orders(asset, trade, live_px=None):
    """Size the trade by fixed dollar risk and describe the orders. Logged
    to orders.log. Returns the plan, or None.

    Sizing uses `live_px` - the price of the still-forming candle - rather
    than trade["entry"], which is the DOJI candle's close and is already
    stale by the time the order goes out. Size is fixed at send time but the
    fill lands at whatever the market is doing now, so sizing off the stale
    price makes the realised risk drift: measured on his own fills it ranged
    from $0.65 to $2.19 against a $2.00 target.
    """
    sym = asset["symbol"]
    entry, stop, tp = trade["entry"], trade["stop"], trade["tp"]
    long_ = trade["verdict"] == "LONG"
    size_px = entry
    if live_px:
        live_gap = (live_px - stop) if long_ else (stop - live_px)
        if live_gap > 0:
            size_px = live_px          # only if it still leaves real risk
    per_unit = abs(size_px - stop)
    if per_unit <= 0:
        return None
    if EXEC_SIZING == "margin":
        # collateral x leverage = position value. lev is clamped to the
        # market's maximum, and place_entry_live SETS that same leverage on
        # the exchange, so the two cannot disagree.
        lev = eff_leverage(asset)
        size = (EXEC_MARGIN_USD * lev) / size_px
    elif EXEC_SIZING == "notional":
        # the SAME dollar amount goes into every trade. What that costs if
        # the stop hits then depends on the stop width: a 0.25% stop loses
        # 0.25% of the position, a 3% stop loses 3%.
        size = EXEC_NOTIONAL_USD / size_px
    else:
        size = EXEC_RISK_USD / per_unit
    if size_px != entry:
        log(f"{sym}: sizing off the live {fmt_px(size_px)} rather than the "
            f"doji close {fmt_px(entry)} "
            f"(stop {per_unit / size_px * 100:.3f}% vs "
            f"{abs(entry - stop) / entry * 100:.3f}%)")
    notional = size * size_px
    capped = ""
    if notional > EXEC_MAX_NOTIONAL_USD:
        size = EXEC_MAX_NOTIONAL_USD / size_px
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
              "TP_HALF": f"target filled - {HA_PARTIAL:.0%} booked, "
                         "stop to entry",
              "RUNNER": "HA flipped - runner closed at market",
              "MANUAL": "closed by hand from the dashboard",
              "BE": "runner stopped at entry - flat",
              }.get(event)
    if not action:
        return
    log_order({"t": now_ms(), "sym": asset["symbol"],
               "mode": "live" if EXEC_LIVE else "sim",
               "event": event, "price": price, "action": action})


def rebase_to_fill(sym, trade, fill, long_):
    """Re-derive the trade from the price actually paid. The stop is a
    structural level - the HA zone low - so it does NOT move; the
    target does, because 2R has to be measured from the real entry. Without
    this, market-IOC slippage silently changes the risk:reward: a fill worse
    than the candle close sits closer to the stop and further from the
    target than the plan assumed."""
    if not fill:
        return
    risk = (trade["stop"] - fill) if not long_ else (fill - trade["stop"])
    if risk <= 0:
        log(f"{sym}: filled at ${fmt_px(fill)}, through its own stop "
            f"${fmt_px(trade['stop'])} - protective orders will close it")
        return
    slip = (fill - trade["entry"]) / trade["entry"] * 100
    trade["entry"], trade["risk0"] = fill, risk
    trade["tp"] = fill + HA_RR * risk if long_ else fill - HA_RR * risk
    stop_pct = risk / fill * 100
    log(f"{sym}: filled ${fmt_px(fill)} ({slip:+.3f}% vs plan) - TP re-based "
        f"to ${fmt_px(trade['tp'])}, real stop {stop_pct:.3f}%")
    if MIN_STOP_PCT and stop_pct < MIN_STOP_PCT:
        log(f"{sym}: WARNING slippage left the stop {stop_pct:.3f}% away, "
            f"under the {MIN_STOP_PCT}% floor")


def resp_error(resp):
    """Hyperliquid signals failure by RETURNING an error, not by raising.
    Two shapes: {'status': 'err', 'response': '<msg>'} for account actions
    like update_leverage, and a nested statuses[0]['error'] for orders. A
    plain try/except catches neither. Seen live on xyz:SMSN: update_leverage
    returned "Cross margin is not allowed for this asset." and the agent
    carried on believing it had set 10x."""
    if not isinstance(resp, dict):
        return None if resp else "no response from the exchange"
    if resp.get("status") == "err":
        return str(resp.get("response") or "unspecified error")
    return None


def order_error(resp):
    """Hyperliquid returns status 'ok' at the TOP level even when the order
    was rejected - the real result sits in statuses[0]. Seen live on
    xyz:SMSN: {'status': 'ok', ... 'statuses': [{'error': 'Insufficient
    margin to place order.'}]}. Reading only the outer status made the agent
    believe it held a position: it then placed a stop and a take-profit
    against nothing, wrote a size onto the trade, and reported the alert as
    placed. Returns the error string, or None if the order really went
    through."""
    try:
        st = resp["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return None if resp else "no response from the exchange"
    if isinstance(st, dict) and st.get("error"):
        return str(st["error"])
    return None


def fill_size(resp):
    """How much of a market order ACTUALLY filled. A slippage-bounded IOC on
    a thin book fills partially, and every protective order has to be sized
    against the real position, not the requested one."""
    try:
        return float(resp["response"]["data"]["statuses"][0]["filled"]["totalSz"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _req_file(sym):
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in sym)
    return CLOSE_REQ_DIR / (safe + ".req")


def close_requested(sym):
    """The dashboard's close request for this symbol, or None.

    `done: True` means the DASHBOARD already closed it on the exchange and
    already booked the ledger row - all that is left is clearing state, and
    the agent must NOT close or book again.
    """
    try:
        f = _req_file(sym)
        if not f.exists():
            return None
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {}          # unreadable marker still means "close this"


def clear_close_request(sym):
    try:
        _req_file(sym).unlink()
    except OSError:
        pass


def close_position_live(asset, trade):
    """Close whatever is left at market, then cancel the resting orders.

    Closes FIRST: if the market order fails the protective stop is still on
    the book, so the position is never left naked by this path.
    """
    if not EXEC_LIVE or not executable(asset["symbol"]) or not trade.get("size"):
        return
    ex = exec_client()
    if not ex:
        return
    sym = exec_symbol(asset["symbol"])
    try:
        r = ex.market_close(sym)
        log(f"{sym}: LIVE manual close sent {r}")
    except Exception as e:
        log(f"{sym}: LIVE manual close FAILED {type(e).__name__}: {e}")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} manual close FAILED - "
                          "the position is still open, close it by hand")
        except Exception:
            pass
        return
    for label, oid in (("stop", trade.get("stop_oid")),
                       ("TP", trade.get("tp_oid"))):
        if not oid:
            continue
        try:
            ex.cancel(sym, oid)
        except Exception as e:
            log(f"{sym}: could not cancel the resting {label} "
                f"({type(e).__name__}) - it is reduce-only, so harmless "
                "with no position, but it will linger in the order book")


def order_oid(resp):
    """The exchange id of a resting order, so it can be cancelled later."""
    try:
        st = resp["response"]["data"]["statuses"][0]
        return st.get("resting", {}).get("oid")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def move_stop_live(asset, trade):
    """After the partial the stop belongs at entry. That means cancelling the
    resting stop and replacing it for what is left - without this the
    exchange still holds a stop at the HA zone low while the bot believes
    the runner is risk-free."""
    if not EXEC_LIVE or not executable(asset["symbol"]) or not trade.get("size"):
        return
    ex = exec_client()
    if not ex:
        return
    sym = exec_symbol(asset["symbol"])
    long_ = trade["verdict"] == "LONG"
    left = round(trade["size"] * trade["left"], sz_decimals(asset["symbol"]))
    try:
        if trade.get("stop_oid"):
            ex.cancel(sym, trade["stop_oid"])
        r = ex.order(sym, not long_, left, round_px(trade["entry"]),
                     {"trigger": {"triggerPx": round_px(trade["entry"]),
                                  "isMarket": True, "tpsl": "sl"}},
                     reduce_only=True)
        trade["stop_oid"] = order_oid(r)
        log(f"{sym}: LIVE stop moved to entry ${fmt_px(trade['entry'])} "
            f"for the remaining {left}")
    except Exception as e:
        log(f"{sym}: could NOT move the live stop ({type(e).__name__}: {e}) - "
            "the exchange stop is still at its original level")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} partial booked but the "
                          "stop could not be moved to entry - check manually")
        except Exception:
            pass


def fill_price(resp):
    """The average price actually paid, from a market_open response."""
    try:
        return float(resp["response"]["data"]["statuses"][0]["filled"]["avgPx"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def place_entry_live(asset, trade, plan):
    """Entry, then the protective stop, then the TP. If the protective
    orders cannot be placed the position is closed immediately - never sit
    unprotected."""
    # NEVER return silently from here. A sized entry that does not reach the
    # exchange still gets tracked and still books to the ledger, so a silent
    # skip leaves a paper trade that looks real - and the journal cannot say
    # why. Every refusal below names itself.
    if not EXEC_LIVE:
        log(f"{asset['symbol']}: live execution OFF (EXEC_LIVE=False) - "
            "tracked only, no order sent")
        return None
    ex = exec_client()
    if not ex:
        log(f"{asset['symbol']}: execution client UNAVAILABLE - no order "
            "sent, but the trade is still being tracked")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(asset['symbol'])} sized but "
                          "the execution client is unavailable - tracked "
                          "only, nothing was sent to Hyperliquid")
        except Exception:
            pass
        return None
    # builder-venue markets (xyz:NBIS, ...) are not in the main perp meta the
    # SDK client was built against - it raises KeyError on the asset lookup.
    # Alert on them, but never try to trade them.
    if not executable(asset["symbol"]):
        log(f"{asset['symbol']}: live execution skipped - builder-venue "
            "market and its dex is not in EXEC_BUILDER_DEXES (alert only)")
        return None
    # a builder market keeps its full "dex:coin" name for the SDK; a
    # main-dex market is just its own name
    sym = exec_symbol(asset["symbol"])
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
    # SET the leverage before entering. Without this, sizing in "margin"
    # mode is only an assumption: a symbol left at 3x on the account would
    # post three times the intended collateral for the same position.
    if EXEC_SIZING == "margin":
        lev = eff_leverage(asset)
        err = None
        # EXEC_MARGIN_MODE decides the FIRST attempt; the other mode is the
        # fallback, because some markets refuse one or the other and say so
        # by RETURNING an error rather than raising.
        # the venue's own flag wins over the preference - a strictIsolated
        # market can never take cross, so do not ask
        forced = only_isolated(asset["symbol"])
        first = False if forced else (EXEC_MARGIN_MODE != "isolated")
        if forced:
            log(f"{sym}: venue marks this market ISOLATED-ONLY - not "
                "attempting cross")
        prev_err = ""
        for is_cross in (first, not first):
            try:
                r = ex.update_leverage(lev, sym, is_cross)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                break
            err = resp_error(r)
            if not err:
                mode = "cross" if is_cross else "ISOLATED"
                if is_cross != first:
                    log(f"{sym}: {EXEC_MARGIN_MODE} refused ({prev_err}) - "
                        f"set {lev}x {mode} instead")
                else:
                    log(f"{sym}: leverage {lev}x {mode}")
                break
            prev_err = err
            low = err.lower()
            if "cross" not in low and "isolated" not in low:
                break
        if err:
            log(f"{sym}: could NOT set leverage to {lev}x ({err}) - "
                "refusing the entry rather than sizing against unknown "
                "collateral")
            try:
                send_telegram(f"\u26a0\ufe0f {esc(sym)} entry refused - "
                              f"could not set {lev}x leverage: {esc(err)}")
            except Exception:
                pass
            return None
    # MARGIN IS THE ONLY REAL CONSTRAINT now the position cap is off, and
    # nothing else guards it. Check it here so a short account produces a
    # named skip instead of an "Insufficient margin" rejection from the
    # venue - the order was never going to fill either way.
    need = size * trade["entry"] / max(eff_leverage(asset), 1)
    avail = free_collateral()
    if avail is not None and avail < need * 1.05:
        log(f"{sym}: ENTRY SKIPPED - needs ${need:.2f} margin, "
            f"${avail:.2f} free")
        return None
    try:
        r = ex.market_open(sym, long_, size)
        log(f"{sym}: LIVE entry sent {r}")
        err = order_error(r)
        if err:
            # never place protective orders against a position that does
            # not exist, and never let the alert claim this was placed
            log(f"{sym}: ENTRY REJECTED by the exchange - {err}")
            try:
                send_telegram(f"\u26a0\ufe0f {esc(sym)} entry REJECTED - "
                              f"{esc(err)}")
            except Exception:
                pass
            return None
        got = fill_size(r)
        if got is not None and abs(got - size) > 10 ** -dec:
            log(f"{sym}: PARTIAL FILL - asked {size}, filled {got} "
                f"({got / size:.0%}); protective orders sized to the fill")
            size = got
        if not size:
            log(f"{sym}: nothing filled - no position to protect")
            return None
        rebase_to_fill(sym, trade, fill_price(r), long_)
    except Exception as e:
        log(f"{sym}: LIVE entry FAILED {type(e).__name__}: {e}")
        try:
            send_telegram(f"\u26a0\ufe0f {esc(sym)} entry order failed - "
                          "no position")
        except Exception:
            pass
        return None
    trade["size"] = size
    per_unit = abs(trade["entry"] - trade["stop"])
    target = ({"margin": f"${EXEC_MARGIN_USD:.2f} margin at "
                         f"{eff_leverage(asset)}x",
               "notional": f"${EXEC_NOTIONAL_USD:.2f} in"}
              .get(EXEC_SIZING, f"${EXEC_RISK_USD:.2f} risk"))
    log(f"{sym}: position {size} units = ${size * trade['entry']:.2f} in, "
        f"loses ${size * per_unit:.2f} at the stop (target {target})")
    try:
        r_stop = ex.order(sym, not long_, size, round_px(trade["stop"]),
                          {"trigger": {"triggerPx": round_px(trade["stop"]),
                                       "isMarket": True, "tpsl": "sl"}},
                          reduce_only=True)
        trade["stop_oid"] = order_oid(r_stop)
        # the target only takes HA_PARTIAL of the position - the rest runs
        # until the HA flips, so it must NOT be resting at the target
        part = round(size * HA_PARTIAL, dec)   # == size when HA_PARTIAL is 1.0
        r_tp = ex.order(sym, not long_, part, round_px(trade["tp"]),
                        {"limit": {"tif": "Gtc"}}, reduce_only=True)
        trade["tp_oid"] = order_oid(r_tp)
        log(f"{sym}: LIVE stop ${fmt_px(trade['stop'])} (full size) and TP "
            f"${fmt_px(trade['tp'])} ({HA_PARTIAL:.0%}) placed")
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


def protect_position(asset, trade):
    """Place the stop and the partial TP for a trade that already exists on
    the exchange. Used when ADOPTING a position the agent did not open -
    a hand-reversed one, say - where the protective orders that belonged to
    the old side are gone. Returns True if both legs rested."""
    if not EXEC_LIVE or not executable(asset["symbol"]):
        return False
    ex = exec_client()
    if not ex or not trade.get("size"):
        return False
    sym = exec_symbol(asset["symbol"])
    long_ = trade["verdict"] == "LONG"
    dec = sz_decimals(asset["symbol"])
    size = round(trade["size"], dec)
    try:
        r_stop = ex.order(sym, not long_, size, round_px(trade["stop"]),
                          {"trigger": {"triggerPx": round_px(trade["stop"]),
                                       "isMarket": True, "tpsl": "sl"}},
                          reduce_only=True)
        trade["stop_oid"] = order_oid(r_stop)
        part = round(size * HA_PARTIAL, dec)   # == size when HA_PARTIAL is 1.0
        r_tp = ex.order(sym, not long_, part, round_px(trade["tp"]),
                        {"limit": {"tif": "Gtc"}}, reduce_only=True)
        trade["tp_oid"] = order_oid(r_tp)
        log(f"{sym}: ADOPTED position re-protected - stop "
            f"${fmt_px(trade['stop'])}, TP ${fmt_px(trade['tp'])}")
        return True
    except Exception as e:
        log(f"{sym}: could NOT protect the adopted position "
            f"({type(e).__name__}: {e})")
        try:
            send_telegram(f"\U0001F6A8 {esc(sym)} is OPEN AND UNPROTECTED - "
                          "the agent adopted it but could not place a stop. "
                          "Close it or set a stop by hand now")
        except Exception:
            pass
        return False


POS_CACHE = {"t": 0.0, "v": None}   # exchange positions, refreshed once per
#                                    scan - never per symbol
CLOSED_THIS_SCAN = set()            # symbols this scan has closed. POS_CACHE
#                                    is a snapshot from the TOP of the scan,
#                                    so anything closed during it still looks
#                                    open - and the orphan sweep would adopt
#                                    the position it just closed. Live 3 Aug
#                                    on CRV: stop booked, ensure_flat closed
#                                    it, the sweep re-adopted it 9s later at
#                                    the same stop price


def exchange_positions():
    """{symbol: signed size} for everything actually open on the account.
    None if the read fails - callers must treat that as "unknown", never as
    "flat", or a failed request would look like every position closing."""
    try:
        st = _EXEC["info"].user_state(_EXEC["addr"]) or {}
    except Exception as e:
        log(f"exchange_positions() failed: {type(e).__name__}: {e}")
        return None
    out = {}
    for p in st.get("assetPositions") or []:
        d = p.get("position") or {}
        try:
            szi = float(d.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if szi:
            out[d.get("coin")] = (szi, float(d.get("entryPx") or 0))
    return out


def cancel_stale_orders(asset, trade):
    """Cancel the resting stop and TP of a position that no longer exists.

    Reduce-only orders on a flat symbol can never fill, but they still
    RESERVE MARGIN - that is what drove withdrawable to 0.00 on 3 Aug with
    15 of them resting. Best effort: a cancel that fails is logged, never
    raised, because the booking has already happened by this point."""
    if not EXEC_LIVE or not executable(asset["symbol"]):
        return
    ex = exec_client()
    if not ex:
        return
    sym = exec_symbol(asset["symbol"])
    for label, oid in (("stop", trade.get("stop_oid")),
                       ("TP", trade.get("tp_oid"))):
        if not oid:
            continue
        try:
            ex.cancel(sym, oid)
            log(f"{sym}: cancelled stale {label} order {oid}")
        except Exception as e:
            log(f"{sym}: stale {label} cancel failed ({type(e).__name__})")


def stop_bars():
    """STOP_LOOKBACK expressed in TF candles.

    The signal path counts it in STOP_TF bars, so 12 means twelve HOURS.
    adopt_position, open_reverse and reverse_alert were slicing raw TF
    candles with the same number, which meant three hours - the same knob
    describing two different spans depending on which path you were in."""
    return max(1, STOP_LOOKBACK * max(1, MS.get(STOP_TF, MS[TF]) // MS[TF]))


def adopt_position(asset, ast, candles, coin_sz, entry_px):
    """Take over a live position the agent is not tracking correctly.

    Covers the hand-reversed case - flip the side in Trust Wallet and the
    agent is left tracking a long while the account is short, with the old
    reduce-only stop and TP no longer reducing anything, so the position
    sits UNPROTECTED. Books whatever was tracked, tracks what is actually
    held, and puts fresh protective orders behind it."""
    sym = asset["symbol"]
    now_long = coin_sz > 0
    old = ast.get("trade")
    if old:
        px = candles[-1]["c"] if candles else entry_px
        log(f"{sym}: tracked {old['verdict']} but the exchange holds "
            f"{'LONG' if now_long else 'SHORT'} - booking the old side")
        # BOOK IT DIRECTLY. _close_trade ends with ensure_flat(), which reads
        # the account, sees a live position and MARKET-CLOSES it - and the
        # live position here is the one being adopted. Routing the booking
        # through _close_trade therefore closed the very position the user
        # had just reversed into. Seen live 3 Aug. The partial books through
        # record_close for the same reason; this must too.
        record_close(sym, old, px, "MANUAL", now_ms(),
                     frac=old.get("left", 1.0))
        if ALERT_LIFECYCLE:
            try:
                send_telegram(lifecycle_message(
                    asset, "MANUAL", old, px, now_ms(),
                    "position changed outside the agent - booked on adopt"))
            except Exception:
                pass
        RUN_ALERTS.append(f"{sym} old side booked on adopt")
    lo = max(0, len(candles) - stop_bars())
    window = candles[lo:] if candles else []
    if not window:
        log(f"{sym}: adopted but no candles for a stop - left unprotected")
        return
    entry = entry_px or window[-1]["c"]
    stop = (min(x["l"] for x in window) if now_long
            else max(x["h"] for x in window))
    risk = (entry - stop) if now_long else (stop - entry)
    if risk <= 0:
        log(f"{sym}: adopted but the {len(window)}-candle extreme is already "
            "through the entry - no stop placed")
        return
    trade = {"verdict": "LONG" if now_long else "SHORT",
             "entry": entry, "stop": stop,
             "tp": entry + HA_RR * risk if now_long else entry - HA_RR * risk,
             "size": abs(coin_sz), "left": 1.0, "half": False,
             "risk0": risk, "rr": HA_RR, "opened_t": now_ms(),
             "checked_t": 0, "source": "ADOPTED"}
    ast["trade"] = trade
    ast["phase"] = "IN_TRADE"
    protect_position(asset, trade)
    try:
        send_telegram(
            f"\U0001f501 <b>ADOPTED \u00b7 {esc(sym)}</b>\n"
            f"<i>the position changed outside the agent</i>\n\n"
            f"Now tracking <b>{trade['verdict']}</b> {abs(coin_sz)}\n"
            f"Entry: <code>${fmt_px(entry)}</code>\n"
            f"Stop:  <code>${fmt_px(stop)}</code>\n"
            f"TP:    <code>${fmt_px(trade['tp'])}</code>")
    except Exception:
        pass


# --------------------------- entry -----------------------------------------
def open_reverse(asset, ast, candles, was_long):
    """Flip the position the dashboard just closed.

    The close is already BOOKED by the caller, so the ledger keeps both
    halves and the two never disagree - which is the whole reason this
    lives here rather than being tapped in the wallet. Levels come from the
    same rules an entry uses; nothing else about the entry chain applies,
    because a reverse is a deliberate override of it."""
    sym = asset["symbol"]
    if not candles:
        return
    c = candles[-1]
    now_long = not was_long
    lo_i = max(0, len(candles) - stop_bars())
    window = candles[lo_i:]
    stop = (min(x["l"] for x in window) if now_long
            else max(x["h"] for x in window))
    direction = "LONG" if now_long else "SHORT"
    log(f"{sym}: REVERSE requested - flipping to {direction}, stop at the "
        f"{len(window)}-candle {'low' if now_long else 'high'} "
        f"${fmt_px(stop)}")
    fire_entry(asset, ast, direction, c, stop, c["h"], c["l"], "REVERSE",
               f"reversed by hand from the dashboard - stop at the "
               f"{len(window)}-candle {'low' if now_long else 'high'}",
               live_px=c["c"])


def fire_entry(asset, ast, direction, c, stop, hi, lo, source, trigger,
               live_px=None):
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
    tp = entry - HA_RR * risk if short else entry + HA_RR * risk
    tgt_pct = abs(tp - entry) / entry * 100 if entry else 0.0
    if MIN_TARGET_PCT and tgt_pct < MIN_TARGET_PCT:
        log(f"{sym}: target only {tgt_pct:.2f}% away "
            f"(min {MIN_TARGET_PCT}%) - move too small to be worth taking, "
            "waiting")
        return False
    plan = {"entry": entry, "stop": stop, "tp": tp}
    event_t = c["t"] + MS[TF]

    # overrides are gone: check_asset never evaluates a symbol that already
    # holds a trade, so fire_entry is only ever reached flat
    if ast.get("trade"):
        log(f"{sym}: {direction} signal ignored - a trade is already open")
        ast["setup"] = None
        return False

    # count BEFORE recording the new trade: ast is the same object STATE_VIEW
    # holds, so counting afterwards makes the trade count itself and a cap of
    # 1 then blocks every live order that could ever be sent
    # only symbols that CAN execute count toward the live cap - builder-venue
    # trades are placed by hand and must not starve the automated ones
    open_now = sum(1 for k, v in STATE_VIEW.items()
                   if isinstance(v, dict) and v.get("trade") and executable(k))
    ast["trade"] = {"verdict": direction, "entry": entry, "stop": stop,
                    "tp": tp, "opened_t": c["t"], "checked_t": c["t"],
                    "rr": HA_RR, "risk0": risk, "half": False, "left": 1.0}
    # remember WHICH setup this came from so it cannot fire a second time
    if ast.get("setup"):
        ast["traded"] = {"ft": ast["setup"].get("ft"), "dir": direction}
    order_plan = plan_entry_orders(asset, ast["trade"], live_px)
    placed, why_not = False, ""
    if not EXEC_LIVE:
        why_not = "live execution is OFF"
    elif not order_plan:
        why_not = "the trade could not be sized"
    else:
        # the daily loss limit needs realised USD from the ledger, which is
        # not tracked yet - only the halt file and the position cap bite
        why = exec_blocked(open_now, 0.0)
        if why:
            why_not = why
            log(f"{sym}: live order blocked - {why}")
        else:
            place_entry_live(asset, ast["trade"], order_plan)
            # place_entry_live returns None on every refusal path, so the
            # only reliable proof an order reached the exchange is the size
            # it wrote back onto the trade
            placed = bool(ast["trade"].get("size"))
            if not placed:
                why_not = "the order did not reach the exchange"

    # ALERT LAST, from the trade as it now stands. Sending it before the
    # order meant Telegram carried the PLANNED levels (the doji candle's
    # close) while the dashboard showed the REBASED ones (the actual fill),
    # so the two disagreed on entry, stop distance and target for every
    # executed trade. Now both read from the same numbers.
    t = ast["trade"]
    filled = abs(t["entry"] - entry) > 1e-12
    alert_plan = dict(plan, entry=t["entry"], stop=t["stop"], tp=t["tp"])
    if ALERT_ENTRIES:
        try:
            msg = entry_message(asset, direction, alert_plan, hi, lo,
                                source, event_t, trigger)
            if not placed:
                # An alert that looks identical whether or not a position
                # exists is worse than no alert. Say so on its own line.
                msg += (f"\n\n\u26a0\ufe0f NOT PLACED on Hyperliquid - "
                        f"{esc(why_not)}. Tracked only.")
            send_telegram(msg)
        except Exception as e:
            log(f"{sym}: entry alert failed: {type(e).__name__}: {e}")
    log(f"ALERT SENT -> telegram: {sym} {direction} ENTRY @ "
        f"${fmt_px(t['entry'])} ({trigger})"
        + (f" [filled, planned ${fmt_px(entry)}]" if filled else "")
        + ("" if placed else f" [NOT PLACED - {why_not}]"))
    RUN_ALERTS.append(f"{sym} {direction} entry @ ${fmt_px(t['entry'])}")
    if not placed and not TRACK_UNPLACED and EXEC_LIVE:
        # NOTHING WAS OPENED, so nothing is tracked. The alert above already
        # said NOT PLACED. Keeping the trade would put a row on the dashboard
        # that no hand-close can ever clear: there is no position to close,
        # and the GONE reconciler skips sizeless trades by design.
        log(f"{sym}: not placed and TRACK_UNPLACED is off - not tracking it")
        ast["trade"], ast["phase"], ast["setup"] = None, "SCAN", None
        return True
    ast["phase"], ast["setup"] = "IN_TRADE", None
    return True


# --------------------------- the strategy ----------------------------------
def process_candle(asset, ast, candles, ha, i):
    """A visible trend, then a DOJI - an HA body small against that trend.
    The doji is the turn, and the trade is taken on it. No pullback, no
    retest, no confirming candle.

    Everything is re-derived from the series on every scan, so a restart
    cannot lose half a signal.
    """
    sym = asset["symbol"]
    c = candles[i]
    hd = ha[i]

    # record where this symbol sits in the chain, for the dashboard. Report
    # only - it never gates anything. Written every scan so the panel is
    # never staler than the last candle close.
    try:
        g = gate_status(ha, candles, i, sym)
        if g:
            g["t"] = hd["t"]
            g["px"] = c["c"]
        # None means "no flip in the last two bars" - CLEAR the old row
        # rather than leaving a stale one on the panel forever
        ast["gate"] = g       # save_state runs at the end of every scan
    except Exception as e:
        log(f"{sym}: gate_status failed: {type(e).__name__}: {e}")

    for signal_long in (True, False):
        found = ha_nowick_signal(ha, i, signal_long)
        if not found:
            continue
        # THE 50 EMA DECIDES WHICH SIDE IS ALLOWED. Shorts only below it,
        # longs only above, so a reversal is never taken against the wider
        # trend it is turning inside of.
        ok_ema, ema_v, ema_px = ema_side(candles, i, signal_long)
        if not ok_ema:
            gap = (abs(ema_px - ema_v) / ema_v * 100) if ema_v else 0
            why = ("on the wrong side of"
                   if ((ema_px > ema_v) != signal_long)
                   else f"{gap:.2f}% from (needs <= {EMA_RETEST_PCT}%)")
            log(f"{sym}: {'LONG' if signal_long else 'SHORT'} setup but "
                f"price ${fmt_px(ema_px)} is {why} the "
                f"{EMA_FILTER_LEN} EMA ${fmt_px(ema_v)} - skipped")
            continue
        d = found[0]                         # the FLIP bar - what the stop
        #                                      window sits behind
        # THE SIGNAL'S OWN SIDE STANDS. Bitcoin used to override this for
        # every alt, which made sense when the doji was pure timing - but
        # the 50 EMA now decides which side is allowed, and it tests the
        # SETUP's direction. Letting BTC flip it afterwards would take a
        # short that passed "below the EMA" and enter it long.
        want_long = signal_long if HA_MODE == "reversal" else not signal_long
        direction = "LONG" if want_long else "SHORT"
        dt = hd["t"]                         # identify by TIMESTAMP, never by
        #                                      index - the fetch window rolls
        rt = ha[found[1]]["t"]               # timestamp of the RUN's first
        #                                     candle - the dedupe key

        # ONE TRADE PER TREND RUN. The signal is re-derived every scan, so
        # without this the same setup fires again on the next pass at a
        # slightly different price into the identical stop. Seen live 31 Jul
        # on AAVE, CASHCAT, KAITO and SOL: -6.13% of a -14.53% book.
        #
        # Keyed on the RUN START, not the doji. Under HA_DOJI_COLOUR="same"
        # the doji is TREND-COLOURED, so it does not end the run - the next
        # candle can be another small trend-coloured body, a fresh doji
        # timestamp, and the identical trade. Keying on the run start means
        # one trade per trend, however many stalls it prints. Under "flip"
        # the doji ends the run anyway, so this is equivalent there.
        done = ast.get("traded") or {}
        if done.get("ft") == rt and done.get("dir") == direction:
            continue

        # The doji marks WHERE the turn happened, but the stop is taken from
        # the REAL candle at that point, not the HA one. HA highs and lows
        # are EMA averages that need never have printed, so an HA-derived
        # stop is a price the market may never trade to. The real candle's
        # extreme is a level that actually exists on the book.
        # It also guarantees positive risk: for a long, low <= close always.
        # THE WINDOW SITS BEFORE THE DOJI, not before the entry. The doji at
        # d is the turn; the level being risked against is the extreme of
        # the move that led INTO it, so the confirmation bars after it are
        # excluded - they are already part of the new direction and would
        # drag the stop toward entry.
        # the run start comes back from the detector, so the stop can span
        # the move being turned instead of the last few bars of it - which
        # on a fade are the SMALLEST ones, putting the stop nearest entry
        # exactly when conviction is weakest
        run_start = found[1]
        # THE STOP IS MEASURED ON STOP_TF. Resample the bars BEFORE the flip,
        # offset so the last group ends exactly at the flip rather than
        # wherever the series happens to start - an unaligned grouping would
        # shift every level by up to factor-1 bars.
        if STOP_SOURCE == "percent":
            # every stop the SAME distance, ignoring structure entirely
            ep = c["o"] if ENTRY_AT_OPEN else c["c"]
            window = [c]
            stop = (ep * (1 - MAX_STOP_PCT / 100) if want_long
                    else ep * (1 + MAX_STOP_PCT / 100))
        elif STOP_SOURCE == "turn":
            # the flip bar and the no-wick bar - the reversal itself. On TF,
            # since resampling two specific candles would blur them into the
            # hour around them.
            window = candles[d:i] or candles[max(0, d - 1):d]
        else:
            sfac = max(1, MS.get(STOP_TF, MS[TF]) // MS[TF])
            pre = candles[d % sfac:d] if sfac > 1 else candles[:d]
            higher = resample(pre, sfac)
            if STOP_SOURCE == "run":
                span = -(-(d - run_start) // sfac)  # ceil, in STOP_TF bars
                need = max(STOP_LOOKBACK, span)
            else:
                need = STOP_LOOKBACK
            window = higher[-need:] if higher else candles[max(0, d - need):d]
        if not window:
            log(f"{sym}: {direction} flip at the start of the series - no "
                "candles before it to take a stop from, skipped")
            continue
        # "percent" already HAS its stop; taking the window extreme here
        # silently replaced it with the entry bar's own low - a 0.2% stop
        # where 10% was configured. Only the structural modes derive one.
        if STOP_SOURCE != "percent":
            stop = (min(x["l"] for x in window) if want_long
                    else max(x["h"] for x in window))
        # CLAMP. A 12-hour window can hand back a level 20%+ away, which on
        # a leveraged perp sits past liquidation - the stop would never fire
        # and the position would be liquidated for the full collateral
        # instead. Pull it in to MAX_STOP_PCT so the order still works.
        # WIDEN FIRST, then clamp - so the band is honoured in both
        # directions and a widened stop can never exceed the ceiling.
        if STOP_WIDEN_PCT and STOP_SOURCE != "percent":
            ep = c["o"] if ENTRY_AT_OPEN else c["c"]
            near = abs(ep - stop) / ep * 100 if ep else 0
            if near < STOP_WIDEN_PCT:
                wider = (ep * (1 - STOP_WIDEN_PCT / 100) if want_long
                         else ep * (1 + STOP_WIDEN_PCT / 100))
                log(f"{sym}: stop {near:.2f}% away widened to "
                    f"{STOP_WIDEN_PCT:.1f}% (${fmt_px(stop)} -> "
                    f"${fmt_px(wider)})")
                stop = wider
        if MAX_STOP_PCT:
            entry_px = c["o"] if ENTRY_AT_OPEN else c["c"]
            far = abs(entry_px - stop) / entry_px * 100 if entry_px else 0
            if far > MAX_STOP_PCT:
                capped = (entry_px * (1 - MAX_STOP_PCT / 100) if want_long
                          else entry_px * (1 + MAX_STOP_PCT / 100))
                log(f"{sym}: stop {far:.2f}% away clamped to "
                    f"{MAX_STOP_PCT:.1f}% (${fmt_px(stop)} -> "
                    f"${fmt_px(capped)})")
                stop = capped
        # ENTRY IS THE OPEN of this candle, not its close: the setup
        # completed on the previous bar, so the trade is taken as this one
        # begins. Everything downstream still keys off c, so hand it a copy
        # whose close IS that open - fire_entry, the alert and the ledger
        # then all agree on one entry price.
        entry = c["o"] if ENTRY_AT_OPEN else c["c"]
        c = dict(c, c=entry)
        risk = (entry - stop) if want_long else (stop - entry)
        if risk <= 0:
            log(f"{sym}: {direction} doji but entry is already through the "
                f"{len(window)}-candle "
                f"{'low' if want_long else 'high'} - no risk distance, "
                "skipped")
            continue

        ast["setup"] = {"dir": direction, "zhi": c["h"], "zlo": c["l"],
                        "ft": rt, "departed": True, "touched": True,
                        "frozen": True, "t": c["t"]}
        log(f"{sym}: HA DOJI CONFIRMED after {HA_CONFIRM_BARS} bar(s) - "
            f"turning {direction}, stop at the {len(window)}-candle "
            f"{'low' if want_long else 'high'} "
            f"{'at the turn' if STOP_SOURCE == 'turn' else 'before the flip'} "
            f"${fmt_px(stop)} (flip was {fmt_ts(ha[d]['t'])}, run began "
            f"{fmt_ts(ha[found[1]]['t'])})")
        # candles[-1] is the still-forming candle: its close is the current
        # price, free, with no extra API call
        fire_entry(asset, ast, direction, c, stop, c["h"], c["l"], "HA",
                   f"HA {'down' if want_long else 'up'}trend faded, flipped "
                   f"{'green' if want_long else 'red'}, then a no-wick "
                   f"candle - entered at the next open, stop at the "
                   f"{len(window)}-candle extreme before the flip, "
                   f"{'low' if want_long else 'high'}",
                   live_px=candles[-1]["c"])
        return True

    if ast.get("setup"):
        ast["setup"] = None
    return False


# --------------------------- per-asset scan --------------------------------
def adopt_orphans(state, assets):
    """Positions held on the exchange that NOTHING in state is tracking.

    The per-symbol path only compares sides on symbols that already have a
    tracked trade, so it cannot see these at all. They are the dangerous
    shape: live size, no stop the agent knows about, and no ledger row when
    they eventually close.

    Scoped by ADOPT_ORPHANS so a position opened deliberately outside the
    agent's universe is reported and left alone rather than taken over."""
    if ADOPT_ORPHANS == "off":
        return False
    # FRESH read, not POS_CACHE: the scan has been closing positions since
    # that snapshot was taken, and a stale one makes them look like orphans.
    live = exchange_positions()
    if live is None:
        return False
    known = {exec_symbol(a["symbol"]): a for a in assets}
    changed = False
    for coin, (szi, entry_px) in live.items():
        if coin in CLOSED_THIS_SCAN:
            log(f"{coin}: closed earlier this scan - not adopting the "
                "position on its way out")
            continue
        asset = known.get(coin)
        if asset is None:
            if ADOPT_ORPHANS == "universe":
                log(f"{coin}: position held outside the agent's universe "
                    f"({szi:+g}) - reported, not adopted")
                continue
            asset = {"symbol": coin, "hl_coin": coin,
                     "label": f"{coin}-PERP", "fallbacks": [], "cls": "crypto"}
        sym = asset["symbol"]
        ast = state.get(sym) or blank_asset_state()
        if ast.get("trade"):
            continue                       # the side check already covers it
        try:
            _, cs = fetch(asset, TF, 60)
            if not cs:
                log(f"{sym}: orphan position but no candles - not adopted")
                continue
            log(f"{sym}: ORPHAN position on the exchange ({szi:+g}) with "
                "nothing tracked - adopting")
            adopt_position(asset, ast, cs, szi, entry_px)
            state[sym] = ast
            changed = True
        except Exception as e:
            log(f"{sym}: orphan adoption failed: {type(e).__name__}: {e}")
    return changed


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
        source, cs = fetch(asset, TF, 60)
        # a manual close from the dashboard beats everything else, including
        # the stop and target watch - the request is only cleared once the
        # position is actually flat, so a failed close is retried next scan
        req = close_requested(sym)
        if req is not None:
            # capture the side BEFORE the close clears the trade
            # NOT gated on done. The dashboard closes the position itself
            # and marks done=True, which is exactly the state open_reverse
            # expects: booked and flat. Requiring `not done` meant the
            # button closed and never opened the other side.
            rev = bool(req.get("reverse"))
            was_long = (ast.get("trade") or {}).get("verdict") == "LONG"
            if req.get("done"):
                # the dashboard already closed it and booked the row
                log(f"{sym}: manually closed from the dashboard at "
                    f"${fmt_px(req.get('exit') or 0)} - clearing state")
            else:
                px = cs[-1]["c"] if cs else ast["trade"]["entry"]
                log(f"{sym}: manual close requested from the dashboard")
                close_position_live(asset, ast["trade"])
                _close_trade(asset, ast["trade"], px, "MANUAL", now_ms(),
                             "closed by hand from the dashboard")
            ast["trade"] = None
            ast["phase"] = "SCAN"
            clear_close_request(sym)
            RUN_STATUS.append(f"{sym} manually closed")
            if rev:
                try:
                    open_reverse(asset, ast, cs, was_long)
                except Exception as e:
                    log(f"{sym}: reverse FAILED after the close - now FLAT: "
                        f"{type(e).__name__}: {e}")
                    try:
                        send_telegram(
                            f"\u26a0\ufe0f {esc(sym)} closed but the reverse "
                            "did NOT open - you are flat")
                    except Exception:
                        pass
            state[sym] = ast
            return True
        # ADOPT BEFORE WATCHING. If the position changed outside the agent -
        # a hand-reversed side, a hand-closed position - watching the old
        # trade would book an outcome that never happened, and a flipped
        # position is sitting with no working stop.
        live = POS_CACHE.get("v")
        if live is not None and cs:
            held = live.get(exec_symbol(sym))
            tracked_long = ast["trade"]["verdict"] == "LONG"
            if held and (held[0] > 0) != tracked_long:
                adopt_position(asset, ast, cs, held[0], held[1])
                state[sym] = ast
                return True
            # TRACKED BUT GONE - the third shape, and the one that produced
            # the DOGE phantom: state held a SHORT 4276 while the account was
            # flat and no orders rested. Without this the agent watches a
            # stop that can never trigger, forever, and the ledger never
            # gets its row.
            #
            # TWO GUARDS, both load-bearing. Only trades with a SIZE are
            # reconciled, so a tracked-but-never-placed paper trade is left
            # alone. And only trades opened BEFORE the positions read, since
            # POS_CACHE is filled at the top of the scan - a trade opened
            # later in this same scan is legitimately absent from it and
            # would otherwise be booked as gone the instant it opened.
            opened = (ast["trade"].get("opened_t") or 0)
            if (not held and ast["trade"].get("size")
                    and opened < POS_CACHE.get("t", 0) * 1000):
                px = cs[-1]["c"]
                log(f"{sym}: tracked {ast['trade']['verdict']} but the "
                    "exchange is FLAT - booking it closed")
                record_close(sym, ast["trade"], px, "GONE", now_ms(),
                             frac=ast["trade"].get("left", 1.0))
                try:
                    send_telegram(
                        f"\u26a0\ufe0f <b>{esc(sym)}</b> was tracked "
                        f"{ast['trade']['verdict']} but the exchange is flat "
                        f"- booked closed at <code>${fmt_px(px)}</code>")
                except Exception:
                    pass
                cancel_stale_orders(asset, ast["trade"])
                ast["trade"] = None
                ast["phase"] = "SCAN"
                state[sym] = ast
                RUN_STATUS.append(f"{sym} booked GONE")
                return True
        if cs:
            trade, ch = process_open_trade(asset, ast["trade"], cs,
                                           smoothed_ha(cs), cs[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        # exits win over overrides (the watch ran first). Fall through to the
        # candle walk when the trade just closed, or when overrides are on.
        if ast["trade"]:
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

    ha = smoothed_ha(cs)
    last_closed = len(cs) - 2
    # ENTRY_AT_OPEN lets the signal path see the FORMING bar. That is safe
    # here and nowhere else: ha_nowick_signal reads only i-1 (the no-wick
    # bar), i-2 (the flip) and the run before them - all closed - and the
    # entry price is cs[i]["o"], fixed the moment the bar prints. Waiting
    # for the bar to close would enter a full candle after the open the
    # rule names, which on 15m is 15 minutes of drift.
    last_eval = (len(cs) - 1) if ENTRY_AT_OPEN else last_closed
    cutoff = cs[last_closed]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        if i > last_eval or cs[i]["t"] <= ast["last_candle_t"]:
            continue
        changed = process_candle(asset, ast, cs, ha, i) or changed
        ast["last_candle_t"] = cs[i]["t"]
        if ast["trade"] and ast["trade"].get("opened_t") == cs[i]["t"]:
            break                              # a trade opened on this candle

    # an open trade always reports IN_TRADE, even when a fresh setup is armed
    # on the same symbol - otherwise the run summary undercounts open trades
    setup = ast.get("setup")
    armed_dir = setup["dir"] if setup else None
    if ast["trade"]:
        stage = "IN_TRADE"
    elif setup:
        stage = ("HA-" + armed_dir
                 + (" pulled back" if setup.get("touched") else " waiting"))
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
    # ONE positions read per scan, shared by every symbol. None means the
    # read failed, and callers treat that as "unknown" - never as flat, or a
    # timeout would look like every position closing at once.
    CLOSED_THIS_SCAN.clear()
    POS_CACHE["v"] = exchange_positions() if EXEC_LIVE else None
    POS_CACHE["t"] = time.time()
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

        # AFTER the per-symbol pass: anything held that nothing tracks
        try:
            changed = adopt_orphans(state, assets) or changed
        except Exception as e:
            log(f"orphan sweep failed: {type(e).__name__}: {e}")

        new_cursor = stopped_at if stopped_at is not None else 0
        if meta.get("cursor", 0) != new_cursor:
            state["_meta"] = {"cursor": new_cursor}
            changed = True
        # the state file always gets written: its contents double as the
        # dashboard's liveness heartbeat
        state["_meta"] = dict(state.get("_meta") or {},
                              cursor=new_cursor,
                              scan_every_s=MS[SCAN_EVERY] // 1000,
                              tf=TF,
                              tz=TIMEZONE,
                              last_scan_utc=datetime.now(timezone.utc)
                              .isoformat(timespec="seconds"))
        save_state(state)
        if failures:
            log(f"{failures} asset(s) failed this run - they retry next cycle.")
    finally:
        write_run_summary()


def seconds_to_next_close(buffer_s=15):
    period = MS[SCAN_EVERY] // 1000
    return period - (time.time() % period) + buffer_s


def run_loop():
    log("smoothed HA agent started (loop mode). Ctrl+C to stop.")
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
        send_telegram("\u2705 <b>smoothed HA agent</b> - test message, "
                      "Telegram wiring is good.")
        log("test message sent")
        return
    if "--loop" in args:
        run_loop()
        return
    check_once()


if __name__ == "__main__":
    main()
