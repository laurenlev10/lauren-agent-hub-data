#!/usr/bin/env python3
"""
@recount — per-event THREE-LIST builder (Lauren spec 2026-06-22).

Lauren's directive: every event's recount dashboard shows THREE lists:

  1. רשימת ספירה (to_count)        = every product that currently carries the
                                     RECOUNT tag (OCTOPOS category "Recount",
                                     id 14) — RAW, no smart filtering. Shows
                                     before the event too (the plan).
  2. נספרו במהלך האירוע (counted)  = every product that appears in the OCTOPOS
                                     RECOUNT report for the Fri->Sun window
                                     (both DR and CR rows = physically counted,
                                     IRON RULE #9 trap B), whether or not it was
                                     on list 1.
  3. תג אבל לא נספרו (tagged_not_counted) = list 1 minus list 2 (still tagged,
                                     never counted at the event).

Plus a per-event "didn't sell at all" set (all ACTIVE products with zero
units_sold in the Fri->Sun window — Lauren 2026-06-22 chose "all active").

This module is the single source of truth for the list math. Imported by
recount_weekend.py (writes the lists into octopos_recount.json) and runnable
standalone to (re)populate one event.

IT NEVER MUTATES OCTOPOS. Tag removal stays in recount_weekend.py and is
explicitly gated by Lauren. This file is read-only against OCTOPOS.
"""
import os, sys, json, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recount_prebuild as P  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPO_ROOT / "docs/state/octopos_products.json"
STATE_PATH = REPO_ROOT / "docs/state/octopos_recount.json"


def _enrich_from_snapshot(snapshot):
    by_pid = {}
    for code, vd in (snapshot.get("vendors") or {}).items():
        supplier = vd.get("display_name") or vd.get("name") or code
        for p in (vd.get("products") or []):
            try:
                pid = int(p.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not pid:
                continue
            cats = [(c.get("name") or "").strip().lower() for c in (p.get("categories") or [])]
            by_pid[pid] = {
                "pid": pid,
                "name": p.get("name") or "",
                "sku": p.get("sku") or "",
                "supplier": supplier,
                "stock": float(p.get("in_stock_qty") or 0),
                "active": bool(p.get("active", True)),
                "has_recount": "recount" in cats,
            }
    return by_pid


def build_lists(start, end, jwt=None, snapshot=None):
    if snapshot is None:
        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if jwt is None:
        jwt = P.octopos_jwt()

    by_pid = _enrich_from_snapshot(snapshot)

    # LIST 1 — currently tagged "Recount" (raw)
    tagged_pids = sorted(pid for pid, m in by_pid.items() if m["has_recount"])

    # LIST 2 — counted at the event (recount report, DR+CR, client-side window filter)
    counted_rows = P.fetch_recount_data(jwt, start, end) if hasattr(P, "fetch_recount_data") else []
    if not counted_rows:
        code, resp = P.http_post(
            f"{P.OCTO_BASE}/api/v1/get-recount-data",
            {"location_id": 2, "start_date": start, "end_date": end,
             "limit": 5000, "page": 1, "order": "id", "order_type": "desc", "filter": ""},
            {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
        all_rows = (resp.get("data") or {}).get("data") or []

        def _in_win(r):
            try:
                d = dt.datetime.strptime(str(r.get("created_at") or "").split()[0], "%m/%d/%Y").date()
                return dt.date.fromisoformat(start) <= d <= dt.date.fromisoformat(end)
            except Exception:
                return False
        counted_rows = [r for r in all_rows if _in_win(r)]

    counted_meta = {}
    for r in counted_rows:
        try:
            pid = int(r.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        prev = counted_meta.get(pid)
        ca = str(r.get("created_at") or "")
        if not prev or ca > (prev.get("last_counted_at") or ""):
            counted_meta[pid] = {
                "name": r.get("product_name") or (by_pid.get(pid, {}).get("name") or ""),
                "brand": r.get("brand_name") or "",
                "last_counted_at": ca,
                "balance": r.get("balance"),
            }
    counted_pids = sorted(counted_meta.keys())

    # LIST 3 — tagged but NOT counted
    tagged_set = set(tagged_pids)
    counted_set = set(counted_pids)
    tagged_not_counted_pids = sorted(tagged_set - counted_set)

    # DIDN'T SELL — ACTIVE products with stock > 0 and zero units sold Fri->Sun.
    # Lauren 2026-06-22: drop stock==0 (nothing to sell anyway); keep only what
    # "supposedly has stock but didn't move".
    sold_pids, _ = P.fetch_real_sales_pids(jwt, start, end)
    in_stock_active = {pid for pid, m in by_pid.items()
                       if m["active"] and (m.get("stock") or 0) > 0}
    didnt_sell_pids = sorted(in_stock_active - set(sold_pids))

    def entry(pid, extra=None):
        m = by_pid.get(pid, {})
        e = {
            "pid": pid,
            "name": (extra or {}).get("name") or m.get("name") or f"#{pid}",
            "sku": m.get("sku") or "",
            "supplier": m.get("supplier") or (extra or {}).get("brand") or "",
            "stock": m.get("stock"),
        }
        if extra:
            for k in ("last_counted_at", "balance"):
                if extra.get(k) is not None:
                    e[k] = extra[k]
        return e

    to_count = [dict(entry(pid), counted=(pid in counted_set)) for pid in tagged_pids]
    counted = [dict(entry(pid, counted_meta.get(pid)), was_tagged=(pid in tagged_set)) for pid in counted_pids]
    tagged_not_counted = [entry(pid) for pid in tagged_not_counted_pids]
    didnt_sell = [entry(pid) for pid in didnt_sell_pids]

    today = dt.date.today()
    phase = "post_event" if today > dt.date.fromisoformat(end) else (
        "live" if today >= dt.date.fromisoformat(start) else "pre_event")

    return {
        "_generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": start, "end": end},
        "phase": phase,
        "snapshot_at": snapshot.get("_updated_at"),
        "counts": {
            "to_count": len(to_count),
            "counted": len(counted),
            "tagged_not_counted": len(tagged_not_counted),
            "counted_extra": len(counted_set - tagged_set),
            "didnt_sell": len(didnt_sell),
        },
        "to_count": to_count,
        "counted": counted,
        "tagged_not_counted": tagged_not_counted,
        "didnt_sell": didnt_sell,
    }


def write_into_state(evkey, lists_payload, state_path=STATE_PATH):
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("events", {}).setdefault(evkey, {})
    state["events"][evkey]["lists"] = lists_payload
    state["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    payload = build_lists(a.start, a.end)
    print(json.dumps(payload["counts"], ensure_ascii=False, indent=2))
    if a.dry:
        print("(dry - not written)")
    else:
        write_into_state(a.evkey, payload)
        print(f"wrote lists into events[{a.evkey}].lists")
