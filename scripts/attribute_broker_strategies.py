#!/usr/bin/env python3
"""
Attribute each REAL broker round-trip (broker-ledger.json) to a strategy
(BASE / PARTIAL / MOMENTUM) by joining to the tagged journal alerts
(journal-data.json) on entry-time + direction, disambiguating with the
per-strategy bracket signature (BASE TP 650 / PARTIAL TP 325 / MOM TP 475 / MOM SL -175).

Writes non-destructive fields on each broker trade:
  strategy      : "base" | "partial" | "momentum" | None
  strategy_conf : "high" | "med" | "low"
  strategy_src  : "journal" | "signature" | "journal+sig"
Idempotent: recomputes from scratch each run.
"""
import json, sys, os
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROKER = os.path.join(REPO, "docs/trading/broker-ledger.json")
JOURNAL = os.path.join(REPO, "docs/trading/journal-data.json")

# bracket signatures (ticks)
SIG = {"base": {"tp": 650, "sl": -150}, "partial": {"tp": 325, "sl": -150}, "momentum": {"tp": 475, "sl": -175}}

def la_minute(s):
    # "28/06/2026 22:15:02" or "30/06/2026 15:11" -> "DD/MM/YYYY HH:MM"
    if not s: return None
    p = s.strip().split(" ")
    if len(p) < 2: return None
    hm = ":".join(p[1].split(":")[:2])
    return p[0] + " " + hm

def sig_strategy(ticks):
    """best strategy guess purely from the exit-tick signature. returns (strat, conf) or (None,None)."""
    if ticks is None: return (None, None)
    t = float(ticks)
    # clear TP hits
    if t >= 560: return ("base", "high")       # ~650
    if 400 <= t < 560: return ("momentum", "high")  # ~475
    if 250 <= t < 400: return ("partial", "high")   # ~325
    # SLs
    if -190 <= t <= -160: return ("momentum", "high")  # ~-175
    # -150 is base OR partial -> ambiguous by signature
    if -160 < t <= -140: return (None, "low")
    return (None, None)  # swap / small -> no signature info

def main():
    broker = json.load(open(BROKER, encoding="utf-8"))
    journal = json.load(open(JOURNAL, encoding="utf-8"))
    jtrades = journal.get("trades", [])

    # index journal events by (entry-minute, direction) -> set of strategies
    jidx = {}
    for e in jtrades:
        m = la_minute(e.get("_entry_time_la"))
        d = (e.get("direction") or e.get("dir") or "").lower()
        s = (e.get("strategy") or "").lower()
        if not m or not s: continue
        jidx.setdefault((m, d), {})
        # store strategy -> its journal result_ticks (helps signature match)
        jidx[(m, d)][s] = e.get("result_ticks")

    # ---- GROUP-based assignment (Lauren's insight 2026-06-30) ----
    # BASE & PARTIAL ALWAYS co-enter (same entry). They differ on the WIN exit
    # (650 vs 325) and on MOMENTUM (475 / -175). On a LOSS/SWAP base & partial exit
    # together at the SAME value -> it doesn't matter which label; assign one to each.
    # So: group broker trades by (entry-minute, direction); assign signature-clear
    # trades first; then pair the leftovers 1:1 to the remaining strategies.
    from collections import defaultdict
    groups = defaultdict(list)
    for t in broker.get("trades", []):
        groups[(la_minute(t.get("entry_la")), (t.get("direction") or "").lower())].append(t)

    tagged = {"signature": 0, "paired": 0, "none": 0}
    for (m, d), trs in groups.items():
        # strategies that journal says fired this minute (if any)
        jstrats = set()
        for dm in (0, 1, -1, 2, -2):
            key = None
            if m:
                try:
                    dd, tt = m.split(" "); hh, mm = tt.split(":")
                    mm2 = int(mm) + dm; hh2 = int(hh)
                    if mm2 < 0: mm2 += 60; hh2 -= 1
                    if mm2 > 59: mm2 -= 60; hh2 += 1
                    if 0 <= hh2 <= 23: key = (f"{dd} {hh2:02d}:{mm2:02d}", d)
                except Exception: key = None
            if key and key in jidx:
                jstrats = set(jidx[key].keys()); break

        # pass 1: signature-clear assignment
        for t in trs:
            sig_s, _ = sig_strategy(t.get("result_ticks"))
            t["strategy"] = sig_s
            t["strategy_conf"] = "high" if sig_s else None
            t["strategy_src"] = "signature" if sig_s else None
            if sig_s: tagged["signature"] += 1

        # pass 2: pair leftovers to remaining strategies
        assigned = {t["strategy"] for t in trs if t.get("strategy")}
        # candidate strategies present this group: journal set, else default base+partial
        present = jstrats if jstrats else {"base", "partial"}
        # if a leftover count > present-assigned, include base/partial as the natural pair
        leftover_trades = [t for t in trs if not t.get("strategy")]
        remaining = [s for s in ["base", "partial", "momentum"] if s in present and s not in assigned]
        # base & partial co-enter: ensure both available as pairing slots when 2 leftovers
        if len(leftover_trades) >= 1 and not remaining:
            remaining = [s for s in ["base", "partial"] if s not in assigned] or ["base", "partial"]
        for i, t in enumerate(leftover_trades):
            if remaining:
                t["strategy"] = remaining[i % len(remaining)]
                # loss/swap identical for base&partial -> value is exact; label is the symmetric pair
                t["strategy_conf"] = "med"
                t["strategy_src"] = "paired"
                tagged["paired"] += 1
            else:
                t["strategy_conf"] = "low"; t["strategy_src"] = None
                tagged["none"] += 1

    # per-strategy real net (only tagged)
    per = {}
    for t in broker.get("trades", []):
        s = t.get("strategy")
        if not s: continue
        per.setdefault(s, {"n": 0, "net": 0.0, "w": 0})
        per[s]["n"] += 1; per[s]["net"] += t.get("result_dollars", 0) or 0
        if (t.get("result_dollars", 0) or 0) > 0: per[s]["w"] += 1
    broker["_strategy_attribution"] = {"method": "journal-time-join + bracket-signature", "tagged": tagged, "per_strategy": per, "_computed_at": datetime.utcnow().isoformat()+"Z"}

    json.dump(broker, open(BROKER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("attribution:", tagged)
    for s, v in per.items():
        print(f"  {s:9s}: {v['n']:2d} trades  net ${v['net']:+.0f}  win {round(v['w']/v['n']*100) if v['n'] else 0}%")

if __name__ == "__main__":
    main()
