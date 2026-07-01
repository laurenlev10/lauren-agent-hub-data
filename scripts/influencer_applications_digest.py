"""
influencer_applications_digest — daily SMS to Lauren when new influencer
applications arrived via events.themakeupblowout.com/collab/ .

Reads docs/state/influencer_applications.json (written by the manager-report
Cloudflare Worker, kind=influencer_application). Applications newer than the
agent-owned watermark `_notified_through` are summarized in a Hebrew SMS to
Lauren (Lauren only). Then the watermark advances and the file is committed
by the workflow.

SMS length safety (lauren-comms IRON RULE): body capped, dashboard URL sent
as a SEPARATE second SMS.

Exit 0 always (fail-soft on SMS errors is NOT allowed here — a failed send
must fail the workflow so the IRON RULE #3 step alerts Lauren — but "no new
applications" is a normal, silent exit).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms, LAUREN_PHONE

STATE = Path("docs/state/influencer_applications.json")
DASH_URL = "https://laurenlev10.github.io/lauren-agent-hub-data/pr-influencer/"


def main() -> int:
    if not STATE.exists():
        print("no state file — nothing to do")
        return 0
    data = json.loads(STATE.read_text(encoding="utf-8"))
    apps = data.get("applications") or []
    mark = data.get("_notified_through") or ""
    new = [a for a in apps if (a.get("received_at") or "") > mark]
    if not new:
        print(f"no new applications (total {len(apps)}, watermark {mark or '—'})")
        return 0

    new.sort(key=lambda a: a.get("received_at") or "")
    lines = [f"@pr-influencer 📥 {len(new)} מועמדויות חדשות מדף Collab:"]
    for a in new[:3]:
        handle = (a.get("handle") or "?").lstrip("@")
        lines.append(f"@{handle} · {a.get('followers','?')} עוקבים · {a.get('city','?')}")
    if len(new) > 3:
        lines.append(f"...ועוד {len(new) - 3}. הכל בדשבורד:")
    body = "\n".join(lines)[:275]

    send_sms(LAUREN_PHONE, body)
    send_sms(LAUREN_PHONE, DASH_URL)

    data["_notified_through"] = max(a.get("received_at") or "" for a in new)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"notified {len(new)} new; watermark → {data['_notified_through']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
