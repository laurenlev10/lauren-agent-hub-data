"""
meta_inbox_weekly_digest — Sundays 8pm PT.

Reads docs/meta/inbox-api-preview/data.json (the snapshot the daily run produces)
+ docs/meta/handled.json (all-time handled state) and computes a weekly KPI summary
SMS to Lauren + Eli.

Metrics:
- Total DMs received this week (Mon-Sun)
- Total FB + IG comments received
- Response rate: replied / received (auto-replied + Lauren-manual)
- Average time-to-reply (where computable)
- Top topics (Bucket A categories) — most common KB matches
- Sentiment breakdown: angry/complaint/positive/neutral counts
- Urgent count: items flagged urgent

Set 2026-05-14 PM.
"""
import os, json, datetime, sys
from pathlib import Path
from collections import Counter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from lauren_sms import send_sms, LAUREN_PHONE

ELI_PHONE = os.environ.get("ELI_PHONE", "").strip()
DATA = REPO / "docs" / "meta" / "inbox-api-preview" / "data.json"
HANDLED = REPO / "docs" / "meta" / "handled.json"


def main() -> int:
    if not DATA.exists():
        print(f"[weekly] {DATA} not found")
        return 0
    snap = json.loads(DATA.read_text())
    handled = json.loads(HANDLED.read_text()) if HANDLED.exists() else {}

    week_start = datetime.date.today() - datetime.timedelta(days=7)
    week_start_iso = week_start.isoformat()

    msgs = snap.get("messenger", []) or []
    fb_c = snap.get("fb_comments", []) or []
    ig_c = snap.get("ig_comments", []) or []

    # Total counts (in the current snapshot — which is a window not strictly 1 week,
    # but the daily run keeps it fresh so the count approximates this week)
    total_dm = len(msgs)
    total_fb = len(fb_c)
    total_ig = len(ig_c)
    total = total_dm + total_fb + total_ig

    # Handled this week
    week_handled = 0
    auto = 0
    manual = 0
    for k, h in handled.items():
        when = h.get("handledAt", "")
        if when and when >= week_start_iso:
            week_handled += 1
            method = h.get("method", "")
            if "auto" in method or "phase2" in method:
                auto += 1
            else:
                manual += 1

    # Response rate — handled-this-week / (handled-this-week + still-open-now).
    # This is a fair approximation: handled vs in-the-queue.
    denom = week_handled + total
    response_rate = (week_handled / denom * 100) if denom > 0 else 0

    # Sentiment + urgent across all classified
    all_items = msgs + fb_c + ig_c
    sentiment = Counter(it.get("sentiment", "neutral") for it in all_items)
    urgent = sum(1 for it in all_items if it.get("urgent"))

    # Top topics — Bucket A reply intent
    topics = Counter()
    for it in all_items:
        cls = it.get("cls", {})
        if cls.get("bucket") == "A":
            reason = (cls.get("reason") or "").lower()
            for kw in ("event", "אירוע", "city", "עיר", "date", "תאריך", "address", "כתובת", "hours", "שעות", "free", "חינם", "kids", "ילדים", "payment", "תשלום"):
                if kw in reason:
                    topics[kw] += 1
                    break

    top3 = ", ".join(f"{k}({n})" for k, n in topics.most_common(3)) or "—"

    body_lines = [
        f"📊 Meta Inbox · סיכום שבועי · {week_start.strftime('%-d.%-m')}–היום",
        "",
        f"📥 {total} פניות: {total_dm} DM · {total_fb} FB · {total_ig} IG",
        f"✓ {week_handled} טופלו ({auto} auto · {manual} ידני)",
        f"   response rate: {response_rate:.0f}%",
        "",
        f"🎭 sentiment: {sentiment.get('positive',0)}+ · {sentiment.get('neutral',0)}~ · {sentiment.get('complaint',0)}? · {sentiment.get('angry',0)}!",
    ]
    if urgent:
        body_lines.append(f"🚨 {urgent} urgent (כבר נשלחו התראות)")
    body_lines.append(f"📌 top topics: {top3}")
    body_lines.append("")
    body_lines.append("👉 laurenlev10.github.io/lauren-agent-hub-data/meta/")
    body = "\n".join(body_lines)

    print(body)
    print()

    recipients = []
    if LAUREN_PHONE: recipients.append(("Lauren", LAUREN_PHONE))
    if ELI_PHONE:    recipients.append(("Eli", ELI_PHONE))
    if not recipients or not os.environ.get("SIMPLETEXTING_TOKEN"):
        print("[weekly] no SMS env — dry-run")
        return 0
    for name, phone in recipients:
        try:
            send_sms(phone, body)
            print(f"  ✓ sent to {name}")
        except Exception as e:
            print(f"  ⚠ SMS to {name} failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
