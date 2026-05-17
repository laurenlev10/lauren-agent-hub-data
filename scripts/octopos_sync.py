#!/usr/bin/env python3
"""
OCTOPOS daily stock sync — pulls all of Lauren's products from her POS,
filters to her 15 mapped suppliers, and writes a snapshot the inventory
dashboard reads.

Tenant: themakeup.octoretail.com
Output: docs/state/octopos_products.json

API quirks discovered 2026-05-12:
- get_products_by_filter ignores page/pagination params (always returns first 100).
- get_products_by_filter ignores vendor_id filter.
- Get product by ID (GET /api/v2/products/{id}) DOES work for any valid id.
- Auth header is raw token: `Authorization: <token>` (no Bearer prefix).
- Location field must be `location_ids` (plural array), not `location_id`.

Workaround: iterate by product id from 1 to ~MAX_ID using HEAD-then-GET.
Currently ~1112 products; binary-search to discover max each run.
"""
import json, os, sys, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone

BASE = "https://themakeup.octoretail.com/api/v2"

# Lauren's 15 suppliers → list of OCTOPOS vendor_ids (confirmed 2026-05-12; 2026-05-13: Mystery Box now spans Garage + Storage).
# Most suppliers are 1:1. Mystery Box is special — Lauren's "מסחורה ממוזגת מהמחסן" combines two
# internal OCTOPOS vendors (24=Garage + 19=Storage). Adding more multi-vendor suppliers later is
# just appending IDs to the list.
MAPPING = {
    "she-makeup":       [18],         # OCTOPOS: "She"
    "mystery-box":      [24, 19],     # OCTOPOS: "Garage" + "Storage" — mixed warehouse merchandise (Lauren 2026-05-13)
    "amuse-cosmetics":  [2],          # OCTOPOS: "Amuse"
    "nabi":             [14],
    "bb-and-w":         [3],
    "prolux":           [15],
    "golden-touch":     [8],
    "ebs-perfumes":     [6],          # was "EBC" in early dashboard; renamed per Lauren 2026-05-12
    "rude":             [17],
    "xime-beauty":      [23],         # OCTOPOS: "Xime"
    "feral-edge":       [7],
    "lurella":          [12],
    "kara-beauty":      [10],
    "romantic-beauty":  [16],
    "beauty-creations": [4],
}
# Inverse: vendor_id → supplier_code (multi-vendor → first-match wins, but each vid is unique across the table)
VENDOR_TO_CODE = {vid: code for code, vids in MAPPING.items() for vid in vids}

LOCATION_ID = 2  # "THE MAKEUP BLOWOUT SALE GROUP INC"


def _request(method, path, token=None, body=None, timeout=20):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def authenticate(email, password):
    code, resp = _request("POST", "/authenticate", body={"email": email, "password": password})
    if code != 200 or "token" not in resp:
        raise SystemExit(f"OCTOPOS auth failed: HTTP {code} {resp}")
    return resp["token"], resp.get("locations", [])


def list_vendors(token):
    code, resp = _request("GET", "/vendors", token=token)
    if code != 200:
        raise SystemExit(f"list_vendors HTTP {code}: {resp}")
    return resp.get("data", []) if isinstance(resp, dict) else resp


def find_max_product_id(token, start=1500, ceiling=50000):
    """Binary-search for the highest existing product id."""
    def exists(pid):
        code, _ = _request("GET", f"/products/{pid}", token=token, timeout=8)
        return code != 404
    lo = 1
    hi = start
    while exists(hi) and hi < ceiling:
        hi *= 2
    # binary search between lo and hi
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if exists(mid):
            lo = mid
        else:
            hi = mid
    return lo


def fetch_product(token, pid):
    code, resp = _request("GET", f"/products/{pid}", token=token, timeout=12)
    if code == 200 and isinstance(resp, dict) and "id" in resp:
        return resp
    return None


