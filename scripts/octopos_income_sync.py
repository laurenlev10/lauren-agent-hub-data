#!/usr/bin/env python3
"""
octopos_income_sync.py — reconcile OCTOPOS card sales vs the merchant-account deposits.

Set up 2026-06-08 (Lauren: weekly "Bnkcd Settle Merch" deposits to WF #1955 for each
weekend's card sales — pull them, map to event by date, assign the QB Class, and verify
no money is missing per event).

Pipeline (token-free except QB + OCTOPOS, both via existing helpers):
  1. Read QB Deposits into "#1955 WF Checking" whose memo contains "BNKCD SETTLE" + "MERCH"
     (the 'Octopos Income' bank rule auto-posts these as Merchandise Sales, no Class).
  2. Map each deposit to an EVENT by date — the event whose weekend most recently ENDED
     before the deposit (settlements land 1-3 business days after the sale; LAG_DAYS guard).
  3. Sum deposits per event; fetch OCTOPOS card total for the event date range; reconcile.
       OCTOPOS card is EXACT when the event has no split (CASH+card) tickets; otherwise a
       tight range (order-level data only gives a payment_types STRING, not the split — see
       the split-ticket note in pnl_octopos / CLAUDE.md). card = gross - cash.
  4. Assign QB Class = "City Year" (matched against live QB Class list) and record each
     deposit's Id + SyncToken so a later push step can stamp the Class onto the QB Deposit.
  5. Write docs/state/octopos_income.json (event-keyed). Merge-safe; agent-owned.

Reconcile status: 'match' (deposits == exact card, ±$1), 'in_range' (within card min/max),
'gap_low'/'gap_high' (outside) — surfaced on the /income/ dashboard + 💰 Income button.
"""
import argparse, datetime as dt, json, sys, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "docs" / "state"
IDX_FILE = STATE / "events_index.json"
OUT_FILE = STATE / "octopos_income.json"

sys.path.insert(0, str(ROOT / "scripts"))
import pnl_quickbooks as qb
import pnl_octopos as octo

BANK_ACCOUNT = "#1955 WF Checking"
MEMO_NEEDLES = ("BNKCD SETTLE", "MERCH")
LAG_DAYS = 16          # a settlement maps to an event whose end_date is within this many days before it
MATCH_TOL = 1.00       # $ tolerance for an exact match


