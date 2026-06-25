#!/usr/bin/env python3
"""
lauren_signups_sms — daily SimpleTexting signups digest.

Lauren's request (2026-06-25): know how many people signed up to each
event's SimpleTexting list EACH DAY. The 6-hourly registrations workflow
already maintains LIST_STATS inside docs/launch/index.html, but no SMS ever
reported it. This sends ONE concise Hebrew digest/day: every UPCOMING
event's active SMS-list count + how many were added today, sorted by event
date, plus a grand total of new signups across the tour.

Data flow:
  * Reads SETUPS (per-event smslist.list_id/name) + LIST_STATS (history for
    the daily delta) from docs/launch/index.html.
  * Pulls each upcoming event's live count from SimpleTexting
    (GET /v2/api/contact-lists/<id>) — same endpoint registrations-6h uses.
  * Today's delta = live active - most recent prior-day active in history.

Recipients: Lauren + Eli (content digest, IRON RULE #4.C).
Env: SIMPLETEXTING_TOKEN (required), LAUREN_PHONE (default 4243547625), ELI_PHONE.
Flags: --dry  compose + print, do not send.
"""

import os, re, sys, json, datetime, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from lauren_sms import send_sms  # noqa: E402

LAUNCH_HTML = REPO / "docs" / "launch" / "index.html"
ST_TOKEN = os.environ.get("SIMPLETEXTING_TOKEN", "").strip()


def _extract_map(src, name):
    m = re.search(r'(?:const|let|var)\s+' + name + r'\s*=\s*(\{.*?\});', src, re.S)
    return json.loads(m.group(1)) if m else {}


def _evkey_date(evkey):
    m = re.search(r'(\d{4}-\d{2}-\d{2})$', evkey)
    if not m:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _city_label(evkey):
    return re.sub(r'-\d{4}-\d{2}-\d{2}$', '', evkey).replace('-', ' ').title()


def st_get_counts(list_id):
    req = urllib.request.Request(
        f"https://app2.simpletexting.com/v2/api/contact-lists/{list_id}",
        headers={"Authorization": f"Bearer {ST_TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    return int(d.get("totalContactsCount", 0) or 0), int(d.get("activeContactsCount", 0) or 0)


def prior_active(hist, today):
    for h in reversed(hist or []):
        if h.get("date") and h["date"] < today:
            return h.get("active", h.get("total"))
    return None


def build_digest():
    src = LAUNCH_HTML.read_text(encoding="utf-8")
    setups = _extract_map(src, "SETUPS")
    list_stats = _extract_map(src, "LIST_STATS")
    today = datetime.date.today()
    today_s = today.isoformat()
    rows = []
    for evkey, s in setups.items():
        sms = (s or {}).get("smslist") or {}
        lid = sms.get("list_id")
        if not lid:
            continue
        edate = _evkey_date(evkey)
        if not edate or edate < today:
            continue
        try:
            total, active = st_get_counts(lid)
        except Exception as e:
            print(f"  ST err {evkey}: {e}")
            continue
        prev = prior_active(list_stats.get(evkey, {}).get("history", []), today_s)
        delta = (active - prev) if prev is not None else None
        rows.append((edate, _city_label(evkey), active, delta))
    rows.sort(key=lambda r: r[0])
    return rows, today


def compose_sms(rows, today):
    total_new = sum(r[3] for r in rows if r[3])
    lines = [f"📲 הרשמות SMS — {today.strftime('%d/%m')}"]
    if total_new:
        lines.append(f"➕ {total_new} חדשים היום בסך הכל")
    lines.append("")
    for edate, city, active, delta in rows:
        if delta is None:
            d = ""
        elif delta > 0:
            d = f" (+{delta})"
        elif delta < 0:
            d = f" ({delta})"
        else:
            d = " (±0)"
        lines.append(f"{city}: {active}{d}")
    lines.append("")
    lines.append("📊 dashboard.themakeupblowout.com/launch/")
    return "\n".join(lines)


def main():
    if not ST_TOKEN:
        print("[signups_sms] SIMPLETEXTING_TOKEN missing; cannot run.")
        return 1
    dry = "--dry" in sys.argv
    rows, today = build_digest()
    if not rows:
        print("[signups_sms] no upcoming events with a list; nothing to send.")
        return 0
    body = compose_sms(rows, today)
    print("--- SMS body ---"); print(body); print("----------------")
    recipients = []
    for env_key, label in [("LAUREN_PHONE", "Lauren"), ("ELI_PHONE", "Eli")]:
        v = os.environ.get(env_key, "").strip()
        if not v and env_key == "LAUREN_PHONE":
            v = "4243547625"
        if v:
            recipients.append((label, v))
    if dry:
        print(f"[dry] would send to: {[r[0] for r in recipients]}")
        return 0
    for name, phone in recipients:
        try:
            send_sms(phone, body)
        except Exception as e:
            print(f"  SMS to {name} failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
