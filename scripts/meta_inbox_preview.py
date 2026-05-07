"""
meta_inbox_preview — Phase 1 dry-run for the @meta agent migration.

Fetches Lauren's actual inbox via the Meta Graph API (Messenger DMs + FB/IG
comments), classifies each item against the KB, and generates a static HTML
preview at docs/meta/inbox-api-preview/index.html showing what the API-based
agent WOULD reply if it ran in production.

Lauren reviews the preview, compares classifications to her gut feel, and
either approves the cutover or flags items that look wrong.

This is a *read-only* run — no replies are sent, no comments are hidden.
"""

import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

# Add scripts/ to path so we can import lauren_meta
sys.path.insert(0, str(Path(__file__).parent))
from lauren_meta import fetch_recent_inbox, fetch_messenger_messages, get_token, get_fb_page_id

from docx import Document


# --- KB loader ---------------------------------------------------------------

def load_kb(docx_path: Path) -> dict:
    """Parse the inbox_knowledge_base.docx into a structured KB."""
    doc = Document(str(docx_path))
    kb = {"faqs": [], "schedule": {}, "negatives": []}

    # The docx has 4 tables. Order is fixed:
    # 0: FAQ (Question, Standard Reply)
    # 1: Event schedule (Dates, City)
    # 2: Negative/skeptical situations (Situation, How to respond)
    # 3: Optional fourth (links/etc)

    if doc.tables:
        # FAQ table
        for row in doc.tables[0].rows[1:]:  # skip header
            q = row.cells[0].text.strip()
            a = row.cells[1].text.strip()
            if q and a:
                kb["faqs"].append({"q": q, "a": a})

    if len(doc.tables) >= 2:
        for row in doc.tables[1].rows[1:]:
            dates = row.cells[0].text.strip()
            city = row.cells[1].text.strip()
            if dates and city:
                kb["schedule"][city.lower()] = dates

    if len(doc.tables) >= 3:
        for row in doc.tables[2].rows[1:]:
            sit = row.cells[0].text.strip()
            resp = row.cells[1].text.strip()
            if sit and resp:
                kb["negatives"].append({"situation": sit, "reply": resp})

    return kb


# --- Classifier --------------------------------------------------------------

# Keyword → which FAQ to match. Tuned for Lauren's actual common questions.
FAQ_KEYWORDS = [
    (re.compile(r"\b(free|admission|ticket|cost.*entry|enter)\b", re.I),
     "Is admission free?"),
    (re.compile(r"\b(what\s+(time|hour)|when.*(open|close|hour)|"
                r"(hours?|times?)\s+of\s+operation|"
                r"\d{1,2}\s*(am|pm)|10\s*-\s*5)\b", re.I),
     "What are the hours?"),
    (re.compile(r"\b(brand|carry|sell|product line)s?\b", re.I),
     "What brands do you have?"),
    (re.compile(r"\b(what.*mystery\s*box|mystery\s*box.*what|how.*mystery\s*box|mystery\s*box\s*\?|grab\s*bag)\b", re.I),
     "What is the Mystery Box?"),
    (re.compile(r"\b(parking|lot)\b", re.I),
     None),  # FAQ may have "Is parking free?"; if not matched will be Bucket B
    (re.compile(r"\b(payment|cash|card|credit|debit|tap)\b", re.I),
     None),
    (re.compile(r"\b(kids|child|stroller|baby)\b", re.I),
     None),
    (re.compile(r"\b(refund|return)s?\b", re.I),
     None),
]

NEGATIVE_KEYWORDS = re.compile(
    r"\b(scam|fake|fraud|rip[\s-]?off|too good to be true|bots?|spam)\b",
    re.I,
)

# Customer-issue patterns — past purchase / problem report. ALWAYS Bucket B.
CUSTOMER_ISSUE_PAT = re.compile(
    r"\b(i\s+(bought|purchased|got|paid|ordered)|"
    r"problem|issue|complaint|didn'?t\s+(receive|get|work)|"
    r"never\s+(received|got)|broken|damaged|wrong\s+item|"
    r"refund|return|missing|disappointed|unhappy)\b",
    re.I,
)

CITY_PAT = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


# Pattern that detects "when are you coming to X / X event / sale in X"
# style intent, regardless of capitalization.
CITY_QUESTION_PAT = re.compile(
    r"\b(when|are you|coming|visit|going|sale|event|come)\b.*"
    r"\b(?:to|in|at|near|around)\s+"
    # Capture up to ?, end-of-line, or a clearly terminal word. Allows multi-word
    # places like "Northern California bay area" or "San Bernardino county".
    r"([A-Za-z][A-Za-z0-9\s.\-,/]{1,60}?)"
    r"(?:\?|\.|!|$|\s+(?:please|thanks|tho|though|thx|ty)\b)",
    re.IGNORECASE,
)

