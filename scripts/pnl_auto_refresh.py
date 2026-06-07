#!/usr/bin/env python3
"""pnl_auto_refresh.py — keeps P&L pages alive for CURRENT and UPCOMING events
(Lauren 2026-06-08: "זה לא אמור להיות רק לאירועים בעבר").

Builds/refreshes docs/state/event_pnl/<evkey>.json for every event whose
start_date falls in [today-21d .. today+45d]. Expenses accrue long before the
event (inventory orders, flights, marketing, bookkeeping live entries) — the
page fills up as data arrives; sales stay pending until the weekend.
Runs daily inside qb-untagged-refresh.yml + on demand.
"""
from __future__ import annotations
import datetime as dt, json, sys, traceback
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_build as PB

def main():
    today = dt.date.today()
    lo, hi = today - dt.timedelta(days=21), today + dt.timedelta(days=45)
    events = json.loads((ROOT / "docs/state/events_index.json").read_text(encoding="utf-8"))["events"]
    targets = [e for e in events if lo <= dt.date.fromisoformat(e["start_date"]) <= hi]
    outdir = ROOT / "docs/state/event_pnl"
    outdir.mkdir(exist_ok=True)
    ok = err = 0
    for e in targets:
        evkey = e["evkey"]
        try:
            pnl = PB.build_pnl(evkey)
            # never clobber a finalized/manual historical file with an auto build
            old_p = outdir / f"{evkey}.json"
            if old_p.exists():
                try:
                    old = json.loads(old_p.read_text(encoding="utf-8"))
                    if old.get("historical"):
                        print(f"  skip {evkey} (historical/manual)"); continue
                except Exception: pass
            old_p.write_text(json.dumps(pnl, indent=2, ensure_ascii=False), encoding="utf-8")
            prof = pnl.get("profit_preliminary")
            print(f"  ✓ {evkey} · expenses ${pnl.get('total_known_expenses',0):,.0f} · profit {'$%,.0f' % prof if isinstance(prof,(int,float)) else 'pending'}")
            ok += 1
        except Exception as ex:
            print(f"  ✗ {evkey}: {str(ex)[:120]}")
            err += 1
    print(f"\nrefreshed {ok} · errors {err} · window {lo} .. {hi}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
