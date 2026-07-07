"""
lauren_tiktok.py — TikTok ad-comment access for the @meta/@social inbox agent.

TikTok's PUBLIC API does not expose organic-video comments or DMs. But the
TikTok API for Business (Marketing API) DOES expose full comment management for
comments on your OWN ADS — mirroring exactly how we handle Meta dark-post ad
comments (lauren_meta.fetch_ads_comments). Endpoints (all /open_api/v1.3):

    GET  /comment/list/            — read ad comments
    POST /comment/post/            — reply to a comment
    POST /comment/status/update/   — hide / unhide comments
    POST /comment/delete/          — delete comments

Requires the app to hold the **Ad Comments** permission scope and the advertiser
to have granted it (re-auth OAuth). Until then every call returns
    code 40001: advertiser does not grant /comment/list/:GET permission
which is the exact signal that the scope grant is still pending.

Auth: same as lauren_stats.py — header `Access-Token: <TIKTOK_ACCESS_TOKEN>`,
advertiser via TIKTOK_ADVERTISER_ID.

Set 2026-07-03. Inert (not wired into the 3-hourly workflow) until the scope is
live; activate by adding a step to meta-inbox-daily.yml (or a dedicated
tiktok-inbox workflow) once `python3 scripts/tiktok_inbox.py --probe` returns 200.
"""
from __future__ import annotations
import os as _os
import json as _json
import time as _time
import urllib.request as _rq
import urllib.parse as _up
import urllib.error as _ue

TT_BASE = "https://business-api.tiktok.com/open_api/v1.3"