# Also catch the bare "X???" pattern — just a place name, no verb (common on mobile).
# We only fire this fallback if no FAQ keyword matched AND the text is short.
BARE_PLACE_PAT = re.compile(r"^\s*([A-Za-z][\w\s.\-,/]{2,40}?)\s*\?+\s*$", re.IGNORECASE)


def _normalize_place(s: str) -> str:
    """Lower, strip punctuation, collapse spaces."""
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


# Common US-place prefixes that often get concatenated in mobile typing.
_PLACE_PREFIXES = ["san ", "los ", "las ", "el ", "new ", "north ", "south ",
                   "east ", "west ", "fort ", "saint ", "st "]

def _prettify_place(raw: str) -> str:
    """
    Turn user-typed place strings into a clean Title Case for the reply.
    Examples:
      'sanbernardino'           -> 'San Bernardino'
      'Northern California bay' -> 'Northern California Bay'
      'arizona'                 -> 'Arizona'
    """
    p = raw.strip().rstrip("?,.!").strip()
    pl = p.lower()
    if " " not in pl and pl == p.lower():
        for prefix in _PLACE_PREFIXES:
            compact = prefix.replace(" ", "")
            if pl.startswith(compact) and len(pl) >= len(compact) + 4 and not pl.startswith(prefix):
                rest = p[len(compact):]
                return (prefix + rest).title()
    return p.title()


# --- Date parsing for the schedule -------------------------------------------

_MONTHS = {"jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
           "apr":4,"april":4,"may":5,"jun":6,"june":6,"jul":7,"july":7,
           "aug":8,"august":8,"sep":9,"sept":9,"september":9,"oct":10,"october":10,
           "nov":11,"november":11,"dec":12,"december":12}

def _parse_dates(date_str: str, year: int = 2026):
    """
    Parse a schedule string like 'May 1–3', 'March 13–15', 'Jul 31–Aug 2'
    into (start_date, end_date, end_month_name).
    Returns (None, None, None) if parsing fails.
    """
    if not date_str:
        return None, None, None
    # Normalize dashes
    s = date_str.replace("–", "-").replace("—", "-").strip()
    # Form A: "May 1-3"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2})$", s)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            try:
                d1 = dt.date(year, mon, int(m.group(2)))
                d2 = dt.date(year, mon, int(m.group(3)))
                return d1, d2, m.group(1).title()
            except ValueError:
                pass
    # Form B: "Jul 31-Aug 2"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2})$", s)
    if m:
        mon1 = _MONTHS.get(m.group(1).lower())
        mon2 = _MONTHS.get(m.group(3).lower())
        if mon1 and mon2:
            try:
                d1 = dt.date(year, mon1, int(m.group(2)))
                d2 = dt.date(year, mon2, int(m.group(4)))
                return d1, d2, m.group(3).title()
            except ValueError:
                pass
    return None, None, None


def _is_past(date_str: str, today: dt.date = None) -> bool:
    """True if the event end date is strictly before today."""
    today = today or dt.date.today()
    _, end, _ = _parse_dates(date_str)
    if end is None:
        return False
    return end < today


# --- Reply template pools (seeded by conv_id for consistency) ----------------

import hashlib as _hashlib

_ON_SCHEDULE_TEMPLATES = [
    "YESSS babe!! 🎉 We're rolling into {C} {D} — Friday–Sunday, 10am–5pm! "
    "Free entry, free parking, and a whole lotta beauty waiting for you 💄✨ "
    "Can't wait to spoil you 💕",
    "Babe YESSS!! 🎉 {C} sale is locked in for {D} — Fri–Sun 10am–5pm. "
    "Free admission, free parking, just show up & shop till you drop 💄💥 "
    "See you there gorgeous 💕",
    "OMG YES!! 🎉 We're hitting {C} {D} — bring your bestie, your wallet, "
    "and your wishlist 👯‍♀️💄 Friday–Sunday 10am–5pm. Free entry + parking ✨",
    "Heck yes girl!! 🥳 {C} is on the books for {D} — Fri/Sat/Sun 10am–5pm. "
    "Free entry, free parking, 75% off the brands you love 💄💥 "
    "Mark that calendar 📌💕",
    "Queen, YESSS 👑✨ We're swinging into {C} {D} — Fri–Sun 10am–5pm. "
    "Show up, no ticket needed, just a love for makeup 💄 Treat yourself 💕",
    "Babe stop everything 🛑 — {C} sale is happening {D}!! Fri–Sun 10am–5pm. "
    "Free entry, free parking, and the biggest beauty deals you've ever seen ✨💄💥",
]

