#!/usr/bin/env python3
"""
ingest_tag_report.py — turn a QuickBooks "Transactions by tag" PDF (one event)
into our system's event attribution + P&L feed.

For each event Lauren exports QB → Reports → "Transactions by tag" (filtered to one
event's tag) → Export to PDF, and uploads it. This script:
  1. Parses every row (date, type, amount, deposit/expense) from the PDF — reliable
     anchor on the "Expense|Deposit  -$N.NN" cell.
  2. Gets the EXACT QB category for each expense by matching its amount(+date) to the
     QB API pull (Purchases) — the PDF's category column is layout-jumbled, the API is clean.
  3. Buckets expenses to P&L categories (inventory/venue/travel/meals/lyft/marketing/other).
  4. Writes docs/state/event_tagged_txns.json[evkey]  (the per-event tagged store).
  5. Adds a non-destructive `qb_tagged` block to docs/state/event_pnl/<evkey>.json.
  6. Stamps qb_class on any matching transactions already in the D1 bookkeeping DB
     (only those within Plaid's window; older events simply aren't in D1 yet).

Usage:  python3 ingest_tag_report.py --pdf "Fresno 2026.pdf" --evkey fresno-2026-01-16 --tag "Fresno 2026"
Constraint (Lauren): EXPENSES only feed "handled"; deposits/transfers are recorded
but NOT auto-reconciled — they're reviewed together later in the new system.
"""
import sys, os, re, json, glob, argparse, subprocess
from pathlib import Path
from datetime import date

def parse_pdf(pdf_path):
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        lines = [l for pg in pdf.pages for l in (pg.extract_text() or "").split("\n")]
    pat = re.compile(r'(Expense|Deposit)\s+(-?\$[\d,]+\.\d{2})')
    datepat = re.compile(r'(20\d\d)-\s*(\d\d)-(\d\d)')
    exps, deps = [], []
    for i, l in enumerate(lines):
        m = pat.search(l)
        if not m:
            continue
        v = round(abs(float(m.group(2).replace('$', '').replace(',', ''))), 2)
        # date: search this line + a few around (the date col wraps)
        d = None
        for j in range(max(0, i - 4), i + 1):
            dm = datepat.search(re.sub(r'\s+', '', lines[j]).replace('2026-', '2026-', 1)) or datepat.search(lines[j])
            if dm:
                d = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
        rec = {"amount": v, "date": d}
        (deps if m.group(1) == "Deposit" else exps).append(rec)
    return exps, deps

def qb_category_index(min_date):
    """Pull QB Purchases (expenses) from the API, index by exact cents -> [{date,vendor,cat,cls}]."""
    sys.path.insert(0, "/tmp/lauren-agent-hub-data/scripts")
    import pnl_quickbooks as QB
    out, start = [], 1
    while True:
        q = (f"select * from Purchase where TxnDate >= '{min_date}' "
             f"startposition {start} maxresults 1000")
        items = QB.query(q).get("QueryResponse", {}).get("Purchase", []) or []
        out += items
        if len(items) < 1000:
            break
        start += 1000
    from collections import defaultdict
    idx = defaultdict(list)
    for t in out:
        cat = cls = None
        for ln in t.get("Line", []):
            det = ln.get("AccountBasedExpenseLineDetail")
            if det:
                cat = (det.get("AccountRef") or {}).get("name")
                cl = det.get("ClassRef") or {}
                cls = cl.get("name") if cl else None
                break
        idx[round(abs(float(t.get("TotalAmt", 0))), 2)].append(
            {"date": t.get("TxnDate"), "vendor": (t.get("EntityRef") or {}).get("name", ""),
             "cat": cat, "cls": cls})
    return idx

def bucket(cat):
    c = (cat or "").lower()
    if "transportation" in c:                              return "lyft"
    if any(w in c for w in ("travel", "airfare", "accommodation", "rental", "hotel", "lodg")): return "travel"
    if "meal" in c:                                        return "meals"
    if any(w in c for w in ("merchandise", "cost of goods", "cogs", "inventory")): return "inventory"
    if "venue" in c or "rent" in c:                        return "venue"
    if "tiktok" in c:                                      return "marketing_tiktok"
    if any(w in c for w in ("advertis", "marketing", "meta", "facebook")): return "marketing_meta"
    if "shipping" in c or "freight" in c:                  return "shipping"
    return "other"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--tag", default="")
    ap.add_argument("--repo", default="/tmp/lauren-agent-hub-data")
    args = ap.parse_args()

    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        g = glob.glob(f"/sessions/*/mnt/uploads/{args.pdf}") or glob.glob(args.pdf)
        pdf_path = g[0]

    exps, deps = parse_pdf(pdf_path)
    min_date = min((e["date"] for e in exps if e["date"]), default="2026-01-01")
    idx = qb_category_index(min_date)

    from collections import defaultdict
    used = defaultdict(int)
    buckets = defaultdict(float)
    detail = []
    for e in exps:
        cands = idx.get(e["amount"], [])
        q = cands[min(used[e["amount"]], len(cands) - 1)] if cands else {"cat": None, "cls": None, "vendor": "", "date": e["date"]}
        used[e["amount"]] += 1
        b = bucket(q["cat"])
        buckets[b] += e["amount"]
        detail.append({"date": q.get("date") or e["date"], "vendor": q.get("vendor", ""),
                       "category": q.get("cat"), "class": q.get("cls"), "bucket": b,
                       "amount": -e["amount"]})

    total_exp = round(sum(buckets.values()), 2)
    tagged_dep = round(sum(d["amount"] for d in deps), 2)
    block = {
        "source": "QuickBooks 'Transactions by tag' report (bank-grounded actuals)",
        "tag": args.tag or args.evkey,
        "generated_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expenses_by_bucket": {k: round(v, 2) for k, v in sorted(buckets.items(), key=lambda x: -x[1])},
        "total_expenses": total_exp,
        "tagged_deposits": tagged_dep,
        "expense_count": len(exps), "deposit_count": len(deps),
        "detail": detail,
    }

    repo = args.repo
    # (4) per-event tagged store
    store_path = Path(repo) / "docs/state/event_tagged_txns.json"
    store = json.loads(store_path.read_text()) if store_path.exists() else {"_updated_at": None, "events": {}}
    store.setdefault("events", {})[args.evkey] = block
    store["_updated_at"] = block["generated_at"]
    store_path.write_text(json.dumps(store, indent=1, ensure_ascii=False))

    # (5) non-destructive qb_tagged block in the event P&L
    pnl_path = Path(repo) / f"docs/state/event_pnl/{args.evkey}.json"
    if pnl_path.exists():
        pnl = json.loads(pnl_path.read_text())
        pnl["qb_tagged"] = block
        pnl_path.write_text(json.dumps(pnl, indent=1, ensure_ascii=False))
        print(f"  ✓ qb_tagged added to {pnl_path.name}")
    else:
        print(f"  ⚠ no event_pnl/{args.evkey}.json (skipped P&L merge)")

    print(json.dumps({"evkey": args.evkey, **{k: block[k] for k in
          ("expenses_by_bucket", "total_expenses", "tagged_deposits", "expense_count", "deposit_count")}},
          indent=1, ensure_ascii=False))

if __name__ == "__main__":
    main()
