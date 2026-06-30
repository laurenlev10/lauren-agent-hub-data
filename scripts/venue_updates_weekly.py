#!/usr/bin/env python3
"""venue_updates_weekly.py — weekly "what's new from the venues" SMS digest.

Lauren 2026-06-30: she wants a weekly heads-up of any NEW venue correspondence /
changes on UPCOMING events, so she doesn't have to open every card to find what moved.

Reads docs/state/venue_payments.json (email_log refreshed daily by venue_email_digest.py
+ summaries by venue_relationship_sync.py) and docs/state/events_index.json. For every
upcoming event whose most-recent venue email landed in the last 7 days, it lines up a
short Hebrew bullet (event, days-to-go, latest subject/snippet) and texts Lauren + Eli
(fail-soft per recipient, IRON RULE #4.C). Read-only — never writes state.
"""
from __future__ import annotations
import datetime as dt, html, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lauren_sms

VP = json.loads((ROOT / "docs/state/venue_payments.json").read_text(encoding="utf-8"))
EV = json.loads((ROOT / "docs/state/events_index.json").read_text(encoding="utf-8")).get("events", [])
DASH = "https://dashboard.themakeupblowout.com/contract"

def parse_dt(x):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try: return dt.datetime.strptime((x or "")[:16 if " " in fmt else 10], fmt)
        except Exception: pass
    return None

def main():
    today = dt.date.today()
    events = VP.get("events", {})
    idx = {e["evkey"]: e for e in EV}
    rows = []
    for evkey, e in idx.items():
        try:
            sd = dt.date.fromisoformat(e["start_date"])
        except Exception:
            continue
        days = (sd - today).days
        if days < -2 or days > 200:          # upcoming window
            continue
        rec = events.get(evkey, {})
        log = rec.get("email_log") or []
        if not log:
            continue
        last = parse_dt(log[0].get("date"))
        if not last or (dt.datetime.utcnow() - last).days > 7:   # only fresh (<=7d)
            continue
        subj = html.unescape((log[0].get("subject") or log[0].get("snippet") or "").strip())
        rows.append((days, e.get("class_name", evkey), last.strftime("%m-%d"), subj[:70]))
    rows.sort()
    if rows:
        body = "🏛 עדכוני אולמות — השבוע התקבלה התכתבות חדשה ב-%d אירועים:\n" % len(rows)
        for days, name, d, subj in rows[:12]:
            body += f"\n• {name} (עוד {days} ימים) · {d}: {subj}"
        body += f"\n\nפרטים מלאים בדשבורד: {DASH}"
    else:
        body = "🏛 עדכוני אולמות — אין התכתבות חדשה מהאולמות השבוע (לאירועים הקרובים). " + DASH

    recipients = []
    for env_key, label in [("LAUREN_PHONE", "Lauren"), ("ELI_PHONE", "Eli")]:
        v = (os.environ.get(env_key) or "").strip()
        if v: recipients.append((label, v))
    if not recipients:
        recipients = [("Lauren", "4243547625")]
    for name, phone in recipients:
        try:
            lauren_sms.send_sms(phone, body)
            print(f"  sent to {name}")
        except Exception as e:
            print(f"  SMS to {name} failed: {e}")
    print(f"venue_updates_weekly: {len(rows)} fresh events")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
