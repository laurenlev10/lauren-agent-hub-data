#!/usr/bin/env python3
"""qb_untagged_pull.py — pull QuickBooks expenses that have NO Class assigned.

Feeds the bookkeeping dashboard's "משיכה אוטומטית" flow: instead of Lauren uploading a
CSV, the dashboard reads docs/state/qb_untagged.json — every Purchase/Bill expense line
in the window that has no ClassRef (= not yet filed to an event). Uses the production
QB tokens via pnl_quickbooks (refresh-token rotation handled there).
"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_quickbooks as QB

def pull(date_from=None, date_to=None):
    today = dt.date.today()
    date_from = date_from or (today - dt.timedelta(days=180)).isoformat()
    date_to = date_to or today.isoformat()
    rows = []
    for entity in ("Purchase", "Bill"):
        start = 1
        while True:
            q = (f"select * from {entity} where TxnDate >= '{date_from}' and TxnDate <= '{date_to}' "
                 f"startposition {start} maxresults 200")
            r = QB.query(q).get("QueryResponse", {})
            txns = r.get(entity, []) or []
            for t in txns:
                vendor = (t.get("EntityRef") or t.get("VendorRef") or {}).get("name", "")
                acct_top = (t.get("AccountRef") or {}).get("name", "")  # payment account (bank/cc)
                pay_type = t.get("PaymentType", "")
                for ln in t.get("Line", []):
                    det = ln.get("AccountBasedExpenseLineDetail") or ln.get("ItemBasedExpenseLineDetail")
                    if not det:
                        continue
                    if (det.get("ClassRef") or {}).get("value"):
                        continue  # already classified
                    rows.append({
                        "txn_id": t.get("Id"), "txn_type": entity, "line_id": ln.get("Id"),
                        "date": t.get("TxnDate"), "vendor": vendor,
                        "account": (det.get("AccountRef") or {}).get("name", ""),
                        "paid_from": acct_top, "payment_type": pay_type,
                        "amount": round(float(ln.get("Amount") or 0), 2),
                        "memo": (ln.get("Description") or t.get("PrivateNote") or "")[:120],
                    })
            if len(txns) < 200:
                break
            start += 200
    rows.sort(key=lambda x: x.get("date") or "", reverse=True)
    return {"_updated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": [date_from, date_to], "count": len(rows),
            "total": round(sum(r["amount"] for r in rows), 2), "rows": rows}

if __name__ == "__main__":
    args = sys.argv[1:]
    data = pull(*(args[:2] if args else []))
    out = ROOT / "docs/state/qb_untagged.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"untagged expenses: {data['count']} lines · ${data['total']:,.2f} · window {data['window']}")
    for r in data["rows"][:12]:
        print(f"  {r['date']} ${r['amount']:>9,.2f} {r['vendor'][:24]:24} [{r['account'][:28]}] {r['payment_type']}")
