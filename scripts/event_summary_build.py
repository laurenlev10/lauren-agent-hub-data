#!/usr/bin/env python3
"""event_summary_build.py — Pre-aggregate post-event review data for one event."""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, sys
import urllib.error, urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Lauren 2026-06-08 — mirror the @recount exclusion set so the event-summary tabs
# match the recount task exactly (no Market / Clearance / permanently-excluded noise).
EXCL_CATEGORIES = {"Market"}
EXCL_PRODUCT_IDS = {1000, 1001, 1002, 1003, 1011, 921}
EXCL_NAMES = {
    "Roll Shrink", "Plastic Bags", "Gifts - Glitters", "Gifts - Eyeshadows",
    "Romantic Soft Focus Setting Powder - Translucent", "Mini Fan",
}
def is_excluded(snap, name=None, supplier=None):
    pid = snap.get("id") if isinstance(snap, dict) else None
    if pid in EXCL_PRODUCT_IDS:
        return True
    nm = (name if name is not None else (snap.get("name") if isinstance(snap, dict) else "")) or ""
    nm = nm.strip()
    if nm in EXCL_NAMES or nm.startswith("Clearance!"):
        return True
    cats = {(c.get("name") or "").strip() for c in ((snap.get("categories") if isinstance(snap, dict) else None) or [])}
    if cats & EXCL_CATEGORIES:
        return True
    sup = (supplier if supplier is not None else (snap.get("_supplier_name") if isinstance(snap, dict) else "")) or ""
    if sup.strip().lower() == "market":
        return True
    return False
