"""
lauren_meta — shared Meta Graph API module for all GitHub Actions workflows
              and Cowork agents in laurenlev10/lauren-agent-hub-data.

Created 2026-05-06 alongside the @meta inbox migration and the landing-page-builder
reel-picker work.

Pure-stdlib (urllib + json + datetime) — same convention as lauren_sms.py so
no `pip install` step is needed in workflow YAML.

Public functions:

    fetch_recent_media(limit: int=30) -> list[dict]
        GET /{ig_business_id}/media — IG Business posts (reels, photos, carousels)
        ordered newest → oldest. Each dict has: id, caption, permalink,
        thumbnail_url, media_url, media_type, media_product_type, timestamp.

    fetch_recent_fb_posts(limit: int=30) -> list[dict]
        GET /{page_id}/posts — Facebook Page posts (newest first). Returns
        id, message, permalink_url, full_picture, created_time.

    match_by_city(items: list[dict], city: str) -> dict | None
        Case-insensitive fuzzy match on caption/message text. Returns the most-
        recent item whose caption mentions the city, or None.

    get_token() -> str
        Returns the Page Access Token. Reads in priority order:
          1. META_PAGE_TOKEN env var (GitHub Actions Secret)
          2. ./scripts/.meta_page_token (workspace fallback)
          3. ~/.claude/secrets/meta_page_token.txt (Cowork local fallback)

    get_ig_business_id() / get_fb_page_id()
        Same fallback chain for the IG Business Account ID and FB Page ID.

Architecture: derived tokens never expire when the Page admin (Eli) stays
attached, so the workflow doesn't need a refresh cycle. If Eli loses Page
admin or revokes the app, Lauren needs to redo OAuth via Graph API Explorer.

Tokens generated 2026-05-06 PM after Lauren added Use Cases (Instagram API,
Manage Pages, Embed FB/IG/Threads, Marketing API) on app
`Blowout Automation MAIN` (App ID 1478322726983424).
"""

import json as _json
import os as _os
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq
from pathlib import Path as _Path
from typing import Optional as _Optional

API_BASE = "https://graph.facebook.com/v25.0"

# ---------------------------------------------------------------------------
# Credential loading (env > workspace > local fallback)
# ---------------------------------------------------------------------------

def _load_secret(env_var: str, workspace_filename: str, local_filename: str, required: bool = True) -> str:
    v = _os.environ.get(env_var, "").strip()
    if v:
        return v
    # Inside a workflow checkout: scripts/<file>
    candidate = _Path(__file__).parent / workspace_filename
    if candidate.exists():
        return candidate.read_text().strip()
    # Cowork local fallback (Lauren's machine)
    home = _Path.home() / ".claude" / "secrets" / local_filename
    if home.exists():
        return home.read_text().strip()
    # Cowork workspace mount fallback (sandbox path) — current session only
    sess = _os.environ.get("COWORK_SESSION", "dreamy-compassionate-wozniak")
    p = _Path(f"/sessions/{sess}/mnt/Claude/.claude/secrets") / local_filename
    if p.exists():
        try:
            return p.read_text().strip()
        except PermissionError:
            pass
    if not required:
        return None
    raise SystemExit(f"{env_var} not set and no fallback file at {local_filename}")


def get_token() -> str:
    """Returns Page Access Token. Used by Messenger conversations, FB Page posts —
    endpoints Meta explicitly gates to require a Page Access Token (not a System User
    token, which would 400 with code 190 'This method must be called with a Page
    Access Token')."""
    return _load_secret("META_PAGE_TOKEN", ".meta_page_token", "meta_page_token.txt")


def get_insights_token() -> str:
    """Returns System User Token (IRON RULE #8). Used by IG/Reel/Post Insights,
    Ads Reporting, and any endpoint that benefits from the broader scope set
    (instagram_manage_insights, read_insights, etc.) + never-expires stability.
    Falls back to Page Access Token if SU token isn't configured — preserves
    backward compatibility while we transition individual endpoints."""
    return (_load_secret("META_SYSTEM_USER_TOKEN", ".meta_system_user_token", "meta_system_user_token.txt", required=False)
            or _load_secret("META_PAGE_TOKEN", ".meta_page_token", "meta_page_token.txt"))


def get_ig_business_id() -> str:
    return _load_secret("META_IG_BUSINESS_ID", ".meta_ig_business_id", "meta_ig_business_id.txt")


def get_fb_page_id() -> str:
    return _load_secret("META_FB_PAGE_ID", ".meta_fb_page_id", "meta_fb_page_id.txt")


