#!/usr/bin/env python3
"""
@profit-pulse — Phase 1 implementation.

Per-product margin analyzer. Reads the daily OCTOPOS snapshot
(docs/state/octopos_products.json), computes markup/margin/band for every
active mapped product, suggests a tier-rounded price that gets each
product back to Lauren's North-Star markup of 3.0×, and writes the result
to docs/state/profit_pulse.json.

Phase 1 SCOPE (this script):
- Reads existing snapshot only — does NOT re-call OCTOPOS.
- Computes markup, margin_pct, margin_usd, profit_band per product.
- Suggests a standard event-tier price.
- Preserves Lauren's lauren_decision / lauren_note / lauren_decided_at
  fields (browser-owned, never overwritten — IRON RULE #7).
- Appends to history[] (cap 26 ≈ 1 year of weekly snapshots).
- Appends a summary entry to biweekly_runs[] (cap 26).

OUT OF SCOPE (deferred — Phase 2):
- Auto-PUT approved price raises back to OCTOPOS.
- Cross-join with @slow-movers velocity (profit_per_event_week_4w).
  We populate that field at 0 for now since slow_movers.json is empty
  until the @slow-movers agent ships its first real run.

Spec source: Scheduled/NEW/profit-pulse/scoring.md
"""
import json, sys, math, os
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

REPO_ROOT       = Path(__file__).resolve().parent.parent
OCTOPOS_PATH    = REPO_ROOT / "docs" / "state" / "octopos_products.json"
PROFIT_PATH     = REPO_ROOT / "docs" / "state" / "profit_pulse.json"
SLOW_PATH       = REPO_ROOT / "docs" / "state" / "slow_movers.json"

TARGET_MARKUP   = 3.0
TIERS           = [1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 50]
HISTORY_CAP     = 26
RUNS_CAP        = 26


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_biweek(today=None):
    today = today or datetime.now(timezone.utc).date()
    return today.strftime("%G-W%V")


def suggest_price(unit_cost, target_markup=TARGET_MARKUP):
    """Return the smallest standard tier >= unit_cost * target_markup.
    Above the highest tier, round up to next $10. None for invalid input."""
    if not unit_cost or unit_cost <= 0:
        return None
    target = unit_cost * target_markup
    for tier in TIERS:
        if tier >= target:
            return tier
    return int(math.ceil(target / 10) * 10)


def markup_to_band(markup):
    if markup < 1.0:  return "loss"
    if markup < 1.5:  return "critical"
    if markup < 2.0:  return "low"
    if markup < 3.0:  return "ok"
    return "strong"


def score_margin(unit_cost, sale_price):
    """Return scoring dict or {'profit_band': 'data_issue', 'skip': True} for bad data."""
    if not unit_cost or unit_cost <= 0 or not sale_price or sale_price <= 0:
        return {"profit_band": "data_issue", "skip": True,
                "unit_cost": unit_cost or 0.0, "sale_price": sale_price or 0.0}
    markup     = sale_price / unit_cost
    margin_pct = (sale_price - unit_cost) / sale_price * 100
    margin_usd = sale_price - unit_cost
    band       = markup_to_band(markup)
    sp         = suggest_price(unit_cost)
    reason = (f"target = ${unit_cost:.2f} × {TARGET_MARKUP} = ${unit_cost*TARGET_MARKUP:.2f}; "
              f"rounded up to standard tier")
    return {
        "unit_cost":  round(unit_cost, 2),
        "sale_price": round(sale_price, 2),
        "markup":     round(markup, 2),
        "margin_pct": round(margin_pct, 1),
        "margin_usd": round(margin_usd, 2),
        "profit_band": band,
        "suggested_price": sp,
        "suggested_price_reason": reason,
    }


def should_propose_change(current_price, suggested):
    """Stability filter — only propose if change is >= $1 AND >= 10%."""
    if suggested is None: return False
    if current_price <= 0: return True
    diff = abs(suggested - current_price)
    pct  = diff / current_price
    return diff >= 1.0 and pct >= 0.10


def load_json(path, default):
    if not path.exists(): return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: failed to load {path}: {e}", file=sys.stderr)
        return default


