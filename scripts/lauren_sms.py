"""
lauren_sms — shared SMS module for all GitHub Actions workflows in
              laurenlev10/lauren-agent-hub-data.

Replaces the old Chrome-based SimpleTexting flow. Every agent that wants
to message Lauren (or read her messages back) imports from here.

Authored 2026-05-05 as part of the "no Chrome ever again" migration
(Lauren's directive after watching @housing open the SimpleTexting
inbox in Chrome on a second machine).

Three public functions:

    send_sms(phone: str, text: str, *, dry_run: bool=False) -> dict
        Send a one-shot SMS via SimpleTexting v2 API
        (POST https://app2.simpletexting.com/v2/api/messages).
        `phone` is E.164 or 10-digit US (we normalize). `text` is the
        full message body — newlines, emojis, RTL Hebrew all OK.
        Returns the API response (including `id` and remaining `credits`).

    read_inbox_since(since_iso: str, *, contact_phone: str=LAUREN_PHONE,
                     limit: int=500) -> list[dict]
        Pull every inbound message from `contact_phone` since `since_iso`
        (ISO 8601 UTC). Returns a list of message dicts ordered oldest →
        newest. Each dict has at least: id, text, contact_phone,
        timestamp, direction.
        Pagination is handled internally (size=200 per page, capped by
        `limit` to keep runs bounded).

    advance_cursor(repo_path: str, agent: str, message_id: str,
                   timestamp_iso: str) -> None
        Persist a per-agent cursor under
        docs/state/sms_cursor_<agent>.json so the next run only sees
        messages that arrived after this one. Stateless callers — the
        cursor lives in the repo, not in process memory.

Environment:
    SIMPLETEXTING_TOKEN   — required (Bearer token).
    LAUREN_PHONE          — optional, defaults to "4243547625".
    ACCOUNT_PHONE         — optional, defaults to "8665510755".

The module is dependency-free — uses only stdlib (urllib, json,
datetime). That keeps the workflow YAML simple: no `pip install`
step needed.
"""

import datetime as _dt
import json as _json
import os as _os
import time as _time
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq
from pathlib import Path as _Path
from typing import Optional as _Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://app2.simpletexting.com/v2/api"
LAUREN_PHONE = _os.environ.get("LAUREN_PHONE", "4243547625").lstrip("+").lstrip("1")
ACCOUNT_PHONE = _os.environ.get("ACCOUNT_PHONE", "8665510755").lstrip("+").lstrip("1")

# Direction codes from SimpleTexting:
#   MT = mobile-terminated  = sent by us TO Lauren  (outbound)
#   MO = mobile-originated  = sent BY Lauren to us  (inbound)
DIRECTION_OUTBOUND = "MT"
DIRECTION_INBOUND = "MO"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    t = _os.environ.get("SIMPLETEXTING_TOKEN") or _os.environ.get("ST_TOKEN")
    if not t:
        raise SystemExit(
            "SIMPLETEXTING_TOKEN env var not set. "
            "In GitHub Actions: env: ST_TOKEN: ${{ secrets.SIMPLETEXTING_TOKEN }}"
        )
    return t.strip()


def _normalize_phone(p: str) -> str:
    """4243547625 / +14243547625 / (424) 354-7625 / 1-424-354-7625 → 4243547625."""
    digits = "".join(c for c in p if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"unrecognized US phone format: {p!r}")
    return digits


def _request(method: str, path: str, *, params: _Optional[dict] = None,
             body: _Optional[dict] = None, timeout: int = 20) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + _urlparse.urlencode(params)
    headers = {"Authorization": f"Bearer {_token()}"}
    data = None
    if body is not None:
        data = _json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = _urlreq.Request(url, data=data, headers=headers, method=method)
    try:
        with _urlreq.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return {}
            return _json.loads(raw)
    except _urlerr.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"SimpleTexting {method} {path} → HTTP {e.code}: {body_txt}") from e


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_sms(phone: str, text: str, *, dry_run: bool = False,
             retries: int = 1) -> dict:
    """
    Send a single SMS to `phone` from the configured ACCOUNT_PHONE.

    Returns the SimpleTexting response dict on success. On failure raises
    RuntimeError. Use `dry_run=True` to log + skip the network call (handy
    for local testing inside a workflow).

    Multi-line text + emojis + Hebrew RTL all pass through untouched.
    """
    p = _normalize_phone(phone)
    if dry_run:
        print(f"  [dry-run] would send to {p}: {text[:80]}...")
        return {"dry_run": True, "contactPhone": p, "text": text}

    payload = {"contactPhone": p, "mode": "AUTO", "text": text}
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = _request("POST", "/messages", body=payload, timeout=20)
            print(f"  ✓ SMS sent to {p}: id={resp.get('id')} "
                  f"credits={resp.get('credits')}")
            return resp
        except RuntimeError as e:
            last_err = e
            if attempt < retries:
                print(f"  ⚠ send retry {attempt+1}/{retries}: {e}")
                _time.sleep(2)
    raise last_err  # type: ignore[misc]


