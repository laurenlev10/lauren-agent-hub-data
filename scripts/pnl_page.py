#!/usr/bin/env python3
"""pnl_page.py — render a fully-itemized per-event P&L dashboard (RTL Hebrew HTML).

Takes the dict from pnl_build.build_pnl and produces a standalone web page that
itemizes EVERY income and expense line for one event: sales + payment breakdown +
top products; inventory per supplier; staff per person; each meal/other expense;
marketing (Meta/TikTok); travel/venue (QuickBooks, pending until approved).

    from scripts.pnl_page import render_pnl_page
    html = render_pnl_page(pnl_dict)
"""
from __future__ import annotations
import argparse, html as _html, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _m(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "— ממתין"


def _esc(s):
    return _html.escape(str(s if s is not None else ""))


def _rows(items, cols):
    out = []
    for it in items:
        tds = "".join(f"<td class='{c.get('cls','')}'>{c['fmt'](it)}</td>" for c in cols)
        out.append(f"<tr>{tds}</tr>")
    return "".join(out)


def render_pnl_page(p):
    ev = p["event"]; r = p["revenue"]; d = p.get("detail", {}); exp = p["expenses"]
    prelim = p.get("preliminary")
    profit = p.get("profit_preliminary"); margin = p.get("margin")
    pos = (profit is not None and profit >= 0)
    accent = "#16a34a" if pos else "#dc2626"
    res_word = "רווח" if pos else "הפסד"
    title = f"{ev.get('city') or ''}, {ev.get('state') or ''}"

    # KPI cards
    kpis = [("מכירות נטו", _m(r.get("net_sales"))), ("ברוטו", _m(r.get("gross_sales"))),
            (res_word, _m(profit)), ("מרווח", f"{margin*100:.1f}%" if margin is not None else "—"),
            ("עסקאות", str(r.get("transactions") or "—")), ("ממוצע לעסקה", _m(r.get("avg_ticket")))]
    kpi_html = "".join(
        f"<div class='kpi'><div class='kv'>{_esc(v)}</div><div class='kl'>{_esc(l)}</div></div>"
        for l, v in kpis)

    # payment breakdown
    pay = d.get("payment_breakdown", {})
    pay_rows = "".join(f"<tr><td>{_esc(k)}</td><td class='num'>{_m(v)}</td></tr>" for k, v in pay.items())

    # top products
    tp = p.get("top_products", [])
    tp_rows = "".join(
        f"<tr><td>{i}</td><td>{_esc(t.get('name'))}</td><td>{_esc(t.get('vendor'))}</td>"
        f"<td class='num'>{t.get('units_sold'):.0f}</td><td class='num'>{_m(t.get('revenue'))}</td></tr>"
        for i, t in enumerate(tp, 1))

    # ---- expense itemizations ----
    inv_lines = d.get("inventory_lines", [])
    inv_rows = "".join(
        f"<tr><td>{_esc(x.get('supplier'))}</td><td class='num'>{_m(x.get('invoiced'))}</td>"
        f"<td class='num'>{_m(x.get('shipping'))}</td>"
        f"<td>{'⚠ הוחרג' if x.get('status')=='anomaly-excluded' else ('✓' if x.get('status')=='invoiced' else '—')}</td></tr>"
        for x in inv_lines)
    staff_lines = d.get("staff_lines", [])
    staff_rows = "".join(
        f"<tr><td>{_esc(x.get('name'))}</td><td class='num'>{_m(x.get('amount'))}</td></tr>" for x in staff_lines)
    mlines = d.get("manager_expense_lines", [])
    meal_rows = "".join(f"<tr><td>{_esc(x.get('desc'))}</td><td class='num'>{_m(x.get('amount'))}</td></tr>"
                        for x in mlines if x.get("category") == "meals")
    other_rows = "".join(f"<tr><td>{_esc(x.get('desc'))}</td><td class='num'>{_m(x.get('amount'))}</td></tr>"
                         for x in mlines if x.get("category") != "meals")
    mk = d.get("marketing", {})
    mb = d.get("mystery_box", {}) or {}
    mb_name = _esc(mb.get("name") or "—")
    mb_units = f"{mb.get('units'):.0f}" if mb.get("units") else "0"
    mb_unit = _m(mb.get("unit_cost")) if mb.get("unit_cost") else "$15.00"
    mb_cost = _m(exp.get("mystery_box", {}).get("amount"))

    def exp_status(key):
        e = exp.get(key, {})
        if e.get("source") == "manual override":
            return "<span class='manual'>✏️ ידני</span>"
        return "" if e.get("status") == "ok" else f"<span class='pend'>{_esc(e.get('status'))}</span>"

    # P&L summary rows
    pl_rows = [
        ("מלאי", exp.get("inventory", {}).get("amount"), exp_status("inventory")),
        ("משלוח", exp.get("shipping", {}).get("amount"), exp_status("shipping")),
        ("מיסטרי בוקס", exp.get("mystery_box", {}).get("amount"), exp_status("mystery_box")),
        ("צוות", exp.get("staff", {}).get("amount"), exp_status("staff")),
        ("אוכל", exp.get("meals", {}).get("amount"), exp_status("meals")),
        ("שונות", exp.get("other", {}).get("amount"), exp_status("other")),
        ("שיווק — Meta", exp.get("marketing_meta", {}).get("amount"), exp_status("marketing_meta")),
        ("שיווק — TikTok", exp.get("marketing_tiktok", {}).get("amount"), exp_status("marketing_tiktok")),
        ("נסיעות (QB)", exp.get("travel", {}).get("amount"), exp_status("travel")),
        ("מקום (QB)", exp.get("venue", {}).get("amount"), exp_status("venue")),
        ("ULINE — ציוד לאירוע", exp.get("uline", {}).get("amount"), exp_status("uline")),
    ]
    pl_html = "".join(
        f"<tr><td>{_esc(l)} {s}</td><td class='num'>{_m(a)}</td></tr>" for l, a, s in pl_rows)

    pending = p.get("pending_or_missing", [])
    pending_banner = (f"<div class='warn'>⏳ עדיין ממתין/חסר: {_esc(', '.join(pending))} — "
                      f"לכן הרווח <b>ראשוני</b> ויתעדכן כשהמקורות יושלמו.</div>") if prelim else ""
    warn_list = "".join(f"<li>{_esc(w)}</li>" for w in p.get("warnings", []))
    warn_html = f"<div class='warn'><b>שים לב:</b><ul>{warn_list}</ul></div>" if warn_list else ""

    cc = p.get("cash_check")
    cash_html = ""
    if cc:
        ok = abs(cc.get("pct", 0)) <= 0.02
        cash_html = (f"<div class='cash {'good' if ok else 'bad'}'>הצלבת מזומן: מנהלת {_m(cc['manager_cash'])} "
                     f"מול OCTOPOS {_m(cc['octopos_cash'])} — פער {_m(cc['diff'])} ({cc['pct']*100:.1f}%)</div>")

    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>סיכום אירוע — {_esc(title)}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI',Arial,sans-serif;background:#f5f5f7;color:#1d1d1f;margin:0;padding:24px;line-height:1.5}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:26px;margin:0 0 2px}}
.sub{{color:#666;margin-bottom:16px}}
.banner{{background:{accent};color:#fff;border-radius:14px;padding:18px 22px;margin-bottom:18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
.banner .big{{font-size:30px;font-weight:800}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}}
.kpi{{background:#fff;border-radius:12px;padding:14px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.kpi .kv{{font-size:20px;font-weight:700}}
.kpi .kl{{color:#777;font-size:13px;margin-top:2px}}
.card{{background:#fff;border-radius:12px;padding:16px 18px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.card h2{{font-size:17px;margin:0 0 10px;border-bottom:2px solid #f0c;padding-bottom:6px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:7px 8px;text-align:right;border-bottom:1px solid #eee}}
th{{color:#888;font-weight:600;font-size:12px}}
.num{{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}}
.subtot td{{font-weight:800;border-top:2px solid #ddd;border-bottom:none}}
.pl td{{font-size:15px}}
.pl .grand td{{font-size:18px;font-weight:800;color:{accent};border-top:3px solid #333}}
.pend{{background:#fde68a;color:#92400e;font-size:11px;border-radius:6px;padding:1px 6px;margin-right:6px}}
.warn{{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:12px 14px;margin-bottom:14px;font-size:14px}}
.warn ul{{margin:6px 0 0;padding-right:18px}}
.cash{{border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:14px}}
.cash.good{{background:#ecfdf5;border:1px solid #a7f3d0}}
.cash.bad{{background:#fef2f2;border:1px solid #fecaca}}
.foot{{color:#999;font-size:12px;text-align:center;margin-top:18px}}
</style></head><body><div class="wrap">
<h1>סיכום אירוע — {_esc(title)}</h1>
<div class="sub">{_esc(ev.get('start_date'))} – {_esc(ev.get('end_date'))} · {_esc(ev.get('venue') or '')}</div>
<div class="banner"><div>{res_word}{' (ראשוני)' if prelim else ''}</div>
  <div class="big">{_m(profit)}{f' · {margin*100:.1f}%' if margin is not None else ''}</div></div>
{pending_banner}
<div class="kpis">{kpi_html}</div>

<div class="card"><h2>💰 הכנסות</h2>
<table>
<tr><td>מכירות נטו</td><td class="num">{_m(r.get('net_sales'))}</td></tr>
<tr><td>מכירות ברוטו (כולל מס)</td><td class="num">{_m(r.get('gross_sales'))}</td></tr>
<tr><td>מס שנגבה</td><td class="num">{_m(r.get('tax'))}</td></tr>
<tr><td>עסקאות</td><td class="num">{r.get('transactions') or '—'}</td></tr>
<tr><td>ממוצע לעסקה</td><td class="num">{_m(r.get('avg_ticket'))}</td></tr>
</table>
<h2 style="margin-top:16px">פירוט תשלומים</h2>
<table>{pay_rows or '<tr><td>—</td></tr>'}</table>
</div>

<div class="card"><h2>🏆 מוצרים מובילים</h2>
<table><tr><th>#</th><th>מוצר</th><th>ספק</th><th>יח׳</th><th>הכנסה</th></tr>{tp_rows or '<tr><td>—</td></tr>'}</table>
</div>

<div class="card"><h2>📦 מלאי לפי ספק</h2>
<table><tr><th>ספק</th><th>חשבונית</th><th>משלוח</th><th>סטטוס</th></tr>{inv_rows or '<tr><td colspan=4>אין נתוני חשבוניות</td></tr>'}
<tr class="subtot"><td>סה״כ מלאי (בלי משלוח) / משלוח</td><td class="num">{_m(exp.get('inventory',{}).get('amount'))}</td><td class="num">{_m(exp.get('shipping',{}).get('amount'))}</td><td></td></tr>
</table></div>

<div class="card"><h2>🎁 מיסטרי בוקס (עלות לפי יחידות)</h2>
<table><tr><th>מוצר</th><th>יח׳ נמכרו</th><th>עלות ליח׳</th><th>סה״כ עלות</th></tr>
<tr><td>{mb_name}</td><td class="num">{mb_units}</td><td class="num">{mb_unit}</td><td class="num">{mb_cost}</td></tr></table>
<div style="color:#777;font-size:12px;margin-top:6px">מחושב מ-OCTOPOS: יחידות שנמכרו × עלות ליחידה (לא לפי חשבונית).</div></div>
<div class="card"><h2>👥 צוות</h2>
<table><tr><th>שם</th><th>תשלום</th></tr>{staff_rows or '<tr><td colspan=2>—</td></tr>'}
<tr class="subtot"><td>סה״כ צוות</td><td class="num">{_m(exp.get('staff',{}).get('amount'))}</td></tr></table></div>

<div class="card"><h2>🍽️ אוכל</h2>
<table>{meal_rows or '<tr><td colspan=2>—</td></tr>'}
<tr class="subtot"><td>סה״כ אוכל</td><td class="num">{_m(exp.get('meals',{}).get('amount'))}</td></tr></table>
<h2 style="margin-top:16px">🧾 שונות</h2>
<table>{other_rows or '<tr><td colspan=2>—</td></tr>'}
<tr class="subtot"><td>סה״כ שונות</td><td class="num">{_m(exp.get('other',{}).get('amount'))}</td></tr></table></div>

<div class="card"><h2>📣 שיווק</h2>
<table>
<tr><td>Meta</td><td class="num">{_m(mk.get('meta'))}</td></tr>
<tr><td>TikTok</td><td class="num">{_m(mk.get('tiktok'))}</td></tr>
</table></div>

<div class="card pl"><h2>📊 רווח והפסד — סיכום</h2>
<table>
<tr><td>מכירות נטו (הכנסה)</td><td class="num">{_m(r.get('net_sales'))}</td></tr>
{pl_html}
<tr class="subtot"><td>סך הוצאות ידועות</td><td class="num">{_m(p.get('total_known_expenses'))}</td></tr>
<tr class="grand"><td>{res_word}{' ראשוני' if prelim else ''}</td><td class="num">{_m(profit)}</td></tr>
</table>{cash_html}</div>

{warn_html}
<div class="foot">נוצר אוטומטית · מקורות: OCTOPOS (מכירות) · inventory_orders (מלאי) · דוח מנהלת (מזומן/צוות) · event_analytics (פרסום) · QuickBooks (נסיעות/מקום){' · נתונים חלקיים — חלק מהמקורות עדיין בביקורת' if prelim else ''}</div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pnl-json", help="path to a build_pnl JSON dump")
    ap.add_argument("--out", required=True)
    # or build live:
    ap.add_argument("--evkey"); ap.add_argument("--launch"); ap.add_argument("--inv-state")
    ap.add_argument("--mgr-state"); ap.add_argument("--analytics")
    args = ap.parse_args()
    if args.pnl_json:
        p = json.loads(Path(args.pnl_json).read_text(encoding="utf-8"))
    else:
        import pnl_build
        p = pnl_build.build_pnl(args.evkey, launch_html=args.launch, inv_state=args.inv_state,
                                mgr_state=args.mgr_state, analytics_path=args.analytics)
    Path(args.out).write_text(render_pnl_page(p), encoding="utf-8")
    print(f"wrote {args.out} ({Path(args.out).stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
