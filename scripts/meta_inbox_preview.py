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

def load_venues() -> list:
    """Parse scripts/data/venue_details.md into list of {dates, city, venue, address} dicts."""
    candidates = [
        Path(__file__).resolve().parent / "data" / "venue_details.md",
        Path("/sessions/dreamy-compassionate-wozniak/mnt/Claude/Scheduled/NEW/meta-inbox/venue_details.md"),
    ]
    for c in candidates:
        if not c.exists():
            continue
        out = []
        for line in c.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("|") or "---" in line or line.startswith("| Event") or line.startswith("| dates"):
                continue
            cells = [x.strip() for x in line.split("|")[1:-1]]
            if len(cells) == 4 and cells[0]:
                out.append({"dates": cells[0], "city": cells[1], "venue": cells[2], "address": cells[3]})
        return out
    return []


def find_event_today(venues, today=None):
    today = today or dt.date.today()
    for v in venues:
        start, end, _ = _parse_dates(v["dates"])
        if start and end and start <= today <= end:
            return v
    return None


def find_next_event(venues, today=None):
    today = today or dt.date.today()
    upcoming = [(_parse_dates(v["dates"])[0], v) for v in venues if _parse_dates(v["dates"])[0] and _parse_dates(v["dates"])[0] > today]
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    return upcoming[0][1]


def load_handled() -> dict:
    """
    Load docs/meta/handled.json — the per-item dedup memory.
    Schema: { "<channel>:<id>": {"handled": True, "handledAt": "ISO"} }
    Updated by the agent (after auto-reply) AND by Lauren's clicks in the preview UI
    (which calls GitHub Contents API to PUT the file).
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "docs/meta/handled.json",
    ]
    for c in candidates:
        if c.exists():
            try:
                return json.loads(c.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def dedup_key(channel: str, item_id: str) -> str:
    """Stable key for an inbox item across runs."""
    return f"{channel}:{item_id}"


def is_handled(handled: dict, channel: str, item_id: str) -> bool:
    """True if this exact item has been marked handled in a prior run."""
    if not handled or not item_id:
        return False
    entry = handled.get(dedup_key(channel, item_id), {})
    return bool(entry.get("handled"))


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

# "Can I order online?" / "Available online?" — direct her to the website
# "Where?" comments on event-specific reels — answer with the venue from the post caption.
WHERE_PAT = re.compile(
    r"^\s*(?:at\s+)?where\??!?\s*\??!?\s*$|"        # "Where?" / "At where??"
    r"^\s*where\s+(?:is|are|r|u|you|it|this|the\s+sale)\b",  # "where is it"
    re.IGNORECASE,
)

# Patterns that pull event info out of Lauren's reel captions
_CITY_STATE_FROM_CAP = re.compile(
    r"sale\s+in\s+([^,!?\n]+?)\s*,\s*([A-Z]{2})", re.IGNORECASE
)
_WHEN_FROM_CAP = re.compile(r"when[:\s]+([^\n]+?)\s*\n", re.IGNORECASE)
_WHERE_FROM_CAP = re.compile(r"where[:\s]+([^\n]+?)\s*\n", re.IGNORECASE)
_AT_VENUE_FROM_CAP = re.compile(r"\bAt\s+([A-Z][^.\n]+?)(?:\.|\n|$)")


def summarize_event_from_caption(caption: str):
    """Pull (city, state, dates, address, venue) out of one of Lauren's
    standard event reel captions. Returns None if we can't identify a city."""
    if not caption:
        return None
    m = _CITY_STATE_FROM_CAP.search(caption)
    if not m:
        return None
    city, state = m.group(1).strip(), m.group(2).strip().upper()
    dates_m   = _WHEN_FROM_CAP.search(caption)
    address_m = _WHERE_FROM_CAP.search(caption)
    venue_m   = _AT_VENUE_FROM_CAP.search(caption)
    return {
        "city":    city,
        "state":   state,
        "dates":   dates_m.group(1).strip() if dates_m else None,
        "address": address_m.group(1).strip() if address_m else None,
        "venue":   venue_m.group(1).strip() if venue_m else None,
    }


def event_status(dates_str: str, today: dt.date = None) -> str:
    """Returns 'live' / 'upcoming-soon' / 'upcoming' / 'past' / 'unknown'."""
    today = today or dt.date.today()
    start, end, _ = _parse_dates(dates_str or "")
    if not start or not end:
        return "unknown"
    if today < start:
        days = (start - today).days
        return "upcoming-soon" if days <= 7 else "upcoming"
    if today > end:
        return "past"
    return "live"  # today between start and end inclusive