def read_inbox_since(since_iso: str, *, contact_phone: str = LAUREN_PHONE,
                     limit: int = 500) -> list:
    """
    Return every INBOUND message from `contact_phone` whose timestamp is
    strictly greater than `since_iso`, oldest → newest.

    SimpleTexting's `?since=` filter applies to ALL messages (in + out,
    every contact). We pull pages until either the page falls below
    `since_iso` or we hit `limit`, then filter client-side.

    `since_iso` must be ISO 8601 with timezone (e.g. "2026-05-05T00:00:00Z").
    Pass an old date to fetch from the beginning. Pass datetime.now().isoformat()
    to fetch nothing (returns empty list).
    """
    target_phone = _normalize_phone(contact_phone)
    out = []
    page = 0
    page_size = 200
    while len(out) < limit:
        params = {"size": page_size, "page": page, "since": since_iso}
        data = _request("GET", "/messages", params=params)
        content = data.get("content", [])
        if not content:
            break
        for m in content:
            if (m.get("directionType") == DIRECTION_INBOUND
                    and m.get("contactPhone") == target_phone):
                out.append({
                    "id":           m.get("id"),
                    "text":         m.get("text", ""),
                    "contact_phone": m.get("contactPhone"),
                    "timestamp":    m.get("timestamp"),
                    "direction":    "inbound",
                    "raw":          m,
                })
        # `total_pages` in response tells us when to stop
        total_pages = data.get("totalPages", 0)
        page += 1
        if page >= total_pages:
            break
    out.reverse()  # API returns newest-first; we want oldest-first
    return out[:limit]


def advance_cursor(repo_path: str, agent: str, message_id: str,
                   timestamp_iso: str) -> None:
    """
    Persist a per-agent cursor for SMS reads.

    Cursor file: docs/state/sms_cursor_<agent>.json
    Shape:
        {
          "last_processed_msg_id": "...",
          "last_processed_at":     "2026-05-05T16:29:27Z",
          "agent":                 "la-rental-search",
          "updated":               "2026-05-05T16:35:00Z"
        }

    The caller commits the file as part of its normal git push at the
    end of the workflow. Storing in /docs/state/ keeps it on the live
    GitHub Pages site (handy for debugging via curl).
    """
    state_dir = _Path(repo_path) / "docs" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp = state_dir / f"sms_cursor_{agent}.json"
    payload = {
        "last_processed_msg_id": message_id,
        "last_processed_at":     timestamp_iso,
        "agent":                 agent,
        "updated":               _dt.datetime.now(_dt.timezone.utc)
                                            .isoformat(timespec="seconds"),
    }
    fp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                  encoding="utf-8")


def load_cursor(repo_path: str, agent: str, *,
                default_iso: _Optional[str] = None) -> dict:
    """
    Read the per-agent cursor written by `advance_cursor`. If missing,
    return a fresh dict with `last_processed_at` defaulting to either
    `default_iso` or 24 h ago. Never raises on missing.
    """
    fp = _Path(repo_path) / "docs" / "state" / f"sms_cursor_{agent}.json"
    if fp.exists():
        try:
            return _json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            pass
    fallback = default_iso or (
        (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24))
        .isoformat(timespec="seconds")
    )
    return {
        "last_processed_msg_id": None,
        "last_processed_at":     fallback,
        "agent":                 agent,
        "updated":               None,
    }


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing
#
#   python3 scripts/lauren_sms.py send 4243547625 "ping"
#   python3 scripts/lauren_sms.py read 2026-05-04T00:00:00Z
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__.strip())
        raise SystemExit(0)
    cmd = sys.argv[1]
    if cmd == "send" and len(sys.argv) >= 4:
        send_sms(sys.argv[2], sys.argv[3])
    elif cmd == "read" and len(sys.argv) >= 3:
        msgs = read_inbox_since(sys.argv[2])
        for m in msgs:
            print(f"{m['timestamp']}  {m['contact_phone']}  {m['text'][:120]}")
        print(f"\n({len(msgs)} inbound messages from {LAUREN_PHONE})")
    else:
        print("usage: lauren_sms.py send <phone> <text>")
        print("       lauren_sms.py read <since_iso>")
        raise SystemExit(2)
