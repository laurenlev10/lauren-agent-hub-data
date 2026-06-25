#!/usr/bin/env python3
"""Weekly FORWARD-TEST digest — compares live trading (after strategy_started_at)
vs the indicator backtest baseline, and SMSes Lauren. Forward = exit rows in the
live journal dated at/after autotrade_enabled.json:strategy_started_at."""
import os, sys, json
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lauren_sms import send_sms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)
def parse(s):
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception: return None
def pf_of(pnls):
    gp = sum(x for x in pnls if x > 0); gl = abs(sum(x for x in pnls if x < 0))
    return (round(gp/gl, 2) if gl else None)

LAUREN = os.environ.get("LAUREN_PHONE", "4243547625")
DASH = "https://dashboard.themakeupblowout.com/trading/analytics/"

journal = load("docs/trading/journal-data.json")
at      = load("docs/trading/autotrade_enabled.json")
bt      = load("docs/trading/analytics/backtest-data.json")

start = parse(at.get("strategy_started_at"))
exits = [t for t in journal.get("trades", [])
         if t.get("action") == "exit" and t.get("result_dollars") is not None]
if start:
    exits = [t for t in exits if (parse(t.get("_received_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= start]

# backtest baseline
btp = [t.get("result_dollars", 0) for t in bt.get("trades", [])]
bt_pf = pf_of(btp); bt_wr = bt.get("summary_computed", {}).get("win_rate_pct", "—")

if not exits:
    body = ("📊 מבחן קדימה — VWAP 1.0\n"
            "אין עדיין עסקאות חדשות (מאז תחילת המבחן). הבוט מחובר וממתין.\n"
            "אעדכן בשבוע הבא. " + DASH)
    send_sms(LAUREN, body)
    print("no forward trades — idle note sent")
    sys.exit(0)

pnl = [t["result_dollars"] for t in exits]
net = round(sum(pnl), 2)
wins = [x for x in pnl if x > 0]
wr = round(len(wins) / len(exits) * 100, 1)
pf = pf_of(pnl)
pf_disp = "∞" if pf is None else str(pf)
net_disp = ("+$" if net >= 0 else "-$") + f"{abs(net):.0f}"

if pf is not None and pf >= 1.5:   verdict = "✅ מחזיק יפה מול הבקטסט"
elif pf is not None and pf >= 1.0: verdict = "🟡 חיובי אך מתחת ליעד (~1.65)"
else:                              verdict = "🔴 מתחת לציפייה — כדאי לבדוק"

body = ("📊 מבחן קדימה — VWAP 1.0 (שבוע)\n"
        f"עסקאות: {len(exits)} · נטו {net_disp} · PF {pf_disp} · הצלחה {wr}%\n"
        f"Baseline בקטסט: PF {bt_pf} · הצלחה {bt_wr}% (יעד מסונן ~1.65)\n"
        f"{verdict}\n{DASH}")
send_sms(LAUREN, body)
print("forward digest sent:", body.replace("\n", " | "))
