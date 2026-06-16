#!/usr/bin/env python3
"""
update_subscribe_target.py

Picks the current OR next-upcoming event from launch/index.html's SCHEDULE map,
joins it with SETUPS (SimpleTexting list_id), event_form_ids.json (webForm ID),
and launch/notes.json (per-event Reel URL), and writes
docs/state/subscribe_target.json - consumed cross-origin by the
public landing page at https://www.themakeupblowoutsale-group.com/subscribe-list/

Source of truth lives in launch/index.html (SCHEDULE/SETUPS) and
docs/state/event_form_ids.json. We never duplicate event data - we just
publish a thin pointer to "the relevant one right now".
"""

import datetime, json, re, sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(".")
LAUNCH    = ROOT / "docs/launch/index.html"
NOTES     = ROOT / "docs/launch/notes.json"
FORM_IDS  = ROOT / "docs/state/event_form_ids.json"
TARGET    = ROOT / "docs/state/subscribe_target.json"
UPCOMING  = ROOT / "docs/state/upcoming_events.json"

DEFAULT_FB = "https://www.facebook.com/themakeupblowoutsale/"
DEFAULT_TT = "https://www.tiktok.com/@makeupblowoutsale"
DEFAULT_IG = "https://www.instagram.com/themakeupblowoutsale/"


def slug_of(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def parse_map(html: str, name: str) -> dict:
    m = re.search(rf"const {name} = (\{{[^;]+\}});", html, re.S)
    return json.loads(m.group(1)) if m else {}


# US state -> IANA timezone (mirrors STATE_TZ in scripts/insta_reel_scan.py).
STATE_TZ = {
    "AL":"America/Chicago","AK":"America/Anchorage","AZ":"America/Phoenix","AR":"America/Chicago",
    "CA":"America/Los_Angeles","CO":"America/Denver","CT":"America/New_York","DE":"America/New_York",
    "FL":"America/New_York","GA":"America/New_York","HI":"Pacific/Honolulu","ID":"America/Boise",
    "IL":"America/Chicago","IN":"America/Indiana/Indianapolis","IA":"America/Chicago","KS":"America/Chicago",
    "KY":"America/New_York","LA":"America/Chicago","ME":"America/New_York","MD":"America/New_York",
    "MA":"America/New_York","MI":"America/Detroit","MN":"America/Chicago","MS":"America/Chicago",
    "MO":"America/Chicago","MT":"America/Denver","NE":"America/Chicago","NV":"America/Los_Angeles",
    "NH":"America/New_York","NJ":"America/New_York","NM":"America/Denver","NY":"America/New_York",
    "NC":"America/New_York","ND":"America/Chicago","OH":"America/New_York","OK":"America/Chicago",
    "OR":"America/Los_Angeles","PA":"America/New_York","RI":"America/New_York","SC":"America/New_York",
    "SD":"America/Chicago","TN":"America/Chicago","TX":"America/Chicago","UT":"America/Denver",
    "VT":"America/New_York","VA":"America/New_York","WA":"America/Los_Angeles","WV":"America/New_York",
    "WI":"America/Chicago","WY":"America/Denver","DC":"America/New_York",
}
ROLLOVER_HOUR = 19  # roll to the next event 1h after the Sunday 18:00 close (Lauren 2026-06-15)


def _event_status(ev, now_utc):
    """'live' | 'upcoming' | 'past' using the event's LOCAL timezone.
    live = within [start 00:00, end_date 19:00] local (19:00 = 1h after the Sunday close)."""
    tz = ZoneInfo(STATE_TZ.get((ev.get("state") or "").upper(), "America/Los_Angeles"))
    loc = now_utc.astimezone(tz)
    sd = datetime.date.fromisoformat(ev["start_date"])
    ed = datetime.date.fromisoformat(ev.get("end_date") or ev.get("_ed") or ev["start_date"])
    start  = datetime.datetime.combine(sd, datetime.time(0, 0), tzinfo=tz)
    cutoff = datetime.datetime.combine(ed, datetime.time(ROLLOVER_HOUR, 0), tzinfo=tz)
    if loc >= cutoff:
        return "past"
    if loc >= start:
        return "live"
    return "upcoming"


def _write_if_changed(path, new_obj):
    """Write only if meaningful content changed (ignoring _updated_at). Returns True if written.
    Prevents the multi-zone Sunday-19:00 crons from committing/SMSing on no-op fires."""
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        old = None
    def _strip(o):
        return {k: v for k, v in o.items() if k != "_updated_at"} if isinstance(o, dict) else o
    if isinstance(old, dict) and _strip(old) == _strip(new_obj):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_obj, indent=2, ensure_ascii=False))
    return True


