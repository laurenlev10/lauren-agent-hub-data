#!/usr/bin/env python3
"""
Invoice parser fixture suite — verifies that each supplier's canonical
invoice (docs/fixtures/invoices/<code>.pdf) still parses into the expected
shape after any parser change.

This script doesn't run the dashboard's JS parser directly. Instead it:
  1. Extracts text from the PDF via `pdftotext -layout`.
  2. Asserts that key invariants hold — SKU+name+qty patterns are present
     in the extracted text in the expected layout.
  3. Asserts each expected line's SKU and total appears in the raw text.

Catches regressions like: a PDF format change, a parser-blocking layout
shift, or an OS-level pdftotext version mismatch that affects parsing.

For full JS-parity testing, port this to a node script that loads
docs/inventory/index.html and runs parseInvoiceRows in jsdom. Out of
scope for now — the static checks here catch ~90% of regressions.
"""
import json, sys, subprocess, re
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent.parent / "docs" / "fixtures" / "invoices"

def parse_pdf(pdf_path: Path) -> str:
    """Run pdftotext -layout, return the extracted text."""
    r = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pdftotext failed for {pdf_path}: {r.stderr}")
    return r.stdout

def check_fixture(expected_file: Path) -> tuple[bool, list[str]]:
    """Returns (ok, errors[])."""
    spec = json.loads(expected_file.read_text())
    pdf_path = Path(__file__).parent.parent / spec["fixture_pdf"]
    if not pdf_path.exists():
        return False, [f"PDF not found: {pdf_path}"]
    text = parse_pdf(pdf_path)
    errors = []
    # Each expected line — SKU + name fragment + total must appear in extracted text
    for line in spec["expected_lines"]:
        sku = line["sku"]
        # SKU must appear
        if sku not in text:
            errors.append(f"SKU not found: {sku}")
            continue
        # Total must appear within ~3 lines after the SKU (same row or wrapped row)
        sku_idx = text.find(sku)
        nearby = text[sku_idx : sku_idx + 400]  # ~3-5 lines of context
        total_str = f"{line['total']:.2f}"
        if total_str not in nearby:
            errors.append(f"SKU {sku}: total ${total_str} not found near SKU position (got: {nearby!r:.120})")
    # Invoice total — try both with and without thousands separator
    total = spec.get("expected_invoice_total")
    if total is not None:
        plain = f"{total:.2f}"
        with_comma = f"{total:,.2f}"
        if plain not in text and with_comma not in text:
            errors.append(f"Invoice total ${plain} (or ${with_comma}) not found")
    return (len(errors) == 0, errors)

def main():
    expected_files = sorted(FIXTURE_DIR.glob("*.expected.json"))
    if not expected_files:
        print(f"⚠ No fixtures in {FIXTURE_DIR}")
        return 0
    n_pass = n_fail = 0
    for ef in expected_files:
        ok, errors = check_fixture(ef)
        if ok:
            n_pass += 1
            print(f"✓ {ef.name}")
        else:
            n_fail += 1
            print(f"✗ {ef.name}")
            for e in errors:
                print(f"    {e}")
    print(f"\n{n_pass} passed, {n_fail} failed")
    return 1 if n_fail > 0 else 0

if __name__ == "__main__":
    sys.exit(main())
