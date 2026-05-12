#!/usr/bin/env python3
"""
weekly_event_check.py

Runs every Monday 09:00 PT. Checks every event in the next 8 weeks and SMSes
Lauren ONLY IF something needs her attention. Specifically:

For each upcoming event within the next 4 weeks (the "action window"):
  - Does it have a per-event landing page on events.themakeupblowout.com?
    (Source of truth: LANDING_PAGES map in launch/index.html, fallback HEAD probe)
  - Does it have a hero.png in the events repo?
    (HEAD probe to events.themakeupblowout.com/_assets/events/<slug>/hero.png)
  - Does it have an insta_reel_url in launch/notes.json?

If anything is missing on a <4-week-out event → ONE SMS to Lauren listing all
gaps, with a link to the launch dashboard.

Silent-success: if nothing is missing, no SMS sent (just logs to stdout).

Author: 2026-05-11. Lauren approved scope: "אני אדאג לייצר דפי נחיתה לכל
האירועים לפני שיגיע ה-4 שבועות לפני" — this is the safety net.
"""

import datetime, json, os, re, sys, urllib.request
from pathlib import Path

ROOT       = Path(".")
LAUNCH     = ROOT / "docs/launch/index.html"
NOTES      = ROOT / "docs/launch/notes.json"
EVENTS_BASE = "https://events.themakeupblowout.com"
DASHBOARD_URL = "https://laurenlev10.github.io/lauren-agent-hub-data/launch/"
ACTION_WINDOW_DAYS = 28   # 4 weeks
LOOKAHEAD_DAYS    = 56   # only consider events in next 8 weeks

# Allow dry-run via env var (workflow can flip this for testing without sending)
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


def slug_of(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def parse_map(html: str, name: str) -> dict:
    m = re.search(rf"const {name} = (\{{[^;]+\}});", html, re.S)
    return json.loads(m.group(1)) if m else {}


def today_pt() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=7)).date()


def head_check(url: str, timeout: int = 8) -> bool:
    """Return True if HEAD request returns 2xx."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def main() -> int:
    if not LAUNCH.exists():
        print(f"ERR: {LAUNCH} not found", file=sys.stderr)
        return 1

    html         = LAUNCH.read_text(encoding="utf-8")
    SCHEDULE     = parse_map(html, "SCHEDULE")
    LANDING_PAGES = parse_map(html, "LANDING_PAGES")
    notes        = json.loads(NOTES.read_text(encoding="utf-8")) if NOTES.exists() else {}
    today        = today_pt()
    cutoff       = today + datetime.timedelta(days=LOOKAHEAD_DAYS)
    action_cut   = today + datetime.timedelta(days=ACTION_WINDOW_DAYS)

    # Flatten all events from year-keyed lists
    events = []
    for year, lst in SCHEDULE.items():
        if not (isinstance(lst, list) and year.isdigit()):
            continue
        for ev in lst:
            sd = ev.get("start_date")
            if not sd:
                continue
            try:
                sd_d = datetime.date.fromisoformat(sd)
            except Exception:
                continue
            if today <= sd_d <= cutoff:
                events.append({**ev, "_sd": sd_d})
    events.sort(key=lambda e: e["_sd"])

    print(f"today={today} action_window=until {action_cut} lookahead=until {cutoff}")
    print(f"events in scope: {len(events)}")

    issues = []  # list of dicts: {city, state, sd, days_out, missing: [...]}
    for ev in events:
        city_slug  = slug_of(ev["city"])
        state_lc   = (ev.get("state") or "").lower()
        year_str   = ev["start_date"][:4]
        evkey_short = f"{city_slug}-{ev['start_date']}"             # SETUPS / LANDING_PAGES key
        evkey_full  = f"{city_slug}-{state_lc}-{year_str}"          # /events/<slug>/ URL
        days_out   = (ev["_sd"] - today).days

        # Skip checks for events outside action window — only flag those <4 weeks out
        if days_out > ACTION_WINDOW_DAYS:
            print(f"  [{days_out:3d}d] {ev['city']}, {ev['state']} ({ev['start_date']}) — OUT OF WINDOW (skipping)")
            continue

        # Check 1: landing page registered in LANDING_PAGES?
        in_map  = evkey_short in LANDING_PAGES
        # Fallback: HEAD probe the actual URL on the events site
        url     = f"{EVENTS_BASE}/events/{evkey_full}/"
        live    = in_map or head_check(url)

        # Check 2: hero.png exists?
        hero_url = f"{EVENTS_BASE}/_assets/events/{evkey_full}/hero.png"
        hero    = head_check(hero_url)

        # Check 3: insta_reel_url set in notes?
        ig      = bool((notes.get(evkey_short) or {}).get("insta_reel_url"))

        missing = []
        if not live: missing.append("דף נחיתה")
        if not hero: missing.append("hero.png")
        if not ig:   missing.append("IG Reel URL")

        status = "OK" if not missing else f"MISSING: {', '.join(missing)}"
        print(f"  [{days_out:3d}d] {ev['city']}, {ev['state']} ({ev['start_date']}) — {status}")

        if missing:
            issues.append({
                "city":     ev["city"],
                "state":    ev.get("state", ""),
                "sd":       ev["start_date"],
                "days_out": days_out,
                "missing":  missing,
            })

    if not issues:
        print("\n✓ all events in the 4-week window are fully prepared. No SMS sent.")
        return 0

    # Build the SMS body
    lines = ["@landing יש משימות לטפל השבוע:"]
    for it in issues:
        weeks_out = round(it["days_out"] / 7, 1)
        lines.append(f"• {it['city']}, {it['state']} ({it['sd']}, בעוד {weeks_out} שבועות) — חסר: {', '.join(it['missing'])}")
    lines.append("")
    lines.append("תבני: " + DASHBOARD_URL)
    body = "\n".join(lines)

    print("\n--- SMS body ---")
    print(body)
    print("--- end SMS ---\n")

    # Send to Lauren only (per Lauren's directive 2026-05-11)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from lauren_sms import send_sms, LAUREN_PHONE
    except ImportError as e:
        print(f"ERR: cannot import lauren_sms: {e}", file=sys.stderr)
        return 1

    try:
        result = send_sms(LAUREN_PHONE, body, dry_run=DRY_RUN)
        print(f"SMS sent: {result}")
    except Exception as e:
        print(f"ERR sending SMS: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
