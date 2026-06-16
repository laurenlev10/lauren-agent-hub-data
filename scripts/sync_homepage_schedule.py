#!/usr/bin/env python3
"""
sync_homepage_schedule.py — keeps the main site's tour schedule current.

Reads the master event list (SCHEDULE map in docs/launch/index.html), keeps every
CONFIRMED event whose end_date is today-or-later (Pacific Time), and rewrites
src/lib/schedule-data.ts in laurenlev10/beauty-bash-usa. Pushing makes Lovable
auto-stage (Lauren clicks "Update" to publish).

Per event it also resolves a best signup link, so the site's Register / "Get my
gift" buttons send people to the right place:
  landingUrl  = https://events.themakeupblowout.com/events/<city-state-year>/  (if that landing page exists)
  signupUrl   = landingUrl  OR  the event's SimpleTexting sign-up web-form URL
                (from docs/state/event_form_ids.json)  OR  None (site falls back
                to its internal /register/<city> page).

Env:
  GH_PAT    PAT with push access to beauty-bash-usa (reuse secrets.EVENTS_REPO_PAT)
  DRY_RUN   "1"/"true" => compute + print, do NOT clone/commit/push
"""
import os, re, json, subprocess, datetime, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

MASTER   = Path("docs/launch/index.html")
FORM_IDS = Path("docs/state/event_form_ids.json")
BUTTON_URLS = Path("docs/state/event_button_urls.json")
EVENTS_REPO = "laurenlev10/themakeupblowout-events"
TARGET_REPO = "laurenlev10/beauty-bash-usa"
TARGET_FILE = "src/lib/schedule-data.ts"
EVENTS_BASE = "https://events.themakeupblowout.com"
DRY_RUN  = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MONTHS = ["January","February","March","April","May","June","July","August",
          "September","October","November","December"]

# 🛑 Lauren 2026-06-01: do NOT list 2027 events on the homepage (tour list +
# "Choose your city" dropdown) until further notice. Temporary cap — raise/remove
# only on Lauren's explicit say-so. See memory.md 2026-06-01 (#3).
MAX_HOMEPAGE_YEAR = 2026

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

def _still_upcoming(e, now_utc):
    """Keep an event until 19:00 (event-local) on its end_date - i.e. 1h after its Sunday close."""
    ed = datetime.date.fromisoformat(e["end_date"])
    tz = ZoneInfo(STATE_TZ.get((e.get("state") or "").upper(), "America/Los_Angeles"))
    cutoff = datetime.datetime.combine(ed, datetime.time(ROLLOVER_HOUR, 0), tzinfo=tz)
    return now_utc.astimezone(tz) < cutoff


def slugify(c): return re.sub(r"[^a-z0-9]+", "-", (c or "").lower()).strip("-")

def load_schedule():
    html = MASTER.read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\{.*?\});\n", html, re.S)
    if not m:
        raise SystemExit("FATAL: SCHEDULE map not found in docs/launch/index.html")
    return json.loads(m.group(1))

def load_button_urls():
    try:
        return json.loads(BUTTON_URLS.read_text(encoding="utf-8")).get("urls", {})
    except Exception as e:
        print(f"warn: could not read event_button_urls.json: {e}")
        return {}

def load_form_ids():
    try:
        return json.loads(FORM_IDS.read_text(encoding="utf-8")).get("events", {})
    except Exception as e:
        print(f"warn: could not read event_form_ids.json: {e}")
        return {}

def landing_slugs(pat):
    """Set of slugs that have a published landing page (clone events repo, list docs/events/)."""
    if not pat:
        return set()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git","clone","--depth","1","-q",
                            f"https://x-access-token:{pat}@github.com/{EVENTS_REPO}.git", tmp],
                           check=True)
            d = Path(tmp) / "docs" / "events"
            return {p.name for p in d.iterdir()} if d.exists() else set()
    except Exception as e:
        print(f"warn: could not list landing pages: {e}")
        return set()

def fmt_dates(sd, ed):
    s = datetime.date.fromisoformat(sd); e = datetime.date.fromisoformat(ed)
    return f"{MONTHS[s.month-1]} {s.day} – {MONTHS[e.month-1]} {e.day}, {e.year}"

def jstr(s):  # JS double-quoted string, sanitised
    return (s or "").replace('"', "'")

