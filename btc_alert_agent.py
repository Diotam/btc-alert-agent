#!/usr/bin/env python3
"""
IMPULSE MACD AGENT (LazyBear)
-----------------------------
Two pathways off one indicator. Long side described; shorts mirror it.

  md = mi>hi ? mi-hi : (mi<lo ? mi-lo : 0)   hi/lo = SMMA(high/low, 34)
  sb = SMA(md, 9)                            mi    = ZLEMA(hlc3, 34)
  sh = md - sb                               md is EXACTLY 0 inside the band

  PATHWAY 1 - EXTENSION. md crossing UP through the signal is upward
     momentum, crossing DOWN is downward. Only MAJOR crossovers count:
     the cross must happen BEYOND the band, below the oversold line for a
     long or above the overbought line for a short. Crossovers between the
     lines are minor and ignored. Stop at the nearest swing, target 1.5R.

  PATHWAY 2 - BREAKOUT. Wait for md to sit flat - exactly 0, the
     indicator's own "inside the band" state - for a long stretch, which
     is a range. Then take the first histogram push with the candle
     agreeing. Stop slightly below entry, target 2.5R.

  The overbought line is a PERCENTILE of the symbol's own recent |md|,
  not a fixed number: md is in price units, so one constant cannot serve
  BTC and kPEPE at once.

OLDER ENGINES still in the file, all switched off: reversal-200 (RS_MODE),
impulse-free doji (SD_MODE), EMA crossover (CROSS_MODE), 4h flip
(FLIP_MODE). Their notes follow.

  2. DOJI - an HA body no more  2. DOJI - an HA body no more than HA_DOJI_FRACTION of the biggest body in
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
DISCOVER_DEXES = True              # scan HIP-3 builder venues. ON as of
                                   # 2 Aug: with EXEC_BUILDER_DEXES empty
                                   # they could only ever alert, never trade,
                                   # so every xyz signal was noise that also
                                   # booked paper rows into the ledger. False
                                   # falls back to DEXES = [""], the main dex
                                   # alone. Turn both back on together if the
                                   # xyz pool is ever funded again
ADMIT_COMMODITIES = True
ADMIT_STOCKS = True                # equities IN the universe. Moot while
                                   # DISCOVER_DEXES is off - every xyz market
                                   # is out either way. COMMODITIES DO NOT
                                   # NEED THIS DOOR: the ones he trades, PAXG
                                   # among them, are MAIN-DEX perps already
                                   # in the crypto universe. Original note:
                                   # they are
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
EXEC_BUILDER_DEXES = ("xyz",)      # builder dexes to trade AUTOMATICALLY.
                                   # ON as of 5 Aug, his call: COMMODITIES
                                   # margin from the MAIN pool even though
                                   # they carry the xyz prefix - only the
                                   # STOCKS sit behind a separate pool, and
                                   # ADMIT_STOCKS is False so none are in the
                                   # universe. Original note follows,
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
ONLY_SYMBOLS = ()
                                   # if non-empty, the universe is EXACTLY
                                   # these and nothing else - volume floors,
                                   # MAX_ASSETS and dex discovery no longer
                                   # decide anything. Narrowed to four on
                                   # 8 Aug, his call. Empty tuple restores
                                   # the discovered universe
EXCLUDE = []                       # never trade these (matches the base name
                                   # on any venue). PUMP was removed from
                                   # this list 3 Aug - it trades again
MAX_ASSETS = 100

ASSETS = [                         # used when DISCOVER_ALL = False, or when
    {"symbol": "BTC", "label": "BTC-PERP", "hl_coin": "BTC",   # discovery fails
     "fallbacks": ["binance:BTCUSDT", "kraken:XBTUSD"]},
]

# --- strategy dials -------------------------------------------------------
TF = "30m"                         # execution timeframe. 15m -> 30m on
                                   # 9 Aug: a 50 EMA on 15m was too fast
                                   # for these markets, so price crossed
                                   # it constantly without going
                                   # anywhere - PUMP moved 0.48% between
                                   # crosses at 15m and 1.16% at 30m
SCAN_EVERY = "5m"                   # how often the loop wakes. Aligning it to
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
BTC_ALIGN = False                  # BITCOIN CORRELATION. Alts follow BTC, so
                                   # a long taken while BTC is falling is
                                   # fighting the thing that actually moves
                                   # the book. Measured the SAME way as every
                                   # other trend here - the slope of BTC's
                                   # own 50 EMA - rather than the old HA
                                   # colour gate, so one idea of "trending"
                                   # runs through the whole engine
BTC_ALIGN_PCT = 0.30               # BTC's slope must exceed this before it
                                   # gets a vote. BELOW it BTC is NEUTRAL and
                                   # BLOCKS NOTHING - that matters, because a
                                   # filter that refuses every trade whenever
                                   # BTC drifts would gate the engine to
                                   # silence, which is how every earlier
                                   # version died. 0 disables
_BTC_BIAS = {"t": 0.0, "v": None}
BTC_TREND_TTL_S = 120              # one BTC fetch per scan, not per symbol
_BTC_CACHE = {"t": 0.0, "v": None}
HA_MIN_BODY_PCT = 0                # the trend run must contain at least one
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
BREAKOUT_ON = False                # the SECOND engine, 6 Aug. It trades the
                                   # markets the HA engine cannot: those whose
                                   # EMA slope is too flat to call a trend.
                                   # The two never compete for the same symbol
BO_TF = "1h"                       # TIMEFRAME the RANGE is measured on. The
                                   # break itself is still judged on the
                                   # current TF close, but the level it has
                                   # to clear comes from BO_LOOKBACK bars of
                                   # THIS timeframe - so 20 x 1h is twenty
                                   # HOURS of structure, not five. A 15m
                                   # range is a lunch break; an hourly one is
                                   # a level the market actually respected.
                                   # Set to TF for the old behaviour
BO_LOOKBACK = 20                   # bars forming the range (5 hours on 15m)
BO_TIGHT_PCT = 3.0                 # the range must be NO WIDER than this % of
                                   # price. Without it every drifting market
                                   # "breaks out" of its own noise every few
                                   # bars - a coiled range is the setup, a
                                   # sprawling one is just chop
BO_BUFFER_PCT = 0.05               # how far BEYOND the level the close must
                                   # sit, as a % of price, so a touch is not
                                   # a break
BO_TAG = "bo"                      # ledger tag for breakout trades
ENGINE_TAG = "ha"                  # which detector produced a trade. Written
                                   # onto the trade record and into EVERY
                                   # ledger row, so a second engine's results
                                   # can be told apart from this one's later.
                                   # Rows written before 6 Aug have no tag;
                                   # any analysis should read a missing tag
                                   # as "ha"
CROSS_MODE = False                 # THE EMA-CROSSOVER ENGINE, 8 Aug, his
                                   # spec. REPLACES the HA flip engine when
                                   # on. Price crossing the 50 EMA arms a
                                   # side; the first NO-WICK candle after the
                                   # cross triggers entry at the NEXT open.
                                   # The position then runs until price
                                   # crosses BACK, which closes it and arms
                                   # the other side - so the book is always
                                   # in the market, flipping at each cross.
                                   # No target and no structural stop: the
                                   # cross is the only exit
CROSS_DISASTER_PCT = 10.0          # ...except this. A cross-only exit can sit
                                   # through an unbounded move, and at market
                                   # max leverage that reaches LIQUIDATION
                                   # before any cross arrives. This is a
                                   # brake, not a strategy stop - it should
                                   # almost never fire. 0 removes it and
                                   # makes liquidation the only backstop

# ---- 16 Aug rework of the cross engine, his spec: the CROSS ALONE enters,
# intrabar, with volume required, and 1.5R or the stop is the only way out.
CROSS_NEEDS_NOWICK = False         # the 8 Aug two-step (cross arms, no-wick
                                   # candle triggers) is OFF. The cross is
                                   # the entry.
CROSS_INTRABAR = True              # do NOT wait for the 30m close. Compare
                                   # the LIVE price against the EMA on every
                                   # 5m pulse and enter the moment the side
                                   # differs from the last CLOSED bar's side.
                                   # Costs a fetch on every symbol every
                                   # pulse - the L4283 429 pattern - and the
                                   # EMA repaints intrabar, since the forming
                                   # bar's live close is inside its own EMA.
CROSS_NEEDS_VOL = True             # no volume on the bar, no entry.
CROSS_MIN_VOL = 0.0                # absolute floor. v must be STRICTLY
                                   # greater than this. Kept at 0 - an
                                   # absolute volume number is not comparable
                                   # across BTC and kPEPE, so the real gate is
                                   # the multiple below.
CROSS_VOL_MULT = 1.0               # volume must reach this multiple of the
                                   # symbol's OWN recent average. 1.0 = at
                                   # least average, 2.0 = twice average. This
                                   # scales across all 48 markets where an
                                   # absolute floor cannot. 0 disables it and
                                   # leaves only CROSS_MIN_VOL.
CROSS_VOL_LEN = 20                 # bars in that average.
CROSS_MAX_BARS = 3                 # 16 Aug: 1.5R gets THIS MANY bars from the
                                   # entry bar (inclusive) to fill. If it has
                                   # not, the trade is closed at market. On
                                   # 30m that is 90 minutes. Checked AFTER the
                                   # target test, so a bar that reaches 1.5R
                                   # on the deadline books a WIN, not an
                                   # expiry. 0 disables the expiry entirely.
                                   # Why a window at all: his CRV/MON pair -
                                   # MON filled 1.5R inside the entry bar,
                                   # CRV needed the NEXT bar. Same setup, same
                                   # volume; the difference was how far the
                                   # 12h swing low sat. A 1-bar rule would
                                   # have thrown CRV away for being 0.29% shy.
CROSS_TREND_GATE = True            # 16 Aug: LONGS need the 20 EMA's slope to
                                   # be >= 0 (an uptrend) or negative and
                                   # RISING (beginning one). SHORTS mirror it.
                                   # A cross into a trend that is still
                                   # steepening the OTHER way is refused - the
                                   # counter-trend pop, which is what a failed
                                   # breakout looks like even with volume.
CROSS_SLOPE_BARS = 5               # bars per slope window. 5 on 30m = 2.5h.
                                   # Two adjacent windows are compared, so
                                   # this needs 2x this many bars of history.
# ===================== STOCH-DOJI ENGINE, his spec 17 Aug =====================
# A doji ends an HA run, two clean candles the other way confirm the turn, and
# the stochastic must be oversold WITHOUT strong momentum behind the fall.
# This engine is SELF-CONTAINED: it does not use any of the 16-17 Aug cross
# filters (volume pace, EMA slope, flat zone, trend age). CROSS_MODE = False
# switches all of that off.
# ======================= REVERSAL 200SMA ENGINE, his spec 18 Aug ============
# 30m. NOTHING from any previous engine feeds this: no doji, no stochastic,
# no HA wicks, no ATR gate, no volume, no trend-age. SD_MODE / CROSS_MODE /
# FLIP_MODE are all off.
#
#   UPTREND   (price above the 200SMA): close below the 20 ARMS a SHORT,
#             then a close below the 50 ENTERS it.
#   DOWNTREND (price below the 200SMA): close above the 20 ARMS a LONG,
#             then a close above the 50 ENTERS it.
#
# ===================== IMPULSE MACD (LazyBear), his spec 20 Aug ============
# md = mi>hi ? mi-hi : (mi<lo ? mi-lo : 0), sb = SMA(md,9), sh = md-sb
# NOTHING from the reversal-200 engine feeds this. RS_MODE is off.
IM_MODE = True
IM_LEN = 34                        # LazyBear's default
IM_SIG = 9                         # signal line

# ---- PATHWAY 1: EXTENSION. Only crossovers OUTSIDE the bands are taken -
# a cross UP while md is BELOW the oversold line is a LONG, a cross DOWN
# while ABOVE the overbought line is a SHORT. Crossovers between the lines
# are ignored: those are his "minor" crossovers, too close to the middle.
IM_P1_ON = True
IM_BAND_MODE = "percentile"        # how the overbought line is set.
                                   # "percentile" - a percentile of THIS
                                   #   symbol's own recent |md|, so the split
                                   #   between major and minor scales per
                                   #   market. md is in PRICE UNITS, so a
                                   #   fixed number would mean something
                                   #   different on BTC than on kPEPE.
                                   # "pct_of_price" - IM_BAND as a % of price
                                   # "abs" - IM_BAND literally
IM_BAND_PCTILE = 70                # with "percentile": the cut. 70 means the
                                   # top 30% of |md| readings count as MAJOR.
IM_BAND_DAYS = 30                  # 20 Aug: the percentile is taken over a
                                   # THIRTY DAY window, converted to bars from
                                   # TF - 1440 bars on 30m. LOOKBACK below had
                                   # to rise to match, or the window silently
                                   # clamped to the 400 bars actually fetched
                                   # and the "30 day" band was really 8 days.
IM_BAND_LOOKBACK = 0               # bars; 0 means derive it from IM_BAND_DAYS
IM_BAND = 0.5                      # used by the other two modes
IM_REQUIRE_TURN = True             # 20 Aug: the md LINE must itself be
                                   # heading the trade's way at the cross.
                                   # A crossover only says md moved above sb -
                                   # md can still be FALLING while sb falls
                                   # faster, which is a "bullish" cross with
                                   # momentum still deteriorating. This
                                   # requires md rising for a long, falling
                                   # for a short.
                                   # MEASURED over 1500 bars: it binds on
                                   # about 2% of crossovers. sb is a 9-bar SMA
                                   # OF md, so md has nearly always turned
                                   # already by the time it crosses its own
                                   # average - the condition is mostly
                                   # implied. Kept because the 2% it catches
                                   # are real, but do not expect it to change
                                   # the signal count.
IM_TURN_BARS = 3                   # bars the turn is measured over.
IM_TURN_REF = "md"                 # 21 Aug: WHAT the turn is measured
                                   # AGAINST. "band" scaled it by the
                                   # overbought level - but the band is a
                                   # percentile of a 30-day |md| sample that
                                   # is mostly ZEROS, so in any sustained
                                   # trend md sits far above it and the band
                                   # becomes a tiny yardstick. PENGU 21 Aug:
                                   # band 8.1e-05, md 4.4e-04 - five times
                                   # above it, permanently "overbought", and
                                   # a 2.5% wobble in md measured 0.134 of the
                                   # band and passed. "md" measures the turn
                                   # against |md| itself, so what counts is
                                   # how much the impulse line moved RELATIVE
                                   # TO HOW EXTENDED IT ALREADY IS.
IM_TURN_MIN = 0.10                 # with "md": the turn must be this fraction
                                   # of |md|. On the PENGU sideways stretch
                                   # the LARGEST turn was 0.064 and the two
                                   # tangled crossovers were 0.025 and 0.045,
                                   # so 0.10 rejects the lot. Needs
                                   # recalibrating on real trending data - it
                                   # was set to reject a known-bad case, which
                                   # is not the same as knowing it keeps the
                                   # good ones.
IM_TURN_MIN_BAND = 0.045           # 20 Aug: HOW HARD md must be turning, as a
                                   # fraction of the band. The sign test alone
                                   # was useless - any sideways wiggle has a
                                   # sign, which is why it only bound on 2% of
                                   # crosses. FARTCOIN 20 Aug: md and sb drifted
                                   # along together near 0.004 and tangled, and
                                   # that crossover was taken. md must now move
                                   # at least this fraction of the band across
                                   # IM_TURN_BARS. The band is the per-symbol
                                   # yardstick, so this scales like everything
                                   # else. 0 disables the magnitude test.
                                   # CALIBRATED, not guessed: across 82
                                   # crossovers the turn size ranged 0.000 to
                                   # 0.161 of the band, median 0.044. 0.15
                                   # rejected EVERY signal; 0.045 sits at the
                                   # median and drops the flatter half.
                                   # Raise toward 0.08 to be stricter.
IM_P1_RR = 1.5                     # target, in R
IM_SWING_BARS = 20                 # the nearest swing high/low for the stop

# ---- PATHWAY 2: BREAKOUT. Wait for md to sit FLAT (exactly 0 - the
# indicator's own "inside the band" state) for a long stretch, then take the
# first histogram push, with price agreeing.
IM_P2_ON = True
IM_FLAT_BARS = 20                  # 21 Aug: 6 -> 20 at his call. md must sit
                                   # at exactly 0 for this many bars - ten
                                   # hours on 30m - before a push counts as a
                                   # range breakout.
                                   # MEASURED over 3000 bars (~62 days):
                                   #   vol/bar   >=6   >=10   >=20
                                   #     0.2%     39     35     16
                                   #     0.4%     32     19      9
                                   #     0.7%     22     11      2
                                   #     1.2%     11      6      0
                                   # So 20 is selective on quiet markets and
                                   # near-silent on volatile ones - a fast
                                   # symbol may never produce a qualifying
                                   # range at all. That is the trade: fewer,
                                   # cleaner setups, concentrated in the
                                   # calmer half of the book.
IM_P2_RR = 2.5                     # target, in R
IM_P2_STOP = "swing"               # 21 Aug: pathway 2 stops at the NEAREST
                                   # SWING high/low, same as pathway 1, rather
                                   # than a fixed % below entry. His spec said
                                   # "slightly below entry", which needed a
                                   # number I had to invent; the swing is a
                                   # level the market actually made.
                                   # "pct" restores the fixed distance.
IM_P2_STOP_PCT = 0.35              # used only when IM_P2_STOP = "pct".
IM_STOP_ON_CLOSE = True            # close-confirmed stops: a wick through the
                                   # level that closes back inside does not
                                   # take the trade out. Carried across from
                                   # the reversal engine at his 20 Aug ask -
                                   # it was the stop-hunt fix and applies the
                                   # same way here.
IM_DISASTER_R = 1.5                # ...unless price runs this many stop
                                   # distances past entry, which exits at
                                   # once. 0 disables it.
# =============================================================================

RS_MODE = False
RS_TREND_LEN = 200                 # the regime MA
RS_ARM_LEN = 20                    # arms the setup
RS_TRIGGER_LEN = 50                # the MIDDLE line of the stack. 18 Aug: it
                                   # no longer pulls the trigger - the 20
                                   # cross does that on its own.
# ---- 18 Aug restructure. The three lines must be FANNED IN ORDER and all
# three must be TRENDING. Crossed or flat stacks take no trades at all.
#   above the 200:  20 > 50 > 200   |   below the 200:  20 < 50 < 200
# Then a close CROSSING the 20 is the entry - down for a short, up for a
# long - in either regime. The regime only certifies the stack is clean.
RS_WITH_TREND = True               # 18 Aug: DIRECTION COMES FROM THE FAN.
                                   #   bullish fan -> LONG only, on a cross
                                   #                  UP through the 20
                                   #   bearish fan -> SHORT only, on a cross
                                   #                  DOWN through the 20
                                   # The earlier spec allowed both sides in
                                   # both regimes; this one does not. That
                                   # makes the engine TREND-FOLLOWING - it
                                   # buys a pullback that reclaims the 20 -
                                   # despite "Reversal" in the name below.
                                   # False restores both-sides behaviour.
RS_STACK_ORDER = True              # require the fan. False = order ignored.
RS_FLAT_PCT = 0.05                 # each line must move at least this much,
                                   # as a % of itself, over RS_SLOPE_BARS.
                                   # This is the "no flat lines" rule and the
                                   # number is NOT from his spec - calibrate
                                   # it against how many symbols qualify.
RS_SLOPE_BARS = 5                  # bars the slope is measured over
RS_REQUIRE_DIVERGING = True        # 18 Aug: the lines must be SPREADING, not
                                   # closing up. Slope alone does not catch
                                   # this - all three can rise while the gaps
                                   # between them shrink, which is the 20
                                   # decelerating and the 200 catching up: a
                                   # trend on its last legs, about to tangle.
                                   # Both gaps (20-50 and 50-200) are measured
                                   # as a % of price and compared with
                                   # RS_SLOPE_BARS ago. BOTH must widen.
RS_REVERSAL_ON = True              # 18 Aug: CONVERGENCE IS NOW A SETUP, not
                                   # just a veto. A fan that is closing up is
                                   # a trend ending, so we watch for the turn:
                                   #   bearish fan converging -> a cross UP
                                   #     through the 50 ARMS a LONG, and a
                                   #     cross UP through the 200 ENTERS it
                                   #   bullish fan converging -> mirrored,
                                   #     down through the 50 then the 200
                                   # The fan ORDER must still hold - this is a
                                   # tightening trend, not a tangle. The arm
                                   # is dropped if price falls back through
                                   # the 50.
RS_REV_ARM_CANCEL = True           # a close back across the 50 clears the arm
RS_DIVERGE_GAPS = "50_200"         # WHICH gap decides diverging vs
                                   # converging. "50_200" uses only the
                                   # structural gap. "both" also requires the
                                   # 20-50 gap to widen - which CONTRADICTS
                                   # the continuation setup, because a
                                   # pullback to the 20 necessarily closes
                                   # that gap, so every continuation entry was
                                   # reclassified as a reversal and none could
                                   # fire. The 20 is the TRIGGER line and is
                                   # meant to oscillate; only the 50 and 200
                                   # describe the trend's structure.
RS_DIVERGE_MIN = 0.0               # how much they must widen, in percentage
                                   # POINTS. 0 = any widening counts. Raise it
                                   # to demand the fan actually open out
                                   # rather than merely not close.
RS_STOP_ON_CLOSE = True            # 18 Aug: CLOSE-CONFIRMED STOP. A wick
                                   # THROUGH the swing stop that closes back
                                   # inside does NOT take the trade out - only
                                   # a bar that CLOSES past the level does,
                                   # and then the exit is at that close, not
                                   # at the level. THE COST: a genuine loss
                                   # runs slightly past 1R. The backstop is
                                   # RS_DISASTER_R below.
RS_DISASTER_R = 1.5                # ...unless price runs THIS many stop
                                   # distances past entry, which exits at
                                   # once, no close needed. Close confirmation
                                   # is safe against wicks but exposed on a
                                   # runaway bar; this caps that tail.
                                   # 0 disables it.
RS_INTRABAR = True                 # 18 Aug: enter the MOMENT price crosses
                                   # the 20, not on the bar close. Three
                                   # things follow. The floor is the 5m scan
                                   # pulse, so "the moment" means within five
                                   # minutes. Every market is refetched every
                                   # pulse, which is the L4283 HTTP 429
                                   # pattern. And an intrabar cross can
                                   # UN-cross before the bar closes, so this
                                   # takes entries a close-based rule never
                                   # would. The STACK is still judged on
                                   # CLOSED bars, so the fan and the slopes
                                   # never repaint - only the cross is live.
RS_MA = "smma"                     # 18 Aug: SMA -> SMMA at his call, on all
                                   # three lengths. Wilder's smoothed MA:
                                   # seeded with the SMA, then
                                   # (prev*(n-1) + close)/n. That is an EMA
                                   # with alpha 1/n, so SMMA(20) tracks about
                                   # like an EMA(39) - MUCH slower than the
                                   # SMA(20) it replaces. Expect fewer arms,
                                   # later triggers and a 200 line that turns
                                   # slowly. "sma" and "ema" still work.
RS_TREND_BY_SLOPE = False          # False: "uptrend" means price is ABOVE the
                                   # 200SMA. True: it means the 200SMA is
                                   # RISING. His wording fits the first.
RS_ARM_ON_CROSS = True             # 18 Aug: the arm needs a CROSSING, not a
                                   # level. "Closes below the 20" means the
                                   # PREVIOUS close was above it and this one
                                   # is below. Without this it was a level
                                   # test, so a symbol that broke down on
                                   # MONDAY was still "below the 20" today,
                                   # armed on the spot and - being below the
                                   # 50 as well - triggered on the same bar.
                                   # xyz:SNDK entered that way on a cross
                                   # three days stale. False restores the
                                   # level test.
RS_SAME_BAR_OK = True              # may one bar both ARM and TRIGGER? A close
                                   # under the 20 is usually also under the 50
                                   # when the break is sharp. False forces the
                                   # trigger onto a LATER bar.
RS_ARM_EXPIRY = 0                  # bars an arm survives. 0 = forever, until
                                   # cancelled or fired. NOT IN HIS SPEC.
RS_ARM_CANCEL = True               # a close back on the far side of the 20
                                   # clears the arm. NOT IN HIS SPEC either -
                                   # without it a setup armed days ago can
                                   # still fire.
RS_RR = 1.0                        # 18 Aug: 1.5 -> 1.0 at his call. The
                                   # target now sits the SAME distance from
                                   # entry as the stop. Break-even therefore
                                   # needs a win rate above 50% before costs,
                                   # where 1.5R only needed 40%. It should
                                   # fill more often - the question is whether
                                   # enough more often to cover the smaller
                                   # win. Nothing in the ledger can answer
                                   # that yet: it stores no R.
RS_STOP = "swing"                  # his 18 Aug call: the RECENT SWING high
                                   # for a short, swing low for a long.
RS_STOP_CONT = "200smma"           # 18 Aug: the CONTINUATION path (pathway 1,
                                   # diverging fan, cross of the 20) now stops
                                   # at the 200 SMMA instead of the swing.
                                   # NOTE THE SIZE: in a fanned trend the 200
                                   # sits a long way from price, so R becomes
                                   # several percent and the 1.5R target
                                   # several more. "swing" restores the
                                   # 20-bar stop. The REVERSAL path (pathway
                                   # 2) is unaffected and still uses the
                                   # swing - its entry IS the 200 cross, so a
                                   # 200 stop would be zero distance.
RS_SWING_BARS = 20                 # how far back "recent" reaches, in TF
                                   # bars. 20 on 30m is ten hours. NOT IN HIS
                                   # SPEC - a fresh default, not inherited
                                   # from any previous engine's stop window.
RS_ONE_PER_SETUP = True            # his 18 Aug call: NO re-firing. After a
                                   # trigger, price must close back across the
                                   # 20 before the engine will arm again -
                                   # otherwise a sustained break re-armed and
                                   # re-triggered on every single bar.
# =============================================================================

SD_MODE = False                    # the stoch-doji engine owns entries
# ---- 17 Aug: ATR STOP, CLOSE-CONFIRMED. Replaces the time-window swing stop
# FOR THE STOCH-DOJI ENGINE ONLY. A window stop produced a distance unrelated
# to the setup: BTC 0.20% because six hours happened to be quiet, SKR 5.386%
# because the window caught an unrelated low. R was noise, so 1.5R was noise,
# so MIN_TARGET_PCT kept rejecting perfectly good setups.
SD_ATR_STOP = True                 # False falls back to the swing stop
SD_ATR_LEN = 14                    # ATR lookback, in TF bars
SD_ATR_MULT = 2.0                  # stop sits this many ATR from entry.
                                   # 1.5 stops more often, 3.0 rarely.
SD_STOP_ON_CLOSE = True            # the stop fires only when a bar CLOSES
                                   # past it. A wick through and back leaves
                                   # the trade alone - that is the stop-hunt
                                   # fix. THE COST: on a real move you exit at
                                   # the close, not the level, so genuine
                                   # losses run slightly past 1R.
SD_MAX_STOP_PCT = 2.0              # 17 Aug: CAP ON R. ATR is inflated by gaps
                                   # and spread on thin books, so the same 2x
                                   # ATR gave 0.47% on BTC but 3.60% on
                                   # xyz:CXMT, 3.76% on xyz:SKHX and 2.47% on
                                   # CASHCAT - and those were the two biggest
                                   # losses of 17 Aug. A stop wider than this
                                   # % of price is CLAMPED to it, which also
                                   # pulls the 1.5R target back in range.
                                   # 0 disables the cap.
SD_DISASTER_R = 1.5                # ...unless price runs THIS many stop
                                   # distances past entry, which exits
                                   # IMMEDIATELY, no close needed. Close
                                   # confirmation is safe against wicks but
                                   # exposed on a runaway bar; this caps that
                                   # tail. 0 disables it.
STOCH_N = 14                       # %K lookback
STOCH_SMOOTH_K = 1                 # 14,1,3 - the FAST stochastic, his pick
STOCH_D = 3                        # %D = SMA of %K over this
STOCH_LOW = 20.0                   # the "bottom line"
STOCH_HIGH = 80.0                  # the top line, for shorts
STOCH_CROSS_LOOKBACK = 6           # the oversold CROSS does not happen on the
                                   # same bar as the entry - the doji plus two
                                   # confirmations take at least three bars -
                                   # so the cross is allowed to have happened
                                   # this many bars back.
SD_CONFIRM_BARS = 2                # candles the other way needed after the
                                   # doji. His rule: each must have a wick on
                                   # ONE side only (the side the trade runs
                                   # WITH). A candle wicked at BOTH ends is
                                   # SKIPPED, not counted and not fatal.
SD_CONFIRM_WINDOW = 8              # give up if two clean candles have not
                                   # appeared within this many bars.
# ---- WEAK vs STRONG momentum. His definition: weak = the prior HA run was
# SHORT and SHALLOW. A long deep fall is a strong one and we stay out.
# NOTE THE REVERSAL: on 17 Aug morning the rule was "enter after a STRONG
# downtrend flip" and CROSS_REVERSAL_MIN_BARS/PCT were MINIMUMS. These are
# MAXIMUMS. The two specs are opposites; this one is live.
# His 17 Aug definition: WEAK momentum is SMALLER HA BARS leading into the
# flip - the move running out of steam, not the absence of a move. The old
# test was a pair of CEILINGS (run <= 8 bars AND <= 2%), which a 1-bar 0.00%
# run passed trivially: BTC entered on exactly that. Deceleration cannot be
# measured without several bars, so the floor comes for free.
SD_WEAK_MIN_BARS = 3               # need at least this many bars in the run
                                   # before deceleration means anything
SD_WEAK_TAIL = 3                   # the last N bars are the "leading up to
                                   # the flip" part
SD_WEAK_DECAY = 0.70               # tail bodies must average at most this
                                   # fraction of the earlier bodies. 0.70 =
                                   # 30% smaller. Lower is stricter.
SD_WEAK_MAX_BARS = 0               # SUPERSEDED by the decay test. Non-zero
SD_WEAK_MAX_PCT = 0.0              # re-enables them as extra ceilings.
# =============================================================================

CROSS_REVERSAL_ON = True           # 17 Aug: ENTER AFTER A STRONG DOWNTREND
                                   # HA COLOUR FLIP. This DELIBERATELY
                                   # OVERRIDES the slope gate, which would
                                   # otherwise refuse the setup as
                                   # "downtrend still steepening" - a flip out
                                   # of a downtrend is counter-trend by
                                   # definition, and earlier today that was
                                   # the thing being screened OUT. It is now
                                   # the thing being screened FOR.
                                   # The slope/flat gates still apply to
                                   # NON-reversal entries.
CROSS_REVERSAL_MIN_BARS = 6        # the prior opposite run must be at least
                                   # this many smoothed-HA bars. "Strong"
                                   # means sustained, not a two-bar dip.
CROSS_REVERSAL_MIN_PCT = 2.0       # ...AND price must have moved at least
                                   # this far against us across that run, in
                                   # %. Bars alone would pass a long slow
                                   # drift; this is what makes it a downtrend
                                   # rather than a slope.
CROSS_TRIGGER = "sha_flip"         # 17 Aug: WHAT OPENS THE TRADE.
                                   #   "sha_flip"  - the SMOOTHED HA colour
                                   #                 flip. Fires EARLY: the
                                   #                 smoothed series turns
                                   #                 before price clears a
                                   #                 20-bar average.
                                   #   "ema_cross" - the 8 Aug price/EMA20
                                   #                 cross. LAGGING BY
                                   #                 CONSTRUCTION - enough move
                                   #                 has to pile up to drag
                                   #                 price past the average of
                                   #                 the last ten hours, which
                                   #                 is why alerts kept landing
                                   #                 at the END of trends.
                                   # The EMA is now a FILTER (slope + flat
                                   # zone), not the trigger. Everything else -
                                   # volume, swing stop, 1.5R, the 3-bar
                                   # expiry - is unchanged and shared.
CROSS_MAX_TREND_AGE = 6            # 17 Aug: NO ENTRIES INTO A TREND ALREADY
                                   # UNDERWAY. The smoothed HA is the trend
                                   # read: if it has ALREADY been the trade's
                                   # colour for more than this many bars, the
                                   # move started without us and the entry is
                                   # buying the end of it. His SOXL crossed
                                   # Sunday 19:00 and was still "entering" 40
                                   # bars later at the top; HEMI fired ~15
                                   # bars into its run. 0 disables the gate.
                                   # Measured at SHA_IN/SHA_OUT - see the note
                                   # there, the agent is on 5,5 and his charts
                                   # are on 10,10, which give DIFFERENT ages.
CROSS_FLAT_PCT = 0.20              # 17 Aug: DEAD ZONE. If the EMA's slope is
                                   # inside +/- this and is not materially
                                   # moving, the market is CHOP and NOTHING
                                   # enters. Without it a flat EMA satisfies
                                   # slope >= 0 AND slope <= 0, so sideways
                                   # markets permitted entries in BOTH
                                   # directions - ENA, his 17 Aug example.
                                   # A slope near zero that is CHANGING by at
                                   # least CROSS_TURN_MIN still counts as a
                                   # turn and is allowed through, otherwise
                                   # this would also kill every "beginning a
                                   # trend" entry, which by definition has a
                                   # slope near zero. 0 disables the zone.
                                   # CALIBRATE IT - run slopes.py on the box.
CROSS_TURN_MIN = 0.25              # how much the slope must actually MOVE, in
                                   # percentage POINTS, to count as turning. A
                                   # bare now-vs-prior test has a hole: slope
                                   # is a % of the EMA, so a steady LINEAR
                                   # uptrend shows a slowly DECAYING % slope
                                   # (+2.516 -> +2.471) and read as "beginning
                                   # a downtrend", permitting shorts in a
                                   # healthy uptrend. This deadband kills that.
CROSS_ALLOW_TURN = True            # the "or beginning one" half. False makes
                                   # it slope-sign only and refuses every
                                   # reversal entry. NOTE: a slope requirement
                                   # has been measured before on the flip
                                   # engine and the data went AGAINST it -
                                   # this wants a replay before it is trusted.
CROSS_VOL_PRORATE = True           # the FORMING bar's volume accumulates from
                                   # zero across the 30m, so judging it against
                                   # a full-bar average would refuse every
                                   # early entry and only admit trades near the
                                   # close - silently undoing CROSS_INTRABAR.
                                   # Scale the requirement by the fraction of
                                   # the bar elapsed. Closed bars are judged
                                   # against the full average.
CROSS_STOP_SWING = True            # swing stop over stop_bars(), as the flip
                                   # engine uses, so 1.5R here means what it
                                   # means there. False falls back to the
                                   # flat CROSS_DISASTER_PCT, which made 1.5R
                                   # a 15% move.
CROSS_EXIT_ON_CROSSBACK = False    # the cross-back exit is OFF. With it on,
                                   # the block at L2580 `continue`s past the
                                   # target test and 1.5R could never book.
LONG_LOOKBACK_DAYS = 30            # is the 50 EMA LOWER than it was this
                                   # many days ago? If so the market is in a
                                   # multi-week downtrend and no long is
                                   # taken, however good the setup looks. His
                                   # call 13 Aug. Measured on the 4h series -
                                   # 30 days is 180 4h bars, where 1h only
                                   # reaches ~21 days at LOOKBACK 500.
                                   # 0 disables
_LTREND = {}                       # per-symbol cache, same TTL as the regime
REGIME_ON = True                   # THE HIGHER-TIMEFRAME PERMISSION GATE.
                                   # The 15m pattern says WHEN; this says
                                   # WHETHER. Longs only while price is above
                                   # the REGIME_TF EMA, shorts only below
REGIME_TF = "4h"                   # timeframe the permission EMA lives on.
                                   # Falls back to resampling 1h if the venue
                                   # will not serve it directly
REGIME_EMA_LEN = 50                # 50 x 4h = 200 hours, about 8 days. Slow
                                   # enough that a two-day pullback does not
                                   # revoke permission
REGIME_SLOPE_BARS = 0              # the regime EMA must also be MOVING, not
                                   # just have price on one side of it. His
                                   # call 15 Aug: a short needs a genuine
                                   # downtrend, and price can sit below a
                                   # RISING EMA during a pullback inside an
                                   # uptrend. Measured over this many
                                   # REGIME_TF bars - 2 x 4h = EIGHT HOURS
REGIME_SLOPE_PCT = 0.0             # how far it must have moved, as a % of the
                                   # EMA. 0 means "any movement in the right
                                   # direction"; raise it to demand a steeper
                                   # trend. A FLAT EMA returns 0 and blocks
                                   # NOTHING, as before
_REGIME = {}                       # per-symbol cache, TTL below
REGIME_TTL_S = 300                 # one higher-TF fetch per symbol per scan
ALLOW_SHORTS = True                # 17 Aug, LATER: back ON for the stoch-doji
                                   # engine, which mirrors the setup - a doji
                                   # ending a GREEN run with two clean red
                                   # candles and %K crossing ABOVE 80 is the
                                   # short. The long-only window earlier today
                                   # lasted about an hour and was never
                                   # deployed.
                                   # take the short side at all. FALSE earlier
                                   # as of 17 Aug, at his call, on the 580
                                   # legs of his own ledger spanning 19 Jul -
                                   # 9 Aug: LONGS made +82.61% at a 39% win
                                   # rate while SHORTS lost -25.37% at 32%
                                   # over a comparable 280 legs.
                                   # THE CAVEAT, and it is not small: that
                                   # window was a broadly rising market, so a
                                   # long-only engine looks excellent in an
                                   # uptrend and bleeds in a downtrend.
                                   # THE CONTRADICTING MEASUREMENT: the
                                   # full-year 2026 replay put long-only at
                                   # -424.5R, against +444R for both sides
                                   # with the regime filter - a ~870R swing on
                                   # the same codebase. The three-week ledger
                                   # sample simply did not contain the regime
                                   # where the short side pays. Both numbers
                                   # were taken on the 1h no-wick FLIP engine,
                                   # not this one, so neither transfers
                                   # cleanly - but the year is the larger
                                   # sample and it points the other way.
                                   # True restores both sides.
HA_MODE = "reversal"                   # what the doji MEANS.
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
EMA_FILTER_TF = "30m"               # TIMEFRAME the filter's EMA is measured
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
LOG_SKIPS = False                  # log every gate refusal. FALSE as of
                                   # 11 Aug: with 71 markets the whole-run
                                   # test refuses dozens per scan and the
                                   # RECENT EVENTS panel became nothing but
                                   # skip lines, burying the entries and the
                                   # errors that matter. The PIPELINE PANEL
                                   # still shows every refusal with its
                                   # reason - that is the place to read them
TREND_MAX_AGE = 3                  # how many bars after the EMA CROSS a setup
                                   # may still be called "the beginning" of
                                   # the trend. His spec, 12 Aug, from AMZN:
                                   # the downtrend crossed at 09:00 and the
                                   # first setup to survive every gate fired
                                   # at 23:00 - FOURTEEN HOURS IN. ONE_PER_TREND
                                   # did not block it because nothing had fired
                                   # yet, so it counted as the first. This is
                                   # the separate rule: too late is too late,
                                   # whether or not anything fired earlier.
                                   # 0 disables the age check
FLIP_NEEDS_NOWICK = False          # must a no-wick candle follow the flip
                                   # before entering? FALSE as of 12 Aug, his
                                   # call: enter at the flip itself. The wick
                                   # test was costing a bar of the move and
                                   # refusing turns outright when the flip
                                   # candle happened to carry a tail. True
                                   # restores the arm-then-trigger sequence
FLIP_TARGET = True                 # book a WIN at HA_RR when the flip engine
                                   # reaches it, instead of parking the target
                                   # and riding to the colour flip. His call
                                   # 16 Aug. A trade that touches 1.5R and
                                   # then gives it all back was booking as a
                                   # LOSS; now it closes there. The flip is
                                   # still the exit for anything that does not
                                   # get that far. False parks the target again
FLIP_EXIT_ON_REGIME = False        # what the flip exit WATCHES. TRUE as of
                                   # 15 Aug, his call: close when price
                                   # crosses the REGIME_TF EMA against the
                                   # trade, not when the 1h HA flips. The HA
                                   # turns many times inside one 4h trend, so
                                   # it cut trades short; the 4h cross is the
                                   # same line that granted permission to
                                   # enter, so the trade lasts as long as the
                                   # reason for taking it. False restores the
                                   # HA-flip exit with its smoothed filter
FLIP_EXIT = False                  # CLOSE ON THE HA FLIP, independently of
                                   # FLIP_MODE. His call 15 Aug. With
                                   # STOP_EXIT False the 1.5R target was the
                                   # ONLY way out, so a losing trade sat open
                                   # forever - the backtests all booked those
                                   # as -1R stop-outs, which the live engine
                                   # never does. This is the exit that ends
                                   # losers. The SMOOTHED series must agree,
                                   # or a single opposite candle closes the
                                   # trade: on HYPE that turned +10.2% into
                                   # -2.0% across the same decline
SHA_FILTER = True                  # SMOOTHED HA AS A FILTER over regular HA,
                                   # his idea 12 Aug. Regular HA still arms,
                                   # enters and exits; the SMOOTHED series
                                   # only has to AGREE. On HYPE the descent
                                   # from 21 Jul was full of single green
                                   # candles that closed a short and cost the
                                   # trend - smoothed HA stays red through
                                   # them, so the trade survives
SHA_ON_ENTRY = True                # does the smoothed series have to agree
                                   # to ENTER? FALSE as of 12 Aug, his call:
                                   # the flip alone opens the trade. Smoothed
                                   # HA is only there to hold the position
                                   # through noise candles and step aside at
                                   # the real turn - an exit filter, not an
                                   # entry one
SHA_IN = 10                        # 17 Aug: was 5,5. Moved to 10,10 to match
SHA_OUT = 10                       # the TradingView charts he actually reads
                                   # ("Smoothed Ha Candles 10 10"). This is a
                                   # SLOWER series: it turns later and holds a
                                   # colour longer, so CROSS_MAX_TREND_AGE
                                   # counts differently than it did at 5,5.
                                   # Also feeds sha_side, so the flip engine's
                                   # SHA_ON_ENTRY and flip exit change with it
                                   # - both currently off under CROSS_MODE.
FLIP_MODE = False                  # THE 4h FLIP ENGINE, his spec 12 Aug.
                                   # ALWAYS IN THE MARKET: an HA colour flip
                                   # ARMS a side, the first no-wick candle in
                                   # that direction ENTERS, and the next
                                   # colour flip EXITS and arms the other way.
                                   # The 50 MA only says whether a trend
                                   # exists at all - a flat market trades
                                   # nothing. Replaces the flip-and-enter
                                   # engine when on
FLIP_FLAT_PCT = 0                  # the 50 MA must have moved this much over
                                   # FLIP_SLOPE_BARS to count as trending.
                                   # Below it the market is sideways and BOTH
                                   # sides are refused - the "avoid flatline"
                                   # half of his spec
FLIP_SLOPE_BARS = 6                # bars the slope is measured over
NOWICK_ONLY = True                 # THE SIMPLEST FORM, his spec 12 Aug: "as
                                   # long as there is a no-wick candle in
                                   # either direction above or below the 50
                                   # EMA it qualifies". No run, no colour
                                   # flip, no fade - just a clean candle on
                                   # the correct side. That also shortens the
                                   # pattern to TWO bars after the cross, so
                                   # TREND_MAX_AGE can actually be met.
                                   # False restores the run-flip-nowick
                                   # sequence
ONE_PER_TREND = True               # ONE ALERT PER TREND. His spec, 11 Aug:
                                   # PUMP's trend began with the first no-wick
                                   # candle above the 50 EMA on 8 Aug 11:00,
                                   # and every later long in that same move is
                                   # noise. A signal is keyed to the EMA CROSS
                                   # that started the current side, so the
                                   # next one cannot fire until price crosses
                                   # back and a NEW trend begins. False
                                   # restores one-per-run
EMA_WHOLE_RUN = True               # the EMA side test applies to the WHOLE
                                   # RUN being faded, not just the entry bar.
                                   # His point, 10 Aug: a SHORT needs an
                                   # uptrend that happened BELOW the 50 EMA -
                                   # a rally that ran ABOVE the line and only
                                   # dipped under it by entry time is a
                                   # different setup. ema_side alone tests one
                                   # candle, so ONDO could short a run that
                                   # spent its life on the wrong side
EMA_RUN_TOL = 0                    # candles inside the run allowed to sit on
                                   # the wrong side. 0 is strict; 1 forgives a
                                   # single poke through the line
EMA_SIDE_RULE = False               # require price to be on the EMA's side.
                                   # OFF as of 6 Aug: the 50 EMA is here for
                                   # TREND CONTEXT ONLY now — its SLOPE says
                                   # whether a trend exists and which way, and
                                   # that is all it is asked. Whether price
                                   # happens to sit above or below the line no
                                   # longer decides anything, and the retest
                                   # band is off with it
EMA_SLOPE_TF = "30m"               # TIMEFRAME the slope is measured on, kept
                                   # SEPARATE from EMA_FILTER_TF on purpose.
                                   # The 50 EMA itself stays on 1h — that was
                                   # his own correction when a 15m EMA read a
                                   # pullback as a trend change — but the
                                   # SLOPE now reads 15m, so it reacts to a
                                   # turn without waiting for the hour to
                                   # register it
EMA_SLOPE_PCT = 0                  # the 1h EMA must have MOVED at least this
                                   # % over EMA_SLOPE_BARS, in the trade's
                                   # direction. In a range the average goes
                                   # FLAT, so both sides get refused - which
                                   # is the point: a reversal engine's worst
                                   # environment is chop, where every push to
                                   # the top of the range looks like a fading
                                   # uptrend and flips straight back. 0 off
EMA_SLOPE_BARS = 12                # how far back to measure that slope, in
                                   # EMA_FILTER_TF bars. 12 = half a day on 1h
EMA_TREND_CLEAR_PCT = 1.00         # slope above which the trend is CLEAR and
                                   # the retest band NO LONGER APPLIES. His
                                   # point: 0.75% is a PULLBACK rule - it
                                   # says "wait for price to come back to the
                                   # average". In a decisive trend price does
                                   # not come back, so demanding it there
                                   # refuses the best moves for being too
                                   # good. Measured on the same slope as
                                   # EMA_SLOPE_PCT, so the two form a band:
                                   # under EMA_SLOPE_PCT nothing trades (a
                                   # range), between them a retest is
                                   # required, above this the side alone is
                                   # enough. 0 disables, restoring "always
                                   # require the retest"
EMA_RETEST_PCT = 0                 # entry must be within this % of the EMA -
                                   # a RETEST, not just the right side of it.
                                   # Turns the filter from "shorts anywhere
                                   # below the EMA" into "shorts where price
                                   # has rallied back INTO the EMA and
                                   # failed". The existing pattern already
                                   # fits: that rally is a green run, and its
                                   # flip at the EMA is the short trigger.
                                   # 0 disables, restoring side-only
EMA_FILTER_LEN = 20                # trend filter on the REAL closes. Only
                                   # SHORT while price is BELOW this EMA and
                                   # only LONG while it is above, so the
                                   # reversal is never taken against the
                                   # bigger trend. Measured on the last
                                   # CLOSED candle - the no-wick bar - since
                                   # the entry bar is still forming when the
                                   # signal is read. 0 disables the filter
HA_FADE_BARS = 0                   # trailing HA bodies that must SHRINK
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
ENTRY_AT_OPEN = False              # 17 Aug: FALSE. Was True, which recorded
                                   # the entry at the OPEN of the CURRENT 15m
                                   # bar. This engine scans every 5 min and
                                   # fires mid-bar, so that open could be ten
                                   # minutes and a large move stale - CXMT was
                                   # booked at 8.7939 with the market at
                                   # 8.6366, and its stop/target/R were all
                                   # computed off a price you could not get.
                                   # False uses the forming bar's close, i.e.
                                   # the live price.
HA_MIN_RUN_PCT = 0                 # MINIMUM MOVE across the run, start to
                                   # flip, as a % of price. HA_MIN_RUN counts
                                   # CANDLES, so eight bars drifting sideways
                                   # scored the same as eight falling hard -
                                   # and only the second is a trend worth
                                   # fading. HA_MIN_BODY_PCT does not cover
                                   # this: it only asks that ONE body in the
                                   # run clears a floor, not that the run
                                   # went anywhere. 0 disables
HA_MIN_RUN = 0                     # MINIMUM trend-coloured HA candles before
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
TP_SOURCE = "rr"                   # "swing" = target the PREVIOUS SWING on
                                   # REAL candles - the last pivot beyond
                                   # entry in the trade's direction. "rr"
                                   # restores HA_RR x the stop. His spec,
                                   # 6 Aug: structure decides the target
                                   # instead of a fixed multiple
SWING_PIVOT_BARS = 3               # bars either side that a candle must
                                   # exceed to count as a pivot
SWING_MAX_LOOKBACK = 96            # how far back to hunt for one (24h on 15m)
TP_MIN_RR = 1.0                    # refuse the setup when the swing sits
                                   # closer than the stop - paying 1 to make
                                   # less than 1 is a losing shape however
                                   # often it wins
RETARGET_ON = False                # 17 Aug: OFF at his call - not part of the
                                   # stoch-doji spec. The target now simply
                                   # closes the trade at 1.5R.
                                   # ROLL THE TRADE AT THE TARGET. His spec
                                   # 13 Aug: when the 1.5R target is reached,
                                   # book it and IMMEDIATELY reopen at that
                                   # same price with a fresh 1.5R target. The
                                   # ladder keeps riding a move that is still
                                   # going instead of ending at the first
                                   # target. Risk distance carries over, so
                                   # each rung is the same size as the first
RETARGET_MAX = 0                   # how many rolls before it stops. 0 = no
                                   # limit; the ladder ends when a stop or a
                                   # flip ends it
HA_RR = 1.5                        # first target = 3x the stop distance
TRAIL_ON = False                   # TRAILING STOP. Ratchets ONLY - it never
                                   # moves against the trade, so it can turn a
                                   # winner into a smaller winner but never
                                   # widens risk on a loser
TRAIL_START_R = 0.75               # profit, in R, before the trail engages.
                                   # Below this the original structural stop
                                   # stands: tightening early just turns
                                   # ordinary noise into a stop-out
TRAIL_GAP_R = 0.75                 # how far behind the BEST price reached the
                                   # stop sits, in R. At 0.75 the first
                                   # ratchet lands on entry, so the trade goes
                                   # risk-free the moment it is 0.75R up, then
                                   # follows from there
HA_PARTIAL = 1.0                   # fraction booked there; the stop then moves
                                   # to entry and the remainder is held until
                                   # the HA flips against the trade
ADOPT_ORPHANS = "off"              # a position on the exchange with NO
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
STOP_EXIT = True                   # 17 Aug: back ON. Off since 11 Aug, which
                                   # meant NO losing exit at all - and made
                                   # every ATR stop, close-confirm and
                                   # disaster-stop path dead code.
                                   # does the stop LEVEL close the trade?
                                   # FALSE as of 11 Aug, his call. The level
                                   # is still COMPUTED - sizing needs the risk
                                   # distance and the target is HA_RR x it -
                                   # but price reaching it no longer exits,
                                   # and no protective order is placed live.
                                   # WITH HA_PARTIAL AT 1.0 THERE IS NO OTHER
                                   # LOSING EXIT: a trade that goes wrong
                                   # stays open until the target or forever.
                                   # True restores the stop
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
STOP_LOOKBACK = 6                  # 17 Aug: 12 -> 6. A SIX-HOUR swing stop,
                                   # his call, for the 15m stoch-doji engine.
                                   # At 12 on 15m bars this sliced 48 candles
                                   # back, so R was a half-day range and 1.5R
                                   # sat a long way from a 15m reversal entry.
                                   # FLOOR on the stop window, COUNTED IN
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
MIN_TARGET_PCT = 0.5               # 17 Aug: 1.5 -> 0.5. MEASURED, not guessed:
                                   # on 15m BTC a 2x ATR stop gives R = 0.474%
                                   # of price, so 1.5R lands 0.711% away. Even
                                   # 4x ATR only reaches 1.42%. A 1.5% floor
                                   # is simply incompatible with 15m trading -
                                   # it was set when the engine ran 1h/30m
                                   # bars and a 12h swing stop, and it was
                                   # rejecting BTC, LINK and NEAR on 17 Aug.
                                   # 0.5% keeps a real fee backstop without
                                   # vetoing the whole timeframe.
                                   # the TARGET must sit at least this far
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
MIN_STOP_PCT = 0.10                # skip entries whose stop sits closer than
                                   # this % of price - sub-noise stops just
                                   # churn. 0.25 -> 0.10 on 15 Aug: the EMA 20
                                   # switch made runs shorter and their stops
                                   # tighter, so the floor started refusing
                                   # ordinary setups - xyz:MSFT was turned
                                   # away at 0.246%, four thousandths under.
                                   # It also never appeared in the EMA 20 vs
                                   # 50 replay, so that +144R counted trades
                                   # the live engine was rejecting
TRACK_UNPLACED = True              # keep tracking a trade whose order never
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
EXEC_LIVE = False                   # place real orders. Back ON 4 Aug after a
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
EXEC_MARGIN_USD = 150.0             # collateral per trade in "margin" mode
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
# 20 Aug: 30m raised 400 -> 1500 so a 30-day band has data to work with.
# That is ~3.7x the candles per market. Fetches are per NEW BAR here, not
# per pulse, so the rate-limit exposure is unchanged - but memory is not:
# the agent held a 25MB peak at 400 bars.
LOOKBACK = {"5m": 300, "10m": 300, "15m": 400, "30m": 1500, "1h": 500,
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
REPLAY_CANDLES = 0                 # candles replayed per run. 3 -> 0 on 12 Aug:
                                   # the replay existed to cover a missed
                                   # scan, but it also meant a setup found on
                                   # an ALREADY-CLOSED bar was entered at that
                                   # bar's open - a price hours in the past.
                                   # VVV booked 11.667 while the market was at
                                   # 11.823, turning a 2.7% stop into 4.0%.
                                   # At 0 only the FORMING bar is evaluated,
                                   # so the alerted price is always live.
                                   # The cost: a scan missed entirely loses
                                   # that candle's setups rather than catching
                                   # them late


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


def engine_label():
    """What the alerts should call the running engine."""
    if IM_MODE:
        return f"Impulse MACD {IM_LEN},{IM_SIG}"
    if RS_MODE:
        return f"Reversal {RS_TREND_LEN} {RS_MA.upper()}"
    if SD_MODE:
        return "Stoch-doji"
    if CROSS_MODE:
        return f"EMA{EMA_FILTER_LEN} cross"
    if FLIP_MODE:
        return "HA flip"
    return ha_label()


def ha_label():
    """What the alerts should call the series. At 1,1 there is no smoothing
    at all, and calling it "smoothed HA" misdescribes the engine."""
    return ("Heikin Ashi" if HA_SMOOTH_IN <= 1 and HA_SMOOTH_OUT <= 1
            else f"smoothed HA {HA_SMOOTH_IN},{HA_SMOOTH_OUT}")


def btc_bias():
    """+1 if BTC is trending up, -1 down, 0 if it is not trending at all.

    Uses ema_slope on BTC's own candles - the same measure ema_side applies
    per symbol - so "trending" means one thing everywhere. Cached like
    btc_trend so a 39-symbol scan costs ONE extra fetch, and fails soft to
    0 (neutral, blocks nothing) rather than refusing the whole scan."""
    if not BTC_ALIGN or not BTC_ALIGN_PCT:
        return 0
    now = time.time()
    if _BTC_BIAS["t"] and now - _BTC_BIAS["t"] < BTC_TREND_TTL_S:
        return _BTC_BIAS["v"]
    bias = 0
    try:
        _, cs = fetch({"symbol": "BTC", "hl_coin": "BTC", "fallbacks": [],
                       "cls": "crypto"}, TF, LOOKBACK.get(TF, 400))
        if cs:
            sl = ema_slope(cs, len(cs) - 1)
            if sl is not None:
                bias = 1 if sl >= BTC_ALIGN_PCT else (
                    -1 if sl <= -BTC_ALIGN_PCT else 0)
    except Exception as e:
        log(f"btc_bias() failed: {type(e).__name__}: {e} - treating BTC as "
            "neutral")
        bias = 0
    _BTC_BIAS.update(t=now, v=bias)
    return bias


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


def trend_start_t(candles, i):
    """Timestamp of the EMA cross that began the side price is on NOW.

    Walks back from i to the last bar where the side differs, and returns the
    candle AFTER it - the first bar of the current trend. That timestamp is
    the key: one signal per trend, however many setups the trend prints."""
    side = ema_cross_side(candles, i)
    if side is None:
        return None
    k = i
    while k > 1:
        prev = ema_cross_side(candles, k - 1)
        if prev is None or prev != side:
            return candles[k]["t"]
        k -= 1
    return candles[max(0, k)]["t"]


def ema_run_side(candles, start, upto, want_long):
    """Did EVERY candle from `start` to `upto` sit on the trade's side of the
    EMA? Returns (ok, offenders).

    ema_side answers "is price on the right side NOW". This answers "was the
    run we are fading on the right side THE WHOLE TIME", which is what makes
    a short a fade of a rally BELOW the average rather than one that has just
    crossed under it."""
    if not EMA_WHOLE_RUN or not EMA_FILTER_LEN:
        return True, 0
    base = candles[:max(0, upto) + 1]
    factor = max(1, MS.get(EMA_FILTER_TF, MS[TF]) // MS[TF])
    higher = resample(base, factor) if factor > 1 else base
    if len(higher) < EMA_FILTER_LEN + 2:
        return True, 0                      # too little history - fail open
    series = ema([c["c"] for c in higher], EMA_FILTER_LEN)
    off = len(higher) - len(series)
    bad = 0
    for k in range(max(0, start), max(0, upto) + 1):
        hk = (k // factor) - off
        if hk < 0 or hk >= len(series):
            continue
        e = series[hk]
        if (candles[k]["c"] > e) != want_long:
            bad += 1
    return bad <= EMA_RUN_TOL, bad


def ema_slope(candles, i):
    """How far the EMA has travelled over EMA_SLOPE_BARS, as a % of itself.
    None when there is not enough history. Positive means rising."""
    if not EMA_FILTER_LEN or i < 1:
        return None
    base = candles[:max(0, i - 1) + 1]
    factor = max(1, MS.get(EMA_SLOPE_TF, MS[TF]) // MS[TF])
    higher = resample(base, factor)
    if len(higher) < EMA_FILTER_LEN + EMA_SLOPE_BARS + 1:
        return None
    series = ema([c["c"] for c in higher], EMA_FILTER_LEN)
    then = series[-1 - EMA_SLOPE_BARS]
    return (series[-1] - then) / then * 100 if then else None


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
    series = ema([c["c"] for c in higher], EMA_FILTER_LEN)
    e, px = series[-1], base[-1]["c"]
    if EMA_SIDE_RULE and not ((px > e) if want_long else (px < e)):
        return False, e, px
    # IS THE AVERAGE ACTUALLY GOING ANYWHERE? A flat EMA means a range, and
    # a reversal taken inside a range is a coin flip: both edges look like
    # fading trends and both flip back.
    # ONE slope implementation, shared with the panel. It reads EMA_SLOPE_TF,
    # which is NOT the timeframe this EMA sits on - inlining it here again
    # would silently keep measuring the slope on EMA_FILTER_TF.
    raw = ema_slope(candles, i)
    with_trend = 0.0
    if raw is not None:
        with_trend = raw if want_long else -raw
        if EMA_SLOPE_PCT and with_trend < EMA_SLOPE_PCT:
            return False, e, px          # flat - a range, no trade either way
    # A CLEAR TREND DOES NOT NEED A RETEST. The band is a pullback rule; in
    # a decisive move price never returns to the average, so applying it
    # there refuses the strongest setups for being too strong.
    if EMA_TREND_CLEAR_PCT and with_trend >= EMA_TREND_CLEAR_PCT:
        return True, e, px
    if not EMA_RETEST_PCT:
        return True, e, px
    # RETEST: price has to be back AT the EMA, not merely on its side. A
    # short 6% under the average is chasing a move that already happened;
    # a short 0.3% under it is selling the failed pullback.
    near = abs(px - e) / e * 100 if e else 999
    return near <= EMA_RETEST_PCT, e, px


def find_swing(candles, before, want_long):
    """The most recent PIVOT on real candles before index `before`.

    A pivot high is a candle whose high exceeds every high within
    SWING_PIVOT_BARS either side; a pivot low mirrors it. A LONG targets the
    previous swing HIGH, a SHORT the previous swing LOW - the level price
    last turned at, which is where it is most likely to stall again."""
    p = max(1, SWING_PIVOT_BARS)
    lo = max(p, before - SWING_MAX_LOOKBACK)
    for k in range(before - p - 1, lo - 1, -1):
        window = candles[k - p:k + p + 1]
        if len(window) < 2 * p + 1:
            continue
        if want_long:
            if candles[k]["h"] >= max(x["h"] for x in window):
                return candles[k]["h"], k
        else:
            if candles[k]["l"] <= min(x["l"] for x in window):
                return candles[k]["l"], k
    return None, None


def breakout_signal(candles, i):
    """Did candle i CLOSE beyond a tight range? Returns (want_long, stop).

    The range is the BO_LOOKBACK bars BEFORE i, so the breakout bar never
    contributes to the level it is breaking. The stop is the far edge - the
    level that says the break failed - and the range must be tight, because
    a wide range is not a coil, it is just noise with two edges."""
    # The range is built on BO_TF, offset so the last whole group ends at the
    # bar BEFORE i - an unaligned grouping would shift every level.
    factor = max(1, MS.get(BO_TF, MS[TF]) // MS[TF])
    pre = candles[i % factor:i] if factor > 1 else candles[:i]
    window = resample(pre, factor)[-BO_LOOKBACK:] if factor > 1 \
        else candles[max(0, i - BO_LOOKBACK):i]
    if len(window) < BO_LOOKBACK:
        return None
    hi = max(x["h"] for x in window)
    lo = min(x["l"] for x in window)
    px = candles[i]["c"]
    if px <= 0 or hi <= lo:
        return None
    if BO_TIGHT_PCT and (hi - lo) / px * 100 > BO_TIGHT_PCT:
        return None
    buf = px * (BO_BUFFER_PCT / 100.0)
    if px > hi + buf:
        return True, lo
    if px < lo - buf:
        return False, hi
    return None


def long_trend_ok(asset, want_long):
    """Refuse a LONG while the 50 EMA is below where it was 30 days ago.

    Uses the 4h series because 1h cannot reach back far enough. Fails OPEN -
    a read it cannot make must not veto every trade."""
    if not LONG_LOOKBACK_DAYS or not want_long:
        return True, 0.0
    sym = asset["symbol"]
    now = time.time()
    hit = _LTREND.get(sym)
    if hit and now - hit[0] < REGIME_TTL_S:
        return hit[1], hit[2]
    ok, chg = True, 0.0
    try:
        bars = int(LONG_LOOKBACK_DAYS * 24 * 3600000 / MS["4h"])
        _, cs = fetch(asset, "4h", LOOKBACK.get("4h", 400))
        if not cs or len(cs) < EMA_FILTER_LEN + bars:
            _, h1 = fetch(asset, "1h", LOOKBACK.get("1h", 500))
            cs = resample(h1, 4) if h1 else None
        if cs and len(cs) >= EMA_FILTER_LEN + bars:
            e = ema([c["c"] for c in cs], EMA_FILTER_LEN)
            if len(e) > bars and e[-1 - bars]:
                chg = (e[-1] - e[-1 - bars]) / e[-1 - bars] * 100
                ok = chg >= 0
    except Exception as ex:
        log(f"{sym}: 30-day trend read failed ({type(ex).__name__}) - "
            "not gating")
        ok, chg = True, 0.0
    _LTREND[sym] = (now, ok, chg)
    return ok, chg


def regime_side(asset):
    """+1 if price is above the higher-timeframe EMA, -1 below, 0 unknown.

    Fetches REGIME_TF directly; if the venue will not serve that interval it
    resamples 1h instead, so the gate never silently vanishes. Fails to 0,
    which BLOCKS NOTHING - a filter that cannot read its data must not veto
    every trade."""
    sym = asset["symbol"]
    now = time.time()
    hit = _REGIME.get(sym)
    if hit and now - hit[0] < REGIME_TTL_S:
        return hit[1]
    side = 0
    try:
        _, cs = fetch(asset, REGIME_TF, LOOKBACK.get(REGIME_TF, 400))
        if not cs or len(cs) < REGIME_EMA_LEN + 2:
            # the venue would not serve REGIME_TF - build it from 1h
            _, h1 = fetch(asset, "1h", LOOKBACK.get("1h", 400))
            factor = max(1, MS.get(REGIME_TF, MS["1h"]) // MS["1h"])
            cs = resample(h1, factor) if h1 else None
        if cs and len(cs) >= REGIME_EMA_LEN + REGIME_SLOPE_BARS + 2:
            series = ema([c["c"] for c in cs], REGIME_EMA_LEN)
            e = series[-1]
            px = cs[-1]["c"]
            side = 1 if px > e else -1
            # POSITION IS NOT A TREND. Price can sit below a RISING EMA in a
            # pullback, which used to permit a short into an uptrend. The
            # line has to be moving the same way before the side counts.
            if REGIME_SLOPE_BARS and len(series) > REGIME_SLOPE_BARS:
                then = series[-1 - REGIME_SLOPE_BARS]
                if then:
                    chg = (e - then) / then * 100
                    if abs(chg) < REGIME_SLOPE_PCT or (chg > 0) != (side > 0):
                        side = 0        # flat, or disagreeing - permit both
    except Exception as ex:
        log(f"{sym}: regime read failed ({type(ex).__name__}) - not gating")
        side = 0
    _REGIME[sym] = (now, side)
    return side


def sha_side(candles, i):
    """The SMOOTHED HA colour at bar i: True green, False red, None unknown.

    Built at SHA_IN/SHA_OUT, not the signal smoothing, so it is a genuinely
    slower read of the same candles - which is the point of using it as a
    filter rather than as the signal."""
    if not SHA_FILTER or i < 1:
        return None
    sha = smoothed_ha(candles[:i + 1], SHA_IN, SHA_OUT)
    if not sha:
        return None
    return ha_green(sha[-1])


def update_flip_arm(ast, ha, i):
    """Record an HA colour flip even while a trade is open. Bookkeeping only
    - this never enters.

    process_candle is unreachable while ast["trade"] is set: the scan returns
    at the IN_TRADE branch. A flip is only detectable on the ONE bar after it
    (ha[i-1] vs ha[i-2]), so a flip that happened DURING a trade was never
    armed. The exit lags the flip whenever the smoothed series has to agree,
    so by the time the trade closes the flip is two bars back, both bars read
    the same colour, and the reversal is lost outright. XMR 16 Aug: flipped
    green 20:00, short exited 21:00, no long. Same on BRENTOIL.
    """
    if i < 2:
        return
    now_green = ha_green(ha[i - 1])
    if now_green == ha_green(ha[i - 2]):
        return
    if (ast.get("flip_arm") or {}).get("t") == ha[i - 1]["t"]:
        return                                  # already armed on this bar
    ast["flip_arm"] = {"side": "LONG" if now_green else "SHORT",
                       "t": ha[i - 1]["t"]}


def flip_signal(ast, candles, ha, i):
    """The 4h flip engine. Returns "LONG" / "SHORT" to ENTER, else None.

    Two steps kept deliberately apart, as in cross mode: an HA COLOUR FLIP
    arms a side, and the first NO-WICK candle in that direction pulls the
    trigger. Arming lives on the state record so a flip on one bar can still
    fire several bars later."""
    if i < 2:
        return None
    now_green = ha_green(ha[i - 1])
    prev_green = ha_green(ha[i - 2])
    arm = ast.get("flip_arm") or {}

    if now_green != prev_green:                 # a COLOUR FLIP just closed
        arm = {"side": "LONG" if now_green else "SHORT", "t": ha[i - 1]["t"]}
        ast["flip_arm"] = arm
        # WITHOUT the wick test the flip IS the trigger - there is nothing
        # left to wait for, so fire on this bar rather than the next.
        if not FLIP_NEEDS_NOWICK:
            if SHA_ON_ENTRY:
                sh = sha_side(candles, i - 1)
                if sh is not None and sh != now_green:
                    # this used to return silently, which is why BRENTOIL and
                    # XMR both took an hour to explain
                    log(f"{ast.get('sym', '?')}: HA flipped "
                        f"{'green' if now_green else 'red'} but the smoothed "
                        f"{SHA_IN},{SHA_OUT} is still "
                        f"{'red' if now_green else 'green'} - armed "
                        f"{arm['side']}, entry held until it agrees")
                    return None
            log(f"{ast.get('sym', '?')}: HA flipped "
                f"{'green' if now_green else 'red'} - entering "
                f"{arm['side']} at the next open")
            return arm["side"]
        log(f"{ast.get('sym', '?')}: HA flipped "
            f"{'green' if now_green else 'red'} - arming {arm['side']}, "
            "waiting for a no-wick candle")
        return None

    if not arm:
        return None
    want_long = arm["side"] == "LONG"
    if ha[i - 1]["t"] <= arm["t"]:              # must come AFTER the flip
        return None
    if ha_green(ha[i - 1]) != want_long:
        return None
    if FLIP_NEEDS_NOWICK and not no_wick(ha[i - 1], want_long):
        return None
    if SHA_ON_ENTRY:
        sh = sha_side(candles, i - 1)
        if sh is not None and sh != want_long:
            return None
    return arm["side"]


def flip_trending(candles, i):
    """Is the 50 MA actually going somewhere? +1 up, -1 down, 0 flat.

    The spec asks the MA to say "upward or downward trend, avoid flatline
    sideways" - so this answers direction AND flatness in one number, and 0
    refuses both sides."""
    if not FLIP_FLAT_PCT:
        return 1
    base = candles[:max(0, i) + 1]
    if len(base) < EMA_FILTER_LEN + FLIP_SLOPE_BARS + 1:
        return 0
    e = ema([c["c"] for c in base], EMA_FILTER_LEN)
    if len(e) <= FLIP_SLOPE_BARS:
        return 0
    then = e[-1 - FLIP_SLOPE_BARS]
    if not then:
        return 0
    slope = (e[-1] - then) / then * 100
    if slope >= FLIP_FLAT_PCT:
        return 1
    if slope <= -FLIP_FLAT_PCT:
        return -1
    return 0


def ema_cross_side(candles, i):
    """Which side of the 50 EMA is price on at bar i? +1 above, -1 below.

    Uses the same EMA the filter uses, so "above the EMA" means one thing
    everywhere. None when there is not enough history."""
    if i < 1:
        return None
    base = candles[:i + 1]
    factor = max(1, MS.get(EMA_FILTER_TF, MS[TF]) // MS[TF])
    higher = resample(base, factor)
    if len(higher) < EMA_FILTER_LEN:
        return None
    e = ema([c["c"] for c in higher], EMA_FILTER_LEN)[-1]
    px = base[-1]["c"]
    return 1 if px > e else -1


def sha_run_len(candles, i, max_look=300):
    """(side, bars): the SMOOTHED HA colour at bar i and how many consecutive
    bars it has held that colour. side True green / False red / None unknown.
    """
    if i < 1:
        return None, 0
    sha = smoothed_ha(candles[:i + 1], SHA_IN, SHA_OUT)
    if not sha:
        return None, 0
    side = ha_green(sha[-1])
    n = 1
    for k in range(len(sha) - 2, max(-1, len(sha) - 2 - max_look), -1):
        if ha_green(sha[k]) != side:
            break
        n += 1
    return side, n


def cross_age_ok(candles, i, want_long):
    """(allowed, reason). Refuse a cross into a trend that is already running.

    Judged on the LAST CLOSED bar, like the slope, because the forming bar's
    smoothed HA repaints.
    """
    if not CROSS_MAX_TREND_AGE:
        return True, "age gate off"
    side, bars = sha_run_len(candles, i - 1)
    if side is None:
        return True, "no smoothed HA read"
    colour = "green" if side else "red"
    if side == want_long and bars > CROSS_MAX_TREND_AGE:
        return False, (f"trend already underway - smoothed HA {colour} for "
                       f"{bars} bars, limit is {CROSS_MAX_TREND_AGE}")
    return True, (f"smoothed HA {colour} {bars} bar"
                  + ("s" if bars != 1 else ""))


def zlema(vals, n):
    """Zero-lag EMA, LazyBear's form: ema1 + (ema1 - ema2)."""
    if not vals or n <= 0:
        return []
    e1 = ema(vals, n)
    e2 = ema(e1, n)
    return [a + (a - b) for a, b in zip(e1, e2)]


