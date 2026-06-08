#!/usr/bin/env python3
"""
@recount pre-event worklist builder.

Lauren's directive (2026-05-21 PM): "הכפתור RECOUNT מופיע בחלון של האירוע וזה
אמור להיות שימושי לפני האירוע - אני אמורה להכין רשימה לספירה לצוות בזמן האירוע".

This script runs Thursday morning before each weekend event. It builds the
recount worklist for the UPCOMING event so Lauren has the count list ready
to hand to staff Friday morning. Companion to recount_weekend.py (which runs
DURING the event for cleanup, not BEFORE for prep).

Algorithm (subset of octopos-recount/SKILL.md — the parts we can run without
the unverified sales-movement endpoint):
  1. Read the OCTOPOS products snapshot from docs/state/octopos_products.json.
  2. Determine the UPCOMING event window from launch/index.html SCHEDULE
     (next Fri-Sun within 1-7 days from today).
  3. Determine the LAST event window (most recent past Fri-Sun).
  4. Fetch counted_pids from last event window via /api/v1/get-recount-data.
  5. Build worklist:
       negative   = qty < 0
       preexisting = currently tagged 'Recount' AND not just counted
       stale       = qty > 0 AND NOT counted last weekend AND NOT already tagged
     minus PERMANENT_EXCLUDE_PRODUCT_IDS and PERMANENT_EXCLUDE_CATEGORIES.
  6. Write to docs/state/octopos_recount.json keyed by upcoming evkey.
  7. SMS Lauren when ready.

Crons in recount-prebuild.yml schedule this to fire Thursday 8 AM PT (which
maps to event-local 10/11 AM in ET/CT/MT, comfortably before staff arrives
Friday morning).
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
OCTO_BASE = "https://themakeup.octoretail.com"
OCTO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Lauren's permanent excludes — see octopos-recount/recount.md
PERMANENT_EXCLUDE_CATEGORIES = {"Market"}
PERMANENT_EXCLUDE_PRODUCT_IDS = {1000, 1001, 1002, 1003, 1011, 921}
PERMANENT_EXCLUDE_NAMES = {
    "Roll Shrink", "Plastic Bags", "Gifts - Glitters", "Gifts - Eyeshadows",
    "Romantic Soft Focus Setting Powder - Translucent", "Mini Fan",
}


def http_post(url, body, headers, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={**headers, "Content-Type":"application/json", "Accept":"application/json", "User-Agent": OCTO_UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except: return e.code, {}


def octopos_jwt():
    email = os.environ["OCTOPOS_EMAIL"]
    pw    = os.environ["OCTOPOS_PASSWORD"]
    code, resp = http_post(f"{OCTO_BASE}/api/v1/authenticate", {"email":email,"password":pw}, {})
    if code != 200 or not resp.get("flag"):
        raise SystemExit(f"OCTOPOS login failed: HTTP {code} {resp}")
    return resp["data"]["token"]


def parse_schedule():
    """Extract SCHEDULE from docs/launch/index.html.

    SCHEDULE is a dict keyed by year ({"2026":[...], "2027":[...], "_baked_at":...}).
    This flattens to a single list of event dicts across all years.
    """
    html = (REPO_ROOT / "docs/launch/index.html").read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE\s*=\s*(\{[\s\S]*?\});\s*\n", html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    events = []
    if isinstance(data, dict):
        for year_key, year_events in data.items():
            if year_key.startswith("_"):
                continue
            if isinstance(year_events, list):
                events.extend(year_events)
    elif isinstance(data, list):
        events = data
    return events


def find_upcoming_event():
    """Find the next Fri-Sun event whose start_date is within 1-7 days from today (PT)."""
    today = dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date()
    candidates = []
    for ev in parse_schedule():
        try:
            sd = dt.date.fromisoformat((ev.get("start_date") or "").strip())
        except ValueError:
            continue
        delta = (sd - today).days
        if 0 <= delta <= 7:  # today through next 7 days
            candidates.append((delta, ev))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def find_previous_event(before_date):
    """Find the most recent past Fri-Sun event whose end_date is BEFORE before_date."""
    candidates = []
    for ev in parse_schedule():
        try:
            ed = dt.date.fromisoformat((ev.get("end_date") or "").strip())
        except ValueError:
            continue
        if ed < before_date:
            candidates.append((ed, ev))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def is_permanent_exclude(p):
    if p.get("id") in PERMANENT_EXCLUDE_PRODUCT_IDS:
        return True
    if (p.get("name") or "").strip() in PERMANENT_EXCLUDE_NAMES:
        return True
    cats = {(c.get("name") or "").strip() for c in (p.get("categories") or [])}
    if cats & PERMANENT_EXCLUDE_CATEGORIES:
        return True
    return False


def fetch_all_vendors(jwt):
    """Fetch the LIVE vendor list from OCTOPOS — single source of truth.
    Lauren 2026-05-21 PM #15 — the hard-coded VENDORS list in scripts/octopos_sync.py
    has 3 wrong IDs:
      she-makeup=[8] but OCTOPOS id=8 is 'Golden Touch' (real She is id=18)
      mystery-box=[9] but OCTOPOS id=9 is 'Joya Mia'
      amuse-cosmetics=[1] but OCTOPOS id=1 is 'Amorus' (real Amuse is id=2)
    Result: SHE Retractable Eye Lip Pencil sold 15 units at Milwaukee but
    appeared in sat_unsold because fetch_real_sales never queried vendor 18.
    Fix: pull the vendor list live + iterate ALL 23 vendors, not just a stale
    subset.
    """
    req = urllib.request.Request(
        f"{OCTO_BASE}/api/v1/get-attributes-for-sales-by-vendor-report",
        headers={"Authorization": f"Bearer {jwt}", "Permission": "report-total-sales-vendor",
                 "Accept":"application/json", "User-Agent": OCTO_UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        vendors = (d.get("data") or {}).get("vendor") or (d.get("data") or {}).get("vendors") or []
        return [(int(v["id"]), v.get("name") or "") for v in vendors]
    except Exception as e:
        print(f"WARN: vendor list fetch failed: {e}", file=sys.stderr)
        return []


def fetch_real_sales_pids(jwt, start_date, end_date):
    """Call /api/v1/get-sales-by-vendor-product-report per vendor and union the
    set of pids with units_sold > 0. This is the REAL sales signal — the DR
    rows in get-recount-data are inventory adjustments, NOT POS sales.

    Lauren 2026-05-21 PM #10 — confirmed via 'BC Kiss And Tell Duo Lip' (sold 12)
    Lauren 2026-05-21 PM #15 — extended via SHE retractable eye lip pencil-13 (sold 15)
    Both were misclassified as sat_unsold; this function now queries the LIVE
    vendor list (not a stale hard-coded subset) so no vendor is missed.
    """
    VENDORS = fetch_all_vendors(jwt)
    if not VENDORS:
        # Fallback to a minimal hardcoded list (best-effort), but warn loud
        print("WARN: empty vendor list — using hardcoded fallback (some products will be missed)", file=sys.stderr)
        VENDORS = [
            (18, "She"), (2,  "Amuse"), (13, "Market"),
            (14, "Nabi"),       (3,  "BB&W"),    (15, "Prolux"),
            (6,  "EBS Perfumes"),(17, "Rude"),       (23, "Xime"),
            (7,  "Feral Edge"), (12, "Lurella"),     (10, "Kara Beauty"),
            (16, "Romantic Beauty"), (4, "Beauty Creations"),
        ]
    # OCTOPOS expects dates as "MM/DD/YYYY HH:MM:SS"
    df = dt.date.fromisoformat(start_date).strftime("%m/%d/%Y") + " 00:00:00"
    dt_ = dt.date.fromisoformat(end_date).strftime("%m/%d/%Y") + " 23:59:59"
    sold_pids = set()
    sold_qty = {}  # pid -> units_sold (cumulative across vendors, but each pid is single-vendor anyway)
    for vid, vname in VENDORS:
        body = {
            "data": {
                "location": {"label": "THE MAKEUP BLOWOUT SALE GROUP INC",
                             "value": {"id": 2, "name": "THE MAKEUP BLOWOUT SALE GROUP INC"}},
                "departments": [], "categories": [],
                "vendor": [{"id": vid, "name": vname}],
                "dateFrom": df, "dateTo": dt_,
            },
            "query": {"limit": 5000, "page": 1, "order": "name", "order_type": "asc", "filter": ""}
        }
        try:
            code, resp = http_post(
                f"{OCTO_BASE}/api/v1/get-sales-by-vendor-product-report",
                body,
                {"Authorization": f"Bearer {jwt}", "Permission": "report-total-sales-vendor"})
            if code != 200 or not resp.get("flag"):
                print(f"  WARN: sales fetch for vendor {vname} HTTP {code}", file=sys.stderr)
                continue
            prods = (resp.get("data") or {}).get("products") or []
            for p in prods:
                u = int(p.get("units_sold") or 0)
                if u > 0:
                    pid = int(p["id"])
                    sold_pids.add(pid)
                    sold_qty[pid] = u
        except Exception as e:
            print(f"  WARN: vendor {vname} fetch error: {e}", file=sys.stderr)
    print(f"  real sold pids ({start_date} → {end_date}): {len(sold_pids)}")
    return sold_pids, sold_qty


def fetch_ever_counted_pids(jwt, lookback_days=90):
    """Pull recount-data over the last N days. Return set of pids that ever
    appeared in a CR (physical count) row. Lauren 2026-05-21 PM #5 — a product
    is 'never counted' if it has never been physically verified at ANY past
    event, not just the most recent one. Used to flag new_unverified products."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=lookback_days)).isoformat()
    end   = today.isoformat()
    code, resp = http_post(
        f"{OCTO_BASE}/api/v1/get-recount-data",
        {"location_id": 2, "start_date": start, "end_date": end,
         "limit": 50000, "page": 1, "order": "id", "order_type": "desc", "filter": ""},
        {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
    if code != 200 or not resp.get("flag"):
        print(f"WARN: historical recount fetch failed (HTTP {code}) — proceeding with empty set", file=sys.stderr)
        return set()
    rows = resp.get("data", {}).get("data", [])
    ever_counted = {int(r["product_id"]) for r in rows if r.get("type") == "CR"}
    print(f"  historical CR pids (last {lookback_days}d): {len(ever_counted)}")
    return ever_counted


def fetch_activity_pids(jwt, start_date, end_date):
    """Pull product activity from OCTOPOS for the given window.
    Returns dict with two sets:
      'sale_pids'  = (legacy, kept for back-compat) — left empty. Real sales come from
                     fetch_real_sales_pids() / get-sales-by-vendor-product-report per IRON RULE #9.
      'count_pids' = pids that had ANY inventory-adjustment row (DR OR CR) in the window —
                     i.e. were physically counted during the event.

    🛑 Lauren 2026-05-22 fix — TWO bugs were here:
      (1) The OCTOPOS API ignores start_date/end_date on /get-recount-data — it returns
          ALL rows YTD (1262 rows, 586 unique pids). The old code stuffed all of them
          into the set, so count_pids reported "586 products counted at Milwaukee" when
          really only ~120 products were counted in that 3-day window. Fix: filter
          client-side by parsing the row's created_at (format MM/DD/YYYY HH:MM:SS).
      (2) Both DR and CR rows are inventory ADJUSTMENT events. A physical count that
          discovers SHRINKAGE writes a DR row (system says 191, real is 1 -> qty_delta=-190
          → DR). The old code treated DR as 'sale' and only CR as 'count', so any product
          counted-with-shrinkage-discovered (the common case for Lauren's events) was
          mis-classified as 'sold but not counted'. Concrete victim: XB-789 Xime Go
          Bananas Powder was counted at Milwaukee on 5/17 08:50 (DR -190 → 1) but the
          prebuild flagged it as 🔵 קיים מקודם on the Roseville worklist.
    """
    code, resp = http_post(
        f"{OCTO_BASE}/api/v1/get-recount-data",
        {"location_id": 2, "start_date": start_date, "end_date": end_date,
         "limit": 5000, "page": 1, "order": "id", "order_type": "desc", "filter": ""},
        {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
    if code != 200 or not resp.get("flag"):
        print(f"WARN: get-recount-data failed (HTTP {code}) — proceeding with empty activity sets", file=sys.stderr)
        return {"sale_pids": set(), "count_pids": set()}

    # Paginate — API ignores date filter but DOES paginate (totalItems can exceed limit).
    all_rows = list(resp.get("data", {}).get("data", []))
    total = (resp.get("data") or {}).get("totalItems") or len(all_rows)
    page = 2
    while len(all_rows) < total and page < 20:
        c2, r2 = http_post(
            f"{OCTO_BASE}/api/v1/get-recount-data",
            {"location_id": 2, "start_date": start_date, "end_date": end_date,
             "limit": 5000, "page": page, "order": "id", "order_type": "desc", "filter": ""},
            {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
        if c2 != 200 or not r2.get("flag"): break
        more = r2.get("data", {}).get("data", [])
        if not more: break
        all_rows.extend(more)
        page += 1

    # Client-side date filter — API ignores start_date/end_date.
    from datetime import datetime as _dt
    def _in_window(created_at):
        try:
            d = _dt.strptime(str(created_at).split()[0], "%m/%d/%Y").date()
            s = _dt.fromisoformat(start_date).date()
            e = _dt.fromisoformat(end_date).date()
            return s <= d <= e
        except Exception:
            return False

    count_pids = set()
    for row in all_rows:
        if not _in_window(row.get("created_at")):
            continue
        try:
            pid = int(row.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if pid:
            # Both DR and CR count as "physically counted". (DR = count revealed shrinkage,
            # CR = count revealed overage.) Either way, a human counted it.
            count_pids.add(pid)
    # sale_pids kept as empty set for back-compat with the build_worklist signature.
    # Real sales come from fetch_real_sales_pids() (sales-by-vendor-product report).
    return {"sale_pids": set(), "count_pids": count_pids}


def build_worklist(snapshot, activity, prior_start, prior_end, ever_counted_pids, real_sold_pids, real_sold_qty, stale_enabled=True):
    """Iterate the OCTOPOS snapshot. Return enriched worklist entries.

    Lauren 2026-05-21 PM #4 — only list products that WERE at the prior event.
    A product with zero activity in the prior window simply wasn't there to
    sell or be counted — listing it on a recount is noise, not signal.

    Categories on the list:
      🔴 negative           = qty < 0 (always — strongest 'something is wrong')
      🆕 new_unverified     = created AFTER prior event ended (never had a chance)
      🔵 preexisting        = currently tagged 'Recount' in OCTOPOS
      💤 moved_not_counted  = at the event (had DR sale OR updated_at change)
                              BUT was not physically counted (no CR row)
    Skipped (NOT on the list):
      ❌ wasn't at last event (no activity at all) — Lauren's rule
      ✅ moved AND counted — qty trustworthy
    """
    # OLD: sale_pids from get-recount-data DR rows — that was INVENTORY ADJUSTMENTS,
    # not POS sales. Replaced with real_sold_pids from get-sales-by-vendor-product-report.
    sale_pids   = real_sold_pids or set()
    count_pids  = activity.get("count_pids")  or set()

    worklist = []
    n_neg = n_sat_unsold = n_preexisting = 0
    n_neg_skipped_counted = 0  # qty<0 but counted in prior window — skip
    n_preexisting_stale = 0  # tagged RECOUNT but counted in prior window (stale tag)
    n_excluded = 0
    n_not_at_event = 0       # qty>0 but no activity — wasn't at the event
    n_already_counted = 0    # had CR row — trust the count, skip
    n_sold = 0               # had DR row — sold, trust the qty, skip

    # 'moved_pids' from snapshot updated_at — back-up signal in case OCTOPOS
    # recount-data missed a sale (the API timing isn't perfectly synced).
    moved_pids = set()
    for code, vdata in (snapshot.get("vendors") or {}).items():
        for p in (vdata.get("products") or []):
            u = (p.get("updated_at") or "")[:10]
            if prior_start <= u <= prior_end:
                moved_pids.add(int(p.get("id") or 0))
    activity_pids = sale_pids | count_pids | moved_pids  # was at event (sold/counted/touched)
    for code, vdata in (snapshot.get("vendors") or {}).items():
        supplier = vdata.get("display_name") or vdata.get("name") or code
        for p in (vdata.get("products") or []):
            if is_permanent_exclude(p):
                n_excluded += 1
                continue
            if not p.get("active", True):
                continue  # Lauren 2026-06-08 — ACTIVE products only on the count list
            qty = float(p.get("in_stock_qty") or 0)
            # Lauren 2026-05-21 PM #8 — read from 'categories' (OCTOPOS's tag mechanism),
            # NOT 'tags'. octopos_sync.py stores them under 'categories' since 2026-05-13.
            # Previously was reading 'tags' which always returned empty → preexisting was
            # ALWAYS 0 even though 71 products were tagged Recount in OCTOPOS. Bug since
            # the script was first written.
            tags_raw = [(c.get("name") or "").strip().lower() for c in (p.get("categories") or [])]
            has_recount = "recount" in tags_raw
            pid = int(p.get("id") or 0)
            reason = None
            # Lauren 2026-05-21 PM: skip negative-stock items that were physically
            # counted in the prior event window (within ~7 days). Trust the recent
            # count — the negative reflects post-count sales tracking, not a
            # counting error. Without this filter, the SAME 5 negatives appear on
            # every weekly recount even though Lauren just verified them.
            created_at = (p.get("created_at") or "")[:10]
            is_new_since_prior = bool(created_at and created_at > prior_end)
            was_at_event = pid in activity_pids
            had_sale = pid in sale_pids
            was_physically_counted = pid in count_pids
            if qty < 0 and pid not in count_pids:
                # Lauren 2026-05-21 PM #7 (final): negatives only if NOT counted at the
                # prior event. If it was counted and is still negative, trust the count
                # for now — the residual negative is post-count sales tracking, not a
                # count error worth re-verifying.
                reason = "negative"
                n_neg += 1
            elif qty < 0 and pid in count_pids:
                # Negative but counted — skip
                n_neg_skipped_counted += 1
                continue
            elif has_recount and pid in count_pids:
                # Tagged Recount BUT was counted at the prior event — the tag is stale
                # (recount_weekend.py's auto-cleanup endpoint isn't verified yet so the
                # tag remains until Lauren removes it manually). Treat the count as proof
                # of verification and skip. Lauren 2026-05-21 PM #8.
                n_preexisting_stale += 1
                continue
            elif has_recount:
                # Lauren manually tagged with RECOUNT in OCTOPOS, no recent count → on list.
                reason = "preexisting"
                n_preexisting += 1
            # Lauren 2026-05-21 PM #7 (final): new_unverified category REMOVED.
            # Lauren's reasoning: a product that was added before the upcoming event but
            # didn't have sales at the prior event is just-new-and-not-selling-yet, not
            # a count problem. If it was created mid-cycle, she counted it manually when
            # adding it to OCTOPOS.

            elif had_sale:
                # Sold ≥1 unit at the last event → qty is trustworthy, NOT stale.
                n_sold += 1
                continue
            elif stale_enabled and qty >= 5:
                # Lauren 2026-06-08 — SINGLE stale rule: 5+ units in stock AND zero units
                # sold at the last CONFIRMED Fri-Sun event (threshold 5+). Count status, prior RECOUNT
                # tag and updated_at are intentionally NOT conditions anymore.
                reason = "sat_unsold"
                n_sat_unsold += 1
            else:
                # qty ≤ 2, or no confirmed event last weekend → not a stale signal. Skip.
                n_not_at_event += 1
                continue
            if reason:
                worklist.append({
                    "id": pid,
                    "sku": (p.get("sku") or "").strip(),
                    "barcode": (p.get("barcode") or "").strip(),
                    "name": (p.get("name") or "").strip(),
                    "supplier": supplier,
                    "department": (p.get("department") or "").strip(),
                    "qty": qty,
                    "threshold": float(p.get("threshold") or 0),
                    "sold_in_window": real_sold_qty.get(pid, 0),
                    "tags": [t for t in tags_raw if t],
                    "reason": reason,
                    "updated_at": p.get("updated_at") or "",
                })
    stats = {
        "negative": n_neg,
        "sat_unsold": n_sat_unsold,
        "preexisting": n_preexisting,
        "excluded_permanent": n_excluded,
        "excluded_not_at_event": n_not_at_event,
        "excluded_already_counted": n_already_counted,
        "excluded_sold": n_sold,
        "excluded_neg_already_counted": n_neg_skipped_counted,
        "excluded_preexisting_stale_tag": n_preexisting_stale,
        "ever_counted_total": len(ever_counted_pids),
        "sale_last_event": len(sale_pids),
        "count_last_event": len(count_pids),
        "moved_last_event": len(moved_pids),
        "activity_last_event": len(activity_pids),
        "final_worklist_size": len(worklist),
        "removed_recount_tag": None,  # tag-mutation handled by recount_weekend.py (Sunday)
    }
    return worklist, stats


RECOUNT_CATEGORY_ID = 14  # OCTOPOS category id for "Recount" — verified 2026-05-22


def sync_recount_tags(worklist, v2_token):
    """Make OCTOPOS's "Recount" tag exactly match the current worklist.

    Lauren's directive 2026-05-22 PM: "שיש לכל המוצרים האלו TAG RECOUNT — אלא
    המוצרים היחידים שצריך שיהיה להם אלא אם כן אני אוסיף ידנית תג במערכת של אוקטופוס".
    Interpretation: the agent owns the Recount tag set on OCTOPOS — it should
    match the worklist exactly. If Lauren manually tags a product, the next
    prebuild will see that product as has_recount → include it on the worklist
    → sync keeps the tag. The closed loop just works.

    Returns dict with counts: {added, removed, add_failed, remove_failed}.
    """
    if not v2_token:
        print("OCTOPOS_TOKEN not set — skipping Recount tag sync")
        return {"added": 0, "removed": 0, "add_failed": 0, "remove_failed": 0, "skipped": True}

    worklist_pids = {int(it["id"]) for it in worklist}
    print(f"Recount-tag sync: worklist has {len(worklist_pids)} products")

    # Discover what's currently tagged Recount in OCTOPOS by reading the most
    # recent local snapshot (octopos_sync.py runs daily). This avoids a
    # full-catalog scan — the snapshot already has every product's categories.
    snap_path = REPO_ROOT / "docs/state/octopos_products.json"
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    currently_tagged = set()
    for vdata in (snapshot.get("vendors") or {}).values():
        for p in (vdata.get("products") or []):
            cats = [(c.get("name") or "").strip().lower() for c in (p.get("categories") or [])]
            if "recount" in cats:
                try: currently_tagged.add(int(p.get("id") or 0))
                except (TypeError, ValueError): pass
    currently_tagged.discard(0)
    print(f"Recount-tag sync: OCTOPOS snapshot has {len(currently_tagged)} products currently tagged Recount")

    to_add = worklist_pids - currently_tagged
    to_remove = currently_tagged - worklist_pids
    print(f"Recount-tag sync: +{len(to_add)} to add, -{len(to_remove)} to remove")

    def _get_product(pid):
        req = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
            headers={"Authorization": v2_token, "Accept": "application/json", "User-Agent": OCTO_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _put_categories(pid, new_cat_ids):
        req = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
            data=json.dumps({"category_ids": new_cat_ids}).encode(),
            headers={"Authorization": v2_token, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": OCTO_UA}, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    added = removed = add_failed = remove_failed = 0
    for pid in sorted(to_add):
        try:
            p = _get_product(pid)
            cat_ids = [c["id"] for c in (p.get("categories") or [])]
            if RECOUNT_CATEGORY_ID in cat_ids:
                # Snapshot was stale — product already tagged. Nothing to do.
                continue
            status = _put_categories(pid, cat_ids + [RECOUNT_CATEGORY_ID])
            if status == 200:
                added += 1
            else:
                add_failed += 1
                print(f"  add pid={pid} returned HTTP {status}")
        except Exception as e:
            add_failed += 1
            print(f"  add pid={pid} ERROR: {e}")

    for pid in sorted(to_remove):
        try:
            p = _get_product(pid)
            cat_ids = [c["id"] for c in (p.get("categories") or [])]
            if RECOUNT_CATEGORY_ID not in cat_ids:
                continue
            new_ids = [cid for cid in cat_ids if cid != RECOUNT_CATEGORY_ID]
            status = _put_categories(pid, new_ids)
            if status == 200:
                removed += 1
            else:
                remove_failed += 1
                print(f"  remove pid={pid} returned HTTP {status}")
        except Exception as e:
            remove_failed += 1
            print(f"  remove pid={pid} ERROR: {e}")

    print(f"Recount-tag sync done: added={added} (failed {add_failed}), removed={removed} (failed {remove_failed})")
    return {"added": added, "removed": removed, "add_failed": add_failed, "remove_failed": remove_failed,
            "skipped": False}


def sms_lauren(body):
    token = os.environ.get("SIMPLETEXTING_TOKEN", "")
    phone = os.environ.get("LAUREN_PHONE", "4243547625")
    if not token:
        print("(no SimpleTexting token — skipping SMS)")
        return
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from lauren_sms import send_sms
        send_sms(phone, body)
    except Exception as e:
        print(f"SMS send failed: {e}")


def main():
    ev = find_upcoming_event()
    if not ev:
        print("No upcoming event within 7 days. Exiting silent.")
        return 0

    upcoming_evkey = f"{(ev.get('city') or '').lower().replace(' ', '-')}-{ev.get('start_date')}"
    upcoming_start = ev["start_date"]
    upcoming_end   = ev["end_date"]
    city  = ev.get("city") or ""
    state = ev.get("state") or ""
    print(f"Upcoming event: {city}, {state} {upcoming_start} → {upcoming_end} (evkey={upcoming_evkey})")

    # Find prior event for the counted_pids data window
    prior = find_previous_event(dt.date.fromisoformat(upcoming_start))
    if prior:
        prior_start = prior["start_date"]
        prior_end   = prior["end_date"]
        print(f"Prior event for data window: {prior.get('city')}, {prior.get('state')} {prior_start} → {prior_end}")
    else:
        prior_start = (dt.date.fromisoformat(upcoming_start) - dt.timedelta(days=7)).isoformat()
        prior_end   = (dt.date.fromisoformat(upcoming_start) - dt.timedelta(days=4)).isoformat()
        print(f"No prior event — using default 7-day-back window: {prior_start} → {prior_end}")

    # Load OCTOPOS snapshot
    snap_path = REPO_ROOT / "docs/state/octopos_products.json"
    if not snap_path.exists():
        raise SystemExit(f"snapshot missing: {snap_path}")
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    snap_updated = snapshot.get("_updated_at", "?")
    print(f"OCTOPOS snapshot: _updated_at={snap_updated}")

    # Authenticate + fetch counted_pids
    jwt = octopos_jwt()
    activity = fetch_activity_pids(jwt, prior_start, prior_end)
    ever_counted_pids = fetch_ever_counted_pids(jwt, lookback_days=90)
    real_sold_pids, real_sold_qty = fetch_real_sales_pids(jwt, prior_start, prior_end)
    print(f"Activity in prior window ({prior_start} → {prior_end}): sale={len(activity['sale_pids'])} count={len(activity['count_pids'])}")

    # Lauren 2026-06-08 — STALE only fires when there was a real event LAST WEEKEND.
    # If the most recent event ended >10 days ago, zero-sales is meaningless → skip stale.
    today_pt = dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date()
    if prior:
        last_event_age = (today_pt - dt.date.fromisoformat(prior_end)).days
        stale_enabled = 0 <= last_event_age <= 10
    else:
        last_event_age = None
        stale_enabled = False
    print(f"STALE {'ENABLED' if stale_enabled else 'DISABLED'} — last event end={prior_end if prior else None}, age={last_event_age}d")

    # Build worklist
    worklist, stats = build_worklist(snapshot, activity, prior_start, prior_end, ever_counted_pids, real_sold_pids, real_sold_qty, stale_enabled=stale_enabled)
    stats["stale_enabled"] = stale_enabled
    print(f"Worklist: {stats}")

    # Write to state
    state_path = REPO_ROOT / "docs/state/octopos_recount.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("events", {})[upcoming_evkey] = {
        "worklist": worklist,
        "stats": stats,
        "window": {"start": upcoming_start, "end": upcoming_end},
        "prior_window": {"start": prior_start, "end": prior_end},
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "pre_event",
        "source": "recount_prebuild.py",
    }
    state["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote pre-event worklist for {upcoming_evkey}: {len(worklist)} items")

    # Lauren 2026-05-22 PM — sync OCTOPOS Recount tag to match the worklist exactly.
    # Add tag to worklist products that don't have it; remove from products that have
    # the tag but aren't on the worklist. Gated by OCTOPOS_TOKEN env (v2 raw token).
    v2_token = os.environ.get("OCTOPOS_TOKEN") or ""
    tag_sync_stats = sync_recount_tags(worklist, v2_token)
    state["events"][upcoming_evkey]["tag_sync"] = tag_sync_stats
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    # SMS Lauren
    stale_note = "" if stale_enabled else "⚠️ לא היה אירוע בסוף\"ש האחרון — דילגתי על זיהוי STALE.\n"
    sms_body = (
        f"@recount ✓ רשימת ספירה מוכנה ל-{city}, {state} ({upcoming_start} → {upcoming_end}).\n"
        f"📋 {len(worklist)} מוצרים לספירה:\n"
        f"🔴 {stats['negative']} מינוס · 🔵 {stats['preexisting']} מתויגי RECOUNT · "
        f"💤 {stats['sat_unsold']} עם 3+ במלאי ו-0 מכירות באירוע האחרון.\n"
        f"{stale_note}"
        f"חלון נתונים מהאירוע הקודם: {prior_start} → {prior_end}\n"
        f"https://dashboard.themakeupblowout.com/recount/?evkey={upcoming_evkey}"
    )
    sms_lauren(sms_body)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
