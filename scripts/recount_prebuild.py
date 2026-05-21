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
      'sale_pids'  = pids that had a DR row (decrease, i.e. likely a POS sale)
      'count_pids' = pids that had a CR row (credit, i.e. a physical recount adjustment)
    Lauren 2026-05-21 PM #4 — to skip 'wasn't at last event' products we need to
    distinguish 'was at event but not counted' (suspicious — list it) from
    'wasn't at event at all' (skip — that's why it didn't move).
    """
    code, resp = http_post(
        f"{OCTO_BASE}/api/v1/get-recount-data",
        {"location_id": 2, "start_date": start_date, "end_date": end_date,
         "limit": 5000, "page": 1, "order": "id", "order_type": "desc", "filter": ""},
        {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
    if code != 200 or not resp.get("flag"):
        print(f"WARN: get-recount-data failed (HTTP {code}) — proceeding with empty activity sets", file=sys.stderr)
        return {"sale_pids": set(), "count_pids": set()}
    sale_pids, count_pids = set(), set()
    for row in resp.get("data", {}).get("data", []):
        pid = int(row["product_id"])
        if row.get("type") == "DR":
            sale_pids.add(pid)
        elif row.get("type") == "CR":
            count_pids.add(pid)
    return {"sale_pids": sale_pids, "count_pids": count_pids}


def build_worklist(snapshot, activity, prior_start, prior_end, ever_counted_pids):
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
    sale_pids   = activity.get("sale_pids")   or set()
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

            elif qty > 0 and was_physically_counted:
                # Already counted at the prior event → trust the count, skip.
                n_already_counted += 1
                continue
            elif qty > 0 and had_sale:
                # Had a sale (DR) → trust the qty, skip.
                n_sold += 1
                continue
            elif qty > 0 and was_at_event:
                # Was at the event (proof via updated_at) but no DR and no CR →
                # sat unsold and uncounted. THIS is the suspicious bucket.
                reason = "sat_unsold"
                n_sat_unsold += 1
            else:
                # qty > 0 + no activity at all → wasn't at the prior event. Skip
                # (Lauren 2026-05-21 PM #4 — that's WHY it didn't move).
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
                    "sold_in_window": None,  # sales endpoint unverified — left null
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
    print(f"Activity in prior window ({prior_start} → {prior_end}): sale={len(activity['sale_pids'])} count={len(activity['count_pids'])}")

    # Build worklist
    worklist, stats = build_worklist(snapshot, activity, prior_start, prior_end, ever_counted_pids)
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

    # SMS Lauren
    sms_body = (
        f"@recount ✓ רשימת ספירה מוכנה ל-{city}, {state} ({upcoming_start} → {upcoming_end}).\n"
        f"📋 {len(worklist)} מוצרים לספירה:\n"
        f"🔴 {stats['negative']} מינוס (לא נספרו) · 🔵 {stats['preexisting']} מתויגי RECOUNT · "
        f"💤 {stats['sat_unsold']} היו ולא נמכרו.\n"
        f"חלון נתונים מהאירוע הקודם: {prior_start} → {prior_end}\n"
        f"https://laurenlev10.github.io/lauren-agent-hub-data/recount/?evkey={upcoming_evkey}"
    )
    sms_lauren(sms_body)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