def build_rows(pat):
    sched = load_schedule()
    forms = load_form_ids()
    overrides = load_button_urls()
    land  = landing_slugs(pat)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    evs = []
    for yr, lst in sched.items():
        if not isinstance(lst, list):
            continue
        for e in lst:
            if isinstance(e, dict) and e.get("status") == "confirmed" \
               and _still_upcoming(e, now_utc) \
               and datetime.date.fromisoformat(e["end_date"]).year <= MAX_HOMEPAGE_YEAR:
                evs.append(e)
    evs.sort(key=lambda x: x["start_date"])
    rows = []
    for e in evs:
        cs = slugify(e["city"]); st = e["state"].lower(); year = e["end_date"][:4]
        lslug = f"{cs}-{st}-{year}"
        landing = f"{EVENTS_BASE}/events/{lslug}/" if lslug in land else None
        rec = forms.get(f"{lslug}-{e['start_date']}") or {}
        st_form = rec.get("form_url")
        signup = overrides.get(lslug) or landing or st_form or None
        parts = [
            f'city: "{jstr(e["city"])}"', f'state: "{e["state"]}"',
            f'dates: "{fmt_dates(e["start_date"], e["end_date"])}"', 'days: "Fri – Sun"',
            f'venue: "{jstr(e.get("venue"))}"', f'address: "{jstr(e.get("address"))}"',
        ]
        if landing: parts.append(f'landingUrl: "{landing}"')
        if signup:  parts.append(f'signupUrl: "{signup}"')
        rows.append("  { " + ", ".join(parts) + " },")
    return rows, evs

def rewrite(ts_text, rows):
    new_body = "\n".join(rows)
    pat = re.compile(r'(const RAW: Omit<TourStop, "slug">\[\] = \[\n).*?(\n\];)', re.S)
    if not pat.search(ts_text):
        raise SystemExit("FATAL: RAW array not found in schedule-data.ts")
    return pat.sub(lambda m: m.group(1) + new_body + m.group(2), ts_text, count=1)

def _set_output(changed: bool):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")

def main():
    pat = os.environ.get("GH_PAT", "").strip()
    rows, evs = build_rows(pat)
    print(f"computed {len(rows)} confirmed upcoming events")

    def _record(extra):
        """Record a bullet summary for the /scheduled/ dashboard (never breaks the job)."""
        try:
            from run_summary import record
            bl = [f"סונכרנו {len(rows)} אירועים מאושרים ללוח דף הבית"]
            if evs:
                e0 = evs[0]
                bl.append(f"האירוע הקרוב: {e0['city']}, {e0['state']} · {fmt_dates(e0['start_date'], e0['end_date'])}")
            if extra:
                bl.append(extra)
            record("sync-homepage-schedule", bl, status="ok")
        except Exception as e:
            print(f"[summary] skipped: {e}")
    if DRY_RUN or not pat:
        _set_output(False)
        print("DRY_RUN (or no GH_PAT) — not pushing. Preview:")
        for r in rows[:3] + (["  ..."] if len(rows) > 5 else []) + rows[-2:]:
            print(r)
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://x-access-token:{pat}@github.com/{TARGET_REPO}.git"
        subprocess.run(["git", "clone", "--depth", "1", "-q", url, tmp], check=True)
        repo = Path(tmp)
        subprocess.run(["git","-C",tmp,"config","user.email","lauren@noreply.github.com"], check=True)
        subprocess.run(["git","-C",tmp,"config","user.name","Lauren (via sync-homepage-schedule)"], check=True)
        f = repo / TARGET_FILE
        old = f.read_text(encoding="utf-8"); new = rewrite(old, rows)
        if new == old:
            _set_output(False); print("no change — homepage schedule already current.")
            _record("הלוח כבר מעודכן — אין שינוי בריצה זו")
            return 0
        f.write_text(new, encoding="utf-8")
        subprocess.run(["git","-C",tmp,"add",TARGET_FILE], check=True)
        subprocess.run(["git","-C",tmp,"commit","-q","-m",
                        f"schedule: weekly sync — {len(rows)} events + per-event signup links [auto]"], check=True)
        subprocess.run(["git","-C",tmp,"push","-q","origin","HEAD:main"], check=True)
        _set_output(True); print(f"✓ pushed updated schedule ({len(rows)} events) -> Lovable will stage")
        _record("נדחף עדכון ל-beauty-bash-usa — מתפרסם דרך Lovable")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
