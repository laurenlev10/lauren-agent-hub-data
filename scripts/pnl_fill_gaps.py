#!/usr/bin/env python3
"""pnl_fill_gaps.py — FILL-ONLY-MISSING completer for event P&L files.

Lauren 2026-06-10 (verbatim): "אני רוצה להשלים רק את ההוצאות שחסרות ב-P&L והם נמצאים
במערכת... אני לא רוצה להוריד ולשנות את מה שבמערכת כבר אלא רק להשלים את הנתונים איפה
שהם חסרים בתמונה השלמה."

For every event from --since (default 2026-05-29, Cleveland) that has an
event_pnl/<evkey>.json file, fill ONLY expense lines whose amount is null/absent,
from the sources already in the system:

  inventory, shipping       <- inventory_orders.json   (pnl_inventory)
  staff, meals, other       <- manager_reports.json    (pnl_manager, FINAL reports only)
  marketing_meta/_tiktok    <- event_analytics.json    (pnl_build.fetch_marketing)
  travel, venue, uline,lyft <- QuickBooks by Class     (pnl_quickbooks, same window as build)
  revenue (gross/tax/net/transactions/avg_ticket) <- OCTOPOS (pnl_octopos, ended events only;
                                                     step 1 of Lauren's 2026-06-10 backfill)

NEVER overwrites an existing numeric amount (including manual overrides — they're
numeric). Never touches revenue. Recomputes total_known_expenses / profit_preliminary
the same way pnl_build does. Historical (sheet) files are filled the same gentle way.
"""
from __future__ import annotations
import argparse, datetime as dt, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_build as PB
import pnl_inventory, pnl_manager

QB_KEYS = ("travel", "venue", "uline", "lyft")


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _load_unit_cost_override(evkey):
    try:
        ovr = json.loads((ROOT / "docs/state/pnl_overrides.json").read_text(encoding="utf-8"))
        v = ((ovr.get("events") or {}).get(evkey) or {}).get("mystery_box_unit_cost")
        return float(v) if v is not None and v != "" else None
    except Exception:
        return None


