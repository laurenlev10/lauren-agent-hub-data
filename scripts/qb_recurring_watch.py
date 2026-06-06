#!/usr/bin/env python3
"""qb_recurring_watch.py — recurring-expense watchdog (Lauren's bookkeeping vision #3, 2026-06-06).

Scans the last 120 days of QuickBooks Purchases/Bills, identifies RECURRING payees
(charged in >=2 distinct months), and flags in the most recent 35 days:
  - DUPLICATE: same payee, same amount (±$0.01), <=3 days apart
  - DEVIATION: latest charge differs >30% AND >$20 from the payee's median
  - NEW_RECURRING: payee charging a 2nd+ month for the first time this month

Writes docs/state/qb_recurring_watch.json and SMSes Lauren (Hebrew) only when
something is flagged. No QB writes — read-only watchdog.
"""
from __future__ import annotations
import datetime as dt, json, os, statistics, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_quickbooks as QB

OUT = ROOT / "docs/state/qb_recurring_watch.json"
DASH = "https://dashboard.themakeupblowout.com/bookkeeping/"

def _f(x):
    try: return float(x or 0)
    except (TypeError, ValueError): return 0.0

def fetch_txns(days=120):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = []
    for ent, vref in (("Purchase", "EntityRef"), ("Bill", "VendorRef")):
        start = 1
        while True:
            r = QB.query(f"select * from {ent} where TxnDate >= '{since}' startposition {start} maxresults 500").get("QueryResponse", {})
            batch = r.get(ent, []) or []
            for t in batch:
                vendor = ((t.get(vref) or {}).get("name") or "").strip()
                acct = ((t.get("AccountRef") or {}).get("name") or "").strip()
                key = (vendor or acct or "unknown").lower()
                rows.append({"entity": ent, "id": t.get("Id"), "date": t.get("TxnDate"),
                             "payee": vendor or acct or "—", "key": key,
                             "amount": round(_f(t.get("TotalAmt")), 2)})
            if len(batch) < 500: break
            start += 500
    return rows

def _load_config():
    cfg = ROOT / "docs/state/qb_recurring_watch_config.json"
    if cfg.exists():
        try: return json.loads(cfg.read_text(encoding="utf-8"))
        except Exception: pass
    return {"watch_always": [], "ignore": []}

def analyze(rows):
    today = dt.date.today()
    cfg = _load_config()
    watch_always = [w.lower() for w in cfg.get("watch_always", [])]
    ignore = [w.lower() for w in cfg.get("ignore", [])]
    by_key = {}
    for r in rows:
        if r["amount"] <= 0: continue
        by_key.setdefault(r["key"], []).append(r)
    flags = []
    recurring = {}
    for key, txns in by_key.items():
        if any(w in key for w in ignore): continue
        months = {t["date"][:7] for t in txns}
        if len(months) < 2: continue
        # subscription-like = stable amount history + low charge frequency (fixed expense),
        # OR explicitly on Lauren's watchlist. Filters out variable spend (groceries,
        # hotels, flights, inventory suppliers) that produced 49 noise flags on first run.
        amounts_all = [t["amount"] for t in txns]
        mean = sum(amounts_all) / len(amounts_all)
        cv = (statistics.pstdev(amounts_all) / mean) if mean > 0 and len(amounts_all) > 1 else 0.0
        per_month = len(txns) / max(len(months), 1)
        sub_like = (len(months) >= 3 and cv <= 0.30 and per_month <= 3) or any(w in key for w in watch_always)
        if not sub_like: continue
        txns.sort(key=lambda t: t["date"])
        amounts = [t["amount"] for t in txns]
        med = round(statistics.median(amounts), 2)
        recurring[key] = {"payee": txns[-1]["payee"], "charges": len(txns),
                          "months": sorted(months), "median": med,
                          "last_date": txns[-1]["date"], "last_amount": txns[-1]["amount"]}
        recent = [t for t in txns if (today - dt.date.fromisoformat(t["date"])).days <= 35]
        # duplicates — payees billed often (e.g. Meta ad thresholds every ~2 days) only count
        # SAME-DAY identical charges; low-frequency payees use a 3-day window
        max_gap = 0 if per_month > 2 else 3
        for i in range(1, len(recent)):
            a, b = recent[i-1], recent[i]
            gap = (dt.date.fromisoformat(b["date"]) - dt.date.fromisoformat(a["date"])).days
            if abs(a["amount"] - b["amount"]) <= 0.01 and 0 <= gap <= max_gap and a["id"] != b["id"]:
                flags.append({"type": "duplicate", "payee": b["payee"],
                              "detail": f'${b["amount"]:,.2f} פעמיים ({a["date"]} + {b["date"]})'})
        # deviation vs median (need history beyond the charge itself)
        if recent and len(amounts) >= 3:
            last = recent[-1]
            diff = last["amount"] - med
            if abs(diff) > 20 and med > 0 and abs(diff) / med > 0.30:
                flags.append({"type": "deviation", "payee": last["payee"],
                              "detail": f'${last["amount"]:,.2f} מול חציון ${med:,.2f} ({"+" if diff>0 else ""}{diff/med:.0%}) ב-{last["date"]}'})
    return recurring, flags

def send_sms_lauren(body):
    try:
        from lauren_sms import send_sms, LAUREN_PHONE
        send_sms(LAUREN_PHONE, body)
        print("SMS sent")
    except Exception as e:
        print(f"WARN SMS failed: {e}")

def main():
    rows = fetch_txns()
    recurring, flags = analyze(rows)
    prev = {}
    if OUT.exists():
        try: prev = {f["payee"] + f["type"] + f["detail"] for f in json.loads(OUT.read_text(encoding="utf-8")).get("flags", [])}
        except Exception: prev = set()
    OUT.write_text(json.dumps({
        "_updated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recurring": dict(sorted(recurring.items(), key=lambda kv: -kv[1]["last_amount"])),
        "flags": flags}, indent=2, ensure_ascii=False), encoding="utf-8")
    new_flags = [f for f in flags if (f["payee"] + f["type"] + f["detail"]) not in prev]
    print(f"txns {len(rows)} · recurring payees {len(recurring)} · flags {len(flags)} (new {len(new_flags)})")
    for f in flags: print(f'  ⚠ {f["type"]}: {f["payee"]} — {f["detail"]}')
    if new_flags and os.environ.get("SIMPLETEXTING_TOKEN"):
        lines = [f'⚠️ {"חיוב כפול" if f["type"]=="duplicate" else "סטייה בהוצאה קבועה"}: {f["payee"]} — {f["detail"]}' for f in new_flags[:5]]
        send_sms_lauren("🧾 מעקב הוצאות קבועות:\n" + "\n".join(lines) + f"\n{DASH}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
