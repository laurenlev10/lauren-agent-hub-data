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
ELI_PHONE = os.environ.get("ELI_PHONE", "").strip()

CONV_HISTORY = REPO / "docs" / "state" / "conversion_history.json"
NOTES        = REPO / "docs" / "launch" / "notes.json"

ACTIVE_SPEND_THRESHOLD = 5.0  # $/30d — below this we don't dignify with SMS


def compute_event_averages(events: dict) -> dict:
    """Across all active events, compute mean CPL, CPF, CTR, etc. Used as benchmark
    for individual events ('Cleveland CPL $0.18 = 30% below avg $0.26')."""
    eligible = []
    for slug, ev in events.items():
        m = ev.get("meta", {})
        spend = float(m.get("spend", 0) or 0)
        lpv = int(m.get("landing_page_views", 0) or 0)
        if spend >= 100 and lpv >= 200:
            eligible.append((slug, m))
    if not eligible:
        return {}
    cpls = [m["spend"]/m["landing_page_views"] for _,m in eligible if m.get("landing_page_views")]
    ctrs = [m.get("ctr", 0) for _,m in eligible]
    spends = [m.get("spend", 0) for _,m in eligible]
    return {
        "mean_cpl": sum(cpls)/len(cpls) if cpls else 0,
        "mean_ctr": sum(ctrs)/len(ctrs) if ctrs else 0,
        "mean_spend": sum(spends)/len(spends) if spends else 0,
        "best_cpl_event": min(eligible, key=lambda kv: kv[1]["spend"]/max(kv[1].get("landing_page_views",1),1))[0],
        "worst_cpl_event": max(eligible, key=lambda kv: kv[1]["spend"]/max(kv[1].get("landing_page_views",1),1))[0],
    }


def compose_insight_line(slug: str, ev: dict, avgs: dict) -> str:
    """Generate ONE actionable insight per event. Picks the most-impactful angle."""
    m = ev.get("meta", {})
    t = ev.get("tiktok", {})
    by_lang = m.get("by_lang", {})
    en = by_lang.get("english", {}); es = by_lang.get("spanish", {})

    # Insight 1: TT vs Meta CPF gap (form efficiency)
    meta_leads = int(m.get("leads", 0) or 0)
    tt_convs = int(t.get("conversions", 0) or 0)
    cpf_m = (m.get("spend",0)/meta_leads) if meta_leads else None
    cpf_t = (t.get("spend",0)/tt_convs) if tt_convs else None
    if cpf_m and cpf_t and meta_leads >= 5 and tt_convs >= 5:
        cheap = "Meta" if cpf_m < cpf_t else "TT"
        ratio = max(cpf_m, cpf_t) / min(cpf_m, cpf_t)
        if ratio >= 2:
            return f"💡 {cheap} זול פי {ratio:.1f}× — להזיז ${(max(m.get('spend',0),t.get('spend',0))*0.15):.0f}/יום"

    # Insight 2: Language gap (paid only)
    cpl_en = en.get("cpl", 0)
    cpl_es = es.get("cpl", 0)
    if en.get("lpv", 0) >= 100 and es.get("lpv", 0) >= 100 and cpl_en and cpl_es:
        if cpl_en < cpl_es * 0.7:
            pct = int((cpl_es - cpl_en) / cpl_es * 100)
            saved = es.get("spend", 0) * pct/100
            return f"💡 English זוול {pct}% — הזזה תחסוך ~${saved:.0f}/חודש"
        if cpl_es < cpl_en * 0.7:
            pct = int((cpl_en - cpl_es) / cpl_en * 100)
            saved = en.get("spend", 0) * pct/100
            return f"💡 Spanish זוול {pct}% — הזזה תחסוך ~${saved:.0f}/חודש"

    # Insight 3: vs cross-event mean CPL
    own_cpl = m.get("spend",0)/max(m.get("landing_page_views",1),1)
    mean_cpl = avgs.get("mean_cpl", 0)
    if mean_cpl and own_cpl:
        if own_cpl < mean_cpl * 0.75:
            return f"💡 CPL ${own_cpl:.2f} = {int((mean_cpl-own_cpl)/mean_cpl*100)}% מתחת לממוצע ${mean_cpl:.2f} — אירוע יעיל"
        if own_cpl > mean_cpl * 1.3:
            return f"💡 CPL ${own_cpl:.2f} = {int((own_cpl-mean_cpl)/mean_cpl*100)}% מעל ממוצע — לבדוק קריאייטיב"

    # Insight 4: positive — best event
    if avgs.get("best_cpl_event") == slug:
        return f"💡 האירוע היעיל ביותר השבוע (CPL ${own_cpl:.2f})"

    return ""


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


_averages_cache = {}


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

    # 2026-05-14 PM — lead with the most-actionable insight (Lauren's directive
    # "תתחזורי לתת לי את המסקנות / תובנות חדשות"). The averages arg is set by caller.
    insight = compose_insight_line(slug, ev, _averages_cache.get("avgs") or {})
    if insight:
        lines.append(insight)

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

    # Compute cross-event averages BEFORE filtering — uses all spend-active events
    # (so even past events contribute to "average CPL across recent weeks")
    _averages_cache["avgs"] = compute_event_averages(events)

    active = [(slug, ev) for slug, ev in events.items() if is_active(slug, ev)]
    if not active:
        print("[digest] no events with active campaigns — skipping"); return 0

    # Send header SMS first — lead with the cross-event headline
    when = datetime.datetime.now().strftime("%H:%M")
    avgs = _averages_cache["avgs"]
    best = avgs.get("best_cpl_event")
    worst = avgs.get("worst_cpl_event")
    mean_cpl = avgs.get("mean_cpl", 0)
    header_lines = [f"🎯 6h marketing pulse · {when} PT", ""]
    header_lines.append(f"{len(active)} אירועים פעילים · ממוצע CPL ${mean_cpl:.2f}")
    if best and best != worst:
        best_city = best.split("-")[0].title()
        worst_city = worst.split("-")[0].title()
        header_lines.append(f"🏆 הזול: {best_city}  ·  ⚠️ היקר: {worst_city}")
    header_lines.append("")
    header_lines.append("SMS לכל אירוע ⤵️")
    header = "\n".join(header_lines)
    recipients = []
    if LAUREN_PHONE: recipients.append(("Lauren", LAUREN_PHONE))
    if ELI_PHONE:    recipients.append(("Eli", ELI_PHONE))
    if recipients and os.environ.get("SIMPLETEXTING_TOKEN"):
        for name, phone in recipients:
            try:
                send_sms(phone, header)
                print(f"[digest] header sent to {name}")
            except Exception as e:
                print(f"[digest] header SMS to {name} failed: {e}")

    # Sort: most spend first
    active.sort(key=lambda kv: -(float(kv[1].get("meta",{}).get("spend",0) or 0) +
                                  float(kv[1].get("tiktok",{}).get("spend",0) or 0)))

    sent = 0
    for slug, ev in active:
        body = compose_event_sms(slug, ev)
        print(f"\n--- {slug} ---")
        print(body)
        print(f"--- ({len(body)} chars) ---")
        if not (recipients and os.environ.get("SIMPLETEXTING_TOKEN")):
            print("(no SMS env — dry-run)")
            continue
        for name, phone in recipients:
            try:
                send_sms(phone, body)
                sent += 1
            except Exception as e:
                print(f"  ⚠ SMS failed for {slug} → {name}: {e}")

    print(f"\n[digest] sent {sent}/{len(active)} per-event SMS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
