"""
insta_reel_scan — IG Reel share-count scan for active event weekends.

Triggered hourly on Tue–Sun by .github/workflows/insta-reel-share-scan.yml.
Two scan phases (2026-05-20):
  • pre_event  — Tue/Wed/Thu in the 3 days leading up to an event.
                 One scan per day at event-local 12:00 (organic baseline).
  • event_live — Fri/Sat/Sun in the event's start–end window.
                 Three scans per day at event-local 12/14/17.
For each active event, computes the current event-local hour. If that hour
is in the phase's slot set, fetches Instagram Graph insights for the
configured Reel (manual override or auto-pinned) and appends a scan record
to MANUAL_TASKS[evkey].insta_reel_scans in docs/launch/notes.json.

Designed to fail soft — never crashes the workflow; missing tokens, no
active events, or a single API failure all just produce a no-op log line.
"""

import datetime as _dt
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Make sibling lauren_meta importable when running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lauren_meta as meta
import lauren_sms as sms

NOTES_PATH = Path("docs/launch/notes.json")
LAUNCH_HTML_PATH = Path("docs/launch/index.html")

# Recipients for the per-scan SMS summary (Lauren 2026-05-10 PM)
ELI_PHONE = os.environ.get("ELI_PHONE", "").lstrip("+").lstrip("1")

# US state → IANA timezone (covers all states currently in Lauren's schedule)
STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "HI": "Pacific/Honolulu", "ID": "America/Boise",
    "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago",
    "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/Detroit", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York",
    "NM": "America/Denver", "NY": "America/New_York", "NC": "America/New_York",
    "ND": "America/Chicago", "OH": "America/New_York", "OK": "America/Chicago",
    "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "UT": "America/Denver", "VT": "America/New_York",
    "VA": "America/New_York", "WA": "America/Los_Angeles", "WV": "America/New_York",
    "WI": "America/Chicago", "WY": "America/Denver", "DC": "America/New_York",
}

# Hours (event-local) at which we scan.
# event_live days (Fri/Sat/Sun in event window) scan at 12/14/17 local.
# pre_event days (Tue/Wed/Thu in the 3 days leading up to a Fri-Sun event)
# scan once per day at 12 local — daily baseline so we capture the organic
# share momentum BEFORE the event weekend (2026-05-20 — Lauren's directive).
SCAN_HOURS_EVENT = {12, 14, 17}
SCAN_HOURS_PRE   = {12}
SCAN_HOURS = SCAN_HOURS_EVENT  # legacy alias


def _slug(city: str, start_date: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (city or "").lower()).strip("-")
    return f"{s}-{start_date}"


def _load_schedule() -> list:
    """Pull SCHEDULE constant from launch/index.html so the workflow stays in
    sync with whatever Lauren has uploaded most recently. Returns a flat
    list of all events across all years."""
    html = LAUNCH_HTML_PATH.read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = ({.*?});\n", html, re.S)
    if not m:
        print("[scan] could not find SCHEDULE constant; bailing.")
        return []
    sched = json.loads(m.group(1))
    out = []
    for year_key, events in sched.items():
        if not isinstance(events, list):
            continue
        out.extend(events)
    return out


def _load_notes() -> dict:
    if NOTES_PATH.exists():
        try:
            return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_notes(notes: dict) -> None:
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTES_PATH.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")


def _active_events_today(events: list, today: _dt.date) -> list:
    """Events whose Friday-Sunday window contains today."""
    out = []
    for e in events:
        try:
            sd = _dt.date.fromisoformat(e["start_date"])
            ed = _dt.date.fromisoformat(e["end_date"])
        except Exception:
            continue
        if sd <= today <= ed:
            out.append(e)
    return out


def _event_local_hour(state: str) -> int:
    """Return the current hour in the event's local timezone (0-23)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python <3.9 fallback — naive UTC hour, no DST awareness.
        return _dt.datetime.utcnow().hour
    tz = STATE_TZ.get((state or "").upper(), "America/Los_Angeles")
    return _dt.datetime.now(ZoneInfo(tz)).hour


def _event_local_now(state: str):
    """Return the current datetime in the event's local timezone (or naive UTC fallback)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return _dt.datetime.utcnow()
    tz = STATE_TZ.get((state or "").upper(), "America/Los_Angeles")
    return _dt.datetime.now(ZoneInfo(tz))