def today_pt() -> datetime.date:
    """Return today's date in America/Los_Angeles. cron runs in UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        # Approximate fallback - UTC minus 7h (PDT) or 8h (PST). Worst case
        # we pick the wrong event for ~1h on a day-boundary.
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=7)).date()


def main() -> int:
    if not LAUNCH.exists():
        print(f"ERR: {LAUNCH} not found", file=sys.stderr)
        return 1

    html      = LAUNCH.read_text(encoding="utf-8")
    SCHEDULE  = parse_map(html, "SCHEDULE")
    SETUPS    = parse_map(html, "SETUPS")
    notes     = json.loads(NOTES.read_text(encoding="utf-8")) if NOTES.exists() else {}
    form_ids_root = json.loads(FORM_IDS.read_text(encoding="utf-8")) if FORM_IDS.exists() else {}
    form_ids  = form_ids_root.get("events", {}) if isinstance(form_ids_root, dict) else {}

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # Flatten events from year-keyed lists (skip _baked_at and other meta keys)
    all_events = []
    for year, lst in SCHEDULE.items():
        if not (isinstance(lst, list) and year.isdigit()):
            continue
        for ev in lst:
            sd = ev.get("start_date")
            if not sd:
                continue
            all_events.append({**ev, "_ed": ev.get("end_date") or sd})

    # Pick using each event's LOCAL timezone: an event stays "live" until 19:00 on its
    # end_date (1h after the Sunday 18:00 close - Lauren 2026-06-15), then we roll to the
    # next upcoming event. This is what rotates the QR door page 1h after the event ends.
    def parse(s): return datetime.date.fromisoformat(s)
    current = None
    upcoming = []
    for ev in all_events:
        try:
            parse(ev["start_date"]); parse(ev["_ed"])
        except Exception:
            continue
        status = _event_status(ev, now_utc)
        if status == "live" and current is None:
            current = ev
        elif status == "upcoming":
            upcoming.append((parse(ev["start_date"]), ev))
    upcoming.sort(key=lambda x: x[0])

    picked = current or (upcoming[0][1] if upcoming else None)
    if not picked:
        print("ERR: no current or upcoming events found in SCHEDULE", file=sys.stderr)
        return 1

    city_slug  = slug_of(picked["city"])
    state_lc   = (picked.get("state") or "").lower()
    year_str   = picked["start_date"][:4]
    setups_key = f"{city_slug}-{picked['start_date']}"
    form_key   = f"{city_slug}-{state_lc}-{year_str}-{picked['start_date']}"

    setup = SETUPS.get(setups_key) or {}
    form  = form_ids.get(form_key) or {}
    note  = notes.get(setups_key) or {}

    smslist = setup.get("smslist") or {}

    target = {
        "_doc": (
            "Auto-generated by .github/workflows/update-subscribe-target.yml. "
            "Picks current/next event for the public /subscribe-list/ landing page on "
            "themakeupblowoutsale-group.com. Do not edit by hand."
        ),
        "_updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_key":   setups_key,
        "form_key":    form_key,
        "city":        picked["city"],
        "state":       picked.get("state", ""),
        "start_date":  picked["start_date"],
        "end_date":    picked.get("end_date") or picked["start_date"],
        "venue":       picked.get("venue", ""),
        "address":     picked.get("address", ""),
        "is_live":     bool(current),
        "form_id":     form.get("form_id"),
        "form_url":    form.get("form_url"),
        "list_id":     smslist.get("list_id"),
        "list_name":   smslist.get("list_name"),
        "ig_reel_url": note.get("insta_reel_url") or DEFAULT_IG,
        "fb_url":      note.get("fb_url")         or DEFAULT_FB,
        "tiktok_url":  note.get("tiktok_url")     or DEFAULT_TT,
    }


    # ─────────────────────────────────────────────
    # Also publish the upcoming-events list (consumed by /events/ page on
    # themakeupblowoutsale-group.com — same repo as /subscribe-list/).
    # ─────────────────────────────────────────────
    upcoming_list = []
    for sd, ev in upcoming[:14]:  # 14 = ~3.5 months of weekly events (covers Lauren's 3-month visible window)
        u_city_slug  = slug_of(ev["city"])
        u_state_lc   = (ev.get("state") or "").lower()
        u_year       = ev["start_date"][:4]
        u_setup_key  = f"{u_city_slug}-{ev['start_date']}"
        u_form_key   = f"{u_city_slug}-{u_state_lc}-{u_year}-{ev['start_date']}"
        u_setup      = SETUPS.get(u_setup_key) or {}
        u_form       = form_ids.get(u_form_key) or {}
        u_smslist    = u_setup.get("smslist") or {}
        u_eb         = u_setup.get("eventbrite") or {}
        u_note = notes.get(u_setup_key) or {}
        upcoming_list.append({
            "event_key":      u_setup_key,
            "city":           ev["city"],
            "state":          ev.get("state", ""),
            "start_date":     ev["start_date"],
            "end_date":       ev.get("end_date") or ev["start_date"],
            "venue":          ev.get("venue", ""),
            "address":        ev.get("address", ""),
            "form_id":        u_form.get("form_id"),
            "list_id":        u_smslist.get("list_id"),
            "eventbrite_url": u_eb.get("url"),
            "ig_reel_url":    u_note.get("insta_reel_url"),
            "fb_url":         u_note.get("fb_url"),
            "tiktok_url":     u_note.get("tiktok_url"),
        })
    upcoming_doc = {
        "_doc": ("Auto-generated by .github/workflows/update-subscribe-target.yml. "
                 "Lists the next 8 upcoming events for /events/ page on "
                 "themakeupblowoutsale-group.com. Do not edit by hand."),
        "_updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": upcoming_list,
    }
    if _write_if_changed(UPCOMING, upcoming_doc):
        print(f"wrote {UPCOMING}: {len(upcoming_list)} upcoming events")
    else:
        print(f"{UPCOMING} unchanged - skip")

    tg_changed = _write_if_changed(TARGET, target)
    if tg_changed:
        n = TARGET.stat().st_size
        print(f"wrote {TARGET} ({n}B): event_key={target['event_key']} "
              f"is_live={target['is_live']} form_id={target['form_id']}")
    else:
        print(f"{TARGET} unchanged - skip (event_key={target['event_key']})")

    # Self-healing: sync LANDING_PAGES with reality (catches cases where @landing
    # built a per-event page but forgot to upsert the dashboard map — STEP 6 in SKILL).
    print("--- syncing LANDING_PAGES with live events repo ---")
    sync_landing_pages_map()

    try:
        from run_summary import record
        def _md(d):
            try: return f"{int(d[5:7])}/{int(d[8:10])}"
            except Exception: return d or ""
        _bl = [f"דף ה-QR מצביע על: {target['city']}, {target.get('state','')} ({_md(target.get('start_date'))}–{_md(target.get('end_date'))})"]
        if target.get("list_name"):
            _bl.append(f"רשימת SMS: {target['list_name']}")
        _bl.append("האירוע פעיל כעת" if target.get("is_live") else "ממתין לאירוע הקרוב")
        _bl.append("התחלף לאירוע חדש בריצה זו" if tg_changed else "ללא שינוי מהריצה הקודמת")
        record("update-subscribe-target", _bl, status="ok")
    except Exception as e:
        print(f"[summary] skipped: {e}")

    return 0


def sync_landing_pages_map() -> int:
    """
    Self-healing: scan events.themakeupblowout.com for per-event landing pages
    that exist on disk but aren't registered in LANDING_PAGES map. Auto-add
    missing entries so the launch dashboard's 🪧 Landing buttons flip green.

    This is the safety net for @landing agent's STEP 6 (SKILL.md) — when the
    agent forgets to upsert the map, this catches it within 24h.

    Returns 0 always (sync failures are non-fatal — main subscribe-target work
    has already succeeded by this point).
    """
    import urllib.request
    LAUNCH = ROOT / "docs/launch/index.html"
    if not LAUNCH.exists():
        return 0

    html = LAUNCH.read_text(encoding="utf-8")
    SCHEDULE = parse_map(html, "SCHEDULE")
    map_match = re.search(r"const LANDING_PAGES = (\{[^;]+\});", html)
    if not map_match:
        print("  sync: no LANDING_PAGES anchor — skipping")
        return 0
    LANDING_PAGES = json.loads(map_match.group(1))

    # Walk all upcoming events from SCHEDULE
    today = today_pt()
    candidates = []
    for year, lst in SCHEDULE.items():
        if not (isinstance(lst, list) and year.isdigit()):
            continue
        for ev in lst:
            sd = ev.get("start_date")
            if not sd:
                continue
            try:
                sd_d = datetime.date.fromisoformat(sd)
            except Exception:
                continue
            if sd_d < today:
                continue   # ignore past events; they had their landing page (or didnt) and the moment has passed
            candidates.append((ev, sd_d))

    added = 0
    for ev, sd_d in candidates:
        city_slug  = slug_of(ev["city"])
        state_lc   = (ev.get("state") or "").lower()
        year_str   = ev["start_date"][:4]
        evkey_short = f"{city_slug}-{ev['start_date']}"            # SETUPS / LANDING_PAGES key
        evkey_full  = f"{city_slug}-{state_lc}-{year_str}"           # /events/<slug>/ URL slug

        # Skip if already in map
        if evkey_short in LANDING_PAGES:
            continue

        # HEAD probe — does the landing page actually exist on the events site?
        url = f"https://events.themakeupblowout.com/events/{evkey_full}/"
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                if not (200 <= r.status < 300):
                    continue
        except Exception:
            continue

        # It exists but isn't mapped → upsert it
        LANDING_PAGES[evkey_short] = {
            "url":          url,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
            "has_form_id":  True,
            "has_venue":    True,
            "has_ig_url":   True,
        }
        added += 1
        print(f"  sync: + {evkey_short} → {url}")

    if added == 0:
        print("  sync: LANDING_PAGES already in sync — no changes")
        return 0

    # Write back surgically (preserves all other content)
    new_block = "const LANDING_PAGES = " + json.dumps(LANDING_PAGES, ensure_ascii=False, separators=(",", ":")) + ";"
    new_html = re.sub(r"const LANDING_PAGES = \{[^;]+\};", new_block, html, count=1)
    LAUNCH.write_text(new_html, encoding="utf-8")
    print(f"  sync: wrote launch/index.html — {added} entries added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
