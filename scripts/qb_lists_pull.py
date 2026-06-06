#!/usr/bin/env python3
"""qb_lists_pull.py — reference lists for the bookkeeping workstation dropdowns.
Pulls expense Accounts (categories), Classes, and Vendors from QuickBooks into
docs/state/qb_lists.json. Re-run whenever Lauren adds categories/classes/vendors."""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_quickbooks as QB

def pull():
    accounts = []
    start = 1
    while True:
        r = QB.query(f"select Id, Name, FullyQualifiedName, AccountType, Active from Account startposition {start} maxresults 500").get("QueryResponse", {})
        batch = r.get("Account", []) or []
        accounts += batch
        if len(batch) < 500: break
        start += 500
    exp_types = {"Expense", "Other Expense", "Cost of Goods Sold"}
    cats = [{"id": a["Id"], "name": a.get("FullyQualifiedName") or a.get("Name"), "type": a.get("AccountType")}
            for a in accounts if a.get("Active", True) and a.get("AccountType") in exp_types]
    classes = [{"id": c["Id"], "name": c["Name"]}
               for c in QB.query("select Id, Name, Active from Class maxresults 500").get("QueryResponse", {}).get("Class", [])
               if c.get("Active", True)]
    vendors = []
    start = 1
    while True:
        r = QB.query(f"select Id, DisplayName, Active from Vendor startposition {start} maxresults 500").get("QueryResponse", {})
        batch = r.get("Vendor", []) or []
        vendors += [{"id": v["Id"], "name": v.get("DisplayName")} for v in batch if v.get("Active", True)]
        if len(batch) < 500: break
        start += 500
    return {"_updated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "categories": sorted(cats, key=lambda x: x["name"]),
            "classes": sorted(classes, key=lambda x: x["name"]),
            "vendors": sorted(vendors, key=lambda x: (x["name"] or "").lower())}

if __name__ == "__main__":
    data = pull()
    (ROOT / "docs/state/qb_lists.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"categories: {len(data['categories'])} · classes: {len(data['classes'])} · vendors: {len(data['vendors'])}")