def _resolve_reel(notes: dict, evkey: str) -> tuple[str, str, str]:
    """
    Returns (reel_url, set_by, media_id_or_empty).
    Priority:
      1. Manual override stored in notes[evkey].insta_reel_url (set_by="manual")
      2. Auto: most recent Reel from the IG business account (set_by="auto")
    """
    note = notes.get(evkey) or {}
    manual_url = (note.get("insta_reel_url") or "").strip()
    set_by = note.get("insta_reel_url_set_by") or ""

    if manual_url and set_by == "manual":
        media_id = meta.find_media_id_by_permalink(manual_url) or ""
        return manual_url, "manual", media_id

    pinned = meta.find_pinned_or_latest_reel()
    if pinned:
        return pinned.get("permalink", ""), "auto", pinned.get("id", "")

    # Last resort: keep whatever is there even if origin unknown
    return manual_url, set_by or "manual", ""


def main() -> int:
    if not os.environ.get("META_PAGE_TOKEN"):
        print("[scan] META_PAGE_TOKEN not set; nothing to do.")
        return 0

    # Active = any event whose Fri-Sun window contains TODAY in the event's
    # LOCAL timezone — not UTC. At boundary times (e.g. ~midnight UTC =
    # ~7 PM ET prev-day = ~5 PM PT prev-day), UTC and event-local can be
    # on different calendar days; the event-local date is the one that
    # matters for "is the event happening right now".
    # 2026-05-10 PM bugfix — script previously returned early when UTC
    # ticked to Monday even though Columbia (CDT) was still Sunday evening.
    try:
        from zoneinfo import ZoneInfo as _ZI
    except ImportError:
        _ZI = None

    # 2026-05-20 — TWO phases:
    #   pre_event  → today is 1-3 calendar days BEFORE the event's start_date
    #                (Tue/Wed/Thu before a Fri-Sun event). One scan per day at
    #                event-local 12:00 — daily organic baseline.
    #   event_live → today is in the event's [start_date, end_date] window
    #                (Fri/Sat/Sun). Three scans per day at event-local 12/14/17.
    PRE_EVENT_DAYS = 3
    events = _load_schedule()
    active = []  # list of (ev, phase) tuples
    for ev in events:
        st = (ev.get("state") or "").upper()
        tz_name = STATE_TZ.get(st, "America/Los_Angeles")
        if _ZI is not None:
            try:
                local_today = _dt.datetime.now(_ZI(tz_name)).date()
            except Exception:
                local_today = _dt.date.today()
        else:
            local_today = _dt.date.today()
        try:
            sd = _dt.date.fromisoformat(ev["start_date"])
            ed = _dt.date.fromisoformat(ev["end_date"])
        except Exception:
            continue
        if sd <= local_today <= ed:
            active.append((ev, "event_live"))
        else:
            days_until = (sd - local_today).days
            if 1 <= days_until <= PRE_EVENT_DAYS:
                active.append((ev, "pre_event"))
    # For the rest of the function, use UTC `today` as a logging label
    today = _dt.date.today()
    if not active:
        print(f"[scan] no active events on {today}.")
        return 0

    notes = _load_notes()
    now_utc = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    any_change = False

    def _delta(cur_val, prev_val):
        if cur_val is None or prev_val is None:
            return ""
        d = cur_val - prev_val
        if d == 0:
            return " (±0)"
        return f" ({'+' if d > 0 else ''}{d})"

    for ev, phase in active:
        city = ev.get("city", "?")
        state = ev.get("state", "")
        evkey = _slug(city, ev.get("start_date", ""))
        local_hour = _event_local_hour(state)

        # Slot set depends on phase. Pre-event = one baseline at 12:00 local;
        # event-live = three scans at 12/14/17 local (existing cadence).
        SCAN_SLOTS = sorted(SCAN_HOURS_EVENT if phase == "event_live" else SCAN_HOURS_PRE)

        # Slot logic — 2026-05-22 PM update.
        # pre_event phase: still gates by slot (one scan/day at 12:00 local; backfills if cron lagged).
        # event_live phase: NO slot constraint — every cron fire scans, with a 25-minute floor since the
        # most recent event_live scan today to prevent doubled-up writes if a cron retries.
        # Lauren's directive: during a live event she wants ~30 min cadence, not 3h. Cron is */30 on event days.
        notes.setdefault(evkey, {})
        existing = notes[evkey].get("insta_reel_scans") or []
        today_str = now_utc[:10]
        if phase == "event_live":
            # 2026-05-22 PM Lauren directive — only scan during the actual event window:
            # event-local 10:30 to 17:00 (event starts 10:00, first scan 30 min after open,
            # last scan at close at 17:00). Outside that window: skip silently.
            local_now = _event_local_now(state)
            local_min_of_day = local_now.hour * 60 + local_now.minute
            WINDOW_START = 10 * 60 + 30   # 10:30
            WINDOW_END   = 17 * 60        # 17:00
            if local_min_of_day < WINDOW_START or local_min_of_day > WINDOW_END:
                print(f"[scan] {evkey}: outside event-local window 10:30-17:00 (local {local_now.hour:02d}:{local_now.minute:02d}); skip.")
                continue
            # Rate-limit guard: skip if last event_live scan today was < 25 min ago.
            last_today = None
            for s in reversed(existing):
                if (s.get("scanned_at", "")[:10] == today_str
                        and s.get("phase") == "event_live"
                        and not (s.get("source") or "").startswith("manual")):
                    last_today = s; break
            if last_today:
                try:
                    last_dt = _dt.datetime.fromisoformat((last_today.get("scanned_at") or "").replace("Z", "+00:00"))
                    age_min = (_dt.datetime.now(last_dt.tzinfo) - last_dt).total_seconds() / 60.0
                    if age_min < 25:
                        print(f"[scan] {evkey}: last event_live scan only {age_min:.0f} min ago; skip (25-min floor).")
                        continue
                except Exception:
                    pass
            # In event_live the "slot" concept doesn't apply — record the actual local hour.
            eligible_slots = [local_hour]
        else:
            # pre_event — keep slot logic.
            done_slots = {
                s.get("event_local_hour")
                for s in existing
                if (s.get("scanned_at", "")[:10] == today_str and s.get("phase") == "pre_event")
            }
            eligible_slots = [S for S in SCAN_SLOTS if local_hour >= S and S not in done_slots]
            if not eligible_slots:
                print(f"[scan] {evkey}: local hour {local_hour:02d} · pre_event slots done today {sorted(done_slots)}; nothing eligible.")
                continue

        # ── Slot 2 (New Reel) — independent second reel for this event, same scan cadence (Lauren 2026-06-09).
        # Tracked separately into insta_reel_scans_2; powers the 🎬 New Reel button + its stats modal.
        reel_url2 = (notes[evkey].get("insta_reel_url_2") or "").strip()
        if reel_url2:
            media_id2 = ""
            try:
                media_id2 = meta.find_media_id_by_permalink(reel_url2) or ""
            except Exception as e:
                print(f"[scan] {evkey}: slot2 media resolve failed: {e}")
            if media_id2:
                existing2 = notes[evkey].get("insta_reel_scans_2") or []
                done2 = {s.get("event_local_hour") for s in existing2
                         if s.get("scanned_at", "")[:10] == today_str and s.get("phase") == phase}
                slots2 = [S for S in eligible_slots if S not in done2]
                if slots2:
                    try:
                        insights2 = meta.fetch_media_insights(media_id2)
                        for slot in slots2:
                            existing2.append({
                                "scanned_at": now_utc, "event_local_hour": slot,
                                "actual_local_hour": local_hour, "phase": phase,
                                "url_at_scan": reel_url2, "media_id": media_id2,
                                "shares": insights2.get("shares"), "views": insights2.get("views"),
                                "reach": insights2.get("reach"), "likes": insights2.get("likes"),
                                "comments": insights2.get("comments"), "saved": insights2.get("saved"),
                                "total_interactions": insights2.get("total_interactions"),
                                "catchup": (local_hour != slot),
                            })
                        notes[evkey]["insta_reel_scans_2"] = existing2
                        notes[evkey]["updated_at"] = now_utc
                        any_change = True
                        print(f"[scan] {evkey}: slot2 (New Reel) appended {len(slots2)} slot(s) -> shares={insights2.get('shares')}")
                    except Exception as e:
                        print(f"[scan] {evkey}: slot2 insights/append failed: {e}")

        reel_url, set_by, media_id = _resolve_reel(notes, evkey)
        if not reel_url or not media_id:
            print(f"[scan] {evkey}: no resolvable reel (url={reel_url!r}, media_id={media_id!r}); skip.")
            continue

        try:
            insights = meta.fetch_media_insights(media_id)
        except Exception as e:
            print(f"[scan] {evkey}: insights fetch failed: {e}")
            continue

        # Persist URL meta once (in case auto-detect just picked it up)
        if reel_url and notes[evkey].get("insta_reel_url") != reel_url:
            notes[evkey]["insta_reel_url"] = reel_url
            notes[evkey]["insta_reel_url_set_by"] = set_by
            notes[evkey]["insta_reel_url_set_at"] = now_utc

        # Per eligible slot: append a scan record + send an SMS digest.
        # When catching up multiple slots in one run, each gets its own record
        # (same scanned_at, same insights — Meta API doesn\'t expose historical
        # snapshots, so this is the best available proxy for the missed slot).
        for slot in eligible_slots:
            scan_rec = {
                "scanned_at": now_utc,
                "event_local_hour": slot,
                "actual_local_hour": local_hour,
                "phase":    phase,
                "url_at_scan": reel_url,
                "media_id": media_id,
                "shares":   insights.get("shares"),
                "views":    insights.get("views"),   # 2026-05-20 — 'plays' deprecated in v22+
                "reach":    insights.get("reach"),
                "likes":    insights.get("likes"),
                "comments": insights.get("comments"),
                "saved":    insights.get("saved"),
                "total_interactions": insights.get("total_interactions"),
                "catchup":  (local_hour != slot),
            }
            existing.append(scan_rec)
            notes[evkey]["insta_reel_scans"] = existing
            notes[evkey]["updated_at"] = now_utc
            any_change = True
            print(f"[scan] {evkey}: appended slot {slot:02d}:00 phase={phase} (actual local {local_hour:02d}:00, catchup={scan_rec['catchup']}) → shares={scan_rec['shares']} views={scan_rec['views']} reach={scan_rec['reach']}")

            # SMS summary to Lauren + Eli — Hebrew, ends with the reel URL.
            try:
                prev = existing[-2] if len(existing) >= 2 else None
                sh, vw, re_, lk = scan_rec.get('shares'), scan_rec.get('views'), scan_rec.get('reach'), scan_rec.get('likes')
                psh = prev.get('shares') if prev else None
                # Back-compat: legacy scans stored the metric under 'plays' (pre-v22).
                pvw = (prev.get('views') if prev else None) or (prev.get('plays') if prev else None)
                pre = prev.get('reach')  if prev else None
                plk = prev.get('likes')  if prev else None
                scan_num = len(existing)
                ev_label = f"{city}, {state}"
                catchup_note = "  ⚠ catch-up (cron איחור)" if scan_rec['catchup'] else ""
                phase_label = "סריקה לפני-אירוע" if phase == "pre_event" else "סריקה בזמן אירוע"
                sms_body = (
                    f"📸 INSTA REEL · {phase_label} #{scan_num}\n"
                    f"{ev_label} · {ev.get('start_date','')} · סלוט {slot:02d}:00 מקומי{catchup_note}\n"
                    f"\n"
                    f"Shares: {sh if sh is not None else '—'}{_delta(sh, psh)}\n"
                    f"Views:  {vw if vw is not None else '—'}{_delta(vw, pvw)}\n"
                    f"Reach:  {re_ if re_ is not None else '—'}{_delta(re_, pre)}\n"
                    f"Likes:  {lk if lk is not None else '—'}{_delta(lk, plk)}\n"
                    f"\n"
                    f"{reel_url}"
                )
                recipients = []
                if sms.LAUREN_PHONE:
                    recipients.append(("Lauren", sms.LAUREN_PHONE))
                if ELI_PHONE:
                    recipients.append(("Eli", ELI_PHONE))
                for name, phone in recipients:
                    try:
                        sms.send_sms(phone, sms_body)
                        print(f"[scan] {evkey}: SMS sent to {name} ({phone}) for slot {slot:02d}.")
                    except Exception as se:
                        print(f"[scan] {evkey}: SMS to {name} failed: {se}")
            except Exception as e:
                print(f"[scan] {evkey}: SMS summary block failed: {e}")

    if any_change:
        _save_notes(notes)
        print("[scan] notes.json updated.")
    else:
        print("[scan] no changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