_OFF_SCHEDULE_TEMPLATES = [
    "Aww babe — {C} isn't on our 2026 stop list this year 💔 We rotate cities "
    "every 1–2 years to keep the magic alive ✨ But we're putting {C} on the "
    "radar HARD — gonna do our absolute BEST to make it happen next year 💄💕 "
    "Keep an eye on our Facebook page so you don't miss the announcement 👀✨",
    "Aww girl 💔 {C} didn't make our 2026 lineup this year — we visit each "
    "city every 1–2 years to keep things juicy 🤩 But trust me, you're on our "
    "radar! Gonna do our best to bring the sale to {C} in 2027 💄💕 Follow our "
    "Facebook for the announcement 👀✨",
    "Oof babe — no {C} on the 2026 schedule this year 😢 We rotate cities to "
    "keep the excitement up (and the deals deeper 💄✨) — but {C} is officially "
    "on our wishlist for next year 💕 Keep an eye on FB for updates 👀",
    "Babe nooooo 💔 We're not making it to {C} in 2026 — we rotate cities every "
    "1–2 years so we can give each one our ALL when we come ✨ But you've got "
    "us on it for 2027 💄💕 Follow us on FB so you don't miss when we drop dates 👀",
    "Aww {C} babe 💔 Not on our 2026 stop list this year — but we hear you loud "
    "and clear! Adding to our 2027 wishlist now 📝💄 We rotate cities every 1–2 "
    "years to keep things fresh ✨ Watch our Facebook for the announcement 👀💕",
]

_PAST_SCHEDULE_TEMPLATES = [
    "Aww babe, you JUST missed us!! 😭 We were in {C} {D} 💔 We won't be back "
    "this year (we rotate cities every 1–2 years) but you're locked into our "
    "2027 wishlist for sure 💄💕 Follow us on FB so you don't miss the next one ✨",
    "Oof babe — we were just there!! {C} happened {D} 😩 We rotate cities every "
    "1–2 years so we won't be back in 2026, but we'll do our BEST to come back in "
    "2027 💄💕 Stay close on Facebook for the announcement 👀✨",
    "Babe nooooo 💔 You missed us by THIS much — we wrapped up {C} on {D}! "
    "Can't sneak in another visit this year (we rotate cities), but you're "
    "officially on our 2027 list 💄✨ See you next time gorgeous 💕",
    "Awww girl, we were just in {C} {D}! 😭💔 The sale already wrapped — "
    "we rotate cities every 1–2 years, so {C} is on the radar for 2027. "
    "Follow our Facebook so you catch the next announcement 👀💄💕",
]


def _seeded_pick(pool: list, seed_str: str) -> str:
    """Pick a template deterministically from the conv/comment ID — same input
    gets the same template every run, but different inputs spread across the pool."""
    if not pool:
        return ""
    h = int(_hashlib.md5(seed_str.encode("utf-8")).hexdigest(), 16) if seed_str else 0
    return pool[h % len(pool)]


def _find_in_schedule(place: str, schedule: dict):
    """
    Try to find this place in the schedule with fuzzy matching:
    - Exact lowercase match
    - Substring match (e.g. 'columbia' matches 'columbia, mo')
    - Word-by-word: 'sanbernardino' matches 'san bernardino, ca'
    Returns (city_key, dates) tuple or None.
    """
    p = _normalize_place(place)
    if not p:
        return None
    sched_keys = list(schedule.keys())
    # Exact
    for k in sched_keys:
        if _normalize_place(k) == p:
            return (k, schedule[k])
    # Substring
    for k in sched_keys:
        nk = _normalize_place(k)
        if p in nk or nk.split(",")[0].strip() in p:
            return (k, schedule[k])
    # Concatenated (sanbernardino → san bernardino)
    p_compact = p.replace(" ", "")
    for k in sched_keys:
        if _normalize_place(k).replace(" ", "").startswith(p_compact[:8]):
            return (k, schedule[k])
    return None


def city_rotation_reply(place: str, *, seed: str = "") -> str:
    """
    Off-schedule reply, varied per conversation. Pulls from
    _OFF_SCHEDULE_TEMPLATES, picked deterministically from `seed` (conv_id).
    """
    p = _prettify_place(place) if place else "your area"
    template = _seeded_pick(_OFF_SCHEDULE_TEMPLATES, seed or p)
    return template.format(C=p)


def city_past_reply(city_title: str, dates: str, *, seed: str = "") -> str:
    """Past-event reply — 'we were just there {dates}'."""
    template = _seeded_pick(_PAST_SCHEDULE_TEMPLATES, seed or city_title)
    return template.format(C=city_title, D=dates)


def city_on_schedule_reply(city_title: str, dates: str, *, seed: str = "") -> str:
    """Future/upcoming on-schedule reply, varied per conversation."""
    template = _seeded_pick(_ON_SCHEDULE_TEMPLATES, seed or city_title)
    return template.format(C=city_title, D=dates)


