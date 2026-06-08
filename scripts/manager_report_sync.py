#!/usr/bin/env python3
"""
manager_report_sync.py  —  propagate FINAL manager reports into the rest of the system.

Set up 2026-06-08 (Lauren: "כל מה שלמדנו פה ישתפר לפעם הבאה הכל כולל הכל").

For every event that has a FINAL manager report in docs/state/manager_reports.json this
script does TWO things, both merge-safe and token-free (pure repo-JSON, runs in CI):

  (A) P&L refresh — updates that event's docs/state/event_pnl/<evkey>.json manager-sourced
      lines (staff / meals / other) + cash_check, then recomputes total_known_expenses /
      profit_preliminary / margin. Skips a line if a manual override is present. This is the
      automation of what used to be a manual rebuild — answering Lauren's "do you update the
      P&L from the manager report?" with "yes, automatically now."

  (B) GARAGE prep notes — writes the manager's low-stock / on-hand answers into the NEXT
      event's inventory_orders.json slice as `garage_prep_notes`, which the inventory
      dashboard renders on the GARAGE (mystery-box) supplier card. So whoever orders GARAGE
      supplies for the next event sees what the previous manager flagged as low / out.

Cash reconciliation uses the split-ticket RANGE (see pnl_manager.py) — no false mismatch.

Idempotent: (B) only rewrites when the source report's submitted_at changed, so manual
inspection isn't clobbered every run.
"""
import argparse, datetime as dt, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "docs" / "state"
MGR_FILE   = STATE / "manager_reports.json"
IDX_FILE   = STATE / "events_index.json"
INV_FILE   = STATE / "inventory_orders.json"
PNL_DIR    = STATE / "event_pnl"

sys.path.insert(0, str(ROOT / "scripts"))
import pnl_manager  # noqa: E402


def _now():
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


# ----- form field metadata (mirrors docs/manager-report/index.html SECTIONS) -----
# kind: need_yesno | level_select | count_number | text_need
FIELD_DEFS = [
    ("plastic_bags",    "500 Plastic bags",            "level_select"),
    ("trash_bags",      "Trash bags",                  "need_yesno"),
    ("tester_stickers", "Testers stickers",            "need_yesno"),
    ("neon_stars",      "Neon stars for signs",        "need_yesno"),
    ("shrink_roll",     "Roll Shrink",                 "need_yesno"),
    ("table_covers",    "Clean table covers",          "count_number"),
    ("scotch_tape",     "Scotch tape",                 "need_yesno"),
    ("price_numbers",   "Price numbers",               "text_need"),
    ("glitters",        "Glitters",                    "level_select"),
    ("eyeshadow_gifts", "Eyeshadow gifts",             "level_select"),
    ("brush_set_gifts", "$12 Brush Set gifts",         "count_or_level"),
    ("xime_irresistible","Xime Irresistible palette",  "count_number"),
    ("xime_goddess",    "Xime Goddess palette",        "count_number"),
]
LOW_LEVELS = {"no more", "less than 1/2 box", "1/2 box"}