def smma_series(vals, n):
    """SMMA as a SERIES - seeded on the SMA of the first n, then Wilder."""
    if n <= 0 or len(vals) < n:
        return []
    out = [None] * (n - 1)
    s = sum(vals[:n]) / float(n)
    out.append(s)
    for v in vals[n:]:
        s = (s * (n - 1) + v) / float(n)
        out.append(s)
    return out


def impulse_macd(candles, n=None, sig=None):
    """LazyBear's Impulse MACD. Returns (md, sb, sh) as full series.

        hi = SMMA(high, n)        lo = SMMA(low, n)
        mi = ZLEMA(hlc3, n)
        md = mi>hi ? mi-hi : (mi<lo ? mi-lo : 0)     <- ZERO inside the band
        sb = SMA(md, sig)                            <- signal line
        sh = md - sb                                 <- histogram

    md is exactly 0 whenever the zero-lag average sits INSIDE the high/low
    band, which is the indicator's own definition of a flat market - that is
    what pathway 2 waits for.
    """
    n = IM_LEN if n is None else n
    sig = IM_SIG if sig is None else sig
    if not candles or len(candles) < n + sig + 2:
        return [], [], []          # a FAILED FETCH hands us None - three
                                   # diagnostics died on len(None) on 21 Aug
    hi = smma_series([c["h"] for c in candles], n)
    lo = smma_series([c["l"] for c in candles], n)
    mi = zlema([(c["h"] + c["l"] + c["c"]) / 3.0 for c in candles], n)
    md = []
    for k in range(len(candles)):
        h, l, m = hi[k], lo[k], mi[k]
        if h is None or l is None or m is None:
            md.append(0.0)
        elif m > h:
            md.append(m - h)
        elif m < l:
            md.append(m - l)
        else:
            md.append(0.0)
    sb = []
    for k in range(len(md)):
        if k + 1 < sig:
            sb.append(0.0)
        else:
            sb.append(sum(md[k - sig + 1:k + 1]) / float(sig))
    return md, sb, [a - b for a, b in zip(md, sb)]


