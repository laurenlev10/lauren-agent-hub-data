#!/usr/bin/env python3
"""run_summary.py - record a task's last-run summary into docs/scheduled-runs.json.

Lets the /scheduled/ dashboard show, per task, a short bullet summary of what the
last run actually did (the expandable arrow). Also bumps lastRunAt + sets lastStatus.

CLI:  python3 scripts/run_summary.py <task_id> --status ok -b "bullet 1" -b "bullet 2"
API:  from run_summary import record; record("task-id", ["b1","b2"], status="ok")

Defensive by design: never raises into the caller's main job - callers should wrap
in try/except so a dashboard-cosmetic failure can't break the real task.
"""
import argparse, datetime, json
from pathlib import Path

REGISTRY = Path("docs/scheduled-runs.json")
MAX_BULLETS = 8


def record(task_id, bullets, status="ok", run_url=None, when=None):
    if not REGISTRY.exists():
        print(f"[run_summary] {REGISTRY} missing; skip")
        return False
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    now = (when or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bullets = [str(b).strip() for b in (bullets or []) if str(b).strip()][:MAX_BULLETS]
    hit = False
    for t in data.get("tasks", []):
        if t.get("id") == task_id:
            t["lastSummary"] = bullets
            t["lastStatus"] = status
            t["lastRunAt"] = now
            if run_url:
                t["lastRunUrl"] = run_url
            hit = True
            break
    if not hit:
        print(f"[run_summary] task id '{task_id}' not found in registry")
        return False
    data["_updated_at"] = now
    REGISTRY.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[run_summary] recorded {len(bullets)} bullet(s) for {task_id} (status={status})")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--status", default="ok")
    ap.add_argument("--run-url", default=None)
    ap.add_argument("-b", "--bullet", action="append", default=[], dest="bullets")
    a = ap.parse_args(argv)
    return 0 if record(a.task_id, a.bullets, status=a.status, run_url=a.run_url) else 1


if __name__ == "__main__":
    raise SystemExit(main())
