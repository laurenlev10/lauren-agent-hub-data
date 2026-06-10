#!/usr/bin/env python3
"""
Stock-spike watchdog — polls docs/state/octopos_stock_audit.json and SMSes
Lauren about any unacknowledged stock change where |delta| >= 100 units OR
delta_pct >= 200%. Even if the dashboard's PREFLIGHT G1 guard was bypassed
(e.g., by a future automation or a bug in the UI), this watchdog catches
the spike async within minutes and alerts Lauren.

Each "acked" entry has been smsd_at timestamp. Entries already smsd are
skipped on subsequent runs. Lauren can also pre-ack from the dashboard by
setting acked_at on a row.

Cron: every 30 min, see .github/workflows/stock-spike-watchdog.yml.
"""
import json, os, sys, urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).parent.parent
AUDIT_FILE = REPO_ROOT / "docs" / "state" / "octopos_stock_audit.json"

SPIKE_ABS = 100   # units
SPIKE_PCT = 200   # %

def is_spike(entry):
    delta = abs(float(entry.get("delta", 0)))
    before = float(entry.get("before", 0))
    if delta >= SPIKE_ABS:
        return True
    if before > 0 and (delta / before * 100) >= SPIKE_PCT:
        return True
    if before == 0 and delta >= 12:   # 0 → 12+ also flagged (anything ≥1 case-pack jumping from empty)
        return True
    return False

def send_sms(phone, body):
    token = os.environ.get("SIMPLETEXTING_TOKEN", "").strip()
    if not token:
        print(f"  (no SIMPLETEXTING_TOKEN — would have sent to {phone}: {body[:80]})")
        return False
    # 2026-06-08 — was hitting the dead host app-api.simpletexting.com (DNS fail → every
    # run errored). Route through the shared lauren_sms helper (app2.simpletexting.com).
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from lauren_sms import send_sms as _send
        _send(phone, body)
        return True
    except Exception as e:
        print(f"  SMS to {phone} failed: {e}")
        return False

def main():
    if not AUDIT_FILE.exists():
        print(f"audit file missing: {AUDIT_FILE}")
        return 0
    data = json.loads(AUDIT_FILE.read_text())
    entries = data.get("entries", [])
    if not entries:
        print("audit log empty — nothing to check")
        return 0

    spikes_to_alert = []
    for e in entries:
        if not is_spike(e):
            continue
        if e.get("smsd_at"):     # already smsd
            continue
        if e.get("acked_at"):    # Lauren pre-acked in dashboard
            continue
        # 2026-06-10 (Lauren's decision): invoice receives confirmed via the
        # dashboard PREFLIGHT (source @inventory/receive) are intentional —
        # don't alert on them. The watchdog guards NON-UI writers only.
        if (e.get("source") or "").startswith("@inventory/receive"):
            continue
        spikes_to_alert.append(e)

    if not spikes_to_alert:
        print(f"no unack'd spikes among {len(entries)} entries")
        return 0

    print(f"⚠ {len(spikes_to_alert)} unack'd stock spike(s) — sending SMS")

    # Build the body — first 5 spikes inline, rest summarized
    sample = spikes_to_alert[:5]
    body_lines = ["⚠ קפיצות מלאי חריגות זוהו:"]
    for s in sample:
        sign = "+" if s.get("delta", 0) >= 0 else ""
        body_lines.append(f"  {s.get('sku', '?')}: {int(s.get('before',0))} → {int(s.get('after',0))} ({sign}{int(s.get('delta',0))})")
    if len(spikes_to_alert) > 5:
        body_lines.append(f"  ועוד {len(spikes_to_alert) - 5} מוצרים...")
    body_lines.append("")
    body_lines.append("בדקי: https://dashboard.themakeupblowout.com/inventory/")
    body = "\n".join(body_lines)

    # Recipients: Lauren only (operational alert per IRON RULE #4.C)
    phone = os.environ.get("LAUREN_PHONE", "").strip()
    if not phone:
        print("LAUREN_PHONE not set — skipping send")
        return 0

    if send_sms(phone, body):
        # Mark all alerted entries as smsd_at — so we don't alert again
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for s in spikes_to_alert:
            s["smsd_at"] = now
        data["_updated_at"] = now
        AUDIT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"✓ SMSd {len(spikes_to_alert)} spikes, marked smsd_at in audit log")
    else:
        print("✗ SMS failed — keeping spikes un-marked for next run")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
