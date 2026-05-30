#!/usr/bin/env python3
"""
sync_homepage_schedule.py  —  keeps the main site's tour schedule current.

Reads the master event list (SCHEDULE map in docs/launch/index.html), keeps every
CONFIRMED event whose end_date is today-or-later (Pacific Time), formats them as the
Lovable site's TourStop[] and rewrites src/lib/schedule-data.ts in the
laurenlev10/beauty-bash-usa repo. Pushing to that repo's main branch makes Lovable
auto-publish the updated homepage (themakeupblowout.com).

This is the successor to the manual weekly maintenance of events.themakeupblowout.com:
one Monday-morning job keeps the homepage tour list fresh, dropping events that have
passed and adding newly-confirmed ones — no manual edits.

Env:
  GH_PAT    PAT with push access to beauty-bash-usa (reuse secrets.EVENTS_REPO_PAT)
  DRY_RUN   "1"/"true" => compute + print the diff, do NOT clone/commit/push
"""
import os, re, json, subprocess, datetime, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

MASTER   = Path("docs/launch/index.html")
TARGET_REPO = "laurenlev10/beauty-bash-usa"
TARGET_FILE = "src/lib/schedule-data.ts"
DRY_RUN  = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MONTHS = ["January","February","March","April","May","June","July","August",
          "September","October","November","December"]

def load_schedule():
    html = MASTER.read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\{.*?\});\n", html, re.S)
    if not m:
        raise SystemExit("FATAL: SCHEDULE map not found in docs/launch/index.html")
    return json.loads(m.group(1))

def fmt_dates(sd, ed):
    s = datetime.date.fromisoformat(sd); e = datetime.date.fromisoformat(ed)
    return f"{MONTHS[s.month-1]} {s.day} – {MONTHS[e.month-1]} {e.day}, {e.year}"


def _set_output(changed: bool):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")

def build_rows():
    sched = load_schedule()
    today = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    evs = []
    for yr, lst in sched.items():
        if not isinstance(lst, list):
            continue
        for e in lst:
            if not isinstance(e, dict):
                continue
            if e.get("status") != "confirmed":
                continue
            if datetime.date.fromisoformat(e["end_date"]) < today:
                continue
            evs.append(e)
    evs.sort(key=lambda x: x["start_date"])
    rows = []
    for e in evs:
        city = e["city"].replace('"', "")
        venue = (e.get("venue") or "").replace('"', "'")
        addr = (e.get("address") or "").replace('"', "'")
        rows.append(
            f'  {{ city: "{city}", state: "{e["state"]}", '
            f'dates: "{fmt_dates(e["start_date"], e["end_date"])}", days: "Fri – Sun", '
            f'venue: "{venue}", address: "{addr}" }},'
        )
    return rows

def rewrite(ts_text, rows):
    new_body = "\n".join(rows)
    pat = re.compile(r'(const RAW: Omit<TourStop, "slug">\[\] = \[\n).*?(\n\];)', re.S)
    if not pat.search(ts_text):
        raise SystemExit("FATAL: RAW array not found in schedule-data.ts")
    return pat.sub(lambda m: m.group(1) + new_body + m.group(2), ts_text, count=1)

def main():
    rows = build_rows()
    print(f"computed {len(rows)} confirmed upcoming events")
    pat = os.environ.get("GH_PAT", "").strip()

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
        subprocess.run(["git", "-C", tmp, "config", "user.email", "lauren@noreply.github.com"], check=True)
        subprocess.run(["git", "-C", tmp, "config", "user.name", "Lauren (via sync-homepage-schedule)"], check=True)
        f = repo / TARGET_FILE
        old = f.read_text(encoding="utf-8")
        new = rewrite(old, rows)
        if new == old:
            _set_output(False); print("no change — homepage schedule already current.")
            return 0
        f.write_text(new, encoding="utf-8")
        subprocess.run(["git", "-C", tmp, "add", TARGET_FILE], check=True)
        msg = f"schedule: weekly sync — {len(rows)} confirmed upcoming events [auto]"
        subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", msg], check=True)
        subprocess.run(["git", "-C", tmp, "push", "-q", "origin", "HEAD:main"], check=True)
        _set_output(True); print(f"✓ pushed updated schedule ({len(rows)} events) -> Lovable will publish")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