def _build_prep_notes(rep):
    """Turn a manager report's answers + notes into a garage_prep_notes dict."""
    answers = rep.get("answers") or {}
    need, on_hand, dont = [], [], []

    def yes(v):  return str(v).strip().lower() in ("yes", "y", "true", "1", "yes — need it")
    def no(v):   return str(v).strip().lower() in ("no", "n", "false", "0", "")

    for key, label, kind in FIELD_DEFS:
        v = answers.get(key, "")
        sv = str(v).strip()
        if kind == "need_yesno":
            if yes(v):  need.append({"item": label, "detail": "need more"})
            elif sv:    dont.append(label)
        elif kind == "level_select":
            if not sv:  continue
            on_hand.append({"item": label, "detail": sv})
            if sv.lower() in LOW_LEVELS:
                need.append({"item": label, "detail": "low — " + sv})
        elif kind == "count_or_level":     # brush set: number now, legacy "No More"/levels
            if not sv:  continue
            if sv.lower() in ("no more",) or sv in ("0",):
                need.append({"item": label, "detail": "OUT — " + sv})
            else:
                on_hand.append({"item": label, "detail": sv})
        elif kind == "count_number":
            if sv == "":  continue
            try:
                n = float(sv)
            except ValueError:
                on_hand.append({"item": label, "detail": sv}); continue
            if n <= 0:  need.append({"item": label, "detail": "0 — need more"})
            else:       on_hand.append({"item": label, "detail": str(int(n) if n == int(n) else n)})
        elif kind == "text_need":
            if sv:  need.append({"item": label, "detail": sv})

    return {
        "from_evkey": rep.get("evkey"),
        "from_event": _event_label(rep),
        "manager": rep.get("manager_name"),
        "submitted_at": rep.get("submitted_at"),
        "need_to_order": need,
        "on_hand": on_hand,
        "dont_need": dont,
        "manager_notes": rep.get("notes") or "",
        "_generated_at": _now(),
    }


def _event_label(rep):
    ev = rep.get("event") or {}
    c = ev.get("city") or rep.get("evkey", "")
    st = ev.get("state") or ""
    sd = (ev.get("start_date") or "")[5:].replace("-", "/")
    return f"{c}, {st} · {sd}".strip(" ,·")


def _latest_finals(mgr):
    """{evkey: latest final report}."""
    out = {}
    for evkey, lst in (mgr.get("reports") or {}).items():
        finals = [r for r in (lst or []) if str(r.get("mode", "final")).lower() != "draft"]
        if not finals:
            continue
        finals.sort(key=lambda r: r.get("submitted_at") or "")
        rep = dict(finals[-1])
        rep.setdefault("evkey", evkey)
        out[evkey] = rep
    return out


def _next_evkey(evkey, idx):
    evs = sorted((idx.get("events") or []), key=lambda e: e.get("start_date") or "")
    keys = [e.get("evkey") for e in evs]
    meta = {e.get("evkey"): e for e in evs}
    if evkey not in keys:
        return None, None
    i = keys.index(evkey)
    if i + 1 >= len(keys):
        return None, None
    nk = keys[i + 1]
    return nk, meta.get(nk, {})