def zlema_unused(vals, n):
    return zlema(vals, n)


def sma(vals, n):
    """Simple moving average of the last n values, or None."""
    if n <= 0 or len(vals) < n:
        return None
    return sum(vals[-n:]) / float(n)


def smma(vals, n):
    """Smoothed / Wilder moving average.

    Seeded with the SMA of the first n values, then
        smma = (prev * (n - 1) + value) / n
    which is an EMA with alpha = 1/n - roughly an EMA(2n-1), so an SMMA(20)
    is far slower than an SMA(20). Fewer crossings, later arms.
    """
    if n <= 0 or len(vals) < n:
        return None
    s = sum(vals[:n]) / float(n)
    for v in vals[n:]:
        s = (s * (n - 1) + v) / float(n)
    return s


def rs_ma(closes, n):
    """The engine's moving average - SMMA, SMA or EMA per RS_MA."""
    if RS_MA == "ema":
        e = ema(closes, n)
        return e[-1] if e and len(closes) >= n else None
    if RS_MA == "sma":
        return sma(closes, n)
    return smma(closes, n)



    """The overbought level. The oversold line is its negative."""
    if IM_BAND_MODE == "abs":
        return abs(IM_BAND)
    if IM_BAND_MODE == "pct_of_price":
        return None                       # resolved by the caller, needs price
    span = MS.get(TF, 0)
    bars = (IM_BAND_LOOKBACK if IM_BAND_LOOKBACK
            else (int(IM_BAND_DAYS * 86_400_000 / span) if span else 300))
    lo = max(0, i - bars)
    hist = sorted(abs(x) for x in md[lo:i + 1] if x)
    if len(hist) < 20:
        return None
    k = min(len(hist) - 1, int(len(hist) * IM_BAND_PCTILE / 100.0))
    return hist[k]


