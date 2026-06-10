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
    lo = today - dt.timedelta(days=21)
    hi = max(dt.date(today.year, 12, 31), today + dt.timedelta(days=45))  # all of this year ahead — hotel deposits are paid months early
    events = json.loads((ROOT / "docs/state/events_index.json").read_text(encoding="utf-8"))["events"]
    targets = [e for e in events if lo <= dt.date.fromisoformat(e["start_date"]) <= hi]
    print(f"targets: {len(targets)} events · window {lo} .. {hi}", flush=True)
    outdir = ROOT / "docs/state/event_pnl"
    outdir.mkdir(exist_ok=True)
    ok = err = 0
    for e in targets:
        evkey = e["evkey"]
        future = dt.date.fromisoformat(e["start_date"]) > today
        ended = dt.date.fromisoformat(e.get("end_date") or e["start_date"]) < today
        old_p = outdir / f"{evkey}.json"
        old = None
        if old_p.exists():
            try:
                old = json.loads(old_p.read_text(encoding="utf-8"))
                if old.get("historical"):
                    print(f"  skip {evkey} (historical/manual)", flush=True); continue
            except Exception:
                old = None
        # Lauren 2026-06-10: sales (revenue + mystery box) are captured from OCTOPOS ONCE,
        # right after the event ends — NOT re-pulled every morning. Once captured
        # (sales_captured_at set), daily runs rebuild only the expense sources
        # (QB / manager / inventory / marketing) and reuse the frozen sales blocks.
        sales_frozen = bool(old and old.get("sales_captured_at"))
        try:
            pnl = PB.build_pnl(evkey, skip_sales=(future or sales_frozen))
            if sales_frozen:
                pnl["revenue"] = old.get("revenue") or pnl["revenue"]
                pnl["top_products"] = old.get("top_products") or []
                if not pnl.get("cash_check"):
                    pnl["cash_check"] = old.get("cash_check")
                det_old = old.get("detail") or {}
                pnl["detail"]["payment_breakdown"] = det_old.get("payment_breakdown", {})
                pnl["detail"]["mystery_box"] = det_old.get("mystery_box", {})
                if isinstance((old.get("expenses") or {}).get("mystery_box"), dict):
                    pnl["expenses"]["mystery_box"] = old["expenses"]["mystery_box"]
                pnl["sales_captured_at"] = old["sales_captured_at"]
                # recompute totals/profit with the frozen net + fresh expenses
                net = (pnl["revenue"] or {}).get("net_sales")
                known = [v["amount"] for v in pnl["expenses"].values()
                         if isinstance(v, dict) and isinstance(v.get("amount"), (int, float))]
                pnl["total_known_expenses"] = round(sum(known), 2) if known else 0.0
                pnl["profit_preliminary"] = (round(net - pnl["total_known_expenses"], 2)
                                             if isinstance(net, (int, float)) else None)
                pnl["margin"] = (round(pnl["profit_preliminary"] / net, 4)
                                 if (pnl["profit_preliminary"] is not None and net) else None)
                pnl["pending_or_missing"] = [k for k, v in pnl["expenses"].items()
                                             if isinstance(v, dict) and v.get("status") in ("pending", "missing", "incomplete")]
                pnl["preliminary"] = bool(pnl["pending_or_missing"])
            elif ended and isinstance((pnl.get("revenue") or {}).get("net_sales"), (int, float))                     and str((pnl.get("revenue") or {}).get("status")) == "ok":
                # first successful post-event capture — freeze from the next run onward
                pnl["sales_captured_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            old_p.write_text(json.dumps(pnl, indent=2, ensure_ascii=False), encoding="utf-8")
            prof = pnl.get("profit_preliminary")
            ptxt = f"${prof:,.0f}" if isinstance(prof, (int, float)) else "pending"
            print(f"  ✓ {evkey} · expenses ${pnl.get('total_known_expenses',0):,.0f} · profit {ptxt}", flush=True)
            ok += 1
        except BaseException as ex:           # SystemExit from OCTOPOS must not kill the whole loop
            print(f"  ✗ {evkey}: {type(ex).__name__}: {str(ex)[:120]}", flush=True)
            err += 1
    print(f"\nrefreshed {ok} · errors {err} · window {lo} .. {hi}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
