"""
lauren_digest_sms — per-event marketing pulse SMS digest.

Reads docs/state/conversion_history.json (the aggregator's output) and sends
Lauren a concise Hebrew SMS per event with active campaigns. One SMS per event
so each is readable on its own.

Set 2026-05-14 PM. Lauren's directive: "אני רוצה לקבל את הנתונים האלו כל 6
שעות בצורה מסודרת ב-SMS בקצרה לכל אירוע שיש עליו כבר תקציב וקמפיינים פעילים".

Wired into .github/workflows/marketing-stats.yml — runs only on 9am/9pm PT
crons (SHOULD_SEND_SMS=true) to avoid spamming Lauren every 6 hours overnight.

Env:
  SIMPLETEXTING_TOKEN — required
  LAUREN_PHONE        — default 4243547625
"""
import json, os, sys, datetime, urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from lauren_sms import send_sms, LAUREN_PHONE

CONV_HISTORY = REPO / "docs" / "state" / "conversion_history.json"
NOTES        = REPO / "docs" / "launch" / "notes.json"

ACTIVE_SPEND_THRESHOLD = 5.0  # $/30d — below this we don't dignify with SMS


def event_days_label(slug: str) -> str:
    """Return Hebrew label for days to event, e.g. 'בעוד 9 ימים' or 'מחר!'."""
    # slug like cleveland-oh-2026 — we need a date. Pull from notes.json mapping.
    if not NOTES.exists():
        return ""
    try:
        notes = json.loads(NOTES.read_text())
    except Exception:
        return ""
    city_prefix = slug.split("-")[0]
    year_suffix = slug.split("-")[-1]
    candidates = [k for k in notes if k.startswith(city_prefix + "-") and year_suffix in k]
    if not candidates: return ""
    # The notes key format is "<city>-<start_date>" e.g. cleveland-2026-05-29
    parts = candidates[0].split("-")
    if len(parts) < 4: return ""
    try:
        d = datetime.date(int(parts[-3]), int(parts[-2]), int(parts[-1]))
        today = datetime.date.today()
        delta = (d - today).days
        if delta < 0: return f"לפני {-delta} ימים"
        if delta == 0: return "היום!"
        if delta == 1: return "מחר!"
        if delta == 2: return "מחרתיים"
        if delta <= 7: return f"בעוד {delta} ימים"
        return f"{delta}d"
    except Exception:
        return ""


def fmt_money(n) -> str:
    n = int(float(n or 0))
    return f"${n:,}"


def compose_event_sms(slug: str, ev: dict) -> str:
    """Build one concise Hebrew SMS for one event."""
    m = ev.get("meta", {})
    t = ev.get("tiktok", {})
    rs = ev.get("reel_shares", {})

    meta_spend = float(m.get("spend", 0) or 0)
    tt_spend   = float(t.get("spend", 0) or 0)
    total_spend = meta_spend + tt_spend

    meta_leads = int(m.get("leads", 0) or 0)
    tt_convs   = int(t.get("conversions", 0) or 0)
    total_forms = meta_leads + tt_convs

    # Best channel by CPF (cost per form)
    cpf_meta = meta_spend / meta_leads if meta_leads else None
    cpf_tt   = tt_spend / tt_convs if tt_convs else None

    # City label
    city = slug.split("-")[0].title()
    state = slug.split("-")[1].upper() if len(slug.split("-")) >= 2 else ""
    days = event_days_label(slug)

    lines = []
    title = f"🎯 {city}"
    if state: title += f", {state}"
    if days: title += f" · {days}"
    lines.append(title)

    # Spend line
    spend_line = f"{fmt_money(total_spend)} spend"
    if meta_spend and tt_spend:
        spend_line += f" (Meta {fmt_money(meta_spend)} + TT {fmt_money(tt_spend)})"
    elif tt_spend:
        spend_line += " (TikTok only)"
    elif meta_spend:
        spend_line += " (Meta only)"
    lines.append(spend_line)

    # Forms line — only crown a "best" when BOTH channels have form data;
    # otherwise just show the count + which channel is reporting.
    if total_forms > 0:
        parts = []
        if meta_leads > 0:
            parts.append(f"Meta {meta_leads} @ ${cpf_meta:.2f}")
        if tt_convs > 0:
            parts.append(f"TT {tt_convs} @ ${cpf_tt:.2f}")
        lines.append(f"📝 {total_forms} forms · " + " · ".join(parts))
        # Comparative call-out only when both channels are non-zero
        if meta_leads >= 5 and tt_convs >= 5 and cpf_meta and cpf_tt:
            ratio = max(cpf_meta, cpf_tt) / min(cpf_meta, cpf_tt)
            if ratio >= 1.5:
                cheaper = "Meta" if cpf_meta < cpf_tt else "TT"
                lines.append(f"💡 {cheaper} זול פי {ratio:.1f} מהשני — להזיז תקציב")
    else:
        lines.append("📝 0 forms (Meta pixel חדש, צפי 24-48h)")

    # Reel shares — surface PROMINENTLY per IRON RULE "shares are #1"
    total_shares = int(rs.get("total", 0) or 0)
    if total_shares > 0:
        paid = int(rs.get("paid", 0) or 0)
        organic = int(rs.get("organic", 0) or 0)
        d6 = int(rs.get("delta_6h", 0) or 0)
        delta_str = f" (+{d6})" if d6 else ""
        lines.append(f"📸 Reel: {total_shares} shares{delta_str} · {organic} organic + {paid} paid")
    elif rs.get("url"):
        lines.append(f"📸 Reel: עדיין אין scans (לא סוף שבוע)")

    # Top ad
    top_ads = m.get("top_ads") or []
    if top_ads:
        a = top_ads[0]
        cpl = a.get("spend",0)/a.get("lpv",1) if a.get("lpv") else 0
        ad_name = (a.get("ad_name","") or "")[:24]
        lines.append(f"🏆 ad: {ad_name} (${cpl:.2f} CPL)")

    # Language insight
    by_lang = m.get("by_lang", {})
    en = by_lang.get("english", {})
    es = by_lang.get("spanish", {})
    if (en.get("lpv",0) or 0) >= 50 and (es.get("lpv",0) or 0) >= 50:
        cpl_en = en.get("cpl", 0)
        cpl_es = es.get("cpl", 0)
        if cpl_en and cpl_es:
            if cpl_en < cpl_es * 0.7:
                pct = int((cpl_es - cpl_en) / cpl_es * 100)
                lines.append(f"🇺🇸 English זוול {pct}% מ-🇲🇽 Spanish — להזיז תקציב")
            elif cpl_es < cpl_en * 0.7:
                pct = int((cpl_en - cpl_es) / cpl_en * 100)
                lines.append(f"🇲🇽 Spanish זוול {pct}% מ-🇺🇸 English — להזיז תקציב")

    # Dashboard link (always last)
    lines.append(f"📊 events.themakeupblowout.com/events/{slug}/stats.html")

    return "\n".join(lines)


