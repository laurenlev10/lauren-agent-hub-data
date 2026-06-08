#!/usr/bin/env python3
"""qb_email_match.py — button-triggered Gmail event-matching for the bookkeeping
workstation (Lauren 2026-06-07).

Reads docs/state/qb_email_check_queue.json pending entries, searches Eli's Gmail
(API, read-only) for the booking confirmation by EXACT AMOUNT, extracts the
TRAVEL date (not the charge date — per references/tag_rules.md), assigns the
nearest event from docs/state/events_index.json, and writes result_cls /
result_account back onto the queue entry. The dashboard polls the queue and
fills the rows itself (single-writer sessions).

Gmail auth: env GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN
(GitHub Secrets), or local .claude/secrets/gmail_{client_id,client_secret,refresh_token}.txt
Scope needed: gmail.readonly. NO writes to email, NO writes to QB here.
"""
from __future__ import annotations
import base64, datetime as dt, json, os, re, sys, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "docs/state/qb_email_check_queue.json"
EVENTS = ROOT / "docs/state/events_index.json"

TRAVEL_SENDERS = ("southwest", "aa.com", "united", "delta", "flyfrontier", "spirit",
                  "alaskaair", "hotels.com", "marriott", "hilton", "hyatt", "ihg",
                  "choicehotels", "wyndham", "expedia", "booking.com", "priceline",
                  "alamo", "hertz", "enterprise", "avis", "budget")
# Lauren 2026-06-08: venue payments (hotel = event hall) are matched too -
# by venue/city name in the email, not by travel date (deposits precede events by months)
VENUE_HINTS = ("banquet", "event order", "beo", "catering", "ballroom", "event space",
               "rental", "deposit", "agreement", "contract", "group sales", "sales & catering",
               "function space", "invoice", "docusign", "exhibit", "vendor")
VENUE_STOPWORDS = ("hotel", "hotels", "center", "centre", "suites", "event", "events",
                   "conference", "community", "inn")

def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
for m, i in list(MONTHS.items()):
    MONTHS[m[:3]] = i


def _sec(name):
    e = os.environ.get(name.upper(), "").strip()
    if e: return e
    for c in Path("/sessions").glob(f"*/mnt/Claude/.claude/secrets/{name.lower()}.txt"):
        return c.read_text().strip()
    p = Path.home() / f".claude/secrets/{name.lower()}.txt"
    return p.read_text().strip() if p.exists() else ""


def gmail_token():
    body = urllib.parse.urlencode({
        "client_id": _sec("gmail_client_id"), "client_secret": _sec("gmail_client_secret"),
        "refresh_token": _sec("gmail_refresh_token"), "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def _api(tok, path, params=None):
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    if params: url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def search(tok, q, n=8):
    try:
        return _api(tok, "messages", {"q": q, "maxResults": n}).get("messages", []) or []
    except Exception:
        return []


def get_msg(tok, mid):
    return _api(tok, f"messages/{mid}", {"format": "full"})


def headers_of(msg):
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}


def body_text(msg):
    out = []
    def walk(part):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime.startswith("text/"):
            try:
                txt = base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore")
                if mime == "text/html":
                    txt = re.sub(r"<[^>]+>", " ", txt)
                out.append(txt)
            except Exception:
                pass
        for p in part.get("parts") or []:
            walk(p)
    walk(msg.get("payload") or {})
    return re.sub(r"\s+", " ", " ".join(out))[:30000]


def dates_in(text, charge):
    """All plausible dates near the charge date (-10d .. +60d travel window)."""
    found = set()
    yr = charge.year
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?,?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\b", text):
        mon = MONTHS.get(m.group(1).lower())
        if not mon: continue
        try: found.add(dt.date(int(m.group(3) or yr), mon, int(m.group(2))))
        except ValueError: pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text):
        y = m.group(3)
        y = int(y) + (2000 if y and len(y) == 2 else 0) if y else yr
        try: found.add(dt.date(y, int(m.group(1)), int(m.group(2))))
        except ValueError: pass
    return sorted(d for d in found if -10 <= (d - charge).days <= 60)


def load_events():
    return json.loads(EVENTS.read_text(encoding="utf-8"))["events"]


def nearest_event(events, d):
    best, bd = None, 99999
    for ev in events:
        s, e = dt.date.fromisoformat(ev["start_date"]), dt.date.fromisoformat(ev["end_date"])
        dist = 0 if s <= d <= e else min(abs((d - s).days), abs((d - e).days))
        if dist < bd:
            best, bd = ev["class_name"], dist
    return best, bd


def venue_config():
    try:
        t = json.loads((ROOT / "docs/state/qb_expense_types.json").read_text(encoding="utf-8"))
        vr = t.get("venue_rent") or {}
        return float(vr.get("min_amount") or 1500), vr.get("account") or "Rent Expense:Trade Show Rent"
    except Exception:
        return 1500.0, "Rent Expense:Trade Show Rent"


