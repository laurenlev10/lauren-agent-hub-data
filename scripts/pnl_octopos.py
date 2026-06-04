#!/usr/bin/env python3
"""pnl_octopos.py — OCTOPOS sales source for the automated event P&L.

Source A of 5 for the post-event summary (see event_summary_BUILD_BRIEF.md).
Pulls top-line sales totals + top products for an event date window, straight
from the OCTOPOS API. Replaces the manual OCTOPOS screenshots / Google Sheet
entry the @mbs-event-summary agent used to do by hand.

Verified shapes live in Scheduled/NEW/shared/OCTOPOS_AUTH.md:
  - /api/v1/authenticate                         -> JWT (14-day)
  - /api/v1/get-sales-report  (FLAT body)        -> order-level totals
  - /api/v1/get-attributes-for-sales-by-vendor-report (GET) -> live vendor list
  - /api/v1/get-sales-by-vendor-product-report (data-wrapped) -> per-product units_sold

Public API:
    from scripts.pnl_octopos import fetch_octopos_pnl
    data = fetch_octopos_pnl("2026-05-22", "2026-05-24")
    # -> {gross, net, tax, transactions, avg_ticket,
    #     payment_breakdown{}, top_products[], generated_at, window}

Credentials: env OCTOPOS_EMAIL / OCTOPOS_PASSWORD (GitHub Actions), else falls
back to .claude/secrets/octopos_credentials.txt (email:password) for local
Cowork runs.

🛑 Cloudflare blocks Python-urllib — every call MUST send a browser User-Agent.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys
import urllib.error, urllib.request
from pathlib import Path

OCTO_BASE = "https://themakeup.octoretail.com"
OCTO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
LOCATION = {"label": "THE MAKEUP BLOWOUT SALE GROUP INC",
            "value": {"id": 2, "name": "THE MAKEUP BLOWOUT SALE GROUP INC",
                      "time_zone": "America/Los_Angeles"}}

# Always-top-sellers to drop from the "best sellers" insight (IRON RULE — these
# are structural top sellers at every event, so they carry no signal).
ALWAYS_TOP_RE = ("hair clip", "hair clips", "hair pin", "bobby pin", "mirror",
                 "scrunchie", "hair tie", "hair band", "headband")


def _http(url, body, headers, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=("POST" if body is not None else "GET"),
        headers={**headers, "Accept": "application/json", "User-Agent": OCTO_UA,
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}


def _credentials():
    email = os.environ.get("OCTOPOS_EMAIL"); pw = os.environ.get("OCTOPOS_PASSWORD")
    if email and pw:
        return email, pw
    # Local Cowork fallback — find octopos_credentials.txt under any mounted secrets dir.
    for cand in Path("/sessions").glob("*/mnt/Claude/.claude/secrets/octopos_credentials.txt"):
        txt = cand.read_text().strip()
        if ":" in txt:
            e, p = txt.split(":", 1)
            return e.strip(), p.strip()
    raise SystemExit("No OCTOPOS credentials (env OCTOPOS_EMAIL/PASSWORD or octopos_credentials.txt)")


def octopos_jwt():
    email, pw = _credentials()
    code, resp = _http(f"{OCTO_BASE}/api/v1/authenticate", {"email": email, "password": pw}, {})
    if code != 200 or not resp.get("flag"):
        raise SystemExit(f"OCTOPOS login failed: HTTP {code} {resp}")
    return resp["data"]["token"]


def _fmt(d, end=False):
    """YYYY-MM-DD -> MM/DD/YYYY HH:MM:SS (OCTOPOS format)."""
    day = dt.date.fromisoformat(d)
    return day.strftime("%m/%d/%Y") + (" 23:59:59" if end else " 00:00:00")


def fetch_sales_totals(jwt, start, end):
    """Order-level totals for [start, end] inclusive. Paginates if needed."""
    hdr = {"Authorization": f"Bearer {jwt}", "Permission": "report-total-sales"}
    orders, page = [], 1
    while page < 50:
        body = {"location": LOCATION, "dateFrom": _fmt(start), "dateTo": _fmt(end, end=True),
                "departments": [], "categories": [],
                "query": {"limit": 5000, "page": page, "order": "id", "order_type": "desc", "filter": ""}}
        code, resp = _http(f"{OCTO_BASE}/api/v1/get-sales-report", body, hdr)
        if code != 200 or not resp.get("flag"):
            raise SystemExit(f"get-sales-report HTTP {code}: {str(resp)[:300]}")
        od = (resp.get("data") or {}).get("orders") or {}
        rows = od.get("data") or []
        orders.extend(rows)
        last_page = od.get("last_page") or 1
        if page >= last_page or not rows:
            break
        page += 1

    def f(x):
        try: return float(x or 0)
        except (TypeError, ValueError): return 0.0

    paid = [o for o in orders if str(o.get("paid")).lower() == "paid"]
    gross = sum(f(o.get("total_payment_amount")) for o in paid)
    net   = sum(f(o.get("total_sale_price")) for o in paid)
    tax   = sum(f(o.get("total_tax_collected")) for o in paid)
    n = len(paid)

    # Payment-type breakdown (for cash reconciliation vs the manager report).
    pay = {}
    for o in paid:
        pt = (o.get("payment_types") or "Unknown").strip() or "Unknown"
        pay[pt] = round(pay.get(pt, 0.0) + f(o.get("total_payment_amount")), 2)

    return {"gross": round(gross, 2), "net": round(net, 2), "tax": round(tax, 2),
            "transactions": n, "avg_ticket": round(gross / n, 2) if n else 0.0,
            "payment_breakdown": dict(sorted(pay.items(), key=lambda kv: -kv[1])),
            "orders_total": len(orders)}


def fetch_all_vendors(jwt):
    code, resp = _http(f"{OCTO_BASE}/api/v1/get-attributes-for-sales-by-vendor-report",
                       None, {"Authorization": f"Bearer {jwt}", "Permission": "report-total-sales-vendor"})
    if code != 200:
        print(f"WARN vendor list HTTP {code}", file=sys.stderr); return []
    v = (resp.get("data") or {}).get("vendor") or (resp.get("data") or {}).get("vendors") or []
    return [(int(x["id"]), x.get("name") or "") for x in v]


def fetch_top_products(jwt, start, end, limit=25):
    """Union per-vendor products by units_sold, drop structural always-top-sellers."""
    vendors = fetch_all_vendors(jwt)
    if not vendors:
        print("WARN empty vendor list — top products skipped", file=sys.stderr); return []
    df, dt_ = _fmt(start), _fmt(end, end=True)
    hdr = {"Authorization": f"Bearer {jwt}", "Permission": "report-total-sales-vendor"}
    sales = {}
    for vid, vname in vendors:
        body = {"data": {"location": LOCATION, "departments": [], "categories": [],
                         "vendor": [{"id": vid, "name": vname}], "dateFrom": df, "dateTo": dt_},
                "query": {"limit": 5000, "page": 1, "order": "name", "order_type": "asc", "filter": ""}}
        code, resp = _http(f"{OCTO_BASE}/api/v1/get-sales-by-vendor-product-report", body, hdr)
        if code != 200 or not resp.get("flag"):
            print(f"  WARN {vname}: HTTP {code}", file=sys.stderr); continue
        for p in (resp.get("data") or {}).get("products") or []:
            try: pid = int(p["id"])
            except (KeyError, TypeError, ValueError): continue
            units = float(p.get("units_sold") or 0)
            if units <= 0: continue
            revenue = float(p.get("total_sales") or 0)
            sales[pid] = {"product_id": pid, "name": p.get("name") or p.get("product_name") or "",
                          "sku": p.get("sku") or "", "vendor": vname,
                          "units_sold": units, "revenue": round(revenue, 2)}
    rows = list(sales.values())

    def is_always_top(r):
        nm = (r["name"] or "").lower()
        return any(k in nm for k in ALWAYS_TOP_RE)

    rows.sort(key=lambda r: -r["units_sold"])
    filtered = [r for r in rows if not is_always_top(r)]
    return filtered[:limit]


def fetch_octopos_pnl(start, end, top_limit=25):
    jwt = octopos_jwt()
    totals = fetch_sales_totals(jwt, start, end)
    top = fetch_top_products(jwt, start, end, limit=top_limit)
    return {"source": "octopos", "window": {"start": start, "end": end},
            "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            **totals, "top_products": top}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json", action="store_true", help="dump full JSON")
    args = ap.parse_args()
    data = fetch_octopos_pnl(args.start, args.end, top_limit=args.top)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False)); return 0
    print(f"\n=== OCTOPOS sales {args.start} .. {args.end} ===")
    print(f"  Gross (paid, incl tax/tip): ${data['gross']:,.2f}")
    print(f"  Net (sale price):           ${data['net']:,.2f}")
    print(f"  Tax collected:              ${data['tax']:,.2f}")
    print(f"  Transactions:               {data['transactions']:,}")
    print(f"  Avg ticket:                 ${data['avg_ticket']:,.2f}")
    print(f"  Payment breakdown:          {data['payment_breakdown']}")
    print(f"\n  Top {len(data['top_products'])} products (always-top-sellers filtered):")
    for i, r in enumerate(data["top_products"][:15], 1):
        print(f"   {i:2}. {r['units_sold']:>6.0f}u  ${r['revenue']:>9,.0f}  {r['name'][:45]}  [{r['vendor']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