def get_token() -> str:
    """Comment-scoped token. The Ad Comments OAuth re-auth (2026-07-07) mints a
    token that carries ONLY the comment scope — it can read/reply comments but
    CANNOT do reporting/ad-management. So comments use their OWN secret
    (TIKTOK_COMMENT_TOKEN); we fall back to TIKTOK_ACCESS_TOKEN only if the
    dedicated one is unset."""
    return (_os.environ.get("TIKTOK_COMMENT_TOKEN")
            or _os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()


def get_report_token() -> str:
    """Reporting/ad-management token (TIKTOK_ACCESS_TOKEN) — used ONLY to
    enumerate ad-group IDs via /report/integrated/get/. Kept separate from the
    comment token so refreshing one never breaks the other (marketing-stats
    depends on TIKTOK_ACCESS_TOKEN for spend)."""
    return (_os.environ.get("TIKTOK_ACCESS_TOKEN")
            or _os.environ.get("TIKTOK_COMMENT_TOKEN") or "").strip()


def get_advertiser_id() -> str:
    return (_os.environ.get("TIKTOK_ADVERTISER_ID") or "").strip()


class TikTokPermissionError(RuntimeError):
    """Raised on code 40001 — the Ad Comments scope is not granted yet."""


def _tt_get(path: str, params: dict) -> dict:
    tok = get_token()
    url = f"{TT_BASE}{path}?{_up.urlencode(params)}"
    req = _rq.Request(url, headers={"Access-Token": tok})
    with _rq.urlopen(req, timeout=30) as r:
        return _json.loads(r.read().decode("utf-8"))


def _tt_post(path: str, body: dict) -> dict:
    tok = get_token()
    data = _json.dumps(body).encode("utf-8")
    req = _rq.Request(f"{TT_BASE}{path}", data=data, method="POST",
                      headers={"Access-Token": tok, "Content-Type": "application/json"})
    with _rq.urlopen(req, timeout=30) as r:
        return _json.loads(r.read().decode("utf-8"))


def _check(resp: dict, where: str) -> dict:
    """TikTok returns HTTP 200 with a business `code` — 0 = ok. Raise otherwise."""
    code = resp.get("code")
    if code == 0:
        return resp.get("data") or {}
    msg = resp.get("message", "")
    if code == 40001 or "does not grant" in msg:
        raise TikTokPermissionError(f"{where}: {msg} (code {code})")
    raise RuntimeError(f"{where}: code {code} — {msg}")


# ---------------------------------------------------------------------------
# READ — ad comments
# ---------------------------------------------------------------------------
def _enumerate_adgroup_ids(start_date: str, end_date: str, *, only_active=True,
                           max_ids: int = 150) -> list:
    """Ad-group IDs that ran in [start_date, end_date] (YYYY-MM-DD), via the
    REPORTING endpoint (only path available to us — /adgroup/get/ needs an
    ad-management scope neither token holds). Only active ads collect comments,
    so we default to spend>0 ad groups to keep the comment scan small."""
    adv = get_advertiser_id()
    tok = get_report_token()
    if not adv or not tok:
        return []
    url = ("https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/"
           "?" + _up.urlencode({
               "advertiser_id": adv,
               "report_type": "BASIC",
               "data_level": "AUCTION_ADGROUP",
               "dimensions": _json.dumps(["adgroup_id"]),
               "metrics": _json.dumps(["adgroup_name", "spend"]),
               "page_size": 1000,
               "start_date": start_date,
               "end_date": end_date,
           }))
    req = _rq.Request(url, headers={"Access-Token": tok})
    try:
        with _rq.urlopen(req, timeout=30) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠ tiktok: adgroup enumeration failed: {str(e)[:120]}")
        return []
    ids = []
    for row in (data.get("data", {}) or {}).get("list", []):
        ag = (row.get("dimensions") or {}).get("adgroup_id")
        spend = 0.0
        try:
            spend = float((row.get("metrics") or {}).get("spend") or 0)
        except Exception:
            pass
        if not ag:
            continue
        if only_active and spend <= 0:
            continue
        ids.append(ag)
        if len(ids) >= max_ids:
            break
    return ids


def _post(path: str, body: dict) -> dict:
    return _tt_post(path, body)


def fetch_ad_comments(*, start_time: str, end_time: str,
                      comment_status=("PUBLIC",), page_size: int = 50,
                      max_pages: int = 20, poll_tries: int = 4,
                      poll_sleep: float = 2.0) -> list:
    """Return normalized ad comments in [start_time, end_time] (UTC
    'YYYY-MM-DD HH:MM:SS').

    🛑 TikTok reality (verified live 2026-07-07): the SYNC endpoint
    /comment/list/ returns code 51010 "Internal Time out" for this advertiser's
    ads, and the ASYNC export (/comment/task/create/ → /comment/task/check/) is
    the supported path. BUT the advertiser's campaigns are **Smart+**
    ("Upgraded Smart Plus"), which the comment API does NOT support:
      • some ad groups are rejected at task/create with 40002 "This API does not
        support Upgraded Smart Plus ads."
      • the rest accept the task but task/check returns status FAILED.
    So today this returns [] for every ad group — correctly and harmlessly. If
    Lauren ever runs NON-Smart+ (standard/manual) TikTok campaigns, or TikTok
    adds Smart+ comment support, this lights up automatically: the async task
    completes, we download + normalize, and the SAME @meta engine classifies.

    Uses the comment token to read; the reporting token (get_report_token) only
    to enumerate ad-group IDs. Raises TikTokPermissionError on a real 40001.
    """
    adv = get_advertiser_id()
    if not get_token() or not adv:
        print("  ⚠ tiktok: comment token/advertiser not set — skipping")
        return []

    sd, ed = start_time[:10], end_time[:10]
    adgroups = _enumerate_adgroup_ids(sd, ed)
    if not adgroups:
        print("  ⚠ tiktok: no active ad groups in window — nothing to scan")
        return []
    print(f"  scanning {len(adgroups)} active ad group(s) for comments (async export)")

    out = []
    smartplus = failed = completed = 0
    for ag in adgroups:
        body = {
            "advertiser_id": adv,
            "start_time": start_time,
            "end_time": end_time,
            "search_field": "ADGROUP_ID",
            "search_value": ag,
        }
        if comment_status:
            body["comment_status"] = list(comment_status)
        try:
            created = _check(_post("/comment/task/create/", body), "comment_task_create")
        except TikTokPermissionError:
            raise
        except RuntimeError as e:
            if "Smart Plus" in str(e) or "40002" in str(e):
                smartplus += 1
                continue
            raise
        task_id = created.get("task_id")
        if not task_id:
            continue
        # poll for completion
        status = None
        for _ in range(poll_tries):
            _time.sleep(poll_sleep)
            try:
                chk = _check(_tt_get("/comment/task/check/",
                                     {"advertiser_id": adv, "task_id": task_id}),
                             "comment_task_check")
            except Exception:
                break
            status = (chk.get("status") or "").upper()
            if status in ("COMPLETED", "SUCCESS", "FINISH", "DONE", "FAILED"):
                break
        if status == "FAILED" or status is None:
            failed += 1
            continue
        # COMPLETED — download + parse (best-effort; format captured for parser)
        completed += 1
        url = chk.get("url") or chk.get("download_url") or chk.get("file_url") or ""
        rows = _download_comment_export(url) if url else (chk.get("comments") or chk.get("list") or [])
        if not rows:
            print(f"  ⚠ tiktok: task {task_id[-6:]} COMPLETED but no rows parsed; "
                  f"raw check payload: {_json.dumps(chk, ensure_ascii=False)[:300]}")
        for raw in rows:
            out.append(normalize_comment(raw))

    notes = []
    if smartplus:  notes.append(f"{smartplus} Smart+ ad group(s) unsupported by comment API")
    if failed:     notes.append(f"{failed} export(s) FAILED")
    if completed:  notes.append(f"{completed} export(s) completed")
    if notes:
        print("  ⓘ tiktok comments: " + "; ".join(notes))
    return out


def _download_comment_export(url: str) -> list:
    """Download a completed comment-export file and return a list of raw comment
    dicts. Handles JSON (list, or {data:{list:[]}}/{comments:[]}) and CSV.
    Best-effort — the exact format will be confirmed against the first real
    COMPLETED task (Smart+ blocks all of them today)."""
    try:
        with _rq.urlopen(_rq.Request(url), timeout=30) as r:
            body = r.read()
    except Exception as e:
        print(f"  ⚠ tiktok: export download failed: {str(e)[:120]}")
        return []
    txt = body.decode("utf-8", "replace").strip()
    # JSON?
    try:
        j = _json.loads(txt)
        if isinstance(j, list):
            return j
        if isinstance(j, dict):
            return (j.get("data", {}) or {}).get("list") or j.get("comments") or j.get("list") or []
    except Exception:
        pass
    # CSV?
    import csv, io
    try:
        rdr = csv.DictReader(io.StringIO(txt))
        return [dict(row) for row in rdr]
    except Exception:
        return []


def normalize_comment(raw: dict) -> dict:
    """Map a raw TikTok comment record to the shape the inbox engine expects.

    ⚠ Field names below are best-effort from the SDK docs; run
    `tiktok_inbox.py --probe` once the scope is live and reconcile against the
    real payload (per the OCTOPOS/Meta 'probe before you parse' rule).
    """
    return {
        "comment_id":    raw.get("comment_id") or raw.get("id") or "",
        "text":          raw.get("text") or raw.get("comment_text") or "",
        "username":      raw.get("username") or raw.get("nickname") or raw.get("display_name") or "?",
        "create_time":   raw.get("create_time") or raw.get("created_time") or "",
        # fields required to POST a reply (CommentPostBody):
        "ad_id":         raw.get("ad_id") or "",
        "tiktok_item_id": raw.get("tiktok_item_id") or raw.get("item_id") or "",
        "comment_type":  raw.get("comment_type") or "AD_COMMENT",
        "identity_id":   raw.get("identity_id") or _os.environ.get("TIKTOK_IDENTITY_ID", ""),
        "identity_type": raw.get("identity_type") or _os.environ.get("TIKTOK_IDENTITY_TYPE", ""),
        "status":        raw.get("comment_status") or raw.get("status") or "",
        "reply_url":     "https://ads.tiktok.com/i18n/comment/",  # advertiser comment center
        "_raw":          raw,
    }


# ---------------------------------------------------------------------------
# WRITE — reply / hide  (dry_run=True by default in the runner)
# ---------------------------------------------------------------------------
def reply_to_comment(comment: dict, text: str, *, dry_run: bool = True) -> dict:
    """Reply to one ad comment via /comment/post/."""
    if dry_run:
        return {"dry_run": True, "comment_id": comment.get("comment_id"), "text": text}
    body = {
        "advertiser_id": get_advertiser_id(),
        "comment_id":    comment["comment_id"],
        "ad_id":         comment.get("ad_id", ""),
        "tiktok_item_id": comment.get("tiktok_item_id", ""),
        "comment_type":  comment.get("comment_type", "AD_COMMENT"),
        "identity_id":   comment.get("identity_id", ""),
        "identity_type": comment.get("identity_type", ""),
        "text":          text,
    }
    return _check(_tt_post("/comment/post/", body), "comment_post")


def hide_comments(comment_ids, *, operation: str = "HIDE", dry_run: bool = True) -> dict:
    """Hide/unhide a list of ad comments via /comment/status/update/.
    operation ∈ {HIDE, UNHIDE} (verify exact enum on first live call)."""
    if dry_run:
        return {"dry_run": True, "comment_ids": list(comment_ids), "operation": operation}
    body = {
        "advertiser_id": get_advertiser_id(),
        "comment_ids":   list(comment_ids),
        "operation":     operation,
    }
    return _check(_tt_post("/comment/status/update/", body), "comment_status_update")
