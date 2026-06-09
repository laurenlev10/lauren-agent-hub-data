#!/usr/bin/env python3
"""
verify_form_ids - consistency guard for per-event SimpleTexting sign-up forms.

Set 2026-06-09 (Lauren: "תעדכן קבצים ואת הזיכרון שלא יהיו בעיות בעתיד").
After an audit found 5 upcoming 2026 events whose FORM_ID chip was dark in the
launch dashboard even though their landing pages routed signups correctly,
this script locks the invariant down.

THE INVARIANT (form-routing audit, IRON RULE #19):
For every UPCOMING event that has a landing page, the SimpleTexting webFormId
must be IDENTICAL across all four places:
  1. landing index.html <form> embed     <- routes EN signups (SOURCE OF TRUTH)
  2. landing index-es.html <form> embed   <- routes ES signups
  3. event_form_ids.json events[<slug>-<start_date>]
  4. FORM_IDS map in launch/index.html [<evkey>]  (dashboard green chip)
CAMPAIGNS_RESULTS.webform_id is a soft signal.

The landing embed decides which SimpleTexting list a signup lands in. If the
dashboard/tracking files disagree with the embed, THAT is the bug.

env: EVENTS_REPO_DIR (local checkout of themakeupblowout-events; else raw GH),
     SIMPLETEXTING_TOKEN / LAUREN_PHONE (optional, for alert SMS).
Exit 0 normally (alerts via SMS); --strict exits 1 on mismatch.
"""
import os, re, sys, json, datetime, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCH = REPO / "docs" / "launch" / "index.html"
EFI = REPO / "docs" / "state" / "event_form_ids.json"
EVENTS_DIR = os.environ.get("EVENTS_REPO_DIR", "").strip()
RAW_BASE = "https://events.themakeupblowout.com/events"

def _const(src, name):
    m = re.search(r'const ' + name + r' = (\{.*?\});', src, re.S)
    return json.loads(m.group(1)) if m else {}

def slugify(c):
    return re.sub(r'[^a-z0-9]+', '-', c.lower()).strip('-')

def embed_id(text):
    if not text:
        return None
    m = (re.search(r'name="webFormId" value="([a-f0-9]{20,})"', text)
         or re.search(r'st-join-web-form-([a-f0-9]{20,})', text))
    return m.group(1) if m else None

def read_landing(slug, fn):
    if EVENTS_DIR:
        p = Path(EVENTS_DIR) / "docs" / "events" / slug / fn
        return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None
    try:
        with urllib.request.urlopen(f"{RAW_BASE}/{slug}/{fn}", timeout=20) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return None

def main():
    strict = "--strict" in sys.argv
    today = datetime.date.today().isoformat()
    src = LAUNCH.read_text(encoding="utf-8")
    SCHEDULE = _const(src, "SCHEDULE")
    FORM_IDS = _const(src, "FORM_IDS")
    LANDING = _const(src, "LANDING_PAGES")
    CAMP = _const(src, "CAMPAIGNS_RESULTS")
    efi = json.loads(EFI.read_text(encoding="utf-8")).get("events", {})

    events = []
    for yr, lst in SCHEDULE.items():
        if re.match(r'^20\d\d$', str(yr)) and isinstance(lst, list):
            events += [e for e in lst if isinstance(e, dict)]
    upcoming = sorted([e for e in events if e.get("start_date", "") >= today],
                      key=lambda e: e["start_date"])

    problems, checked = [], 0
    for e in upcoming:
        city, sd = e.get("city", ""), e.get("start_date", "")
        evkey = f"{slugify(city)}-{sd}"
        lp = LANDING.get(evkey)
        if not lp or not lp.get("url"):
            continue
        slug = lp["url"].rstrip("/").split("/")[-1]
        checked += 1
        en = embed_id(read_landing(slug, "index.html"))
        es = embed_id(read_landing(slug, "index-es.html"))
        fm = (FORM_IDS.get(evkey) or {}).get("form_id")
        ef = (efi.get(f"{slug}-{sd}") or {}).get("form_id")
        cw = (CAMP.get(evkey) or {}).get("webform_id")
        truth = en
        issues = []
        if not en: issues.append("landing-EN has NO form embed")
        if en and es and en != es: issues.append(f"EN/ES embed differ (EN={en} ES={es})")
        if not es: issues.append("landing-ES has NO form embed")
        if truth:
            if not fm: issues.append("dashboard FORM_IDS missing (dark chip)")
            elif fm != truth: issues.append(f"dashboard FORM_IDS wrong ({fm} != embed {truth})")
            if not ef: issues.append("event_form_ids.json missing")
            elif ef != truth: issues.append(f"event_form_ids wrong ({ef} != embed {truth})")
            if cw and cw != truth: issues.append(f"CAMPAIGNS_RESULTS.webform_id wrong ({cw} != embed {truth})")
        if issues:
            problems.append({"event": f"{city} {sd}", "slug": slug, "embed": truth, "issues": issues})

    print(f"verify_form_ids: checked {checked} upcoming events with landing pages "
          f"(of {len(upcoming)} upcoming); {len(problems)} with problems.")
    for p in problems:
        print(f"  X {p['event']} ({p['slug']}) embed={p['embed']}")
        for i in p["issues"]: print(f"      - {i}")
    if not problems:
        print("  OK all consistent - every landing embed matches dashboard + tracking.")

    if problems:
        body = ("⚠ אי-התאמה ב-FORM ID לאירועים:\n" +
                "\n".join(f"• {p['event']}: {p['issues'][0]}" for p in problems[:6]) +
                "\nבדקי: dashboard.themakeupblowout.com/launch")
        try:
            sys.path.insert(0, str(REPO / "scripts"))
            from lauren_sms import send_sms, LAUREN_PHONE
            if os.environ.get("SIMPLETEXTING_TOKEN"):
                send_sms(LAUREN_PHONE, body); print("  SMS sent to Lauren.")
        except Exception as ex:
            print(f"  SMS skipped/failed: {ex}")
    return 1 if (problems and strict) else 0

if __name__ == "__main__":
    raise SystemExit(main())
