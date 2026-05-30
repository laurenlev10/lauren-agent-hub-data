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
EVENTS_REPO = "laurenlev10/themakeupblowout-events"
TARGET_REPO = "laurenlev10/beauty-bash-usa"
TARGET_FILE = "src/lib/schedule-data.ts"
EVENTS_BASE = "https://events.themakeupblowout.com"
DRY_RUN  = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MONTHS = ["January","February","March","April","May","June","July","August",
          "September","October","November","December"]

def slugify(c): return re.sub(r"[^a-z0-9]+", "-", (c or "").lower()).strip("-")

def load_schedule():
    html = MASTER.read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\{.*?\});\n", html, re.S)
    if not m:
        raise SystemExit("FATAL: SCHEDULE map not found in docs/launch/index.html")
    return json.loads(m.group(1))

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
    land  = landing_slugs(pat)
    today = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    evs = []
    for yr, lst in sched.items():
        if not isinstance(lst, list):
            continue
        for e in lst:
            if isinstance(e, dict) and e.get("status") == "confirmed" \
               and datetime.date.fromisoformat(e["end_date"]) >= today:
                evs.append(e)
    evs.sort(key=lambda x: x["start_date"])
    rows = []
    for e in evs:
        cs = slugify(e["city"]); st = e["state"].lower(); year = e["end_date"][:4]
        lslug = f"{cs}-{st}-{year}"
        landing = f"{EVENTS_BASE}/events/{lslug}/" if lslug in land else None
        rec = forms.get(f"{lslug}-{e['start_date']}") or {}
        st_form = rec.get("form_url")
        signup = landing or st_form or None
        parts = [
            f'city: "{jstr(e["city"])}"', f'state: "{e["state"]}"',
            f'dates: "{fmt_dates(e["start_date"], e["end_date"])}"', 'days: "Fri – Sun"',
            f'venue: "{jstr(e.get("venue"))}"', f'address: "{jstr(e.get("address"))}"',
        ]
        if landing: parts.append(f'landingUrl: "{landing}"')
        if signup:  parts.append(f'signupUrl: "{signup}"')
        rows.append("  { " + ", ".join(parts) + " },")
    return rows

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
    rows = build_rows(pat)
    print(f"computed {len(rows)} confirmed upcoming events")
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
            _set_output(False); print("no change — homepage schedule already current."); return 0
        f.write_text(new, encoding="utf-8")
        subprocess.run(["git","-C",tmp,"add",TARGET_FILE], check=True)
        subprocess.run(["git","-C",tmp,"commit","-q","-m",
                        f"schedule: weekly sync — {len(rows)} events + per-event signup links [auto]"], check=True)
        subprocess.run(["git","-C",tmp,"push","-q","origin","HEAD:main"], check=True)
        _set_output(True); print(f"✓ pushed updated schedule ({len(rows)} events) -> Lovable will stage")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
