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

# 2026-05-20: Cloudflare started blocking the default Python urllib UA
# (Error 1010 browser_signature_banned). Send a browser-like UA on every OCTOPOS call.
OCTO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

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
WINDOW_MIN_BEFORE = 15  # tolerate up to 15 min early — GitHub Actions cron can jitter (2026-05-26 fix: Roseville 22:09 UTC fire missed the 22:15 target by 6 min and Auto-cleanup never ran)
WINDOW_MIN_AFTER  = 35  # tolerate up to 17:50 (so an hourly cron at :15 catches it)


# ─── Helpers ────────────────────────────────────────────────────────────────

def http_post(url, body, headers, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={**headers, "Content-Type":"application/json", "Accept":"application/json", "User-Agent": OCTO_UA},
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
    """Extract the SCHEDULE map from docs/launch/index.html. Returns a FLAT list
    of event dicts across all years.

    🛑 2026-06-22 fix — on 2026-04-28 (commit 4149bb3b "year tabs") the SCHEDULE
    changed from a flat array `const SCHEDULE = [...]` to a year-keyed object
    `const SCHEDULE = {"2026":[...],"2027":[...]}`. The old array-only regex
    stopped matching → this returned [] → the Sunday weekend run found NO event
    and silently skipped tag-removal for ~2 months. We now accept BOTH shapes and
    flatten the object's year arrays. JSON has no ';', so the first `};`/`];`
    after the literal is always the real terminator.
    """
    html = (REPO_ROOT / "docs/launch/index.html").read_text(encoding="utf-8")
    # Try year-keyed object first, then legacy flat array.
    raw = None
    m = re.search(r"const SCHEDULE\s*=\s*(\{[\s\S]*?\});\s*\n", html)
    if m:
        raw = m.group(1)
    else:
        m = re.search(r"const SCHEDULE\s*=\s*(\[[\s\S]*?\]);\s*\n", html)
        raw = m.group(1) if m else None
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # year-keyed: {"2026":[...], "2027":[...]} → flatten every list value.
        out = []
        for v in data.values():
            if isinstance(v, list):
                out.extend(v)
        return out
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
    """Pull recount/adjustment events from OCTOPOS that fall in [start, end].

    🛑 Lauren 2026-05-22 PM #4 — TWO API quirks to defend against:
      (1) /get-recount-data IGNORES start_date/end_date and returns the full YTD
          set (~1262 rows, 586 unique pids in 2026). Before this fix the script
          treated all of them as "counted at this event" — would have stripped
          the Recount tag from products counted in January from any Sunday-after-
          event cron fire. Fix: filter client-side by parsing created_at
          (MM/DD/YYYY format) into the start/end window.
      (2) The API DOES paginate (totalItems can exceed limit). Walk pages until
          we have everything before client-side filtering.

    Both rows of type DR and CR count as "physically counted" — see CLAUDE.md
    IRON RULE #9 trap B (DR = count revealed shrinkage; CR = count revealed
    overage; either way a human did the count).
    """
    all_rows = []
    page = 1
    while page < 20:
        code, resp = http_post(
            f"{OCTO_BASE}/api/v1/get-recount-data",
            {"location_id": location_id, "start_date": start, "end_date": end,
             "limit": 5000, "page": page, "order": "id", "order_type": "desc", "filter": ""},
            {"Authorization": f"Bearer {jwt}", "Permission": "report-inventary-recount"})
        if code != 200 or not resp.get("flag"):
            if page == 1:
                raise SystemExit(f"get-recount-data failed: HTTP {code} {resp}")
            break
        items = resp.get("data", {}).get("data", []) or []
        if not items:
            break
        all_rows.extend(items)
        total = (resp.get("data") or {}).get("totalItems") or len(all_rows)
        if len(all_rows) >= total:
            break
        page += 1

    # Client-side date filter — API ignores start_date/end_date.
    def _in_window(row):
        try:
            ca = str(row.get("created_at") or "").split()[0]
            d = dt.datetime.strptime(ca, "%m/%d/%Y").date()
            s = dt.date.fromisoformat(start)
            e = dt.date.fromisoformat(end)
            return s <= d <= e
        except Exception:
            return False

    filtered = [r for r in all_rows if _in_window(r)]
    print(f"fetch_recount_data: {len(all_rows)} total rows returned by API, {len(filtered)} fall in window {start}..{end}")
    return filtered


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
    # 🛑 2026-06-22 — fail LOUD if the SCHEDULE parser returns nothing. The schedule
    # ALWAYS has events; 0 events can only mean the parser broke (as it silently did
    # from 2026-04-28 to 2026-06-22 when SCHEDULE became year-keyed). Don't let that
    # rot in silence again. Dedupe to one SMS/day via a marker in the state file.
    all_events = parse_schedule_from_launch_html()
    if not all_events:
        now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        today_pt = dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
        print(f"[{now_utc}] 🛑 SCHEDULE parser returned 0 events — parser likely broken.")
        try:
            sp = REPO_ROOT / "docs/state/octopos_recount.json"
            st = json.loads(sp.read_text(encoding="utf-8"))
            if st.get("_parser_broken_alert_date") != today_pt:
                sms_lauren("🛑 @recount: ה-parser של לוח האירועים לא מצא אף אירוע — כנראה נשבר. "
                           "הסרת תגיות RECOUNT לא תרוץ עד שיתוקן.")
                st["_parser_broken_alert_date"] = today_pt
                sp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"  (parser-broken alert failed: {e})")
        return 0

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

    # Auto-cleanup: REMOVE the "Recount" tag (category_id=14) from every counted product
    # that still has it. Approved by Lauren 2026-05-17 PM late.
    # Uses PUT /api/v2/products/{id} with raw v2 token (Lauren architecture: this is the
    # ONE place we DO mutate OCTOPOS — clearing tags after positive count evidence).
    v2_token = os.environ.get("OCTOPOS_TOKEN") or ""
    cleaned_count = 0
    failed_count = 0
    if v2_token and counted_pids:
        for pid in counted_pids:
            try:
                # Read current categories
                rr = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
                    headers={"Authorization": v2_token, "Accept": "application/json", "User-Agent": OCTO_UA})
                with urllib.request.urlopen(rr, timeout=8) as r:
                    prod = json.loads(r.read())
                cats = prod.get("categories") or []
                has_recount = any((c.get("name") or "").strip().lower() == "recount" for c in cats)
                if not has_recount:
                    continue  # nothing to clean
                # PUT with category_ids minus Recount
                new_cat_ids = [c["id"] for c in cats if (c.get("name") or "").strip().lower() != "recount"]
                pr = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
                    data=json.dumps({"category_ids": new_cat_ids}).encode(),
                    headers={"Authorization": v2_token, "Content-Type": "application/json",
                             "Accept": "application/json", "User-Agent": OCTO_UA}, method="PUT")
                with urllib.request.urlopen(pr, timeout=8) as r:
                    if r.status == 200:
                        cleaned_count += 1
                    else:
                        failed_count += 1
            except Exception as e:
                failed_count += 1
                print(f"  cleanup err id={pid}: {e}")
        print(f"Auto-cleanup: removed RECOUNT tag from {cleaned_count} products (failed: {failed_count})")
    elif not v2_token:
        print("OCTOPOS_TOKEN env var not set — skipping auto-cleanup (read-only run)")

    # Bump @recount state — minimal slice; full worklist recompute lives in the Cowork
    # session. The cron job: (a) prove freshness, (b) save the audit trail, (c) clear tags.
    state_path = REPO_ROOT / "docs/state/octopos_recount.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("events", {}).setdefault(evkey, {})
    state["events"][evkey]["counted_pids_real"] = counted_pids
    state["events"][evkey]["recount_events_count"] = len(events)
    state["events"][evkey]["auto_cleanup_removed_recount_tag"] = cleaned_count
    state["events"][evkey]["auto_cleanup_failed"] = failed_count
    state["events"][evkey]["last_cron_fire_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state["events"][evkey]["last_cron_fire_local"] = local.strftime("%Y-%m-%d %H:%M %Z")
    state["events"][evkey]["window"] = {"start": start, "end": end}
    state["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote state slice for {evkey}")

    # 🛑 2026-06-22 — build the THREE lists (to_count / counted / tagged_not_counted)
    # + the per-event didn't-sell set, and freeze them into the slice so the dashboard
    # has real data and the history survives later tag removal. Read-only on OCTOPOS.
    try:
        import recount_lists as RL
        payload = RL.build_lists(start, end, jwt=jwt)
        RL.write_into_state(evkey, payload)
        print(f"Wrote lists slice for {evkey}: {payload['counts']}")
    except Exception as e:
        print(f"  (lists build failed — non-fatal: {e})")

    body = (
        f"✓ ספירת אירוע הושלמה — {city} ({len(counted_pids)} נספרו, {cleaned_count} תגיות הוסרו).\n"
        f"https://dashboard.themakeupblowout.com/recount/?evkey={evkey}"
    )
    sms_lauren(body)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
