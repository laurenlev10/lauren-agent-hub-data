#!/usr/bin/env python3
"""SMS Lauren the Monday-morning recap: sales, RECOUNT changes, top order recommendations."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
try: from lauren_sms import send_sms
except ImportError:
    def send_sms(phone, body):
        print(f"(would SMS to {phone}): {body}")
        return True

ws = json.loads(Path('docs/state/weekend_sales.json').read_text()) if Path('docs/state/weekend_sales.json').exists() else {}
recs = json.loads(Path('docs/state/order_recommendations.json').read_text()) if Path('docs/state/order_recommendations.json').exists() else {}
inv_archive = json.loads(Path('docs/state/invoice_archive.json').read_text()) if Path('docs/state/invoice_archive.json').exists() else {}

# Last weekend summary
weekends = (ws.get('weekends') or [])
last = weekends[-1] if weekends else {}
sales_count = last.get('sales_computed', 0)
tags_added = last.get('tags_added', 0)
tags_removed = last.get('tags_removed', 0)
event_date = last.get('event_date', '?')

# Top suppliers by total recommendation $
sup_totals = []
for sc, info in (recs.get('suppliers') or {}).items():
    sup_totals.append((sc, info.get('total_usd', 0), len(info.get('lines') or [])))
sup_totals.sort(key=lambda x: -x[1])
top_str = '\n'.join(f"• {sc}: {n}p ${t:.0f}" for sc, t, n in sup_totals[:5])
total_rec = sum(s[1] for s in sup_totals)
total_lines = sum(s[2] for s in sup_totals)
backorders = sum(len(v) for v in (inv_archive.get('backorders') or {}).values())

body = (
    f"📊 סיכום סופ\"ש {event_date}\n"
    f"• {sales_count} מוצרים עם מכירות\n"
    f"• {tags_added} RECOUNT נוספו, {tags_removed} הוסרו\n"
    f"• {backorders} backorders פעילים\n\n"
    f"📦 המלצות הזמנה ({total_lines} פריטים · ${total_rec:.0f}):\n"
    f"{top_str}\n\n"
    f"https://dashboard.themakeupblowout.com/inventory/?mode=global"
)
phone = os.environ.get('LAUREN_PHONE', '4243547625')
try: ok = send_sms(phone, body)
except Exception as e: print(f"SMS failed: {e}"); ok = False
print(f"{'✓ sent' if ok else '✗ failed'} to {phone}\n--- body ---\n{body}")
