#!/usr/bin/env python3
"""build_supplier_invoices.py — consolidate per-supplier invoices for the AP dashboard.

SOURCES (per event, live-first):
  1. inventory_orders.json  — the REAL invoices Lauren enters in the inventory dashboard
     (per-supplier invoice_total_usd + shipping, anomaly-guarded via pnl_inventory).
     This is the live source: any invoice she enters flows in on the next run.
  2. event_pnl/<evkey>.json — historical sheet-derived inventory_lines, used ONLY for
     events that aren't in inventory_orders.json (the pre-system Jan–Apr events).

Output: docs/state/supplier_invoices.json (supplier-grouped). Payment tracking + manual
edits live in supplier_payments.json / supplier_manual_invoices.json (browser-owned).

Run by .github/workflows/supplier-invoices-rebuild.yml (daily) so the dashboard stays live.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVDIR = ROOT / "docs/state/event_pnl"
INV_ORDERS = ROOT / "docs/state/inventory_orders.json"
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_inventory  # noqa: E402

ALIASES = {
    "she": "She Makeup", "she makeup": "She Makeup",
    "amuse": "Amuse Cosmetics", "amuse cosmetics": "Amuse Cosmetics",
    "bc": "Beauty Creations", "beauty creations": "Beauty Creations",
    "bb&w": "BB&W", "bb and w": "BB&W",
    "romantic": "Romantic Beauty", "romantic beauty": "Romantic Beauty",
    "kara": "Kara Beauty", "kara beauty": "Kara Beauty",
    "xime": "Xime Beauty", "xime beauty": "Xime Beauty",
    "nabi": "Nabi", "prolux": "Prolux", "rude": "Rude", "lurella": "Lurella",
    "ebc": "EBC", "ebs perfumes": "EBC", "ebs": "EBC",
    "golden touch": "Golden Touch", "feral edge": "Feral Edge",
    "market": "Market", "mystery box": "Mystery Box", "garage": "Mystery Box",
}


def canon(name):
    n = re.sub(r"\s+", " ", (name or "").replace("-", " ").replace("_", " ").strip())
    return ALIASES.get(n.lower(), n)


def slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def _ff(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0

# Per-order anomaly guard (mirrors pnl_inventory): an invoice wildly above the order
# estimate is almost certainly a mis-keyed lump sum — skip it.
_ANOM_RATIO = 3.0
_ANOM_ABS = 1000.0


def add(suppliers, raw, evkey, ename, date, amount, shipping, id_suffix="", label_suffix=""):
    if not amount:
        return
    cname = canon(raw)
    inv_id = f"{evkey}__{slug(cname)}" + (("__" + id_suffix) if id_suffix else "")
    ev_label = ename + (" · " + label_suffix if label_suffix else "")
    rec = suppliers.setdefault(cname, {"total": 0.0, "count": 0, "invoices": []})
    rec["invoices"].append({"id": inv_id, "evkey": evkey, "event": ev_label, "date": date,
                            "amount": round(float(amount), 2),
                            "shipping": round(float(shipping or 0), 2), "supplier_raw": raw})
    rec["total"] = round(rec["total"] + float(amount), 2)
    rec["count"] += 1


def build():
    suppliers = {}
    live_evkeys = set()

    # 1) LIVE — inventory_orders.json (real invoices)
    if INV_ORDERS.exists():
        data = json.loads(INV_ORDERS.read_text(encoding="utf-8"))
        from collections import defaultdict
        for evkey, node in (data.get("events") or {}).items():
            inv = pnl_inventory.fetch_inventory_pnl(evkey, state_path=str(INV_ORDERS))
            status_by_code = {l.get("supplier_code"): l.get("status") for l in inv.get("supplier_lines", [])}
            ename = f"{node.get('city','')}, {node.get('state','')}".strip(", ")
            date = node.get("start_date")
            sups = node.get("suppliers") or {}
            obc = defaultdict(list)
            for o in (node.get("local_orders") or []):
                if o.get("cancelled_at") or o.get("moved_to"):
                    continue
                obc[o.get("supplier_code")].append(o)
            added_here = False
            for code in (set(sups.keys()) | set(obc.keys())):
                if status_by_code.get(code) == "anomaly-excluded":
                    continue  # mis-keyed supplier-level lump sum — excluded by pnl guard
                s_node = sups.get(code) if isinstance(sups.get(code), dict) else {}
                raw = (s_node.get("name") or (obc[code][0].get("supplier_name") if obc[code] else None) or code)
                sup_inv = _ff(s_node.get("invoice_total_usd"))
                # Build the list of REAL invoices for this supplier+event (invoice_total_usd,
                # NOT total_cost which is only the order estimate).
                invs = []  # (amount, shipping, id_suffix, label_suffix)
                if sup_inv > 0:
                    invs.append((sup_inv, _ff(s_node.get("shipping_cost_usd")), "", "חשבונית ללא הזמנה"))
                for oi, o in enumerate(obc[code]):
                    oa = _ff(o.get("invoice_total_usd"))
                    if oa <= 0:
                        continue
                    est = _ff(o.get("total_cost"))
                    if est > 0 and oa > _ANOM_RATIO * est and oa > _ANOM_ABS:
                        continue  # per-order mis-keyed lump sum — skip
                    invs.append((oa, _ff(o.get("shipping_cost_usd")), "ord%d" % oi, "הזמנה #%d" % (oi + 1)))
                if not invs:
                    continue
                # Stability: if there's exactly ONE invoice, keep the legacy id/label
                # (evkey__slug, no suffix) so historical payment-tracking ids don't orphan.
                if len(invs) == 1:
                    amt, shp, _, _ = invs[0]
                    add(suppliers, raw, evkey, ename, date, amt, shp)
                else:
                    for amt, shp, idsuf, lblsuf in invs:
                        add(suppliers, raw, evkey, ename, date, amt, shp, id_suffix=idsuf, label_suffix=lblsuf)
                added_here = True
            if added_here:
                live_evkeys.add(evkey)

    # 2) HISTORICAL — event_pnl for events NOT already live-sourced
    for f in sorted(EVDIR.glob("*.json")):
        if f.name == "_index.json":
            continue
        j = json.loads(f.read_text(encoding="utf-8"))
        evkey = j.get("evkey") or f.stem
        if evkey in live_evkeys:
            continue
        ev = j.get("event", {})
        ename = f"{ev.get('city','')}, {ev.get('state','')}".strip(", ")
        date = ev.get("start_date")
        for line in (j.get("detail", {}).get("inventory_lines") or []):
            add(suppliers, line.get("supplier") or line.get("supplier_code"), evkey, ename,
                date, line.get("invoiced"), line.get("shipping"))

    for rec in suppliers.values():
        rec["invoices"].sort(key=lambda x: x.get("date") or "")
    return {"_updated_at": None,
            "_note": "auto-built: inventory_orders.json (live) + event_pnl (historical). Payment/manual edits live in supplier_payments.json / supplier_manual_invoices.json.",
            "_live_events": sorted(live_evkeys),
            "suppliers": dict(sorted(suppliers.items(), key=lambda kv: -kv[1]["total"]))}


if __name__ == "__main__":
    import datetime as dt
    out = build()
    out["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    (ROOT / "docs/state/supplier_invoices.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"suppliers: {len(out['suppliers'])} · live events: {out['_live_events']}")
    for name, rec in list(out["suppliers"].items()):
        print(f"  {name:18} {rec['count']:3} inv  ${rec['total']:>10,.2f}")
