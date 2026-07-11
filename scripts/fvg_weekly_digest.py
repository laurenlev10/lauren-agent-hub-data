#!/usr/bin/env python3
"""
fvg_weekly_digest.py — weekly SMS to Lauren summarizing the FVG strategy's REAL
performance (from broker-ledger.json) + the indicator-vs-broker gap and
execution issues (from fvg-journal.json, once the theoretical feed is live).

Trading is Lauren's personal activity -> SMS to Lauren only.
"""
import json, os, sys, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms

REPO = Path(__file__).resolve().parent.parent
COMM = 1.82
FVG_START = "2026-07-09T21:00:00Z"  # FVG began IL 2026-07-10 00:00; earlier = VWAP
LAUREN_PHONE = os.environ.get("LAUREN_PHONE", "4243547625")


def pi(s):
    try: return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def group(legs):
    legs = sorted(legs, key=lambda t: t.get("entry_iso", ""))
    out, cur, prev = [], [], None
    for t in legs:
        et = pi(t.get("entry_iso", ""))
        if prev is None or (et - prev).total_seconds() <= 90: cur.append(t)
        else: out.append(cur); cur = [t]
        prev = et
    if cur: out.append(cur)
    res = []
    for s in out:
        q = sum(x.get("qty", 0) for x in s)
        gross = sum(x.get("result_dollars", 0) for x in s)
        res.append({"iso": s[0]["entry_iso"], "la": s[0].get("entry_la", ""),
                    "dir": s[0].get("direction"), "qty": q,
                    "net": round(gross - q * COMM, 2), "legs": len(s)})
    return res


def main():
    led = json.loads((REPO / "docs/trading/broker-ledger.json").read_text())
    legs = led.get("trades", [])
    now = dt.datetime.now(dt.timezone.utc)
    wk = now - dt.timedelta(days=7)
    weekly = [t for t in legs if (t.get("entry_iso","") >= FVG_START) and pi(t.get("entry_iso","")) and pi(t["entry_iso"]) >= wk]
    if not weekly:
        print("no FVG trades this week — skipping SMS"); return
    trades = group(weekly)
    net = round(sum(t["net"] for t in trades), 2)
    wins = sum(1 for t in trades if t["net"] >= 0)
    wr = round(100 * wins / len(trades))
    partial = sum(1 for t in trades if t["qty"] < 4)
    # per-day best/worst
    byday = {}
    for t in trades:
        d = (t["la"] or "").split(" ")[0]
        byday[d] = byday.get(d, 0) + t["net"]
    best = max(byday.items(), key=lambda x: x[1]) if byday else ("", 0)
    worst = min(byday.items(), key=lambda x: x[1]) if byday else ("", 0)

    lines = ["📊 סיכום שבועי — FVG (חשבון אמיתי)",
             f"רווח נטו: ${net:,.2f}",
             f"{len(trades)} עסקאות · {wr}% הצלחה",
             f"יום הכי טוב: {best[0][:5]} ${best[1]:,.0f} · הכי גרוע: {worst[0][:5]} ${worst[1]:,.0f}"]
    if partial:
        lines.append(f"⚠ {partial} מילויים חלקיים (פחות מ-4 חוזים)")

    # gap vs indicator (if theoretical feed active)
    fvgp = REPO / "docs/trading/fvg-journal.json"
    try:
        fvg = json.loads(fvgp.read_text())
        entries = [e for e in fvg.get("events", []) if e.get("event") == "entry"
                   and pi((e.get("received_at") or "")) and pi(e["received_at"]) >= wk]
    except Exception:
        entries = []
    if entries:
        # simple phantom count: theoretical entries with no broker trade within 6 min
        def tmin(s):
            d = pi(s)
            return d.timestamp() if d else 0
        btimes = [pi(t["iso"]).timestamp() for t in trades if pi(t["iso"])]
        phantom = 0
        for e in entries:
            et = None
            try: et = dt.datetime.strptime(e["la"], "%d/%m/%Y %H:%M:%S").timestamp()
            except Exception: continue
            if not any(abs(bt - et) < 360 for bt in btimes):
                phantom += 1
        lines.append(f"אינדיקטור: {len(entries)} כניסות · {phantom} רפאים (לא נכנסו בברוקר)")
    else:
        lines.append("(הזנת האינדיקטור עדיין לא פעילה — פער יתווסף כשתופעל)")

    lines.append("dashboard.themakeupblowout.com/trading/fvg/")
    body = "\n".join(lines)
    try:
        send_sms(LAUREN_PHONE, body)
        print("✓ weekly FVG SMS sent:\n" + body)
    except Exception as e:
        print("SMS failed:", str(e)[:150]); raise


if __name__ == "__main__":
    main()
