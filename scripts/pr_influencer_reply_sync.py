"""
pr_influencer_reply_sync — close the "she answered and it got missed" gap.

Background (2026-07-12, Lauren): a contacted influencer (Alyssa Luckey /
@luckey1ss, Mesa) replied warmly in a Meta DM, but her card in the
pr-influencer dashboard stayed on "נשלחה טיוטה / DM" — because the dashboard
only auto-populated from (a) the outbound roster (status set MANUALLY) and
(b) __inbound__ (the Collab FORM only). Nothing linked a reply back to the
contacted creator, so replies got lost.

This bridge reconciles inbound signals against the creators we ALREADY
CONTACTED (every one of whom has a status key in notes.json of the form
"City, ST|rank|-handle"), flips their status + logs the reply + SMSes Lauren
— so a reply can't be silently missed. Matching off notes.json keys makes it
independent of where the per-event roster is stored (creators.json vs the
embedded dashboard blocks).

Two sources:
  A. Collab form applications (docs/state/influencer_applications.json)
     token-FREE, high precision (an application IS an interested reply).
     A match flips the contacted creator to "ענתה — מעוניינת!".
  B. Meta comments (IG media + FB posts) matchable via `username`.
     TOKEN-GATED + FAIL-SOFT: if META_PAGE_TOKEN is missing/invalid this
     source is skipped cleanly. Matches are logged as "review" WITHOUT a hard
     status flip (comments are noisier than a form submission).
     DMs are intentionally NOT auto-matched — Meta returns opaque participant
     IDs (not usernames) for DM threads. The reply Lauren sends funnels
     DM-repliers into the Collab form (Source A), which IS reliable.

Idempotent via the __replies__ log; only NEW replies trigger an SMS.

Usage:
  python3 scripts/pr_influencer_reply_sync.py            # apply + SMS
  python3 scripts/pr_influencer_reply_sync.py --dry      # preview only
"""

import json
import re
import sys
import datetime as _dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lauren_sms import send_sms, LAUREN_PHONE

NOTES = Path("docs/pr-influencer/notes.json")
APPS = Path("docs/state/influencer_applications.json")
DASH_URL = "https://laurenlev10.github.io/lauren-agent-hub-data/pr-influencer/"

# Status vocabulary — MUST match STAGES/OTHER in docs/pr-influencer/index.html
S_REPLIED_YES = "ענתה — מעוניינת!"
STAGES = ["לא פניתי", "נשלחה טיוטה / DM", S_REPLIED_YES,
          "אישרה הגעה (סטורי הועלה)", "הגיעה ולקחה מוצרים (חוזה חתום)",
          "הגישה תוכן ✓ (תיוג נכון)"]
OTHER = ["ענתה — לא מעוניינת", "לא הגישה תוכן (deadline עבר)",
         "החזירה מוצרים / שילמה", "🏆 Hall of Fame", "🚫 חסומה לעתיד"]
STATUS_SET = set(STAGES + OTHER)
# Only auto-advance a creator sitting in one of these early stages.
EARLY = {"", "לא פניתי", "נשלחה טיוטה / DM"}
# A creator sitting in EARLY who DMs us back = a reply we must surface.
S_REPLIED_NO = "ענתה — לא מעוניינת"
# Light sentiment: obvious declines route to the "not interested" bucket
# (still surfaced), everything else to "ענתה — מעוניינת!".
_DECLINE_RE = re.compile(
    r"(no thanks|no thank you|not interested|i'?m not interested|not for me|"
    r"i'?ll pass|i will pass|unsubscribe|please stop|not able to|can'?t make it|"
    r"cannot make it|won'?t be able|not this time|no gracias|no me interesa)",
    re.I)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_handle(h: str) -> str:
    h = str(h or "").strip().lower()
    if "/" in h:
        h = h.rsplit("/", 1)[-1]
    h = h.lstrip("@")
    return re.sub(r"[^a-z0-9]", "", h)


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def contacted_index(notes: dict):
    """norm_handle -> list of contacted-creator targets, parsed from the base
    status keys in notes.json ('City, ST|rank|-handle' -> status label)."""
    idx = {}
    for k, v in notes.items():
        if not isinstance(v, str) or v not in STATUS_SET:
            continue
        parts = k.split("|")
        if len(parts) != 3:
            continue
        city, rank, hslug = parts
        nh = re.sub(r"[^a-z0-9]", "", hslug.lower())
        if not nh:
            continue
        idx.setdefault(nh, []).append({
            "key": k, "city": city, "rank": rank,
            "handle": hslug.strip("-"),
        })
    return idx