OCTO_BASE = "https://themakeup.octoretail.com"
OCTO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def http_post(url, body, headers, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={**headers, "Content-Type":"application/json","Accept":"application/json","User-Agent":OCTO_UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}


def http_get(url, headers, timeout=20):
    req = urllib.request.Request(url,
        headers={**headers, "Accept":"application/json","User-Agent":OCTO_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}


def octopos_jwt():
    email = os.environ["OCTOPOS_EMAIL"]; pw = os.environ["OCTOPOS_PASSWORD"]
    code, resp = http_post(f"{OCTO_BASE}/api/v1/authenticate",
                           {"email":email,"password":pw}, {})
    if code != 200 or not resp.get("flag"):
        raise SystemExit(f"OCTOPOS login failed: HTTP {code} {resp}")
    return resp["data"]["token"]


def parse_schedule():
    html = (REPO_ROOT / "docs/launch/index.html").read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\{[\s\S]*?\});", html)
    if not m: raise SystemExit("SCHEDULE not found")
    sched = json.loads(m.group(1))
    out = []
    for year, evs in sched.items():
        if not isinstance(evs, list): continue
        for ev in evs:
            ev["_year"] = year
            out.append(ev)
    return out


def evkey_of(ev):
    return f"{(ev.get('city') or '').lower().replace(' ','-')}-{ev.get('start_date')}"


def find_event(evkey, schedule):
    for ev in schedule:
        if evkey_of(ev) == evkey: return ev
    return None


def find_most_recent_event(schedule):
    today = dt.date.today()
    past = []
    for ev in schedule:
        try: end = dt.date.fromisoformat(ev.get("end_date") or "")
        except ValueError: continue
        if end <= today: past.append((end, ev))
    if not past: return None
    past.sort(key=lambda x: x[0], reverse=True)
    return past[0][1]


def fetch_all_vendors(jwt):
    code, resp = http_get(f"{OCTO_BASE}/api/v1/get-attributes-for-sales-by-vendor-report",
        {"Authorization": f"Bearer {jwt}", "Permission":"report-total-sales-vendor"})
    if code != 200:
        print(f"WARN: vendor list HTTP {code}", file=sys.stderr); return []
    v = (resp.get("data") or {}).get("vendor") or (resp.get("data") or {}).get("vendors") or []
    return [(int(x["id"]), x.get("name") or "") for x in v]


def fetch_recount_rows(jwt, start, end, location_id=2):
    all_rows=[]; page=1
    while page < 20:
        code,resp = http_post(f"{OCTO_BASE}/api/v1/get-recount-data",
            {"location_id":location_id,"start_date":start,"end_date":end,
             "limit":5000,"page":page,"order":"id","order_type":"desc","filter":""},
            {"Authorization":f"Bearer {jwt}","Permission":"report-inventary-recount"})
        if code != 200 or not resp.get("flag"):
            if page==1: raise SystemExit(f"get-recount-data: HTTP {code} {resp}")
            break
        items = (resp.get("data") or {}).get("data", []) or []
        if not items: break
        all_rows.extend(items)
        total = (resp.get("data") or {}).get("totalItems") or len(all_rows)
        if len(all_rows) >= total: break
        page += 1
    s = dt.date.fromisoformat(start); e = dt.date.fromisoformat(end)
    def in_win(row):
        try:
            ca = str(row.get("created_at") or "").split()[0]
            d = dt.datetime.strptime(ca,"%m/%d/%Y").date()
            return s <= d <= e
        except: return False
    f = [r for r in all_rows if in_win(r)]
    print(f"recount: {len(all_rows)} from API, {len(f)} in {start}..{end}")
    return f


def fetch_sales_by_vendor_product(jwt, start, end):
    vendors = fetch_all_vendors(jwt)
    if not vendors:
        print("WARN: empty vendor list — fallback", file=sys.stderr)
        vendors = [(18,"She"),(2,"Amuse"),(13,"Market"),(14,"Nabi"),(3,"BB&W"),
                   (15,"Prolux"),(17,"Rude"),(23,"Xime"),(7,"Feral Edge"),
                   (12,"Lurella"),(10,"Kara Beauty"),(16,"Romantic Beauty"),(4,"Beauty Creations")]
    print(f"sales: iterating {len(vendors)} vendors for {start}..{end}")
    df = dt.date.fromisoformat(start).strftime("%m/%d/%Y") + " 00:00:00"
    dt_ = dt.date.fromisoformat(end).strftime("%m/%d/%Y") + " 23:59:59"
    sales = {}
    for vid, vname in vendors:
        body = {"data":{
            "location":{"label":"THE MAKEUP BLOWOUT SALE GROUP INC",
                        "value":{"id":2,"name":"THE MAKEUP BLOWOUT SALE GROUP INC"}},
            "departments":[],"categories":[],"vendor":[{"id":vid,"name":vname}],
            "dateFrom":df,"dateTo":dt_},
            "query":{"limit":5000,"page":1,"order":"name","order_type":"asc","filter":""}}
        try:
            code,resp = http_post(f"{OCTO_BASE}/api/v1/get-sales-by-vendor-product-report",
                body, {"Authorization":f"Bearer {jwt}","Permission":"report-total-sales-vendor"})
            if code != 200 or not resp.get("flag"):
                print(f"  WARN {vname}: HTTP {code}", file=sys.stderr); continue
            for p in (resp.get("data") or {}).get("products") or []:
                try: pid = int(p["id"])
                except: continue
                units = float(p.get("units_sold") or 0)
                if units <= 0: continue
                revenue = float(p.get("total_sales") or 0)
                price = (revenue / units) if units else 0
                sales[pid] = {"units_sold":units, "revenue":revenue,
                    "name":p.get("name") or p.get("product_name") or "",
                    "sku":p.get("sku") or "", "vendor_id":vid, "vendor_name":vname,
                    "price":price, "in_stock_qty_at_event": p.get("in_stock_qty"),
                    "department": p.get("department") or ""}
        except Exception as e:
            print(f"  WARN {vname}: {e}", file=sys.stderr)
    print(f"sales: {len(sales)} unique products sold")
    return sales


def load_snapshot():
    p = REPO_ROOT / "docs/state/octopos_products.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for code, vinfo in (d.get("vendors") or {}).items():
        for prod in (vinfo.get("products") or []):
            try: pid = int(prod["id"])
            except: continue
            prod["_supplier_code"] = code
            prod["_supplier_name"] = vinfo.get("octopos_name") or vinfo.get("name") or code
            out[pid] = prod
    print(f"snapshot: {len(out)} active products")
    return out


def load_worklist(evkey):
    p = REPO_ROOT / "docs/state/octopos_recount.json"
    if not p.exists(): return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return ((d.get("events") or {}).get(evkey) or {}).get("worklist") or []


def fetch_live_product_v2(pid, v2_token, retries=3):
    """Fallback when a product isn't in the daily snapshot (e.g. no vendor_id).
    Returns a snap-like dict so callers can use it interchangeably."""
    import time
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{OCTO_BASE}/api/v2/products/{pid}",
                headers={"Authorization": v2_token, "Accept":"application/json", "User-Agent":OCTO_UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read())
            break
        except Exception as e:
            if attempt + 1 == retries:
                print(f"  live fetch failed for pid {pid} after {retries} tries: {e}", file=sys.stderr)
                return None
            time.sleep(0.5 + attempt * 0.5)
    try:
        d = data.get("data", data)
        if isinstance(d, list): d = d[0] if d else {}
        return {
            "id": d.get("id"),
            "name": d.get("name") or "",
            "sku": d.get("sku") or "",
            "barcode": d.get("barcode") or "",
            "in_stock_qty": float(d.get("in_stock_qty") or 0) if d.get("in_stock_qty") is not None else None,
            "threshold": float(d.get("threshold") or 0) if d.get("threshold") is not None else None,
            "unit_cost": float(d.get("cost") or 0) if d.get("cost") is not None else None,
            "sale_price": float(d.get("sale_price") or 0) if d.get("sale_price") is not None else None,
            "active": bool(d.get("active", True)),
            "categories": d.get("categories") or [],
            "department": (d.get("department") or {}).get("name") if isinstance(d.get("department"), dict) else (d.get("department") or ""),
            "_supplier_name": "—",
            "_supplier_code": "",
            "_orphan": True,  # flag — fetched live, not in snapshot
        }
    except Exception as e:
        print(f"  live fetch parse failed for pid {pid}: {e}", file=sys.stderr)
        return None


def hydrate_orphans(rows, snapshot, v2_token):
    """Find rows whose snapshot lookup returned no data, and live-fetch from OCTOPOS."""
    if not v2_token: return 0
    missing_pids = set()
    for r in rows:
        pid = r.get("product_id")
        if pid and (snapshot.get(pid) is None):
            missing_pids.add(pid)
    if not missing_pids:
        return 0
    print(f"hydrate: fetching {len(missing_pids)} orphan products (missing from daily snapshot)")
    fetched = 0
    for pid in missing_pids:
        snap = fetch_live_product_v2(pid, v2_token)
        if snap:
            snapshot[pid] = snap
            fetched += 1
    print(f"hydrate: fetched {fetched}/{len(missing_pids)}")
    return fetched


def cats_and_recount(snap):
    """Returns (categories_list, has_recount_bool) from a snapshot product dict."""
    cats = snap.get("categories") or []
    out_cats = [{"id": c.get("id"), "name": c.get("name")} for c in cats if c.get("id")]
    has_rc = any((c.get("name") or "").strip().lower() == "recount" for c in cats)
    return out_cats, has_rc


def build_counted(rows, snapshot, sales=None):
    by_pid = {}
    for r in rows:
        try: pid = int(r["product_id"])
        except: continue
        rec = by_pid.setdefault(pid, {"product_id":pid,"name":r.get("product_name") or "",
            "events":[],"delta_total":0.0})
        try: q = float(r.get("quantity") or 0)
        except: q = 0.0
        delta = q if r.get("type")=="CR" else -q
        rec["delta_total"] += delta
        rec["events"].append({"type":r.get("type"),"qty":q,
            "balance_after":r.get("balance"),"at":r.get("created_at")})
    out = []
    for pid, rec in by_pid.items():
        snap = snapshot.get(pid) or {}
        rec["sku"] = snap.get("sku") or ""
        rec["supplier"] = snap.get("_supplier_name") or ""
        rec["current_stock"] = snap.get("in_stock_qty")
        rec["threshold"] = snap.get("threshold")
        rec["event_count"] = len(rec["events"])
        cats, has_rc = cats_and_recount(snap)
        rec["categories"] = cats
        rec["has_recount_tag"] = has_rc
        # Per Lauren 2026-05-26 v2: units sold at this event (from sales API)
        s = (sales or {}).get(pid) or {}
        rec["units_sold_at_event"] = s.get("units_sold") or 0
        rec["revenue_at_event"] = s.get("revenue") or 0
        out.append(rec)
    out.sort(key=lambda x: abs(x["delta_total"]), reverse=True)
    return out


def build_missed(worklist, counted_pids, snapshot, sales=None):
    name_to_pid = {(s.get("name") or "").strip().lower(): pid for pid,s in snapshot.items()}
    out = []
    for w in worklist:
        wpid = w.get("id") or w.get("product_id")
        if wpid is None:
            wpid = name_to_pid.get((w.get("name") or "").strip().lower())
        if wpid is not None and int(wpid) in counted_pids:
            continue
        snap = snapshot.get(int(wpid)) if wpid is not None else None
        cats, has_rc = cats_and_recount(snap or {})
        s = (sales or {}).get(int(wpid)) if wpid is not None else None
        units_sold = (s or {}).get("units_sold") or 0
        out.append({"product_id": int(wpid) if wpid is not None else None,
            "name": w.get("name") or "",
            "sku": (snap or {}).get("sku") or w.get("sku") or "",
            "supplier": (snap or {}).get("_supplier_name") or w.get("supplier") or "",
            "reason": w.get("reason") or "",
            "current_stock": (snap or {}).get("in_stock_qty"),
            "threshold": (snap or {}).get("threshold"),
            "units_sold_at_event": units_sold,
            "categories": cats, "has_recount_tag": has_rc})
    return out


def build_negatives(snapshot):
    out = []
    for pid, snap in snapshot.items():
        try: qty = float(snap.get("in_stock_qty") or 0)
        except: continue
        if qty >= 0 or not snap.get("active", True): continue
        if is_excluded(snap): continue
        cats, has_rc = cats_and_recount(snap)
        out.append({"product_id":pid,"name":snap.get("name") or "",
            "sku":snap.get("sku") or "","supplier":snap.get("_supplier_name") or "",
            "department":snap.get("department") or "","in_stock_qty":qty,
            "current_stock":qty,
            "threshold":snap.get("threshold"),
            "needs_recount":bool(snap.get("needs_recount")),
            "last_updated":snap.get("updated_at"),
            "categories":cats,"has_recount_tag":has_rc})
    out.sort(key=lambda x: x["in_stock_qty"])
    return out


def build_slow_movers(snapshot, sales):
    out = []
    for pid, snap in snapshot.items():
        try: qty = float(snap.get("in_stock_qty") or 0)
        except: continue
        if qty <= 1 or not snap.get("active", True): continue
        if pid in sales: continue
        if is_excluded(snap): continue
        cost = float(snap.get("unit_cost") or 0)
        cats, has_rc = cats_and_recount(snap)
        out.append({"product_id":pid,"name":snap.get("name") or "",
            "sku":snap.get("sku") or "","supplier":snap.get("_supplier_name") or "",
            "department":snap.get("department") or "","in_stock_qty":qty,
            "current_stock":qty,
            "threshold":snap.get("threshold"),"unit_cost":cost,
            "sale_price":snap.get("sale_price"),"tied_up_cost":cost*qty,
            "categories":cats,"has_recount_tag":has_rc})
    out.sort(key=lambda x: x["tied_up_cost"] or 0, reverse=True)
    return out


def build_top_sellers(sales, snapshot, limit=100):
    rows = []
    for pid, s in sales.items():
        snap = snapshot.get(pid) or {}
        if is_excluded(snap, name=(s.get("name") or snap.get("name")), supplier=(s.get("vendor_name") or snap.get("_supplier_name"))):
            continue
        cats, has_rc = cats_and_recount(snap)
        rows.append({"product_id":pid,
            "name":s["name"] or snap.get("name") or "",
            "sku":s["sku"] or snap.get("sku") or "",
            "supplier":s["vendor_name"] or snap.get("_supplier_name") or "",
            "units_sold":s["units_sold"],"price":s["price"],"revenue":s["revenue"],
            "current_stock":snap.get("in_stock_qty"),"threshold":snap.get("threshold"),
            "categories":cats,"has_recount_tag":has_rc})
    rows.sort(key=lambda x: x["units_sold"], reverse=True)
    return rows[:limit]


def build_no_threshold(snapshot, sales=None):
    out = []
    for pid, snap in snapshot.items():
        try: thr = float(snap.get("threshold") or 0)
        except: thr = 0
        if thr > 0 or not snap.get("active", True): continue
        if is_excluded(snap): continue
        cats, has_rc = cats_and_recount(snap)
        s = (sales or {}).get(pid) or {}
        out.append({"product_id":pid,"name":snap.get("name") or "",
            "sku":snap.get("sku") or "","supplier":snap.get("_supplier_name") or "",
            "department":snap.get("department") or "",
            "in_stock_qty":snap.get("in_stock_qty"),
            "current_stock":snap.get("in_stock_qty"),
            "threshold":thr,
            "unit_cost":snap.get("unit_cost"),"sale_price":snap.get("sale_price"),
            "created_at":snap.get("created_at"),
            "units_sold_at_event": s.get("units_sold") or 0,
            "categories":cats,"has_recount_tag":has_rc})
    out.sort(key=lambda x: x["supplier"] or "")
    return out


def build_for_event(ev):
    evkey = evkey_of(ev); start = ev["start_date"]; end = ev["end_date"]
    print(f"\n=== Building {evkey} ({start}→{end}) ===")
    jwt = octopos_jwt()
    snapshot = load_snapshot()
    worklist = load_worklist(evkey); print(f"worklist: {len(worklist)}")
    recount_rows = fetch_recount_rows(jwt, start, end)
    sales = fetch_sales_by_vendor_product(jwt, start, end)

    # Hydrate orphan products (counted at event but missing from daily snapshot —
    # e.g. items with no vendor_id assigned in OCTOPOS). Look up live via /api/v2.
    v2_token = os.environ.get("OCTOPOS_TOKEN") or ""
    if not v2_token:
        # Fall back: try reading from local secret file (Cowork session)
        sec = Path("/sessions/magical-loving-heisenberg/mnt/Claude/.claude/secrets/octopos_token.txt")
        if sec.exists():
            v2_token = sec.read_text().strip()
    if v2_token:
        # Always-hydrate pids that appear in recount_rows OR in the worklist.
        # These products show up in counted_at_event / missed_from_worklist tabs and are
        # the most visible to Lauren — must reflect live OCTOPOS values, not stale snapshot.
        # For sales pids, only hydrate if missing (keeps API call count reasonable).
        always_hydrate = set()
        for row in recount_rows:
            try: always_hydrate.add(int(row.get("product_id")))
            except: pass
        for w in worklist:
            wpid = w.get("id") or w.get("product_id")
            if wpid is not None:
                try: always_hydrate.add(int(wpid))
                except: pass
        if_missing = set()
        for pid in sales.keys():
            if pid not in always_hydrate: if_missing.add(pid)

        hydrate_count = 0
        import time
        for pid in always_hydrate:
            snap = fetch_live_product_v2(pid, v2_token)
            if snap:
                snapshot[pid] = snap  # always overwrite — live is truth
                hydrate_count += 1
            time.sleep(0.12)
        for pid in if_missing:
            if snapshot.get(pid) is None:
                snap = fetch_live_product_v2(pid, v2_token)
                if snap:
                    snapshot[pid] = snap
                    hydrate_count += 1
                time.sleep(0.12)
        print(f"hydrate: {hydrate_count} products refreshed live ({len(always_hydrate)} always, {len(if_missing)} if-missing)")

    counted = build_counted(recount_rows, snapshot, sales)
    counted_pids = {c["product_id"] for c in counted}
    missed = build_missed(worklist, counted_pids, snapshot, sales)
    negatives = build_negatives(snapshot)
    slow_movers = build_slow_movers(snapshot, sales)
    top_sellers = build_top_sellers(sales, snapshot)
    no_threshold = build_no_threshold(snapshot, sales)

    summary = {"evkey":evkey,
        "event":{"city":ev.get("city"),"state":ev.get("state"),
                 "venue":ev.get("venue"),"address":ev.get("address"),
                 "start_date":start,"end_date":end,"year":ev.get("_year"),
                 "tier":ev.get("tier")},
        "generated_at":dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tabs":{"counted_at_event":counted,"missed_from_worklist":missed,
                "negatives":negatives,"slow_movers":slow_movers,
                "top_sellers":top_sellers,"no_threshold":no_threshold},
        "stats":{"counted_count":len(counted),"missed_count":len(missed),
                 "negatives_count":len(negatives),
                 "slow_movers_count":len(slow_movers),
                 "top_sellers_count":len(top_sellers),
                 "no_threshold_count":len(no_threshold),
                 "total_units_sold":sum(s["units_sold"] for s in sales.values()),
                 "total_revenue":sum(s["revenue"] for s in sales.values()),
                 "unique_products_sold":len(sales)}}

    out_dir = REPO_ROOT / "docs/state/event_summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{evkey}.json"
    p.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✓ Wrote {p.relative_to(REPO_ROOT)} ({p.stat().st_size:,} bytes)")
    print(f"  counted={len(counted)} missed={len(missed)} negatives={len(negatives)} slow={len(slow_movers)} top={len(top_sellers)} no_thr={len(no_threshold)}")
    print(f"  revenue ${summary['stats']['total_revenue']:,.2f} from {summary['stats']['unique_products_sold']} products")

    idx_path = out_dir / "_index.json"
    idx = {}
    if idx_path.exists():
        try: idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except: idx = {}
    idx[evkey] = {"generated_at":summary["generated_at"],"city":ev.get("city"),
        "state":ev.get("state"),"start_date":start,"end_date":end,"stats":summary["stats"]}
    idx_path.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey"); ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()
    schedule = parse_schedule()
    if args.auto and not args.evkey:
        ev = find_most_recent_event(schedule)
        if not ev: print("No past events"); return 1
        print(f"--auto picked: {evkey_of(ev)}")
    elif args.evkey:
        ev = find_event(args.evkey, schedule)
        if not ev: print(f"evkey not in SCHEDULE: {args.evkey}", file=sys.stderr); return 2
    else:
        ap.print_help(); return 0
    build_for_event(ev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
