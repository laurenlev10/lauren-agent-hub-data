"""
daily_reel_shares_digest — one-SMS-per-day summary of organic Reel shares
across the next upcoming events. Mon-Thu 09:00 PT (Lauren 2026-05-20 PM).

Per IRON RULE: shares are the #1 metric — surfacing them daily across
all upcoming events helps Lauren see organic momentum building before
each event weekend. Companion to insta-reel-share-scan.yml (which gives
per-event per-slot detail); this digest gives a single quick check-in.

Output: one SMS to Lauren + Eli with one line per event (up to 5),
showing current shares + delta from yesterday's value + group total.
Also appends a digest scan record (phase='digest_daily',
source='daily_reel_shares_digest') to notes.json so history grows even
on Mon (which the regular pre_event scanner skips).
"""
import datetime as _dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lauren_meta as meta
import lauren_sms as sms
from insta_reel_scan import _load_schedule, _slug

NOTES_PATH = Path("docs/launch/notes.json")
MAX_EVENTS = 5  # SMS-segment-friendly (Lauren currently has 4 upcoming reels)
HORIZON_DAYS = 30
DASH_URL = "https://dashboard.themakeupblowout.com/launch/"

ELI_PHONE = os.environ.get("ELI_PHONE", "").lstrip("+").lstrip("1")


def _shortcode_from_url(url):
    m = re.search(r"/reel/([A-Za-z0-9_-]+)/?", url or "")
    return m.group(1) if m else None


def _fmt_he_date(date_iso):
    d = _dt.date.fromisoformat(date_iso)
    return f"{d.day}.{d.month}"


def _delta_str(cur, prev):
    if cur is None or prev is None:
        return ""
    d = cur - prev
    if d > 0:
        return f" (+{d})"
    if d < 0:
        return f" ({d})"
    return " (±0)"