def classify(text: str, kb: dict) -> dict:
    """
    Return {bucket: 'A'|'B'|'NEG', reason, suggested_reply} for a message.
    Bucket A = answerable from KB (auto-reply).
    Bucket B = needs Lauren (low confidence, complex, or no KB match).
    NEG     = never auto-reply (negative/skeptical comment).
    """
    if not text or len(text.strip()) < 2:
        return {"bucket": "B", "reason": "empty/too short", "reply": None}

    t = text.strip()

    # Negative — never auto-reply
    if NEGATIVE_KEYWORDS.search(t):
        return {"bucket": "NEG", "reason": "negative keywords detected", "reply": None}

    # Past-purchase or customer-issue — always Bucket B (Lauren handles personally)
    if CUSTOMER_ISSUE_PAT.search(t):
        return {"bucket": "B",
                "reason": "Customer issue / past purchase pattern — needs Lauren personally",
                "reply": None}

    # FAQ keyword match (priority over city question — "are you free?" outranks "are you coming")
    for pattern, faq_q in FAQ_KEYWORDS:
        if pattern.search(t):
            if faq_q:
                for f in kb["faqs"]:
                    if f["q"].lower().startswith(faq_q.lower()[:20]):
                        return {"bucket": "A", "reason": f"FAQ match: {f['q']}",
                                "reply": f["a"]}
            return {"bucket": "B", "reason": "keyword matched but no KB answer",
                    "reply": None}

    # City question intent — "when are you coming to X" / "sale in X"
    m_q = CITY_QUESTION_PAT.search(t)
    m_b = BARE_PLACE_PAT.match(t) if not m_q else None
    if m_q or m_b:
        place = (m_q.group(2) if m_q else m_b.group(1)).strip()
        hit = _find_in_schedule(place, kb["schedule"])
        seed = kb.get("_seed", "") + place  # conv-id + place for variation seed
        if hit:
            city_key, dates = hit
            city_title = city_key.split(",")[0].strip().title()
            if _is_past(dates):
                return {"bucket": "A",
                        "reason": f"City question — '{place}' was on schedule but already happened ({dates})",
                        "reply": city_past_reply(city_title, dates, seed=seed)}
            return {"bucket": "A",
                    "reason": f"City question — '{place}' matches upcoming schedule entry '{city_key}'",
                    "reply": city_on_schedule_reply(city_title, dates, seed=seed)}
        # Strong city-question intent but not on schedule → off-schedule reply
        if m_q:
            return {"bucket": "A",
                    "reason": f"City question — '{place}' NOT on 2026 schedule, off-schedule reply",
                    "reply": city_rotation_reply(place, seed=seed)}
        # Bare-place pattern (no verb) and not on schedule = ambiguous — Lauren reviews
        return {"bucket": "B",
                "reason": f"Bare-place pattern matched '{place}' but not on schedule — ambiguous, Lauren reviews",
                "reply": None}

    # Generic fallback
    return {"bucket": "B", "reason": "no KB pattern matched",
            "reply": None}


# --- Preview HTML builder ----------------------------------------------------