# ---------------------------------------------------------------------------
# Internal request helper
# ---------------------------------------------------------------------------

def _get(path: str, params: dict, timeout: int = 20) -> dict:
    url = f"{API_BASE}{path}?{_urlparse.urlencode(params)}"
    req = _urlreq.Request(url, method="GET")
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except _urlerr.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Meta API {e.code} on {path}: {body[:300]}") from e


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_recent_media(limit: int = 30, *, token: _Optional[str] = None,
                       ig_id: _Optional[str] = None) -> list:
    """Newest-first list of recent IG Business media items."""
    tok = token or get_token()
    iid = ig_id or get_ig_business_id()
    fields = "id,caption,permalink,thumbnail_url,media_url,media_type,media_product_type,timestamp"
    out, after = [], None
    while len(out) < limit:
        params = {"fields": fields, "limit": min(50, limit - len(out)), "access_token": tok}
        if after:
            params["after"] = after
        data = _get(f"/{iid}/media", params)
        items = data.get("data", [])
        if not items:
            break
        out.extend(items)
        cursors = data.get("paging", {}).get("cursors", {})
        after = cursors.get("after")
        if not after or len(items) < params["limit"]:
            break
    return out[:limit]


def fetch_recent_fb_posts(limit: int = 30, *, token: _Optional[str] = None,
                          page_id: _Optional[str] = None) -> list:
    """Newest-first list of recent Facebook Page posts."""
    tok = token or get_token()
    pid = page_id or get_fb_page_id()
    fields = "id,message,permalink_url,full_picture,created_time"
    params = {"fields": fields, "limit": limit, "access_token": tok}
    data = _get(f"/{pid}/posts", params)
    return data.get("data", [])[:limit]


def fetch_media_insights(media_id: str, metrics: _Optional[list] = None,
                         *, token: _Optional[str] = None) -> dict:
    """2026-05-14 PM update: defaults to get_insights_token() (System User Token)
    which has instagram_manage_insights scope. Callers can still override via token=.
    """
    """
    Fetch Instagram Graph API insights for a single media item (reel or post).
    Returns a flat dict of metric_name → integer value.

    Default metrics for Reels: shares, plays, reach, likes, comments, saved.
    Note: not every metric is supported on every media_product_type. We
    request them individually and skip any that the API rejects (HTTP 400)
    so a single unsupported metric doesn't blank the whole result.
    """
    tok = token or get_insights_token()
    if metrics is None:
        metrics = ["shares", "plays", "reach", "likes", "comments", "saved"]
    out = {}
    # Try a single combined call first — usually works.
    try:
        params = {"metric": ",".join(metrics), "access_token": tok}
        data = _get(f"/{media_id}/insights", params)
        for m in data.get("data", []):
            name = m.get("name")
            vals = m.get("values") or []
            if name and vals:
                v = vals[0].get("value")
                if isinstance(v, (int, float)):
                    out[name] = int(v)
        if out:
            return out
    except RuntimeError:
        pass
    # Fallback: try each metric individually so one bad metric doesn't kill all.
    for metric in metrics:
        try:
            params = {"metric": metric, "access_token": tok}
            data = _get(f"/{media_id}/insights", params)
            for m in data.get("data", []):
                vals = m.get("values") or []
                if vals:
                    v = vals[0].get("value")
                    if isinstance(v, (int, float)):
                        out[m.get("name", metric)] = int(v)
        except RuntimeError:
            # Skip metrics this media doesn't support.
            continue
    return out


def find_media_id_by_permalink(permalink_url: str, *, token: _Optional[str] = None,
                               ig_id: _Optional[str] = None,
                               max_search: int = 100) -> _Optional[str]:
    """
    Convert an Instagram reel permalink (e.g. https://www.instagram.com/reel/<shortcode>/)
    into the underlying ig_business media_id by scanning recent media.

    Returns the media_id string if found, else None. Uses normalized comparison
    (case-insensitive, ignores trailing slashes / query strings) since the
    Graph API permalink doesn't always match the user-facing URL exactly.
    """
    import re as _re
    def _norm(u: str) -> str:
        u = (u or "").lower().split("?")[0].rstrip("/")
        return u
    target = _norm(permalink_url)
    # Also extract the shortcode for a fuzzier fallback match.
    m = _re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)", permalink_url)
    target_sc = (m.group(1) if m else "").lower()

    items = fetch_recent_media(limit=max_search, token=token, ig_id=ig_id)
    for it in items:
        plink = _norm(it.get("permalink", ""))
        if plink == target:
            return it.get("id")
        # Fallback: match by shortcode in case URL form differs
        if target_sc:
            mm = _re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)", it.get("permalink", ""))
            if mm and mm.group(1).lower() == target_sc:
                return it.get("id")
    return None


