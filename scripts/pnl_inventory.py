#!/usr/bin/env python3
"""pnl_inventory.py — Inventory + Shipping source for the automated event P&L.

Source B of 5 (see event_summary_BUILD_BRIEF.md). Reads docs/state/inventory_orders.json
(read-only — IRON RULE #18 merge-on-write belongs to the dashboard) and produces
two P&L lines for one event:

    Inventory = Σ invoice_total_usd  −  Σ shipping_cost_usd
    Shipping  = Σ shipping_cost_usd                       (separate row, IRON RULE #9)

invoice_total_usd is the grand total actually paid INCLUDING shipping (per CLAUDE.md);
shipping_cost_usd is the informational breakdown. Invoices live at TWO levels and are
additive: suppliers[code].invoice_total_usd + local_orders[i].invoice_total_usd.
A supplier-level invoice that dwarfs its own order estimate is a data artifact and is
excluded (rude/Omaha). Completeness is judged on TRUSTED invoices.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def _find_state_file(explicit=None):
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve()
    cand = here.parent.parent / "docs/state/inventory_orders.json"
    if cand.exists():
        return cand
    for c in Path("/").glob("**/docs/state/inventory_orders.json"):
        return c
    raise SystemExit("inventory_orders.json not found")


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_inventory_pnl(evkey, state_path=None):
    path = _find_state_file(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    ev = (data.get("events") or {}).get(evkey)
    if ev is None:
        return {"source": "inventory_orders", "evkey": evkey, "found": False,
                "error": f"no event '{evkey}' in inventory_orders.json",
                "inventory": 0.0, "shipping": 0.0, "complete": False}

    suppliers = ev.get("suppliers") or {}
    orders = ev.get("local_orders") or []

    orders_by_code = {}
    for o in orders:
        orders_by_code.setdefault(o.get("supplier_code"), []).append(o)

    ANOMALY_RATIO = 3.0
    ANOMALY_ABS = 1000.0

    trusted_invoiced = 0.0
    trusted_shipping = 0.0
    raw_invoiced = 0.0
    anomalies = []
    trusted_codes = set()
    supplier_lines = []

    all_codes = set(suppliers.keys()) | set(orders_by_code.keys())
    for code in all_codes:
        s = suppliers.get(code) if isinstance(suppliers.get(code), dict) else {}
        ords = orders_by_code.get(code, [])
        est = sum(_f(o.get("total_cost")) for o in ords)
        sup_inv = _f(s.get("invoice_total_usd"))
        sup_ship = _f(s.get("shipping_cost_usd"))
        ord_inv = sum(_f(o.get("invoice_total_usd")) for o in ords)
        ord_ship = sum(_f(o.get("shipping_cost_usd")) for o in ords)
        raw_invoiced += sup_inv + ord_inv

        if sup_inv > 0 and est > 0 and sup_inv > ANOMALY_RATIO * est and sup_inv > ANOMALY_ABS:
            anomalies.append(f"{code}: supplier invoice ${sup_inv:,.0f} is {sup_inv/est:.0f}x "
                             f"its order estimate ${est:,.0f} — likely a mis-keyed lump sum, excluded")
            sup_inv = 0.0
            sup_ship = 0.0

        # supplier-level invoice OVERRIDES order-level (Lauren 2026-06-04) — no double-count
        if sup_inv > 0:
            supplier_total = sup_inv; supplier_ship = sup_ship
        else:
            supplier_total = ord_inv; supplier_ship = ord_ship
        supplier_name = (s.get("name") or (ords[0].get("supplier_name") if ords else None) or code)
        is_anom = any(code + ":" in a for a in anomalies)
        supplier_lines.append({
            "supplier_code": code, "supplier": supplier_name,
            "invoiced": round(supplier_total, 2),
            "shipping": round(supplier_ship, 2),
            "estimate": round(est, 2),
            "status": ("anomaly-excluded" if is_anom else ("invoiced" if supplier_total > 0 else "no-invoice"))})
        if supplier_total > 0:
            trusted_invoiced += supplier_total
            trusted_shipping += supplier_ship
            trusted_codes.add(code)

    trusted_invoiced = round(trusted_invoiced, 2)
    trusted_shipping = round(trusted_shipping, 2)
    raw_invoiced = round(raw_invoiced, 2)
    inventory_line = round(trusted_invoiced - trusted_shipping, 2)
    shipping_total = trusted_shipping

    est_total = _f((ev.get("summary") or {}).get("total_usd"))
    if not est_total:
        est_total = sum(_f(o.get("total_cost")) for o in orders)
    est_total = round(est_total, 2)

    n_codes = len(all_codes)
    distinct_invoiced = len(trusted_codes)
    coverage = round(distinct_invoiced / n_codes, 2) if n_codes else 0.0
    inv_vs_est = round(trusted_invoiced / est_total, 2) if est_total else 0.0
    complete = coverage >= 0.9 and inv_vs_est >= 0.9 and not anomalies

    warnings = list(anomalies)
    if coverage < 0.9:
        warnings.append(f"only {distinct_invoiced}/{n_codes} suppliers have trusted invoices "
                        f"({coverage:.0%}) — Inventory line understated; STOP and ask Lauren")
    if est_total and inv_vs_est < 0.9:
        warnings.append(f"trusted invoiced ${trusted_invoiced:,.0f} vs estimate ${est_total:,.0f} "
                        f"({inv_vs_est:.0%}) — likely missing invoices")
    if n_codes and n_codes < 8:
        warnings.append(f"only {n_codes} suppliers ordered — below typical event")

    return {"source": "inventory_orders", "evkey": evkey, "found": True,
            "inventory": inventory_line, "shipping": shipping_total,
            "invoiced_total": trusted_invoiced, "raw_invoiced_incl_anomalies": raw_invoiced,
            "estimate_total": est_total, "invoiced_vs_estimate": inv_vs_est,
            "counts": {"suppliers": len(suppliers) or n_codes, "orders": len(orders),
                       "supplier_codes": n_codes, "invoiced_trusted": distinct_invoiced,
                       "coverage": coverage},
            "anomalies": anomalies, "supplier_lines": sorted(supplier_lines, key=lambda x: -x["invoiced"]),
            "complete": complete, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--state")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = fetch_inventory_pnl(args.evkey, state_path=args.state)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    if not d["found"]:
        print(f"WARN {d['error']}")
        return 1
    print(f"\n=== Inventory P&L — {args.evkey} ===")
    print(f"  Inventory (invoiced - shipping): ${d['inventory']:,.2f}")
    print(f"  Shipping (separate row):         ${d['shipping']:,.2f}")
    print(f"  Invoiced total (trusted):        ${d['invoiced_total']:,.2f}")
    print(f"  Estimate (ordered):              ${d['estimate_total']:,.2f}  ({d['invoiced_vs_estimate']:.0%} invoiced)")
    cov = d['counts']
    print(f"  Coverage: {cov['invoiced_trusted']}/{cov['supplier_codes']} suppliers invoiced (trusted)")
    print(f"  Raw incl. anomalies:             ${d['raw_invoiced_incl_anomalies']:,.2f}")
    print(f"  COMPLETE: {d['complete']}")
    for w in d["warnings"]:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