PAGE_HEAD = '''<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>@meta inbox — API preview (Phase 1 dry-run)</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#1a1a2a;color:#eee;padding:20px;line-height:1.5}
h1{color:#f5e45b;font-size:28px;margin-bottom:8px}
h2{color:#f01070;font-size:20px;margin:24px 0 12px}
.sub{color:#aaa;font-size:14px;margin-bottom:20px}
.warning{background:#3a2a0a;border:1px solid #d97706;color:#fbbf24;
         padding:12px 16px;border-radius:8px;margin-bottom:20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
       gap:12px;margin-bottom:20px}
.stat{background:#25253a;padding:12px;border-radius:8px;text-align:center}
.stat .num{font-size:28px;font-weight:700;color:#f5e45b}
.stat .lbl{font-size:12px;color:#aaa;text-transform:uppercase;margin-top:4px}
.item{background:#25253a;padding:14px;border-radius:8px;margin-bottom:12px;
      border-left:4px solid #444}
.item.bucket-A{border-left-color:#16a34a}
.item.bucket-B{border-left-color:#f01070}
.item.bucket-NEG{border-left-color:#dc2626}
.item .meta{color:#888;font-size:12px;margin-bottom:6px;direction:ltr;text-align:left}
.item .name{font-weight:700;color:#f5e45b;margin-bottom:6px}
.item .msg{background:#1a1a2a;padding:10px;border-radius:6px;
           margin-bottom:8px;white-space:pre-wrap}
.bucket-pill{display:inline-block;padding:2px 10px;border-radius:12px;
             font-size:11px;font-weight:700;letter-spacing:.5px}
.bucket-A .bucket-pill{background:#16a34a;color:#fff}
.bucket-B .bucket-pill{background:#f01070;color:#fff}
.bucket-NEG .bucket-pill{background:#dc2626;color:#fff}
.reason{color:#aaa;font-size:12px;margin-top:6px}
.reply{background:#0f3a1f;border:1px solid #16a34a;padding:10px;
       border-radius:6px;margin-top:8px;color:#a7f3d0}
.reply::before{content:"📤 Suggested reply: ";color:#86efac;font-weight:700;font-size:11px;display:block;margin-bottom:4px}
.no-reply{color:#fca5a5;font-size:12px;margin-top:6px;font-style:italic}
.errors{background:#3a1010;color:#fca5a5;padding:12px;border-radius:6px;margin:20px 0}
footer{margin-top:40px;color:#666;font-size:12px;text-align:center}

/* === Mobile responsive === */
@media (max-width: 600px) {
  body { padding: 12px; font-size: 15px; }
  h1 { font-size: 22px; }
  h2 { font-size: 17px; margin: 18px 0 8px; }
  .stats { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .stat .num { font-size: 22px; }
  .stat .lbl { font-size: 10px; }
  .attention-block { padding: 14px 14px; }
  .attention-block h2 { font-size: 18px; }
  .attention-row {
    flex-direction: column;
    align-items: stretch;
    gap: 6px;
    padding: 10px 12px;
  }
  .attention-row .who { font-size: 13px; }
  .attention-row .what {
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
    font-size: 12px;
    line-height: 1.4;
  }
  .attention-row .reply-btn { width: 100%; text-align: center; }
  .item { padding: 12px; font-size: 14px; }
  .item .meta { font-size: 11px; }
  .item .msg { font-size: 13px; padding: 8px; }
  .reply { padding: 8px; font-size: 13px; }
  details summary { padding: 12px; font-size: 14px; }
}

/* Reply button */
.reply-btn{display:inline-block;background:#f01070;color:#fff !important;
           padding:8px 14px;border-radius:6px;text-decoration:none;font-weight:700;
           font-size:13px;margin-top:6px;transition:transform .1s,filter .1s}
.reply-btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.reply-btn::before{content:"💬 "}

/* "Needs your attention" hero block */
.attention-block{background:linear-gradient(135deg,#7c2d12,#9a3412);
                 border:2px solid #f01070;border-radius:12px;
                 padding:18px 22px;margin:20px 0 30px}
.attention-block h2{color:#fbbf24;margin:0 0 4px;font-size:22px}
.attention-block p.intro{color:#fde68a;font-size:14px;margin:0 0 14px}
.attention-list{display:grid;gap:8px}
.attention-row{background:rgba(0,0,0,0.3);padding:10px 14px;border-radius:8px;
               display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.attention-row .who{font-weight:700;color:#f5e45b;flex:0 0 auto}
.attention-row .what{color:#eee;font-size:13px;flex:1 1 200px;min-width:0;
                     overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.attention-row .reply-btn{margin-top:0;flex:0 0 auto}

/* Collapsed Bucket A drafts section */
details.drafts-section {
  background: #1a2030;
  border-radius: 8px;
  margin: 24px 0;
  padding: 0;
  border: 1px solid #2a3040;
}
details.drafts-section summary {
  padding: 14px 18px;
  cursor: pointer;
  font-weight: 700;
  color: #aaa;
  font-size: 15px;
  user-select: none;
  list-style: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
details.drafts-section summary::-webkit-details-marker { display: none; }
details.drafts-section summary::after {
  content: " ▼";
  color: #555;
  font-size: 12px;
}
details.drafts-section[open] summary::after { content: " ▲"; }
details.drafts-section summary:hover { color: #fff; background: #1f2540; }
details.drafts-section .drafts-body { padding: 0 18px 18px }
</style>
</head>
<body>
'''

PAGE_FOOT = '''
<footer>
Generated by <code>meta_inbox_preview.py</code> · <a href="../" style="color:#888">← Back to @meta dashboard</a>
</footer>
</body>
</html>'''


