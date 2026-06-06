#!/usr/bin/env python3
"""qb_fixed_loan_tracker.py — feeds the 🟣 fixed-expenses and 🔴 loan-payments
tracking tabs in the bookkeeping dashboard (Lauren 2026-06-06).
Pulls 365 days of Purchases/Bills from QuickBooks, classifies rows as FIXED
(monthly recurring payees from qb_expense_types.json), LOAN (payee patterns OR
account name containing 'loan'), or OWNER (personal payees OR account containing
owner/personal), writes docs/state/qb_fixed_loan_tracker.json.
Runs daily in qb-untagged-refresh.yml."""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_quickbooks as QB

def _f(x):
    try: return float(x or 0)
    except (TypeError, ValueError): return 0.0

def main():
    types = json.loads((ROOT/"docs/state/qb_expense_types.json").read_text(encoding="utf-8"))
    fixed_pats = [p.lower() for p in types["types"]["fixed"]["patterns"]]
    loan_pats = [p.lower() for p in types["types"]["loan"]["patterns"]]
    owner_pats = [p.lower() for p in types["types"]["owner"]["patterns"]]
    accum_pats = [p.lower() for p in types["types"].get("fixed_accum", {}).get("patterns", [])]
    bank_pats = [p.lower() for p in types["types"].get("bank_fees", {}).get("patterns", [])]
    since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    fixed, loans, owner, accum, bank = [], [], [], [], []
    for ent, vref in (("Purchase", "EntityRef"), ("Bill", "VendorRef")):
        start = 1
        while True:
            r = QB.query(f"select * from {ent} where TxnDate >= '{since}' startposition {start} maxresults 500").get("QueryResponse", {})
            batch = r.get(ent, []) or []
            for t in batch:
                amt = round(_f(t.get("TotalAmt")), 2)
                if amt <= 0: continue
                vendor = ((t.get(vref) or {}).get("name") or "").strip()
                acct = ((t.get("AccountRef") or {}).get("name") or "").strip()
                # line-level account names (loan detection lives there for Purchases)
                line_accts = " ".join(((l.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef") or {}).get("name") or "" for l in (t.get("Line") or []))
                hay = f"{vendor} {acct}".lower()
                row = {"date": t.get("TxnDate"), "payee": vendor or acct or "—", "amount": amt,
                       "account": acct, "line_accounts": line_accts[:120], "paid_from": acct}
                hay_full = f"{hay} {line_accts.lower()}"
                memo_hay = f"{hay_full} {(t.get('PrivateNote') or '').lower()}"
                if any(p in memo_hay for p in bank_pats) or "bank charges" in hay_full:
                    bank.append(row)
                elif any(p in hay for p in loan_pats) or "loan" in hay_full:
                    loans.append(row)
                elif any(p in hay for p in accum_pats):
                    accum.append(row)
                elif any(p in hay for p in owner_pats) or "owner" in hay_full or "personal" in hay_full:
                    owner.append(row)
                elif any(p in hay for p in fixed_pats):
                    fixed.append(row)
            if len(batch) < 500: break
            start += 500
    for lst in (fixed, loans, owner, accum, bank): lst.sort(key=lambda r: r["date"], reverse=True)
    out = {"_updated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "window_days": 365, "fixed": fixed, "fixed_accum": accum, "bank_fees": bank, "loans": loans, "owner": owner}
    (ROOT/"docs/state/qb_fixed_loan_tracker.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"bank: {len(bank)} (${sum(r['amount'] for r in bank):,.0f})")
    print(f"fixed: {len(fixed)} (${sum(r['amount'] for r in fixed):,.0f}) · accum: {len(accum)} (${sum(r['amount'] for r in accum):,.0f}) · loans: {len(loans)} (${sum(r['amount'] for r in loans):,.0f}) · owner: {len(owner)} (${sum(r['amount'] for r in owner):,.0f})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