def match_venue_event(events, subj, sender, text, charge):
    # does this email talk about one of OUR event venues? (hall-rent path)
    hay = (subj + " " + sender + " " + text).lower()
    hayn = _norm(hay)
    best, bs = None, 0
    for ev in events:
        if not ev.get("venue"):
            continue
        vwords = [w for w in re.split(r"[^a-z0-9]+", ev["venue"].lower())
                  if len(w) >= 4 and w not in VENUE_STOPWORDS]
        hits = sum(1 for w in vwords if w in hay)
        score = 0
        if vwords and hits >= min(2, len(vwords)):
            score += 2
        cityn = _norm(ev.get("city"))
        if cityn and cityn in hayn:
            score += 2
        try:
            sd = dt.date.fromisoformat(ev["start_date"])
            if -30 <= (sd - charge).days <= 400:
                score += 1   # deposits are paid in advance of the event
        except Exception:
            pass
        if score > bs:
            bs, best = score, ev
    return (best, bs) if bs >= 3 else (None, 0)


def classify_account(subj_from):
    t = subj_from.lower()
    # order matters: rental → airline (sender domains are decisive) → hotel.
    # "reservation" removed — too generic (airline cancellations say it too).
    if "car rental" in t or any(k in t for k in ("alamo", "hertz", "enterprise", "avis", "budget")):
        return "Travel Expense:Rental Car"
    if any(k in t for k in ("southwest", "aa.com", "american airlines", "united.com", "delta", "frontier", "spirit", "alaskaair", "trip confirmation", "going to", "flight")):
        return "Travel Expense:Airfare"
    if any(k in t for k in ("hotel", "marriott", "hilton", "hyatt", "doubletree", "booking.com", "expedia", "check-in")):
        return "Travel Expense:Accommodations"
    return None


def process(tok, events, e):
    amt = float(e["amount"])
    charge = dt.date.fromisoformat(e["date"])
    after = (charge - dt.timedelta(days=60)).strftime("%Y/%m/%d")
    before = (charge + dt.timedelta(days=7)).strftime("%Y/%m/%d")
    variants = [f'"{amt:,.2f}"'] if amt >= 1000 else [f'"{amt:.2f}"']
    if amt >= 1000: variants.append(f'"{amt:.2f}"')
    memo_digits = re.sub(r"\D", "", e.get("desc") or "")[-8:]
    cands = []
    for v in variants:
        for m in search(tok, f"{v} after:{after} before:{before}"):
            if not any(m["id"] == c["id"] for c in cands):
                cands.append(m)
    if not cands:
        return {"status": "no_receipt", "result_note": "לא נמצא אישור באימייל לפי הסכום"}
    scored = []
    for c in cands[:8]:
        msg = get_msg(tok, c["id"])
        h = headers_of(msg)
        sender, subj = h.get("from", ""), h.get("subject", "")
        score = 2 if any(k in sender.lower() for k in TRAVEL_SENDERS) else 0
        text = subj + " " + body_text(msg)
        if memo_digits and len(memo_digits) >= 5 and memo_digits in re.sub(r"\D", "", text):
            score += 5  # itinerary number matches the bank memo — definitive
        scored.append((score, subj, sender, text))
    scored.sort(key=lambda x: -x[0])
    score, subj, sender, text = scored[0]
    # venue path FIRST - hall payments have no travel date and must not become Accommodations
    ven_ev, vscore = match_venue_event(events, subj, sender, text, charge)
    venue_hint = any(k in (subj + " " + text).lower() for k in VENUE_HINTS)
    if ven_ev and (venue_hint or vscore >= 4):
        v_min, v_account = venue_config()
        note = subj[:60] + " · 🏛 אולם — " + (ven_ev.get("venue") or "")[:40]
        res = {"status": "done", "result_cls": ven_ev["class_name"], "result_note": note}
        if amt >= v_min:
            res["result_account"] = v_account
        else:
            res["result_note"] += " · סכום קטן — כנראה לינות צוות"
        return res
    ds = dates_in(subj, charge) or dates_in(text, charge)
    if not ds:
        return {"status": "no_receipt", "result_note": f"נמצא אימייל ({subj[:60]}) אבל בלי תאריך נסיעה ברור"}
    travel = ds[0]
    cls, dist = nearest_event(events, travel)
    res = {"status": "done", "result_cls": cls,
           "result_note": f"{subj[:70]} · נסיעה {travel.strftime('%m/%d')} · מרחק {dist} ימים מהאירוע"}
    acc = classify_account(subj + " " + sender)
    if acc: res["result_account"] = acc
    if score < 2:
        res["result_note"] += " · ⚠ ודאות נמוכה — בדקי"
    return res


def main():
    q = json.loads(QUEUE.read_text(encoding="utf-8"))
    pending = [e for e in q.get("queue", []) if e.get("status") == "pending"]
    if not pending:
        print("queue empty"); return 0
    tok = gmail_token()
    events = load_events()
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for e in pending:
        try:
            res = process(tok, events, e)
        except Exception as ex:
            res = {"status": "error", "result_note": str(ex)[:150]}
        e.update(res); e["checked_at"] = now
        print(f"  {e.get('status')} · ${e['amount']} {e.get('vendor','')[:25]} → {e.get('result_cls','—')} [{e.get('result_note','')[:80]}]")
    q["_updated_at"] = now
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"processed {len(pending)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
