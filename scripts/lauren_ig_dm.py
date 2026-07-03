"""
lauren_ig_dm.py — Instagram DIRECT MESSAGE (DM) access for the @meta inbox agent.

BREAKTHROUGH 2026-07-03: IG private DMs are reachable WITHOUT Meta App Review —
via the "Instagram API with Instagram login" flow. The app owner (Lauren)
authorizes her OWN account (themakeupblowoutsale) and gets a long-lived (60-day,
refreshable) token that can read AND send DMs on that account with Standard
Access. This sidesteps the app-capability wall that the Page-token path hits
(`(#3) Application does not have the capability`).

Endpoint host: graph.instagram.com (NOT graph.facebook.com).
Token: env IG_LOGIN_TOKEN (also cached at .claude/secrets/ig_login_token.txt).
Account: themakeupblowoutsale, IG user id 17841403894235577.

Verified live 2026-07-03: /me/conversations, conversation messages, and
POST /me/messages (reply) all work with this token.
"""
from __future__ import annotations
import os as _os
import json as _json
import urllib.request as _rq
import urllib.parse as _up
import urllib.error as _ue

IG_BASE = "https://graph.instagram.com/v22.0"
# messages whose text starts with this are the built-in Meta "instant reply"
# auto-responder — they must NOT count as a real human answer for dedup.
AUTO_REPLY_PREFIX = "Hi, thanks for contacting us"


def get_token() -> str:
    t = (_os.environ.get("IG_LOGIN_TOKEN") or "").strip()
    if t:
        return t
    for p in (".claude/secrets/ig_login_token.txt",
              _os.path.expanduser("~/.claude/secrets/ig_login_token.txt")):
        try:
            return open(p).read().strip()
        except Exception:
            pass
    return ""


def _get(path: str, params: dict) -> dict:
    params = dict(params); params["access_token"] = get_token()
    url = f"{IG_BASE}/{path}?{_up.urlencode(params)}"
    with _rq.urlopen(_rq.Request(url), timeout=30) as r:
        return _json.loads(r.read().decode("utf-8"))


def fetch_conversations(limit: int = 25) -> list:
    """Return IG DM conversations (newest first) with participants."""
    if not get_token():
        print("  ⚠ ig-dm: no IG_LOGIN_TOKEN — skipping IG DMs")
        return []
    try:
        d = _get("me/conversations",
                 {"platform": "instagram", "fields": "id,updated_time,participants", "limit": limit})
        return d.get("data", [])
    except _ue.HTTPError as e:
        print(f"  ⚠ ig-dm conversations HTTP {e.code}: {e.read().decode()[:150]}")
        return []
    except Exception as e:
        print(f"  ⚠ ig-dm conversations failed: {e}")
        return []


def fetch_messages(conv_id: str, limit: int = 8) -> list:
    """Messages in a conversation, newest first: [{message, from{id,username}, created_time}]."""
    try:
        d = _get(conv_id, {"fields": f"messages.limit({limit}){{message,from,created_time}}"})
        return (d.get("messages") or {}).get("data", [])
    except Exception as e:
        print(f"  ⚠ ig-dm messages[{conv_id}] failed: {str(e)[:80]}")
        return []


ME_ID = "17841403894235577"


def latest_customer_message(msgs: list, me_id: str = ME_ID):
    """Return (msg, already_answered_by_human). msgs newest-first.

    already_answered_by_human = the account sent a NON-auto-reply message AFTER
    the customer's latest message (so a human/other tool already handled it).
    """
    cust = next((m for m in msgs if (m.get("from") or {}).get("id") != me_id), None)
    if not cust:
        return None, False
    ct = cust.get("created_time", "")
    answered = any(
        (m.get("from") or {}).get("id") == me_id
        and (m.get("created_time", "") > ct)
        and not (m.get("message") or "").startswith(AUTO_REPLY_PREFIX)
        for m in msgs
    )
    return cust, answered


def send_reply(recipient_id: str, text: str, *, dry_run: bool = True) -> dict:
    """Send an IG DM reply. Standard 24h messaging window applies."""
    if dry_run:
        return {"dry_run": True, "recipient_id": recipient_id, "text": text}
    body = _json.dumps({"recipient": {"id": recipient_id},
                        "message": {"text": text}}).encode("utf-8")
    req = _rq.Request(f"{IG_BASE}/me/messages?access_token={get_token()}",
                      data=body, method="POST",
                      headers={"Content-Type": "application/json"})
    with _rq.urlopen(req, timeout=25) as r:
        return _json.loads(r.read().decode("utf-8"))


def refresh_token() -> dict:
    """Refresh the long-lived IG token (~60 days). Returns {access_token, expires_in}.
    NOTE: the refresh endpoint is NOT under the versioned path."""
    url = ("https://graph.instagram.com/refresh_access_token?"
           + _up.urlencode({"grant_type": "ig_refresh_token", "access_token": get_token()}))
    with _rq.urlopen(_rq.Request(url), timeout=30) as r:
        return _json.loads(r.read().decode("utf-8"))