def find_max_po_id(token, start=250, ceiling=10000):
    """Binary-search for the highest existing PurchaseOrder id."""
    def exists(pid):
        code, _ = _request("GET", f"/purchase_orders/{pid}", token=token, timeout=8)
        return code != 404
    lo = 1
    hi = start
    while exists(hi) and hi < ceiling:
        hi *= 2
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if exists(mid): lo = mid
        else: hi = mid
    return lo


def fetch_po(token, pid):
    code, resp = _request("GET", f"/purchase_orders/{pid}", token=token, timeout=10)
    if code == 200 and isinstance(resp, dict) and "id" in resp:
        return resp
    return None


def fetch_all_purchase_orders(token, max_id, concurrency=25):
    pos = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for i, p in enumerate(pool.map(lambda pid: fetch_po(token, pid), range(1, max_id + 1)), start=1):
            if p:
                pos.append(p)
            if i % 50 == 0:
                print(f"  …scanned {i}/{max_id} PO ids, {len(pos)} POs so far")
    return pos


def fetch_all_products(token, max_id, concurrency=50):
    products = []

    def task(pid):
        return fetch_product(token, pid)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for i, p in enumerate(pool.map(task, range(1, max_id + 1)), start=1):
            if p:
                products.append(p)
            if i % 200 == 0:
                print(f"  …scanned {i}/{max_id} ids, {len(products)} products so far")
    return products


