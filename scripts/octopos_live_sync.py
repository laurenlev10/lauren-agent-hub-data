#!/usr/bin/env python3
"""
OCTOPOS live POS sync — runs every 30 minutes during event weekends (Fri/Sat/Sun).

Fetches today's sales from `/api/v1/get-sales-report` and writes
`docs/state/octopos_live.json` with totals matching the OCTOPOS dashboard widgets
("Total Payments", "Number of Transactions", "Average Transaction Value").

The launch dashboard reads this file and renders a row below the live hall photo
for the currently-live event.

🛑 IRON RULE #9 — `/get-sales-report` is the canonical sales endpoint. Returns
order-level rows with `total_payment_amount`. Do NOT use `/get-recount-data`
for any sales-derived metric (DR rows there are inventory adjustments, not POS).

Body shape (verified 2026-05-22): FLAT top-level fields, NOT wrapped in `data`
(differs from `/get-sales-by-vendor-product-report` which uses `data` wrapper).
Permission: report-total-sales.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "docs" / "state" / "octopos_live.json"
OCTO_BASE = "https://themakeup.octoretail.com"
OCTO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
LOCATION = {
    "label": "THE MAKEUP BLOWOUT SALE GROUP INC",
    "value": {
        "id": 2,
        "name": "THE MAKEUP BLOWOUT SALE GROUP INC",
        "time_zone": "America/Los_Angeles",
    },
}

# US state → IANA timezone. Mirrors STATE_TZ in scripts/insta_reel_scan.py.
# Used to compute "event-local 10 AM" (event opening) and convert to PT
# (OCTOPOS's aggregation TZ) so the sales window starts at doors-open,
# not at midnight PT. Lauren's directive 2026-05-24.
STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "HI": "Pacific/Honolulu", "ID": "America/Boise",
    "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago",
    "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/Detroit", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York",
    "NM": "America/Denver", "NY": "America/New_York", "NC": "America/New_York",
    "ND": "America/Chicago", "OH": "America/New_York", "OK": "America/Chicago",
    "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "UT": "America/Denver", "VT": "America/New_York",
    "VA": "America/New_York", "WA": "America/Los_Angeles", "WV": "America/New_York",
    "WI": "America/Chicago", "WY": "America/Denver", "DC": "America/New_York",
}

# Event doors open every day of the event at 10:00 local time (Fri/Sat/Sun).
EVENT_OPEN_HOUR_LOCAL = 10



def http_post(url, body, headers=None, timeout=25):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": OCTO_UA,
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=h, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {"raw": (e.read() or b"").decode(errors="replace")[:300]}


def octopos_jwt():
    email = os.environ.get("OCTOPOS_EMAIL")
    pw = os.environ.get("OCTOPOS_PASSWORD")
    if not email or not pw:
        for sess in Path("/sessions").glob("*/mnt/Claude/.claude/secrets/octopos_credentials.txt"):
            email, _, pw = sess.read_text().strip().partition(":")
            break
    if not email or not pw:
        sys.exit("ERR: missing OCTOPOS_EMAIL / OCTOPOS_PASSWORD env vars")
    code, resp = http_post(
        f"{OCTO_BASE}/api/v1/authenticate",
        {"email": email, "password": pw},
    )
    if code != 200 or not resp.get("flag"):
        sys.exit(f"ERR: OCTOPOS login failed HTTP {code}: {resp}")
    return resp["data"]["token"]


def fetch_sales_today(jwt, since_pt=None):
    """Fetch today's orders from /api/v1/get-sales-report, optionally starting
    at a specific PT-zoned datetime (e.g. event-doors-open) rather than midnight.

    Returns (list of order dicts, dateFrom_string_used). Pages through if needed.
    OCTOPOS aggregates by Pacific Time (location.time_zone), so all bounds are
    PT — matches what the OCTOPOS web dashboard shows.

    since_pt:
      - None or naive → defaults to "today 00:00:00 PT" (legacy behavior).
      - tz-aware (PT) → use that as the `dateFrom` boundary so we only count
        orders from event-doors-open onward. Required when Lauren wants the
        purple POS row to show "since doors open" not "since midnight."
    """
    la_now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today_mdy = la_now.strftime("%m/%d/%Y")

    if since_pt is None:
        date_from_str = f"{today_mdy} 00:00:00"
    else:
        # Ensure the since_pt is on today's PT date (defensive — the caller
        # should already ensure this via compute_event_open_in_pt).
        if since_pt.date() != la_now.date():
            print(f"WARN: since_pt date {since_pt.date()} != PT today {la_now.date()} — falling back to midnight.")
            date_from_str = f"{today_mdy} 00:00:00"
        else:
            date_from_str = since_pt.strftime("%m/%d/%Y %H:%M:%S")

    headers = {
        "Authorization": f"Bearer {jwt}",
        "Permission": "report-total-sales",
    }

    all_orders = []
    page = 1
    while True:
        body = {
            "location": LOCATION,
            "dateFrom": date_from_str,
            "dateTo":   f"{today_mdy} 23:59:59",
            "departments": [],
            "categories": [],
            "query": {
                "limit": 5000,
                "page": page,
                "order": "id",
                "order_type": "desc",
                "filter": "",
            },
        }
        code, resp = http_post(
            f"{OCTO_BASE}/api/v1/get-sales-report", body, headers
        )
        # OCTOPOS responds 404 with message "There are no Total Sales By Day."
        # BEFORE the first transaction of the day is logged. That's a valid
        # "no sales yet" state — NOT an error. Treat as empty orders so the
        # state file still gets written with today's date + zeros, and the
        # dashboard renders "$0 / 0 txns" until the first sale comes in.
        msg = (resp.get("message") or "") if isinstance(resp, dict) else ""
        if code == 404 and "no Total Sales" in msg:
            print(f"INFO: OCTOPOS reports no sales yet today ({today_mdy}) — returning zeros.")
            break
        if code != 200:
            sys.exit(f"ERR: get-sales-report HTTP {code}: {resp}")
        d = resp.get("data") or {}
        orders_block = d.get("orders") or {}
        # `orders` is "" (empty string) when no rows exist; treat as empty list.
        if isinstance(orders_block, str):
            break
        orders = orders_block.get("data") or []
        if not orders:
            break
        all_orders.extend(orders)
        last_page = orders_block.get("last_page") or 1
        if page >= last_page:
            break
        page += 1

    return all_orders, la_now, date_from_str


def compute_metrics(orders):
    """Compute the three dashboard widgets from the order list.

    Only orders with paid='Paid' are counted (matches OCTOPOS dashboard).
    """
    paid = [o for o in orders if (o.get("paid") or "").lower() == "paid"]
    total = sum(float(o.get("total_payment_amount") or 0) for o in paid)
    count = len(paid)
    avg = (total / count) if count else 0.0
    return {
        "total_payments": round(total, 2),
        "transaction_count": count,
        "avg_transaction": round(avg, 2),
        "paid_order_count": count,
        "all_order_count": len(orders),
    }




def compute_event_open_in_pt(state):
    """Compute today's event-doors-open moment (10:00 event-local) as a PT-zoned datetime.

    OCTOPOS aggregates by Pacific Time (location.time_zone), so query bounds must
    be in PT. We take the event city's IANA timezone (via STATE_TZ), build a
    naive datetime at 10:00 local for today's PT date, localize it to the event
    TZ, then convert to PT.

    Edge case — an event city's "today" can be one calendar day ahead of PT
    (NY event at 02:00 EDT = 23:00 PT previous day). We anchor on PT-today
    intentionally: the workflow only runs when PT-today matches the event row's
    Fri-Sun window, so PT-today === event-local-today for all events whose
    timezone is PT or east of PT (which covers all states in STATE_TZ). HI is
    west of PT but no HI events exist in the current schedule.

    Returns a tz-aware datetime in America/Los_Angeles.
    """
    if not state:
        # Fallback: 10:00 PT today (treat as PT event).
        la = datetime.now(ZoneInfo("America/Los_Angeles")).replace(
            hour=EVENT_OPEN_HOUR_LOCAL, minute=0, second=0, microsecond=0
        )
        return la
    tz_name = STATE_TZ.get(state, "America/Los_Angeles")
    ev_tz = ZoneInfo(tz_name)
    today_la = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    # Build the 10:00-event-local datetime *for today's PT date*. Using PT-today
    # as the date anchor is intentional — see docstring.
    naive = datetime(
        today_la.year, today_la.month, today_la.day,
        EVENT_OPEN_HOUR_LOCAL, 0, 0,
    )
    ev_open_local = naive.replace(tzinfo=ev_tz)
    return ev_open_local.astimezone(ZoneInfo("America/Los_Angeles"))


def find_live_event():
    """Find the event whose Fri-Sun window contains today's PT date.

    Parses SCHEDULE from docs/launch/index.html using a regex that pulls the
    minimal fields (city, state, start_date, end_date) per row.

    Returns (event_key, state) or (None, None).
    """
    import re
    html_path = REPO_ROOT / "docs" / "launch" / "index.html"
    if not html_path.exists():
        return (None, None)
    html = html_path.read_text(encoding="utf-8")
    today_la = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    pat = re.compile(
        r'\{[^{}]*?"city"\s*:\s*"([^"]+)"[^{}]*?"state"\s*:\s*"([^"]+)"[^{}]*?"start_date"\s*:\s*"(\d{4}-\d{2}-\d{2})"[^{}]*?"end_date"\s*:\s*"(\d{4}-\d{2}-\d{2})"'
    )
    for city, state, sd, ed in pat.findall(html):
        try:
            sd_d = datetime.fromisoformat(sd).date()
            ed_d = datetime.fromisoformat(ed).date()
        except Exception:
            continue
        if sd_d <= today_la <= ed_d:
            slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
            return (f"{slug}-{sd}", (state or "").upper())
    return (None, None)


def main():
    jwt = octopos_jwt()
    evkey, state_code = find_live_event()
    # Compute "event doors open" in PT — used as the dateFrom for the sales
    # query so we only count sales from doors-open onward, not from midnight.
    # Falls back gracefully when no event is live (state_code is None).
    event_open_pt = compute_event_open_in_pt(state_code) if state_code else None
    orders, la_now, date_from_str = fetch_sales_today(jwt, since_pt=event_open_pt)
    metrics = compute_metrics(orders)
    state = {
        "_updated_at": datetime.now(timezone.utc).isoformat(),
        "_about": (
            "Live POS metrics from OCTOPOS dashboard. Refreshed every 30 min "
            "during event weekends. Shown on /launch/ below the live hall photo. "
            "Window: event-doors-open (10:00 event-local) → now."
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "date_local": la_now.strftime("%Y-%m-%d"),
        "tz": "America/Los_Angeles",
        "event_key": evkey,
        "event_state": state_code,
        # Window bounds (PT). Consumers can show "since 10:00 event-local"
        # or compute elapsed hours. ISO 8601 with offset.
        "since_pt": event_open_pt.isoformat() if event_open_pt else None,
        "since_str": date_from_str,  # MM/DD/YYYY HH:MM:SS as passed to OCTOPOS
        **metrics,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    print(f"OK wrote {STATE_PATH}")
    print(f"  date_local: {state['date_local']}  evkey: {evkey}  state: {state_code}")
    print(f"  since:      {date_from_str} PT")
    print(f"  payments:   ${metrics['total_payments']:,.2f}")
    print(f"  txns:       {metrics['transaction_count']}")
    print(f"  avg:        ${metrics['avg_transaction']:,.2f}")


if __name__ == "__main__":
    main()
