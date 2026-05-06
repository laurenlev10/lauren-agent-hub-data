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

def _load_secret(env_var: str, workspace_filename: str, local_filename: str) -> str:
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
    raise SystemExit(f"{env_var} not set and no fallback file at {local_filename}")


def get_token() -> str:
    return _load_secret("META_PAGE_TOKEN", ".meta_page_token", "meta_page_token.txt")


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
