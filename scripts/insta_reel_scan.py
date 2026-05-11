"""
insta_reel_scan — IG Reel share-count scan for active event weekends.

Triggered hourly on Fri/Sat/Sun by .github/workflows/insta-reel-share-scan.yml.
For each event whose Friday-Sunday window contains today, computes the
current event-local hour. If that hour ∈ {12, 14, 17}, fetches Instagram
Graph insights for the configured Reel (manual override or auto-pinned)
and appends a scan record to MANUAL_TASKS[evkey].insta_reel_scans in
docs/launch/notes.json.

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
SCAN_HOURS = {12, 14, 17}


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

    today = _dt.date.today()
    if today.weekday() not in (4, 5, 6):  # Fri=4, Sat=5, Sun=6
        print(f"[scan] today is {today} ({today.strftime('%A')}); not Fri/Sat/Sun.")
        return 0

    events = _load_schedule()
    active = _active_events_today(events, today)
    if not active:
        print(f"[scan] no active events on {today}.")
        return 0

    notes = _load_notes()
    now_utc = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    any_change = False

    SCAN_SLOTS = sorted(SCAN_HOURS)   # [12, 14, 17]

    def _delta(cur_val, prev_val):
        if cur_val is None or prev_val is None:
            return ""
        d = cur_val - prev_val
        if d == 0:
            return " (±0)"
        return f" ({'+' if d > 0 else ''}{d})"

    for ev in active:
        city = ev.get("city", "?")
        state = ev.get("state", "")
        evkey = _slug(city, ev.get("start_date", ""))
        local_hour = _event_local_hour(state)

        # Slot-based catch-up (Lauren 2026-05-10 PM bugfix).
        # GitHub Actions cron can lag 30-90 min; a strict `if local_hour not in
        # [12,14,17]: skip` missed scans entirely when the cron landed at e.g.
        # 15:32 instead of 14:00. New rule: for each scan slot S, if local_hour
        # >= S AND no scan recorded for S today, do the scan now with
        # event_local_hour = S (the slot label, not the actual hour). One
        # delayed run can backfill multiple missed slots in a single pass, and
        # idempotency by slot still holds.
        notes.setdefault(evkey, {})
        existing = notes[evkey].get("insta_reel_scans") or []
        today_str = now_utc[:10]
        done_slots = {
            s.get("event_local_hour")
            for s in existing
            if (s.get("scanned_at", "")[:10] == today_str)
        }
        eligible_slots = [S for S in SCAN_SLOTS if local_hour >= S and S not in done_slots]
        if not eligible_slots:
            print(f"[scan] {evkey}: local hour {local_hour:02d} · slots done today {sorted(done_slots)}; nothing eligible.")
            continue

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
                "url_at_scan": reel_url,
                "media_id": media_id,
                "shares":   insights.get("shares"),
                "plays":    insights.get("plays"),
                "reach":    insights.get("reach"),
                "likes":    insights.get("likes"),
                "comments": insights.get("comments"),
                "saved":    insights.get("saved"),
                "catchup":  (local_hour != slot),
            }
            existing.append(scan_rec)
            notes[evkey]["insta_reel_scans"] = existing
            notes[evkey]["updated_at"] = now_utc
            any_change = True
            print(f"[scan] {evkey}: appended slot {slot:02d}:00 (actual local {local_hour:02d}:00, catchup={scan_rec['catchup']}) → shares={scan_rec['shares']} plays={scan_rec['plays']} reach={scan_rec['reach']}")

            # SMS summary to Lauren + Eli — Hebrew, ends with the reel URL.
            try:
                prev = existing[-2] if len(existing) >= 2 else None
                sh, pl, re_, lk = scan_rec.get('shares'), scan_rec.get('plays'), scan_rec.get('reach'), scan_rec.get('likes')
                psh = prev.get('shares') if prev else None
                ppl = prev.get('plays')  if prev else None
                pre = prev.get('reach')  if prev else None
                plk = prev.get('likes')  if prev else None
                scan_num = len(existing)
                ev_label = f"{city}, {state}"
                catchup_note = "  ⚠ catch-up (cron איחור)" if scan_rec['catchup'] else ""
                sms_body = (
                    f"📸 INSTA REEL · סריקה {scan_num}\n"
                    f"{ev_label} · {ev.get('start_date','')} · סלוט {slot:02d}:00 מקומי{catchup_note}\n"
                    f"\n"
                    f"Shares: {sh if sh is not None else '—'}{_delta(sh, psh)}\n"
                    f"Plays:  {pl if pl is not None else '—'}{_delta(pl, ppl)}\n"
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
