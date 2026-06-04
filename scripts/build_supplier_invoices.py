#!/usr/bin/env python3
"""build_supplier_invoices.py — consolidate per-supplier invoices across all 2026 events.

Reads every docs/state/event_pnl/<evkey>.json (which carries detail.inventory_lines:
supplier + invoiced + shipping per event) and produces docs/state/supplier_invoices.json:
a supplier-grouped list of invoices for the supplier-payments dashboard.

    suppliers: { <canonical supplier>: { total, count, invoices: [
        { id, evkey, event, date, amount, shipping, supplier_raw } ] } }

Payment tracking (invoice #, paid date, method) is stored SEPARATELY in
docs/state/supplier_payments.json (browser-owned, GitHub-synced) keyed by `id`.
"""
from __future__ import annotations
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVDIR = ROOT / "docs/state/event_pnl"

# canonicalize supplier-name variants across the sheet + inventory_orders
ALIASES = {
    "she": "She Makeup", "she makeup": "She Makeup",
    "amuse": "Amuse Cosmetics", "amuse cosmetics": "Amuse Cosmetics",
    "bc": "Beauty Creations", "beauty creations": "Beauty Creations",
    "bb&w": "BB&W", "bb and w": "BB&W", "bb-and-w": "BB&W",
    "romantic": "Romantic Beauty", "romantic beauty": "Romantic Beauty",
    "kara": "Kara Beauty", "kara beauty": "Kara Beauty",
    "xime": "Xime Beauty", "xime beauty": "Xime Beauty",
    "nabi": "Nabi", "prolux": "Prolux", "rude": "Rude", "lurella": "Lurella",
    "ebc": "EBC", "ebs perfumes": "EBC", "ebs": "EBC", "ebs perfume": "EBC", "golden touch": "Golden Touch",
    "feral edge": "Feral Edge", "feral-edge": "Feral Edge",
    "market": "Market", "mystery box": "Mystery Box", "garage": "Mystery Box",
    "mystery-box": "Mystery Box",
}


def canon(name):
    n = re.sub(r"\s+", " ", (name or "").replace("-", " ").replace("_", " ").strip())
    return ALIASES.get(n.lower(), n)


def slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def build():
    suppliers = {}
    for f in sorted(EVDIR.glob("*.json")):
        if f.name == "_index.json":
            continue
        j = json.loads(f.read_text(encoding="utf-8"))
        ev = j.get("event", {})
        evkey = j.get("evkey") or f.stem
        ename = f"{ev.get('city','')}, {ev.get('state','')}".strip(", ")
        date = ev.get("start_date")
        for line in (j.get("detail", {}).get("inventory_lines") or []):
            amt = line.get("invoiced") or 0
            if not amt:
                continue
            raw = line.get("supplier") or line.get("supplier_code") or "?"
            cname = canon(raw)
            inv_id = f"{evkey}__{slug(cname)}"
            rec = suppliers.setdefault(cname, {"total": 0.0, "count": 0, "invoices": []})
            rec["invoices"].append({
                "id": inv_id, "evkey": evkey, "event": ename, "date": date,
                "amount": round(float(amt), 2), "shipping": round(float(line.get("shipping") or 0), 2),
                "supplier_raw": raw})
            rec["total"] = round(rec["total"] + float(amt), 2)
            rec["count"] += 1
    # sort each supplier's invoices by date
    for rec in suppliers.values():
        rec["invoices"].sort(key=lambda x: x.get("date") or "")
    out = {"_updated_at": None, "_note": "auto-built from event_pnl; payment tracking lives in supplier_payments.json",
           "suppliers": dict(sorted(suppliers.items(), key=lambda kv: -kv[1]["total"]))}
    return out


if __name__ == "__main__":
    out = build()
    (ROOT / "docs/state/supplier_invoices.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"suppliers: {len(out['suppliers'])}")
    for name, rec in list(out["suppliers"].items()):
        print(f"  {name:20} {rec['count']:3} invoices  ${rec['total']:>10,.0f}")
