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


def fetch_counted_pids(jwt, start_date, end_date):
    """Pull counted_pids from OCTOPOS for the given window."""
    code, resp = http_post(
        f"{OCTO_BASE}/api/v1/get-recount-data",
        {"location_id": 2, "start_date": start_date, "end_date": end_date,
         "limit": 5000, "page": 1, "order": "id", "order_type": "desc", "filter": ""},
        {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
    if code != 200 or not resp.get("flag"):
        print(f"WARN: get-recount-data failed (HTTP {code}) — proceeding with empty counted_pids", file=sys.stderr)
        return set()
    return {int(row["product_id"]) for row in resp.get("data", {}).get("data", [])}


def build_worklist(snapshot, counted_pids):
    """Iterate the OCTOPOS snapshot. Return enriched worklist entries."""
    worklist = []
    n_neg = n_stale = n_preexisting = 0
    n_excluded = 0
    for code, vdata in (snapshot.get("vendors") or {}).items():
        supplier = vdata.get("display_name") or vdata.get("name") or code
        for p in (vdata.get("products") or []):
            if is_permanent_exclude(p):
                n_excluded += 1
                continue
            qty = float(p.get("in_stock_qty") or 0)
            tags_raw = [(t.get("name") or "").strip().lower() for t in (p.get("tags") or [])]
            has_recount = "recount" in tags_raw
            pid = int(p.get("id") or 0)
            reason = None
            if qty < 0:
                reason = "negative"
                n_neg += 1
            elif has_recount and pid not in counted_pids:
                reason = "preexisting"
                n_preexisting += 1
            elif qty > 0 and pid not in counted_pids and not has_recount:
                # Only flag as 'stale' if the product was created BEFORE the last event window
                # (otherwise we'd flag everything that was just stocked).
                # Use a heuristic: if 'created_at' is missing or older than the last counted
                # window, include it. Most products in the snapshot lack created_at → fall
                # back to including by default (the user can dismiss in dashboard).
                reason = "stale"
                n_stale += 1
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
        "stale": n_stale,
        "preexisting": n_preexisting,
        "excluded_permanent": n_excluded,
        "counted_last_event": len(counted_pids),
        "final_worklist_size": len(worklist),
        "removed_recount_tag": None,  # tag-mutation not wired pre-event (cleanup runs Sunday)
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
    counted_pids = fetch_counted_pids(jwt, prior_start, prior_end)
    print(f"Counted PIDs in prior window ({prior_start} → {prior_end}): {len(counted_pids)}")

    # Build worklist
    worklist, stats = build_worklist(snapshot, counted_pids)
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
        f"📋 {len(worklist)} מוצרים לספירה: 🔴 {stats['negative']} מינוס · 💤 {stats['stale']} לא זזו · 🔵 {stats['preexisting']} קיים מקודם.\n"
        f"חלון נתונים מהאירוע הקודם: {prior_start} → {prior_end}\n"
        f"https://laurenlev10.github.io/lauren-agent-hub-data/recount/?evkey={upcoming_evkey}"
    )
    sms_lauren(sms_body)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
