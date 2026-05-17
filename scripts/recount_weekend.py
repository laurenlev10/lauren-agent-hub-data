#!/usr/bin/env python3
"""
@recount weekly runner — fires Sunday 17:15 LOCAL time at the event's location.

Strategy:
  - The cron fires multiple times Sunday (21:15/22:15/23:15 UTC + Monday 00:15 UTC)
    to cover Eastern → Pacific event timezones.
  - Each fire, this script:
      1. Loads the current week's event from docs/launch/notes.json SCHEDULE.
      2. Looks up city → state → IANA timezone.
      3. Checks if `now` falls in the 17:00–17:30 local window for that event.
      4. If yes, fetches OCTOPOS recount report for the Fri→Sun window,
         builds the worklist, writes state, SMSes Lauren.
      5. If no, exits silently (the next cron tick will get it).

Phase 0 (this commit): only the bootstrap is wired — the actual OCTOPOS pull
runs against the verified /api/v1/get-recount-data endpoint shipped earlier today.
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

# US state → IANA timezone (single TZ per state — good enough for Lauren's events
# which are always in major cities; if Lauren ever runs an event in a multi-TZ
# state border city, override via STAFF_DEFAULTS[evkey].tz_override).
STATE_TZ = {
    "AL":"America/Chicago","AK":"America/Anchorage","AZ":"America/Phoenix",
    "AR":"America/Chicago","CA":"America/Los_Angeles","CO":"America/Denver",
    "CT":"America/New_York","DE":"America/New_York","FL":"America/New_York",
    "GA":"America/New_York","HI":"Pacific/Honolulu","ID":"America/Boise",
    "IL":"America/Chicago","IN":"America/Indiana/Indianapolis","IA":"America/Chicago",
    "KS":"America/Chicago","KY":"America/New_York","LA":"America/Chicago",
    "ME":"America/New_York","MD":"America/New_York","MA":"America/New_York",
    "MI":"America/Detroit","MN":"America/Chicago","MS":"America/Chicago",
    "MO":"America/Chicago","MT":"America/Denver","NE":"America/Chicago",
    "NV":"America/Los_Angeles","NH":"America/New_York","NJ":"America/New_York",
    "NM":"America/Denver","NY":"America/New_York","NC":"America/New_York",
    "ND":"America/Chicago","OH":"America/New_York","OK":"America/Chicago",
    "OR":"America/Los_Angeles","PA":"America/New_York","RI":"America/New_York",
    "SC":"America/New_York","SD":"America/Chicago","TN":"America/Chicago",
    "TX":"America/Chicago","UT":"America/Denver","VT":"America/New_York",
    "VA":"America/New_York","WA":"America/Los_Angeles","WV":"America/New_York",
    "WI":"America/Chicago","WY":"America/Denver","DC":"America/New_York",
}

TARGET_LOCAL_HOUR = 17
TARGET_LOCAL_MIN  = 15
WINDOW_MIN_BEFORE = 0   # only fire AT or AFTER 17:15
WINDOW_MIN_AFTER  = 35  # tolerate up to 17:50 (so an hourly cron at :15 catches it)


# ─── Helpers ────────────────────────────────────────────────────────────────

def http_post(url, body, headers, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={**headers, "Content-Type":"application/json", "Accept":"application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def octopos_jwt():
    email = os.environ["OCTOPOS_EMAIL"]
    pw    = os.environ["OCTOPOS_PASSWORD"]
    code, resp = http_post(f"{OCTO_BASE}/api/v1/authenticate", {"email":email,"password":pw}, {})
    if code != 200 or not resp.get("flag"):
        raise SystemExit(f"OCTOPOS login failed: HTTP {code} {resp}")
    return resp["data"]["token"]


def parse_schedule_from_launch_html():
    """Extract the SCHEDULE map from docs/launch/index.html. Returns list of event dicts."""
    html = (REPO_ROOT / "docs/launch/index.html").read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\[[\s\S]*?\]);\s*\n", html)
    if not m:
        return []
    raw = m.group(1)
    # The SCHEDULE in launch HTML is JSON-compatible (Lauren wrote it that way intentionally).
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fall back: relaxed parse (allow unquoted keys / trailing commas)
        return []


def find_event_for_today():
    """
    Returns the event dict whose end_date == today's local date (US-aware) AND
    whose local 17:15 is within the firing window. Returns None if no match.
    """
    now_utc = dt.datetime.now(dt.timezone.utc)
    today_date = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).date()

    events = parse_schedule_from_launch_html()
    candidates = []
    for ev in events:
        end_str = (ev.get("end_date") or "").strip()
        try:
            end_date = dt.date.fromisoformat(end_str)
        except ValueError:
            continue
        # Accept events ending today OR yesterday (in case timezone math straddles midnight)
        if abs((end_date - today_date).days) > 1:
            continue
        state = (ev.get("state") or "").strip().upper()
        tz_name = ev.get("tz_override") or STATE_TZ.get(state)
        if not tz_name:
            continue
        local = now_utc.astimezone(ZoneInfo(tz_name))
        # Must be on Sunday local AND in the 17:15-17:50 window
        if local.weekday() != 6:  # 6 = Sunday
            continue
        local_min_of_day = local.hour * 60 + local.minute
        target = TARGET_LOCAL_HOUR * 60 + TARGET_LOCAL_MIN
        if not (target - WINDOW_MIN_BEFORE <= local_min_of_day <= target + WINDOW_MIN_AFTER):
            continue
        candidates.append((ev, tz_name, local))
    if not candidates:
        return None
    # If multiple match somehow, prefer event whose end_date == today exactly
    candidates.sort(key=lambda x: abs((dt.date.fromisoformat(x[0]["end_date"]) - today_date).days))
    return candidates[0]


def fetch_recount_data(jwt, start, end, location_id=2):
    code, resp = http_post(
        f"{OCTO_BASE}/api/v1/get-recount-data",
        {"location_id": location_id, "start_date": start, "end_date": end,
         "limit": 5000, "page": 1, "order": "id", "order_type": "desc", "filter": ""},
        {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
    if code != 200 or not resp.get("flag"):
        raise SystemExit(f"get-recount-data failed: HTTP {code} {resp}")
    return resp.get("data", {}).get("data", [])


def sms_lauren(body):
    """Best-effort SMS via SimpleTexting v2 (the canonical lauren_sms.py is a thin wrapper)."""
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


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    match = find_event_for_today()
    if not match:
        now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{now_utc}] No event matches 17:15 local fire window. Exiting silent.")
        return 0

    ev, tz_name, local = match
    evkey = f"{(ev.get('city') or '').lower().replace(' ','-')}-{ev.get('start_date')}"
    city  = ev.get("city") or ""
    state = ev.get("state") or ""
    start = ev.get("start_date")
    end   = ev.get("end_date")

    print(f"FIRING for evkey={evkey} (local {local.strftime('%Y-%m-%d %H:%M %Z')})")

    jwt = octopos_jwt()
    events = fetch_recount_data(jwt, start, end)
    counted_pids = sorted(set(int(r["product_id"]) for r in events))
    print(f"OCTOPOS returned {len(events)} recount events → {len(counted_pids)} unique product ids")

    # Bump @recount state — minimal slice; full worklist recompute lives in the Cowork
    # session. The cron's job is to (a) prove freshness, (b) save the audit trail.
    state_path = REPO_ROOT / "docs/state/octopos_recount.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("events", {}).setdefault(evkey, {})
    state["events"][evkey]["counted_pids_real"] = counted_pids
    state["events"][evkey]["recount_events_count"] = len(events)
    state["events"][evkey]["last_cron_fire_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state["events"][evkey]["last_cron_fire_local"] = local.strftime("%Y-%m-%d %H:%M %Z")
    state["events"][evkey]["window"] = {"start": start, "end": end}
    state["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote state slice for {evkey}")

    body = (
        f"@recount ✓ סיימתי סריקה אוטומטית של {city}, {state} ({local.strftime('%H:%M %Z')}).\n"
        f"📊 {len(events)} תנועות ספירה · {len(counted_pids)} מוצרים יחודיים נספרו.\n\n"
        f"https://laurenlev10.github.io/lauren-agent-hub-data/recount/?evkey={evkey}"
    )
    sms_lauren(body)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
