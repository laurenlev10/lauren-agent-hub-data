#!/usr/bin/env python3
"""
Order recommendation engine — runs after weekend_recap.py.

For every active product:
  avg_sales = average of last 4 weekend sales (Tier 1 if available),
              otherwise average qty across recent POs (Tier 2 fallback),
              otherwise just the threshold (Tier 3 fallback).
  target    = max(threshold, avg_sales × 1.2)  -- 20% safety margin
  gap       = target - current_stock_post_event
  rec_qty   = round_up_to_pack(gap, pack_size)  -- multiple of 12 typically

Writes docs/state/order_recommendations.json keyed by supplier, listing
per-product recommendation with reasoning string so Lauren can audit.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

OCT_PATH = Path('docs/state/octopos_products.json')
WSALES_PATH = Path('docs/state/weekend_sales.json')
ARCH_PATH = Path('docs/state/invoice_archive.json')
RULES_PATH = Path('docs/state/product_rules.json')
RECV_PATH = Path('docs/state/inventory_receive_state.json')
REC_PATH = Path('docs/state/order_recommendations.json')

def get_pack(pid, sc, rules, sps):
    r = rules.get(str(pid), {})
    if r.get('pack_size', 0) > 1: return int(r['pack_size'])
    if r.get('min_display', 0) > 1: return int(r['min_display'])
    if sc and sps.get(sc, 0) > 1: return int(sps[sc])
    return 12   # safe default — almost everything ships in dozens

def round_up_to_pack(qty, pack):
    if qty <= 0: return 0
    if pack <= 1: return int(qty)
    return int(((int(qty) + pack - 1) // pack) * pack)

def main():
    octopos = json.loads(OCT_PATH.read_text())
    wsales  = json.loads(WSALES_PATH.read_text()) if WSALES_PATH.exists() else {'products': {}}
    archive = json.loads(ARCH_PATH.read_text()) if ARCH_PATH.exists() else {'invoices': {}}
    rules   = json.loads(RULES_PATH.read_text())['rules']
    sps     = json.loads(RECV_PATH.read_text()).get('supplier_pack_sizes', {})

    # Build pid → recent-PO-history (for Tier 2 fallback)
    po_qty_by_pid = {}
    for sc, invs in archive.get('invoices', {}).items():
        for inv in invs[:8]:   # last 8 POs per supplier
            for L in inv.get('lines') or []:
                pid = str(L.get('matched_product_id') or '')
                if pid: po_qty_by_pid.setdefault(pid, []).append(L.get('unit_qty', 0))

    recs = {
        '_updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        '_about': 'Per-product order recommendations for the next event. Updated weekly.',
        'suppliers': {}
    }
    sum_recs = 0
    for code, vd in octopos['vendors'].items():
        sup_recs = []
        for p in (vd.get('products') or []):
            pid = str(p['id'])
            # Tier 1: weekend sales avg (when available)
            wprod = (wsales.get('products') or {}).get(pid, {})
            recent_sales = [w.get('sold') for w in (wprod.get('weekends') or [])[-4:] if w.get('sold') not in (None,)]
            tier = None; avg_sales = 0; reasoning = []
            if recent_sales:
                avg_sales = sum(recent_sales) / len(recent_sales)
                tier = 'weekend_sales'
                reasoning.append(f"{len(recent_sales)} סופ\"ש אחרונים: {recent_sales} → ממוצע {avg_sales:.1f}")
            elif po_qty_by_pid.get(pid):
                po_q = po_qty_by_pid[pid][:6]
                avg_sales = sum(po_q) / len(po_q)
                tier = 'po_history'
                reasoning.append(f"היסטוריית POs: {len(po_q)} אחרונות, ממוצע {avg_sales:.0f}u")
            threshold = float(p.get('threshold') or 0)
            stock = float(p.get('in_stock_qty') or 0)
            target = max(threshold, avg_sales * 1.2)
            gap = target - stock
            pack = get_pack(pid, code, rules, sps)
            qty = round_up_to_pack(gap, pack)
            if qty <= 0: continue   # nothing to order
            reasoning.append(f"target={target:.0f} (max of thr {threshold:.0f} / avg×1.2 {avg_sales*1.2:.0f}), stock={stock:.0f}, gap={gap:.0f} → round up to pack-{pack} = {qty}")
            sup_recs.append({
                'product_id': p['id'], 'sku': p.get('sku'), 'name': p.get('name'),
                'current_stock': stock, 'threshold': threshold,
                'avg_sales': round(avg_sales, 1), 'target': round(target, 0),
                'recommended_qty': qty, 'pack_size': pack,
                'unit_cost': float(p.get('unit_cost') or 0),
                'line_total': round(qty * float(p.get('unit_cost') or 0), 2),
                'tier': tier, 'reasoning': ' · '.join(reasoning),
            })
            sum_recs += 1
        sup_recs.sort(key=lambda x: -x['line_total'])
        if sup_recs:
            sup_total = round(sum(r['line_total'] for r in sup_recs), 2)
            recs['suppliers'][code] = {'total_usd': sup_total, 'lines': sup_recs}
    REC_PATH.write_text(json.dumps(recs, indent=2, ensure_ascii=False))
    print(f"✓ wrote {REC_PATH} — {sum_recs} recommendations across {len(recs['suppliers'])} suppliers")
    for sc, info in sorted(recs['suppliers'].items(), key=lambda x: -x[1]['total_usd']):
        print(f"  {sc:<22} {len(info['lines']):>3} products · ${info['total_usd']:>9.2f}")

if __name__ == '__main__': main()
