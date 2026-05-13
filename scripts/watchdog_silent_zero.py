"""
watchdog_silent_zero — final-step watchdog for marketing-stats.yml.

Catches the silent-zero failure class: aggregator runs successfully and
writes valid JSON, but the JSON contains zeros because slug-matching
broke or API field names changed. Workflow exit code is 0, no failure
SMS fires, dashboards quietly ship wrong numbers.

This watchdog cross-checks the aggregator's output against a direct
account-level query to Meta. If the gap is huge, it SMSes Lauren.

Triggers (any one fires the alert):
  1. Aggregator's total Meta spend (sum across events) is 0 but the
     direct account /insights query shows spend > $10 in the same window.
  2. Aggregator's total < 10% of direct query (i.e. matched <10% of
     real campaigns — strong sign the slug matcher broke).

Set 2026-05-13 PM after the @stats silent-zero incident. See memory.md
change-log "2026-05-13 PM — @stats was lying" for context.

Exit codes:
  0 — all good (or watchdog couldn't reach Meta — fail-soft)
  0 — alert fired (still 0 so workflow stays green; SMS already sent)

Reads env:
  META_PAGE_TOKEN, META_AD_ACCOUNT_ID — for the direct query
  SIMPLETEXTING_TOKEN, LAUREN_PHONE — for the alert
"""

import json, os, sys, urllib.request, urllib.parse, urllib.error, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parent.parent
CONV_HISTORY = REPO / "docs" / "state" / "conversion_history.json"

def fetch_account_level_spend(start_date: str, end_date: str) -> float:
    """Direct query to Meta — total account spend in window, no slug matching."""
    token = os.environ.get("META_PAGE_TOKEN")
    ad_acct = os.environ.get("META_AD_ACCOUNT_ID")
    if not token or not ad_acct:
        print("[watchdog] Meta creds missing — skipping cross-check")
        return -1.0
    url = f"https://graph.facebook.com/v25.0/{ad_acct}/insights"
    params = {
        "access_token": token,
        "fields": "spend",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "level": "account",
    }
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        rows = d.get("data", [])
        return float(rows[0].get("spend", 0)) if rows else 0.0
    except Exception as e:
        print(f"[watchdog] direct query failed: {e}")
        return -1.0


def main() -> int:
    if not CONV_HISTORY.exists():
        print(f"[watchdog] {CONV_HISTORY} not found — skipping")
        return 0
    data = json.loads(CONV_HISTORY.read_text())
    events = data.get("events", {})

    aggregator_total = 0.0
    nonzero_events = 0
    for slug, ev in events.items():
        m = ev.get("meta", {})
        s = float(m.get("spend", 0) or 0)
        aggregator_total += s
        if s > 0: nonzero_events += 1

    # Same 30-day window the aggregator uses by default
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=30)).isoformat()
    end = today.isoformat()
    direct_total = fetch_account_level_spend(start, end)
    if direct_total < 0:
        print("[watchdog] cross-check unavailable; cannot evaluate. Exiting clean.")
        return 0

    print(f"[watchdog] aggregator total: ${aggregator_total:.2f}")
    print(f"[watchdog] direct API total: ${direct_total:.2f}")
    print(f"[watchdog] events with spend>0: {nonzero_events}/{len(events)}")

    # Decide if we have a problem.
    reason = None
    if direct_total > 10 and aggregator_total < 0.01:
        reason = (f"אגרגטור החזיר 0 אבל ב-Meta יש ${direct_total:.0f} השבועיים האחרונים — "
                  f"כנראה slug matcher שבור או שדה API השתנה")
    elif direct_total > 50 and aggregator_total < direct_total * 0.1:
        reason = (f"אגרגטור החזיר ${aggregator_total:.0f} אבל ב-Meta יש ${direct_total:.0f} "
                  f"(<10% נתפס) — כנראה slug matcher לא תפס קמפיינים")

    if not reason:
        print("[watchdog] OK — aggregator within tolerance of direct query")
        return 0

    # Alert
    print(f"[watchdog] ⚠ ALERT: {reason}")
    try:
        from lauren_sms import send_sms, LAUREN_PHONE
    except Exception as e:
        print(f"[watchdog] couldn't import lauren_sms: {e}")
        return 0
    if not LAUREN_PHONE:
        print("[watchdog] LAUREN_PHONE not set — can't SMS")
        return 0
    if not os.environ.get("SIMPLETEXTING_TOKEN"):
        print("[watchdog] SIMPLETEXTING_TOKEN missing — can't SMS")
        return 0

    body = "\n".join([
        "⚠ @stats watchdog — שתיקה מחשידה",
        "",
        reason,
        "",
        f"aggregator: ${aggregator_total:.2f}",
        f"meta API direct: ${direct_total:.2f}",
        "",
        "פתחי את ה-log:",
        os.environ.get("RUN_URL", "https://github.com/laurenlev10/lauren-agent-hub-data/actions"),
    ])
    try:
        resp = send_sms(LAUREN_PHONE, body)
        print(f"[watchdog] SMS sent: id={resp.get('id', '?')}")
    except Exception as e:
        print(f"[watchdog] SMS failed: {e}")

    # Return 0 — the workflow itself isn't failed; we already SMSed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
