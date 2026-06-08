#!/usr/bin/env python3
"""qb_apply_queue.py — applies Lauren's bookkeeping POSTs to QuickBooks.

Reads docs/state/qb_post_queue.json (written by the bookkeeping dashboard when
Lauren clicks 🚀 POST), and for each pending entry updates the Purchase/Bill in
QuickBooks: line AccountRef (category), line ClassRef, txn-level vendor
(EntityRef on Purchase / VendorRef on Bill). Names are resolved against
docs/state/qb_lists.json — an unknown name is skipped with a warning (never
auto-created). Full-object update: GET entity → modify → POST back w/ SyncToken.

🛑 Writes happen ONLY for entries Lauren explicitly queued via POST. Multiple
queue entries for the same txn are grouped into ONE update.

Runs in CI (qb-post-queue.yml, triggered by the queue commit) or locally.
"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_quickbooks as QB

QUEUE = ROOT / "docs/state/qb_post_queue.json"
LISTS = ROOT / "docs/state/qb_lists.json"

def _norm(s): return (s or "").strip().lower()

def build_maps():
    L = json.loads(LISTS.read_text(encoding="utf-8"))
    cats, classes, vendors = {}, {}, {}
    for c in L.get("categories", []):
        cats[_norm(c["name"])] = c
        cats[_norm(c["name"].split(":")[-1])] = c  # leaf name of FQN too
    for c in L.get("classes", []): classes[_norm(c["name"])] = c
    for v in L.get("vendors", []): vendors[_norm(v["name"])] = v
    return cats, classes, vendors

EVENTS_IDX = ROOT / "docs/state/events_index.json"

def ensure_class(name, classes):
    """Auto-create a missing QB Class — ONLY for known event classes from events_index.
    (Lauren 2026-06-08: venue deposits for future events kept hitting "class not in QB"
    because per-event Classes are created lazily. Arbitrary names are still never created.)"""
    k = _norm(name)
    if not name or k in classes:
        return classes.get(k)
    try:
        evcls = {e["class_name"] for e in json.loads(EVENTS_IDX.read_text(encoding="utf-8"))["events"]}
    except Exception:
        evcls = set()
    if name.strip() not in evcls:
        return None                      # not a known event class — keep the old skip behavior
    res = QB.qb_post("class", {"Name": name.strip()})
    c = (res or {}).get("Class") or {}
    if not c.get("Id"):
        return None
    rec = {"id": c["Id"], "name": c["Name"]}
    classes[k] = rec
    print(f"  + created QB Class '{c['Name']}' (id {c['Id']})")
    try:                                  # persist so the dashboard ⚠ clears without waiting for daily pull
        L = json.loads(LISTS.read_text(encoding="utf-8"))
        if not any(_norm(x.get("name")) == k for x in L.get("classes", [])):
            L.setdefault("classes", []).append(rec)
            LISTS.write_text(json.dumps(L, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as ex:
        print(f"  warn: qb_lists.json update failed: {ex}")
    return rec

def apply_entry_group(entity, txn_id, entries, cats, classes, vendors):
    """One QB update covering all queued lines of this txn. Returns per-entry results."""
    obj = QB.qb_get_entity(entity, txn_id).get(entity)
    if not obj:
        return {e["key"]: ("error", f"{entity} {txn_id} not found") for e in entries}
    lines = obj.get("Line") or []
    results, changed = {}, False
    for e in entries:
        line = next((l for l in lines if str(l.get("Id")) == str(e["line_id"])), None)
        if line is None:
            results[e["key"]] = ("error", f"line {e['line_id']} not found"); continue
        det = line.get("AccountBasedExpenseLineDetail")
        if det is None:
            results[e["key"]] = ("error", "not an account-based expense line"); continue
        notes = []
        st = e.get("set") or {}
        acc = cats.get(_norm(st.get("account")))
        if st.get("account") and acc:
            det["AccountRef"] = {"value": acc["id"], "name": acc["name"]}; changed = True
        elif st.get("account"):
            notes.append(f"category '{st['account']}' not in QB")
        cls = classes.get(_norm(st.get("cls"))) or ensure_class(st.get("cls"), classes)
        if st.get("cls") and cls:
            det["ClassRef"] = {"value": cls["id"], "name": cls["name"]}; changed = True
        elif st.get("cls"):
            notes.append(f"class '{st['cls']}' not in QB")
        ven = vendors.get(_norm(st.get("vendor")))
        if st.get("vendor") and ven:
            ref = {"value": ven["id"], "name": ven["name"]}
            if entity == "Purchase":
                obj["EntityRef"] = {**ref, "type": "Vendor"}
            else:
                obj["VendorRef"] = ref
            changed = True
        elif st.get("vendor"):
            notes.append(f"vendor '{st['vendor']}' not in QB (not auto-created)")
        results[e["key"]] = ("applied", "; ".join(notes))
    if changed:
        QB.qb_post(entity.lower(), obj)
    return results

def apply_return(entity, txn_id, cats):
    """Try to delete the posted txn (bank line returns to For Review). QBO blocks
    deleting bank-MATCHED txns — fallback: park it in 'Ask My Accountant' so it is
    uniformly flagged as unclear inside QB (Lauren can Undo manually if needed)."""
    obj = QB.qb_get_entity(entity, txn_id).get(entity)
    if not obj:
        return ("error", f"{entity} {txn_id} not found")
    try:
        QB.qb_post(f"{entity.lower()}?operation=delete",
                   {"Id": obj["Id"], "SyncToken": obj["SyncToken"]})
        return ("applied", "returned to For Review")
    except RuntimeError as e:
        if "Matched" not in str(e):
            raise
        ask = cats.get("ask my accountant")
        if not ask:
            return ("error", "matched txn — delete blocked, no Ask My Accountant account")
        changed = False
        for ln in obj.get("Line") or []:
            det = ln.get("AccountBasedExpenseLineDetail")
            if det:
                det["AccountRef"] = {"value": ask["id"], "name": ask["name"]}
                changed = True
        if changed:
            QB.qb_post(entity.lower(), obj)
        return ("applied", "matched txn — parked in Ask My Accountant (Undo manually in QB to fully return)")

def main():
    q = json.loads(QUEUE.read_text(encoding="utf-8"))
    pending = [e for e in q.get("queue", []) if e.get("status") not in ("applied", "staged") and int(e.get("attempts") or 0) < 3]
    if not pending:
        print("queue empty — nothing to apply"); return 0
    cats, classes, vendors = build_maps()
    groups = {}
    for e in pending:
        ent = "Bill" if (e.get("txn_type") or "").lower() == "bill" else "Purchase"
        groups.setdefault((ent, str(e["txn_id"])), []).append(e)
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    n_ok = n_err = 0
    for (ent, tid), entries in groups.items():
        try:
            if any(e.get("action") == "return" for e in entries):
                st, note = apply_return(ent, tid, cats)
                res = {e["key"]: (st, note) for e in entries}
            else:
                res = apply_entry_group(ent, tid, entries, cats, classes, vendors)
        except Exception as ex:
            res = {e["key"]: ("error", str(ex)[:200]) for e in entries}
        for e in entries:
            status, note = res.get(e["key"], ("error", "no result"))
            e["status"] = status
            e["attempts"] = int(e.get("attempts") or 0) + 1
            e["applied_at" if status == "applied" else "last_error_at"] = now
            if note: e["note"] = note
            if status == "applied": n_ok += 1
            else: n_err += 1
            print(f"  {'✓' if status=='applied' else '✗'} {ent} {tid} line {e['line_id']} — {e.get('set', e.get('action',''))} {('['+note+']') if note else ''}")
    q["_updated_at"] = now
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\napplied {n_ok} · errors {n_err} · queue size {len(q.get('queue', []))}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