def main() -> int:
    dry = "--dry" in sys.argv
    notes = load_json(NOTES, {})
    idx = contacted_index(notes)

    notes.setdefault("__replies__", [])
    logged = {r.get("id") for r in notes["__replies__"] if isinstance(r, dict)}

    new_replies = []
    status_flips = []

    # ---- Source A: Collab form applications -> contacted creators ----
    apps_doc = load_json(APPS, {})
    for a in (apps_doc.get("applications") or []):
        nh = norm_handle(a.get("handle") or "")
        if not nh or nh not in idx:
            continue  # brand-new applicants -> covered by the digest/__inbound__
        for t in idx[nh]:
            rid = "collab-form:" + t["key"]
            if rid in logged:
                continue
            key = t["key"]
            cur = notes.get(key, "")
            flipped = False
            if cur in EARLY:
                notes[key] = S_REPLIED_YES
                notes[key + "|status_set_at"] = _now_iso()
                stamp = f"ענתה דרך טופס Collab {(a.get('received_at') or '')[:10]}"
                prev = notes.get(key + "|note")
                notes[key + "|note"] = (prev + " · " + stamp) if prev and stamp not in prev else stamp
                status_flips.append((key, cur, S_REPLIED_YES))
                flipped = True
            rec = _rec(rid, "collab-form", t, a.get("full_name"),
                       (a.get("about") or "")[:200], flipped)
            notes["__replies__"].append(rec)
            logged.add(rid)
            new_replies.append(rec)

    # ---- Source B: Meta comments (token-gated, FAIL-SOFT) ----
    try:
        _scan_meta_comments(idx, notes, logged, new_replies)
    except (Exception, SystemExit) as e:
        print(f"[meta] skipped (token not ready / fetch failed): {e}")

    # ---- Source C: Instagram DM replies (token-gated, FAIL-SOFT) ----
    # The reliable auto-path Lauren asked for: an influencer who replied in an
    # IG DM (incl. the Partnership-messages inbox) is matched by username to the
    # creator we contacted, and flipped out of the early stage automatically.
    try:
        _scan_ig_dms(idx, notes, logged, new_replies, status_flips)
    except (Exception, SystemExit) as e:
        print(f"[ig-dm] skipped (token not ready / fetch failed): {e}")

    if dry:
        print(f"DRY RUN — {len(new_replies)} new replies, {len(status_flips)} flips")
        for k, o, n in status_flips:
            print(f"  FLIP {k}: {o!r} -> {n!r}")
        for r in new_replies:
            print(f"  REPLY [{r['source']}] @{r['handle']} ({r['city']}) flip={r['status_flipped']}")
        return 0

    if not new_replies:
        print("no new replies")
        return 0

    NOTES.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote notes.json — {len(new_replies)} new replies, {len(status_flips)} flips")

    flips = [r for r in new_replies if r.get("status_flipped")]
    review = [r for r in new_replies if not r.get("status_flipped")]
    lines = [f"@pr-influencer 🔔 {len(new_replies)} משפיעניות ענו:"]
    for r in (flips + review)[:4]:
        tag = "✅ עודכן ל'ענתה'" if r.get("status_flipped") else "👀 לבדיקה"
        lines.append(f"@{r['handle']} · {r['city']} · {tag}")
    if len(new_replies) > 4:
        lines.append(f"...ועוד {len(new_replies) - 4}.")
    body = "\n".join(lines)[:280]
    send_sms(LAUREN_PHONE, body)
    send_sms(LAUREN_PHONE, DASH_URL)
    print("SMS sent to Lauren")
    return 0


def _rec(rid, source, target, full_name, message, flipped):
    return {
        "id": rid, "source": source, "key": target["key"],
        "city": target["city"], "handle": target["handle"],
        "display_name": full_name or "", "detected_at": _now_iso(),
        "status_flipped": flipped, "message": message,
    }