def main():
    if not os.environ.get("META_SYSTEM_USER_TOKEN"):
        print("[digest] META_SYSTEM_USER_TOKEN not set; bail.")
        return 0
    notes = json.load(open(NOTES_PATH))
    events = _load_schedule()
    today = _dt.date.today()
    now_utc = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    today_str = now_utc[:10]

    # Build shortcode → media_id map
    ig_id = os.environ["META_IG_BUSINESS_ID"]
    su_token = os.environ["META_SYSTEM_USER_TOKEN"]
    url = (
        f"https://graph.facebook.com/v25.0/{ig_id}/media"
        f"?fields=id,shortcode,permalink&limit=100&access_token={su_token}"
    )
    data = json.loads(urllib.request.urlopen(url, timeout=20).read())
    sc2id = {m["shortcode"]: m["id"] for m in data.get("data", [])}
    print(f"[digest] resolved {len(sc2id)} IG media shortcodes")

    # Gather upcoming events with reel URLs (sorted by start_date, capped)
    upcoming = []
    for ev in events:
        try:
            sd = _dt.date.fromisoformat(ev["start_date"])
        except Exception:
            continue
        if sd < today or (sd - today).days > HORIZON_DAYS:
            continue
        evkey = _slug(ev.get("city", "?"), ev["start_date"])
        note = notes.get(evkey, {})
        if not (note.get("insta_reel_url") or note.get("insta_reel_url_2")):
            continue
        upcoming.append((ev, evkey, note))
    upcoming.sort(key=lambda x: x[0]["start_date"])
    upcoming = upcoming[:MAX_EVENTS]

    if not upcoming:
        print("[digest] no upcoming events with reel URL; no SMS.")
        return 0

    lines = []
    total = 0
    total_delta = 0
    have_any_delta = False
    any_change = False

    for ev, evkey, note in upcoming:
        for _sfx in ("", "_2"):
            # 2026-06-10 — scan BOTH reel slots: slot "" = Reel 1, slot "_2" = New Reel
            # (the reel running in the Meta campaigns for the final week + the SHARE page).
            reel_url = (note.get("insta_reel_url" + _sfx) or "").strip()
            if not reel_url:
                continue
            sc = _shortcode_from_url(reel_url)
            media_id = sc2id.get(sc)
            if not media_id:
                print(f"[digest] {evkey}: shortcode {sc} not in recent media; skip")
                continue
            try:
                ins = meta.fetch_media_insights(media_id)
            except Exception as e:
                print(f"[digest] {evkey}: insights fetch failed: {e}")
                continue

            shares = ins.get("shares")
            # Find most recent prior scan with a non-null shares value
            prev_shares = None
            existing = note.get("insta_reel_scans" + _sfx) or []
            # Idempotency — if a digest scan was already appended today, skip this event.
            # Defensive: protects against double-runs (manual dispatch + retry, cron + manual).
            if any(
                (s.get("scanned_at", "")[:10] == today_str)
                and s.get("source") == "daily_reel_shares_digest"
                for s in existing
            ):
                print(f"[digest] {evkey}: digest already appended today; skip append (still reading shares for SMS)")
                already_appended_today = True
            else:
                already_appended_today = False
            for s in reversed(existing):
                if s.get("scanned_at", "")[:10] < today_str and s.get("shares") is not None:
                    prev_shares = s.get("shares")
                    break

            delta = (shares - prev_shares) if (shares is not None and prev_shares is not None) else None
            ds = _delta_str(shares, prev_shares)
            city = ev.get("city", "?")
            date_short = _fmt_he_date(ev["start_date"])
            _slot_lbl = " · ריל חדש" if _sfx else ""
            lines.append(f"{city} ({date_short}){_slot_lbl} · {shares if shares is not None else '—'}{ds}")
            if shares is not None:
                total += shares
            if delta is not None:
                total_delta += delta
                have_any_delta = True

            # Append digest scan record (phase='digest_daily')
            scan_rec = {
                "scanned_at": now_utc,
                "event_local_hour": None,
                "actual_local_hour": None,
                "phase": "digest_daily",
                "source": "daily_reel_shares_digest",
                "url_at_scan": reel_url,
                "media_id": media_id,
                "shares": ins.get("shares"),
                "views": ins.get("views"),
                "reach": ins.get("reach"),
                "likes": ins.get("likes"),
                "comments": ins.get("comments"),
                "saved": ins.get("saved"),
                "catchup": False,
            }
            if not already_appended_today:
                existing.append(scan_rec)
                notes.setdefault(evkey, {})
                notes[evkey]["insta_reel_scans" + _sfx] = existing
                notes[evkey]["updated_at"] = now_utc
                any_change = True

    if not lines:
        print("[digest] no events had usable data; no SMS.")
        return 0

    today_he = f"{today.day}.{today.month}"
    body_parts = [
        "📸 שיתופי Reel · אירועים קרובים",
        today_he,
        "",
    ]
    body_parts.extend(lines)
    body_parts.append("")
    if have_any_delta:
        if total_delta > 0:
            body_parts.append(f"סה״כ: {total} (+{total_delta} מאתמול)")
        elif total_delta < 0:
            body_parts.append(f"סה״כ: {total} ({total_delta} מאתמול)")
        else:
            body_parts.append(f"סה״כ: {total} (±0 מאתמול)")
    else:
        body_parts.append(f"סה״כ: {total}")
    body_parts.append(DASH_URL)
    body = "\n".join(body_parts)

    # SMS to Lauren + Eli, fail-soft per recipient
    recipients = []
    for env_key, label in [("LAUREN_PHONE", "Lauren"), ("ELI_PHONE", "Eli")]:
        v = os.environ.get(env_key, "").strip()
        if v:
            recipients.append((label, v))
    for name, phone in recipients:
        try:
            sms.send_sms(phone, body)
            print(f"[digest] SMS sent to {name} ({phone}); body={len(body)} chars")
        except Exception as e:
            print(f"[digest] SMS to {name} failed: {e}")

    if any_change:
        with open(NOTES_PATH, "w") as f:
            json.dump(dict(sorted(notes.items())), f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("[digest] notes.json updated with digest scan records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
