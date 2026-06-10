#!/usr/bin/env python3
"""
Weekend recap — runs Monday morning after each event weekend.

Computes (per active product):
1. Sales over the weekend (from stock_timeseries Friday vs Monday delta,
   adjusted for any PO arrivals in between). Falls back to "unknown" if
   the time-series doesn't span the weekend yet.
2. RECOUNT tag management:
   • Auto-ADD if stock < 0 on Monday (sold-out + over-sold past inventory)
   • Auto-ADD if positive stock + zero movement during the event (probably
     not displayed on the table — staff should physically verify)
   • Auto-REMOVE if product had RECOUNT going in AND stock is now positive
     (the physical count happened on Saturday/Sunday)
3. First-sale tracking: when a brand-new product (no prior weekend record)
   sees its first non-zero sale, capture it as the baseline.

Writes docs/state/weekend_sales.json:
{
  "_updated_at": "...",
  "_about": "Per-product weekend sales history. Drives compute_recommendations.py.",
  "weekends": [{"event_date": "2026-05-22", "computed_at": "..."}],
  "products": {
    "987": {
      "name": "BB&W Lipstick + Lipliner",
      "first_seen": "2026-05-22",
      "first_sale_qty": 24,
      "weekends": [{"event_date": "2026-05-22", "stock_friday": 96, "po_received": 0, "stock_monday": 24, "sold": 72}]
    }
  }
}

The OCTOPOS RECOUNT mutations happen via PUT /products/<id> with category_ids.
Same auth pattern as octopos_sync.py + the manual fix scripts from earlier today.
"""
import json, os, sys, urllib.request, urllib.error
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = "https://themakeup.octoretail.com/api/v2"
RECOUNT_CAT_ID = 14
OCT_PATH = Path('docs/state/octopos_products.json')
TS_PATH = Path('docs/state/octopos_stock_timeseries.json')
ARCH_PATH = Path('docs/state/invoice_archive.json')
WSALES_PATH = Path('docs/state/weekend_sales.json')