def _scan_meta_comments(idx, notes, logged, new_replies):
    import lauren_meta as meta
    tok = meta.get_token()
    if not tok:
        raise RuntimeError("no META_PAGE_TOKEN")

    try:
        media = meta.fetch_recent_media(limit=15)
    except Exception:
        media = []
    for m in media:
        try:
            comments = meta.fetch_ig_media_comments(m.get("id"), limit=50)
        except Exception:
            continue
        for c in comments:
            _match_comment(idx, notes, logged, new_replies,
                           author=c.get("username"), text=c.get("text"),
                           where="IG comment")

    try:
        posts = meta._get(f"/{meta.get_fb_page_id()}/posts",
                          {"fields": "id", "limit": 15, "access_token": tok}).get("data", [])
    except Exception:
        posts = []
    for p in posts:
        try:
            comments = meta.fetch_fb_post_comments(p.get("id"), limit=50)
        except Exception:
            continue
        for c in comments:
            frm = c.get("from") or {}
            _match_comment(idx, notes, logged, new_replies,
                           author=frm.get("name"), text=c.get("message"),
                           where="FB comment")


def _scan_ig_dms(idx, notes, logged, new_replies, status_flips):
    """Source C — match IG DM senders to contacted creators by username.

    For every conversation whose non-us participant is a creator we contacted
    AND who is still in an EARLY stage, look for a real reply FROM the creator.
    If found: flip status (interested, or 'not interested' on an obvious
    decline), stamp a note, log to __replies__, and queue an SMS. Idempotent
    via the log id 'igdm:<status-key>'. Token-gated + fail-soft."""
    import lauren_ig_dm as igdm
    if not igdm.get_token():
        raise RuntimeError("no IG_LOGIN_TOKEN")

    convs = igdm.fetch_all_conversations(folder="primary", limit=50, max_pages=4)
    print(f"[ig-dm] scanning {len(convs)} IG DM conversations")
    for c in convs:
        parts = ((c.get("participants") or {}).get("data")) or []
        creator = next((p.get("username") for p in parts
                        if (p.get("username") or "").lower() != "themakeupblowoutsale"
                        and p.get("username")), None)
        if not creator:
            continue
        nh = norm_handle(creator)
        if nh not in idx:
            continue

        # Only pull messages when there's a match to score.
        try:
            msgs = igdm.fetch_messages(c.get("id"), limit=15)
        except Exception:
            continue
        cust, _answered = igdm.latest_customer_message(msgs)
        if not cust:
            continue  # only WE messaged — no reply yet
        text = (cust.get("message") or "").strip()
        if not text:
            continue
        when = (cust.get("created_time") or "")[:10]
        new_status = S_REPLIED_NO if _DECLINE_RE.search(text) else S_REPLIED_YES

        for t in idx[nh]:
            rid = "igdm:" + t["key"]
            if rid in logged:
                continue
            key = t["key"]
            cur = notes.get(key, "")
            flipped = False
            if cur in EARLY:
                notes[key] = new_status
                notes[key + "|status_set_at"] = _now_iso()
                stamp = f"ענתה ב-IG DM {when}".strip()
                prev = notes.get(key + "|note")
                notes[key + "|note"] = (prev + " · " + stamp) if prev and stamp not in prev else stamp
                status_flips.append((key, cur, new_status))
                flipped = True
            rec = _rec(rid, "ig-dm", t, creator, f"IG DM: {text[:160]}", flipped)
            notes["__replies__"].append(rec)
            logged.add(rid)
            new_replies.append(rec)


def _match_comment(idx, notes, logged, new_replies, *, author, text, where):
    nh = norm_handle(author or "")
    if not nh or nh not in idx:
        return
    for t in idx[nh]:
        rid = "meta-comment:" + t["key"]
        if rid in logged:
            return
        rec = _rec(rid, "meta-comment", t, t["handle"],
                   f"{where}: {(text or '')[:160]}", False)
        notes["__replies__"].append(rec)
        logged.add(rid)
        new_replies.append(rec)


if __name__ == "__main__":
    raise SystemExit(main())
