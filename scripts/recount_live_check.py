#!/usr/bin/env python3
"""
recount_live_check.py — LIVE "who has been counted so far?" check (set 2026-07-05).

Lauren's need: while an event is still running she wants to know which of the
products she asked the manager to count (the RECOUNT worklist) have ACTUALLY been
counted in OCTOPOS yet — so she can remind the manager about the ones still not
counted BEFORE the event ends.

This is the same signal @recount computes at the END of the event (Sunday 17:15),
but on demand, mid-event. It is dispatched from the recount dashboard's
"Live check" button (workflow_dispatch, input `evkey`).

How "counted" is determined — IRON RULE #9 (trap A/B/C):
  * Source of truth = /api/v1/get-recount-data (Bearer JWT + Permission
    report-inventary-recount). A product_id appearing in that report within the
    event window means it was PHYSICALLY COUNTED (adjustment row).
  * BOTH type "DR" and "CR" rows count (DR = count revealed shrinkage, CR =
    count revealed overage). We simply take every product_id in the window.
  * The endpoint IGNORES start_date/end_date (trap C) — filter client-side by
    parsing created_at (MM/DD/YYYY).

Output: writes slice["live"] into docs/state/octopos_recount.json for the given
evkey (MERGE-safe — only that event's `live` key is touched). The dashboard reads
it and marks each worklist product counted / not-yet + a remaining-list reminder.

Env: OCTOPOS_EMAIL, OCTOPOS_PASSWORD.
Usage: python3 scripts/recount_live_check.py --evkey fort-collins-2026-07-03
"""

import argparse
import datetime as dt
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OCTO_BASE = "https://themakeup.octoretail.com"
STATE_FILE = REPO_ROOT / "docs" / "state" / "octopos_recount.json"

# 2026-05-20: Cloudflare blocks the default urllib UA (1010) — send a browser UA.
OCTO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def http_post(url, body, headers, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": OCTO_UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def octopos_jwt():
    email = os.environ["OCTOPOS_EMAIL"]
    pw = os.environ["OCTOPOS_PASSWORD"]
    code, resp = http_post(f"{OCTO_BASE}/api/v1/authenticate",
                           {"email": email, "password": pw}, {})
    if code != 200 or not resp.get("flag"):
        raise SystemExit(f"OCTOPOS login failed: HTTP {code} {resp}")
    return resp["data"]["token"]


def _parse_created(row):
    """created_at is 'MM/DD/YYYY HH:MM:SS' — return a date or None."""
    ca = str(row.get("created_at") or "").split()[0]
    try:
        return dt.datetime.strptime(ca, "%m/%d/%Y").date()
    except Exception:
        return None


def fetch_counted_pids(jwt, start, end, location_id=2):
    """Return the set of product_ids physically counted within [start, end].

    Pages newest-first (order id desc). Stops once a page's rows are entirely
    older than the window start (we've paged past the event) or the API is
    exhausted. Client-side date filter — the API ignores start_date/end_date.
    """
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    counted = set()
    total_rows = 0
    page = 1
    MAX_PAGES = 8  # safety cap (8 * 1000 recount events back covers many months)
    while page <= MAX_PAGES:
        code, resp = http_post(
            f"{OCTO_BASE}/api/v1/get-recount-data",
            {"location_id": location_id, "start_date": start, "end_date": end,
             "limit": 1000, "page": page, "order": "id", "order_type": "desc",
             "filter": ""},
            {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
        if code != 200 or not resp.get("flag"):
            raise SystemExit(f"get-recount-data failed: HTTP {code} {resp}")
        data = resp.get("data") or {}
        rows = data.get("data") or []
        total_rows += len(rows)
        if not rows:
            break
        page_dates = []
        for r in rows:
            d = _parse_created(r)
            if d is None:
                continue
            page_dates.append(d)
            if start_d <= d <= end_d:
                try:
                    counted.add(int(r["product_id"]))
                except (KeyError, ValueError, TypeError):
                    pass
        # Rows are newest-first. If the whole page is older than the window
        # start, everything after it is older too — stop.
        if page_dates and max(page_dates) < start_d:
            break
        total_items = data.get("totalItems")
        if total_items is not None and page * 1000 >= int(total_items):
            break
        page += 1
    print(f"fetch_counted_pids: scanned {total_rows} rows over {page} page(s); "
          f"{len(counted)} unique product_ids counted in {start}..{end}")
    return counted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--dry", action="store_true", help="don't write the file")
    args = ap.parse_args()
    evkey = args.evkey.strip()

    state = json.loads(STATE_FILE.read_text())
    slice_ = (state.get("events") or {}).get(evkey)
    if slice_ is None:
        raise SystemExit(f"evkey {evkey!r} not found in {STATE_FILE.name}")

    # Event window — prefer the slice's stored window; else derive from evkey
    # (last 10 chars = start_date; end = start + 2 days, Fri->Sun).
    win = slice_.get("window") or {}
    start = (win.get("start") or "").strip()
    end = (win.get("end") or "").strip()
    if not start:
        start = evkey[-10:]
    if not end:
        try:
            end = (dt.date.fromisoformat(start) + dt.timedelta(days=2)).isoformat()
        except Exception:
            end = start
    print(f"evkey={evkey} window={start}..{end}")

    # Worklist = the products Lauren asked the manager to count.
    worklist = slice_.get("worklist") or []
    wl_ids = []
    wl_by_id = {}
    for w in worklist:
        try:
            pid = int(w.get("id"))
        except (TypeError, ValueError):
            continue
        wl_ids.append(pid)
        wl_by_id[pid] = w

    jwt = octopos_jwt()
    counted_all = fetch_counted_pids(jwt, start, end)

    counted_on_list = sorted(pid for pid in wl_ids if pid in counted_all)
    remaining = [
        {"id": pid,
         "sku": (wl_by_id[pid].get("sku") or ""),
         "name": (wl_by_id[pid].get("name") or f"#{pid}"),
         "supplier": (wl_by_id[pid].get("supplier") or ""),
         "stock": (None if wl_by_id[pid].get("qty") is None else wl_by_id[pid].get("qty"))}
        for pid in wl_ids if pid not in counted_all
    ]

    live = {
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": start, "end": end},
        "counted_pids": sorted(counted_all),
        "counted_on_list": counted_on_list,
        "worklist_total": len(wl_ids),
        "counted_count": len(counted_on_list),
        "remaining_count": len(remaining),
        "remaining": remaining,
    }
    slice_["live"] = live

    print(f"worklist={len(wl_ids)} · counted={len(counted_on_list)} · "
          f"remaining={len(remaining)}")
    if remaining:
        print("STILL NOT COUNTED:")
        for r in remaining:
            print(f"  * {r['name']} ({r['sku']})")

    if args.dry:
        print("(--dry: not writing)")
        return
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"wrote live block to {STATE_FILE.name} for {evkey}")


if __name__ == "__main__":
    main()
