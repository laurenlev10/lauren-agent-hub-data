#!/usr/bin/env python3
"""venue_relationship_sync.py — regenerate the per-event RELATIONSHIP SUMMARY + key
points on the contract dashboard from the captured email_log (no Gmail call needed).

Lauren 2026-06-30: she wants the contract dashboard to always show an up-to-date
"relationship with the venue" summary for every upcoming event, refreshed from email.
`venue_email_digest.py` already fills events.<evkey>.email_log daily; THIS script turns
that raw log into:
    events.<evkey>.relationship_summary    Hebrew narrative (first contact -> now)
    events.<evkey>.relationship_points      [{date, who, text}]  newest first
    events.<evkey>.relationship_synced_at   ISO
    events.<evkey>.relationship_latest_email  latest email date seen (for "what's new")
    events.<evkey>.contact_name             only if it was empty

MERGE-on-write (IRON RULE #18): only the fields above are touched; deposits, totals,
contract_checks, milestones, team_notes, signed_contract are never modified.
Pure JSON — safe to run daily right after the email digest.
"""
from __future__ import annotations
import json, re, statistics, html, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "docs/state/venue_payments.json"

def clean(s): return re.sub(r"\s+", " ", html.unescape(s or "")).strip()

def whoof(frm, snip):
    f = (frm or "").lower()
    if "makeupblowout" in f or "eli@" in f or "info@" in f: return "אנחנו"
    if "docusign" in f or "sertifi" in f: return "מערכת חתימה"
    if f: return "האולם"
    s = snip or ""
    if re.match(r"^(eli|hi eli|hello eli|good (morning|afternoon|evening) eli|dear eli)", s, re.I): return "האולם"
    if re.search(r"thank you for (your|the|reaching|sending|returning)|i (will|have) (get|sent|attached|submitted)|attached is|our hotel|the hotel (will|can)", s, re.I): return "האולם"
    return ""

def parse_dt(x):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try: return dt.datetime.strptime((x or "")[:len("2000-01-01 00:00" if " " in fmt else "2000-01-01")], fmt)
        except Exception: pass
    return None

def main():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    ev = state.get("events", {})
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    for k in sorted(ev):
        r = ev[k]
        log = r.get("email_log") or []
        if not log:
            continue
        contact_email = r.get("contact_email") or ""
        cname = r.get("contact_name") or ""
        if not cname and contact_email and "@" in contact_email:
            lp = re.sub(r"\d+$", "", contact_email.split("@")[0])
            cname = " ".join(w.capitalize() for w in re.split(r"[._-]+", lp) if w)
        dts = [d for d in (parse_dt(m.get("date")) for m in log) if d]
        first, last, n = (min(dts) if dts else None), (max(dts) if dts else None), len(log)
        blob = " ".join((clean(m.get("subject", "")) + " " + clean(m.get("snippet", ""))) for m in log).lower()
        signed_existing = (r.get("milestones") or {}).get("contract_signed") or (r.get("signals") or {}).get("contract_signed")
        signed_detect = bool(re.search(r"docusign.*complet|completed:.*docusign|signed the agreement|fully executed|counter ?signed|fully signed|executed contract", blob))
        has_proposal = bool(re.search(r"proposal|contract|agreement|beo|banquet event order|sertifi", blob))
        has_deposit = bool(re.search(r"deposit|credit card authorization|authorization form|cc auth", blob))
        has_setup = bool(re.search(r"\btables?\b|diagram|floor ?plan|set ?up|pallet|loading dock|forklift|water station", blob))
        sd = sorted(dts)
        gaps = [(sd[i + 1] - sd[i]).days for i in range(len(sd) - 1)] if len(sd) > 1 else []
        med = statistics.median(gaps) if gaps else None
        fd = first.strftime("%Y-%m-%d") if first else "?"
        ld = last.strftime("%Y-%m-%d") if last else "?"
        parts = [f"קשר מול {cname or 'איש הקשר'}.", f"פנייה ראשונה ב-{fd}."]
        if signed_existing: parts.append("החוזה נחתם ✅.")
        elif signed_detect: parts.append("מהמיילים עולה שהחוזה כבר נחתם (כדאי לסמן 'חוזה חתום').")
        elif has_proposal: parts.append("התקבלה הצעה/חוזה — טרם נחתם.")
        else: parts.append("בשלב קשר ראשוני.")
        if has_deposit: parts.append("נדרש/שולם דיפוזיט.")
        if has_setup: parts.append("סוכמו פרטי הקמה (שולחנות/דיאגרמה/גישה לסחורה).")
        parts.append(f"התכתבות אחרונה ב-{ld} (סה”כ {n} מיילים).")
        if med is not None:
            parts.append("מגיבים מהר." if med <= 1.5 else ("קצב תגובה סביר." if med <= 4 else "לעיתים לוקח להם זמן לחזור."))
        summary = " ".join(parts)
        def score(m):
            t = (clean(m.get("subject", "")) + " " + clean(m.get("snippet", ""))).lower()
            return sum(kw in t for kw in ["sign", "contract", "agreement", "proposal", "deposit", "beo", "diagram", "table", "confirm", "available", "cancel", "price", "quote", "invoice", "authorization", "executed"])
        chosen = {0, n - 1}
        if n > 1: chosen.add(1)
        if n > 2: chosen.add(2)
        for i in sorted(range(n), key=lambda i: -score(log[i])):
            if len(chosen) >= 8: break
            chosen.add(i)
        pts = []
        for i in sorted(chosen):
            m = log[i]
            txt, sn = clean(m.get("subject", "")), clean(m.get("snippet", ""))
            body = (txt + " — " + sn) if (txt and txt.lower() not in sn.lower()) else (txt or sn)
            pts.append({"date": (m.get("date") or "")[:16], "who": whoof(m.get("from"), m.get("snippet")), "text": body[:150]})
        r["relationship_summary"] = summary
        r["relationship_points"] = pts
        r["relationship_synced_at"] = now
        r["relationship_latest_email"] = (log[0].get("date") or "")[:16]
        if not r.get("contact_name") and cname: r["contact_name"] = cname
        updated += 1
    state["_updated_at"] = now
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"venue_relationship_sync: regenerated {updated} event summaries")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