_WHERE_LIVE_TEMPLATES = [
    "We're LIVE NOW in {city}, {state}!! 💄✨ 📍 {address} — open today "
    "10am-5pm. Come thru gorgeous 💕",
    "Happening RIGHT NOW 💄 {city}, {state} — 📍 {address}, open till 5pm. "
    "Don't sleep on this 🛍️✨",
    "Babe we're LIVE 🎉 {city}, {state} — 📍 {address} (at {venue}). "
    "10am-5pm today, free entry + parking 💄💕",
    "Today's the day!! 🥳 {city}, {state} — 📍 {address}. Open 10am-5pm 💄✨",
]
_WHERE_UPCOMING_SOON_TEMPLATES = [
    "Coming up THIS weekend 🎉 {city}, {state} — 📍 {address}, Fri-Sun "
    "10am-5pm 💄✨ Free entry + parking 💕",
    "We're heading to {city}, {state} — {dates}!! 📍 {address}. "
    "See you there gorgeous 💄✨",
    "Locked in for {city}, {state} — {dates} 💄 📍 {address}. "
    "Fri-Sun 10am-5pm, free admission 💕",
]
_WHERE_UPCOMING_TEMPLATES = [
    "{city}, {state} — {dates} 💄 📍 {address}. Fri-Sun 10am-5pm. "
    "Mark your calendar 📌💕",
    "We're popping up in {city}, {state} for {dates} 🎉 📍 {address}. "
    "Free entry, free parking 💄✨",
    "Heading to {city}, {state} — {dates}! 📍 {address} (at {venue}). "
    "Fri-Sun 10am-5pm 💄✨",
]
OPEN_TODAY_PAT = re.compile(
    r"\b(?:open\s+today|here\s+today|live\s+today|are\s+you\s+open|"
    r"are\s+you\s+here|you\s+open\s+today|today\s+open)\b",
    re.IGNORECASE,
)
PAYMENT_PAT = re.compile(
    r"\b(?:cash\s+only|take\s+(?:credit|debit|cards?|cash|apple\s*pay|tap)|"
    r"accept\s+(?:credit|debit|cards?|cash|apple\s*pay|tap)|"
    r"do\s+you\s+(?:take|accept)\s+(?:credit|debit|cards?|cash|venmo|zelle)|"
    r"payment\s+method|tap\s*to\s*pay|apple\s*pay)\b",
    re.IGNORECASE,
)
PARKING_PAT = re.compile(r"\b(?:parking|where\s+(?:do|can)\s+i\s+park|valet)\b", re.IGNORECASE)
HOURS_PAT = re.compile(
    r"\b(?:what\s+time|what\s+(?:are\s+the\s+)?hours?|when\s+(?:do\s+you\s+)?(?:open|close)|"
    r"hours?\s+of\s+operation|opening\s+hours?)\b",
    re.IGNORECASE,
)
KIDS_PAT = re.compile(
    r"\b(?:bring\s+(?:my\s+)?(?:kids?|children|baby|toddler|stroller)|"
    r"kid[s]?\s+(?:ok|allowed|welcome|friendly)|kid[\s-]?friendly|"
    r"family[\s-]?friendly|stroller)\b",
    re.IGNORECASE,
)
ADDRESS_PAT = re.compile(
    r"\b(?:what'?s?\s+the\s+(?:address|location|venue)|address(?:\s|\?|$)|"
    r"location(?:\s|\?|$)|venue\s*\?|exact\s+(?:address|location)|"
    r"where\s+exactly|directions\??)\b",
    re.IGNORECASE,
)


_OPEN_TODAY_LIVE_TEMPLATES = [
    "YES we're LIVE NOW!! 💄✨ Open till 5pm at {address} ({city}, {state}) — come thru gorgeous 💕",
    "OPEN!! 🎉 We're at {address} ({city}, {state}) until 5pm tonight — bring your bestie 💄✨",
    "Yes ma'am 💄 LIVE today at {address}, open till 5pm. See you soon 💕",
    "🔥 Live + open!! {city}, {state} — 📍 {address}, until 5pm 💄✨",
]
_OPEN_TODAY_CLOSED_TEMPLATES = [
    "Not today girl 💔 But coming up: {city}, {state} — {dates}!! 📍 {address}. See you there 💄✨",
    "Aww not open today 😢 Next stop: {city}, {state} ({dates}) — 📍 {address}. Mark your calendar 📌💄",
    "Closed today, but {city}, {state} is up next ({dates}) — 📍 {address} 💄✨",
]
_PAYMENT_TEMPLATES = [
    "We take EVERYTHING — cash, cards, debit, tap-to-pay, Apple Pay 💳✨ However works for you 💄",
    "All payment methods welcome! 💳 Cash, cards, tap-to-pay, Apple Pay — you name it 💄✨",
    "Cash, cards, tap-to-pay — we accept it all 💳💄 Whatever's easiest 💕",
    "Yes! 💳 Cash, credit, debit, tap-to-pay, Apple Pay — all good 💄✨",
]
_PARKING_TEMPLATES = [
    "Yes!! 🚗 Free parking AND free entry — bring the whole gang 💄✨",
    "Parking is FREE 🅿️ And so is admission — just show up & shop 💄💕",
    "Free parking, free entry, free vibes 💄✨ See you there 💕",
    "🅿️ Yep, parking is on the house — and admission too 💄✨",
]
_HOURS_TEMPLATES = [
    "Friday, Saturday & Sunday — 10am to 5pm ⏰✨ See you there 💄",
    "We're open Fri-Sun 10am-5pm 💄 Plenty of time to come browse 💕",
    "10am-5pm Friday through Sunday ⏰💄 Free entry, no ticket needed ✨",
    "Fri/Sat/Sun, 10am-5pm 💄✨ Drop by anytime in those hours 💕",
]
_KIDS_TEMPLATES = [
    "Yes!! 👶 Kids are welcome — bring the whole crew 💄💕",
    "Of course gorgeous 💄 The whole family is welcome, kids included 👶✨",
    "All ages welcome 💕 Bring the kids, the bestie, your mama 💄✨",
    "Kid-friendly all the way 👶💄 Strollers, the works — come on by ✨",
]


_WHERE_PAST_TEMPLATES = [
    "Aww we already wrapped {city} {dates} 😭💔 — but watch our FB for "
    "the next stop near you! 💄✨",
    "{city} just wrapped {dates} 😭 — we rotate cities every 1–2 years, "
    "but stay close on FB so you catch the next one 💄💕",
]


