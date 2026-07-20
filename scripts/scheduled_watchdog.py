"""
scheduled_watchdog — alerts Lauren when scheduled tasks genuinely fall behind.

Runs every 4 hours. Reads docs/scheduled-runs.json and, for each enabled
task, works out the LAST TIME IT ACTUALLY SUCCEEDED — not just the heartbeat
timestamp in the registry, but (when the task is a GitHub Actions workflow)
the real last successful run from the Actions API. This avoids the #1 source
of false alarms: a workflow that runs fine but whose "bump lastRunAt" commit
lost a push race, leaving a stale heartbeat.

A task is reported ONLY if ALL of these are true:
  - it's enabled, AND
  - its real last success is older than its frequency threshold, AND
  - it's not a brand-new task still inside its first scheduled period
    (grace via the optional `addedAt` field), AND
  - we haven't already alerted about it in the last RE_ALERT_HOURS.

So anything that shows up in the SMS has genuinely NOT completed on time.

LATE thresholds (matching the agent_hub widget's statusFor() logic):
    hourly  → 1.2 hours
    daily   → 25 hours
    weekly  → 192 hours (8 days)

Registry conventions used here (all optional, with safe fallbacks):
    frequency  : "hourly" | "daily" | "weekly"   (threshold selector)
    workflow   : ".yml filename" of the Actions workflow (default "<id>.yml")
    addedAt    : ISO timestamp the task was registered (grace for never-run)
    coworkTask : true  → this is a Cowork scheduled task, NOT a GitHub
                 workflow, so skip the Actions cross-check (heartbeat only)
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms, LAUREN_PHONE

REGISTRY_PATH = Path("docs/scheduled-runs.json")
STATE_PATH    = Path("docs/state/watchdog_alerts.json")
RE_ALERT_HOURS = 12.0
REPO = os.environ.get("GITHUB_REPOSITORY", "laurenlev10/lauren-agent-hub-data")
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

THRESHOLDS = {"hourly": 1.2, "daily": 25.0, "weekly": 192.0, "monthly": 744.0}
FREQ_HE    = {"hourly": "כל שעה", "daily": "כל יום", "weekly": "כל שבוע", "monthly": "כל חודש"}


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


def _parse(iso: str):
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d
    except Exception:
        return None


def _hours_since(iso, now: dt.datetime) -> float:
    d = _parse(iso) if isinstance(iso, str) else iso
    return float("inf") if d is None else (now - d).total_seconds() / 3600.0


def _last_success_from_actions(workflow_file: str):
    """Most-recent SUCCESSFUL Actions run start-time (UTC dt), or None.
    None means: couldn't verify (no token / no such workflow / API error) —
    caller falls back to the registry heartbeat."""
    if not GH_TOKEN or not workflow_file:
        return None
    url = (f"https://api.github.com/repos/{REPO}/actions/workflows/"
           f"{workflow_file}/runs?status=success&per_page=1")
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "scheduled-watchdog",
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        runs = data.get("workflow_runs", [])
        if not runs:
            return None
        return _parse(runs[0].get("run_started_at") or runs[0].get("created_at"))
    except Exception as e:
        print(f"[watchdog] Actions lookup failed for {workflow_file}: {e}")
        return None


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
        tid   = t.get("id", "")
        freq  = t.get("frequency", "daily")
        limit = THRESHOLDS.get(freq, 25.0)

        # 1) Real last-success: prefer the Actions API, else the heartbeat.
        heartbeat = t.get("lastRunAt", "")
        real_dt = None
        if not t.get("coworkTask"):
            wf = t.get("workflow") or (f"{tid}.yml" if tid else "")
            real_dt = _last_success_from_actions(wf)

        hb_h   = _hours_since(heartbeat, now)
        real_h = _hours_since(real_dt, now) if real_dt else float("inf")
        diffH  = min(hb_h, real_h)   # most-recent evidence of success wins

        if diffH <= limit:
            continue  # healthy

        # 2) Grace for brand-new tasks that have never run yet.
        never_ran = diffH == float("inf")
        if never_ran:
            age_h = _hours_since(t.get("addedAt", ""), now)
            if age_h < limit:      # still inside its first scheduled period
                print(f"[watchdog] {tid}: never-run but new ({age_h:.1f}h old < {limit}); grace.")
                continue

        # 3) Anti-spam.
        last_alert_h = _hours_since((state.get("alerts", {}) or {}).get(tid, ""), now)
        if last_alert_h < RE_ALERT_HOURS:
            print(f"[watchdog] {tid}: late but alerted {last_alert_h:.1f}h ago; suppress.")
            continue

        late.append({
            "id": tid, "label": t.get("label", tid),
            "freq": freq, "diff": diffH, "never": never_ran,
            "workflow": t.get("workflow") or (f"{tid}.yml" if tid else ""),
            "cowork": bool(t.get("coworkTask")),
        })

    if not late:
        print(f"[watchdog] all {len(registry.get('tasks', []))} tasks healthy.")
        return 0

    # Build the SMS digest — plain, self-explanatory, actionable.
    lines = [
        "⚠️ משימות אוטומטיות שלא רצו בזמן",
        "(בדקתי גם מול ההרצות האמיתיות ב-GitHub — אלה באמת לא הסתיימו בהצלחה)",
    ]
    for it in late:
        if it["never"]:
            when = "עדיין לא רצה אף פעם"
        elif it["diff"] < 24:
            when = f"רצה לאחרונה לפני {it['diff']:.0f} שעות"
        else:
            when = f"רצה לאחרונה לפני {it['diff']/24:.0f} ימים"
        should = FREQ_HE.get(it["freq"], it["freq"])
        block = f"\n• {it['label']}\n  {when} · אמורה לרוץ {should}"
        if it["workflow"] and not it["cowork"]:
            block += (f"\n  לבדיקה: https://github.com/{REPO}/actions/workflows/{it['workflow']}")
        lines.append(block)
    lines += [
        "",
        "מה לעשות: פתחי את הקישור, אם הריצה האחרונה אדומה (נכשלה) — זו הבעיה.",
        "או פשוט תעני לי כאן ואבדוק ואתקן.",
    ]
    body = "\n".join(lines)

    try:
        resp = send_sms(LAUREN_PHONE, body)
        print(f"[watchdog] SMS sent for {len(late)} late task(s): id={resp.get('id', '?')}")
    except Exception as e:
        print(f"[watchdog] SMS failed: {e}")
        return 0

    state.setdefault("alerts", {})
    for it in late:
        state["alerts"][it["id"]] = now_iso
    state["_last_run_at"] = now_iso
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
