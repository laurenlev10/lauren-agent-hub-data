#!/usr/bin/env python3
"""venue_email_digest.py — dated correspondence timeline per event venue contact.

Lauren 2026-06-08: "ריכוז של כל ההתכתבויות והנקודות החשובות בינינו לבין איש הקשר
מהאימייל — סיכום בתאריכים". For every event in [today-45d .. today+400d] that has a
contact email (events.<evkey>.contact in venue_payments.json — Lauren edits it on the
contract dashboard), search Eli's Gmail for the thread with that contact and write an
agent-owned timeline:

    events.<evkey>.email_log        [{date, from, subject, snippet}]  (newest first, max 25)
    events.<evkey>.email_log_synced_at

Fallback when no contact email: search by venue name words. Read-only Gmail.
Runs daily inside qb-untagged-refresh.yml. MERGE-on-write (IRON RULE #18) —
browser-owned fields are never touched.
"""
from __future__ import annotations
import datetime as dt, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import qb_email_match as GM   # gmail_token / search / get_msg / headers_of

STATE = ROOT / "docs/state/venue_payments.json"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def msg_meta(tok, mid):
    m = GM._api(tok, f"messages/{mid}", {"format": "metadata",
                                         "metadataHeaders": ["From", "Subject", "Date"]})
    h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers", [])}
    ts = int(m.get("internalDate", "0")) / 1000
    return {"date": dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
            "from": h.get("from", "")[:80], "subject": h.get("subject", "")[:120],
            "snippet": (m.get("snippet") or "")[:200]}


def main():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    events = state.get("events", {})
    today = dt.date.today()
    targets = []
    for k, rec in events.items():
        try:
            sd = dt.date.fromisoformat(rec.get("start_date") or "")
        except ValueError:
            continue
        if -45 <= (sd - today).days <= 400:
            targets.append((k, rec))
    if not targets:
        print("no events in window"); return 0
    tok = GM.gmail_token()
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    synced = 0
    for k, rec in sorted(targets):
        emails = EMAIL_RE.findall(rec.get("contact") or "")
        if emails:
            q = "(" + " OR ".join(f"from:{e} OR to:{e}" for e in emails[:3]) + ")"
        else:
            venue = rec.get("venue") or ""
            words = [w for w in re.split(r"[^A-Za-z0-9]+", venue)
                     if len(w) >= 5 and w.lower() not in ("hotel", "suites", "center", "centre")]
            if not words:
                continue
            q = '"' + " ".join(words[:3]) + '"'
        q += " newer_than:540d"
        try:
            ids = GM.search(tok, q, n=25)
            log = [msg_meta(tok, m["id"]) for m in ids]
            log.sort(key=lambda x: x["date"], reverse=True)
            rec["email_log"] = log[:25]
            rec["email_log_synced_at"] = now
            synced += 1
            print(f"  ✓ {k}: {len(log)} messages ({'contact' if emails else 'venue-name'})")
        except Exception as ex:
            print(f"  ✗ {k}: {str(ex)[:100]}")
    state["_updated_at"] = now
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"email digest: {synced}/{len(targets)} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