def main():
    print(f"@profit-pulse Phase 1 — starting at {now_iso()}")

    octo = load_json(OCTOPOS_PATH, {})
    if not octo or not octo.get("vendors"):
        print("ERROR: octopos_products.json missing or empty. Has the daily snapshot run yet?", file=sys.stderr)
        sys.exit(1)
    print(f"  loaded octopos snapshot: _updated_at={octo.get('_updated_at')}")

    prev_state  = load_json(PROFIT_PATH, {})
    prev_products = prev_state.get("products", {}) or {}
    print(f"  loaded prior profit_pulse.json: {len(prev_products)} products previously tracked")

    slow_state    = load_json(SLOW_PATH, {})
    slow_products = (slow_state or {}).get("products", {}) or {}
    print(f"  loaded slow_movers.json: {len(slow_products)} products with velocity data")

    week = iso_biweek()

    band_counts = Counter()
    new_products = {}
    n_processed  = 0
    n_propose    = 0

    for code, vdata in (octo.get("vendors") or {}).items():
        supplier_name = vdata.get("display_name") or vdata.get("name") or code
        for p in (vdata.get("products") or []):
            if not p.get("active", True):
                continue
            pid = str(p["id"])
            unit_cost  = float(p.get("unit_cost")  or 0)
            sale_price = float(p.get("sale_price") or 0)

            scored = score_margin(unit_cost, sale_price)
            band   = scored.get("profit_band", "data_issue")
            band_counts[band] += 1
            n_processed += 1

            prev_rec = prev_products.get(pid, {}) or {}

            # History append (cap)
            history = list(prev_rec.get("history", []) or [])
            history.append({
                "week":       week,
                "sale_price": scored.get("sale_price"),
                "unit_cost":  scored.get("unit_cost"),
                "markup":     scored.get("markup"),
                "band":       band,
            })
            history = history[-HISTORY_CAP:]

            # Velocity join — Phase 2 will activate this fully
            slow_rec = slow_products.get(pid, {}) or {}
            vel = slow_rec.get("avg_sold_per_event_week_4w") or 0
            profit_pew = round((scored.get("margin_usd") or 0) * (vel or 0), 2)

            # Stability flag — don't badge the dashboard with sub-$1/<10% suggestions
            propose = should_propose_change(sale_price, scored.get("suggested_price"))
            if propose: n_propose += 1

            new_rec = {
                "id":   p.get("id"),
                "sku":  p.get("sku") or "",
                "name": p.get("name") or "",
                "supplier": supplier_name,
                **{k: v for k, v in scored.items() if k != "skip"},
                "should_propose_change": propose,
                "profit_per_event_week_4w": profit_pew,
                "history": history,
                # Preserve Lauren's decisions — NEVER overwrite (IRON RULE #7)
                "lauren_decision":    prev_rec.get("lauren_decision"),
                "lauren_note":        prev_rec.get("lauren_note", ""),
                "lauren_decided_at":  prev_rec.get("lauren_decided_at"),
                "price_history":      prev_rec.get("price_history", []),
            }
            new_products[pid] = new_rec

    # Build the run summary
    run_summary = {
        "iso_biweek": week,
        "ran_at":     now_iso(),
        "products_evaluated": n_processed,
        "n_strong":   band_counts.get("strong", 0),
        "n_ok":       band_counts.get("ok", 0),
        "n_low":      band_counts.get("low", 0),
        "n_critical": band_counts.get("critical", 0) + band_counts.get("loss", 0),
        "n_loss":     band_counts.get("loss", 0),
        "n_data_issue": band_counts.get("data_issue", 0),
        "n_propose_change": n_propose,
    }

    biweekly_runs = list(prev_state.get("biweekly_runs", []) or [])
    biweekly_runs.append(run_summary)
    biweekly_runs = biweekly_runs[-RUNS_CAP:]

    out = {
        "_updated_at":  now_iso(),
        "_about":       ("Owner: @profit-pulse. Consumers: @inventory-orders Wizard, /recount/ 💰 tab. "
                         "Lauren writes lauren_decision via 💰 tab (GitHub Contents API per IRON RULE #7)."),
        "_schema_version": 1,
        "_target_markup":  TARGET_MARKUP,
        "_tiers":          TIERS,
        "biweekly_runs":   biweekly_runs,
        "products":        new_products,
    }

    PROFIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(out, ensure_ascii=False, indent=2)
    PROFIT_PATH.write_text(content, encoding="utf-8")
    print(f"\n✓ wrote {PROFIT_PATH} ({len(content):,} bytes)")
    print(f"  products evaluated: {n_processed}")
    print(f"    🟢 strong    : {run_summary['n_strong']:>4}")
    print(f"    🟡 ok        : {run_summary['n_ok']:>4}")
    print(f"    🟠 low       : {run_summary['n_low']:>4}")
    print(f"    🔴 critical  : {band_counts.get('critical',0):>4}")
    print(f"    ⚫ loss      : {run_summary['n_loss']:>4}")
    print(f"    ⚠  data_issue: {run_summary['n_data_issue']:>4}")
    print(f"    proposed price changes (≥$1 & ≥10%): {n_propose}")


if __name__ == "__main__":
    main()
