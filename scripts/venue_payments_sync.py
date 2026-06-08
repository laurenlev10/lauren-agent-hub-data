#!/usr/bin/env python3
"""venue_payments_sync.py — agent-owned slice of docs/state/venue_payments.json.

Lauren 2026-06-08: the contract dashboard tracks venue (hall) payments + deposits
per event. This script fills the QB-actuals side: for every event in
[today-45d .. today+400d] it pulls QuickBooks expenses by Class with a WIDE
per-event window (start-400d .. end+30d — hall deposits are paid months early),
keeps only category == "venue" lines, and writes:

    events.<evkey>.qb_lines        [{date, vendor, amount, account}]
    events.<evkey>.qb_paid_total   float
    events.<evkey>.venue / class_name / start_date   (refresher metadata)
    events.<evkey>.qb_synced_at

Ownership partition (mirror of slow_movers.json convention):
    agent (this script):  qb_lines, qb_paid_total, qb_synced_at, venue, class_name, start_date
    browser (dashboard):  contract_total, deposits[], note, updated_at
MERGE-on-write per IRON RULE #18 — never clobber browser-owned fields.
Runs inside qb-untagged-refresh.yml (daily) — failures there SMS Lauren (IRON RULE #3).
"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
OUT = ROOT / "docs/state/venue_payments.json"


def main():
    import pnl_quickbooks as PQ
    today = dt.date.today()
    events = json.loads((ROOT / "docs/state/events_index.json").read_text(encoding="utf-8"))["events"]
    targets = [e for e in events
               if -45 <= (dt.date.fromisoformat(e["start_date"]) - today).days <= 400]
    try:
        state = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state.setdefault("events", {})
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    synced = errors = 0
    for e in targets:
        evkey, cls = e["evkey"], e["class_name"]
        start = dt.date.fromisoformat(e["start_date"])
        end = dt.date.fromisoformat(e["end_date"])
        try:
            qbd = PQ.fetch_qb_expenses(cls,
                                       date_from=(start - dt.timedelta(days=400)).isoformat(),
                                       date_to=(end + dt.timedelta(days=30)).isoformat())
        except Exception as ex:
            print(f"  ✗ {evkey}: {str(ex)[:100]}", flush=True)
            errors += 1
            continue
        vlines = [{"date": l["date"], "vendor": l["vendor"], "amount": l["amount"], "account": l["account"]}
                  for l in qbd.get("lines", []) if l.get("category") == "venue"]
        rec = state["events"].setdefault(evkey, {})          # MERGE — keep browser fields
        rec.update({"qb_lines": vlines,
                    "qb_paid_total": round(sum(l["amount"] for l in vlines), 2),
                    "qb_synced_at": now,
                    "venue": e.get("venue") or "", "class_name": cls,
                    "start_date": e["start_date"]})
        synced += 1
        if vlines:
            print(f"  ✓ {evkey}: {len(vlines)} venue lines · ${rec['qb_paid_total']:,.2f}", flush=True)
    state["_updated_at"] = now
    OUT.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"venue_payments: {synced} events synced · {errors} errors")
    # errors on individual events are soft; only total failure should alarm
    return 0 if synced or not targets else 1


if __name__ == "__main__":
    sys.exit(main())
