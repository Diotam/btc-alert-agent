#!/usr/bin/env python3
"""Funnel + CONFIRMATION detail.

    python3 /opt/btc-agent/funnel2.py

For every market that finds a doji but cannot assemble two clean candles,
prints each bar after the doji with its wick sizes as a % of its own body,
and why it was skipped. That tells you whether to loosen HA_NOWICK_TOL_PCT
or widen SD_CONFIRM_WINDOW - rather than guessing.

  col   HA colour
  up%   upper wick as a % of the body
  dn%   lower wick as a % of the body
  verdict  COUNTS / skip-both / skip-wick / RESET (wrong colour)
"""
import sys
sys.path.insert(0, '/opt/btc-agent')
import btc_alert_agent as A

TOL = A.HA_NOWICK_TOL_PCT
stage = {"no doji": 0, "confirms short": 0, "stoch": 0, "momentum": 0,
         "SIGNAL": 0, "error": 0}
near = []          # wick ratios that JUST missed, for calibration

for a in A.active_assets():
    sym = a["symbol"]
    try:
        _, cs = A.fetch(a, A.TF, 300)
        ha = A.smoothed_ha(cs)
        i = len(cs) - 1
        last = i - 1
        dojis = [d for d in range(last - 1, last - 1 - A.SD_CONFIRM_WINDOW, -1)
                 if d > 1 and A.sd_is_doji(ha, d)]
        if not dojis:
            stage["no doji"] += 1
            continue
        d = dojis[0]
        want_long = not A.ha_green(ha[d - 1])
        if A.sd_signal({"sym": sym}, cs, ha, i):
            stage["SIGNAL"] += 1
            continue
        crossed, _ = A.stoch_crossed(cs, last, want_long)
        wk, _ = A.sd_momentum_weak(cs, ha, d, want_long)
        if not crossed:
            stage["stoch"] += 1
            continue
        if not wk:
            stage["momentum"] += 1
            continue

        # it died at the confirmations - show every bar after the doji
        stage["confirms short"] += 1
        print(f"\n{sym}  want {'LONG' if want_long else 'SHORT'}  "
              f"doji at bar {d}, {last - d} bars since  (tol {TOL}% of body)")
        print(f"    {'bar':>4}{'col':>5}{'up%':>9}{'dn%':>9}   verdict")
        count = 0
        for k in range(d + 1, last + 1):
            c = ha[k]
            body = A.ha_body(c)
            if body <= 0:
                print(f"    {k:>4}{'-':>5}{'':>9}{'':>9}   zero body, skipped")
                continue
            up = A.ha_wick(c, upper=True) / body * 100
            dn = A.ha_wick(c, upper=False) / body * 100
            against = dn if want_long else up
            col = "g" if A.ha_green(c) else "r"
            if up > TOL and dn > TOL:
                v = "skip-both"
                near.append(against)
            elif A.ha_green(c) != want_long:
                v = "RESET (wrong colour)"
            elif against > TOL:
                v = "skip-wick"
                near.append(against)
            else:
                count += 1
                v = f"COUNTS ({count})"
            print(f"    {k:>4}{col:>5}{up:>8.1f}%{dn:>8.1f}%   {v}")
        print(f"    -> {count} clean of {A.SD_CONFIRM_BARS} needed")
    except Exception:
        stage["error"] += 1

print()
for k2, v in stage.items():
    print(f"{k2:16} {v}")
if near:
    near.sort()
    print(f"\nwick ratios that MISSED the {TOL}% tolerance, "
          f"{len(near)} of them:")
    print("  median %.0f%%   25th %.0f%%   10th %.0f%%"
          % (near[len(near)//2], near[len(near)//4], near[len(near)//10]))
    print("  -> setting HA_NOWICK_TOL_PCT near the 25th percentile would "
          "recover about a quarter of them")