def render_preview(snapshot: dict, classified_messenger: list,
                   classified_fb_comments: list,
                   classified_ig_comments: list) -> str:
    """Build the static HTML preview page."""
    n_a = sum(1 for c in classified_messenger if c["cls"]["bucket"] == "A")
    n_b = sum(1 for c in classified_messenger if c["cls"]["bucket"] == "B")
    n_neg = sum(1 for c in classified_messenger if c["cls"]["bucket"] == "NEG")
    n_fb = len(classified_fb_comments)
    n_ig = len(classified_ig_comments)

    # Collect every Bucket B item across all channels — the things needing Lauren
    attention_items = []
    for m in classified_messenger:
        if m["cls"]["bucket"] in ("B", "NEG"):
            attention_items.append({
                "channel": "Messenger",
                "who":     m.get("name", "?"),
                "what":    m.get("msg", "(no text)") or "(empty)",
                "url":     m.get("reply_url", ""),
                "bucket":  m["cls"]["bucket"],
            })
    for c in classified_fb_comments:
        if c["cls"]["bucket"] in ("B", "NEG"):
            attention_items.append({
                "channel": "FB comment",
                "who":     c.get("from", "?"),
                "what":    c.get("text", "(no text)"),
                "url":     c.get("reply_url", ""),
                "bucket":  c["cls"]["bucket"],
            })
    for c in classified_ig_comments:
        if c["cls"]["bucket"] in ("B", "NEG"):
            attention_items.append({
                "channel": "IG comment",
                "who":     "@" + c.get("username", "?"),
                "what":    c.get("text", "(no text)"),
                "url":     c.get("reply_url", ""),
                "bucket":  c["cls"]["bucket"],
            })

    parts = [PAGE_HEAD]
    parts.append(f"<h1>@meta — API Preview (Phase 1 dry-run)</h1>")
    parts.append(f'<div class="sub">'
                 f'Generated {snapshot["fetched_at"]} · '
                 f'Read-only — nothing was sent or hidden.</div>')

    # === Attention block: items needing Lauren — at the very top ===
    if attention_items:
        parts.append('<div class="attention-block">')
        parts.append(f'<h2>👀 {len(attention_items)} items need your attention</h2>')
        parts.append('<p class="intro">Click "💬 Reply" to open the thread on Meta and respond yourself.</p>')
        parts.append('<div class="attention-list">')
        for item in attention_items:
            url_attr = html.escape(item["url"]) if item["url"] else "#"
            label = "💬 Reply" if item["bucket"] == "B" else "💬 Open"
            parts.append('<div class="attention-row">')
            parts.append(f'<span class="who">[{item["channel"]}] {html.escape(item["who"])}</span>')
            parts.append(f'<span class="what">{html.escape(item["what"][:120] or "(empty)")}</span>')
            if item["url"]:
                parts.append(f'<a class="reply-btn" href="{url_attr}" target="_blank" rel="noopener">{label}</a>')
            parts.append('</div>')
        parts.append('</div></div>')

    # (verbose warning removed for mobile cleanup)

    # Stats
    parts.append('<div class="stats">')
    for label, num in [("Messenger Bucket A", n_a),
                       ("Messenger Bucket B", n_b),
                       ("Messenger Negative", n_neg),
                       ("FB Comments", n_fb),
                       ("IG Comments", n_ig)]:
        parts.append(f'<div class="stat"><div class="num">{num}</div>'
                     f'<div class="lbl">{label}</div></div>')
    parts.append('</div>')

    if snapshot.get("errors"):
        parts.append('<div class="errors"><strong>Non-fatal errors during fetch:</strong><ul>')
        for e in snapshot["errors"]:
            parts.append(f'<li>{html.escape(e[:200])}</li>')
        parts.append('</ul></div>')

    # Per-channel sections — Bucket A only, collapsed by default (Bucket B already in attention block)
    msg_a = [c for c in classified_messenger if c["cls"]["bucket"] == "A"]
    fb_a  = [c for c in classified_fb_comments if c["cls"]["bucket"] == "A"]
    ig_a  = [c for c in classified_ig_comments if c["cls"]["bucket"] == "A"]

    if msg_a:
        parts.append('<details class="drafts-section">')
        parts.append(f'<summary>📬 {len(msg_a)} Messenger auto-drafts ready (tap to inspect)</summary>')
        parts.append('<div class="drafts-body">')
    for c in msg_a:
        cls = c["cls"]
        bucket = cls["bucket"]
        parts.append(f'<div class="item bucket-{bucket}">')
        parts.append(f'<div class="meta">conv: {html.escape(c["conv_id"])} · '
                     f'updated: {html.escape(c.get("updated_time","")[:16])}</div>')
        parts.append(f'<div class="name">{html.escape(c.get("name","?"))}</div>')
        parts.append(f'<div class="msg">{html.escape(c.get("msg","(no message text)"))}</div>')
        parts.append(f'<span class="bucket-pill">Bucket {bucket}</span>')
        parts.append(f'<div class="reason">{html.escape(cls.get("reason",""))}</div>')
        if cls.get("reply"):
            parts.append(f'<div class="reply">{html.escape(cls["reply"])}</div>')
        else:
            parts.append('<div class="no-reply">→ Lauren reviews / replies manually</div>')
        if c.get("reply_url"):
            parts.append(f'<a class="reply-btn" href="{html.escape(c["reply_url"])}" target="_blank" rel="noopener">💬 Open in Messenger</a>')
        parts.append('</div>')
    if msg_a:
        parts.append('</div></details>')

    # FB comment auto-drafts (collapsed)
    if fb_a:
        parts.append('<details class="drafts-section">')
        parts.append(f'<summary>💬 {len(fb_a)} Facebook auto-drafts (tap to inspect)</summary>')
        parts.append('<div class="drafts-body">')
        for c in fb_a:
            cls = c["cls"]
            bucket = cls["bucket"]
            parts.append(f'<div class="item bucket-{bucket}">')
            parts.append(f'<div class="meta">on post: {html.escape(c.get("post_msg","")[:60])}</div>')
            parts.append(f'<div class="name">{html.escape(c.get("from","?"))}</div>')
            parts.append(f'<div class="msg">{html.escape(c.get("text","(no text)"))}</div>')
            parts.append(f'<span class="bucket-pill">Bucket {bucket}</span>')
            parts.append(f'<div class="reason">{html.escape(cls.get("reason",""))}</div>')
            if cls.get("reply"):
                parts.append(f'<div class="reply">{html.escape(cls["reply"])}</div>')
            else:
                parts.append('<div class="no-reply">→ Lauren reviews / replies manually</div>')
            if c.get("reply_url"):
                parts.append(f'<a class="reply-btn" href="{html.escape(c["reply_url"])}" target="_blank" rel="noopener">💬 Open on Facebook</a>')
            parts.append('</div>')
        parts.append('</div></details>')

    # IG comment auto-drafts (collapsed)
    if ig_a:
        parts.append('<details class="drafts-section">')
        parts.append(f'<summary>📷 {len(ig_a)} Instagram auto-drafts (tap to inspect)</summary>')
        parts.append('<div class="drafts-body">')
        for c in ig_a:
            cls = c["cls"]
            bucket = cls["bucket"]
            parts.append(f'<div class="item bucket-{bucket}">')
            parts.append(f'<div class="meta">on reel: {html.escape(c.get("media_caption","")[:60])}</div>')
            parts.append(f'<div class="name">@{html.escape(c.get("username","?"))}</div>')
            parts.append(f'<div class="msg">{html.escape(c.get("text","(no text)"))}</div>')
            parts.append(f'<span class="bucket-pill">Bucket {bucket}</span>')
            parts.append(f'<div class="reason">{html.escape(cls.get("reason",""))}</div>')
            if cls.get("reply"):
                parts.append(f'<div class="reply">{html.escape(cls["reply"])}</div>')
            else:
                parts.append('<div class="no-reply">→ Lauren reviews / replies manually</div>')
            if c.get("reply_url"):
                parts.append(f'<a class="reply-btn" href="{html.escape(c["reply_url"])}" target="_blank" rel="noopener">💬 Open on Instagram</a>')
            parts.append('</div>')
        parts.append('</div></details>')

    parts.append(PAGE_FOOT)
    return "\n".join(parts)