def find_pinned_or_latest_reel(*, token: _Optional[str] = None,
                               ig_id: _Optional[str] = None) -> _Optional[dict]:
    """
    Find the current pinned/most-recent Reel of the IG business account.
    The Graph API does not directly expose 'pinned' status, so we treat
    'most recent reel' as the proxy — Lauren's workflow is to re-pin the
    new weekly reel as the current featured content, so the latest reel
    in the feed should match the pinned one in 99% of cases.
    """
    items = fetch_recent_media(limit=15, token=token, ig_id=ig_id)
    for it in items:
        if it.get("media_product_type") == "REELS":
            return it
    # Fallback: first VIDEO item if no item carries REELS product type
    for it in items:
        if it.get("media_type") == "VIDEO":
            return it
    return None


def match_by_city(items: list, city: str, *, state: _Optional[str] = None) -> _Optional[dict]:
    """
    Find the BEST match — the item whose caption clearly identifies it as the
    subject reel for this city. Scoring prioritizes captions that say
    "Sale in {city}, {ST}" up top, demoting accidental matches like venue
    addresses that happen to mention "{city} Ave" or similar.

    Returns the highest-scored item, or None if no item scores above zero.

    Scoring (per item):
      +10  caption matches /sale in {city}, *{state}/i  (strongest signal)
      +6   caption matches /in {city}, /i  in first 80 chars
      +3   caption matches /in {city}/i  anywhere
      +1   caption mentions {city} anywhere
    """
    if not city:
        return None
    import re
    c = re.escape(city.strip().lower())
    s = re.escape(state.strip().lower()) if state else r"[a-z]{2}"
    pat_sale  = re.compile(rf"sale\s+in\s+{c}\s*,\s*{s}", re.IGNORECASE)
    pat_in_co = re.compile(rf"in\s+{c}\s*,", re.IGNORECASE)
    pat_in    = re.compile(rf"in\s+{c}", re.IGNORECASE)
    pat_any   = re.compile(rf"{c}", re.IGNORECASE)

    best, best_score = None, 0
    for it in items:
        text = (it.get("caption") or it.get("message") or "")
        head = text[:80]
        score = 0
        if pat_sale.search(text):
            score += 10
        if pat_in_co.search(head):
            score += 6
        if pat_in.search(text):
            score += 3
        if pat_any.search(text):
            score += 1
        if score > best_score:
            best, best_score = it, score
    return best


