#!/usr/bin/env python3
"""
Seed docs/state/invoice_archive.json from OCTOPOS purchase_orders_received.

Lauren 2026-05-21: "POs ב-OCTOPOS מדוייקים — בוא נעבור על כל ההזמנות ונעדכן
את המערכת לכל הספקים לפי זה". The PDF parser was a stepping stone; the
OCTOPOS PO data is the source of truth.

This REPLACES the entire invoice_archive.json. Going forward, every time
Lauren confirms a CMP_CTX invoice, it writes to OCTOPOS, which flows back
through this seed on next nightly sync.

Schema mapping (PO → archive entry):
  po.id                → invoice_number = "PO #168"
  po.received_date     → invoice_date (often null in OCTOPOS)
  po.vendor_invoice_number → if set, replaces "PO #N" with the real one
  po.total_cost        → total_usd
  po.items[i]          → lines[i] (with units, not displays — POs are in units)

Active products only — Lauren 2026-05-21 instruction.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

OCT_PATH = Path('docs/state/octopos_products.json')
ARCH_PATH = Path('docs/state/invoice_archive.json')

def main():
    oct = json.loads(OCT_PATH.read_text())
    # Build set of active product ids per supplier (active = in `products`, not `inactive_products`)
    active_pids = {}
    for code, vd in oct['vendors'].items():
        active_pids[code] = set(str(p.get('id')) for p in (vd.get('products') or []))
    archive = {
        '_updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        '_about': 'Built from OCTOPOS purchase_orders_received. Source of truth for per-product order history. Lauren 2026-05-21.',
        '_source': 'octopos_pos',
        'invoices': {}
    }
    total_pos = 0; total_lines_in = 0; total_lines_kept = 0; total_lines_skipped = 0
    for code, vd in oct['vendors'].items():
        pos = vd.get('purchase_orders_received') or []
        if not pos: continue
        entries = []
        active_for_sup = active_pids.get(code, set())
        for po in pos:
            items = po.get('items') or []
            kept = []
            for it in items:
                pid = str(it.get('product_id'))
                total_lines_in += 1
                if pid not in active_for_sup:
                    total_lines_skipped += 1
                    continue   # inactive products excluded per Lauren
                qty = float(it.get('quantity') or 0)
                cost = float(it.get('cost_unit') or 0)
                line_total = round(qty * cost, 2)
                kept.append({
                    'sku': it.get('product_sku') or '',
                    'name': it.get('product_name') or '',
                    'raw_qty': qty,           # OCTOPOS qty is already in UNITS
                    'pack_size': 1,           # No pack-multiplication needed
                    'unit_qty': qty,
                    'raw_price': cost,
                    'unit_price': cost,
                    'total': line_total,
                    'matched_product_id': int(it.get('product_id')) if it.get('product_id') else None,
                })
                total_lines_kept += 1
            if not kept: continue   # no active lines → skip the whole PO
            po_id = po.get('id')
            entries.append({
                'invoice_number': po.get('vendor_invoice_number') or f"PO #{po_id}",
                'invoice_date': po.get('received_date'),  # often null in OCTOPOS
                'po_id': po_id,
                'po_status': po.get('status') or 'Received',
                'parsed_at': archive['_updated_at'],
                'source': 'octopos_po',
                'total_usd': round(sum(L['total'] for L in kept), 2),
                'line_count': len(kept),
                'matched_count': len(kept),  # all by definition (already filtered)
                'lines': kept,
            })
            total_pos += 1
        # Sort newest-first by po_id desc (since dates are usually null)
        entries.sort(key=lambda e: -(e['po_id'] or 0))
        archive['invoices'][code] = entries

    # ─── BACKORDERS: ordered but not delivered ─────────────────────────────────
    # Source A: open Generated POs (status not "Received") — supplier has the
    # order, hasn't shipped. After ~21 days these are likely backordered.
    # Source B: CMP_CTX diff entries marked 'missing' from reconciled invoices
    # (lines on Lauren's PO that didn't appear on the supplier's invoice).
    backorders = {}  # supplier_code → list of {product_id, sku, name, qty, source, ref, date}
    today = datetime.now(timezone.utc).date()
    for code, vd in oct['vendors'].items():
        sup_back = []
        # Source A: open generated POs
        for po in (vd.get('purchase_orders_generated') or []):
            for it in (po.get('items') or []):
                pid = str(it.get('product_id'))
                if pid not in active_pids.get(code, set()): continue
                sup_back.append({
                    'product_id': int(it.get('product_id')) if it.get('product_id') else None,
                    'sku': it.get('product_sku') or '',
                    'name': it.get('product_name') or '',
                    'qty_ordered': float(it.get('quantity') or 0),
                    'unit_cost': float(it.get('cost_unit') or 0),
                    'source': 'generated_po',
                    'po_id': po.get('id'),
                    'date': po.get('received_date'),   # usually null
                    'note': f"PO #{po.get('id')} ב-OCTOPOS (סטטוס: Generated — לא קיבל ספק עדיין)",
                })
        if sup_back:
            backorders[code] = sup_back
    # Source B: read CMP_CTX 'missing' lines from inventory_orders.json
    inv_orders_path = Path('docs/state/inventory_orders.json')
    if inv_orders_path.exists():
        try:
            state = json.loads(inv_orders_path.read_text())
            for evkey, ev in (state.get('events') or {}).items():
                for sc, sd in (ev.get('suppliers') or {}).items():
                    pi = sd.get('_pending_invoice')
                    if not pi or not sd.get('invoice_compared_at'): continue
                    # For each 'missing' kind in the diff — these are PO lines NOT on the invoice
                    for diff_row in (pi.get('diff') or []):
                        if diff_row.get('kind') != 'missing': continue
                        po_line = diff_row.get('po_line') or {}
                        pid = po_line.get('product_id')
                        if not pid: continue
                        # Active only
                        if str(pid) not in active_pids.get(sc, set()): continue
                        backorders.setdefault(sc, []).append({
                            'product_id': pid,
                            'sku': po_line.get('product_sku') or '',
                            'name': po_line.get('product_name') or '',
                            'qty_ordered': float(po_line.get('quantity') or 0),
                            'unit_cost': float(po_line.get('cost_unit') or 0),
                            'source': 'invoice_missing',
                            'po_id': None,
                            'date': sd.get('invoice_compared_at', '')[:10],
                            'note': f"הוזמן באירוע {evkey}, לא הגיע בחשבונית של {sc}",
                        })
        except Exception as e: print(f"  ⚠ backorder source-B parse failed: {e}")
    archive['backorders'] = backorders
    total_back = sum(len(v) for v in backorders.values())
    print(f"\n→ backorders: {total_back} entries across {len(backorders)} suppliers")
    for sc, items in sorted(backorders.items(), key=lambda x: -len(x[1])):
        srcs = {}
        for i in items: srcs[i['source']] = srcs.get(i['source'], 0) + 1
        src_str = ', '.join(f"{k}={v}" for k, v in srcs.items())
        print(f"  {sc:<20} {len(items):>3} backorders ({src_str})")

    ARCH_PATH.write_text(json.dumps(archive, indent=2, ensure_ascii=False))
    print(f"✓ wrote {ARCH_PATH}")
    print(f"  {total_pos} POs across {len(archive['invoices'])} suppliers")
    print(f"  {total_lines_kept} active line items / {total_lines_in} total ({total_lines_skipped} inactive skipped)")
    print()
    print(f"{'supplier':<22} {'POs':>5} {'lines':>7}")
    print('-'*40)
    for sc, entries in sorted(archive['invoices'].items(), key=lambda x: -sum(e['line_count'] for e in x[1])):
        n_lines = sum(e['line_count'] for e in entries)
        print(f"  {sc:<20} {len(entries):>5} {n_lines:>7}")

if __name__ == '__main__': main()