def im_gate_status(ast, candles, i, sym=None):
    """Watchlist row for the impulse engine. Report only.

      coiled   md has been FLAT long enough - pathway 2 is armed and waiting
               for the first push
      stretched  md is past the band - a crossover here would be MAJOR, so
               pathway 1 would take it
    Symbols in neither state return None, so the panel stays short.
    """
    last = i - 1
    if last < IM_LEN + IM_SIG + IM_FLAT_BARS + 5:
        return None
    md, sb, sh = impulse_macd(candles[:last + 1])
    if not md or len(md) < IM_FLAT_BARS + 3:
        return None
    j = len(md) - 1
    px = candles[last]["c"]

    if IM_P2_ON:
        run = 0
        for k in range(j, -1, -1):
            if md[k] != 0.0:
                break
            run += 1
        if run >= IM_FLAT_BARS:
            return {"sym": sym, "dir": "", "stage": "ready", "run": run,
                    "trend": "coiled", "age": 0,
                    "detail": (f"impulse MACD flat {run} bars - the first "
                               f"push with the candle agreeing enters")}

    if IM_P1_ON:
        band = im_band(md, j)
        if band is None and IM_BAND_MODE == "pct_of_price":
            band = abs(px) * IM_BAND / 100.0
        if band and abs(md[j]) > band:
            over = md[j] > 0
            gap = abs(md[j] - sb[j])
            return {"sym": sym, "dir": "SHORT" if over else "LONG",
                    "stage": "waiting", "run": 0,
                    "trend": "overbought" if over else "oversold",
                    "age": 0,
                    "detail": (f"md {md[j]:.4g} past the "
                               f"{'+' if over else '-'}{band:.4g} line - a "
                               f"cross {'down' if over else 'up'} through the "
                               f"signal enters ({gap:.4g} away)")}
    return None


IM_PATH = {}                       # sym -> which pathway fired, so the alert
                                   # can say whether the bands were even part
                                   # of the decision
IM_LEVELS = {}                     # sym -> (md, sb, band) at the signal bar,
                                   # so the alert can print where the impulse
                                   # line sat against its own bands


def im_band(md, i):
    """The overbought level; the oversold line is its negative.

    Under "percentile" this is a cut through THIS symbol's own recent |md|,
    which is his major/minor split: readings past the line are the crossovers
    far from the middle. md is in price units, so a fixed number could not
    serve BTC and kPEPE at once.
    """
    if IM_BAND_MODE == "abs":
        return abs(IM_BAND)
    if IM_BAND_MODE == "pct_of_price":
        return None                       # caller resolves it, needs price
    span = MS.get(TF, 0)
    bars = (IM_BAND_LOOKBACK if IM_BAND_LOOKBACK
            else (int(IM_BAND_DAYS * 86_400_000 / span) if span else 300))
    lo = max(0, i - bars)
    hist = sorted(abs(x) for x in md[lo:i + 1] if x)
    if len(hist) < 20:
        return None
    k = min(len(hist) - 1, int(len(hist) * IM_BAND_PCTILE / 100.0))
    return hist[k]


def im_signal(ast, candles, i):
    """Impulse MACD, both pathways. Returns "LONG"/"SHORT" to ENTER, else None.

    Judged on the LAST CLOSED bar (i-1) - every rule is about a crossover of
    closed values, so nothing repaints.
    """
    last = i - 1
    if last < IM_LEN + IM_SIG + IM_FLAT_BARS + 5:
        return None
    md, sb, sh = impulse_macd(candles[:last + 1])
    if not md or len(md) < IM_FLAT_BARS + 3:
        return None
    j = len(md) - 1
    px = candles[last]["c"]
    _b = im_band(md, j)
    if _b is None and IM_BAND_MODE == "pct_of_price":
        _b = abs(px) * IM_BAND / 100.0
    IM_LEVELS[ast.get("sym", "?")] = (md[j], sb[j], _b, sh[j])

    # ---------------- PATHWAY 1: extension ----------------
    if IM_P1_ON:
        band = im_band(md, j)
        if band is None and IM_BAND_MODE == "pct_of_price":
            band = abs(px) * IM_BAND / 100.0
        if band:
            up = md[j - 1] <= sb[j - 1] and md[j] > sb[j]
            dn = md[j - 1] >= sb[j - 1] and md[j] < sb[j]
            # the md LINE must be heading the trade's way, not just crossing
            if IM_REQUIRE_TURN:
                n = max(1, IM_TURN_BARS)
                if j - n < 0:
                    return None
                move = md[j] - md[j - n]
                if IM_TURN_REF == "md":
                    ref = abs(md[j]) or abs(band)
                    need = IM_TURN_MIN * ref
                else:
                    need = IM_TURN_MIN_BAND * band
                if up and move <= need:
                    ast["im_note"] = (f"cross up but md only moved "
                                      f"{move:+.3e} over {n} bars "
                                      f"({abs(move) / (abs(md[j]) or 1):.3f} "
                                      f"of |md|), needs {need:+.3e} - "
                                      f"sideways, skipped")
                    up = False
                if dn and move >= -need:
                    ast["im_note"] = (f"cross down but md only moved "
                                      f"{move:+.3e} over {n} bars "
                                      f"({abs(move) / (abs(md[j]) or 1):.3f} "
                                      f"of |md|), needs {-need:+.3e} - "
                                      f"sideways, skipped")
                    dn = False
            if up and md[j] < -band:
                ast["im_path"] = "extension"
                ast["im_why"] = (f"impulse MACD crossed UP at {md[j]:.6g}, "
                                 f"below the oversold line {-band:.6g} "
                                 f"(major crossover)")
                return "LONG"
            if dn and md[j] > band:
                ast["im_path"] = "extension"
                ast["im_why"] = (f"impulse MACD crossed DOWN at {md[j]:.6g}, "
                                 f"above the overbought line {band:.6g} "
                                 f"(major crossover)")
                return "SHORT"
            if up or dn:
                ast["im_note"] = (f"minor crossover at {md[j]:.6g}, inside "
                                  f"+/-{band:.6g} - ignored")

    # ---------------- PATHWAY 2: breakout out of a flat ----------------
    if IM_P2_ON:
        flat = md[j - IM_FLAT_BARS:j]
        if flat and all(x == 0.0 for x in flat) and md[j] != 0.0:
            rising = sh[j] > sh[j - 1]
            up_px = candles[last]["c"] > candles[last]["o"]
            if md[j] > 0 and rising and up_px:
                ast["im_path"] = "breakout"
                ast["im_why"] = (f"impulse MACD flat {IM_FLAT_BARS} bars then "
                                 f"pushed to {md[j]:.6g}, histogram rising, "
                                 f"candle up")
                return "LONG"
            if md[j] < 0 and (not rising) and (not up_px):
                ast["im_path"] = "breakout"
                ast["im_why"] = (f"impulse MACD flat {IM_FLAT_BARS} bars then "
                                 f"pushed to {md[j]:.6g}, histogram falling, "
                                 f"candle down")
                return "SHORT"
    return None


def rs_gate_status(ast, candles, i, sym=None):
    """Watchlist row: symbols whose stack QUALIFIES and are waiting for a 20
    cross. Report only - it never gates anything."""
    last = i - 1
    if last < RS_TREND_LEN + RS_SLOPE_BARS + 2:
        return None
    closes = [c["c"] for c in candles[:last + 1]]
    state, direction, why = rs_stack(closes)
    if state is None:
        return None
    m20 = rs_ma(closes, RS_ARM_LEN)
    m50 = rs_ma(closes, RS_TRIGGER_LEN)
    m200 = rs_ma(closes, RS_TREND_LEN)
    px = candles[i]["c"] if RS_INTRABAR else closes[-1]

    if state == "converge":
        if not RS_REVERSAL_ON:
            return None
        want = "LONG" if direction < 0 else "SHORT"
        armed = bool(ast.get("rs_rev_arm"))
        line, lbl = ((m200, RS_TREND_LEN) if armed else (m50, RS_TRIGGER_LEN))
        dist = abs(px - line) / px * 100.0
        return {"sym": sym, "dir": want,
                "stage": "ready" if armed else "waiting", "run": 0,
                "trend": ("bearish fan closing" if direction < 0
                          else "bullish fan closing"),
                "age": 0,
                "detail": (f"{'armed - ' if armed else ''}{dist:.2f}% from "
                           f"the {lbl} - a cross "
                           f"{'up' if want == 'LONG' else 'down'} through it "
                           f"{'ENTERS' if armed else 'arms'}")}

    dist = (px - m20) / px * 100.0
    want = "LONG" if direction > 0 else "SHORT"
    if RS_WITH_TREND:
        if (want == "LONG" and px > m20) or (want == "SHORT" and px < m20):
            return None
    return {"sym": sym, "dir": want, "stage": "waiting", "run": 0,
            "trend": "bullish fan" if direction > 0 else "bearish fan",
            "age": 0,
            "detail": (f"{'above' if dist > 0 else 'below'} the "
                       f"{RS_ARM_LEN} by {abs(dist):.2f}% - "
                       f"{'a cross up' if want == 'LONG' else 'a cross down'}"
                       f" through it enters")}


def rs_stack(closes):
    """(state, direction, reason) for the three-line stack.

    state is "diverge" (fanned and opening out), "converge" (fanned but
    closing up - a trend ending, which is the REVERSAL setup) or None
    (crossed, flat, or not enough history).
    direction is +1 bullish, -1 bearish, 0 when it does not qualify.
    """
    m20 = rs_ma(closes, RS_ARM_LEN)
    m50 = rs_ma(closes, RS_TRIGGER_LEN)
    m200 = rs_ma(closes, RS_TREND_LEN)
    if m20 is None or m50 is None or m200 is None:
        return None, 0, "not enough history"
    n = max(1, RS_SLOPE_BARS)
    if len(closes) <= n:
        return None, 0, "not enough history"
    back = closes[:-n]
    p20 = rs_ma(back, RS_ARM_LEN)
    p50 = rs_ma(back, RS_TRIGGER_LEN)
    p200 = rs_ma(back, RS_TREND_LEN)
    if p20 is None or p50 is None or p200 is None:
        return None, 0, "not enough history"
    sl = [((a - b) / b * 100.0) if b else 0.0
          for a, b in ((m20, p20), (m50, p50), (m200, p200))]

    up = (m20 > m50 > m200) if RS_STACK_ORDER else True
    dn = (m20 < m50 < m200) if RS_STACK_ORDER else True
    if not up and not dn:
        return None, 0, (f"lines crossed - 20 {fmt_px(m20)}, 50 "
                          f"{fmt_px(m50)}, 200 {fmt_px(m200)}")
    # DIVERGING vs CONVERGING: the two gaps, as a % of price, now against
    # RS_SLOPE_BARS ago. A fan that is closing up is a trend ending.
    px = closes[-1] or 1.0
    g_now = (abs(m20 - m50) / px * 100.0, abs(m50 - m200) / px * 100.0)
    g_was = (abs(p20 - p50) / px * 100.0, abs(p50 - p200) / px * 100.0)
    dg = (g_now[0] - g_was[0], g_now[1] - g_was[1])
    diverging = (dg[1] > RS_DIVERGE_MIN if RS_DIVERGE_GAPS == "50_200"
                 else (dg[0] > RS_DIVERGE_MIN and dg[1] > RS_DIVERGE_MIN))
    gtxt = (f"gaps {g_now[0]:.2f}/{g_now[1]:.2f}% "
            f"({dg[0]:+.2f}/{dg[1]:+.2f})")

    if up and all(x > RS_FLAT_PCT for x in sl):
        if RS_REQUIRE_DIVERGING and not diverging:
            return "converge", 1, f"bullish fan CONVERGING - {gtxt}"
        return "diverge", 1, (f"bullish fan, slopes "
                         f"{sl[0]:+.2f}/{sl[1]:+.2f}/{sl[2]:+.2f}%, {gtxt}")
    if dn and all(x < -RS_FLAT_PCT for x in sl):
        if RS_REQUIRE_DIVERGING and not diverging:
            return "converge", -1, f"bearish fan CONVERGING - {gtxt}"
        return "diverge", -1, (f"bearish fan, slopes "
                          f"{sl[0]:+.2f}/{sl[1]:+.2f}/{sl[2]:+.2f}%, {gtxt}")
    return None, 0, (f"flat or mixed - slopes "
                      f"{sl[0]:+.2f}/{sl[1]:+.2f}/{sl[2]:+.2f}%, need "
                      f"{RS_FLAT_PCT}%")