# ---------------------------------------------------------------------------
# CLI entry — useful for the refresh workflow + ad-hoc testing
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="lauren_meta CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s_media = sub.add_parser("media", help="Fetch recent IG media → JSON to stdout")
    s_media.add_argument("--limit", type=int, default=30)
    s_media.add_argument("--out", help="Write to file instead of stdout")

    s_fb = sub.add_parser("fb-posts", help="Fetch recent FB Page posts → JSON to stdout")
    s_fb.add_argument("--limit", type=int, default=30)
    s_fb.add_argument("--out", help="Write to file instead of stdout")

    s_combined = sub.add_parser("combined", help="Fetch IG + FB → unified JSON for the form")
    s_combined.add_argument("--limit", type=int, default=30)
    s_combined.add_argument("--out", required=True)

    s_match = sub.add_parser("match", help="Find best reel match for a city")
    s_match.add_argument("city")

    args = ap.parse_args()

    if args.cmd == "media":
        items = fetch_recent_media(limit=args.limit)
        out = _json.dumps({"items": items, "fetched_at": _now_iso()}, indent=2, ensure_ascii=False)
        if args.out:
            _Path(args.out).write_text(out, encoding="utf-8")
            print(f"wrote {len(items)} items to {args.out}")
        else:
            print(out)

    elif args.cmd == "fb-posts":
        posts = fetch_recent_fb_posts(limit=args.limit)
        out = _json.dumps({"posts": posts, "fetched_at": _now_iso()}, indent=2, ensure_ascii=False)
        if args.out:
            _Path(args.out).write_text(out, encoding="utf-8")
            print(f"wrote {len(posts)} posts to {args.out}")
        else:
            print(out)

    elif args.cmd == "combined":
        media = fetch_recent_media(limit=args.limit)
        try:
            fb = fetch_recent_fb_posts(limit=args.limit)
        except RuntimeError as e:
            # FB Page might not have posts API permission yet; degrade gracefully
            print(f"⚠ FB posts fetch failed (continuing without): {e}")
            fb = []
        payload = {
            "ig_media": media,
            "fb_posts": fb,
            "fetched_at": _now_iso(),
            "ig_business_id": get_ig_business_id(),
            "fb_page_id": get_fb_page_id(),
        }
        _Path(args.out).write_text(_json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {len(media)} IG + {len(fb)} FB to {args.out}")

    elif args.cmd == "match":
        items = fetch_recent_media(limit=30)
        m = match_by_city(items, args.city)
        if m:
            print(_json.dumps(m, indent=2, ensure_ascii=False))
        else:
            print(f"no match for city={args.city!r} in last {len(items)} reels")


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    _cli()


# ============================================================================
# Inbox helpers — added 2026-05-06 PM for the @meta agent migration
# from Chrome browser automation to pure-API.
# ============================================================================

def fetch_messenger_conversations(limit: int = 50, only_unread: bool = False, *,
                                  token: _Optional[str] = None,
                                  page_id: _Optional[str] = None) -> list:
    """List Facebook Messenger conversations on the Page (newest-first)."""
    tok = token or get_token()
    pid = page_id or get_fb_page_id()
    fields = "id,updated_time,unread_count,participants{id,name,picture}"
    params = {"fields": fields, "limit": limit, "access_token": tok}
    data = _get(f"/{pid}/conversations", params)
    convs = data.get("data", [])
    if only_unread:
        convs = [c for c in convs if c.get("unread_count", 0) > 0]
    return convs


def fetch_messenger_messages(conversation_id: str, limit: int = 25, *,
                             token: _Optional[str] = None) -> list:
    """Get messages within a single Messenger conversation."""
    tok = token or get_token()
    fields = "id,created_time,from,to,message,attachments{mime_type,name,file_url,type},sticker"
    params = {"fields": fields, "limit": limit, "access_token": tok}
    data = _get(f"/{conversation_id}", {"fields": f"messages.limit({limit}){{{fields}}}",
                                         "access_token": tok})
    return (data.get("messages") or {}).get("data", [])


def fetch_ig_conversations(limit: int = 50, only_unread: bool = False, *,
                           token: _Optional[str] = None,
                           ig_id: _Optional[str] = None) -> list:
    """
    List Instagram Direct conversations.
    ⚠ As of 2026-05-06, Meta returns "Application does not have the capability
    to make this API call" until an additional Instagram Messaging webhook
    setup is completed in the Meta App dashboard. Caller should handle the
    RuntimeError gracefully and fall back to skipping IG DMs (or temporarily
    using the browser path) until Lauren completes the setup.
    """
    tok = token or get_token()
    iid = ig_id or get_ig_business_id()
    fields = "id,updated_time"
    params = {"platform": "instagram", "fields": fields, "limit": limit,
              "access_token": tok}
    data = _get(f"/{iid}/conversations", params)
    convs = data.get("data", [])
    if only_unread:
        # IG conversation objects don't expose unread_count uniformly; caller
        # must compare last seen timestamp against their own cursor.
        pass
    return convs


def fetch_fb_post_comments(post_id: str, limit: int = 25, *,
                           token: _Optional[str] = None) -> list:
    """Comments on a single FB Page post."""
    tok = token or get_token()
    fields = "id,message,from,created_time,permalink_url,comment_count,like_count,parent"
    params = {"fields": fields, "limit": limit, "access_token": tok,
              "filter": "stream", "order": "chronological"}
    data = _get(f"/{post_id}/comments", params)
    return data.get("data", [])


def fetch_ig_media_comments(media_id: str, limit: int = 25, *,
                            token: _Optional[str] = None) -> list:
    """Comments on a single IG media item (reel/photo/carousel)."""
    tok = token or get_token()
    fields = "id,text,username,timestamp,like_count,replies{id,text,username,timestamp}"
    params = {"fields": fields, "limit": limit, "access_token": tok}
    try:
        data = _get(f"/{media_id}/comments", params)
    except RuntimeError:
        # Sometimes the replies{} expansion trips a Meta backend quirk that
        # returns concat'd JSON. Retry without expansion.
        params["fields"] = "id,text,username,timestamp,like_count"
        data = _get(f"/{media_id}/comments", params)
    return data.get("data", [])


def fetch_recent_inbox(days: int = 7, *,
                       include_messenger: bool = True,
                       include_ig_dms: bool = False,    # default off — needs Meta capability
                       include_fb_comments: bool = True,
                       include_ig_comments: bool = True,
                       fb_post_limit: int = 10,
                       ig_media_limit: int = 10) -> dict:
    """
    Aggregate snapshot for the @meta inbox triage — one round-trip-friendly
    call that returns everything the daily run needs.

    Returns a dict shaped like:
        {
          "messenger": [conv, ...],
          "ig_dms":    [conv, ...]    # empty if include_ig_dms=False
                                       # or capability not yet granted
          "fb_comments": [
            {"post": {...}, "comments": [...]},
            ...
          ],
          "ig_comments": [
            {"media": {...}, "comments": [...]},
            ...
          ],
          "fetched_at": "2026-05-06T22:00:00Z",
          "errors": ["<one-line cause>", ...]   # non-fatal failures
        }
    """
    out = {"messenger": [], "ig_dms": [], "fb_comments": [], "ig_comments": [],
           "fetched_at": _now_iso(), "errors": []}

    if include_messenger:
        try:
            out["messenger"] = fetch_messenger_conversations(limit=50, only_unread=True)
        except Exception as e:
            out["errors"].append(f"messenger: {e}")

    if include_ig_dms:
        try:
            out["ig_dms"] = fetch_ig_conversations(limit=50)
        except Exception as e:
            out["errors"].append(f"ig_dms: {e}")

    if include_fb_comments:
        try:
            page_id = get_fb_page_id()
            posts_resp = _get(f"/{page_id}/posts",
                              {"fields": "id,created_time,message,permalink_url",
                               "limit": fb_post_limit,
                               "access_token": get_token()})
            for post in posts_resp.get("data", []):
                try:
                    cs = fetch_fb_post_comments(post["id"], limit=25)
                    if cs:
                        out["fb_comments"].append({"post": post, "comments": cs})
                except Exception as e:
                    out["errors"].append(f"fb_comments[{post.get('id')}]: {e}")
        except Exception as e:
            out["errors"].append(f"fb_posts_list: {e}")

    if include_ig_comments:
        try:
            media = fetch_recent_media(limit=ig_media_limit)
            for m in media:
                try:
                    cs = fetch_ig_media_comments(m["id"], limit=25)
                    if cs:
                        out["ig_comments"].append({"media": m, "comments": cs})
                except Exception as e:
                    out["errors"].append(f"ig_comments[{m.get('id')}]: {e}")
        except Exception as e:
            out["errors"].append(f"ig_media_list: {e}")

    return out


# ============================================================================
# Write operations — used by the @meta agent during triage.
# All write operations require explicit confirmation from Lauren before being
# called in production. Pass dry_run=True during development.
# ============================================================================

def reply_to_messenger(recipient_id: str, text: str, *,
                       dry_run: bool = False,
                       token: _Optional[str] = None) -> dict:
    """Send a Messenger reply to a user from the Page."""
    if dry_run:
        return {"dry_run": True, "recipient_id": recipient_id, "text": text}
    tok = token or get_token()
    body = _json.dumps({
        "recipient": {"id": recipient_id},
        "message":   {"text": text},
        "messaging_type": "RESPONSE",
    }).encode("utf-8")
    req = _urlreq.Request(f"{API_BASE}/me/messages?access_token={tok}",
                          data=body, method="POST",
                          headers={"Content-Type": "application/json"})
    with _urlreq.urlopen(req, timeout=20) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def reply_to_comment(comment_id: str, text: str, *,
                     dry_run: bool = False,
                     token: _Optional[str] = None) -> dict:
    """Reply to an FB or IG comment (same endpoint pattern for both)."""
    if dry_run:
        return {"dry_run": True, "comment_id": comment_id, "text": text}
    tok = token or get_token()
    body = _urlparse.urlencode({"message": text, "access_token": tok}).encode("utf-8")
    req = _urlreq.Request(f"{API_BASE}/{comment_id}/comments",
                          data=body, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _urlreq.urlopen(req, timeout=20) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def hide_comment(comment_id: str, *,
                 dry_run: bool = False,
                 token: _Optional[str] = None) -> dict:
    """Hide a Facebook comment (sets is_hidden=true)."""
    if dry_run:
        return {"dry_run": True, "comment_id": comment_id, "action": "hide"}
    tok = token or get_token()
    body = _urlparse.urlencode({"is_hidden": "true", "access_token": tok}).encode("utf-8")
    req = _urlreq.Request(f"{API_BASE}/{comment_id}",
                          data=body, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _urlreq.urlopen(req, timeout=20) as resp:
        return _json.loads(resp.read().decode("utf-8"))
