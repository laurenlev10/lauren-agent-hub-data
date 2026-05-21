#!/usr/bin/env python3
"""
Invoice archive ingestion. Takes a PDF path + supplier_code, parses the PDF
using the same heuristics as the dashboard parser, extracts invoice metadata
+ line items, matches each line to an OCTOPOS product, and merges into
docs/state/invoice_archive.json.

Usage: invoice_archive_ingest.py <supplier_code> <pdf_path> [pdf2 pdf3 ...]
Example: invoice_archive_ingest.py bb-and-w /uploads/5.18.2026\\ Roseville.pdf
"""
import sys, json, re, os
from pathlib import Path
from datetime import datetime, timezone

ARCHIVE_PATH = Path('docs/state/invoice_archive.json')
OCTOPOS_PATH = Path('docs/state/octopos_products.json')
RULES_PATH   = Path('docs/state/product_rules.json')
RECV_STATE_PATH = Path('docs/state/inventory_receive_state.json')

# ─── Parser helpers (Python ports of the dashboard's JS) ────────────────────
def collapse_doubled_chars(token):
    """If a token has >=60% of consecutive char-pairs identical (pdfplumber faux-bold
    pattern: 'LLIIPPSSTTIICCKK' = 'LIPSTICK' with every char doubled), collapse pairs."""
    n = len(token)
    if n < 4: return token
    half = n // 2
    pairs_match = sum(1 for i in range(0, n - 1, 2) if token[i] == token[i+1])
    if pairs_match / max(1, half) >= 0.6:
        # take every-other char (handles odd-length by keeping the dangling last char)
        out = ''.join(token[i] for i in range(0, n, 2))
        return out
    return token

def dedup_drop_shadow(s):
    """Collapse 'BB&BB&' → 'BB&', 'CrCr' → 'Cr' etc. + word-level dup + char-pair-doubling."""
    if not s: return s
    # Char-pair-doubling pass per-token first (pdfplumber-specific pattern)
    s = ' '.join(collapse_doubled_chars(t) for t in s.split(' '))
    prev = None
    while prev != s:
        prev = s
        # word-level: 'X X' → 'X', up to 8 words
        s = re.sub(r'\b(\S+)(?:\s+\1)\b', r'\1', s, flags=re.I)
        for n in range(2, 9):
            pat = r'((?:\S+\s+){' + str(n-1) + r'}\S+)\s+\1\b'
            s = re.sub(pat, r'\1', s, flags=re.I)
        # intra-word doubling: 'BB&BB&' → 'BB&'
        s = re.sub(r'([A-Za-z&]{2,6})\1', r'\1', s)
    return s

