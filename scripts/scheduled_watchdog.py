"""
scheduled_watchdog — alerts Lauren when scheduled tasks fall behind.

Runs every 4 hours. Reads docs/scheduled-runs.json and computes
hours-since-lastRunAt for each enabled task. If any task is LATE
(diff > frequency threshold), sends a single SMS digest naming all
LATE tasks.

Anti-spam: tracks last-alert-sent-at per task in
docs/state/watchdog_alerts.json. Re-alerts a given task only if it's
been ≥12h since the previous alert for that task — so Lauren gets one
ping per day per stuck task, not a torrent.

LATE thresholds (matching the agent_hub widget's statusFor() logic):
    hourly  → 1.2 hours
    daily   → 25 hours
    weekly  → 192 hours (8 days)

Enabled but never-run tasks (lastRunAt == "") are reported as NEVER
on their first appearance, then suppressed for 24h same as LATE.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms, LAUREN_PHONE

REGISTRY_PATH = Path("docs/scheduled-runs.json")
STATE_PATH    = Path("docs/state/watchdog_alerts.json")
RE_ALERT_HOURS = 12.0

THRESHOLDS = {
    "hourly":  1.2,
    "daily":  25.0,
    "weekly": 192.0,
}


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _hours_since(iso: str, now: dt.datetime) -> float:
    if not iso:
        return float("inf")
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return float("inf")
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return (now - d).total_seconds() / 3600.0


def main() -> int:
    if not REGISTRY_PATH.exists():
        print("[watchdog] scheduled-runs.json missing; nothing to check.")
        return 0
    if not os.environ.get("SIMPLETEXTING_TOKEN"):
        print("[watchdog] SIMPLETEXTING_TOKEN missing; cannot send SMS.")
        return 0
    if not LAUREN_PHONE:
        print("[watchdog] LAUREN_PHONE missing.")
        return 0

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    state = _load_state()
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    late = []
    for t in registry.get("tasks", []):
        if not t.get("enabled"):
            continue
        tid    = t.get("id", "")
        freq   = t.get("frequency", "daily")
        limit  = THRESHOLDS.get(freq, 25.0)
        last   = t.get("lastRunAt", "")
        diffH  = _hours_since(last, now)
        is_late = diffH > limit
        if not is_late:
            continue
        # Anti-spam — only re-alert if last alert for this task was ≥ RE_ALERT_HOURS ago.
        last_alert_iso = (state.get("alerts", {}) or {}).get(tid, "")
        last_alert_h   = _hours_since(last_alert_iso, now)
        if last_alert_h < RE_ALERT_HOURS:
            print(f"[watchdog] {tid}: LATE but last alert was {last_alert_h:.1f}h ago (<{RE_ALERT_HOURS}); suppress.")
            continue
        late.append({
            "id":    tid,
            "label": t.get("label", tid),
            "freq":  freq,
            "limit": limit,
            "diff":  diffH,
            "last":  last or "(מעולם לא רץ)",
        })

    if not late:
        print(f"[watchdog] all {len(registry.get('tasks', []))} tasks healthy.")
        return 0

    # Build the SMS digest.
    lines = ["⚠ משימות מתוזמנות שלא רצות"]
    for it in late:
        if it["diff"] == float("inf"):
            ago = "מעולם לא רץ"
        elif it["diff"] < 24:
            ago = f"{it['diff']:.1f}h ago"
        else:
            ago = f"{it['diff']/24:.1f}d ago"
        lines.append(f"\n• {it['label']}\n  ({it['id']}) · last: {ago}")
    lines += [
        "",
        "פתחי את ה-Actions ובדקי:",
        "https://github.com/laurenlev10/lauren-agent-hub-data/actions",
    ]
    body = "\n".join(lines)

    try:
        resp = send_sms(LAUREN_PHONE, body)
        print(f"[watchdog] SMS sent for {len(late)} late task(s): id={resp.get('id', '?')}")
    except Exception as e:
        print(f"[watchdog] SMS failed: {e}")
        return 0  # don't crash workflow

    # Persist the alert timestamps so anti-spam can suppress next time.
    state.setdefault("alerts", {})
    for it in late:
        state["alerts"][it["id"]] = now_iso
    state["_last_run_at"] = now_iso
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
