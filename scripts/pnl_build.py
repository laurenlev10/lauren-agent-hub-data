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
import argparse, datetime as dt, json, re, sys
from pathlib import Path

# import sibling source modules
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pnl_octopos, pnl_inventory, pnl_manager
import urllib.request

EVENT_ANALYTICS_URL = "https://events.themakeupblowout.com/state/event_analytics.json"


def _slugify(s):
    out = "".join(c if c.isalnum() else "-" for c in (s or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def fetch_marketing(ev, analytics_path=None):
    """Return {meta_spend, tiktok_spend} from event_analytics.json.
    event_analytics keys events as <city-slug>-<state>-<year> (e.g. roseville-mn-2026).
    Meta is auto (API); TikTok is whatever is recorded (manual override until the
    Reporting scope clears review)."""
    if not ev:
        return {}
    slug = f"{_slugify(ev.get('city'))}-{(ev.get('state') or '').lower()}-{(ev.get('start_date') or '')[:4]}"
    try:
        if analytics_path:
            data = json.loads(Path(analytics_path).read_text(encoding="utf-8"))
        else:
            req = urllib.request.Request(EVENT_ANALYTICS_URL, headers={"User-Agent": "mbs-pnl"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}
    node = (data.get("events") or {}).get(slug)
    if not node:
        return {"_slug": slug, "_found": False}
    meta = (node.get("meta") or {}).get("spend")
    tt = (node.get("tiktok") or {}).get("spend")
    return {"meta_spend": (round(float(meta), 2) if meta is not None else None),
            "tiktok_spend": (round(float(tt), 2) if tt is not None else None),
            "_slug": slug, "_found": True}


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


def build_pnl(evkey, *, launch_html=None, inv_state=None, mgr_state=None, analytics_path=None,
              overrides_path=None, meta_spend=None, tiktok_spend=None, qb_expenses=None,
              skip_sales=False):
    ev = find_event(evkey, launch_html=launch_html)
    start = (ev or {}).get("start_date") or evkey[-10:]
    end = (ev or {}).get("end_date") or start

    # ---- A. Sales (OCTOPOS) ----
    if skip_sales:
        sales = {"gross": None, "net": None, "tax": None, "transactions": None,
                 "avg_ticket": None, "payment_breakdown": {}, "top_products": []}
        sales_status = "pending (event not started)"
    else:
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
    # Mystery Box — costed from OCTOPOS units x $15 (Lauren's rule), its own COGS line
    mbox = sales.get("mystery_box") or {}
    if mbox.get("found"):
        expenses["mystery_box"] = _line(mbox.get("cost"), "octopos (units x $15)", "ok",
                                        f"{mbox.get('units'):.0f} units x ${mbox.get('unit_cost'):.0f}")
    else:
        expenses["mystery_box"] = _line(0.0, "octopos (units x $15)", "ok", "no Mystery Box sold")
    # Staff / Meals / Other (cash, from manager)
    if mgr.get("found"):
        expenses["staff"] = _line(mgr["staff"], "manager_reports", "ok")
        expenses["meals"] = _line(mgr["meals"], "manager_reports", "ok")
        expenses["other"] = _line(mgr["other"], "manager_reports", "ok")
    else:
        for k in ("staff", "meals", "other"):
            expenses[k] = _line(None, "manager_reports", "missing", mgr.get("error", ""))
    # Marketing — auto-pull Meta + TikTok from event_analytics.json (Meta=API, TikTok=manual until review clears)
    if meta_spend is None or tiktok_spend is None:
        mkt = fetch_marketing(ev, analytics_path=analytics_path)
        if meta_spend is None:
            meta_spend = mkt.get("meta_spend")
        if tiktok_spend is None:
            tiktok_spend = mkt.get("tiktok_spend")
    if meta_spend is not None:
        expenses["marketing_meta"] = _line(meta_spend, "event_analytics(meta)", "ok")
    else:
        expenses["marketing_meta"] = _line(None, "meta_api", "pending", "no meta in event_analytics")
    if tiktok_spend:
        expenses["marketing_tiktok"] = _line(tiktok_spend, "event_analytics(tiktok)", "ok")
    elif tiktok_spend == 0:
        expenses["marketing_tiktok"] = _line(0.0, "event_analytics(tiktok)", "ok", "no TikTok spend recorded (auto API in review)")
    else:
        expenses["marketing_tiktok"] = _line(None, "tiktok_api", "pending", "Reporting scope in review")
    # Travel / Venue / ULINE / Lyft (non-cash) — auto-fetch from QuickBooks by Class
    # "{City} {Year}" (e.g. "Cleveland 2026"). Falls back to pending if QB not available.
    if qb_expenses is None and ev:
        try:
            import pnl_quickbooks
            cls = f"{ev.get('city')} {(start or '')[:4]}"
            qbd = pnl_quickbooks.fetch_qb_expenses(cls)
            if qbd.get("lines"):
                bc = qbd.get("by_category", {})
                qb_expenses = {k: bc[k] for k in ("travel", "venue", "uline", "lyft") if k in bc}
        except Exception as e:
            print(f"WARN QB fetch skipped: {e}", file=sys.stderr)
    # Travel / Venue (non-cash, from QuickBooks)
    qb = qb_expenses or {}
    for k in ("travel", "venue", "other_nonloud"):
        pass
    expenses["travel"] = (_line(qb.get("travel"), "quickbooks", "ok")
                          if "travel" in qb else _line(None, "quickbooks", "pending", "QB production in review"))
    expenses["venue"] = (_line(qb.get("venue"), "quickbooks", "ok")
                         if "venue" in qb else _line(None, "quickbooks", "pending", "QB production in review"))
    # ULINE — event supplies/packing ordered straight to the venue. From QuickBooks
    # (vendor ULINE) in future; manually overridable because the charge may bundle
    # other things and need correcting.
    expenses["uline"] = (_line(qb.get("uline"), "quickbooks (ULINE)", "ok")
                         if "uline" in qb else _line(None, "quickbooks (ULINE)", "pending",
                                                     "QB production in review — or set manually"))
    # Lyft — staff ride-share, recurring ~$500/event. From QuickBooks (Transportation)
    # by Class; manually overridable. (Lyft Business API needs account-manager approval;
    # Gmail receipt parsing is a fallback — see research notes.)
    expenses["lyft"] = (_line(qb.get("lyft"), "quickbooks (Lyft)", "ok")
                        if "lyft" in qb else _line(None, "quickbooks (Lyft)", "pending",
                                                   "QB production in review — or set manually"))

    # ---- manual overrides (IRON RULE #7: GitHub-synced state, browser-owned) ----
    overrides = _load_overrides(evkey, overrides_path)
    applied_overrides = {}
    for line_key, val in overrides.items():
        if line_key.startswith("_"):
            continue
        try:
            amt = float(val)
        except (TypeError, ValueError):
            continue
        expenses[line_key] = _line(amt, "manual override", "ok", "set manually by Lauren")
        applied_overrides[line_key] = amt

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
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        "manual_overrides": applied_overrides,
        "cash_check": mgr.get("cash_check"),
        "top_products": sales.get("top_products", [])[:15],
        "warnings": (inv.get("warnings", []) + mgr.get("warnings", [])),
        "detail": {
            "payment_breakdown": sales.get("payment_breakdown", {}),
            "inventory_lines": inv.get("supplier_lines", []),
            "staff_lines": [{"name": (t.get("name") or "—"),
                             "amount": round((t.get("total") or (_num(t.get("base"))+_num(t.get("bonus"))+_num(t.get("extra")))), 2)}
                            for t in (mgr.get("team") or [])],
            "manager_expense_lines": mgr.get("expense_lines", []),
            "manager_name": mgr.get("manager_name"),
            "manager_notes": mgr.get("notes", ""),
            "marketing": {"meta": meta_spend, "tiktok": tiktok_spend},
            "mystery_box": sales.get("mystery_box", {}),
        },
    }


def _load_overrides(evkey, overrides_path=None):
    """Read manual per-event line overrides from docs/state/pnl_overrides.json."""
    try:
        if overrides_path:
            p = Path(overrides_path)
        else:
            p = _repo_root() / "docs/state/pnl_overrides.json"
            if not p.exists():
                for c in Path("/").glob("**/docs/state/pnl_overrides.json"):
                    p = c; break
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return (data.get("events") or data).get(evkey, {}) or {}
    except Exception:
        return {}


def _num(x):
    try: return float(x or 0)
    except (TypeError, ValueError): return 0.0


def _fmt(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "— pending"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--launch")
    ap.add_argument("--inv-state")
    ap.add_argument("--mgr-state")
    ap.add_argument("--meta-spend", type=float, default=None)
    ap.add_argument("--analytics")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    pnl = build_pnl(args.evkey, launch_html=args.launch, inv_state=args.inv_state,
                    mgr_state=args.mgr_state, analytics_path=args.analytics, meta_spend=args.meta_spend)
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
