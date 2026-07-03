#!/usr/bin/env python3
"""
refresh_ig_token.py — keep the Instagram-login DM token alive.

The IG_LOGIN_TOKEN (Instagram API with Instagram login) is long-lived (~60 days)
but MUST be refreshed before it expires or IG DM handling silently dies. This
refreshes it and writes the new value back to the GitHub Actions secret
IG_LOGIN_TOKEN, using IG_REFRESH_PAT (a PAT with secrets:write).

Run monthly (well inside the 60-day window). Same "never fail silently" lesson
as the Anthropic-credit outage — the workflow SMSes Lauren on failure.
"""
import os, sys, json, base64, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lauren_ig_dm as igdm

REPO = "laurenlev10/lauren-agent-hub-data"


def gh(path, pat, method="GET", data=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}", method=method,
        headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
        data=json.dumps(data).encode() if data else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    pat = os.environ.get("IG_REFRESH_PAT", "").strip()
    if not igdm.get_token():
        print("no IG_LOGIN_TOKEN to refresh"); sys.exit(1)
    res = igdm.refresh_token()
    new = res.get("access_token"); exp = res.get("expires_in")
    if not new:
        print(f"refresh returned no token: {res}"); sys.exit(1)
    print(f"refreshed OK — expires_in ~{round((exp or 0)/86400)} days")
    if not pat:
        print("no IG_REFRESH_PAT — cannot update secret (printed only)"); return
    from nacl import encoding, public
    key = gh("actions/secrets/public-key", pat)
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    enc = base64.b64encode(public.SealedBox(pk).encrypt(new.encode())).decode()
    gh("actions/secrets/IG_LOGIN_TOKEN", pat, "PUT",
       {"encrypted_value": enc, "key_id": key["key_id"]})
    print("IG_LOGIN_TOKEN secret updated ✓")


if __name__ == "__main__":
    main()