def is_contact_info_row(s):
    u = s.upper()
    if re.search(r'\bBUSINESS\s+NUMBER\b', u): return True
    if re.search(r'\bP\.?\s*O\.?\s*BOX\b', u): return True
    if re.search(r'(WWW\.|HTTPS?://)', s, re.I): return True
    if re.search(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', s, re.I): return True
    if re.search(r'\b(PHONE|TEL|FAX|MOBILE|CELL)\b\s*[:#]?\s*\+?\d', s, re.I): return True
    if re.search(r'\b[A-Z]{2}\s+\d{5}(-\d{4})?\b', u): return True
    if re.match(r'^\s*\+?\d?\s*\(\d{3}\)\s*\d{3}[\s\-\.`]+\d{4}\s*$', s): return True
    if re.search(r'\$[\d.]+\s*/\s*(pc|piece|display|dozen|dz|box|case|pack)\b', s, re.I): return True
    if re.search(r'\bper\s+(piece|pc|display|dozen|pack)\b', s, re.I): return True
    # Shipping / freight rows (BBW invoices end with a "UPS" or "Shipping" line)
    if re.match(r'^\s*(ups|fedex|usps|dhl|shipping|freight|delivery|handling)\b', s, re.I): return True
    return False

def parse_invoice_rows(row_strings):
    """Same heuristic as the JS parseInvoiceRows — finds (qty, price, total) triple per row."""
    lines = []
    for row in row_strings:
        if is_contact_info_row(row): continue
        # SKU regex — allow & in body
        sku = ''
        for m in re.finditer(r'\b([A-Z][A-Z0-9&]*(?:[-/][A-Z0-9&]+)*)\b', row.upper()):
            cand = m.group(1)
            has_digit = bool(re.search(r'\d', cand))
            has_amp = '&' in cand
            has_letter = bool(re.search(r'[A-Z]', cand))
            if len(cand) >= 3 and (has_digit or has_amp) and has_letter:
                sku = cand; break
        nums = []
        for m in re.finditer(r'\b(\d+(?:\.\d+)?)\b', row):
            nums.append({'raw': m.group(1), 'val': float(m.group(1))})
        if len(nums) < 3: continue
        candidates = []
        for i in range(len(nums)):
            qty = nums[i]['val']
            if qty < 1 or abs(qty - round(qty)) > 0.01 or qty > 99999: continue
            for j in range(len(nums)):
                if j == i: continue
                price = nums[j]['val']
                if price <= 0 or price > 5000: continue
                for k in range(len(nums)):
                    if k == i or k == j: continue
                    tot = nums[k]['val']
                    expected = qty * price
                    if abs(expected - tot) < max(0.05, expected * 0.01):
                        candidates.append({'qty': round(qty), 'price': price, 'total': tot,
                                           'price_raw': nums[j]['raw']})
        if not candidates: continue
        candidates.sort(key=lambda c: (-c['total'],
                                       -int('.' in c['price_raw']),
                                       -c['price']))
        found = candidates[0]
        # Pack-size detection
        pack = 1
        ru = row.upper()
        if (m := re.search(r'TOTAL\s+(\d+)\s*PCS', ru)): pack = int(m.group(1))
        elif (m := re.search(r'\((\d+)\s*PCS?\s*\+\s*(\d+)\s*FREE', ru)): pack = int(m.group(1)) + int(m.group(2))
        elif (m := re.search(r'\((\d+)\s*/\s*CASE\)', ru)): pack = int(m.group(1))
        elif (m := re.search(r'\((\d+)\s*PCS?\s*[X×*]\s*\d+\s*PACK', ru)): pack = int(m.group(1))
        elif re.search(r'\b(DZ|DOZ|DOZEN)\b', ru): pack = 12
        elif re.search(r'\bGROSS\b', ru): pack = 144
        # Name
        name_raw = row
        if sku: name_raw = re.sub(re.escape(sku), ' ', name_raw, flags=re.I)
        name_raw = re.sub(r'\s+', ' ', name_raw).strip()
        lines.append({'sku': sku, 'name': name_raw, 'raw_qty': found['qty'], 'pack_inline': pack,
                      'price': found['price'], 'total': found['total']})
    return lines

def extract_pdf_rows(pdf_path):
    """Use pdfplumber to get words with (x, y) positions. Group by Y, sort by X, join.
    Then apply dedup_drop_shadow. Same approach as the JS dashboard parser."""
    import pdfplumber
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
            if not words: continue
            # Group by top (Y) coord; tolerance 3
            row_groups = []
            for w in words:
                row = None
                for r in row_groups:
                    if abs(r['top'] - w['top']) < 3:
                        row = r; break
                if row is None:
                    row = {'top': w['top'], 'words': []}
                    row_groups.append(row)
                row['words'].append(w)
            row_groups.sort(key=lambda r: r['top'])
            for r in row_groups:
                r['words'].sort(key=lambda w: w['x0'])
                joined = ' '.join(w['text'] for w in r['words'])
                joined = re.sub(r'\s+', ' ', joined).strip()
                if joined:
                    rows.append(dedup_drop_shadow(joined))
    return rows

def extract_invoice_meta(row_strings, filename=''):
    """Find invoice_number + date from the doc."""
    invoice_number, invoice_date = None, None
    for r in row_strings:
        if not invoice_number:
            # Prefer prefixed form ("EST0078", "INV1234")
            if (m := re.search(r'\b((?:EST|INV|INVOICE)\d+)\b', r, re.I)):
                invoice_number = m.group(1).upper()
            else:
                m = re.search(r'\b(?:EST|INV|INVOICE)[\s#:]+\s*(\d{3,10})\b', r, re.I)
                if m: invoice_number = m.group(1)
        if not invoice_date:
            # Various date formats
            for pat in [r'\b(\d{4}-\d{2}-\d{2})\b',
                        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(\d{4})\b',
                        r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b']:
                if (m := re.search(pat, r, re.I)):
                    try:
                        if pat.startswith(r'\b(\d{4}'): invoice_date = m.group(1)
                        elif 'Jan' in pat:
                            mon_map = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
                            mon = mon_map[m.group(1)[:3].lower()]
                            invoice_date = f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
                        else:
                            mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
                            if yr < 100: yr += 2000
                            invoice_date = f"{yr}-{mo:02d}-{dy:02d}"
                        break
                    except: pass
    # Fallback: try filename like "5.18.2026 Roseville.pdf"
    if not invoice_date and filename:
        if (m := re.search(r'(\d{1,2})[\.\-/](\d{1,2})[\.\-/](\d{4})', filename)):
            mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            invoice_date = f"{yr}-{mo:02d}-{dy:02d}"
    return invoice_number, invoice_date

def load_state():
    octopos = json.loads(OCTOPOS_PATH.read_text())
    rules   = json.loads(RULES_PATH.read_text())['rules']
    sps     = json.loads(RECV_STATE_PATH.read_text()).get('supplier_pack_sizes', {})
    return octopos, rules, sps

def get_pack(pid, sc, rules, sps):
    r = rules.get(str(pid), {}) if pid else {}
    if r.get('pack_size', 0) > 1: return int(r['pack_size'])
    if r.get('min_display', 0) > 1: return int(r['min_display'])
    if sc and sps.get(sc, 0) > 1: return int(sps[sc])
    return 1

def _norm_tokens(s):
    return set(t for t in re.split(r'\W+', (s or '').lower()) if len(t) >= 3)

def match_product(line, supplier_code, octopos_vendors):
    """Match invoice line to OCTOPOS product: SKU-first then name similarity."""
    sku_norm = re.sub(r'[^A-Z0-9]', '', (line.get('sku') or '').upper())
    inv_name = line.get('name') or ''
    inv_tokens = _norm_tokens(inv_name)
    inv_full = re.sub(r'[^a-z0-9]', '', inv_name.lower())
    # Stage 1: SKU exact / prefix match within supplier
    sku_hit = None
    name_candidates = []
    for code, vd in octopos_vendors.items():
        if code != supplier_code: continue
        # Active products only — Lauren 2026-05-21: "להתייחס רק למוצרים של ACTIVE"
        for p in (vd.get('products') or []):
            psku = re.sub(r'[^A-Z0-9]', '', (p.get('sku') or '').upper())
            if sku_norm and psku and not sku_hit:
                if psku == sku_norm:
                    sku_hit = p.get('id')
                    continue
                # prefix match (handles "BB&WL" ↔ "BB&W-LL" → BBWL ↔ BBWLL)
                if len(sku_norm) >= 4 and len(psku) >= 4:
                    if psku.startswith(sku_norm) or sku_norm.startswith(psku):
                        sku_hit = p.get('id')
                        continue
            # Stage 2: collect name-similarity scores
            po_tokens = _norm_tokens(p.get('name'))
            if inv_tokens and po_tokens:
                inter = inv_tokens & po_tokens
                uni = inv_tokens | po_tokens
                j = len(inter) / max(1, len(uni))
                # 2nd-chance: distinctive token substring
                po_full = re.sub(r'[^a-z0-9]', '', (p.get('name') or '').lower())
                # Variant-tail boost: when a product family shares a long boilerplate
                # prefix (e.g. "BBW Creamy Stain Liner Pencil - ..."), the distinctive
                # signal is in the SUFFIX (variant name). Check if the last 8-15 chars
                # of po_full appear in inv_full — that's strong evidence we found the
                # right variant. Score 0.85 (above any boilerplate-only Jaccard).
                for tail_len in (15, 12, 10, 8):
                    if len(po_full) >= tail_len:
                        po_tail = po_full[-tail_len:]
                        if po_tail in inv_full:
                            j = max(j, 0.85); break
                if j < 0.4:
                    # 2nd-chance: count UNIQUE shared distinctive tokens (5+ chars).
                    # Counting hits bidirectionally would double-count the same shared
                    # boilerplate word ("liner") as 2 hits — false-matching Love Deeply
                    # to "Splashed Liner Vol 3". Set union dedupes correctly.
                    shared = set(t for t in inv_tokens if len(t) >= 5 and t in po_full)
                    shared |= set(t for t in po_tokens if len(t) >= 5 and t in inv_full)
                    if len(shared) >= 2: j = max(j, min(0.45, 0.4 + 0.025 * len(shared)))
                if j >= 0.4: name_candidates.append((j, p.get('id')))
    if sku_hit: return sku_hit
    if name_candidates:
        name_candidates.sort(reverse=True)
        return name_candidates[0][1]
    return None

def ingest_one(supplier_code, pdf_path, archive, octopos, rules, sps):
    pdf_path = Path(pdf_path)
    print(f"\n→ ingesting {pdf_path.name} for {supplier_code}")
    row_strings = extract_pdf_rows(str(pdf_path))
    print(f"  extracted {len(row_strings)} rows from PDF")
    inv_num, inv_date = extract_invoice_meta(row_strings, pdf_path.name)
    print(f"  invoice_number={inv_num} invoice_date={inv_date}")
    raw_lines = parse_invoice_rows(row_strings)
    print(f"  parsed {len(raw_lines)} line items")
    # Match each to OCTOPOS + compute unit qty/price
    lines = []
    total = 0
    for L in raw_lines:
        pid = match_product(L, supplier_code, octopos['vendors'])
        pack = max(get_pack(pid, supplier_code, rules, sps), L.get('pack_inline', 1))
        unit_qty = L['raw_qty'] * pack
        unit_price = L['price'] / pack if pack > 0 else L['price']
        lines.append({
            'sku': L['sku'], 'name': L['name'],
            'raw_qty': L['raw_qty'], 'pack_size': pack, 'unit_qty': unit_qty,
            'raw_price': round(L['price'], 4), 'unit_price': round(unit_price, 4),
            'total': L['total'],
            'matched_product_id': pid,
        })
        total += L['total']
    invoice_key = inv_num or pdf_path.stem
    invoices = archive.setdefault('invoices', {}).setdefault(supplier_code, [])
    # Dedup by (invoice_number OR filename)
    invoices[:] = [i for i in invoices if i.get('invoice_number') != inv_num or i.get('source_filename') != pdf_path.name]
    invoices.append({
        'invoice_number': inv_num,
        'invoice_date': inv_date,
        'parsed_at': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'source_filename': pdf_path.name,
        'total_usd': round(total, 2),
        'line_count': len(lines),
        'matched_count': sum(1 for l in lines if l['matched_product_id']),
        'lines': lines,
    })
    invoices.sort(key=lambda x: (x.get('invoice_date') or '', x.get('invoice_number') or ''), reverse=True)
    print(f"  ✓ added invoice {invoice_key} (${total:.2f}, {sum(1 for l in lines if l['matched_product_id'])}/{len(lines)} lines matched)")

def main(argv):
    if len(argv) < 3:
        print(__doc__); sys.exit(1)
    supplier_code = argv[1]
    pdf_paths = argv[2:]
    archive = {'_updated_at': None, '_about': 'Per-supplier invoice archive built by uploading PDFs.', 'invoices': {}}
    if ARCHIVE_PATH.exists(): archive = json.loads(ARCHIVE_PATH.read_text())
    octopos, rules, sps = load_state()
    failures = []
    for pdf in pdf_paths:
        try:
            ingest_one(supplier_code, pdf, archive, octopos, rules, sps)
        except Exception as e:
            print(f"  ✗ FAILED: {Path(pdf).name} — {e}")
            failures.append((pdf, str(e)))
    if failures:
        print(f"\n⚠ {len(failures)} file(s) failed:")
        for pdf, err in failures: print(f"  • {Path(pdf).name}: {err[:80]}")
    archive['_updated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    ARCHIVE_PATH.parent.mkdir(exist_ok=True, parents=True)
    ARCHIVE_PATH.write_text(json.dumps(archive, indent=2, ensure_ascii=False))
    print(f"\n✓ wrote {ARCHIVE_PATH} — {sum(len(v) for v in archive['invoices'].values())} invoices total")

if __name__ == '__main__': main(sys.argv)
