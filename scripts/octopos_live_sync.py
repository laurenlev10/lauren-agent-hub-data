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


def fetch_sales_today(jwt):
    """Fetch all of today's orders from /api/v1/get-sales-report.

    Returns list of order dicts. Pages through if needed.
    OCTOPOS aggregates by Pacific Time (location.time_zone), so "today"
    means the current PT date — matches what the OCTOPOS web dashboard shows.
    """
    la_now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today_mdy = la_now.strftime("%m/%d/%Y")
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Permission": "report-total-sales",
    }

    all_orders = []
    page = 1
    while True:
        body = {
            "location": LOCATION,
            "dateFrom": f"{today_mdy} 00:00:00",
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

    return all_orders, la_now


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


def find_live_event():
    """Find the event whose Fri-Sun window contains today's PT date.

    Parses SCHEDULE from docs/launch/index.html using a regex that pulls the
    minimal fields (city, start_date, end_date) per row.
    """
    import re
    html_path = REPO_ROOT / "docs" / "launch" / "index.html"
    if not html_path.exists():
        return None
    html = html_path.read_text(encoding="utf-8")
    today_la = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    pat = re.compile(
        r'\{[^{}]*?"city"\s*:\s*"([^"]+)"[^{}]*?"start_date"\s*:\s*"(\d{4}-\d{2}-\d{2})"[^{}]*?"end_date"\s*:\s*"(\d{4}-\d{2}-\d{2})"'
    )
    for city, sd, ed in pat.findall(html):
        try:
            sd_d = datetime.fromisoformat(sd).date()
            ed_d = datetime.fromisoformat(ed).date()
        except Exception:
            continue
        if sd_d <= today_la <= ed_d:
            slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
            return f"{slug}-{sd}"
    return None


def main():
    jwt = octopos_jwt()
    orders, la_now = fetch_sales_today(jwt)
    metrics = compute_metrics(orders)
    evkey = find_live_event()
    state = {
        "_updated_at": datetime.now(timezone.utc).isoformat(),
        "_about": (
            "Live POS metrics from OCTOPOS dashboard. Refreshed every 30 min "
            "during event weekends. Shown on /launch/ below the live hall photo."
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "date_local": la_now.strftime("%Y-%m-%d"),
        "tz": "America/Los_Angeles",
        "event_key": evkey,
        **metrics,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    print(f"OK wrote {STATE_PATH}")
    print(f"  date_local: {state['date_local']}  evkey: {evkey}")
    print(f"  payments:   ${metrics['total_payments']:,.2f}")
    print(f"  txns:       {metrics['transaction_count']}")
    print(f"  avg:        ${metrics['avg_transaction']:,.2f}")


if __name__ == "__main__":
    main()
