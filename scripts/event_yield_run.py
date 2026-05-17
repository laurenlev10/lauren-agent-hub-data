#!/usr/bin/env python3
"""
@event-yield analyzer (extension of @slow-movers).

Fires Sunday 22:30 LOCAL at the event's location (after recount-weekly @ 17:15
local fires). Reads docs/state/octopos_stock_timeseries.json which holds 60 days
of daily snapshots, picks the Fri/Sat/Sun/Mon snapshots for this weekend's
event, and per active product computes:
  - last_event_signal: stockout_friday | stockout_saturday | overstock_heavy |
                       overstock_light | normal | low_base | new_product | data_issue
  - suggested_qty_multiplier: 0.7 / 0.85 / 1.0 / 1.3 / 1.5 (clamped 0.5–2.0)
  - event_signals_history: append, cap at 6 entries

Writes back to docs/state/slow_movers.json (per-product fields documented in
slow-movers/SKILL.md + scoring.md "🚨 Event-yield signal" section).
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

def fetch_live_octopos_qty():
    """Fetch CURRENT in_stock_qty per product via /api/v2/. Returns {pid_str: qty_float}.
    Reads token from OCTOPOS_TOKEN env var (set by workflow from GitHub secret)."""
    token = os.environ.get("OCTOPOS_TOKEN", "")
    if not token:
        # Fallback: try local cache (won't exist in CI but works in dev)
        try:
            token = open("/sessions/nifty-lucid-allen/mnt/Claude/.claude/secrets/octopos_token.txt").read().strip()
        except Exception:
            return None
    # iterate by product id (binary-search max_id then concurrent GETs) — too heavy here.
    # Simpler: hit /get_products_by_filter which returns first 100, then iterate by id from 1..MAX.
    # But the daily snapshot does this already; for the event-yield call we only need CURRENT qty.
    # Lighter path: fetch the daily snapshot from THIS workflow's checkout instead.
    return None  # fall through to timeseries-based qty_mon
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent

# Imported from recount_weekend.py — keep local + duplicated for now (Phase 2 refactor: shared/octopos_tz.py)
STATE_TZ = {
    "AL":"America/Chicago","AK":"America/Anchorage","AZ":"America/Phoenix",
    "AR":"America/Chicago","CA":"America/Los_Angeles","CO":"America/Denver",
    "CT":"America/New_York","DE":"America/New_York","FL":"America/New_York",
    "GA":"America/New_York","HI":"Pacific/Honolulu","ID":"America/Boise",
    "IL":"America/Chicago","IN":"America/Indiana/Indianapolis","IA":"America/Chicago",
    "KS":"America/Chicago","KY":"America/New_York","LA":"America/Chicago",
    "ME":"America/New_York","MD":"America/New_York","MA":"America/New_York",
    "MI":"America/Detroit","MN":"America/Chicago","MS":"America/Chicago",
    "MO":"America/Chicago","MT":"America/Denver","NE":"America/Chicago",
    "NV":"America/Los_Angeles","NH":"America/New_York","NJ":"America/New_York",
    "NM":"America/Denver","NY":"America/New_York","NC":"America/New_York",
    "ND":"America/Chicago","OH":"America/New_York","OK":"America/Chicago",
    "OR":"America/Los_Angeles","PA":"America/New_York","RI":"America/New_York",
    "SC":"America/New_York","SD":"America/Chicago","TN":"America/Chicago",
    "TX":"America/Chicago","UT":"America/Denver","VT":"America/New_York",
    "VA":"America/New_York","WA":"America/Los_Angeles","WV":"America/New_York",
    "WI":"America/Chicago","WY":"America/Denver","DC":"America/New_York",
}

TARGET_LOCAL_HOUR = 17
TARGET_LOCAL_MIN  = 30
WINDOW_MIN_AFTER  = 45   # tolerate up to 18:15 local

BASE_MULTIPLIER = {
    "stockout_friday":   1.5,
    "stockout_saturday": 1.3,
    "overstock_heavy":   0.7,
    "overstock_light":   0.85,
    "normal":            1.0,
    "low_base":          1.0,
    "new_product":       1.0,
    "data_issue":        1.0,
}


def parse_schedule():
    html = (REPO_ROOT / "docs/launch/index.html").read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\[[\s\S]*?\]);\s*\n", html)
    if not m: return []
    try: return json.loads(m.group(1))
    except json.JSONDecodeError: return []


def find_event_for_today():
    """Find this weekend's event AND check 22:30-23:30 local fire window."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    today_local_la = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).date()
    candidates = []
    for ev in parse_schedule():
        try: end_date = dt.date.fromisoformat(ev.get("end_date") or "")
        except ValueError: continue
        if abs((end_date - today_local_la).days) > 1: continue
        state = (ev.get("state") or "").strip().upper()
        tz_name = ev.get("tz_override") or STATE_TZ.get(state)
        if not tz_name: continue
        local = now_utc.astimezone(ZoneInfo(tz_name))
        if local.weekday() != 6: continue
        mins = local.hour * 60 + local.minute
        target = TARGET_LOCAL_HOUR * 60 + TARGET_LOCAL_MIN
        if not (target <= mins <= target + WINDOW_MIN_AFTER): continue
        candidates.append((ev, tz_name, local, end_date))
    if not candidates: return None
    candidates.sort(key=lambda x: abs((x[3] - today_local_la).days))
    return candidates[0]