def http(method, path, token=None, body=None, timeout=40):
    # 2026-06-10: retry transient network timeouts (OCTOPOS was slow 6/9 → run failed
    # + needless failure-SMS to Lauren). 3 attempts, backoff 5s/10s; HTTP errors are
    # returned (not retried) — these are read-only report queries, safe to retry.
    h = {"Accept": "application/json", "User-Agent": "weekend-recap/1.0"}
    if token: h["Authorization"] = token
    data = json.dumps(body).encode() if body else None
    if body: h["Content-Type"] = "application/json"
    last = None
    for attempt in range(3):
        req = urllib.request.Request(f"{BASE}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read().decode())
            except: return e.code, {}
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            last = e
            print(f"  http retry {attempt+1}/3 after {type(e).__name__}: {e}")
            time.sleep(5 * (attempt + 1))
    raise last

def authenticate():
    email = os.environ.get("OCTOPOS_EMAIL")
    password = os.environ.get("OCTOPOS_PASSWORD")
    if not email or not password:
        cred_file = Path("/sessions/jolly-bold-knuth/mnt/Claude/.claude/secrets/octopos_credentials.txt")
        if cred_file.exists():
            email, password = cred_file.read_text().strip().split(":", 1)
        else:
            raise SystemExit("missing OCTOPOS credentials")
    code, r = http("POST", "/authenticate", body={"email": email, "password": password})
    if code != 200 or "token" not in r: raise SystemExit(f"auth: {code} {r}")
    return r["token"]

def find_event_weekend(today=None):
    """Pick the most recent Fri-Sun weekend. Returns (friday_date, monday_date)."""
    today = today or datetime.now(timezone.utc).date()
    # Find most recent Monday (or today if Monday)
    days_back = (today.weekday() - 0) % 7   # Monday=0
    monday = today - timedelta(days=days_back)
    friday = monday - timedelta(days=3)
    return friday.isoformat(), monday.isoformat()

def stock_on(ts_snaps, date_str, pid):
    """Returns stock for pid on date_str — exact match or nearest earlier."""
    if date_str in ts_snaps:
        v = ts_snaps[date_str].get(str(pid))
        if v is not None: return float(v)
    # Fallback: look back up to 3 days
    base = datetime.fromisoformat(date_str).date()
    for off in range(1, 4):
        d = (base - timedelta(days=off)).isoformat()
        if d in ts_snaps:
            v = ts_snaps[d].get(str(pid))
            if v is not None: return float(v)
    return None

def main():
    octopos = json.loads(OCT_PATH.read_text())
    ts = json.loads(TS_PATH.read_text()).get('snapshots', {})
    archive = json.loads(ARCH_PATH.read_text()) if ARCH_PATH.exists() else {'invoices': {}}
    wsales = json.loads(WSALES_PATH.read_text()) if WSALES_PATH.exists() else {
        '_updated_at': None,
        '_about': 'Per-product weekend sales. Computed Monday from Friday→Monday stock_timeseries delta.',
        'weekends': [], 'products': {}
    }

    friday, monday = find_event_weekend()
    print(f"→ recap window: {friday} (Fri) → {monday} (Mon)")
    print(f"→ stock_timeseries has {len(ts)} day(s): {sorted(ts.keys())[:5]}{'...' if len(ts)>5 else ''}")

    # Auth (needed for RECOUNT tag mutations)
    token = None
    try: token = authenticate()
    except SystemExit as e: print(f"⚠ auth failed: {e} — RECOUNT mutations will be SKIPPED")

    # Build a map: pid → (active, stock_now, current_category_ids, supplier, name)
    pid_info = {}
    for code, vd in octopos['vendors'].items():
        for p in (vd.get('products') or []):
            pid = str(p['id'])
            pid_info[pid] = {
                'active': True,
                'name': p.get('name') or f"#{pid}",
                'supplier': code,
                'stock_now': float(p.get('in_stock_qty') or 0),
                'cat_ids': [c.get('id') for c in (p.get('categories') or []) if c.get('id')],
                'had_recount': any(c.get('id') == RECOUNT_CAT_ID for c in (p.get('categories') or [])),
            }

    # Sum PO receipts within the Fri→Mon window per pid
    po_received_in_window = {}
    win_dates = set()
    cur = datetime.fromisoformat(friday).date()
    end = datetime.fromisoformat(monday).date()
    while cur <= end:
        win_dates.add(cur.isoformat())
        cur += timedelta(days=1)
    for sup_code, sup_invs in archive.get('invoices', {}).items():
        for inv in sup_invs:
            d = inv.get('invoice_date')
            if d and d in win_dates:
                for L in (inv.get('lines') or []):
                    pid = str(L.get('matched_product_id') or '')
                    if pid:
                        po_received_in_window[pid] = po_received_in_window.get(pid, 0) + (L.get('unit_qty') or 0)

    # Compute per-product
    sales_computed = 0
    tags_added, tags_removed = [], []
    for pid, info in pid_info.items():
        stock_fri = stock_on(ts, friday, pid)
        stock_mon = stock_on(ts, monday, pid)
        po_in = po_received_in_window.get(pid, 0)
        sold = None
        if stock_fri is not None and stock_mon is not None:
            # sales = stock_fri + PO_received - stock_mon
            sold = max(0, stock_fri + po_in - stock_mon)
            sales_computed += 1
        # Update products tracking
        prec = wsales['products'].setdefault(pid, {
            'name': info['name'], 'supplier': info['supplier'],
            'first_seen': None, 'first_sale_qty': None,
            'weekends': []
        })
        # Dedup: don't add the same event_date twice
        if monday not in [w.get('event_date') for w in prec['weekends']]:
            prec['weekends'].append({
                'event_date': monday,
                'stock_friday': stock_fri,
                'po_received': po_in,
                'stock_monday': stock_mon if stock_mon is not None else info['stock_now'],
                'sold': sold
            })
            if sold and sold > 0 and prec['first_sale_qty'] is None:
                prec['first_sale_qty'] = sold
                prec['first_seen'] = monday
        # RECOUNT logic
        cur_stock = info['stock_now']
        had = info['had_recount']
        new_cats = list(info['cat_ids'])
        action = None
        # Rule: add RECOUNT if stock < 0 (in addition to octopos_sync.py's logic)
        if cur_stock < 0 and not had:
            new_cats.append(RECOUNT_CAT_ID)
            action = f"+RECOUNT (stock={cur_stock} <0)"
            tags_added.append((pid, info['name'], action))
        # Rule: add RECOUNT for zero-movement during event (positive stock + sold==0 AND we have data)
        elif sold == 0 and cur_stock > 0 and not had:
            new_cats.append(RECOUNT_CAT_ID)
            action = "+RECOUNT (no sales this weekend — check display)"
            tags_added.append((pid, info['name'], action))
        # Rule: remove RECOUNT if it was on AND stock is now non-negative AND data shows movement
        elif had and cur_stock >= 0 and sold is not None and sold > 0:
            new_cats = [c for c in new_cats if c != RECOUNT_CAT_ID]
            action = f"-RECOUNT (counted+moved, stock={cur_stock}, sold={sold})"
            tags_removed.append((pid, info['name'], action))
        # Apply mutation
        if action and token:
            code, resp = http("PUT", f"/products/{pid}", token=token, body={"category_ids": new_cats})
            if code != 200:
                print(f"  ⚠ tag mutation failed for {pid}: HTTP {code}")
        elif action:
            print(f"  (skip mutation, no auth): {pid} {action}")

    # Record this weekend's recap in the top-level log
    wsales['weekends'].append({
        'event_date': monday, 'computed_at': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'sales_computed': sales_computed, 'tags_added': len(tags_added), 'tags_removed': len(tags_removed)
    })
    wsales['_updated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    WSALES_PATH.write_text(json.dumps(wsales, indent=2, ensure_ascii=False))
    print(f"\n✓ wrote {WSALES_PATH}")
    print(f"  sales computed for {sales_computed} products (need both Fri+Mon snapshots)")
    print(f"  RECOUNT additions: {len(tags_added)}")
    for pid, nm, a in tags_added[:8]: print(f"    + id={pid} {nm[:50]} :: {a}")
    print(f"  RECOUNT removals: {len(tags_removed)}")
    for pid, nm, a in tags_removed[:8]: print(f"    − id={pid} {nm[:50]} :: {a}")

if __name__ == '__main__': main()
