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

    tagged = {"journal": 0, "signature": 0, "journal+sig": 0, "none": 0}
    for t in broker.get("trades", []):
        m = la_minute(t.get("entry_la"))
        d = (t.get("direction") or "").lower()
        ticks = t.get("result_ticks")
        cands = {}
        # try exact minute, then +/-1, +/-2 minute
        for dm in (0, 1, -1, 2, -2):
            key = None
            if m:
                try:
                    dd, tt = m.split(" "); hh, mm = tt.split(":")
                    mm2 = int(mm) + dm
                    hh2 = int(hh)
                    if mm2 < 0: mm2 += 60; hh2 -= 1
                    if mm2 > 59: mm2 -= 60; hh2 += 1
                    if 0 <= hh2 <= 23:
                        key = (f"{dd} {hh2:02d}:{mm2:02d}", d)
                except Exception:
                    key = None
            if key and key in jidx:
                cands = jidx[key]; break

        sig_s, sig_c = sig_strategy(ticks)
        strat = None; conf = None; src = None
        if cands:
            if len(cands) == 1:
                strat = list(cands.keys())[0]; conf = "high"; src = "journal"
            else:
                # multiple strategies fired same minute -> disambiguate by signature
                if sig_s and sig_s in cands:
                    strat = sig_s; conf = "high"; src = "journal+sig"
                else:
                    # signature can't split -> pick by closest journal result_ticks to broker ticks
                    if ticks is not None:
                        best = min(cands.items(), key=lambda kv: abs((kv[1] or 0) - ticks))
                        strat = best[0]; conf = "med"; src = "journal+sig"
                    else:
                        strat = None; conf = "low"; src = "journal"
        elif sig_s:
            strat = sig_s; conf = sig_c; src = "signature"

        t["strategy"] = strat
        t["strategy_conf"] = conf
        t["strategy_src"] = src
        if strat and src == "journal": tagged["journal"] += 1
        elif strat and src == "signature": tagged["signature"] += 1
        elif strat and src == "journal+sig": tagged["journal+sig"] += 1
        else: tagged["none"] += 1

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
