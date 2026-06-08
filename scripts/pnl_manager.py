#!/usr/bin/env python3
"""pnl_manager.py — Manager field-report source for the automated event P&L.

Source C of 5 (see event_summary_BUILD_BRIEF.md). Reads docs/state/manager_reports.json
(written by the Cloudflare Worker, manager-report-worker.js) and extracts, for one event:

  - Staff  : Σ over team[] of (base + bonus + extra)  + official roster (who was on site)
  - Meals  : cash expenses whose description is food/meal-like
  - Other  : the remaining cash expenses (gas, supplies, misc)
  - Cash   : total_cash / deposit / register split → cross-check vs OCTOPOS cash

DECISION 2026-06-03: ALL cash flows through this form (no manual upload, no QB cash).
So Staff + Meals + Other here ARE the event's cash expenses. QB carries only non-cash
(flights / hotel / car / venue / marketing).

Envelope written by the Worker:
    { "_updated_at": ISO,
      "reports": { "<evkey>": [ report, report, ... ] },   # finals, appended
      "drafts":  { "<evkey>": report } }                   # one live draft per event

We take the LATEST non-draft report in reports[evkey]. If only a draft exists (or
nothing), we STOP and ask Lauren — a draft is not authoritative (cash-report-canonical).

Report schema (defined here; the form emits exactly this):
    evkey, manager_name, submitted_at (ISO), mode ("final"|"draft"),
    team:     [{name, base, bonus, extra}],
    expenses: [{desc, amount, category?}],   # category optional override
    cash:     {total_cash, deposit, register_coins, register_bills},
    answers:  {<question>: <answer>}, notes, photos:[urls]
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

MEAL_KEYWORDS = ("meal", "food", "lunch", "dinner", "breakfast", "coffee", "starbucks",
                 "restaurant", "pizza", "snack", "drink", "water", "catering",
                 "אוכל", "ארוחה", "קפה", "מסעדה")


def _find_state_file(explicit=None):
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve()
    cand = here.parent.parent / "docs/state/manager_reports.json"
    if cand.exists():
        return cand
    for c in Path("/").glob("**/docs/state/manager_reports.json"):
        return c
    return None  # not deployed yet — caller handles


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _classify(desc):
    d = (desc or "").lower()
    return "meals" if any(k in d for k in MEAL_KEYWORDS) else "other"


def fetch_manager_pnl(evkey, state_path=None, octopos_cash=None,
                      octopos_cash_min=None, octopos_cash_max=None):
    path = _find_state_file(state_path)
    if path is None or not Path(path).exists():
        return {"source": "manager_reports", "evkey": evkey, "found": False,
                "error": "manager_reports.json not found — form/Worker not deployed yet",
                "staff": 0.0, "meals": 0.0, "other": 0.0, "complete": False,
                "warnings": ["STOP: no manager report file. Deploy the form, or ask Lauren for the cash report."]}

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    reports = (data.get("reports") or {}).get(evkey) or []
    drafts = (data.get("drafts") or {})
    finals = [r for r in reports if str(r.get("mode", "final")).lower() != "draft"]

    if not finals:
        has_draft = evkey in drafts
        return {"source": "manager_reports", "evkey": evkey, "found": False,
                "error": "no FINAL manager report" + (" (only a draft)" if has_draft else ""),
                "staff": 0.0, "meals": 0.0, "other": 0.0, "complete": False,
                "warnings": ["STOP and ask Lauren: " + ("a draft exists but was never submitted as final."
                             if has_draft else "no manager report for this event yet.")]}

    # latest by submitted_at, else last appended
    def _key(r):
        return r.get("submitted_at") or ""
    rep = sorted(finals, key=_key)[-1]

    team = rep.get("team") or []
    roster = [t.get("name") for t in team if t.get("name")]
    def _member_pay(t):
        tot = _f(t.get("total"))
        return tot if tot else _f(t.get("base")) + _f(t.get("bonus")) + _f(t.get("extra"))
    staff = sum(_member_pay(t) for t in team)

    meals = other = 0.0
    expense_lines = []
    for e in (rep.get("expenses") or []):
        amt = _f(e.get("amount"))
        cat = (e.get("category") or _classify(e.get("desc"))).lower()
        if cat == "meals":
            meals += amt
        else:
            other += amt
        expense_lines.append({"desc": e.get("desc"), "amount": round(amt, 2), "category": cat})

    cash = rep.get("cash") or {}
    cash_total = _f(cash.get("total_cash"))

    warnings = []
    cash_check = None
    if octopos_cash is not None and cash_total:
        diff = cash_total - octopos_cash
        pct = abs(diff) / octopos_cash if octopos_cash else 0
        cash_check = {"manager_cash": round(cash_total, 2), "octopos_cash": round(octopos_cash, 2),
                      "diff": round(diff, 2), "pct": round(pct, 3)}
        # Range-based reconciliation (Lauren 2026-06-08 — split-ticket fix). OCTOPOS can't tell
        # us the cash vs card portion of a split ("CASH, VISA") ticket, so true cash is bounded:
        #   min = pure-CASH tickets ; max = min + all split-cash tickets (full value).
        # Only flag a mismatch when the manager's count falls OUTSIDE [min, max] (+/- 2% tol).
        # This kills the false "cash mismatch — investigate" that fired on every event with
        # split tickets (e.g. Omaha: octo full-sum $14,017 vs true cash $13,103 = $914 card portion).
        if octopos_cash_min is not None and octopos_cash_max is not None and octopos_cash_max:
            tol = 0.02
            lo = octopos_cash_min * (1 - tol)
            hi = octopos_cash_max * (1 + tol)
            cash_check["octopos_cash_min"] = round(octopos_cash_min, 2)
            cash_check["octopos_cash_max"] = round(octopos_cash_max, 2)
            cash_check["in_range"] = bool(lo <= cash_total <= hi)
            if not cash_check["in_range"]:
                warnings.append(f"cash mismatch: manager ${cash_total:,.0f} outside OCTOPOS range "
                                f"${octopos_cash_min:,.0f}-${octopos_cash_max:,.0f} — investigate")
        elif pct > 0.02:
            warnings.append(f"cash mismatch: manager ${cash_total:,.0f} vs OCTOPOS ${octopos_cash:,.0f} "
                            f"({pct:.0%} > 2%) — investigate")

    if not team:
        warnings.append("no team[] in report — Staff line is $0; confirm roster with Lauren")

    return {"source": "manager_reports", "evkey": evkey, "found": True,
            "manager_name": rep.get("manager_name"), "submitted_at": rep.get("submitted_at"),
            "staff": round(staff, 2), "meals": round(meals, 2), "other": round(other, 2),
            "roster": roster, "team": team, "expense_lines": expense_lines,
            "cash": {"total_cash": round(cash_total, 2),
                     "payouts_total": round(_f(cash.get("payouts_total")), 2),
                     "deposit": round(_f(cash.get("deposit")), 2),
                     "register_coins": round(_f(cash.get("register_coins")), 2),
                     "register_bills": round(_f(cash.get("register_bills")), 2)},
            "cash_check": cash_check, "answers": rep.get("answers") or {},
            "notes": rep.get("notes") or "", "photos": rep.get("photos") or [],
            "complete": True, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", required=True)
    ap.add_argument("--state")
    ap.add_argument("--octopos-cash", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = fetch_manager_pnl(args.evkey, state_path=args.state, octopos_cash=args.octopos_cash)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    print(f"\n=== Manager report P&L — {args.evkey} ===")
    if not d["found"]:
        print(f"  NOT FOUND: {d['error']}")
        for w in d["warnings"]:
            print(f"  ! {w}")
        return 1
    print(f"  Manager: {d['manager_name']}  ({d['submitted_at']})")
    print(f"  Staff:  ${d['staff']:,.2f}   roster: {', '.join(d['roster']) or '(none)'}")
    print(f"  Meals:  ${d['meals']:,.2f}")
    print(f"  Other:  ${d['other']:,.2f}")
    print(f"  Cash:   {d['cash']}")
    if d["cash_check"]:
        print(f"  Cash check vs OCTOPOS: {d['cash_check']}")
    for w in d["warnings"]:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