def fill_event(evkey, evmeta, dry=False, octopos_truth=False):
    p = ROOT / f"docs/state/event_pnl/{evkey}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    exp = d.setdefault("expenses", {})

    def missing(k):
        return not _num((exp.get(k) or {}).get("amount"))

    filled = {}

    def fill(k, val, src, note=""):
        if val is None or not missing(k):
            return
        exp[k] = {"amount": round(float(val), 2), "source": src,
                  "status": "ok", "note": (note + " · fill-gaps 2026-06-10 — ערכים קיימים לא שונו").strip(" ·")}
        filled[k] = round(float(val), 2)

    # 0. Revenue — from OCTOPOS, ended events only. Fill ONLY null subfields; an existing
    #    net_sales (e.g. sheet-derived) is never touched. (Lauren 2026-06-10 step 1.)
    today = dt.date.today().isoformat()
    end_date = evmeta.get("end_date") or ""
    rev = d.setdefault("revenue", {})
    REV_KEYS = ("net_sales", "gross_sales", "tax", "transactions", "avg_ticket")
    _rev_needs = any(not _num(rev.get(k)) for k in REV_KEYS) or (octopos_truth and rev.get("source") != "octopos")
    if end_date and end_date < today and _rev_needs:
        try:
            import pnl_octopos
            jwt = pnl_octopos.octopos_jwt()
            tot = pnl_octopos.fetch_sales_totals(jwt, evmeta.get("start_date"), end_date)
            src_map = {"net_sales": tot.get("net"), "gross_sales": tot.get("gross"),
                       "tax": tot.get("tax"), "transactions": tot.get("transactions"),
                       "avg_ticket": tot.get("avg_ticket")}
            for k, v in src_map.items():
                cur = rev.get(k)
                overwrite = octopos_truth and _num(v) and _num(cur) and abs(float(cur) - float(v)) > 0.01 and rev.get("source") != "octopos"
                if (not _num(cur) or overwrite) and _num(v):
                    if overwrite:
                        rev.setdefault("_pre_octopos_backup", {})[k] = cur   # audit — old non-OCTOPOS value
                    rev[k] = round(float(v), 2) if k != "transactions" else int(v)
                    filled["revenue." + k] = rev[k]
            if octopos_truth and any(k.startswith("revenue.") for k in filled):
                rev["source"] = "octopos"
            if filled and any(k.startswith("revenue.") for k in filled):
                rev.setdefault("source", "octopos")
                rev["_fill_note"] = "הושלם מאוקטופוס · fill-gaps — ערכים קיימים לא שונו"
                det = d.setdefault("detail", {})
                if not det.get("payment_breakdown") and tot.get("payment_breakdown"):
                    det["payment_breakdown"] = tot["payment_breakdown"]
        except SystemExit as e:
            print(f"  WARN OCTOPOS revenue skipped for {evkey}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  WARN OCTOPOS revenue failed for {evkey}: {e}", file=sys.stderr)

    # 0.5 Mystery Box — units sold from OCTOPOS (Lauren 2026-06-10 step 2). Fills the
    #     UNITS (fact from POS) when absent; the existing expense amount is untouched.
    #     The page recomputes total = units x editable unit-cost (default $15).
    det = d.setdefault("detail", {})
    mb = det.get("mystery_box") or {}
    _mb_exp = exp.get("mystery_box") or {}
    _mb_manual = "manual override" in str(_mb_exp.get("source", ""))
    _mb_recompute = (octopos_truth and not _mb_manual and _num(mb.get("units")))
    if end_date and end_date < today and (not _num(mb.get("units")) or _mb_recompute):
        try:
            import pnl_octopos
            if _num(mb.get("units")) and _mb_recompute:
                # units already from OCTOPOS (last fill) — just recompute the amount below
                res = {"units": mb["units"], "name": mb.get("name"),
                       "product_id": mb.get("product_id"), "revenue": mb.get("revenue")}
            else:
                jwt2 = pnl_octopos.octopos_jwt()
                sales2 = pnl_octopos.fetch_all_vendor_products(jwt2, evmeta.get("start_date"), end_date)
                res = pnl_octopos.mystery_box_from(sales2)
            units = float(res.get("units") or 0)
            newmb = dict(mb)
            newmb["units"] = units
            if not newmb.get("name"):
                newmb["name"] = res.get("name") or "Mystery Box"
            if res.get("product_id") and not newmb.get("product_id"):
                newmb["product_id"] = res["product_id"]
            if res.get("revenue") is not None and newmb.get("revenue") is None:
                newmb["revenue"] = res["revenue"]
            if _num(mb.get("cost")) and units:
                newmb["unit_cost_implied"] = round(float(mb["cost"]) / units, 2)
            if not _num(newmb.get("unit_cost")):
                newmb["unit_cost"] = 15.0
            uc = _load_unit_cost_override(evkey) or 15.0
            newmb["unit_cost"] = uc
            newmb["cost"] = round(units * uc, 2)
            det["mystery_box"] = newmb
            filled["mystery_box.units"] = units
            _new_amt = round(units * uc, 2)
            if missing("mystery_box"):
                fill("mystery_box", _new_amt, f"octopos (units x ${uc:g}) (fill-gaps)", f"{units:.0f} units x ${uc:g}")
            elif octopos_truth and not _mb_manual and abs(float(_mb_exp.get("amount") or 0) - _new_amt) > 0.01:
                old_amt = _mb_exp.get("amount")
                exp["mystery_box"] = {"amount": _new_amt, "source": f"octopos (units x ${uc:g})",
                                      "status": "ok",
                                      "note": f"{units:.0f} units x ${uc:g} · OCTOPOS-truth 2026-06-10 (היה ${old_amt})"}
                filled["mystery_box"] = _new_amt
        except SystemExit as e:
            print(f"  WARN OCTOPOS mystery-box skipped for {evkey}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  WARN OCTOPOS mystery-box failed for {evkey}: {e}", file=sys.stderr)

    # 1. Inventory / Shipping — only when real invoices exist (never fill 0 over pending)
    if missing("inventory") or missing("shipping"):
        inv = pnl_inventory.fetch_inventory_pnl(evkey, state_path=ROOT / "docs/state/inventory_orders.json")
        if inv.get("found") and float(inv.get("inventory") or 0) > 0:
            fill("inventory", inv["inventory"], "inventory_orders (fill-gaps)")
            fill("shipping", inv.get("shipping") or 0.0, "inventory_orders (fill-gaps)")

    # 2. Staff / Meals / Other — only from a FINAL manager report
    if missing("staff") or missing("meals") or missing("other"):
        mgr = pnl_manager.fetch_manager_pnl(evkey, state_path=ROOT / "docs/state/manager_reports.json")
        if mgr.get("found"):
            fill("staff", mgr.get("staff"), "manager_report (fill-gaps)")
            fill("meals", mgr.get("meals"), "manager_report (fill-gaps)")
            fill("other", mgr.get("other"), "manager_report (fill-gaps)")

    # 3. Marketing — from event_analytics
    if missing("marketing_meta") or missing("marketing_tiktok"):
        try:
            mkt = PB.fetch_marketing(evmeta)
            fill("marketing_meta", mkt.get("meta_spend"), "event_analytics(meta) (fill-gaps)")
            fill("marketing_tiktok", mkt.get("tiktok_spend"), "event_analytics(tiktok) (fill-gaps)")
        except Exception as e:
            print(f"  WARN marketing fetch failed: {e}", file=sys.stderr)

    # 4. QB by Class — same class name + wide window as pnl_build
    if any(missing(k) for k in QB_KEYS):
        try:
            import pnl_quickbooks
            start, end = evmeta.get("start_date"), evmeta.get("end_date")
            cls = f"{evmeta.get('city')} {(start or '')[:4]}"
            d0 = (dt.date.fromisoformat(start) - dt.timedelta(days=400)).isoformat() if start else None
            d1 = (dt.date.fromisoformat(end) + dt.timedelta(days=30)).isoformat() if end else None
            qbd = pnl_quickbooks.fetch_qb_expenses(cls, date_from=d0, date_to=d1)
            bc = qbd.get("by_category", {}) if qbd.get("lines") else {}
            for k in QB_KEYS:
                if k in bc:
                    note = ""
                    if k == "venue":
                        vlines = [l for l in qbd["lines"] if l.get("category") == "venue"]
                        note = " + ".join(f"{l['date'][5:]} ${l['amount']:,.0f}" for l in vlines[:6])
                    fill(k, bc[k], f"quickbooks ({cls}) (fill-gaps)", note)
        except Exception as e:
            print(f"  WARN QB fetch skipped for {evkey}: {e}", file=sys.stderr)

    if not filled:
        return {}

    # Recompute derived totals exactly like pnl_build (existing values untouched)
    net = (d.get("revenue") or {}).get("net_sales")
    known = [v["amount"] for v in exp.values() if isinstance(v, dict) and _num(v.get("amount"))]
    pending = [k for k, v in exp.items() if isinstance(v, dict) and v.get("status") in ("pending", "missing", "incomplete")]
    d["total_known_expenses"] = round(sum(known), 2) if known else 0.0
    if _num(net):
        d["profit_preliminary"] = round(net - d["total_known_expenses"], 2)
        if net:
            d["margin"] = round(d["profit_preliminary"] / net, 4)
    d["_fill_gaps_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    if not dry:
        p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    return filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-05-29")
    ap.add_argument("--evkey")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--octopos-truth", action="store_true",
                    help="OCTOPOS is authoritative: overwrite non-OCTOPOS revenue + mystery-box values (manual overrides preserved). Lauren 2026-06-10.")
    a = ap.parse_args()
    events = json.loads((ROOT / "docs/state/events_index.json").read_text(encoding="utf-8"))["events"]
    targets = [e for e in events if (a.evkey and e["evkey"] == a.evkey) or
               (not a.evkey and e.get("start_date", "") >= a.since)]
    total = 0
    for e in sorted(targets, key=lambda x: x["start_date"]):
        r = fill_event(e["evkey"], e, dry=a.dry, octopos_truth=a.octopos_truth)
        if r is None:
            continue
        if r:
            total += 1
            print(f"  ✓ {e['evkey']}: filled {r}")
        else:
            print(f"  · {e['evkey']}: nothing missing that the system can fill")
    print(f"\nfilled {total} events{' (DRY)' if a.dry else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