ONLINE_ORDER_PAT = re.compile(
    r"\b(?:order|buy|purchase|shop|sell|sold|ship|delivery|delivered|"
    r"available|get\s+it)\b.*\bonline\b|"
    r"\bonline\b.*\b(?:order|buy|purchase|shop|sell|sold|ship|delivery|"
    r"delivered|available)\b",
    re.I,
)

_ONLINE_ORDER_TEMPLATES = [
    "YES girl!! 💄 You can grab our Mystery Box online anytime: "
    "https://www.themakeupblowout.com/ ✨💕",
    "Heck yes 🛍️ Online shop right here: https://www.themakeupblowout.com/ — "
    "same fabulous deals delivered to your door 📦✨",
    "OMG yes queen! 💄 Mystery Box online: https://www.themakeupblowout.com/ "
    "✨ Bring the magic home 💕",
    "Lovely yes!! 💕 Online store: https://www.themakeupblowout.com/ — "
    "delivered to your door 📦💄✨",
    "Hey gorgeous 💄 Online shop's right here: https://www.themakeupblowout.com/ "
    "✨ Same magic, delivered 💕",
    "Babe YESSS!! 🛍️ https://www.themakeupblowout.com/ — same fabulous deals "
    "delivered to your door 📦💄💕",
]

CITY_PAT = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


# Pattern that detects "when are you coming to X / X event / sale in X"
# style intent, regardless of capitalization.
CITY_QUESTION_PAT = re.compile(
    r"\b(when|are you|coming|visit|going|sale|event|come)\b.*"
    r"\b(?:to|in|at|near|around)\s+"
    # Capture up to ?, sentence boundary, or a clearly terminal word.
    # Allows multi-word places like "Northern California bay area" or
    # "San Bernardino county". Terminates at a new clause boundary
    # (e.g. " I need" / " love your" / " thanks") so we don't swallow
    # the rest of the message.
    r"([A-Za-z][A-Za-z0-9\s.\-,/]{1,80}?)"
    r"(?:\?|\.|!|$|"
    r"\s+I[\s\u2019']|"     # "Palmdale / Lancaster I need..." / "I'm" / "I've"
    r"\s+(?:love|need|want|hope|wish|please|thanks?|tho|though|thx|ty)\b)",
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
    "YESSS girl!! 🎉 We're rolling into {C} {D} — Friday–Sunday, 10am–5pm. "
    "Free entry, free parking, and a whole lotta beauty waiting for you 💄✨",
    "OMG YES queen 👑 We're hitting {C} {D}! Bring your bestie, your wallet, "
    "and your wishlist — Fri–Sun 10am–5pm, free entry + parking 💄💥",
    "Heck yes mama!! 🥳 {C} is locked in for {D} — Fri/Sat/Sun 10am–5pm. "
    "Free entry, free parking, 75% off your fave brands 💄💥 Mark that calendar 📌",
    "Stop everything 🛑 {C} sale is happening {D}! Fri–Sun 10am–5pm. "
    "Free entry, free parking, the biggest beauty deals you've ever seen 💄✨",
    "Hey gorgeous 💄 {C} sale is on for {D}! Fri–Sun 10am–5pm, free entry. "
    "Show up, no ticket needed, just a love for makeup ✨💕",
    "Lovely yesss!! 💕 We're swinging into {C} {D} — Fri–Sun 10am–5pm. "
    "Free admission, free parking, treat yourself 💄✨",
    "Honey YES!! 🥳 {C} {D} — Fri–Sun 10am–5pm. Free entry + parking, "
    "deals deeper than the ocean 💄💥",
    "Aww babe!! 🎉 Locked in — {C} {D}, Fri–Sun 10am–5pm. Free entry, "
    "free parking. Can't wait to spoil you 💕",
    "Sis YESSS 💄 We're popping up in {C} {D}! Fri–Sun 10am–5pm. "
    "Free admission, no ticket needed — just bring your love for beauty ✨",
    "Doll, locked in!! 👑 {C} sale {D} — Fri–Sun 10am–5pm. Free entry, "
    "free parking, brace yourself for the biggest deals 💄💕",
]

_OFF_SCHEDULE_TEMPLATES = [
    "Aww girl 💔 {C} didn't make our 2026 lineup this year — we visit each city "
    "every 1–2 years to keep things juicy 🤩 Adding {C} to our 2027 wishlist "
    "now 📝💄💕 Follow our Facebook for the announcement 👀",
    "Oof — no {C} on the 2026 schedule this year 😢 We rotate cities to keep "
    "the excitement up (and the deals deeper 💄✨) — but {C} is officially "
    "on our wishlist for 2027 💕 Keep an eye on FB for updates 👀",
    "Sis, {C} isn't on our 2026 stop list this year 💔 We rotate cities every "
    "1–2 years to keep the magic alive ✨ Putting {C} on the radar for 2027 💄💕",
    "Hey love 💕 {C} isn't on the 2026 lineup — but stay close, we'll do our "
    "BEST to come for 2027 💄✨ Watch our Facebook for the announcement 👀",
    "Aww mama 💔 No {C} in 2026 — we rotate cities every 1–2 years to give "
    "each one our ALL when we come ✨ Definitely on our radar for 2027 💄💕",
    "Lovely girl 💕 {C}'s not on our 2026 lineup this year — but trust me, "
    "you're heard. We'll do our best to bring the sale to {C} in 2027 💄✨ "
    "Follow our Facebook for the drop 👀",
    "Gorgeous, {C} didn't make 2026 this year 💔 We rotate cities to keep the "
    "magic alive ✨ But we're putting {C} on the radar HARD for 2027 💄💕 "
    "Keep an eye on FB so you don't miss the announcement 👀",
    "Aww babe — {C} isn't on our 2026 stop list 💔 We visit each city every "
    "1–2 years. {C} is officially on the 2027 wishlist now 📝💄✨ Follow our "
    "Facebook for the announcement 👀",
]