def rs_signal(ast, candles, i):
    """Reversal 200SMMA, 18 Aug spec. Returns "LONG" / "SHORT", else None.

    The stack must be fanned in order AND all three lines trending. Then a
    close that CROSSES the 20 enters: down is a SHORT, up is a LONG, in
    either regime. Judged on the last CLOSED bar, so nothing repaints.
    """
    last = i - 1
    if last < RS_TREND_LEN + RS_SLOPE_BARS + 2:
        return None
    # CLOSED bars only: the stack and the 20 line are fixed for the whole of
    # the forming bar, so nothing here repaints.
    closes = [c["c"] for c in candles[:last + 1]]
    state, direction, why = rs_stack(closes)
    ast["rs_stack"] = why
    if state is None:
        ast["rs_rev_arm"] = None
        return None

    # ---------------- REVERSAL PATH: the fan is CLOSING UP ----------------
    # A tightening trend is a trend ending. Cross the 50 to ARM, cross the
    # 200 to ENTER, in the direction OPPOSITE the fan.
    if state == "converge":
        if not RS_REVERSAL_ON:
            return None
        want = "LONG" if direction < 0 else "SHORT"
        long_r = want == "LONG"
        m50 = rs_ma(closes, RS_TRIGGER_LEN)
        m200 = rs_ma(closes, RS_TREND_LEN)
        px = candles[i]["c"] if RS_INTRABAR else closes[-1]
        prev = closes[-1] if RS_INTRABAR else closes[-2]
        arm = ast.get("rs_rev_arm") or {}
        # drop the arm if price falls back through the 50
        if arm and RS_REV_ARM_CANCEL:
            if (long_r and px < m50) or ((not long_r) and px > m50):
                log(f"{ast.get('sym','?')}: reversal arm dropped - back "
                    f"through the {RS_TRIGGER_LEN} at {fmt_px(m50)}")
                arm = {}
                ast["rs_rev_arm"] = None
        if not arm:
            crossed50 = ((prev <= m50 and px > m50) if long_r
                         else (prev >= m50 and px < m50))
            if crossed50:
                arm = {"side": want, "t": candles[last]["t"]}
                ast["rs_rev_arm"] = arm
                log(f"{ast.get('sym','?')}: REVERSAL ARMED {want} - {why}, "
                    f"crossed the {RS_TRIGGER_LEN} at {fmt_px(m50)}")
        if not arm:
            return None
        crossed200 = ((prev <= m200 and px > m200) if long_r
                      else (prev >= m200 and px < m200))
        if not crossed200:
            return None
        ast["rs_rev_arm"] = None
        ast["rs_path"] = "reversal"
        ast["rs_why"] = (f"{why}, crossed the {RS_TRIGGER_LEN} then the "
                         f"{RS_TREND_LEN} at {fmt_px(m200)} - reversal"
                         + (" (intrabar)" if RS_INTRABAR else ""))
        return want

    # ---------------- CONTINUATION PATH: the fan is OPENING OUT -----------
    ast["rs_rev_arm"] = None
    m20 = rs_ma(closes, RS_ARM_LEN)
    p20 = rs_ma(closes[:-1], RS_ARM_LEN)
    if p20 is None:
        return None
    if RS_INTRABAR:
        # the LIVE price of the forming bar against the CLOSED-bar 20
        px, prev, live = candles[i]["c"], closes[-1], True
    else:
        px, prev, live = closes[-1], closes[-2], False
    ref = m20 if live else p20
    side = None
    if RS_ARM_ON_CROSS:
        if prev >= ref and px < m20:
            side = "SHORT"
        elif prev <= ref and px > m20:
            side = "LONG"
    else:
        side = "SHORT" if px < m20 else ("LONG" if px > m20 else None)
    if not side:
        return None
    # the fan decides the direction: a bullish stack takes LONGS only
    if RS_WITH_TREND:
        want = "LONG" if direction > 0 else "SHORT"
        if side != want:
            return None
    ast["rs_path"] = "continuation"
    ast["rs_why"] = (f"{'bullish' if direction > 0 else 'bearish'} fan "
                     f"({why}), close {fmt_px(px)} crossed "
                     f"{'below' if side == 'SHORT' else 'above'} the "
                     f"{RS_ARM_LEN} at {fmt_px(m20)}"
                     + (" (intrabar)" if RS_INTRABAR else ""))
    return side


def atr(candles, i, n=None):
    """Average True Range at bar i, in price units, over CLOSED bars."""
    n = SD_ATR_LEN if n is None else n
    if i < n or i >= len(candles):
        return None
    trs = []
    for j in range(i - n + 1, i + 1):
        pc = candles[j - 1]["c"]
        trs.append(max(candles[j]["h"] - candles[j]["l"],
                       abs(candles[j]["h"] - pc),
                       abs(candles[j]["l"] - pc)))
    return (sum(trs) / len(trs)) if trs else None


def stoch_kd(candles, i):
    """(%K, %D) of the FAST stochastic at bar i, or (None, None).

    raw %K = 100 * (close - lowest low N) / (highest high N - lowest low N)
    %K     = SMA(raw %K, STOCH_SMOOTH_K)      -- 1 means raw, the fast form
    %D     = SMA(%K, STOCH_D)
    """
    need = STOCH_N + STOCH_SMOOTH_K + STOCH_D
    if i < need or i >= len(candles):
        return None, None
    raws = []
    for j in range(i - STOCH_SMOOTH_K - STOCH_D + 1, i + 1):
        w = candles[j - STOCH_N + 1:j + 1]
        hh = max(x["h"] for x in w)
        ll = min(x["l"] for x in w)
        rng = hh - ll
        raws.append(50.0 if rng <= 0
                    else (candles[j]["c"] - ll) / rng * 100.0)
    ks = []
    for j in range(STOCH_SMOOTH_K - 1, len(raws)):
        seg = raws[j - STOCH_SMOOTH_K + 1:j + 1]
        ks.append(sum(seg) / len(seg))
    if len(ks) < STOCH_D:
        return None, None
    return ks[-1], sum(ks[-STOCH_D:]) / STOCH_D


def stoch_crossed(candles, i, want_long):
    """Did %K cross THROUGH the band edge within STOCH_CROSS_LOOKBACK bars
    ending at i? Long wants a cross DOWN through STOCH_LOW."""
    line = STOCH_LOW if want_long else STOCH_HIGH
    for j in range(max(1, i - STOCH_CROSS_LOOKBACK + 1), i + 1):
        k_now, _ = stoch_kd(candles, j)
        k_prev, _ = stoch_kd(candles, j - 1)
        if k_now is None or k_prev is None:
            continue
        if want_long and k_prev >= line and k_now < line:
            return True, k_now
        if not want_long and k_prev <= line and k_now > line:
            return True, k_now
    k_now, _ = stoch_kd(candles, i)
    return False, k_now


def ha_prior_run(ha, i):
    """(side, bars, first_index) of the HA run ending at bar i."""
    if i < 1:
        return None, 0, i
    side = ha_green(ha[i])
    k = i
    while k > 0 and ha_green(ha[k - 1]) == side:
        k -= 1
    return side, i - k + 1, k


def sd_momentum_weak(candles, ha, doji_i, want_long):
    """(weak, reason). WEAK = the HA bodies SHRANK into the flip.

    Compares the mean body of the last SD_WEAK_TAIL bars of the run against
    the mean body of the bars before them. Shrinking bodies mean the move is
    running out of steam; steady or growing bodies mean it is still driving,
    and we stay out.
    """
    side, bars, start = ha_prior_run(ha, doji_i - 1)
    if side is None:
        return False, "no prior run"
    if side == want_long:
        return False, "prior run is not the opposite direction"
    if bars < max(2, SD_WEAK_MIN_BARS):
        return False, (f"prior run only {bars} bar{'s' if bars != 1 else ''}"
                       f" - too short to show deceleration, need "
                       f"{SD_WEAK_MIN_BARS}")
    bodies = [ha_body(ha[k]) for k in range(start, doji_i)]
    tail_n = min(max(1, SD_WEAK_TAIL), len(bodies))
    tail = bodies[-tail_n:]
    peak = max(bodies)
    if peak <= 0:
        return False, "prior run has no body"
    # Measure the tail against the run's PEAK body, not its opening bars.
    # The first HA candles after a colour flip are always tiny, so a
    # head-vs-tail split on a short run compared the end of the move against
    # its smallest bars and returned nonsense - 499%, 1014%, 1491% on 18 Aug.
    # Against the peak the ratio is bounded at 100% and actually means
    # "how far off its strongest bar has this move faded".
    ratio = (sum(tail) / len(tail)) / peak
    if ratio > SD_WEAK_DECAY:
        return False, (f"bodies not shrinking - last {tail_n} average "
                       f"{ratio * 100:.0f}% of the run's peak body, need "
                       f"under {SD_WEAK_DECAY * 100:.0f}% - strong momentum")
    # optional legacy ceilings, off by default
    p0, p1 = candles[start]["c"], candles[doji_i - 1]["c"]
    pct = ((p1 - p0) / p0 * 100.0) if p0 else 0.0
    if SD_WEAK_MAX_BARS and bars > SD_WEAK_MAX_BARS:
        return False, f"prior run {bars} bars > {SD_WEAK_MAX_BARS}"
    if SD_WEAK_MAX_PCT and abs(pct) > SD_WEAK_MAX_PCT:
        return False, f"prior run moved {pct:+.2f}% > {SD_WEAK_MAX_PCT}%"
    return True, (f"weak momentum - bodies decayed to {ratio * 100:.0f}% over "
                  f"the last {tail_n} of a {bars}-bar run ({pct:+.2f}%)")


def sd_is_doji(ha, i):
    """A doji: body small relative to the biggest body of the run into it."""
    _, bars, start = ha_prior_run(ha, i - 1)
    if bars < 1:
        return False
    biggest = max(ha_body(ha[k]) for k in range(start, i))
    if biggest <= 0:
        return False
    return ha_body(ha[i]) <= HA_DOJI_FRACTION * biggest


def sd_signal(ast, candles, ha, i):
    """Returns "LONG" / "SHORT" to ENTER, else None.

    His 17 Aug spec, judged on CLOSED bars only (i-1 is the last closed):
      1. a DOJI ends the run
      2. then SD_CONFIRM_BARS candles the other way, each with a wick on ONE
         side only - the side the trade runs WITH. Both-ended candles are
         SKIPPED, not counted, not fatal. A wrong-coloured clean candle
         RESETS the search.
      3. the stochastic crossed the band edge within STOCH_CROSS_LOOKBACK
      4. the run into the doji was SHORT and SHALLOW - weak momentum
    """
    last = i - 1
    if last < STOCH_N + STOCH_D + SD_CONFIRM_WINDOW + 5:
        return None
    # find the most recent doji inside the confirmation window
    for d in range(last - 1, last - 1 - SD_CONFIRM_WINDOW, -1):
        if d < 1 or not sd_is_doji(ha, d):
            continue
        prior_side = ha_green(ha[d - 1])
        want_long = not prior_side          # reversal: red run -> LONG
        if not ALLOW_SHORTS and not want_long:
            return None
        count = 0
        ok = True
        for k in range(d + 1, last + 1):
            c = ha[k]
            up = ha_wick(c, upper=True)
            dn = ha_wick(c, upper=False)
            body = ha_body(c)
            tol = (HA_NOWICK_TOL_PCT / 100.0) * body if body > 0 else 0.0
            if body <= 0:
                continue
            if up > tol and dn > tol:
                continue                    # wicked BOTH ends - skip it
            if ha_green(c) != want_long:
                ok = False
                break                       # wrong way - the turn failed
            if not no_wick(c, want_long):
                continue                    # wick on the wrong side - skip
            count += 1
            if count >= SD_CONFIRM_BARS:
                break
        if not ok or count < SD_CONFIRM_BARS:
            continue
        crossed, kval = stoch_crossed(candles, last, want_long)
        if not crossed:
            if LOG_SKIPS:
                log(f"{ast.get('sym','?')}: doji + {count} confirms but the "
                    f"stochastic did not cross "
                    f"{'below' if want_long else 'above'} "
                    f"{STOCH_LOW if want_long else STOCH_HIGH} "
                    f"(%K {kval if kval is None else round(kval, 1)})")
            return None
        weak, why = sd_momentum_weak(candles, ha, d, want_long)
        if not weak:
            if LOG_SKIPS:
                log(f"{ast.get('sym','?')}: doji + confirms + stochastic but "
                    f"{why} - no entry")
            return None
        ast["sd_why"] = (f"doji then {count} clean candle"
                         f"{'s' if count != 1 else ''}, %K {kval:.1f} crossed "
                         f"{'below' if want_long else 'above'} "
                         f"{STOCH_LOW if want_long else STOCH_HIGH}, {why}")
        return "LONG" if want_long else "SHORT"
    return None


def sha_prior_run(candles, i):
    """(side, bars, pct) of the smoothed-HA run IMMEDIATELY BEFORE the run
    that bar i belongs to. pct is the signed price change across it.

    Used to ask "how strong was the move we are flipping out of".
    """
    if i < 2:
        return None, 0, 0.0
    sha = smoothed_ha(candles[:i + 1], SHA_IN, SHA_OUT)
    if not sha or len(sha) < 3:
        return None, 0, 0.0
    cur = ha_green(sha[-1])
    k = len(sha) - 2
    while k >= 0 and ha_green(sha[k]) == cur:
        k -= 1
    if k < 1:
        return None, 0, 0.0
    prior_side, end = ha_green(sha[k]), k
    while k >= 0 and ha_green(sha[k]) == prior_side:
        k -= 1
    start = k + 1
    p0, p1 = candles[start]["c"], candles[end]["c"]
    pct = ((p1 - p0) / p0 * 100) if p0 else 0.0
    return prior_side, end - start + 1, pct


def cross_reversal_ok(candles, i, want_long):
    """(allowed, reason). A FRESH flip out of a STRONG opposite run."""
    if not CROSS_REVERSAL_ON:
        return False, "reversal allowance off"
    side, bars = sha_run_len(candles, i - 1)
    if side is None or side != want_long:
        return False, "smoothed HA does not agree"
    if bars > max(1, CROSS_MAX_TREND_AGE):
        return False, f"flip is {bars} bars old"
    pside, pbars, ppct = sha_prior_run(candles, i - 1)
    if pside is None or pside == want_long:
        return False, "no opposite run before this"
    if pbars < CROSS_REVERSAL_MIN_BARS:
        return False, (f"prior run only {pbars} bars, needs "
                       f"{CROSS_REVERSAL_MIN_BARS}")
    moved = -ppct if want_long else ppct
    if moved < CROSS_REVERSAL_MIN_PCT:
        return False, (f"prior {'downtrend' if want_long else 'uptrend'} only "
                       f"{moved:+.2f}%, needs {CROSS_REVERSAL_MIN_PCT}%")
    return True, (f"flip out of a {pbars}-bar "
                  f"{'downtrend' if want_long else 'uptrend'} of "
                  f"{ppct:+.2f}%")