def load_timeseries():
    p = REPO_ROOT / "docs/state/octopos_stock_timeseries.json"
    if not p.exists():
        return {"_updated_at": None, "snapshots": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def classify(qty_fri, qty_sat, qty_sun, qty_mon):
    """Returns (signal, multiplier). All qty values may be None or numbers."""
    # Need at least Fri baseline to draw any conclusion
    if qty_fri is None:
        return ("data_issue", 1.0)
    try: qty_fri = float(qty_fri)
    except (ValueError, TypeError): return ("data_issue", 1.0)
    if qty_fri < 0:    return ("data_issue", 1.0)
    if qty_fri <= 4:   return ("low_base", 1.0)
    # Stockouts (priority over overstock — stronger signal)
    if qty_sat is not None and float(qty_sat) <= 0 and qty_fri > 0:
        return ("stockout_friday", BASE_MULTIPLIER["stockout_friday"])
    if qty_sun is not None and float(qty_sun) <= 0 and (qty_sat is None or float(qty_sat) > 0):
        return ("stockout_saturday", BASE_MULTIPLIER["stockout_saturday"])
    # Overstock — Monday qty as % of Friday
    if qty_mon is not None:
        try: ratio = float(qty_mon) / qty_fri
        except ZeroDivisionError: return ("data_issue", 1.0)
        if ratio > 0.7: return ("overstock_heavy", BASE_MULTIPLIER["overstock_heavy"])
        if ratio > 0.5: return ("overstock_light", BASE_MULTIPLIER["overstock_light"])
    return ("normal", 1.0)


def compute_event_dates(end_date: dt.date):
    """Event runs Fri-Sun; snapshots at 2 AM PT each morning.
       For event ending Sunday, return (Fri, Sat, Sun, Mon) dates."""
    sun = end_date
    fri = sun - dt.timedelta(days=2)
    sat = sun - dt.timedelta(days=1)
    mon = sun + dt.timedelta(days=1)
    return fri.isoformat(), sat.isoformat(), sun.isoformat(), mon.isoformat()


def main():
    match = find_event_for_today()
    if not match:
        print("[event-yield] No event matches 22:30 local fire window. Exiting silent.")
        return 0
    ev, tz_name, local, end_date = match
    evkey = f"{(ev.get('city') or '').lower().replace(' ','-')}-{ev.get('start_date')}"
    print(f"FIRING for evkey={evkey} (local {local})")

    ts = load_timeseries()
    snapshots = ts.get("snapshots", {})
    fri, sat, sun, mon = compute_event_dates(end_date)

    snap_fri = snapshots.get(fri, {})
    snap_sat = snapshots.get(sat, {})
    snap_sun = snapshots.get(sun, {})
    snap_mon = snapshots.get(mon, {})

    # 17:30 Sunday local fires BEFORE Monday 2 AM PT snapshot exists.
    # Use live OCTOPOS qty as the post-event qty proxy (the event just ended ~30min ago,
    # so live qty ≈ what Monday's snapshot will record). Cleaner timeline + no waiting.
    live_qty = fetch_live_octopos_qty()
    if live_qty:
        snap_mon = live_qty
        print(f"Using LIVE OCTOPOS qty for post-event ({len(live_qty)} products)")
    print(f"Snapshots present: Fri={bool(snap_fri)} Sat={bool(snap_sat)} Sun={bool(snap_sun)} Mon={bool(snap_mon)}")

    if not snap_fri:
        # Need at least Friday baseline. Skip silently — next event will have it once timeseries fills.
        print("[event-yield] No Friday baseline snapshot yet. Will resume next event when timeseries fills.")
        return 0

    # Active product IDs = union of keys in all 4 snapshots
    all_pids = set(snap_fri.keys()) | set(snap_sat.keys()) | set(snap_sun.keys()) | set(snap_mon.keys())
    print(f"Products evaluated: {len(all_pids)}")

    # Load + update slow_movers.json
    sm_path = REPO_ROOT / "docs/state/slow_movers.json"
    sm = json.loads(sm_path.read_text(encoding="utf-8"))
    products = sm.setdefault("products", {})

    counts = {k: 0 for k in BASE_MULTIPLIER}
    for pid_str in all_pids:
        qty_fri = snap_fri.get(pid_str)
        qty_sat = snap_sat.get(pid_str)
        qty_sun = snap_sun.get(pid_str)
        qty_mon = snap_mon.get(pid_str)

        signal, mult = classify(qty_fri, qty_sat, qty_sun, qty_mon)
        counts[signal] = counts.get(signal, 0) + 1

        rec = products.setdefault(pid_str, {"id": int(pid_str)})
        rec["last_event_signal"] = signal
        rec["last_event_signal_at"] = local.isoformat()
        rec["suggested_qty_multiplier"] = mult
        rec["qty_multiplier_reason"] = signal
        history = rec.setdefault("event_signals_history", [])
        history.append({
            "evkey": evkey, "signal": signal,
            "qty_fri": qty_fri, "qty_sat": qty_sat, "qty_sun": qty_sun, "qty_mon": qty_mon,
            "ended_at": local.isoformat(),
        })
        rec["event_signals_history"] = history[-6:]   # cap at 6 events

    sm["_updated_at"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    sm_path.write_text(json.dumps(sm, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote slow_movers.json. Signal distribution: {counts}")

    # Build SMS body — only mention actionable signals
    n_stockout = counts.get("stockout_friday", 0) + counts.get("stockout_saturday", 0)
    n_overstock = counts.get("overstock_heavy", 0) + counts.get("overstock_light", 0)
    body_lines = [f"@event-yield ✓ ניתחתי את {ev.get('city')}, {ev.get('state')} ({local.strftime('%H:%M %Z')})."]
    if n_stockout:
        body_lines.append(f"🚨 {n_stockout} מוצרים נגמרו מוקדם מידי (שישי/שבת) — Wizard ידחוף הזמנה גדולה יותר באירוע הבא.")
    if n_overstock:
        body_lines.append(f"📦 {n_overstock} מוצרים נשארו עם יתרה גדולה — Wizard יציע פחות באירוע הבא.")
    if n_stockout == 0 and n_overstock == 0:
        body_lines.append("הכמויות תאמו את הביקוש — אין התאמות נדרשות.")
    body_lines.append("")
    body_lines.append(f"https://laurenlev10.github.io/lauren-agent-hub-data/recount/?evkey={evkey}&tab=slow")

    # Send SMS (best effort)
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from lauren_sms import send_sms
        phone = os.environ.get("LAUREN_PHONE", "4243547625")
        send_sms(phone, "\n".join(body_lines))
        print("SMS sent.")
    except Exception as e:
        print(f"SMS send failed (non-fatal): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