_PAST_SCHEDULE_TEMPLATES = [
    "Aww girl, we were just in {C} {D}! 😭💔 We rotate cities every 1–2 years "
    "so we won't be back in 2026, but you're locked into our 2027 wishlist 💄💕 "
    "Follow our FB so you don't miss the next one ✨",
    "Oof — you JUST missed us!! 😩 {C} happened {D}. We rotate cities so won't "
    "be back this year, but {C}'s on the 2027 radar 💄✨ Stay close on Facebook 👀",
    "Awww mama, we were in {C} {D}! 😭💔 The sale already wrapped — we'll do "
    "our BEST to come back in 2027 💄💕 Watch our FB for the announcement 👀",
    "Hey gorgeous 💕 We were in {C} {D} — already wrapped! We rotate cities, "
    "so won't be back in 2026, but {C}'s officially on the 2027 list 💄✨",
    "Sweetheart 💔 {C} just wrapped {D} — you're officially on our 2027 "
    "wishlist 💄💕 Stay close on FB for the next announcement 👀",
    "Aww babe, you JUST missed us!! 😭 We were in {C} {D} 💔 We rotate cities "
    "every 1–2 years — but you're locked into our 2027 wishlist for sure 💄✨",
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

    # "Where?" comments — only meaningful when we have the post context
    post_ctx = kb.get("_post_context") if isinstance(kb, dict) else None
    if post_ctx and WHERE_PAT.search(t):
        info = summarize_event_from_caption(post_ctx.get("caption", ""))
        if info:
            seed = (kb.get("_seed", "") or "") + "where"
            status = event_status(info.get("dates", ""))
            address = info.get("address") or "(address tba)"
            venue   = info.get("venue") or "the venue"
            city, state = info.get("city",""), info.get("state","")
            dates = info.get("dates","")
            if status == "live":
                tmpl = _seeded_pick(_WHERE_LIVE_TEMPLATES, seed)
            elif status == "upcoming-soon":
                tmpl = _seeded_pick(_WHERE_UPCOMING_SOON_TEMPLATES, seed)
            elif status == "upcoming":
                tmpl = _seeded_pick(_WHERE_UPCOMING_TEMPLATES, seed)
            elif status == "past":
                tmpl = _seeded_pick(_WHERE_PAST_TEMPLATES, seed)
            else:
                tmpl = None
            if tmpl:
                return {"bucket": "A",
                        "reason": f"'Where?' on event reel — status={status}, city={city}",
                        "reply": tmpl.format(city=city, state=state, dates=dates,
                                             address=address, venue=venue)}

    # Negative — never auto-reply
    if NEGATIVE_KEYWORDS.search(t):
        return {"bucket": "NEG", "reason": "negative keywords detected", "reply": None}

    # Past-purchase or customer-issue — always Bucket B (Lauren handles personally)
    if CUSTOMER_ISSUE_PAT.search(t):
        return {"bucket": "B",
                "reason": "Customer issue / past purchase pattern — needs Lauren personally",
                "reply": None}

    # Online-order question — direct her to the website
    if ONLINE_ORDER_PAT.search(t):
        return {"bucket": "A", "reason": "Online order/availability question",
                "reply": _seeded_pick(_ONLINE_ORDER_TEMPLATES, kb.get("_seed","")+"online")}

    # Open today / are you here today
    if OPEN_TODAY_PAT.search(t):
        venues = kb.get("_venues", [])
        live = find_event_today(venues)
        seed = kb.get("_seed", "") + "today"
        if live:
            tmpl = _seeded_pick(_OPEN_TODAY_LIVE_TEMPLATES, seed)
            cf = live["city"]
            city = cf.split(",")[0].strip(); state = cf.split(",")[1].strip() if "," in cf else ""
            return {"bucket": "A", "reason": f"Open today? — LIVE at {cf}",
                    "reply": tmpl.format(city=city, state=state, address=live["address"], venue=live["venue"], dates=live["dates"])}
        nxt = find_next_event(venues)
        if nxt:
            tmpl = _seeded_pick(_OPEN_TODAY_CLOSED_TEMPLATES, seed)
            cf = nxt["city"]
            city = cf.split(",")[0].strip(); state = cf.split(",")[1].strip() if "," in cf else ""
            return {"bucket": "A", "reason": f"Open today? — closed; next is {cf}",
                    "reply": tmpl.format(city=city, state=state, address=nxt["address"], venue=nxt["venue"], dates=nxt["dates"])}

    if PAYMENT_PAT.search(t):
        return {"bucket": "A", "reason": "Payment methods",
                "reply": _seeded_pick(_PAYMENT_TEMPLATES, kb.get("_seed","")+"pay")}
    if PARKING_PAT.search(t):
        return {"bucket": "A", "reason": "Parking",
                "reply": _seeded_pick(_PARKING_TEMPLATES, kb.get("_seed","")+"park")}
    if HOURS_PAT.search(t):
        return {"bucket": "A", "reason": "Hours",
                "reply": _seeded_pick(_HOURS_TEMPLATES, kb.get("_seed","")+"hrs")}
    if KIDS_PAT.search(t):
        return {"bucket": "A", "reason": "Kids welcome",
                "reply": _seeded_pick(_KIDS_TEMPLATES, kb.get("_seed","")+"kid")}

    # Address question (only with post context)
    post_ctx2 = kb.get("_post_context") if isinstance(kb, dict) else None
    if post_ctx2 and ADDRESS_PAT.search(t):
        info = summarize_event_from_caption(post_ctx2.get("caption", ""))
        if info and info.get("address"):
            seed = (kb.get("_seed","") or "") + "addr"
            status = event_status(info.get("dates",""))
            tmpl = None
            if status == "live":          tmpl = _seeded_pick(_WHERE_LIVE_TEMPLATES, seed)
            elif status == "upcoming-soon": tmpl = _seeded_pick(_WHERE_UPCOMING_SOON_TEMPLATES, seed)
            elif status == "upcoming":      tmpl = _seeded_pick(_WHERE_UPCOMING_TEMPLATES, seed)
            elif status == "past":          tmpl = _seeded_pick(_WHERE_PAST_TEMPLATES, seed)
            if tmpl:
                return {"bucket": "A", "reason": f"Address — status={status}",
                        "reply": tmpl.format(city=info.get("city",""), state=info.get("state",""),
                                             dates=info.get("dates",""), address=info["address"],
                                             venue=info.get("venue") or "the venue")}

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
<title>@meta — Live Inbox Triage</title>
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

/* "Mark done" button — for items Lauren wants to dismiss without replying */
.done-btn{display:inline-block;background:#16a34a;color:#fff !important;
          padding:8px 14px;border-radius:6px;text-decoration:none;font-weight:700;
          font-size:13px;margin-top:6px;margin-right:6px;cursor:pointer;border:none;
          font-family:inherit;transition:transform .1s,filter .1s}
.done-btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.done-btn::before{content:"✓ "}
.done-btn[disabled]{opacity:.5;cursor:default;background:#15803d}

/* Sync token gate — token banner only shows if no token yet */
.token-banner{background:#3a2a0a;border:1px solid #d97706;color:#fbbf24;
              padding:12px 16px;border-radius:8px;margin:12px 0;font-size:13px}
.token-banner input{background:#1a1a2a;color:#eee;border:1px solid #555;
                    padding:6px 10px;border-radius:4px;font-family:monospace;
                    font-size:11px;width:100%;max-width:400px;margin-top:6px}
.token-banner button{background:#fbbf24;color:#1a1a2a;font-weight:700;
                     padding:6px 14px;border-radius:4px;border:none;cursor:pointer;
                     margin-right:6px;font-family:inherit}

/* When an item is being marked, fade it out */
.attention-row.handled-fade,.item.handled-fade{opacity:.35;transition:opacity .3s}

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



/* Inline reply layout (added 2026-05-08) */
.attention-row{flex-direction:column;align-items:stretch}
.attention-row .att-meta{display:flex;justify-content:space-between;align-items:center;gap:8px}
.attention-row .att-message{
  background:rgba(0,0,0,0.45);padding:10px 12px;border-radius:6px;
  color:#fde68a;font-size:14px;line-height:1.5;
  white-space:pre-wrap;word-wrap:break-word;
  border-left:3px solid #fbbf24
}
.attention-row textarea.att-reply{
  width:100%;min-height:80px;background:#1a1a2a;color:#eee;
  border:1px solid #555;border-radius:6px;padding:10px 12px;
  font-family:inherit;font-size:14px;line-height:1.5;
  resize:vertical;direction:auto
}
.attention-row textarea.att-reply:focus{outline:none;border-color:#fbbf24;box-shadow:0 0 0 2px rgba(251,191,36,0.2)}
.attention-row .att-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.send-btn{
  background:linear-gradient(180deg,#10b981,#059669);color:#fff !important;
  padding:9px 16px;border-radius:6px;text-decoration:none;font-weight:800;
  font-size:14px;cursor:pointer;border:none;font-family:inherit;
  transition:transform .1s,filter .1s
}
.send-btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.send-btn[disabled]{opacity:.5;cursor:default;background:#065f46}
.send-btn.sent{background:#16a34a}
.reply-btn-secondary{
  background:transparent;color:#9ca3af !important;
  padding:8px 12px;border-radius:6px;text-decoration:underline;
  font-size:12px;font-weight:600
}
.reply-btn-secondary:hover{color:#fff !important}

@media (max-width: 600px) {
  .attention-row .att-message{font-size:13px}
  .attention-row textarea.att-reply{min-height:90px;font-size:14px}
  .send-btn{width:100%;text-align:center}
}

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

<script>
__RUNTIME_JS__
</script>

</body>
</html>'''



def _format_la_time(iso_str: str) -> str:
    """Convert ISO UTC string to LA-time formatted display.
    
    Input:  '2026-05-08T16:44:25Z' or '2026-05-08T16:44:25.123Z'
    Output: 'May 8, 2026 · 9:44 AM PDT' (or PST in winter)
    """
    import datetime as _dt
    try:
        s = iso_str.rstrip("Z").split(".")[0]
        utc_dt = _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            la_dt = utc_dt.astimezone(ZoneInfo("America/Los_Angeles"))
        except Exception:
            la_dt = utc_dt.astimezone(_dt.timezone(_dt.timedelta(hours=-7)))  # fallback PDT
        # Format like "May 8, 2026 · 9:44 AM PDT"
        try:
            tz_name = la_dt.tzname() or "PT"
        except Exception:
            tz_name = "PT"
        return la_dt.strftime("%b %d, %Y · %-I:%M %p ") + tz_name
    except Exception:
        return iso_str  # fallback to raw

def _make_draft_reply(name: str, customer_text: str, channel: str) -> str:
    """Generate a friendly placeholder draft for Lauren to edit before sending."""
    display_name = (name or "").lstrip("@")
    first_word = display_name.split()[0].split("_")[0] if display_name else "girl"
    first_word = first_word[:20].title() if first_word else "girl"

    text_lower = (customer_text or "").lower()

    if "?" in (customer_text or "") or any(w in text_lower for w in ["when","where","how","what","why","who","do you","can i","is it"]):
        return f"Hey {first_word} 💜 Thanks for reaching out! Let me check on that and get back to you super soon ✨"
    if any(w in text_lower for w in ["bad","terrible","wrong","broken","upset","angry","disappointed","scam","refund","return"]):
        return f"Hi {first_word} — so sorry to hear that 💜 Can you share a bit more so we can make this right?"
    if not (customer_text or "").strip() or any(emj in customer_text for emj in ["📸","🎁","❤️","🎬","🎤","📎"]):
        return f"Hi {first_word} 💜 Thanks so much for the message! Anything we can help with?"
    return f"Hi {first_word} 💜 Thanks for reaching out! Let me get back to you super soon ✨"


def render_preview(snapshot: dict, classified_messenger: list,
                   classified_fb_comments: list,
                   classified_ig_comments: list) -> str:
    """Build the static HTML preview page."""
    n_a = sum(1 for c in classified_messenger if c["cls"]["bucket"] == "A")
    n_b = sum(1 for c in classified_messenger if c["cls"]["bucket"] == "B")
    n_neg = sum(1 for c in classified_messenger if c["cls"]["bucket"] == "NEG")
    n_fb = len(classified_fb_comments)
    n_ig = len(classified_ig_comments)

    # Collect every Bucket B item across all channels — the things needing Lauren.
    # Server-side filter: skip items already marked handled (so count + visible items match).
    try:
        from pathlib import Path as _Path
        import json as _json
        _h = _Path("docs/meta/handled.json")
        _handled = _json.loads(_h.read_text()) if _h.exists() else {}
    except Exception:
        _handled = {}
    def _is_handled(dkey):
        if not dkey: return False
        return bool(_handled.get(dkey, {}).get("handled"))

    attention_items = []
    for m in classified_messenger:
        if _is_handled(m.get("dedup_key", "")):
            continue
        if m["cls"]["bucket"] in ("B", "NEG"):
            who = m.get("name", "?")
            attention_items.append({
                "channel": "Messenger",
                "who":     who,
                "what":    m.get("msg", "(no text)") or "(empty)",
                "url":     m.get("reply_url", ""),
                "bucket":  m["cls"]["bucket"],
                "dedup_key": m.get("dedup_key", ""),
                "reply_kind": "messenger",
                "target_id": m.get("customer_psid", ""),  # PSID for Send API
                "draft":    m["cls"].get("reply") or _make_draft_reply(who, m.get("msg", ""), "messenger"),
            })
    for c in classified_fb_comments:
        if _is_handled(c.get("dedup_key", "")):
            continue
        if c["cls"]["bucket"] in ("B", "NEG"):
            who_from = c.get("from", "?")
            attention_items.append({
                "channel": "FB comment",
                "who":     who_from,
                "what":    c.get("text", "(no text)"),
                "url":     c.get("reply_url", ""),
                "bucket":  c["cls"]["bucket"],
                "dedup_key": c.get("dedup_key", ""),
                "reply_kind": "fb_comment",
                "target_id": c.get("comment_id") or c.get("id", ""),
                "draft":    c["cls"].get("reply") or _make_draft_reply(who_from, c.get("text", ""), "fb_comment"),
            })
    for c in classified_ig_comments:
        if _is_handled(c.get("dedup_key", "")):
            continue
        if c["cls"]["bucket"] in ("B", "NEG"):
            who_ig = "@" + c.get("username", "?")
            attention_items.append({
                "channel": "IG comment",
                "who":     who_ig,
                "what":    c.get("text", "(no text)"),
                "url":     c.get("reply_url", ""),
                "bucket":  c["cls"]["bucket"],
                "dedup_key": c.get("dedup_key", ""),
                "reply_kind": "ig_comment",
                "target_id": c.get("id", ""),
                "draft":    c["cls"].get("reply") or _make_draft_reply(who_ig, c.get("text", ""), "ig_comment"),
            })

    parts = [PAGE_HEAD]
    parts.append(f"<h1>📬 @meta — Live Inbox Triage</h1>")
    parts.append(f'<div class="sub">'
                 f'🕘 סריקה אחרונה: {_format_la_time(snapshot["fetched_at"])} · '
                 f'Bucket A auto-replies are LIVE · Send Reply button on items below sends via Meta API.</div>')

    # === Attention block: items needing Lauren — at the very top ===
    if attention_items:
        parts.append('<div class="attention-block">')
        parts.append(f'<h2>👀 {len(attention_items)} items need your attention</h2>')
        parts.append('<p class="intro">📝 ערכי את הטיוטה אם צריך → 📤 Send Reply → התשובה נשלחת אוטומטית דרך Meta API.</p>')
        parts.append('<div class="attention-list">')
        for item in attention_items:
            url_attr = html.escape(item["url"]) if item["url"] else "#"
            dkey = html.escape(item.get("dedup_key", ""))
            tid  = html.escape(item.get("target_id", ""))
            kind = html.escape(item.get("reply_kind", ""))
            draft = html.escape(item.get("draft") or "")
            parts.append(f'<div class="attention-row" data-dedup-key="{dkey}" data-target-id="{tid}" data-reply-kind="{kind}">')
            parts.append(f'<div class="att-meta"><span class="who">[{item["channel"]}] {html.escape(item["who"])}</span></div>')
            # Full message text (no truncation — let CSS handle wrap)
            parts.append(f'<div class="att-message">💬 {html.escape(item["what"])}</div>')
            # Inline reply textarea + Send button
            parts.append(f'<textarea class="att-reply" data-key="{dkey}" placeholder="כתבי תשובה...">{draft}</textarea>')
            parts.append('<div class="att-actions">')
            if tid and kind:
                parts.append(f'<button class="send-btn" data-key="{dkey}" data-tid="{tid}" data-kind="{kind}" onclick="sendReply(this)">📤 Send Reply</button>')
            if dkey:
                parts.append(f'<button class="done-btn" data-key="{dkey}" onclick="markDone(this)">Skip / Done</button>')
            if item["url"]:
                parts.append(f'<a class="reply-btn-secondary" href="{url_attr}" target="_blank" rel="noopener" onclick="markReplyClicked(this)" data-key="{dkey}">↗ Open in Meta</a>')
            parts.append('</div></div>')
        parts.append('</div></div>')

    # (verbose warning removed for mobile cleanup)

    # Stats
    parts.append('''<div class="legend" style="background:#1a2030;border-radius:10px;padding:14px 18px;margin:14px 0;font-size:13px;line-height:1.7">
<strong style="color:#fbbf24;font-size:14px">📊 מה כל מספר אומר?</strong>
<div style="margin-top:8px;color:#aaa">
🟢 <b style="color:#10b981">MESSENGER BUCKET A</b> = הודעות שהסוכן ענה עליהן <b>אוטומטית</b> (KB matched). את לא צריכה לעשות כלום.<br>
🟡 <b style="color:#fbbf24">MESSENGER BUCKET B</b> = הודעות שהסוכן <b>לא בטוח</b> איך לענות — אלה רואות אותך למעלה ב-"items need your attention".<br>
🔴 <b style="color:#ef4444">MESSENGER NEGATIVE</b> = הודעות שליליות שהסוכן <b>לא יענה לעולם</b> אוטומטית (תלונות חזקות).<br>
💬 <b style="color:#9ca3af">FB COMMENTS / IG COMMENTS</b> = תגובות בפוסטים — מנותחות בנפרד.<br>
📦 <b style="color:#9ca3af">auto-drafts (tap to inspect)</b> = טיוטות שהסוכן הכין; לחיצה תפתח כדי לראות מה נשלח.
</div></div>''')
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

    rendered = "\n".join(parts) + PAGE_FOOT
    # Inject the runtime JS (kept in a separate file to avoid Python escape hell)
    js_path = Path(__file__).resolve().parent / "meta_inbox_preview_runtime.js"
    runtime_js = js_path.read_text(encoding="utf-8") if js_path.exists() else ""
    return rendered.replace("__RUNTIME_JS__", runtime_js)


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


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-sms", action="store_true",
                    help="Send Touchpoint 1 + Touchpoint 3 SMS to Lauren")
    ap.add_argument("--reply-test", metavar="CONV_ID",
                    help="Phase 2 supervised test: send the Bucket A reply for ONE specific Messenger conv_id (live)")
    ap.add_argument("--reply-bucket-a", action="store_true",
                    help="Phase 2 LIVE: actually send all Bucket A Messenger replies (dry_run=False). Use only after a successful --reply-test.")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    kb_path = _resolve_kb_path()
    kb = load_kb(kb_path)
    kb["_venues"] = load_venues()
    print(f"KB loaded: {len(kb['faqs'])} FAQs, {len(kb['schedule'])} cities, "
          f"{len(kb['negatives'])} negative situations")

    print("Fetching inbox snapshot...")
    snap = fetch_recent_inbox(days=7, include_messenger=True, include_ig_dms=False,
                              include_fb_comments=True, include_ig_comments=True,
                              fb_post_limit=10, ig_media_limit=10)
    print(f"  messenger: {len(snap['messenger'])}, "
          f"fb_comments groups: {len(snap['fb_comments'])}, "
          f"ig_comments groups: {len(snap['ig_comments'])}")

    # Load dedup memory + filter items Lauren has already handled
    handled = load_handled()
    print(f"  handled-state entries: {len(handled)}")

    # Enrich messenger conversations with the latest message text
    classified_messenger = []
    for c in snap["messenger"]:
        if is_handled(handled, "messenger", c.get("id", "")):
            continue   # Lauren replied / marked done in a prior run
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
            if last_customer_msg:
                text = last_customer_msg.get("message", "") or ""
                # If text is empty, derive a label from attachments/stickers
                if not text.strip():
                    if last_customer_msg.get("sticker"):
                        text = "🎁 (sticker)"
                    else:
                        atts = (last_customer_msg.get("attachments") or {}).get("data") or []
                        if atts:
                            att = atts[0]
                            mime = (att.get("mime_type") or "").lower()
                            atype = (att.get("type") or "").lower()
                            if "image" in mime or atype == "image":
                                text = "📸 (image attachment)"
                            elif "video" in mime or atype == "video":
                                text = "🎬 (video attachment)"
                            elif atype == "audio":
                                text = "🎤 (audio message)"
                            elif atype == "file":
                                text = "📎 (file attachment)"
                            else:
                                text = f"📎 ({atype or mime or 'attachment'})"
                        else:
                            text = "❤️ (reaction or empty message)"
            else:
                text = ""
        except Exception as e:
            text = f"(error fetching: {e})"
        kb["_seed"] = conv_id  # per-conversation seed for reply variation
        cls = classify(text, kb)
        # Build a direct reply URL using the customer's PSID (the participant
        # who is NOT the Page). Format `https://www.facebook.com/messages/t/{psid}`
        # is the only reliable deep-link that opens the specific Messenger thread.
        # The conv_id (e.g. "t_10242117887745746") is NOT a usable URL component.
        page_id = get_fb_page_id()
        customer_psid = ""
        for pp in (c.get("participants", {}).get("data", []) or []):
            if pp.get("id") and pp.get("id") != page_id:
                customer_psid = pp["id"]
                break
        reply_url = f"https://www.facebook.com/messages/t/{customer_psid}" if customer_psid else \
                    f"https://business.facebook.com/latest/inbox/all?asset_id={page_id}"
        classified_messenger.append({
            "conv_id": conv_id,
            "dedup_key": dedup_key("messenger", conv_id),
            "name": customer.get("name", "?"),
            "msg": text,
            "updated_time": c.get("updated_time", ""),
            "reply_url": reply_url,
            "customer_psid": customer_psid,
            "cls": cls,
        })

    # FB comments
    classified_fb = []
    for grp in snap["fb_comments"]:
        post_msg = grp["post"].get("message", "")[:80]
        post_permalink = grp["post"].get("permalink_url", "")
        for cmt in grp["comments"]:
            if is_handled(handled, "fb-comment", cmt.get("id", "")):
                continue
            txt = cmt.get("message", "")
            kb["_seed"] = cmt.get("id", "")
            kb["_post_context"] = {
                "caption": grp["post"].get("message", ""),
                "date": grp["post"].get("created_time", ""),
            }
            cls = classify(txt, kb)
            # FB comment: prefer post permalink (the comment will be visible there;
            # Meta doesn't always expose direct comment-permalinks for replies)
            classified_fb.append({
                "comment_id": cmt.get("id", ""),
                "dedup_key": dedup_key("fb-comment", cmt.get("id", "")),
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
            if is_handled(handled, "ig-comment", cmt.get("id", "")):
                continue
            txt = cmt.get("text", "")
            kb["_seed"] = cmt.get("id", "")
            kb["_post_context"] = {
                "caption": grp["media"].get("caption", ""),
                "date": grp["media"].get("timestamp", ""),
            }
            cls = classify(txt, kb)
            # IG: tap the reel permalink — comment is visible inline on the reel
            classified_ig.append({
                "comment_id": cmt.get("id", ""),
                "dedup_key": dedup_key("ig-comment", cmt.get("id", "")),
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

    # === Phase 2 — supervised single-reply test ===
    if args.reply_test:
        target = args.reply_test
        match = next((m for m in classified_messenger if m.get("conv_id") == target), None)
        if not match:
            print(f"  ❌ no Bucket A draft found for conv_id={target}")
        elif match["cls"]["bucket"] != "A":
            print(f"  ❌ conv_id={target} is in Bucket {match['cls']['bucket']} — only Bucket A items can be auto-replied")
        elif not match["cls"].get("reply"):
            print(f"  ❌ conv_id={target} has no reply text")
        else:
            from lauren_meta import reply_to_messenger
            # Find the recipient PSID (the customer participant)
            try:
                page_id = get_fb_page_id()
                # We need to fetch the conversation to get participant IDs
                from lauren_meta import fetch_messenger_conversations
                allc = fetch_messenger_conversations(limit=50, only_unread=False)
                full = next((c for c in allc if c.get("id") == target), None)
                psid = ""
                if full:
                    for pp in full.get("participants", {}).get("data", []):
                        if pp.get("id") and pp.get("id") != page_id:
                            psid = pp["id"]
                            break
                if not psid:
                    print(f"  ❌ couldn't resolve recipient PSID for {target}")
                else:
                    print(f"  📤 sending live reply to {match['name']} (PSID={psid})...")
                    print(f"      text: {match['cls']['reply']}")
                    r = reply_to_messenger(psid, match["cls"]["reply"], dry_run=False)
                    print(f"  ✓ sent: {r}")
                    # Mark handled so the next run skips it
                    handled[dedup_key("messenger", target)] = {
                        "handled": True,
                        "handledAt": _now_iso(),
                        "method": "phase2-reply-test",
                    }
                    # Persist (best-effort — the workflow will commit via the deploy step)
                    handled_path = Path(__file__).resolve().parent.parent / "docs/meta/handled.json"
                    handled_path.write_text(_json.dumps(handled, indent=2, ensure_ascii=False), encoding="utf-8")
                    print(f"  ✓ marked handled in docs/meta/handled.json")
            except Exception as e:
                print(f"  ❌ reply failed: {e}")

    # === Phase 2 — full Bucket A auto-reply (live) ===
    if args.reply_bucket_a:
        from lauren_meta import reply_to_messenger, fetch_messenger_conversations
        sent = 0; failed = 0
        page_id = get_fb_page_id()
        allc = fetch_messenger_conversations(limit=50, only_unread=False)
        psid_by_conv = {}
        for c in allc:
            for pp in c.get("participants", {}).get("data", []):
                if pp.get("id") and pp.get("id") != page_id:
                    psid_by_conv[c.get("id")] = pp["id"]
                    break
        for m in classified_messenger:
            if m["cls"]["bucket"] != "A" or not m["cls"].get("reply"):
                continue
            psid = psid_by_conv.get(m["conv_id"])
            if not psid:
                failed += 1; continue
            try:
                reply_to_messenger(psid, m["cls"]["reply"], dry_run=False)
                handled[dedup_key("messenger", m["conv_id"])] = {
                    "handled": True, "handledAt": _now_iso(), "method": "phase2-auto",
                }
                sent += 1
            except Exception as e:
                print(f"  ❌ {m['name']}: {e}")
                failed += 1
        print(f"  Phase 2 auto-reply: sent={sent} failed={failed}")
        handled_path = Path(__file__).resolve().parent.parent / "docs/meta/handled.json"
        handled_path.write_text(_json.dumps(handled, indent=2, ensure_ascii=False), encoding="utf-8")

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
