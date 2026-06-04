#!/usr/bin/env python3
"""pnl_build.py — assemble the automated event P&L from all sources.

Combines the source modules (event_summary_BUILD_BRIEF.md) into one P&L for an event:
  A. Sales      <- pnl_octopos      (gross/net/tx/avg + top products + cash)
  B. Inventory  <- pnl_inventory    (inventory + shipping, with completeness gate)
  C. Staff/Cash <- pnl_manager      (staff/meals/other + cash cross-check)
  D. Marketing  <- Meta (auto) + TikTok (pending review)   [injected]
  E. Travel/Venue/Other-nonCash <- QuickBooks (pending review)  [injected]

Design rules (from the @mbs-event-summary iron rules):
  - Revenue line = NET sales (tax is a pass-through, not income). Gross + tax shown alongside.
  - Shipping is its OWN line, never folded into Inventory or Other (IRON RULE #9).
  - Every expense line carries a SOURCE + STATUS. A source that's missing/pending is
    NOT silently treated as $0 — profit is marked "preliminary" and the gap is listed.
  - All cash expenses (staff/meals/other) come from the manager report (decided 2026-06-03);
    QB carries only non-cash (travel/venue).

Marketing (Meta) and QB are injected by the caller when available, so this module stays
runnable today with sources C/D/E pending. Usage:

    from scripts.pnl_build import build_pnl
    pnl = build_pnl("roseville-2026-05-22",
                    meta_spend=1234.0,        # optional; None => pending
                    tiktok_spend=None,        # None => pending (in review)
                    qb_expenses=None)         # None => pending (in review)
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

# import sibling source modules
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pnl_octopos, pnl_inventory, pnl_manager


def _repo_root():
    here = Path(__file__).resolve().parent.parent
    if (here / "docs/launch/index.html").exists():
        return here
    for c in Path("/").glob("**/docs/launch/index.html"):
        return c.parent.parent
    return here


def find_event(evkey, launch_html=None):
    """Pull event meta (city/state/dates/venue) from SCHEDULE in launch/index.html."""
    path = Path(launch_html) if launch_html else (_repo_root() / "docs/launch/index.html")
    html = path.read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\{[\s\S]*?\});", html)
    if not m:
        return None
    sched = json.loads(m.group(1))
    for year, evs in sched.items():
        if not isinstance(evs, list):
            continue
        for ev in evs:
            slug = (ev.get("city") or "").lower().replace(" ", "-")
            if f"{slug}-{ev.get('start_date')}" == evkey:
                ev["_year"] = year
                return ev
    return None


def _line(amount, source, status="ok", note=""):
    return {"amount": (round(amount, 2) if amount is not None else None),
            "source": source, "status": status, "note": note}


def build_pnl(evkey, *, launch_html=None, inv_state=None, mgr_state=None,
              meta_spend=None, tiktok_spend=None, qb_expenses=None):
    ev = find_event(evkey, launch_html=launch_html)
    start = (ev or {}).get("start_date") or evkey[-10:]
    end = (ev or {}).get("end_date") or start

    # ---- A. Sales (OCTOPOS) ----
    try:
        sales = pnl_octopos.fetch_octopos_pnl(start, end)
        sales_status = "ok"
    except SystemExit as e:
        sales = {"gross": None, "net": None, "tax": None, "transactions": None,
                 "avg_ticket": None, "payment_breakdown": {}, "top_products": []}
        sales_status = f"error: {e}"
    octo_cash = None
    for k, v in (sales.get("payment_breakdown") or {}).items():
        if "CASH" in k.upper():
            octo_cash = (octo_cash or 0) + v

    # ---- B. Inventory ----
    inv = pnl_inventory.fetch_inventory_pnl(evkey, state_path=inv_state)

    # ---- C. Manager report (cash: staff/meals/other) ----
    mgr = pnl_manager.fetch_manager_pnl(evkey, state_path=mgr_state, octopos_cash=octo_cash)

    # ---- assemble expense lines ----
    expenses = {}
    # Inventory + Shipping
    if inv.get("found"):
        st = "ok" if inv.get("complete") else "incomplete"
        expenses["inventory"] = _line(inv["inventory"], "inventory_orders", st,
                                       "; ".join(inv.get("warnings", [])))
        expenses["shipping"] = _line(inv["shipping"], "inventory_orders", st)
    else:
        expenses["inventory"] = _line(None, "inventory_orders", "missing", inv.get("error", ""))
        expenses["shipping"] = _line(None, "inventory_orders", "missing")
    # Staff / Meals / Other (cash, from manager)
    if mgr.get("found"):
        expenses["staff"] = _line(mgr["staff"], "manager_reports", "ok")
        expenses["meals"] = _line(mgr["meals"], "manager_reports", "ok")
        expenses["other"] = _line(mgr["other"], "manager_reports", "ok")
    else:
        for k in ("staff", "meals", "other"):
            expenses[k] = _line(None, "manager_reports", "missing", mgr.get("error", ""))
    # Marketing — Meta (auto) + TikTok (pending review)
    expenses["marketing_meta"] = (_line(meta_spend, "meta_api", "ok")
                                  if meta_spend is not None
                                  else _line(None, "meta_api", "pending", "wire from event_analytics"))
    expenses["marketing_tiktok"] = (_line(tiktok_spend, "tiktok_api", "ok")
                                    if tiktok_spend is not None
                                    else _line(None, "tiktok_api", "pending", "Reporting scope in review"))
    # Travel / Venue (non-cash, from QuickBooks — pending review)
    qb = qb_expenses or {}
    for k in ("travel", "venue", "other_nonloud"):
        pass
    expenses["travel"] = (_line(qb.get("travel"), "quickbooks", "ok")
                          if "travel" in qb else _line(None, "quickbooks", "pending", "QB production in review"))
    expenses["venue"] = (_line(qb.get("venue"), "quickbooks", "ok")
                         if "venue" in qb else _line(None, "quickbooks", "pending", "QB production in review"))

    # ---- profit (preliminary if anything pending/missing) ----
    net = sales.get("net")
    known = [v["amount"] for v in expenses.values() if v["amount"] is not None]
    pending = [k for k, v in expenses.items() if v["status"] in ("pending", "missing", "incomplete")]
    total_known_exp = round(sum(known), 2) if known else 0.0
    profit = round(net - total_known_exp, 2) if net is not None else None
    margin = round(profit / net, 4) if (profit is not None and net) else None
    preliminary = bool(pending) or sales_status != "ok"

    return {
        "evkey": evkey,
        "event": {"city": (ev or {}).get("city"), "state": (ev or {}).get("state"),
                  "start_date": start, "end_date": end,
                  "venue": (ev or {}).get("venue"), "tier": (ev or {}).get("tier")},
        "revenue": {"net_sales": net, "gross_sales": sales.get("gross"),
                    "tax": sales.get("tax"), "transactions": sales.get("transactions"),
                    "avg_ticket": sales.get("avg_ticket"),
                    "octopos_cash": (round(octo_cash, 2) if octo_cash else None),
                    "source": "octopos", "status": sales_status},
        "expenses": expenses,
        "total_known_expenses": total_known_exp,
        "profit_preliminary": profit, "margin": margin,
        "preliminary": preliminary,
        "pending_or_missing": pending,
        "cash_check": mgr.get("cash_check"),
        "top_products": sales.get("top_products", [])[:15],
        "warnings": (inv.get("warnings", []) + mgr.get("warnings", [])),
    }


def _fmt(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "— pending"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--launch")
    ap.add_argument("--inv-state")
    ap.add_argument("--mgr-state")
    ap.add_argument("--meta-spend", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    pnl = build_pnl(args.evkey, launch_html=args.launch, inv_state=args.inv_state,
                    mgr_state=args.mgr_state, meta_spend=args.meta_spend)
    if args.json:
        print(json.dumps(pnl, indent=2, ensure_ascii=False)); return 0
    e = pnl["event"]; r = pnl["revenue"]
    print(f"\n===== P&L — {e['city']}, {e['state']} ({e['start_date']}..{e['end_date']}) =====")
    print(f"  REVENUE")
    print(f"    Net sales:    {_fmt(r['net_sales'])}   (gross {_fmt(r['gross_sales'])}, tax {_fmt(r['tax'])})")
    print(f"    Transactions: {r['transactions']}   avg {_fmt(r['avg_ticket'])}   cash {_fmt(r['octopos_cash'])}")
    print(f"  EXPENSES")
    for k, v in pnl["expenses"].items():
        flag = "" if v["status"] == "ok" else f"  [{v['status']}]"
        print(f"    {k:18} {_fmt(v['amount']):>14}   <{v['source']}>{flag}")
    print(f"  ----")
    print(f"    Known expenses: {_fmt(pnl['total_known_expenses'])}")
    label = "PROFIT (preliminary)" if pnl["preliminary"] else "PROFIT"
    mg = f"  margin {pnl['margin']*100:.1f}%" if pnl["margin"] is not None else ""
    print(f"    {label}: {_fmt(pnl['profit_preliminary'])}{mg}")
    if pnl["pending_or_missing"]:
        print(f"  ⏳ pending/missing: {', '.join(pnl['pending_or_missing'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