# --- Main --------------------------------------------------------------------

def _resolve_kb_path() -> Path:
    """Find the KB docx — checked in to the repo, with Cowork fallback."""
    candidates = [
        Path(__file__).resolve().parent / "data" / "meta_inbox_kb.docx",
        Path("/sessions/dreamy-compassionate-wozniak/mnt/Claude/Scheduled/NEW/meta-inbox/inbox_knowledge_base.docx"),
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"meta_inbox_kb.docx not found in any of: {candidates}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-sms", action="store_true",
                    help="Send Touchpoint 1 + Touchpoint 3 SMS to Lauren")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    kb_path = _resolve_kb_path()
    kb = load_kb(kb_path)
    print(f"KB loaded: {len(kb['faqs'])} FAQs, {len(kb['schedule'])} cities, "
          f"{len(kb['negatives'])} negative situations")

    print("Fetching inbox snapshot...")
    snap = fetch_recent_inbox(days=7, include_messenger=True, include_ig_dms=False,
                              include_fb_comments=True, include_ig_comments=True,
                              fb_post_limit=10, ig_media_limit=10)
    print(f"  messenger: {len(snap['messenger'])}, "
          f"fb_comments groups: {len(snap['fb_comments'])}, "
          f"ig_comments groups: {len(snap['ig_comments'])}")

    # Enrich messenger conversations with the latest message text
    classified_messenger = []
    for c in snap["messenger"]:
        conv_id = c["id"]
        # Get participants (filter out the Page itself)
        parts = c.get("participants", {}).get("data", [])
        customer = next((p for p in parts
                         if p.get("name") != "The Makeup Blowout Sale Group"), parts[0] if parts else {})
        # Fetch the most recent message text
        try:
            messages = fetch_messenger_messages(conv_id, limit=3)
            last_customer_msg = next((m for m in messages
                                      if m.get("from", {}).get("name") != "The Makeup Blowout Sale Group"),
                                     None)
            text = last_customer_msg.get("message", "") if last_customer_msg else ""
        except Exception as e:
            text = f"(error fetching: {e})"
        kb["_seed"] = conv_id  # per-conversation seed for reply variation
        cls = classify(text, kb)
        # Build a direct reply URL — Business Suite inbox is the most reliable
        # entry point. Lauren clicks → opens Messenger thread.
        reply_url = f"https://business.facebook.com/latest/inbox/all?asset_id={get_fb_page_id()}&thread_id={conv_id}"
        classified_messenger.append({
            "conv_id": conv_id,
            "name": customer.get("name", "?"),
            "msg": text,
            "updated_time": c.get("updated_time", ""),
            "reply_url": reply_url,
            "cls": cls,
        })

    # FB comments
    classified_fb = []
    for grp in snap["fb_comments"]:
        post_msg = grp["post"].get("message", "")[:80]
        post_permalink = grp["post"].get("permalink_url", "")
        for cmt in grp["comments"]:
            txt = cmt.get("message", "")
            kb["_seed"] = cmt.get("id", "")
            cls = classify(txt, kb)
            # FB comment: prefer post permalink (the comment will be visible there;
            # Meta doesn't always expose direct comment-permalinks for replies)
            classified_fb.append({
                "post_msg": post_msg,
                "from": cmt.get("from", {}).get("name", "?"),
                "text": txt,
                "reply_url": post_permalink or f"https://www.facebook.com/{cmt.get('id','')}",
                "cls": cls,
            })

    # IG comments
    classified_ig = []
    for grp in snap["ig_comments"]:
        media_caption = (grp["media"].get("caption") or "").split("\n")[0][:80]
        media_permalink = grp["media"].get("permalink", "")
        for cmt in grp["comments"]:
            txt = cmt.get("text", "")
            kb["_seed"] = cmt.get("id", "")
            cls = classify(txt, kb)
            # IG: tap the reel permalink — comment is visible inline on the reel
            classified_ig.append({
                "media_caption": media_caption,
                "username": cmt.get("username", "?"),
                "text": txt,
                "reply_url": media_permalink,
                "cls": cls,
            })

    print(f"Classified: messenger A/B/NEG = "
          f"{sum(1 for c in classified_messenger if c['cls']['bucket']=='A')}/"
          f"{sum(1 for c in classified_messenger if c['cls']['bucket']=='B')}/"
          f"{sum(1 for c in classified_messenger if c['cls']['bucket']=='NEG')}")

    # Render preview
    html_out = render_preview(snap, classified_messenger, classified_fb, classified_ig)
    out_dir = repo / "docs/meta/inbox-api-preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html_out, encoding="utf-8")

    # Also save the JSON (machine-readable)
    (out_dir / "data.json").write_text(json.dumps({
        "fetched_at": snap["fetched_at"],
        "messenger": classified_messenger,
        "fb_comments": classified_fb,
        "ig_comments": classified_ig,
        "errors": snap.get("errors", []),
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {out_dir/'index.html'} ({(out_dir/'index.html').stat().st_size} bytes)")
    print(f"Wrote {out_dir/'data.json'}")

    if args.send_sms:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from lauren_sms import send_sms
            need_lauren = (
                sum(1 for c in classified_messenger if c["cls"]["bucket"] in ("B", "NEG")) +
                sum(1 for c in classified_fb        if c["cls"]["bucket"] in ("B", "NEG")) +
                sum(1 for c in classified_ig        if c["cls"]["bucket"] in ("B", "NEG"))
            )
            has_neg = (
                any(c["cls"]["bucket"] == "NEG" for c in classified_messenger) or
                any(c["cls"]["bucket"] == "NEG" for c in classified_fb) or
                any(c["cls"]["bucket"] == "NEG" for c in classified_ig)
            )
            url = "https://laurenlev10.github.io/lauren-agent-hub-data/meta/inbox-api-preview/"
            if has_neg:
                t3 = f"@meta ⚠ יש תגובה שלילית! {need_lauren} פריטים דורשים אותך.\n{url}"
            elif need_lauren > 0:
                t3 = f"@meta ✓ {need_lauren} פריטים דורשים אותך — לחיצה אחת לכל אחד.\n{url}"
            else:
                t3 = f"@meta ✓ הכל שקט. אין מה לטפל.\n{url}"
            phone = os.environ.get("LAUREN_PHONE", "4243547625")
            r = send_sms(phone, t3)
            print(f"  SMS sent: id={r.get('id')}")
        except Exception as e:
            print(f"  ⚠ SMS failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