def _now():
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _f(x):
    try:
        return float(str(x).replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------- QB deposits ----------
def fetch_merchant_deposits(since):
    """All BNKCD SETTLE MERCH deposits into #1955 with TxnDate >= since (YYYY-MM-DD)."""
    out, start = [], 1
    while True:
        r = qb.query(f"select * from Deposit where TxnDate >= '{since}' "
                     f"startposition {start} maxresults 100")
        rows = (r.get("QueryResponse") or {}).get("Deposit") or []
        if not rows:
            break
        for d in rows:
            acct = (d.get("DepositToAccountRef") or {}).get("name") or ""
            note = (d.get("PrivateNote") or "")
            # also scan line descriptions for the memo (bank-feed deposits vary)
            descs = " ".join((l.get("Description") or "") for l in (d.get("Line") or []))
            blob = (note + " " + descs).upper()
            if BANK_ACCOUNT in acct and all(n in blob for n in MEMO_NEEDLES):
                out.append({
                    "qb_id": d.get("Id"),
                    "sync_token": d.get("SyncToken"),
                    "date": d.get("TxnDate"),
                    "amount": round(_f(d.get("TotalAmt")), 2),
                    "memo": (note or descs)[:80],
                })
        if len(rows) < 100:
            break
        start += 100
    return out


# ---------- event mapping ----------
def event_for_deposit(date_iso, events_sorted):
    """The event whose end_date most recently precedes the deposit date (within LAG_DAYS)."""
    dd = dt.date.fromisoformat(date_iso)
    best = None
    for e in events_sorted:
        end = e.get("end_date") or e.get("start_date")
        if not end:
            continue
        ed = dt.date.fromisoformat(end)
        if ed <= dd and (dd - ed).days <= LAG_DAYS:
            if best is None or ed > dt.date.fromisoformat(best.get("end_date") or best.get("start_date")):
                best = e
    return best


# ---------- QB class match ----------
def build_class_index():
    try:
        classes = qb.list_classes()
    except Exception:
        classes = []
    idx = {}
    for c in classes:
        # list_classes() returns (Id, Name) tuples
        nm = (c[1] if isinstance(c, (list, tuple)) else c.get("Name") or "").strip()
        if nm:
            idx[re.sub(r"[^a-z0-9]+", "", nm.lower())] = nm
    return idx


def match_class(city, start_date, class_idx):
    year = (start_date or "")[:4]
    cand = re.sub(r"[^a-z0-9]+", "", (city + year).lower())
    if cand in class_idx:
        return class_idx[cand]
    # loose: city tokens + year contained
    cslug = re.sub(r"[^a-z0-9]+", "", city.lower())
    for k, v in class_idx.items():
        if cslug and cslug in k and year in k:
            return v
    return None


# ---------- OCTOPOS card ----------
def octopos_card(jwt, start, end):
    s = octo.fetch_sales_totals(jwt, start, end)
    pb = s.get("payment_breakdown") or {}
    cash_min = round(sum(v for k, v in pb.items() if k.upper().strip() == "CASH"), 2)
    cash_max = round(sum(v for k, v in pb.items() if "CASH" in k.upper()), 2)
    gross = s.get("gross") or 0.0
    card_min = round(gross - cash_max, 2)   # least card (most cash counted)
    card_max = round(gross - cash_min, 2)   # most card (least cash counted)
    exact = card_min if abs(card_max - card_min) < 0.01 else None  # exact only when no split tickets
    return {"gross": round(gross, 2), "cash_min": cash_min, "cash_max": cash_max,
            "card_min": card_min, "card_max": card_max, "card_exact": exact,
            "has_split": cash_max != cash_min, "payment_breakdown": pb,
            "transactions": s.get("transactions")}


# Card processing fee is netted at settlement (dual-pricing surcharge ≈ fee), so deposits run
# ~3-4% below OCTOPOS card. That's NORMAL, not missing money (Lauren 2026-06-08 — show gross+net+fee,
# verify the deposits arrived; only flag clearly-abnormal cases like a missing batch or overpayment).
ABNORMAL_FEE = 0.08   # >8% short = likely a missing settlement batch, worth a look
def reconcile(dep_total, card):
    ref = card["card_exact"] if card["card_exact"] is not None else round((card["card_min"] + card["card_max"]) / 2, 2)
    fee_usd = round(ref - dep_total, 2)
    fee_pct = round(fee_usd / ref, 4) if ref else 0.0
    if dep_total <= 0:
        status = "missing"
    elif dep_total > card["card_max"] + MATCH_TOL:
        status = "over"          # deposited MORE than card sales — investigate
    elif fee_pct > ABNORMAL_FEE:
        status = "check"         # short by more than a normal fee — possible missing batch
    else:
        status = "received"      # deposits arrived; gap = processing fee (normal)
    return {"status": status, "fee_usd": fee_usd, "fee_pct": fee_pct,
            "card_ref": ref, "vs": "exact" if card["card_exact"] is not None else "range"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (default: 120 days ago)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    since = args.since or (dt.date.today() - dt.timedelta(days=120)).isoformat()
    idx = _load(IDX_FILE, {})
    events = sorted((idx.get("events") or []), key=lambda e: e.get("start_date") or "")

    print(f"Reading QB merchant deposits since {since} …")
    deposits = fetch_merchant_deposits(since)
    print(f"  {len(deposits)} merchant deposits found")

    class_idx = build_class_index()
    jwt = octo.octopos_jwt()

    # group deposits by event
    grouped, unassigned = {}, []
    for d in deposits:
        ev = event_for_deposit(d["date"], events)
        if not ev:
            unassigned.append(d); continue
        grouped.setdefault(ev["evkey"], {"event": ev, "deposits": []})["deposits"].append(d)

    out = _load(OUT_FILE, {"_updated_at": None, "events": {}})
    out.setdefault("events", {})

    for evkey, g in sorted(grouped.items()):
        ev = g["event"]
        deps = sorted(g["deposits"], key=lambda x: x["date"])
        dep_total = round(sum(x["amount"] for x in deps), 2)
        card = octopos_card(jwt, ev["start_date"], ev["end_date"])
        rec = reconcile(dep_total, card)
        qbclass = match_class(ev["city"], ev["start_date"], class_idx)
        out["events"][evkey] = {
            "evkey": evkey, "city": ev["city"], "state": ev.get("state"),
            "start_date": ev["start_date"], "end_date": ev["end_date"],
            "qb_class": qbclass,
            "deposits": deps, "deposits_total": dep_total, "deposits_count": len(deps),
            "octopos": card, "reconcile": rec, "synced_at": _now(),
        }
        flag = {"received": "✓", "check": "🔴", "missing": "⚫", "over": "🟠"}.get(rec["status"], "?")
        cardstr = (f"${card['card_exact']:,.2f}" if card["card_exact"] is not None
                   else f"${card['card_min']:,.2f}-${card['card_max']:,.2f}")
        print(f"  {flag} {evkey}: net ${dep_total:,.2f} ({len(deps)}) vs gross {cardstr} "
              f"fee ${rec['fee_usd']:,.2f} ({rec['fee_pct']*100:.1f}%) [{rec['status']}] class={qbclass}")

    if unassigned:
        out["_unassigned"] = unassigned
        print(f"  ⚠ {len(unassigned)} deposits not mapped to any event (outside {LAG_DAYS}d window)")

    out["_updated_at"] = _now()
    if not args.dry:
        OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {OUT_FILE.name}")
    else:
        print("(dry run — not written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
