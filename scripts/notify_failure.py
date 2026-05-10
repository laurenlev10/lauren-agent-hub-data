"""
notify_failure — shared helper for all GitHub Actions workflows in
                 laurenlev10/lauren-agent-hub-data.

IRON RULE (Lauren 2026-05-10): every scheduled workflow MUST end with
an `if: failure()` step that runs this script. If a workflow fails for
any reason (API outage, expired token, code error, network timeout),
Lauren receives a Hebrew SMS naming the workflow and linking to the
failed run page so she can investigate quickly.

Reads from environment:
    WORKFLOW_NAME — the workflow name (set automatically via ${{ github.workflow }})
    RUN_URL       — link to the failed run (set via ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }})
    JOB_NAME      — optional, the failing job (${{ github.job }})

Sends SMS only to Lauren (NOT Eli — failure alerts are operational
noise, only the owner needs them).

Fail-soft: if the SMS itself fails (token issue, network), prints the
error to the log instead of crashing — the workflow is already in a
failed state and we don't want to compound the noise.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms, LAUREN_PHONE


def main() -> int:
    workflow = os.environ.get("WORKFLOW_NAME", "(unknown workflow)")
    run_url  = os.environ.get("RUN_URL", "")
    job_name = os.environ.get("JOB_NAME", "")

    body_lines = [
        "⚠ משימה מתוזמנת נכשלה",
        "",
        f"Workflow: {workflow}",
    ]
    if job_name:
        body_lines.append(f"Job: {job_name}")
    if run_url:
        body_lines += ["", "פתחי את ה-log:", run_url]
    body = "\n".join(body_lines)

    if not LAUREN_PHONE:
        print("[notify_failure] LAUREN_PHONE not set; nothing to do.")
        return 0
    if not os.environ.get("SIMPLETEXTING_TOKEN"):
        print("[notify_failure] SIMPLETEXTING_TOKEN missing; can't send SMS.")
        return 0

    try:
        resp = send_sms(LAUREN_PHONE, body)
        print(f"[notify_failure] SMS sent: id={resp.get('id', '?')}")
    except Exception as e:
        # Never re-raise — the workflow already failed; we don't want to
        # mask the original error or pollute the run with a second exception.
        print(f"[notify_failure] SMS send failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