def cross_slope_pair(candles, i):
    """(slope_now, slope_prior) of the cross EMA as a % of itself, measured on
    CLOSED bars only.

    The forming bar repaints - its own live close is inside its EMA - so a
    slope that included it would flip on every tick. Two ADJACENT windows are
    compared so "turning" means the slope itself is improving, not merely that
    price moved.
    """
    if not EMA_FILTER_LEN or i < 2:
        return None, None
    base = candles[:max(0, i - 1) + 1]
    factor = max(1, MS.get(EMA_FILTER_TF, MS[TF]) // MS[TF])
    higher = resample(base, factor)
    n = max(1, CROSS_SLOPE_BARS)
    if len(higher) < EMA_FILTER_LEN + 2 * n + 1:
        return None, None
    s = ema([c["c"] for c in higher], EMA_FILTER_LEN)
    a, b, cc = s[-1], s[-1 - n], s[-1 - 2 * n]
    now = ((a - b) / b * 100) if b else None
    prior = ((b - cc) / cc * 100) if cc else None
    return now, prior


def cross_trend_ok(candles, i, want_long):
    """(allowed, reason). Longs need slope >= 0, or negative and TURNING UP.
    Shorts mirror it."""
    if not CROSS_TREND_GATE:
        return True, "trend gate off"
    now, prior = cross_slope_pair(candles, i)
    if now is None:
        return True, "not enough history to judge the slope"
    # CHOP GATE, before direction is considered at all. A flat EMA satisfies
    # both >= 0 and <= 0, so without this the deadest markets are the most
    # permissive ones.
    # REVERSAL FIRST: a flip out of a strong opposite run is allowed even
    # though the slope still points the other way. That is the whole point.
    rok, rwhy = cross_reversal_ok(candles, i, want_long)
    if rok:
        return True, rwhy
    if CROSS_FLAT_PCT:
        turning = (prior is not None and abs(now - prior) >= CROSS_TURN_MIN)
        if abs(now) < CROSS_FLAT_PCT and not turning:
            return False, (f"EMA is flat - slope {now:+.3f}% inside the "
                           f"+/-{CROSS_FLAT_PCT}% dead zone and not turning")
    if want_long:
        if now >= 0:
            return True, f"uptrend, slope {now:+.3f}%"
        if (CROSS_ALLOW_TURN and prior is not None
                and now > prior + CROSS_TURN_MIN):
            return True, (f"beginning an uptrend, slope {now:+.3f}% rising "
                          f"from {prior:+.3f}%")
        return False, (f"downtrend still steepening, slope {now:+.3f}%"
                       + (f" from {prior:+.3f}%" if prior is not None else ""))
    if now <= 0:
        return True, f"downtrend, slope {now:+.3f}%"
    if (CROSS_ALLOW_TURN and prior is not None
            and now < prior - CROSS_TURN_MIN):
        return True, (f"beginning a downtrend, slope {now:+.3f}% falling "
                      f"from {prior:+.3f}%")
    return False, (f"uptrend still steepening, slope {now:+.3f}%"
                   + (f" from {prior:+.3f}%" if prior is not None else ""))


def cross_vol_need(candles, i, now_ms=None):
    """(volume_on_bar, volume_required) for the bar at i, or (v, None) when
    there is not enough history to judge."""
    try:
        v = float(candles[i].get("v") or 0.0)
    except (TypeError, ValueError):
        return 0.0, None
    if not CROSS_VOL_MULT:
        return v, None
    lo = max(0, i - CROSS_VOL_LEN)
    prior = [float(x.get("v") or 0.0) for x in candles[lo:i]]
    prior = [x for x in prior if x > 0]
    if len(prior) < 3:
        return v, None                 # too little history - do not refuse
    need = (sum(prior) / len(prior)) * CROSS_VOL_MULT
    if CROSS_VOL_PRORATE and i == len(candles) - 1:
        span = MS.get(TF, 0)
        if span:
            elapsed = (now_ms if now_ms is not None
                       else int(time.time() * 1000)) - candles[i]["t"]
            need *= min(1.0, max(0.05, elapsed / span))
    return v, need


def cross_vol_ok(candles, i, now_ms=None):
    """Volume gate. "No volume, no entry" - the bar being judged has to have
    actually traded, and to have traded enough. Matters most for the xyz:
    synthetics, whose underlying market shuts overnight and prints empty 30m
    bars."""
    if not CROSS_NEEDS_VOL:
        return True
    v, need = cross_vol_need(candles, i, now_ms)
    if v <= CROSS_MIN_VOL:
        return False
    return True if need is None else v >= need


def sha_flip_signal(ast, candles, i):
    """Trigger: the SMOOTHED HA colour on the last CLOSED bar, but only while
    the run is still young.

    Returns "LONG" / "SHORT" / None. run == 1 is the flip bar itself; up to
    CROSS_MAX_TREND_AGE lets an entry still fire when volume or slope only
    confirmed a bar or two after the turn. Past that the move is underway and
    we would be buying the end of it, which is the whole complaint.

    Read on i-1 because the forming bar's smoothed HA repaints.
    """
    side, run = sha_run_len(candles, i - 1)
    if side is None:
        return None
    if run > max(1, CROSS_MAX_TREND_AGE):
        return None
    return "LONG" if side else "SHORT"


def cross_signal(ast, candles, ha, i):
    """The crossover engine.

    Returns "LONG" / "SHORT" when this bar should ENTER, else None.

    Under CROSS_TRIGGER = "sha_flip" the SMOOTHED HA flip opens the trade and
    the EMA is only a filter. Under "ema_cross" the 8 Aug behaviour applies.

    16 Aug: the CROSS ITSELF enters. Under CROSS_INTRABAR the live price of
    the FORMING bar is compared against the last CLOSED bar's side, so the
    entry fires on the 5m pulse that sees the cross rather than waiting for
    the 30m close. Two things follow from that and are not bugs: the EMA
    repaints, because ema_cross_side feeds the forming bar's live close into
    its own EMA; and an intrabar cross can un-cross before the bar closes, so
    this takes entries a close-based rule never would.

    CROSS_NEEDS_NOWICK restores the old 8 Aug two-step behaviour.
    """
    if CROSS_TRIGGER == "sha_flip":
        want = sha_flip_signal(ast, candles, i)
        if not want:
            return None
        want_long = want == "LONG"
        if not cross_vol_ok(candles, i):
            if LOG_SKIPS:
                v, need = cross_vol_need(candles, i)
                log(f"{ast.get('sym','?')}: smoothed HA flipped "
                    f"{'green' if want_long else 'red'} but volume {v:g} is "
                    f"under the {('%g' % need) if need is not None else '0'} "
                    f"required - no entry")
            return None
        ok, why = cross_trend_ok(candles, i, want_long)
        if not ok:
            if LOG_SKIPS:
                log(f"{ast.get('sym','?')}: smoothed HA flipped "
                    f"{'green' if want_long else 'red'} refused - {why}")
            return None
        _, run = sha_run_len(candles, i - 1)
        ast["cross_why"] = (f"smoothed HA {SHA_IN},{SHA_OUT} turned "
                            f"{'green' if want_long else 'red'} {run} bar"
                            f"{'s' if run != 1 else ''} ago, {why}")
        return want

    side = ema_cross_side(candles, i)
    prev = ema_cross_side(candles, i - 1)
    if side is None or prev is None:
        return None
    if side != prev:                       # a CROSS happened on this bar
        want = "LONG" if side > 0 else "SHORT"
        if not CROSS_NEEDS_NOWICK:
            if not cross_vol_ok(candles, i):
                if LOG_SKIPS:
                    v, need = cross_vol_need(candles, i)
                    log(f"{ast.get('sym','?')}: EMA cross "
                        f"{'up' if side > 0 else 'down'} but volume {v:g} is "
                        f"under the "
                        f"{('%g' % need) if need is not None else '0'} "
                        f"required - no entry")
                return None
            ok, why = cross_trend_ok(candles, i, side > 0)
            if not ok:
                if LOG_SKIPS:
                    log(f"{ast.get('sym','?')}: EMA cross "
                        f"{'up' if side > 0 else 'down'} refused - {why}")
                return None
            aok, awhy = cross_age_ok(candles, i, side > 0)
            if not aok:
                if LOG_SKIPS:
                    log(f"{ast.get('sym','?')}: EMA cross "
                        f"{'up' if side > 0 else 'down'} refused - {awhy}")
                return None
            ast["cross_why"] = f"{why}, {awhy}"
            return want
        arm = {"side": want, "t": candles[i]["t"]}
        ast["cross_arm"] = arm
        log(f"{ast.get('sym','?')}: EMA CROSS {'up' if side > 0 else 'down'} "
            f"- arming {arm['side']}, waiting for a no-wick candle")
        return None
    if not CROSS_NEEDS_NOWICK:
        return None
    arm = ast.get("cross_arm") or {}
    if not arm:
        return None
    # the no-wick candle must come AFTER the cross, and is judged on the
    # LAST CLOSED bar exactly as the flip engine judges it
    want_long = arm["side"] == "LONG"
    if candles[i - 1]["t"] <= arm["t"]:
        return None
    if not no_wick(ha[i - 1], want_long):
        return None
    if not cross_vol_ok(candles, i - 1):
        return None
    ok, why = cross_trend_ok(candles, i, want_long)
    if not ok:
        if LOG_SKIPS:
            log(f"{ast.get('sym','?')}: no-wick trigger refused - {why}")
        return None
    ast["cross_why"] = why
    return arm["side"]


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
    # NOWICK_ONLY: the candle before the entry bar must simply be the trade's
    # colour with no wick against it. The run and the flip are not consulted,
    # so (flip_index, run_start) both point at that candle - downstream only
    # uses them to size the stop window.
    if NOWICK_ONLY:
        if ha_green(ha[nw]) != want_long:
            return None
        if not no_wick(ha[nw], want_long):
            return None
        return nw, nw
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


def flip_gate(ast, candles, ha, i):
    """What the pipeline card shows in FLIP_MODE."""
    if i < 2:
        return None
    arm = ast.get("flip_arm") or {}
    tr = flip_trending(candles, i)
    d = {"dir": arm.get("side") or ("LONG" if ha_green(ha[i - 1]) else "SHORT"),
         "trend": "up" if tr > 0 else ("down" if tr < 0 else "flat"),
         "age": 0, "need": 1, "t": candles[i]["t"], "px": candles[i]["c"]}
    if FLIP_FLAT_PCT and tr == 0:
        d.update(stage="flat - MA going nowhere",
                 detail=f"needs {FLIP_FLAT_PCT}% over {FLIP_SLOPE_BARS} bars")
        return d
    if not arm:
        d.update(stage="waiting for a colour flip",
                 detail=f"MA trending {'up' if tr > 0 else 'down'}")
        return d
    want_long = arm["side"] == "LONG"
    if FLIP_FLAT_PCT and (tr > 0) != want_long:
        d.update(stage="MA disagrees",
                 detail=f"armed {arm['side']} but the MA is "
                        f"{'up' if tr > 0 else 'down'}")
        return d
    w, b = ha_wick(ha[i], upper=not want_long), ha_body(ha[i])
    pct = (w / b * 100) if b else 999
    d.update(stage="no-wick bar forming",
             detail=(f"clean so far ({pct:.0f}% of body)"
                     if pct <= HA_NOWICK_TOL_PCT
                     else f"wick {pct:.0f}% of body, needs "
                          f"<= {HA_NOWICK_TOL_PCT:.0f}%"))
    return d


def cross_gate(ast, candles, ha, i):
    """What the pipeline card shows in CROSS_MODE."""
    side = ema_cross_side(candles, i)
    if side is None:
        return None
    arm = ast.get("cross_arm") or {}
    d = {"dir": arm.get("side") or ("LONG" if side > 0 else "SHORT"),
         "trend": "up" if side > 0 else "down", "age": 0, "need": 1,
         "t": candles[i]["t"], "px": candles[i]["c"]}
    if not arm:
        d.update(stage="waiting for a cross",
                 detail=f"price is {'above' if side > 0 else 'below'} the "
                        f"{EMA_FILTER_LEN} EMA")
        return d
    want_long = arm["side"] == "LONG"
    w, b = ha_wick(ha[i], upper=not want_long), ha_body(ha[i])
    pct = (w / b * 100) if b else 999
    d.update(stage="no-wick bar forming",
             detail=(f"clean so far ({pct:.0f}% of body)"
                     if pct <= HA_NOWICK_TOL_PCT
                     else f"wick {pct:.0f}% of body, needs "
                          f"<= {HA_NOWICK_TOL_PCT:.0f}%"))
    return d


def gate_status(ha, candles, i, sym=None, ast_for_gate=None):
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
    # want_long here is the SIGNAL's own reading - a green flip means the
    # red run ended. The SIDE ACTUALLY TRADED is that reading passed through
    # HA_MODE, exactly as the signal path does it. Without this the panel
    # showed the opposite direction to the engine under "continuation":
    # "6 candles up · SHORT" on a card that would have entered LONG.
    want_long = ha_green(ha[f])
    traded_long = want_long if HA_MODE == "reversal" else not want_long
    d = {"dir": "LONG" if traded_long else "SHORT",
         "trend": "down" if want_long else "up",
         "age": age, "need": 1}

    # ONE PER TREND, CHECKED FIRST. His call, 11 Aug. A symbol already
    # trading its current trend shows ONE steady row instead of cycling
    # through flipped / forming / missed for every setup inside that trend.
    # The cost is that a card no longer says what ELSE would have refused
    # it - this answer wins outright.
    # TOO LATE IN THE TREND - checked with the trend rules, before the
    # pattern stages, so a card says "mid-trend" rather than "ready".
    if TREND_MAX_AGE:
        tk0 = trend_start_t(candles, i)
        if tk0 is not None:
            age_bars = sum(1 for c in candles[:i + 1] if c["t"] >= tk0) - 1
            if age_bars > TREND_MAX_AGE:
                d.update(stage="too late in the trend",
                         detail=f"{age_bars} bars since the EMA cross, "
                                f"max {TREND_MAX_AGE}")
                return d

    if ONE_PER_TREND and sym:
        dn = (ast_for_gate or {}).get("trend_taken") or {}
        if dn:
            tk = trend_start_t(candles, i)
            if tk and dn.get("t") == tk and dn.get("dir") == d["dir"]:
                d.update(stage="trend already taken",
                         detail="one alert per trend - waiting for the EMA "
                                "to cross back")
                return d

    r = f - 1
    while r >= 0 and ha_green(ha[r]) != want_long:
        r -= 1
    r += 1
    run = ha[r:f]
    d["run"] = len(run)
    if not run:
        return None
    # THE WHOLE-RUN EMA TEST, checked as soon as the run is known. It does
    # not depend on the no-wick bar, so gating it behind age == 2 hid it from
    # every "flipped" and "no-wick bar forming" card - which is why the stage
    # never appeared on the panel.
    if EMA_WHOLE_RUN and run:
        okr, bd = ema_run_side(candles, r, min(i, f), traded_long)
        if not okr:
            d.update(stage="run was on the wrong side",
                     detail=f"{bd} candles of the run sat across the "
                            f"{EMA_FILTER_LEN} EMA")
            return d
    if not NOWICK_ONLY and len(run) < max(1, HA_MIN_RUN):
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

    # THE EMA, CHECKED ONCE FOR EVERY POST-FLIP STAGE. It used to run only
    # at the no-wick and entry bars, so "flipped" and "no-wick bar forming"
    # cards carried no EMA verdict at all and he was comparing them against
    # his own chart and finding them on the wrong side. This is the CURRENT
    # reading - price can cross back before the entry bar arrives, which is
    # why the detail says "now".
    ok_ema, ev, epx = ema_side(candles, i, traded_long)
    if not ok_ema:
        right_side = (epx > ev) == traded_long if (ev and epx) else False
        slope = ema_slope(candles, i)
        with_trend = ((slope if traded_long else -slope)
                      if slope is not None else None)
        if right_side and EMA_SLOPE_PCT and with_trend is not None \
                and with_trend < EMA_SLOPE_PCT:
            d.update(stage="range - EMA is flat",
                     detail=f"{slope:+.2f}% over {EMA_SLOPE_BARS} x "
                            f"{EMA_SLOPE_TF}, needs {EMA_SLOPE_PCT:+.2f}%")
        elif right_side:
            gap = (abs(epx - ev) / ev * 100) if ev else 0
            d.update(stage="too far from EMA",
                     detail=f"{gap:.2f}% away, needs <= {EMA_RETEST_PCT}%")
        else:
            d.update(stage="wrong side of EMA",
                     detail=f"now {fmt_px(epx)} vs {EMA_FILTER_LEN} EMA "
                            f"{fmt_px(ev)}")
        return d

    if age == 0:
        d.update(stage="flipped", detail="needs a no-wick bar next")
        return d
    if age == 1:
        forming = (i == len(ha) - 1)
        if forming:
            # the no-wick candidate is the bar still printing. Its wick can
            # grow before the close, so this is NOT a promise - ETHFI sat at
            # "ready" for 25 minutes on a 20:00 bar that grew an upper wick.
            w, b = ha_wick(ha[i], upper=not want_long), ha_body(ha[i])
            pct = (w / b * 100) if b else 999
            clean = no_wick(ha[i], want_long)
            d.update(stage="no-wick bar forming",
                     detail=(f"clean so far ({pct:.0f}% of body)" if clean
                             else f"wick {pct:.0f}% of body, needs "
                                  f"<= {HA_NOWICK_TOL_PCT:.0f}%"))
            return d
        if no_wick(ha[i], want_long):
            ok_ema, ev, epx = ema_side(candles, i + 1, traded_long)
            if not ok_ema:
                right_side = (epx > ev) == want_long if (ev and epx) else False
                gap = (abs(epx - ev) / ev * 100) if ev else 0
                slope = ema_slope(candles, i + 1)
                flat = (EMA_SLOPE_PCT and slope is not None
                        and (slope if want_long else -slope) < EMA_SLOPE_PCT)
                if right_side and flat:
                    d.update(stage="range - EMA is flat",
                             detail=f"{slope:+.2f}% over {EMA_SLOPE_BARS}"
                                    f"{EMA_FILTER_TF}, needs "
                                    f"{EMA_SLOPE_PCT:+.2f}%")
                elif right_side:
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
    # age 2 IS the entry bar under ENTRY_AT_OPEN - the trade fires on this
    # candle's open. Calling it "missed" here contradicted the detector,
    # which was firing at the same instant.
    if age == 2 and no_wick(ha[i - 1], want_long):
        # THE ENTRY BAR MUST FACE THE SAME EMA TEST the age-1 branch runs.
        # Without it a card read "ready - entering at this bar's open" on a
        # SHORT sitting ABOVE its EMA, which the engine then refused.
        ok_ema, ev, epx = ema_side(candles, i, traded_long)
        if not ok_ema:
            right_side = (epx > ev) == traded_long if (ev and epx) else False
            slope = ema_slope(candles, i)
            with_trend = ((slope if traded_long else -slope)
                          if slope is not None else None)
            if right_side and EMA_SLOPE_PCT and with_trend is not None \
                    and with_trend < EMA_SLOPE_PCT:
                d.update(stage="range - EMA is flat",
                         detail=f"{slope:+.2f}% over {EMA_SLOPE_BARS} x "
                                f"{EMA_SLOPE_TF}, needs "
                                f"{EMA_SLOPE_PCT:+.2f}%")
            elif right_side:
                gap = (abs(epx - ev) / ev * 100) if ev else 0
                d.update(stage="too far from EMA",
                         detail=f"{gap:.2f}% away, needs "
                                f"<= {EMA_RETEST_PCT}%")
            else:
                d.update(stage="wrong side of EMA",
                         detail=f"close {fmt_px(epx)} vs "
                                f"{EMA_FILTER_LEN} EMA {fmt_px(ev)}")
            return d
        if not ALLOW_SHORTS and not traded_long:
            d.update(stage="shorts are off", detail="long-only, by config")
            return d
        if LONG_LOOKBACK_DAYS and traded_long and sym:
            okl, chg = long_trend_ok({"symbol": sym, "hl_coin": sym,
                                      "fallbacks": [], "cls": "crypto"}, True)
            if not okl:
                d.update(stage="30-day downtrend",
                         detail=f"the {EMA_FILTER_LEN} EMA is {chg:+.2f}% "
                                f"over {LONG_LOOKBACK_DAYS} days")
                return d
        if REGIME_ON and sym:
            rg = regime_side({"symbol": sym, "hl_coin": sym,
                              "fallbacks": [], "cls": "crypto"})
            if rg and (rg > 0) != traded_long:
                d.update(stage=f"{REGIME_TF} trend disagrees",
                         detail=f"the {REGIME_TF} {REGIME_EMA_LEN} EMA says "
                                f"{'UP' if rg > 0 else 'DOWN'}")
                return d
        if BTC_ALIGN and sym != "BTC":
            bias = btc_bias()
            if bias and (bias > 0) != traded_long:
                d.update(stage="BTC disagrees",
                         detail=f"bitcoin is trending "
                                f"{'up' if bias > 0 else 'down'}")
                return d
        d.update(stage="ready", detail="entering at this bar's open")
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
    # DERIVE the R multiple from the actual levels. Printing a constant was
    # wrong the moment two pathways with different targets existed.
    ent, stp, tp = plan.get("entry"), plan.get("stop"), plan.get("tp")
    risk = abs(ent - stp) if (ent is not None and stp is not None) else None
    rr = (abs(tp - ent) / risk) if (risk and tp is not None) else None
    rpct = (risk / ent * 100.0) if (risk and ent) else None
    tpct = (abs(tp - ent) / ent * 100.0) if (tp and ent) else None
    lines = [
        f"{e} <b>{direction} ENTRY \u00b7 {esc(asset['symbol'])}</b>",
        f"<i>{esc(asset['label'])} \u00b7 {TF} \u00b7 {engine_label()} \u00b7 "
        f"{esc(fmt_ts(t))}</i>",
        "",
        f"\U0001F4CA <b>Setup</b>: {esc(trigger)}",
        "",
    ]
    lv = IM_LEVELS.get(asset["symbol"]) if IM_MODE else None
    if lv and lv[2]:
        mdv, sbv, band, shv = lv
        where = ("above the overbought line" if mdv > band else
                 "below the oversold line" if mdv < -band else
                 "between the lines")
        pth = IM_PATH.get(asset["symbol"], "extension")
        if pth == "breakout":
            # the bands are NOT part of a breakout decision - it fires from a
            # FLAT md at zero. Saying "above the overbought line" on a
            # breakout LONG reads like the oversold rule was broken.
            note = (f"pathway 2 \u00b7 breakout from a flat range \u00b7 "
                    f"the bands are not used on this pathway")
        else:
            note = (f"pathway 1 \u00b7 {where} \u00b7 {IM_BAND_PCTILE}th pct "
                    f"of |md| over {IM_BAND_DAYS}d")
        lines += [
            "\U0001F4C8 <b>Impulse MACD</b>",
            f"md:    <code>{mdv:+.3e}</code>   signal: <code>{sbv:+.3e}</code>"
            f"   hist: <code>{shv:+.3e}</code>",
            f"Bands: <code>+{band:.3e}</code> overbought / "
            f"<code>-{band:.3e}</code> oversold",
            f"<i>{esc(note)}</i>",
            "",
        ]
    lines += [
        "\U0001F4CB <b>Plan</b>",
        f"Entry: <code>${fmt_px(ent)}</code>",
    ]
    if not STOP_EXIT:
        lines.append(f"Stop:  <code>${fmt_px(stp)}</code>  "
                     f"(sizing reference only - not placed)")
    else:
        lines.append(
            f"Stop:  <code>${fmt_px(stp)}</code>"
            + (f"  ({rpct:.2f}% of price)" if rpct else "")
            + (f" \u00b7 confirmed on the close"
               if (IM_MODE and IM_STOP_ON_CLOSE) else ""))
    lines.append(
        f"TP:    <code>${fmt_px(tp)}</code>"
        + (f"  ({rr:.1f}R" if rr else "")
        + (f" \u00b7 {tpct:.2f}% away)" if tpct else ")" if rr else ""))
    if HA_PARTIAL < 1.0:
        lines.append(f"<i>{HA_PARTIAL:.0%} booked at the target</i>")
    lines.append(f"<i>data: {esc(source)}</i>")
    return "\n".join(lines)


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
        # a stop the TRAIL had already ratcheted above entry is a WIN, and
        # calling it "STOPPED OUT" made a +6% CASHCAT exit read as a loss
        "TRAIL": ("\u2705", "TRAIL EXIT", "trailing stop took the profit"),
        "FLIP": ("\U0001f504", "COLOUR FLIP EXIT",
                 "the HA flipped against the trade"),
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
                # a trade opened before tagging existed has none; read a
                # missing tag as the original engine rather than "unknown"
                "engine": trade.get("engine", "ha"),
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


def _stop_kind(trade):
    """Which name does this stop deserve?

    BE   - the post-partial stop sitting at entry
    TRAIL - the trail ratcheted it INTO PROFIT, so the exit is a win
    STOP  - the original structural level, a real loss"""
    if trade.get("half"):
        return "BE"
    e, st = trade.get("entry"), trade.get("stop")
    if e and st:
        in_profit = (st > e) if trade["verdict"] == "LONG" else (st < e)
        if in_profit:
            return "TRAIL"
    return "STOP"


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


def ensure_partial(asset, trade, px):
    """The partial is only real if the EXCHANGE reduced. _book_partial books
    the ledger row and shrinks the stop; if the TP never rested (or never
    filled) the venue still holds the FULL position, and the stop now covers
    only half of it. Live on SKR 5 Aug: 11342 held, 5671 stop.

    Reads the position and sends a reduce-only market order for whatever is
    still above the intended remainder."""
    if not EXEC_LIVE or not executable(asset["symbol"]) or not trade.get("size"):
        return
    ex = exec_client()
    if not ex:
        return
    sym = exec_symbol(asset["symbol"])
    want = abs(trade["size"]) * trade.get("left", 1.0)
    try:
        st = _EXEC["info"].user_state(_EXEC["addr"]) or {}
        held = 0.0
        for p in st.get("assetPositions", []):
            if p["position"]["coin"] == sym:
                held = abs(float(p["position"]["szi"]))
    except Exception as e:
        log(f"{sym}: could not read the position after the partial "
            f"({type(e).__name__}) - check it by hand")
        return
    excess = held - want
    if excess <= abs(trade["size"]) * 0.01:      # 1% tolerance for rounding
        return
    log(f"{sym}: partial booked but the exchange still holds {held} against "
        f"{want} - selling the {excess} difference at market")
    try:
        dec = size_decimals(asset["symbol"])
        ex.order(sym, trade["verdict"] == "SHORT", round(excess, dec),
                 round_px(px), {"limit": {"tif": "Ioc"}}, reduce_only=True)
        log(f"{sym}: reconciling partial sent")
    except Exception as e:
        log(f"{sym}: partial reconcile FAILED ({type(e).__name__}: {e})")
        try:
            send_telegram(f"\ud83d\udea8 {esc(sym)} booked a partial but the "
                          "exchange still holds the FULL position and the "
                          "stop now covers only half - CLOSE THE DIFFERENCE "
                          "BY HAND")
        except Exception:
            pass


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
    ensure_partial(asset, trade, px)     # the venue must actually have reduced
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


def trail_stop(asset, trade, c, long):
    """Ratchet the stop up behind the best price this trade has seen.

    The PEAK lives on the trade record rather than being recomputed, so the
    stop never falls back when price pulls in. It moves the RESTING exchange
    order too - without that the venue would still hold the original stop
    while the bot believed the trade was protected, which is the exact gap
    move_stop_live was written for after a partial."""
    if not TRAIL_ON or trade.get("half"):
        return False
    risk = trade.get("risk0") or abs(trade["entry"] - trade["stop"])
    if risk <= 0:
        return False
    peak = trade.get("peak")
    now = c["h"] if long else c["l"]
    if peak is None or (now > peak if long else now < peak):
        trade["peak"] = peak = now
    gain_r = ((peak - trade["entry"]) if long
              else (trade["entry"] - peak)) / risk
    if gain_r < TRAIL_START_R:
        return False
    want = (peak - TRAIL_GAP_R * risk) if long else (peak + TRAIL_GAP_R * risk)
    if (want <= trade["stop"]) if long else (want >= trade["stop"]):
        return False                      # RATCHET ONLY
    old = trade["stop"]
    trade["stop"] = want
    log(f"{asset['symbol']}: TRAIL - {gain_r:.2f}R up, stop "
        f"${fmt_px(old)} -> ${fmt_px(want)} "
        f"({TRAIL_GAP_R:.2f}R behind the ${fmt_px(peak)} peak)")
    move_stop_live(asset, trade)
    return True


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
        if STOP_EXIT and ((c["l"] <= trade["stop"]) if long
                          else (c["h"] >= trade["stop"])):
            kind = _stop_kind(trade)
            # CLOSE-CONFIRMED STOP (stoch-doji only). A wick THROUGH the level
            # that closes back inside does NOT stop us out - that wick is the
            # stop hunt. Two escapes: a bar that CLOSES past the level, and
            # the disaster level, which needs no close at all.
            eng = trade.get("engine")
            on_close = ((SD_STOP_ON_CLOSE and eng == "sd")
                        or (RS_STOP_ON_CLOSE and eng == "rs")
                        or (IM_STOP_ON_CLOSE and eng == "im"))
            dis_r = {"sd": SD_DISASTER_R, "im": IM_DISASTER_R}.get(
                eng, RS_DISASTER_R)
            if on_close and STOP_EXIT:
                r0 = abs(trade["entry"] - trade["stop"]) or None
                dis = None
                if r0 and dis_r:
                    dis = ((trade["entry"] - dis_r * r0) if long
                           else (trade["entry"] + dis_r * r0))
                blown = dis is not None and ((c["l"] <= dis) if long
                                             else (c["h"] >= dis))
                closed_past = ((c["c"] <= trade["stop"]) if long
                               else (c["c"] >= trade["stop"]))
                if blown:
                    log(f"{asset['symbol']}: DISASTER STOP - price ran "
                        f"{dis_r}x the stop distance past entry, "
                        f"out at ${fmt_px(dis)} without waiting for a close")
                    return _close_trade(asset, trade, dis, kind, event_t)
                if not closed_past:
                    log(f"{asset['symbol']}: wick to ${fmt_px(c['l'] if long else c['h'])} "
                        f"pierced the ${fmt_px(trade['stop'])} stop but the "
                        f"bar closed at ${fmt_px(c['c'])} - holding")
                    trade["wicked"] = int(trade.get("wicked", 0)) + 1
                    continue
                # closed past: we exit at the CLOSE, not the level
                log(f"{asset['symbol']}: bar CLOSED past the stop at "
                    f"${fmt_px(c['c'])} (level ${fmt_px(trade['stop'])})")
                return _close_trade(asset, trade, c["c"], kind, event_t)
            return _close_trade(asset, trade, trade["stop"], kind, event_t)
        # THE HA FLIP EXIT. Under FLIP_MODE it is the ONLY exit; with
        # FLIP_EXIT it runs ALONGSIDE the target, closing the trades that
        # never reach it.
        if FLIP_MODE or FLIP_EXIT:
            idx = next((n for n, x in enumerate(candles)
                        if x["t"] == c["t"]), None)
            if FLIP_EXIT_ON_REGIME:
                # THE 4h EMA CROSS, not the 1h HA. The same line that
                # permitted the entry now decides when the reason for it is
                # gone, so the trade is not cut short by an hourly wobble.
                rg = regime_side(asset)
                if rg and (rg > 0) != long:
                    log(f"{asset['symbol']}: {REGIME_TF} EMA crossed "
                        f"{'down' if long else 'up'} - closing the "
                        f"{trade['verdict']} at ${fmt_px(c['c'])}")
                    return _close_trade(asset, trade, c["c"], "FLIP", event_t)
            elif idx is not None and idx >= 1:
                # the regular HA flip alone is too twitchy - on HYPE it closed
                # eight shorts inside one 16% descent. The SMOOTHED series has
                # to flip too before the trade is given up.
                sh = sha_side(candles, idx)
                agreed = (sh is None) or (sh != long)
                if ha_green(ha[idx]) != long and agreed:
                    log(f"{asset['symbol']}: HA flipped "
                        f"{'red' if long else 'green'} - closing the "
                        f"{trade['verdict']} at ${fmt_px(c['c'])}")
                    return _close_trade(asset, trade, c["c"], "FLIP", event_t)
            if FLIP_MODE:
                continue        # flip mode has no target to fall through to

        # CROSS_MODE: the ONLY exit is price crossing back. Checked before
        # the trail, which is inert here anyway - there is no R target for
        # it to protect.
        if CROSS_MODE and CROSS_EXIT_ON_CROSSBACK:
            idx = next((n for n, x in enumerate(candles)
                        if x["t"] == c["t"]), None)
            side = ema_cross_side(candles, idx) if idx else None
            if side is not None and ((side < 0) if long else (side > 0)):
                log(f"{asset['symbol']}: EMA CROSS back "
                    f"{'down' if long else 'up'} - closing the "
                    f"{trade['verdict']} at ${fmt_px(c['c'])}")
                return _close_trade(asset, trade, c["c"], "CROSS", event_t)
            continue

        # AFTER the stop test on this candle, so a bar that reached the old
        # stop still exits there rather than at a level moved mid-bar
        if trail_stop(asset, trade, c, long):
            changed = True
        if not trade.get("half"):
            if (c["h"] >= trade["tp"]) if long else (c["l"] <= trade["tp"]):
                # AT HA_PARTIAL = 1.0 _book_partial CLOSES the trade and
                # returns _close_trade's (None, True). Discarding that left
                # the ledger with a TP row while state still tracked the
                # position as open - PUMP, 5 Aug, in both panels at once.
                tp_px = trade["tp"]
                risk0 = trade.get("risk0") or abs(trade["entry"]
                                                  - trade["stop"])
                rung = int(trade.get("rung") or 0)
                done = _book_partial(asset, trade, tp_px, event_t)
                if done is not None:
                    # ROLL: reopen at the target with a fresh 1.5R above it.
                    # The stop travels with the entry so each rung risks the
                    # same distance, and the ladder ends on a stop or a flip.
                    if (RETARGET_ON and risk0 > 0
                            and (not RETARGET_MAX or rung + 1 <= RETARGET_MAX)):
                        nstop = (tp_px - risk0) if long else (tp_px + risk0)
                        ntp = (tp_px + HA_RR * risk0 if long
                               else tp_px - HA_RR * risk0)
                        rolled = {
                            "verdict": trade["verdict"], "entry": tp_px,
                            "stop": nstop, "tp": ntp,
                            "opened_t": c["t"], "checked_t": c["t"],
                            "rr": HA_RR, "risk0": risk0, "half": False,
                            "left": 1.0, "size": trade.get("size"),
                            "engine": trade.get("engine", ENGINE_TAG),
                            "rung": rung + 1, "source": "ROLL"}
                        log(f"{asset['symbol']}: TARGET HIT - rolling to rung "
                            f"{rung + 1} at ${fmt_px(tp_px)}, new target "
                            f"${fmt_px(ntp)}, stop ${fmt_px(nstop)}")
                        try:
                            send_telegram(
                                f"\U0001F501 <b>{esc(asset['symbol'])}</b> "
                                f"target hit - rolled to rung {rung + 1} at "
                                f"<code>${fmt_px(tp_px)}</code>, next target "
                                f"<code>${fmt_px(ntp)}</code>")
                        except Exception:
                            pass
                        # the CALLER assigns this to ast["trade"], so the
                        # ladder continues by returning the new rung rather
                        # than writing to a global that does not exist
                        return rolled, True
                    return done
            # ---- BAR-COUNT EXPIRY (cross engine) -------------------------
            # AFTER the target test on purpose: a bar that reaches 1.5R on the
            # deadline books TP. Counted in TF bars from the ENTRY bar
            # inclusive - fire_entry sets opened_t to the entry candle's t.
            # NOT "sd". The bar-count expiry is a CROSS-ENGINE filter and the
            # 17 Aug spec says nothing from the old engine carries over. A
            # stoch-doji trade exits on its target or its stop, nothing else.
            if CROSS_MAX_BARS and trade.get("engine") == "cross":
                span = MS.get(TF, 0)
                opened = trade.get("opened_t") or 0
                if span and opened:
                    held = int((c["t"] - opened) // span) + 1
                    if held > CROSS_MAX_BARS:
                        log(f"{asset['symbol']}: {CROSS_MAX_BARS}-bar window "
                            f"expired without {HA_RR}R - closing the "
                            f"{trade['verdict']} at ${fmt_px(c['c'])} "
                            f"after {held} bars")
                        return _close_trade(
                            asset, trade, c["c"], "EXPIRED", event_t,
                            f"{HA_RR}R did not fill inside "
                            f"{CROSS_MAX_BARS} bars")
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
        if STOP_EXIT and ((live["l"] <= trade["stop"]) if long
                          else (live["h"] >= trade["stop"])):
            kind = _stop_kind(trade)
            # CLOSE-CONFIRMED ENGINES MUST NOT EXIT HERE. This block fires on
            # the FORMING candle the moment the stop is touched - which is
            # exactly the wick the close test exists to ignore, and it beat
            # the close test every time because a bar touches a level before
            # it can close past it. xyz:AMD 21 Aug stopped at $472.5702
            # "(intrabar)" on a wick while IM_STOP_ON_CLOSE was on.
            # The DISASTER level still exits from here, since that is the
            # runaway case close confirmation is not meant to sit through.
            eng_l = trade.get("engine")
            on_close_l = ((SD_STOP_ON_CLOSE and eng_l == "sd")
                          or (RS_STOP_ON_CLOSE and eng_l == "rs")
                          or (IM_STOP_ON_CLOSE and eng_l == "im"))
            if on_close_l:
                dr = {"sd": SD_DISASTER_R, "im": IM_DISASTER_R}.get(
                    eng_l, RS_DISASTER_R)
                r0 = abs(trade["entry"] - trade["stop"]) or None
                dis = (((trade["entry"] - dr * r0) if long
                        else (trade["entry"] + dr * r0))
                       if (r0 and dr) else None)
                if dis is not None and ((live["l"] <= dis) if long
                                        else (live["h"] >= dis)):
                    log(f"{sym}: DISASTER STOP intrabar - price ran {dr}x the "
                        f"stop distance past entry")
                    return _close_trade(asset, trade, dis, kind, t_now,
                                        "Intrabar - disaster level.")
            else:
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
           # the STOP leg is omitted when STOP_EXIT is off - the level is
           # still computed for sizing and the target, but nothing rests on
           # the venue to enforce it
           "orders": [x for x in [
               {"kind": "entry", "type": "market-IOC",
                "side": "buy" if long_ else "sell", "size": round(size, 8)},
               ({"kind": "stop", "type": "stop-market", "reduce_only": True,
                 "trigger": stop, "size": round(size, 8)}
                if STOP_EXIT else None),
               {"kind": "tp", "type": "limit", "reduce_only": True,
                "price": tp, "size": round(size, 8)}] if x]}
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
              "TRAIL": "trailing stop filled - flat",
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
        tp_err = order_error(r_tp)
        trade["tp_oid"] = order_oid(r_tp)
        if tp_err or not trade["tp_oid"]:
            # NOT a cosmetic gap. Without a resting TP the partial can only
            # be detected from candles, and _book_partial then shrinks the
            # stop to match a reduction that never happened - leaving half
            # the position with NO stop. SKR, 5 Aug.
            log(f"{sym}: TP ORDER DID NOT REST ({tp_err or 'no oid returned'})"
                " - the partial will be reconciled at market instead")
            try:
                send_telegram(f"\u26a0\ufe0f {esc(sym)} TP order did not rest "
                              f"- {esc(str(tp_err or 'no oid'))}. The partial "
                              "will be sent at market when the level trades.")
            except Exception:
                pass
        log(f"{sym}: LIVE stop ${fmt_px(trade['stop'])} (full size) and TP "
            f"${fmt_px(trade['tp'])} ({HA_PARTIAL:.0%}) "
            f"{'placed' if trade['tp_oid'] else 'ATTEMPTED'}")
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
    if not exec_client():          # builds _EXEC if this scan has sent no order
        return None
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


def closing_fill(asset, trade):
    """The venue's OWN closing fill for a position that has vanished.

    The tracked-but-gone path used to book at the current candle close,
    which is not where the trade actually ended: on CASHCAT the ledger said
    0.16419 while the exchange filled 0.18835 - understating a winner by
    nearly half. It also called every one of them "position gone" when most
    were the resting TP doing its job.

    Returns (price, realized_pnl) or (None, None)."""
    if not EXEC_LIVE or not executable(asset["symbol"]):
        return None, None
    if not exec_client():
        return None, None
    sym = exec_symbol(asset["symbol"])
    opened = trade.get("opened_t") or 0
    try:
        fills = _EXEC["info"].user_fills(_EXEC["addr"]) or []
    except Exception as e:
        log(f"{sym}: could not read fills ({type(e).__name__})")
        return None, None
    best = None
    for f in fills:
        if f.get("coin") != sym or f.get("time", 0) < opened:
            continue
        if "Close" not in str(f.get("dir", "")):
            continue
        if best is None or f["time"] > best["time"]:
            best = f
    if not best:
        return None, None
    try:
        return float(best["px"]), float(best.get("closedPnl") or 0.0)
    except Exception:
        return None, None


def gone_kind(trade, px):
    """Was the vanished position a TP, a STOP, or genuinely unexplained?

    A resting TP fills on the VENUE's price feed, which the agent only sees
    a scan later - so most "position gone" rows were really targets hit."""
    if px is None:
        return "GONE"
    long_ = trade["verdict"] == "LONG"
    tp, stop = trade.get("tp"), trade.get("stop")
    tol = abs(trade["entry"]) * 0.001          # 0.1% of price
    if tp and ((px >= tp - tol) if long_ else (px <= tp + tol)):
        return "TP"
    if stop and ((px <= stop + tol) if long_ else (px >= stop - tol)):
        return "STOP"
    return "GONE"


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
             # START THE STOP WATCH NOW, not at the beginning of the series.
             # The stop is the LOWEST LOW of the last stop_bars() candles, so
             # by construction a candle inside that window already touched it.
             # With checked_t at 0 the watcher replayed those bars, saw the
             # level hit and booked an instant STOP on a position that was
             # perfectly healthy - which is what kept happening to hand-placed
             # PUMP trades.
             "checked_t": (candles[-1]["t"] if candles else 0),
             "source": "ADOPTED"}
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
               live_px=None, engine=None, candles=None, idx=None,
               tp_override=None):
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
    if TP_SOURCE == "swing" and candles is not None and idx is not None:
        # STRUCTURE, not a multiple: the last level price turned at is where
        # it is most likely to stall again. Falls back to HA_RR when no
        # pivot exists in range, so a setup is never lost to a quiet chart.
        lvl, k = find_swing(candles, idx, not short)
        beyond = lvl is not None and ((lvl < entry) if short else (lvl > entry))
        if beyond:
            tp = lvl
            rr = abs(tp - entry) / risk if risk else 0
            log(f"{sym}: TP at the previous swing "
                f"{'low' if short else 'high'} ${fmt_px(tp)} "
                f"({fmt_ts(candles[k]['t'],'%m-%d %H:%M')}, {rr:.2f}R)")
            if TP_MIN_RR and rr < TP_MIN_RR:
                log(f"{sym}: swing is only {rr:.2f}R away "
                    f"(min {TP_MIN_RR}) - risking more than the target is "
                    "worth, skipped")
                return False
        else:
            log(f"{sym}: no swing {'low' if short else 'high'} beyond entry "
                f"within {SWING_MAX_LOOKBACK} candles - falling back to "
                f"{HA_RR}R")
    if tp_override is not None:
        tp = tp_override            # CROSS_MODE: the exit is the cross, so
                                    # the target is parked out of reach
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
                    "rr": HA_RR, "risk0": risk, "half": False, "left": 1.0,
                    "engine": engine or ENGINE_TAG}
    # remember WHICH setup this came from so it cannot fire a second time
    if ast.get("setup"):
        ast["traded"] = {"ft": ast["setup"].get("ft"), "dir": direction}
        # fire_entry's index is `idx`, NOT `i` - and both it and `candles`
        # are optional here, so guard rather than assume. Using `i` raised
        # NameError on every entry: INJ, 11 Aug 18:00.
        if ONE_PER_TREND and candles is not None and idx is not None:
            tk = trend_start_t(candles, idx)
            if tk:
                ast["trend_taken"] = {"t": tk, "dir": direction}
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

    # ---------------- 4h FLIP ENGINE ----------------
    # Always in the market: a colour flip arms, a no-wick candle enters, the
    # next flip exits and arms the other way. The 50 MA only vetoes a market
    # going nowhere.
    if FLIP_MODE:
        ast["sym"] = sym
        side = flip_signal(ast, candles, ha, i)
        if not side:
            ast["gate"] = flip_gate(ast, candles, ha, i)
            return False
        want_long = side == "LONG"
        # the MA gate is OPTIONAL. At FLIP_FLAT_PCT = 0 it is bypassed
        # ENTIRELY - flip_trending returns 1 in that case, and reading that
        # as "trending up" would have blocked every short.
        if FLIP_FLAT_PCT:
            tr = flip_trending(candles, i)
            if tr == 0:
                if LOG_SKIPS:
                    log(f"{sym}: {side} armed but the {EMA_FILTER_LEN} MA is "
                        f"flat (needs {FLIP_FLAT_PCT}% over "
                        f"{FLIP_SLOPE_BARS} bars) - skipped")
                return False
            if (tr > 0) != want_long:
                if LOG_SKIPS:
                    log(f"{sym}: {side} armed but the {EMA_FILTER_LEN} MA is "
                        f"trending {'UP' if tr > 0 else 'DOWN'} - skipped")
                return False
        if not ALLOW_SHORTS and not want_long:
            return False
        entry = c["o"] if ENTRY_AT_OPEN else c["c"]
        lo = max(0, i - stop_bars())
        win = candles[lo:i]
        if not win:
            return False
        stop = (min(x["l"] for x in win) if want_long
                else max(x["h"] for x in win))
        log(f"{sym}: FLIP ENTRY {side} at ${fmt_px(entry)} - HA flipped "
            f"{'green' if want_long else 'red'}, exits when it flips back")
        # THE TARGET IS REAL when FLIP_TARGET is on: 1.5R books a win and
        # closes. Parked out of reach otherwise, and the flip is the only way
        # out - which meant a trade could touch 1.5R and still book a loss.
        risk_t = abs(entry - stop)
        tp_far = (((entry + HA_RR * risk_t) if want_long
                   else (entry - HA_RR * risk_t)) if FLIP_TARGET
                  else ((entry * 100) if want_long else (entry * 0.01)))
        return fire_entry(asset, ast, side, dict(c, c=entry), stop,
                          c["h"], c["l"], "FLIP",
                          # the wick step is gone under FLIP_NEEDS_NOWICK -
                          # saying it happened described a rule the engine
                          # had stopped running
                          ((f"HA flipped {'green' if want_long else 'red'} "
                            f"on the {TF}")
                           if not FLIP_NEEDS_NOWICK else
                           (f"HA flipped {'green' if want_long else 'red'}, "
                            f"then a no-wick candle - entered at the next "
                            f"open, exits when the colour flips back")),
                          live_px=candles[-1]["c"], engine="flip",
                          candles=candles, idx=i, tp_override=tp_far)

    # ---------------- CROSSOVER ENGINE ----------------
    # Replaces the flip engine entirely when on. No run gates, no fade, no
    # BTC vote - the cross IS the trend read, so layering the old filters on
    # top would refuse the very setups it exists to take.
    # ---------------- IMPULSE MACD ENGINE (LazyBear, his 20 Aug spec) ------
    if IM_MODE:
        ast["sym"] = sym
        side = im_signal(ast, candles, i)
        try:
            ast["gate"] = im_gate_status(ast, candles, i, sym)
        except Exception as e:
            log(f"{sym}: im_gate_status failed: {type(e).__name__}: {e}")
        if not side:
            return False
        want_long = side == "LONG"
        if not ALLOW_SHORTS and not want_long:
            return False
        entry = c["c"]
        path = ast.get("im_path", "extension")
        IM_PATH[sym] = path
        rr = IM_P2_RR if path == "breakout" else IM_P1_RR
        if path == "breakout" and IM_P2_STOP == "pct":
            risk_t = entry * IM_P2_STOP_PCT / 100.0
            stop = (entry - risk_t) if want_long else (entry + risk_t)
            stop_src = f"{IM_P2_STOP_PCT}% of price"
        else:
            lo = max(0, i - IM_SWING_BARS)
            win = candles[lo:i]
            if not win:
                return False
            stop = (min(x["l"] for x in win) if want_long
                    else max(x["h"] for x in win))
            risk_t = abs(entry - stop)
            stop_src = f"{IM_SWING_BARS}-bar swing"
        if risk_t <= 0:
            return False
        tp = ((entry + rr * risk_t) if want_long
              else (entry - rr * risk_t))
        log(f"{sym}: IMPULSE-MACD {path.upper()} {side} at ${fmt_px(entry)} - "
            f"{ast.get('im_why','?')}; stop at the {stop_src} "
            f"${fmt_px(stop)} ({risk_t / entry * 100:.2f}%), target "
            f"${fmt_px(tp)} ({rr}R)")
        # the trigger text is the SETUP only. The alert's Plan section
        # already prints the stop, its % of price, the target and the R
        # multiple - repeating them here said it twice, and said it less
        # precisely the second time.
        return fire_entry(asset, ast, side, dict(c, c=entry), stop,
                          c["h"], c["l"], "IMPULSE",
                          ast.get("im_why", "?"),
                          live_px=candles[-1]["c"], engine="im",
                          candles=candles, idx=i, tp_override=tp)

    # ---------------- REVERSAL 200SMA ENGINE (his 18 Aug spec) ------------
    if RS_MODE:
        ast["sym"] = sym
        side = rs_signal(ast, candles, i)
        # watchlist row: written EVERY scan, after rs_signal has updated the
        # arm, so the panel is never staler than the last close. None clears
        # a stale row rather than leaving it up forever.
        try:
            ast["gate"] = rs_gate_status(ast, candles, i, sym)
        except Exception as e:
            log(f"{sym}: rs_gate_status failed: {type(e).__name__}: {e}")
        if not side:
            return False
        want_long = side == "LONG"
        if not ALLOW_SHORTS and not want_long:
            return False
        entry = c["c"]
        stop = None
        stop_src = f"{RS_SWING_BARS}-bar swing"
        # PATHWAY 1 (continuation) can stop at the 200 SMMA instead
        if RS_STOP_CONT == "200smma" and ast.get("rs_path") == "continuation":
            m200 = rs_ma([x["c"] for x in candles[:i]], RS_TREND_LEN)
            if m200 is not None:
                onside = (m200 < entry) if want_long else (m200 > entry)
                if onside:
                    stop, stop_src = m200, f"{RS_TREND_LEN} SMMA"
                else:
                    log(f"{sym}: 200 SMMA is on the WRONG side of entry "
                        f"({fmt_px(m200)} vs {fmt_px(entry)}) - falling back "
                        f"to the {RS_SWING_BARS}-bar swing")
        if stop is None:
            lo = max(0, i - RS_SWING_BARS)
            win = candles[lo:i]
            if not win:
                return False
            # the RECENT SWING - high for a short, low for a long
            stop = (min(x["l"] for x in win) if want_long
                    else max(x["h"] for x in win))
        risk_t = abs(entry - stop)
        if risk_t <= 0:
            return False
        tp = ((entry + RS_RR * risk_t) if want_long
              else (entry - RS_RR * risk_t))
        log(f"{sym}: REVERSAL-200 ENTRY {side} at ${fmt_px(entry)} - "
            f"{ast.get('rs_why','?')}; stop at the {stop_src} "
            f"${fmt_px(stop)} ({risk_t / entry * 100:.2f}%), target "
            f"${fmt_px(tp)} ({RS_RR}R)")
        return fire_entry(asset, ast, side, dict(c, c=entry), stop,
                          c["h"], c["l"], "REV200",
                          f"{ast.get('rs_why','?')} - {RS_RR}R target or the "
                          f"{stop_src} stop",
                          live_px=candles[-1]["c"], engine="rs",
                          candles=candles, idx=i, tp_override=tp)

    # ---------------- STOCH-DOJI ENGINE (his 17 Aug spec) ----------------
    if SD_MODE:
        ast["sym"] = sym
        side = sd_signal(ast, candles, ha, i)
        if not side:
            return False
        want_long = side == "LONG"
        if not ALLOW_SHORTS and not want_long:
            return False
        entry = c["o"] if ENTRY_AT_OPEN else c["c"]
        stop = None
        if SD_ATR_STOP:
            a = atr(candles, i - 1)          # CLOSED bars only
            if a and a > 0:
                stop = ((entry - SD_ATR_MULT * a) if want_long
                        else (entry + SD_ATR_MULT * a))
        if stop is None:                     # fallback: the old swing window
            lo = max(0, i - stop_bars())
            win = candles[lo:i]
            if not win:
                return False
            stop = (min(x["l"] for x in win) if want_long
                    else max(x["h"] for x in win))
        risk_t = abs(entry - stop)
        if risk_t <= 0:
            return False
        # CLAMP a runaway ATR stop. Thin synthetics produce an ATR several
        # times BTC's, so R - and every loss taken at it - scales with the
        # book's spread rather than with the setup.
        if SD_MAX_STOP_PCT and entry and risk_t / entry * 100 > SD_MAX_STOP_PCT:
            was = risk_t / entry * 100
            risk_t = entry * SD_MAX_STOP_PCT / 100.0
            stop = (entry - risk_t) if want_long else (entry + risk_t)
            log(f"{sym}: stop was {was:.2f}% of price - clamped to "
                f"{SD_MAX_STOP_PCT}% at ${fmt_px(stop)}")
        if MIN_STOP_PCT and entry and risk_t / entry * 100 < MIN_STOP_PCT:
            if LOG_SKIPS:
                log(f"{sym}: stoch-doji {side} stop "
                    f"{risk_t / entry * 100:.3f}% under the {MIN_STOP_PCT}% "
                    f"floor - skipped")
            return False
        tp = ((entry + HA_RR * risk_t) if want_long
              else (entry - HA_RR * risk_t))
        log(f"{sym}: STOCH-DOJI ENTRY {side} at ${fmt_px(entry)} - "
            f"{ast.get('sd_why','?')} - target ${fmt_px(tp)} ({HA_RR}R), "
            f"stop ${fmt_px(stop)}")
        return fire_entry(asset, ast, side, dict(c, c=entry), stop,
                          c["h"], c["l"], "STOCHDOJI",
                          f"{ast.get('sd_why','?')} - {HA_RR}R target or the "
                          f"stop",
                          live_px=candles[-1]["c"], engine="sd",
                          candles=candles, idx=i, tp_override=tp)

    if CROSS_MODE:
        ast["sym"] = sym
        side = cross_signal(ast, candles, ha, i)
        if not side:
            ast["gate"] = cross_gate(ast, candles, ha, i)
            return False
        want_long = side == "LONG"
        if not ALLOW_SHORTS and not want_long:
            return False
        # INTRABAR enters at the LIVE price. c["o"] would be the price at the
        # top of the 30m bar, which is not where the cross happened.
        entry = (c["c"] if (CROSS_INTRABAR and not CROSS_NEEDS_NOWICK)
                 else (c["o"] if ENTRY_AT_OPEN else c["c"]))
        if CROSS_STOP_SWING:
            lo = max(0, i - stop_bars())
            win = candles[lo:i]
            if not win:
                return False
            stop = (min(x["l"] for x in win) if want_long
                    else max(x["h"] for x in win))
            if stop == entry:
                return False
        else:
            stop = (entry * (1 - CROSS_DISASTER_PCT / 100) if want_long
                    else entry * (1 + CROSS_DISASTER_PCT / 100)) \
                if CROSS_DISASTER_PCT else (entry * 0.5 if want_long
                                            else entry * 1.5)
        risk_t = abs(entry - stop)
        if MIN_STOP_PCT and entry and risk_t / entry * 100 < MIN_STOP_PCT:
            if LOG_SKIPS:
                log(f"{sym}: cross {side} stop {risk_t / entry * 100:.3f}% "
                    f"under the {MIN_STOP_PCT}% floor - skipped")
            return False
        # A REAL 1.5R target. The cross-back exit is gated off, so the only
        # ways out are this and the stop.
        tp = ((entry + HA_RR * risk_t) if want_long
              else (entry - HA_RR * risk_t))
        log(f"{sym}: CROSS ENTRY {side} at ${fmt_px(entry)} - "
            f"{ast.get('cross_why','?')}, vol {c.get('v', 0)} - target "
            f"${fmt_px(tp)} ({HA_RR}R), stop ${fmt_px(stop)}")
        return fire_entry(asset, ast, side, dict(c, c=entry), stop,
                          c["h"], c["l"], "CROSS",
                          f"{ast.get('cross_why','?')} - {HA_RR}R target, "
                          f"the stop, or {CROSS_MAX_BARS} bars",
                          live_px=candles[-1]["c"], engine="cross",
                          candles=candles, idx=i, tp_override=tp)

    # record where this symbol sits in the chain, for the dashboard. Report
    # only - it never gates anything. Written every scan so the panel is
    # never staler than the last candle close.
    try:
        g = gate_status(ha, candles, i, sym, ast)
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
        # DECIDE THE SIDE FIRST. The EMA must be tested against the side
        # ACTUALLY TRADED, not the signal's own reading. Under
        # "continuation" those are OPPOSITE, so testing signal_long asked
        # "is this ok for a long?" and then took a short - which is how ACE
        # came to be sold ABOVE its EMA on 7 Aug.
        want_long = signal_long if HA_MODE == "reversal" else not signal_long
        direction = "LONG" if want_long else "SHORT"
        # THE 50 EMA DECIDES WHICH SIDE IS ALLOWED. Shorts only below it,
        # longs only above.
        ok_ema, ema_v, ema_px = ema_side(candles, i, want_long)
        if not ok_ema:
            gap = (abs(ema_px - ema_v) / ema_v * 100) if ema_v else 0
            slope = ema_slope(candles, i)
            with_trend = (slope if want_long else -slope) if slope is not None else None
            if (ema_px > ema_v) != want_long:
                why = "on the wrong side of"
            elif (EMA_SLOPE_PCT and with_trend is not None
                    and with_trend < EMA_SLOPE_PCT):
                # THE RANGE FILTER, not the retest band. Without this branch
                # the line read "0.61% from (needs <= 0.75%) - skipped",
                # which contradicts itself and sent us hunting the wrong gate.
                why = (f"in a RANGE ({with_trend:+.2f}% slope over "
                       f"{EMA_SLOPE_BARS} x {EMA_SLOPE_TF}, needs "
                       f"{EMA_SLOPE_PCT:+.2f}%) around")
            else:
                why = f"{gap:.2f}% from (needs <= {EMA_RETEST_PCT}%)"
            if LOG_SKIPS:
                log(f"{sym}: {direction} setup but "
                    f"price ${fmt_px(ema_px)} is {why} the "
                    f"{EMA_FILTER_LEN} EMA ${fmt_px(ema_v)} - skipped")
            continue
        d = found[0]                         # the FLIP bar - what the stop
        #                                      window sits behind
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
        if not ALLOW_SHORTS and not want_long:
            if LOG_SKIPS:
                log(f"{sym}: SHORT setup but ALLOW_SHORTS is off - skipped")
            continue

        # ONE PER TREND. The key is the EMA cross that started this side, so
        # a second setup inside the same move is refused - it is the same
        # trend, not a new one.
        # TOO LATE IN THE TREND. Distinct from ONE_PER_TREND: that one asks
        # "have we already traded this trend?", this asks "is this still the
        # BEGINNING of it?". A setup fourteen hours after the cross is
        # mid-trend even if nothing has fired.
        if TREND_MAX_AGE:
            tk0 = trend_start_t(candles, i)
            if tk0 is not None:
                age_bars = sum(1 for c in candles[:i + 1] if c["t"] >= tk0) - 1
                if age_bars > TREND_MAX_AGE:
                    if LOG_SKIPS:
                        log(f"{sym}: {direction} setup but the trend is "
                            f"{age_bars} bars old (max {TREND_MAX_AGE}) - "
                            "skipped")
                    continue

        if ONE_PER_TREND:
            tkey = trend_start_t(candles, i)
            done = ast.get("trend_taken") or {}
            if tkey and done.get("t") == tkey and done.get("dir") == direction:
                continue

        # THE WHOLE RUN MUST HAVE BEEN ON THIS SIDE, not just the entry bar.
        # found[1] is the run start, i-1 the no-wick candle.
        ok_run, bad = ema_run_side(candles, found[1], i - 1, want_long)
        if not ok_run:
            if LOG_SKIPS:
                log(f"{sym}: {direction} setup but {bad} of the run's candles "
                    f"sat on the wrong side of the {EMA_FILTER_LEN} EMA - "
                    "skipped")
            continue

        # THE 30-DAY TREND. A long into a multi-week decline is refused
        # whatever the setup looks like on the hour.
        ok_lt, chg = long_trend_ok(asset, want_long)
        if not ok_lt:
            if LOG_SKIPS:
                log(f"{sym}: {direction} setup but the {EMA_FILTER_LEN} EMA "
                    f"is {chg:+.2f}% over {LONG_LOOKBACK_DAYS} days - "
                    "skipped")
            continue

        # HIGHER-TIMEFRAME PERMISSION. A neutral or unreadable regime blocks
        # nothing; only an ACTIVE disagreement refuses the trade.
        if REGIME_ON:
            rg = regime_side(asset)
            if rg and (rg > 0) != want_long:
                if LOG_SKIPS:
                    log(f"{sym}: {direction} setup but the {REGIME_TF} trend is "
                        f"{'UP' if rg > 0 else 'DOWN'} "
                        f"({REGIME_EMA_LEN} EMA) - skipped")
                continue

        # BITCOIN CORRELATION. BTC cannot follow itself, and a NEUTRAL BTC
        # blocks nothing - only an actively trending BTC gets a vote.
        if BTC_ALIGN and sym != "BTC":
            bias = btc_bias()
            if bias and (bias > 0) != want_long:
                log(f"{sym}: {direction} setup but BITCOIN is trending "
                    f"{'up' if bias > 0 else 'down'} "
                    f"(needs {BTC_ALIGN_PCT:+.2f}% slope) - skipped")
                continue

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
        log(f"{sym}: HA {'down' if signal_long else 'up'}trend flipped "
            f"{'green' if signal_long else 'red'} - taking {direction} "
            f"({HA_MODE}), stop at the {len(window)}-candle "
            f"{'low' if want_long else 'high'} "
            f"{'at the turn' if STOP_SOURCE == 'turn' else 'before the flip'} "
            f"${fmt_px(stop)} (flip was {fmt_ts(ha[d]['t'])}, run began "
            f"{fmt_ts(ha[found[1]]['t'])})")
        # candles[-1] is the still-forming candle: its close is the current
        # price, free, with no extra API call
        fire_entry(asset, ast, direction, c, stop, c["h"], c["l"], "HA",
                   # DESCRIBE THE BARS, NOT THE SIDE. signal_long is the
                   # RUN's own reading (True = a red run flipped green);
                   # want_long is what HA_MODE decided to do about it.
                   # Building this from want_long printed "uptrend faded,
                   # flipped red" on a SHORT that was actually a downtrend
                   # pausing - the exact opposite bars. Live on ACE, 7 Aug.
                   # UNDER NOWICK_ONLY there is no run and no flip to
                   # describe - saying there was made the alert claim a
                   # pattern the engine had stopped requiring.
                   ((f"a no-wick {'green' if want_long else 'red'} candle "
                     f"{'above' if want_long else 'below'} the "
                     f"{EMA_FILTER_LEN} EMA - entered at the next open, "
                     f"stop at the {len(window)}-candle extreme, "
                     f"{'low' if want_long else 'high'}")
                    if NOWICK_ONLY else
                    (f"HA {'down' if signal_long else 'up'}trend "
                     f"{'faded' if HA_MODE == 'reversal' else 'paused'}, "
                     f"flipped {'green' if signal_long else 'red'}, then a "
                     f"no-wick candle - "
                     f"{'reversing it' if HA_MODE == 'reversal' else 'joining the run'}"
                     f" at the next open, stop at the "
                     f"{len(window)}-candle extreme before the flip, "
                     f"{'low' if want_long else 'high'}")),
                   live_px=candles[-1]["c"], engine=ENGINE_TAG,
                   candles=candles, idx=i)
        return True

    # ---------------- BREAKOUT, the second engine ----------------
    # It takes the markets the HA engine CANNOT: those whose EMA slope is
    # too flat for ema_side to call a trend. The two never compete for a
    # symbol, because this runs only where that filter refuses both sides.
    if BREAKOUT_ON:
        sl = ema_slope(candles, i)
        drifting = (sl is None or abs(sl) < EMA_SLOPE_PCT) if EMA_SLOPE_PCT \
            else False
        found_bo = breakout_signal(candles, i) if drifting else None
        if found_bo:
            want_long, stop = found_bo
            direction = "LONG" if want_long else "SHORT"
            # ALLOW_SHORTS is a RULE, not a flip-engine setting. It lived
            # only in the flip path, so breakout shorts walked straight past
            # it - HYPE opened SHORT on 9 Aug with the switch off.
            if not ALLOW_SHORTS and not want_long:
                log(f"{sym}: BREAKOUT SHORT but ALLOW_SHORTS is off - skipped")
                return False
            entry = candles[i]["c"]          # the BREAK's own close
            # the range start is a stable key for one trade per range
            bo_f = max(1, MS.get(BO_TF, MS[TF]) // MS[TF])
            rt = candles[max(0, i - BO_LOOKBACK * bo_f)]["t"]
            tr = ast.get("traded") or {}
            if tr.get("ft") == rt and tr.get("dir") == direction:
                return False
            if MAX_STOP_PCT:
                far = abs(entry - stop) / entry * 100 if entry else 0
                if far > MAX_STOP_PCT:
                    stop = (entry * (1 - MAX_STOP_PCT / 100) if want_long
                            else entry * (1 + MAX_STOP_PCT / 100))
            rng = abs(candles[i]["c"] - stop) / entry * 100 if entry else 0
            log(f"{sym}: BREAKOUT {direction} - closed beyond the "
                f"{BO_LOOKBACK} x {BO_TF} range at ${fmt_px(entry)}, stop at "
                f"the "
                f"far edge ${fmt_px(stop)} ({rng:.2f}%), EMA slope "
                f"{sl if sl is not None else 0:+.2f}% (flat)")
            ast["setup"] = {"dir": direction, "zhi": candles[i]["h"],
                            "zlo": candles[i]["l"], "ft": rt,
                            "departed": True, "touched": True,
                            "frozen": True, "t": candles[i]["t"]}
            if fire_entry(asset, ast, direction, candles[i], stop,
                          candles[i]["h"], candles[i]["l"], "BREAKOUT",
                          f"closed beyond the {BO_LOOKBACK} x {BO_TF} "
                          f"{'high' if want_long else 'low'} of a range under "
                          f"{BO_TIGHT_PCT}% wide - stop at the far edge",
                          live_px=candles[-1]["c"], engine=BO_TAG,
                          candles=candles, idx=i):
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
                # ASK THE VENUE where it actually closed, rather than
                # booking at whatever the candle happens to read now.
                fill_px, real_pnl = closing_fill(asset, ast["trade"])
                px = fill_px if fill_px is not None else cs[-1]["c"]
                kind = gone_kind(ast["trade"], fill_px)
                src = "the exchange fill" if fill_px is not None else "the candle"
                log(f"{sym}: tracked {ast['trade']['verdict']} but the "
                    f"exchange is FLAT - booking {kind} at ${fmt_px(px)} "
                    f"(from {src}"
                    + (f", venue pnl ${real_pnl:+.2f}" if fill_px is not None
                       else "") + ")")
                record_close(sym, ast["trade"], px, kind, now_ms(),
                             frac=ast["trade"].get("left", 1.0))
                # ALWAYS ALERT. Gating this on GONE meant a close the venue
                # had already made - a TP or a STOP - was booked in SILENCE,
                # because this path uses record_close directly and never
                # reaches the lifecycle alert that _close_trade sends. He
                # got nothing when XPL closed.
                try:
                    if kind == "GONE":
                        send_telegram(
                            f"\u26a0\ufe0f <b>{esc(sym)}</b> was tracked "
                            f"{ast['trade']['verdict']} but the exchange is "
                            f"flat - booked closed at "
                            f"<code>${fmt_px(px)}</code>")
                    elif ALERT_LIFECYCLE:
                        note = "closed on the exchange" + (
                            f", venue pnl ${real_pnl:+.2f}"
                            if fill_px is not None else "")
                        send_telegram(lifecycle_message(
                            asset, kind, ast["trade"], px, now_ms(), note))
                except Exception as e:
                    log(f"{sym}: {kind} alert failed: {type(e).__name__}")
                cancel_stale_orders(asset, ast["trade"])
                ast["trade"] = None
                ast["phase"] = "SCAN"
                state[sym] = ast
                RUN_STATUS.append(f"{sym} booked {kind}")
                return True
        if cs:
            trade, ch = process_open_trade(asset, ast["trade"], cs,
                                           smoothed_ha(cs), cs[-2]["t"])
            ast["trade"] = trade
            changed = changed or ch
            if trade is None:
                ast["phase"] = "SCAN"
        # ARM FLIPS THAT HAPPEN MID-TRADE. Must run BEFORE the return below,
        # which is what locked the entry path out for the whole trade.
        if (FLIP_MODE or FLIP_EXIT) and cs and len(cs) >= 3:
            update_flip_arm(ast, smoothed_ha(cs), len(cs) - 1)
        # exits win over overrides (the watch ran first). Fall through to the
        # candle walk when the trade just closed, or when overrides are on.
        if ast["trade"]:
            RUN_STATUS.append(f"{sym} IN_TRADE")
            state[sym] = ast
            return changed

    # ---- skip the fetch entirely when no new candle can exist ------------
    # ~60 markets re-fetched every pulse is what triggers HTTP 429. A symbol
    # with no open trade has nothing new to say until its next candle closes.
    if (cs is None and not ast["trade"]
            and not (CROSS_MODE and CROSS_INTRABAR and not CROSS_NEEDS_NOWICK)
            and not (RS_MODE and RS_INTRABAR)):
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
    last_eval = ((len(cs) - 1)
                 if (ENTRY_AT_OPEN or (RS_MODE and RS_INTRABAR))
                 else last_closed)
    # The cutoff must sit strictly BEHIND last_eval. With ENTRY_AT_OPEN off
    # last_eval IS last_closed, and a cutoff on that same bar made the loop
    # below unsatisfiable - it needs t > last_candle_t AND i <= last_eval, and
    # no bar could be both. process_candle then never ran at all: no arms, no
    # gates, no signals, and nothing in the log to say so. 18 Aug.
    cutoff = cs[max(0, last_eval - 1)]["t"] - REPLAY_CANDLES * MS[TF]
    if ast["last_candle_t"] < cutoff:
        ast["last_candle_t"] = cutoff
    for i in range(len(cs)):
        # INTRABAR: the forming bar must be re-judged on EVERY pulse, so the
        # last_candle_t high-water mark cannot be allowed to skip it. Without
        # this the forming bar is evaluated exactly ONCE, on the first pulse
        # after it opens, and a cross twelve minutes into the bar is invisible.
        recheck = (((CROSS_MODE and CROSS_INTRABAR and not CROSS_NEEDS_NOWICK)
                    or (RS_MODE and RS_INTRABAR))
                   and i == len(cs) - 1 and not ast["trade"])
        if i > last_eval:
            continue
        if cs[i]["t"] <= ast["last_candle_t"] and not recheck:
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
                              # the dashboard cannot guess whether a target
                              # books HALF or the WHOLE position, and said
                              # "booking half" on a full close for two days
                              partial=HA_PARTIAL,
                              # the dashboard froze cards at "stop hit,
                              # closing" and waited forever for a close the
                              # agent never makes when STOP_EXIT is off
                              stop_exit=bool(STOP_EXIT),
                              # ...and the SAME failure returns when the stop
                              # is CLOSE-CONFIRMED: touching the level closes
                              # nothing, so the dashboard must not latch the
                              # card on a touch. CASHCAT and xyz:SKHX sat at
                              # "stop hit - closing" for hours on 17 Aug while
                              # the agent was correctly still holding them.
                              stop_on_close=bool(SD_STOP_ON_CLOSE and SD_MODE),
                              # the fill is at the entry bar's CLOSE when
                              # ENTRY_AT_OPEN is off, so the card's age needs
                              # one candle added - opened_t is the bar's OPEN.
                              # Published rather than assumed, because adding
                              # it unconditionally left 4h cards reading
                              # "just now" for four hours.
                              entry_on_close=not bool(ENTRY_AT_OPEN),
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
    log(f"{engine_label()} agent started (loop mode). Ctrl+C to stop.")
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
