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
from lauren_meta import fetch_recent_inbox, fetch_messenger_messages, get_token

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
    (re.compile(r"\b(hour|when.*open|time|schedule|am|pm|10\s*-\s*5)\b", re.I),
     "What are the hours?"),
    (re.compile(r"\b(brand|carry|sell|product line)s?\b", re.I),
     "What brands do you have?"),
    (re.compile(r"\b(mystery|box|grab\s*bag)\b", re.I),
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

CITY_PAT = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


def classify(text: str, kb: dict) -> dict:
    """
    Return {bucket: 'A'|'B', reason, suggested_reply} for a message.
    Bucket A = answerable from KB (auto-reply).
    Bucket B = needs Lauren (low confidence, complex, or no KB match).
    """
    if not text or len(text.strip()) < 2:
        return {"bucket": "B", "reason": "empty/too short", "reply": None}

    t = text.strip()

    # Negative — never auto-reply
    if NEGATIVE_KEYWORDS.search(t):
        return {"bucket": "NEG", "reason": "negative keywords detected", "reply": None}

    # FAQ keyword match
    for pattern, faq_q in FAQ_KEYWORDS:
        if pattern.search(t):
            if faq_q:
                # Find the FAQ answer
                for f in kb["faqs"]:
                    if f["q"].lower().startswith(faq_q.lower()[:20]):
                        return {"bucket": "A", "reason": f"FAQ match: {f['q']}",
                                "reply": f["a"]}
            return {"bucket": "B", "reason": "keyword matched but no KB answer",
                    "reply": None}

    # City question — does the message mention a city we visit?
    cities_mentioned = []
    for m in CITY_PAT.finditer(t):
        candidate = m.group(1).lower()
        if candidate in kb["schedule"]:
            cities_mentioned.append(candidate)

    if cities_mentioned:
        c = cities_mentioned[0]
        dates = kb["schedule"][c]
        return {"bucket": "A", "reason": f"City on schedule: {c.title()}",
                "reply": f"Yes! 🎉 We're coming to {c.title()} on {dates}! "
                         f"Hours: Fri–Sun 10am–5pm. Free admission, free parking. "
                         f"💄✨ Hope to see you there!"}

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

    parts = [PAGE_HEAD]
    parts.append(f"<h1>@meta — API Preview (Phase 1 dry-run)</h1>")
    parts.append(f'<div class="sub">'
                 f'Generated {snapshot["fetched_at"]} · '
                 f'Read-only — nothing was sent or hidden.</div>')
    parts.append('<div class="warning">⚠️ This is a Phase 1 preview. The classifier here uses '
                 'simple keyword matching, not Claude reasoning. Bucket assignments may differ '
                 'from the final production agent. Review for false positives (Bucket A items '
                 'that should be Bucket B) before approving the cutover.</div>')

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

    # Messenger DMs
    parts.append(f'<h2>📬 Messenger Conversations ({len(classified_messenger)} unread)</h2>')
    if not classified_messenger:
        parts.append('<p class="sub">No unread Messenger DMs.</p>')
    for c in classified_messenger:
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
        parts.append('</div>')

    # FB Comments
    if classified_fb_comments:
        parts.append(f'<h2>💬 Facebook Comments</h2>')
        for c in classified_fb_comments:
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
            parts.append('</div>')

    # IG Comments
    if classified_ig_comments:
        parts.append(f'<h2>📷 Instagram Comments</h2>')
        for c in classified_ig_comments:
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
            parts.append('</div>')

    parts.append(PAGE_FOOT)
    return "\n".join(parts)


# --- Main --------------------------------------------------------------------

def main():
    repo = Path(__file__).resolve().parent.parent
    kb_path = Path("/sessions/dreamy-compassionate-wozniak/mnt/Claude/Scheduled/NEW/meta-inbox/inbox_knowledge_base.docx")
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
            # Find the last message FROM the customer (not from the page)
            last_customer_msg = next((m for m in messages
                                      if m.get("from", {}).get("name") != "The Makeup Blowout Sale Group"),
                                     None)
            text = last_customer_msg.get("message", "") if last_customer_msg else ""
        except Exception as e:
            text = f"(error fetching: {e})"
        cls = classify(text, kb)
        classified_messenger.append({
            "conv_id": conv_id,
            "name": customer.get("name", "?"),
            "msg": text,
            "updated_time": c.get("updated_time", ""),
            "cls": cls,
        })

    # FB comments
    classified_fb = []
    for grp in snap["fb_comments"]:
        post_msg = grp["post"].get("message", "")[:80]
        for cmt in grp["comments"]:
            txt = cmt.get("message", "")
            cls = classify(txt, kb)
            classified_fb.append({
                "post_msg": post_msg,
                "from": cmt.get("from", {}).get("name", "?"),
                "text": txt,
                "cls": cls,
            })

    # IG comments
    classified_ig = []
    for grp in snap["ig_comments"]:
        media_caption = (grp["media"].get("caption") or "").split("\n")[0][:80]
        for cmt in grp["comments"]:
            txt = cmt.get("text", "")
            cls = classify(txt, kb)
            classified_ig.append({
                "media_caption": media_caption,
                "username": cmt.get("username", "?"),
                "text": txt,
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


if __name__ == "__main__":
    main()
