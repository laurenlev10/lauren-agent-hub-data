"""
pr_influencer_t30 — daily check: any event exactly ~30 days out (28-30 window)
that hasn't had its T-30 ping yet -> SMS Lauren the ready-to-run @pr-influencer
reminder + prefilled dashboard form link. State: docs/state/pr_influencer_t30.json.
"""

import json
import sys
import urllib.parse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms, LAUREN_PHONE

EVENTS = Path("docs/state/events_index.json")
STATE = Path("docs/state/pr_influencer_t30.json")
DASH = "https://laurenlev10.github.io/lauren-agent-hub-data/pr-influencer/"


def main() -> int:
    events = json.loads(EVENTS.read_text(encoding="utf-8"))
    if isinstance(events, dict):
        events = events.get("events", [])
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"notified": {}}
    today = date.today()
    fired = 0
    for e in events:
        sd = e.get("start_date")
        evkey = e.get("evkey")
        if not sd or not evkey or evkey in state["notified"]:
            continue
        days = (date.fromisoformat(sd) - today).days
        if not (28 <= days <= 30):
            continue
        city = f"{e.get('city','?')}, {e.get('state','')}".strip(", ")
        body = (f"@pr-influencer ⏰ עוד {days} יום: {city} ({sd}). "
                f"זמן להריץ פנייה למשפיעניות מקומיות — הטופס עם הפקודה בלינק הבא:")
        params = urllib.parse.urlencode({
            "city": city, "start_date": sd, "end_date": e.get("end_date", ""),
            "venue": e.get("venue", ""), "address": e.get("address", ""),
        })
        send_sms(LAUREN_PHONE, body[:275])
        send_sms(LAUREN_PHONE, f"{DASH}?{params}")
        state["notified"][evkey] = today.isoformat()
        fired += 1
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"T-30 pings sent: {fired}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