# ---------- (A) P&L refresh ----------
def refresh_pnl(evkey, rep, dry):
    pf = PNL_DIR / f"{evkey}.json"
    if not pf.exists():
        return f"  P&L  {evkey}: no event_pnl file (skip)"
    j = _load(pf, None)
    if not j:
        return f"  P&L  {evkey}: unreadable (skip)"

    overrides = j.get("manual_overrides") or {}
    pay = (j.get("detail") or {}).get("payment_breakdown") or {}
    cmin = round(sum(v for k, v in pay.items() if k.upper().strip() == "CASH"), 2)
    cmax = round(sum(v for k, v in pay.items() if "CASH" in k.upper()), 2)
    cmid = round((cmin + cmax) / 2, 2) if cmax else (j.get("revenue", {}).get("octopos_cash"))

    mgr = pnl_manager.fetch_manager_pnl(evkey, octopos_cash=cmid,
                                        octopos_cash_min=cmin or None,
                                        octopos_cash_max=cmax or None)
    if not mgr.get("found"):
        return f"  P&L  {evkey}: manager report not found by pnl_manager (skip)"

    def setline(key, amt, note=""):
        if key in overrides:
            return False
        j["expenses"][key] = {"amount": round(amt, 2), "source": "manager_reports",
                              "status": "ok", "note": note}
        return True

    setline("staff", mgr["staff"], f"{mgr.get('manager_name','')} final report")
    setline("meals", mgr["meals"])
    setline("other", mgr["other"])

    if cmax:
        j["revenue"]["octopos_cash"] = cmid
    j["cash_check"] = mgr.get("cash_check")

    j["detail"]["staff_lines"] = [{"name": (t.get("name") or "—"),
                                   "amount": round(t.get("total") or 0, 2)} for t in (mgr.get("team") or [])]
    j["detail"]["manager_expense_lines"] = mgr.get("expense_lines", [])
    j["detail"]["manager_name"] = mgr.get("manager_name")
    j["detail"]["manager_notes"] = mgr.get("notes", "")

    known = [v["amount"] for v in j["expenses"].values() if isinstance(v.get("amount"), (int, float))]
    net = j.get("revenue", {}).get("net_sales")
    j["total_known_expenses"] = round(sum(known), 2)
    if net:
        j["profit_preliminary"] = round(net - j["total_known_expenses"], 2)
        j["margin"] = round(j["profit_preliminary"] / net, 4)
    j["pending_or_missing"] = [k for k in (j.get("pending_or_missing") or []) if k not in ("staff", "meals", "other")]

    # drop stale draft/cash-mismatch warnings; re-add only real ones from mgr
    keep = [w for w in (j.get("warnings") or [])
            if "draft" not in w.lower() and "cash mismatch" not in w.lower()]
    for w in mgr.get("warnings", []):
        if w not in keep:
            keep.append(w)
    j["warnings"] = keep
    j["generated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if not dry:
        pf.write_text(json.dumps(j, indent=2, ensure_ascii=False), encoding="utf-8")
    return (f"  P&L  {evkey}: staff ${mgr['staff']:,.0f} meals ${mgr['meals']:,.0f} "
            f"other ${mgr['other']:,.0f} → profit ${j.get('profit_preliminary',0):,.0f}")


# ---------- (B) GARAGE prep notes ----------
def push_prep_notes(evkey, rep, inv, idx, dry):
    nk, meta = _next_evkey(evkey, idx)
    if not nk:
        return f"  PREP {evkey}: no next event (skip)", False
    events = inv.setdefault("events", {})
    slice_ = events.get(nk, {})
    # idempotency — skip if we already have notes from this exact submission
    existing = slice_.get("garage_prep_notes") or {}
    if existing.get("_source_submitted_at") == rep.get("submitted_at"):
        return f"  PREP {evkey}→{nk}: unchanged (skip)", False

    slice_.setdefault("evkey", nk)
    for k in ("city", "state", "start_date", "end_date", "venue", "address"):
        if meta.get(k) and not slice_.get(k):
            slice_[k] = meta[k]
    slice_.setdefault("local_orders", slice_.get("local_orders", []))

    notes = _build_prep_notes(rep)
    notes["_source_submitted_at"] = rep.get("submitted_at")
    slice_["garage_prep_notes"] = notes
    events[nk] = slice_
    if not dry:
        inv["_updated_at"] = _now()
    return (f"  PREP {evkey}→{nk}: {len(notes['need_to_order'])} to-order, "
            f"{len(notes['on_hand'])} on-hand"), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evkey", help="process only this event (default: all with finals)")
    ap.add_argument("--dry", action="store_true", help="don't write files")
    args = ap.parse_args()

    mgr = _load(MGR_FILE, {})
    idx = _load(IDX_FILE, {})
    inv = _load(INV_FILE, {"_updated_at": None, "events": {}})

    finals = _latest_finals(mgr)
    if args.evkey:
        finals = {k: v for k, v in finals.items() if k == args.evkey}

    if not finals:
        print("No final manager reports to process.")
        return 0

    inv_changed = False
    print(f"Processing {len(finals)} final report(s):")
    for evkey, rep in sorted(finals.items()):
        print(refresh_pnl(evkey, rep, args.dry))
        msg, changed = push_prep_notes(evkey, rep, inv, idx, args.dry)
        print(msg)
        inv_changed = inv_changed or changed

    if inv_changed and not args.dry:
        INV_FILE.write_text(json.dumps(inv, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Wrote inventory_orders.json")
    elif args.dry:
        print("(dry run — no files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
