#!/usr/bin/env python3
"""
Auto-zero phantom low-stock residuals (Lauren 2026-06-08).

Rule: a product with a SMALL residual (1-3 units) that did NOT sell on the last
two days of the most recent event (Saturday + Sunday) is almost certainly really
0 — the residual is accumulated tracking drift, not real stock. Zero it; no
RECOUNT needed. Larger residuals (5+ that didn't sell across the event) go on the
RECOUNT worklist instead (recount_prebuild.py). This script only auto-zeros.

Safety: DRY-RUN by default (prints + writes preview). --apply to PUT 0.
ACTIVE + non-excluded only. Aborts unless an event ended in the last 10 days.
Audit is written per-event (merge-safe, IRON RULE #18) to
docs/state/octopos_phantom_zeros.json so the event-summary dashboard can show it.

Auth: /api/v1 sales report = Bearer JWT (octopos_jwt). /api/v2 products PUT =
RAW v2 token (OCTOPOS_TOKEN env).
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, time
import urllib.error, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recount_prebuild import (
    REPO_ROOT, OCTO_BASE, OCTO_UA,
    find_previous_event, octopos_jwt, fetch_real_sales_pids, is_permanent_exclude, sms_lauren,
)

ZERO_MIN_QTY = 1
ZERO_MAX_QTY = 3
NEG_ZERO_MIN = -2   # small negatives -1,-2 are drift → zero too (Lauren 2026-06-08)
NEG_ZERO_MAX = -1
LAST_EVENT_MAX_AGE_DAYS = 10
AUDIT_PATH = REPO_ROOT / "docs/state/octopos_phantom_zeros.json"


def _evkey(ev):
    return (ev.get("city") or "").strip().lower().replace(" ", "-") + "-" + ev.get("start_date", "")


def _get_product(pid, raw_token):
    req = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
        headers={"Authorization": raw_token, "Accept": "application/json", "User-Agent": OCTO_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    return j.get("data", j)


def _put_stock_zero(pid, raw_token):
    req = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
        data=json.dumps({"in_stock_qty": 0}).encode(),
        headers={"Authorization": raw_token, "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": OCTO_UA}, method="PUT")
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    return r.status, j.get("data", j)


def find_candidates(snapshot, sold_satsun_pids):
    out = []
    for code, vdata in (snapshot.get("vendors") or {}).items():
        supplier = vdata.get("display_name") or vdata.get("name") or code
        for p in (vdata.get("products") or []):
            if is_permanent_exclude(p) or not p.get("active", True):
                continue
            try:
                qty = float(p.get("in_stock_qty") or 0)
            except (TypeError, ValueError):
                continue
            pid = int(p.get("id") or 0)
            is_pos = (ZERO_MIN_QTY <= qty <= ZERO_MAX_QTY) and (pid not in sold_satsun_pids)
            is_neg = (NEG_ZERO_MIN <= qty <= NEG_ZERO_MAX)   # -1, -2 small drift — always zero
            if not (is_pos or is_neg):
                continue
            out.append({"id": pid, "sku": (p.get("sku") or "").strip(),
                        "name": (p.get("name") or "").strip(), "supplier": supplier, "qty": qty,
                        "kind": ("neg_drift" if is_neg else "pos_residual")})
    out.sort(key=lambda x: (x["supplier"], -x["qty"], x["name"]))
    return out


def _load_audit():
    if AUDIT_PATH.exists():
        try:
            d = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "events" in d:
                return d
        except Exception:
            pass
    return {"_updated_at": None, "events": {}}


def _save_audit_event(evkey, payload):
    """Merge-safe per IRON RULE #18 — overlay this event only, never clobber others."""
    d = _load_audit()
    d.setdefault("events", {})[evkey] = payload
    d["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    AUDIT_PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    today = dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date()
    prior = find_previous_event(today + dt.timedelta(days=1))
    if not prior:
        print("No prior event found — aborting."); return 0
    evkey = _evkey(prior)
    sun = dt.date.fromisoformat(prior["end_date"]); sat = sun - dt.timedelta(days=1)
    age = (today - sun).days
    print(f"Last event: {prior.get('city')}, {prior.get('state')} (evkey={evkey}) ended {sun} (age {age}d). Sat-Sun = {sat} -> {sun}")
    if not (0 <= age <= LAST_EVENT_MAX_AGE_DAYS):
        print(f"Last event {age}d ago (>{LAST_EVENT_MAX_AGE_DAYS}) — aborting to avoid wrongful zeroing."); return 0

    snapshot = json.loads((REPO_ROOT / "docs/state/octopos_products.json").read_text(encoding="utf-8"))
    print(f"Snapshot _updated_at = {snapshot.get('_updated_at')}")
    jwt = octopos_jwt()
    sold_pids, _ = fetch_real_sales_pids(jwt, sat.isoformat(), sun.isoformat())
    print(f"Products that sold >=1 unit Sat+Sun: {len(sold_pids)}")

    cands = find_candidates(snapshot, sold_pids)
    by_qty = {}
    for c in cands: by_qty[int(c["qty"])] = by_qty.get(int(c["qty"]), 0) + 1
    print(f"\n=== ZERO CANDIDATES: {len(cands)} (active, qty 1-3, zero Sat+Sun sales) ===")
    print("  by qty:", dict(sorted(by_qty.items())))
    for c in cands[:30]:
        print(f"    #{c['id']:>6}  qty={int(c['qty'])}  [{c['supplier']}]  {c['name'][:46]}")

    payload = {
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_event": {"city": prior.get("city"), "state": prior.get("state"),
                       "start": prior.get("start_date"), "sat": sat.isoformat(), "sun": sun.isoformat()},
        "rule": {"pos_min": ZERO_MIN_QTY, "pos_max": ZERO_MAX_QTY, "neg_min": NEG_ZERO_MIN, "neg_max": NEG_ZERO_MAX, "window": "sat-sun", "no_recount": True},
        "candidate_count": len(cands),
        "applied": False,
        "zeroed": [],
        "candidates": cands,
    }

    if not args.apply:
        _save_audit_event(evkey, payload)
        print(f"\nDRY-RUN — preview written for {evkey}. No changes. Re-run with --apply to zero them.")
        return 0

    raw_token = os.environ.get("OCTOPOS_TOKEN", "").strip()
    if not raw_token:
        print("OCTOPOS_TOKEN not set — cannot apply.", file=sys.stderr); return 1
    todo = cands if args.limit <= 0 else cands[:args.limit]
    print(f"\n=== APPLYING — zeroing up to {len(todo)} ===")
    zeroed = []; ok = fail = skip = 0
    for c in todo:
        pid = c["id"]
        try:
            before = float(_get_product(pid, raw_token).get("in_stock_qty") or 0)
            in_range = (ZERO_MIN_QTY <= before <= ZERO_MAX_QTY) or (NEG_ZERO_MIN <= before <= NEG_ZERO_MAX)
            if not in_range:
                # Already resolved. If it's already 0 it was zeroed by a prior run today —
                # still record it (using snapshot qty as "before") so the per-event audit
                # shows the complete set. Otherwise (restocked) drop silently.
                if before == 0:
                    zeroed.append({"id": pid, "sku": c["sku"], "name": c["name"],
                                   "supplier": c["supplier"], "before": c["qty"], "after": 0,
                                   "http": "prior", "kind": c.get("kind")})
                skip += 1; continue
            status, after = _put_stock_zero(pid, raw_token)
            aq = float(after.get("in_stock_qty", 0) or 0)
            rec = {"id": pid, "sku": c["sku"], "name": c["name"], "supplier": c["supplier"],
                   "before": before, "after": aq, "http": status, "kind": c.get("kind")}
            zeroed.append(rec)
            if status == 200 and aq == 0: ok += 1
            else: fail += 1
            time.sleep(0.15)
        except Exception as e:
            fail += 1; zeroed.append({"id": pid, "name": c["name"], "error": str(e)[:160]})
    payload.update({"applied": True, "zeroed": zeroed,
                    "apply_summary": {"zeroed": ok, "failed": fail, "skipped": skip, "attempted": len(todo)}})
    _save_audit_event(evkey, payload)
    print(f"\nDONE — zeroed {ok}, failed {fail}, skipped {skip}. Audit at {AUDIT_PATH}")
    if ok or fail:
        body = (f"\u2713 \u05d0\u05d9\u05e4\u05d5\u05e1 \u05e4\u05e0\u05d8\u05d5\u05dd \u2014 {prior.get('city')} ({ok} \u05de\u05d5\u05e6\u05e8\u05d9\u05dd \u05d0\u05d5\u05e4\u05e1\u05d5"
                + (f", {fail} \u05e0\u05db\u05e9\u05dc\u05d5" if fail else "") + ").\n"
                + f"https://dashboard.themakeupblowout.com/event-summary/?evkey={evkey}")
        try: sms_lauren(body)
        except Exception as e: print(f"(SMS failed: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