def is_future_event(slug: str) -> bool:
    """Return True if the event date is today or in the future."""
    if not NOTES.exists(): return True  # safe default
    try:
        notes = json.loads(NOTES.read_text())
    except Exception:
        return True
    city_prefix = slug.split("-")[0]
    year_suffix = slug.split("-")[-1]
    candidates = [k for k in notes if k.startswith(city_prefix + "-") and year_suffix in k]
    if not candidates: return True
    parts = candidates[0].split("-")
    if len(parts) < 4: return True
    try:
        event_date = datetime.date(int(parts[-3]), int(parts[-2]), int(parts[-1]))
        return event_date >= datetime.date.today()
    except Exception:
        return True


def is_active(slug: str, ev: dict) -> bool:
    m = ev.get("meta", {}); t = ev.get("tiktok", {})
    spend = float(m.get("spend",0) or 0) + float(t.get("spend",0) or 0)
    if spend < ACTIVE_SPEND_THRESHOLD: return False
    # 2026-05-14 PM — only future events. Past events still have 30d spend
    # in the window but it's not actionable.
    return is_future_event(slug)


def main() -> int:
    if not CONV_HISTORY.exists():
        print(f"[digest] {CONV_HISTORY} not found"); return 0
    data = json.loads(CONV_HISTORY.read_text())
    events = data.get("events", {})

    active = [(slug, ev) for slug, ev in events.items() if is_active(slug, ev)]
    if not active:
        print("[digest] no events with active campaigns — skipping"); return 0

    # Send header SMS first
    when = datetime.datetime.now().strftime("%H:%M")
    header = (
        f"🎯 6h marketing pulse · {when} PT\n\n"
        f"{len(active)} אירועים פעילים. SMS לכל אחד מיד."
    )
    if LAUREN_PHONE and os.environ.get("SIMPLETEXTING_TOKEN"):
        try:
            send_sms(LAUREN_PHONE, header)
            print(f"[digest] header sent")
        except Exception as e:
            print(f"[digest] header SMS failed: {e}")

    # Sort: most spend first
    active.sort(key=lambda kv: -(float(kv[1].get("meta",{}).get("spend",0) or 0) +
                                  float(kv[1].get("tiktok",{}).get("spend",0) or 0)))

    sent = 0
    for slug, ev in active:
        body = compose_event_sms(slug, ev)
        print(f"\n--- {slug} ---")
        print(body)
        print(f"--- ({len(body)} chars) ---")
        if not (LAUREN_PHONE and os.environ.get("SIMPLETEXTING_TOKEN")):
            print("(no SMS env — dry-run)")
            continue
        try:
            send_sms(LAUREN_PHONE, body)
            sent += 1
        except Exception as e:
            print(f"  ⚠ SMS failed for {slug}: {e}")

    print(f"\n[digest] sent {sent}/{len(active)} per-event SMS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