def build_snapshot(vendors, products):
    """Group products by Lauren's mapped suppliers."""
    vendor_by_id = {v["id"]: v for v in vendors}
    snapshot = {
        "_updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_about": "Daily OCTOPOS snapshot — products grouped by Lauren's 15 mapped suppliers. Source: scripts/octopos_sync.py.",
        "_total_products_scanned": len(products),
        "_total_products_mapped": 0,
        "vendors": {},
    }
    # Seed every mapped supplier so the dashboard always sees all 15
    for code, vids in MAPPING.items():
        # First vid is canonical for display/contact; additional vids are co-sources
        primary = vendor_by_id.get(vids[0], {})
        co_names = [vendor_by_id.get(x, {}).get("name", "") for x in vids[1:] if vendor_by_id.get(x)]
        display_name = primary.get("name", "")
        if co_names:
            display_name += " + " + " + ".join(co_names)
        snapshot["vendors"][code] = {
            "octopos_vendor_ids": vids,
            "octopos_name": display_name,
            "active": primary.get("active", True),
            "purchase_orders_generated": [],   # populated below from fetched POs
            "purchase_orders_received": [],    # ditto
            "contact": {
                "name": (primary.get("contact_person") or "").strip(),
                "phone": str(primary.get("phone") or "") if primary.get("phone") else "",
                "email": (primary.get("email") or "").strip(),
                "address": (primary.get("address") or "").strip(),
                "city": (primary.get("city") or "").strip(),
                "state": (primary.get("state") or "").strip(),
            },
            "products": [],            # active products only
            "inactive_products": [],   # products with active=false
            "summary": {
                "count": 0, "in_stock_count": 0, "total_units": 0.0,
                "inactive_count": 0, "inactive_in_stock_count": 0, "inactive_total_units": 0.0,
                "needs_recount": 0,
                "total_threshold": 0.0,  # sum of all active products' thresholds
                "total_to_order": 0.0,   # sum of max(0, threshold - in_stock) across active products
            },
        }

    mapped = 0
    for p in products:
        vendors_arr = p.get("vendors") or []
        if not vendors_arr:
            continue
        default = next((v for v in vendors_arr if v.get("is_default")), vendors_arr[0])
        vid = default.get("id")
        code = VENDOR_TO_CODE.get(vid)
        if not code:
            continue
        try:
            qty = float(p.get("in_stock_qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        is_active = bool(p.get("active", True))
        needs_recount = qty < 0
        # threshold = the minimum qty Lauren wants on hand at event start;
        # to_order = max(0, threshold - in_stock_qty) is computed in the dashboard.
        try:
            threshold = float(p.get("threshold") or 0)
        except (TypeError, ValueError):
            threshold = 0.0
        try:
            unit_cost = float(p.get("cost") or 0)
        except (TypeError, ValueError):
            unit_cost = 0.0
        # OCTOPOS may expose `cost_calculator_base_units_in_a_case` — use as default case_size.
        # If absent / zero, dashboard falls back to 12 (smallest common case).
        try:
            case_size = int(float(p.get("cost_calculator_base_units_in_a_case") or 0))
        except (TypeError, ValueError):
            case_size = 0
        # categories = OCTOPOS's tagging mechanism (Recount, Display, Case, Check, etc.)
        cats = []
        for c in (p.get("categories") or []):
            cats.append({"id": c.get("id"), "name": c.get("name","")})
        entry = {
            "id": p.get("id"),
            "name": p.get("name", ""),
            "sku": p.get("sku", ""),
            "barcode": p.get("barcode", ""),
            "in_stock_qty": qty,
            "threshold": threshold,
            "unit_cost": unit_cost,
            "case_size": case_size,
            "categories": cats,
            "active": is_active,
            "needs_recount": needs_recount,
            "department": (p.get("department") or {}).get("name", ""),
            "updated_at": p.get("updated_at", ""),
        }
        sv = snapshot["vendors"][code]
        sumkey = "summary"
        if is_active:
            sv["products"].append(entry)
            sv[sumkey]["count"] += 1
            if qty > 0: sv[sumkey]["in_stock_count"] += 1
            sv[sumkey]["total_units"] += qty
            sv[sumkey]["total_threshold"] += threshold
            gap = threshold - qty
            if gap > 0:
                sv[sumkey]["total_to_order"] += gap
        else:
            sv["inactive_products"].append(entry)
            sv[sumkey]["inactive_count"] += 1
            if qty > 0: sv[sumkey]["inactive_in_stock_count"] += 1
            sv[sumkey]["inactive_total_units"] += qty
        if needs_recount:
            sv[sumkey]["needs_recount"] += 1
        mapped += 1

    # Sort both lists per supplier: recount-needed first, then in-stock, then alphabetic
    for code in snapshot["vendors"]:
        for k in ("products", "inactive_products"):
            snapshot["vendors"][code][k].sort(key=lambda x: (
                not x.get("needs_recount", False),                  # recount items first
                -(x["in_stock_qty"] > 0),                            # then in-stock
                x["name"].lower()                                    # then alphabetic
            ))

    snapshot["_total_products_mapped"] = mapped
    return snapshot


def main():
    email = os.environ.get("OCTOPOS_EMAIL", "").strip()
    password = os.environ.get("OCTOPOS_PASSWORD", "").strip()
    if not email or not password:
        # Local-run fallback: read from secrets file
        cred_path = Path(__file__).resolve().parent.parent.parent / "Claude" / ".claude" / "secrets" / "octopos_credentials.txt"
        if cred_path.exists():
            raw = cred_path.read_text()
            import re
            parts = [p.strip() for p in re.split(r"[:\r\n\t]+", raw.strip()) if p.strip()]
            if len(parts) >= 2:
                email, password = parts[0], parts[1]
    if not email or not password:
        raise SystemExit("OCTOPOS_EMAIL + OCTOPOS_PASSWORD required (env or secrets file)")

    print(f"→ authenticating as {email[:3]}***@{email.split('@')[-1] if '@' in email else '?'}")
    token, locations = authenticate(email, password)
    print(f"✓ auth ok ({len(token)} char token, {len(locations)} location(s))")

    print("→ listing vendors")
    vendors = list_vendors(token)
    print(f"✓ {len(vendors)} vendors")

    print("→ listing categories (used as tags by Lauren)")
    code_cats, all_cats = _request("GET", "/categories", token=token)
    cats_data = []
    if code_cats == 200:
        cats_data = all_cats if isinstance(all_cats, list) else (all_cats.get("data", []) if isinstance(all_cats, dict) else [])
    cats_simplified = [{"id": c.get("id"), "name": c.get("name","")} for c in cats_data]
    print(f"✓ {len(cats_simplified)} categories")

    # Skip binary search — use a static ceiling (real max ≈ 1112 as of 2026-05-12;
    # bumping to 1500 leaves room for growth without doubling the scan cost).
    # The fetch step gracefully skips 404s, so over-shooting is harmless.
    max_id = 1500
    print(f"→ scanning ids 1..{max_id} (binary search skipped for speed)")

    print(f"→ fetching all {max_id} product ids (concurrency=50)")
    products = fetch_all_products(token, max_id, concurrency=50)
    print(f"✓ {len(products)} products fetched")

    print("→ binary-searching max PO id")
    max_po = find_max_po_id(token)
    print(f"✓ max PO id = {max_po}")

    print(f"→ fetching all {max_po} PO ids (concurrency=25)")
    pos = fetch_all_purchase_orders(token, max_po, concurrency=25)
    print(f"✓ {len(pos)} POs fetched")

    print("→ building snapshot")
    # ── Auto-tag negative-qty products with RECOUNT (Lauren 2026-05-17 PM late) ─────
    # Any product with negative qty gets the Recount tag automatically so the next
    # event's count includes it. CRITICAL: run BEFORE build_snapshot so in-memory
    # products list reflects the new tags and the saved snapshot includes them.
    RECOUNT_CAT_ID = 14
    auto_tagged_count = 0
    auto_tagged_names = []
    already_tagged = 0
    for prod in products:
        try:
            qty_now = float(prod.get("in_stock_qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty_now >= 0 or not prod.get("active"):
            continue
        cats = prod.get("categories") or []
        has_recount = any((c.get("name") or "").lower() == "recount" for c in cats)
        if has_recount:
            already_tagged += 1
            continue
        cat_ids = [c.get("id") for c in cats if c.get("id")] + [RECOUNT_CAT_ID]
        try:
            code, resp = _request("PUT", f"/products/{prod['id']}", token=token,
                                  body={"category_ids": cat_ids})
            if code == 200:
                auto_tagged_count += 1
                auto_tagged_names.append(prod.get("name") or f"#{prod['id']}")
                # CRITICAL: update in-memory categories so build_snapshot picks them up
                prod.setdefault("categories", []).append({"id": RECOUNT_CAT_ID, "name": "Recount"})
            else:
                print(f"  ⚠ failed to tag id={prod['id']}: HTTP {code} {resp}")
        except Exception as e:
            print(f"  ⚠ exception tagging id={prod['id']}: {e}")
    print(f"✓ auto-tagged {auto_tagged_count} negative products with RECOUNT (already tagged: {already_tagged})")

    if auto_tagged_count >= 5:
        try:
            from lauren_sms import send_sms
            lauren_phone = os.environ.get("LAUREN_PHONE", "4243547625")
            body = (f"@inventory 🚨 {auto_tagged_count} מוצרים נכנסו למינוס וסומנו אוטומטית RECOUNT לאירוע הבא:\n"
                    + "\n".join(f"• {n}" for n in auto_tagged_names[:8])
                    + (f"\n…ועוד {auto_tagged_count - 8}" if auto_tagged_count > 8 else "")
                    + "\n\nhttps://laurenlev10.github.io/lauren-agent-hub-data/recount/")
            send_sms(lauren_phone, body)
            print("✓ SMS sent to Lauren about auto-tagged negatives")
        except Exception as e:
            print(f"  (SMS send failed: {e})")

    snapshot = build_snapshot(vendors, products)
    snapshot["categories"] = cats_simplified  # global tag catalog for dashboard picker

    # Merge POs into per-supplier slots. Resolve product_id → name from products list.
    product_by_id = {p["id"]: p for p in products}
    po_total_lines = 0
    for po in pos:
        if not po.get("active"): continue
        vid = po.get("vendor_id")
        code = VENDOR_TO_CODE.get(vid)
        if not code: continue
        items = []
        line_total = 0.0
        for li in (po.get("purchase_order_items") or []):
            pid = li.get("product_id")
            prod = product_by_id.get(pid) or {}
            try:
                qty  = float(li.get("quantity") or li.get("no_of_ea") or 0)
            except (TypeError, ValueError): qty = 0
            try:
                cost = float(li.get("cost_unit") or li.get("cost") or 0)
            except (TypeError, ValueError): cost = 0
            items.append({
                "product_id": pid,
                "product_name": prod.get("name") or f"#{pid}",
                "product_sku":  prod.get("sku") or "",
                "quantity":     qty,
                "cost_unit":    cost,
                "line_total":   round(qty * cost, 2),
            })
            line_total += qty * cost
        po_summary = {
            "id": po.get("id"),
            "status": po.get("status"),
            "vendor_invoice_number": po.get("vendor_invoice_number"),
            "received_date": po.get("received_date"),
            "order_paid": bool(po.get("order_paid")),
            "items": items,
            "total_qty": sum(it["quantity"] for it in items),
            "total_cost": round(line_total, 2),
        }
        bucket = "purchase_orders_generated" if po.get("status") == "Generated" else "purchase_orders_received"
        snapshot["vendors"][code][bucket].append(po_summary)
        po_total_lines += len(items)
    # Sort each bucket: id desc (most recent first)
    for code in snapshot["vendors"]:
        for b in ("purchase_orders_generated", "purchase_orders_received"):
            snapshot["vendors"][code][b].sort(key=lambda x: -(x.get("id") or 0))
    snapshot["_total_pos"] = len(pos)
    snapshot["_total_po_lines"] = po_total_lines
    print(f"✓ merged {len(pos)} POs ({po_total_lines} line items) into supplier slots")
    print(f"✓ mapped {snapshot['_total_products_mapped']} products into 15 suppliers")
    print()
    print("Per-supplier summary:")
    for code, ent in snapshot["vendors"].items():
        s = ent["summary"]
        recount = s.get('needs_recount', 0)
        rc_str = f" · 🔢 RECOUNT={recount}" if recount > 0 else ""
        to_order = int(s.get('total_to_order', 0))
        order_str = f" · 🛒 to-order={to_order}" if to_order > 0 else ""
        po_gen = len(ent.get('purchase_orders_generated', []))
        po_rec = len(ent.get('purchase_orders_received', []))
        po_str = f" · 📋 POs gen={po_gen} rec={po_rec}" if po_gen + po_rec > 0 else ""
        print(f"  {code:<22} ({ent['octopos_name']:<28})  active: {s['count']:>4} ({s['in_stock_count']:>4} in stock, {s['total_units']:>7.0f} units)  · inactive: {s['inactive_count']:>4} ({s['inactive_in_stock_count']:>3} in stock){rc_str}{order_str}{po_str}")

    # Write
    out_path = Path("docs/state/octopos_products.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    print(f"\n✓ wrote {out_path}  ({out_path.stat().st_size:,} bytes)")

    # Also append today's qty snapshot to the timeseries for @event-yield analyzer.
    # Schema: {snapshots: {YYYY-MM-DD: {product_id_str: in_stock_qty}}}. 60-day rolling history.
    from datetime import date, datetime as _dt, timezone as _tz, timedelta as _td
    ts_path = Path("docs/state/octopos_stock_timeseries.json")
    if ts_path.exists():
        ts = json.loads(ts_path.read_text())
    else:
        ts = {"_about": "Daily in_stock_qty per product. Consumed by @event-yield.", "_schema_version": 1, "_retention_days": 60, "snapshots": {}}
    today = date.today().isoformat()
    today_snap = {}
    for p in products:
        pid = p.get("id")
        try:
            qty = float(p.get("in_stock_qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if pid is not None:
            today_snap[str(pid)] = qty
    ts.setdefault("snapshots", {})[today] = today_snap
    # Roll off snapshots older than retention_days
    cutoff = (date.today() - _td(days=int(ts.get("_retention_days", 60)))).isoformat()
    ts["snapshots"] = {d: v for d, v in ts["snapshots"].items() if d >= cutoff}
    ts["_updated_at"] = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_path.write_text(json.dumps(ts, indent=2, ensure_ascii=False))
    print(f"✓ appended today's snapshot to {ts_path} ({len(today_snap)} products, {len(ts['snapshots'])} days retained)")


if __name__ == "__main__":
    main()
