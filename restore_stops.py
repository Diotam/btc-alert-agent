#!/usr/bin/env python3
"""
Re-place the protective stop (and take-profit) for every position that is
open on Hyperliquid but has no orders behind it.

    cd /root && set -a && . /opt/btc-agent/env && set +a
    python3 restore_stops.py            # dry run - shows what it would send
    python3 restore_stops.py --apply

Built after a cleanup script cancelled every protective order on the account
because its position lookup came back empty. Entering nine orders by hand is
error-prone, so this sends them through the same code path the agent uses.

SAFETY
------
* Sizes come from the EXCHANGE position, not from state, so a partial fill
  or a hand-adjusted size cannot leave the stop oversized.
* Levels come from state - they are the structural levels the strategy
  chose.
* Anything already carrying an order is left alone.
* A symbol in state with no exchange position is skipped entirely.
* Nothing is sent without --apply.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/opt/btc-agent")
import btc_alert_agent as ag  # noqa: E402

APPLY = "--apply" in sys.argv
DEXES = [""] + [d for d in ag.EXEC_BUILDER_DEXES if d]


def api(payload):
    r = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=8).read())


def query(kind):
    """Merge a query across the main dex and every builder dex."""
    out = []
    addr = os.environ["HL_ACCOUNT_ADDRESS"]
    for dex in DEXES:
        p = {"type": kind, "user": addr}
        if dex:
            p["dex"] = dex
        try:
            r = api(p)
        except Exception as e:
            raise SystemExit(f"{kind} on {dex or 'main'} failed "
                             f"({type(e).__name__}: {e}) - aborting rather "
                             "than acting on a partial picture")
        out.append(r)
    return out


def main():
    # positions, keyed by the exchange's own coin name
    held = {}
    for st in query("clearinghouseState"):
        for pos in st.get("assetPositions", []):
            q = pos.get("position", {})
            szi = float(q.get("szi") or 0)
            if abs(szi) > 1e-12:
                held[q["coin"]] = szi
    if not held:
        raise SystemExit("no open positions found - nothing to protect "
                         "(and refusing to act on an empty lookup)")

    covered = set()
    for lst in query("openOrders"):
        for o in lst:
            covered.add(o.get("coin"))

    state = json.loads(ag.STATE_FILE.read_text())

    print(f"{'symbol':14} {'position':>11} {'stop':>12} {'tp':>12}  action")
    todo = []
    for sym, v in sorted(state.items()):
        if not isinstance(v, dict):
            continue
        t = v.get("trade")
        if not t:
            continue
        coin = ag.exec_symbol(sym)
        szi = held.get(coin)
        if szi is None:
            print(f"{coin:14} {'-':>11} {'':>12} {'':>12}  no position, skip")
            continue
        if coin in covered:
            print(f"{coin:14} {szi:>11} {t['stop']:>12} {t['tp']:>12}  "
                  "already has orders, skip")
            continue
        todo.append((coin, sym, t, szi))
        half = "" if t.get("half") else f" + TP {ag.HA_PARTIAL:.0%}"
        print(f"{coin:14} {szi:>11} {t['stop']:>12} {t['tp']:>12}  "
              f"PLACE stop{half}")

    if not todo:
        print("\nnothing to do")
        return
    if not APPLY:
        print(f"\nDRY RUN - {len(todo)} symbol(s) would get orders. "
              "Re-run with --apply")
        return

    ex = ag.exec_client()
    if not ex:
        raise SystemExit("no execution client")
    for coin, sym, t, szi in todo:
        long_ = szi > 0
        size = abs(szi)
        dec = ag.sz_decimals(sym)
        try:
            r = ex.order(coin, not long_, size, ag.round_px(t["stop"]),
                         {"trigger": {"triggerPx": ag.round_px(t["stop"]),
                                      "isMarket": True, "tpsl": "sl"}},
                         reduce_only=True)
            print(f"  {coin} stop {size} @ {t['stop']}: "
                  f"{ag.resp_error(r) or ag.order_error(r) or 'ok'}")
        except Exception as e:
            print(f"  {coin} stop FAILED {type(e).__name__}: {e}")
        if t.get("half"):
            continue                      # a runner has no target left
        part = round(size * ag.HA_PARTIAL, dec)
        try:
            r = ex.order(coin, not long_, part, ag.round_px(t["tp"]),
                         {"limit": {"tif": "Gtc"}}, reduce_only=True)
            print(f"  {coin} TP   {part} @ {t['tp']}: "
                  f"{ag.resp_error(r) or ag.order_error(r) or 'ok'}")
        except Exception as e:
            print(f"  {coin} TP FAILED {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
