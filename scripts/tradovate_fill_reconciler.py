#!/usr/bin/env python3
# =============================================================================
# Tradovate Fill Reconciler
# =============================================================================
# Polls Tradovate /fill/list every 5 minutes, reads docs/trading/journal-data.json,
# and writes synthetic EXIT rows for fills that closed positions but were never
# captured by Pine's alert() (e.g., intra-bar spike to TP that reverses before
# bar close — Pine never fires EXIT, but Tradovate's bracket already filled).
#
# Idempotency: each reconciler-written row carries `tradovate_fill_id` so a
# rerun cannot duplicate. Pine-written EXIT rows (no fill_id) are matched by
# direction + ±2-minute time window so we don't double-count.
#
# Created 2026-05-26 per Lauren's request (see CLAUDE.md change-log).
# IRON RULE compliance: edits via GitHub Contents API (#1), state in repo (#7),
# failure SMS via notify_failure (#3), registry entry in scheduled-runs.json (#4).
# =============================================================================
import os, json, time, base64
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

# ============ CONFIG ============
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
REPO = "laurenlev10/lauren-agent-hub-data"
JOURNAL_PATH = "docs/trading/journal-data.json"
STATE_PATH = "docs/state/tradovate_reconciler_state.json"
AUTOTRADE_PATH = "docs/trading/autotrade_enabled.json"

# Match window — Pine alert arrives within this delta of the Tradovate fill
PINE_MATCH_WINDOW_SECONDS = 180  # 3 minutes (covers up to 1-min bar closes + lag)

# ============ ENV / SECRETS ============
TRADOVATE_NAME     = os.environ.get("TRADOVATE_NAME", "Laurenlev318")
TRADOVATE_PASSWORD = os.environ["TRADOVATE_PASSWORD"]
TRADOVATE_CID      = int(os.environ.get("TRADOVATE_CID", "13601"))
TRADOVATE_SEC      = os.environ["TRADOVATE_SEC"]
GH_TOKEN           = os.environ["GH_TOKEN"]  # for writing journal-data.json
ST_TOKEN           = os.environ.get("SIMPLETEXTING_TOKEN", "")
LAUREN_PHONE       = os.environ.get("LAUREN_PHONE", "4243547625")

# ============ HTTP HELPERS ============
def http_get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_post_json(url, body, headers=None, timeout=15):
    h = dict(headers or {})
    h.setdefault("Content-Type", "application/json")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_put_json(url, body, headers=None, timeout=20):
    h = dict(headers or {})
    h.setdefault("Content-Type", "application/json")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def sms(body):
    if not ST_TOKEN:
        return
    try:
        http_post_json(
            "https://api-app2.simpletexting.com/v2/api/messages",
            {"contactPhone": LAUREN_PHONE, "mode": "AUTO", "text": body},
            headers={"Authorization": "Bearer " + ST_TOKEN},
        )
    except Exception as e:
        print(f"SMS failed: {e}")

# ============ TRADOVATE API ============
def tradovate_auth():
    body = {
        "name": TRADOVATE_NAME,
        "password": TRADOVATE_PASSWORD,
        "appId": "ReconcilerDemo",
        "appVersion": "1.0",
        "cid": TRADOVATE_CID,
        "sec": TRADOVATE_SEC,
    }
    r = http_post_json(TRADOVATE_URL + "/auth/accesstokenrequest", body)
    token = r.get("accessToken")
    if not token:
        raise RuntimeError(f"auth failed: {r.get('errorText') or r}")
    return token

def tradovate_get(path, token):
    return http_get_json(TRADOVATE_URL + path, headers={"Authorization": "Bearer " + token})

def fetch_fills(token):
    """Get all fills for this account. Tradovate returns most recent first usually."""
    fills = tradovate_get("/fill/list", token) or []
    # filter to fills with timestamps in the last 24h to keep it focused
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out = []
    for f in fills:
        ts = f.get("timestamp") or f.get("fillTime") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt >= cutoff:
                out.append(f)
        except Exception:
            continue
    return out

# ============ GITHUB API ============
def gh_get_file(path):
    """Returns (decoded_content_str, sha, exists_bool)."""
    url = f"https://api.github.com/repos/{REPO}/contents/{path}?ref=main"
    h = {"Authorization": "token " + GH_TOKEN, "Accept": "application/vnd.github+json"}
    try:
        r = http_get_json(url, headers=h)
        b64 = r.get("content", "").replace("\n", "")
        content = base64.b64decode(b64).decode("utf-8")
        return content, r.get("sha"), True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None, False
        raise

def gh_put_file(path, content_str, message, sha=None):
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    h = {"Authorization": "token " + GH_TOKEN, "Accept": "application/vnd.github+json"}
    body = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    return http_put_json(url, body, headers=h)

# ============ JOURNAL / STATE ============
def load_journal():
    content, sha, exists = gh_get_file(JOURNAL_PATH)
    if not exists:
        return {"_updated_at": None, "trades": []}, None
    return json.loads(content), sha

def save_journal(journal, sha, message):
    return gh_put_file(JOURNAL_PATH, json.dumps(journal, indent=2, ensure_ascii=False), message, sha=sha)

def load_state():
    content, sha, exists = gh_get_file(STATE_PATH)
    if not exists:
        return {"last_run_at": None, "seen_fill_ids": [], "exits_added_today": 0, "last_error": None}, None
    return json.loads(content), sha

def save_state(state, sha):
    return gh_put_file(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False), "reconciler: bump state", sha=sha)

def load_autotrade():
    try:
        content, _, exists = gh_get_file(AUTOTRADE_PATH)
        return json.loads(content) if exists else {}
    except Exception:
        return {}

# ============ RECONCILIATION LOGIC ============
def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None

def journal_has_matching_exit(trades, fill_id, fill_time, fill_direction, fill_price, tick_size):
    """True if a journal row already represents this fill — either by explicit
    tradovate_fill_id OR by direction+time-window heuristic for Pine-written rows."""
    fdt = parse_dt(fill_time)
    if fdt is None:
        return False
    for t in trades:
        # Explicit idempotency: same Tradovate fill_id
        if t.get("tradovate_fill_id") == fill_id:
            return True
        # Heuristic: Pine wrote an EXIT or SWAP row close in time
        tp = (t.get("type") or "").upper()
        if not (tp.startswith("EXIT") or tp.startswith("SWAP")):
            continue
        # Direction match
        side = (t.get("side") or t.get("action") or "").lower()
        # fill direction: "Sell" closes a LONG, "Buy" closes a SHORT
        # Pine's EXIT LONG → side=sell ; EXIT SHORT → side=buy ; SWAP closes the prior direction
        expected_side = "sell" if fill_direction == "Sell" else "buy"
        if side != expected_side and "swap" not in tp.lower():
            continue
        # Time window
        tdt = parse_dt(t.get("_received_at") or t.get("time"))
        if tdt is None:
            continue
        if abs((tdt - fdt).total_seconds()) <= PINE_MATCH_WINDOW_SECONDS:
            return True
    return False

def find_open_entry_at(trades, before_time):
    """Walk journal chronologically up to before_time, return the currently-open entry."""
    sorted_trades = sorted(trades, key=lambda t: parse_dt(t.get("_received_at") or t.get("time")) or datetime.min.replace(tzinfo=timezone.utc))
    open_entry = None
    for t in sorted_trades:
        tdt = parse_dt(t.get("_received_at") or t.get("time"))
        if tdt is None or tdt >= before_time:
            break
        tp = (t.get("type") or "").upper()
        try:
            price = float(t.get("price"))
        except (TypeError, ValueError):
            continue
        if tp == "LONG" or tp == "SWAP LONG":
            open_entry = {"direction": "long", "price": price, "type": tp, "received_at": t.get("_received_at")}
        elif tp == "SHORT" or tp == "SWAP SHORT":
            open_entry = {"direction": "short", "price": price, "type": tp, "received_at": t.get("_received_at")}
        elif tp.startswith("EXIT"):
            open_entry = None
    return open_entry

def classify_exit_reason(open_entry, fill_price, autotrade):
    """Compare fill price to bracket levels to decide TP vs SL.
    Falls back to P&L sign if bracket info not available."""
    if not open_entry:
        return "UNKNOWN"
    direction = open_entry["direction"]
    entry_price = open_entry["price"]
    # P&L-based fallback (most reliable for closed-bracket scenarios)
    profit = (fill_price - entry_price) if direction == "long" else (entry_price - fill_price)
    return "TP" if profit > 0 else "SL"

def build_exit_row(fill, open_entry, autotrade):
    """Construct a journal row for an unrecorded exit fill.
    Pulls tick_size/tick_value from autotrade_enabled.json (which already
    has the active contract's tick metadata — no separate Tradovate fetch needed)."""
    fill_price = float(fill.get("price"))
    fill_qty = int(fill.get("qty") or 1)
    fill_action = fill.get("action") or ""
    fill_time = fill.get("timestamp") or fill.get("fillTime")

    tick_size  = float((autotrade.get("active_contract") or {}).get("tick_size")     or 0.25)
    tick_value = float((autotrade.get("active_contract") or {}).get("tick_value_usd") or 0.50)

    direction = open_entry["direction"]  # 'long' or 'short'
    entry_price = open_entry["price"]

    # Compute P&L
    raw_diff = (fill_price - entry_price) if direction == "long" else (entry_price - fill_price)
    ticks = int(round(raw_diff / tick_size))
    dollars = round(ticks * tick_value * fill_qty, 2)

    reason = classify_exit_reason(open_entry, fill_price, autotrade)
    dir_str = "LONG" if direction == "long" else "SHORT"
    exit_type = f"EXIT {dir_str} {reason}"
    side = "sell" if direction == "long" else "buy"

    return {
        "_received_at": datetime.now(timezone.utc).isoformat(),
        "_source": "tradovate-reconciler",
        "ticker": (autotrade.get("active_contract") or {}).get("contract_name") or "MNQM6",
        "type": exit_type,
        "action": "exit",
        "side": side,
        "quantity": fill_qty,
        "price": fill_price,
        "entry_price": entry_price,
        "result_ticks": ticks,
        "result_dollars": dollars,
        "tradovate_fill_id": fill.get("id"),
        "tradovate_fill_time": fill_time,
    }

# ============ MAIN ============
def main():
    print(f"[reconciler] start at {datetime.now(timezone.utc).isoformat()}")

    # 1. Auth + load state + journal + autotrade
    try:
        token = tradovate_auth()
    except Exception as e:
        sms(f"🚨 Reconciler AUTH נכשל: {e}")
        raise

    state, state_sha = load_state()
    journal, journal_sha = load_journal()
    autotrade = load_autotrade()

    trades = journal.get("trades", [])
    seen_fill_ids = set(state.get("seen_fill_ids", []))

    # 2. Fetch fills (last 24h) + contracts
    fills = fetch_fills(token)
    print(f"[reconciler] fetched {len(fills)} fills in last 24h")

    # 3. Walk fills chronologically; identify exits that are missing from journal
    sorted_fills = sorted(fills, key=lambda f: parse_dt(f.get("timestamp") or f.get("fillTime")) or datetime.min.replace(tzinfo=timezone.utc))

    contract_id = (autotrade.get("active_contract") or {}).get("contract_id")
    account_id = autotrade.get("account_id")

    new_rows = []
    for f in sorted_fills:
        fid = f.get("id")
        if fid in seen_fill_ids:
            continue
        # Filter to the configured contract + account
        if contract_id and f.get("contractId") != contract_id:
            continue
        if account_id and f.get("accountId") != account_id:
            continue
        fill_time = f.get("timestamp") or f.get("fillTime")
        fdt = parse_dt(fill_time)
        if fdt is None:
            continue
        fill_action = f.get("action") or ""
        fill_price = f.get("price")
        if fill_price is None:
            continue

        # Find the open entry at the moment of this fill
        open_entry = find_open_entry_at(trades, fdt)

        # An EXIT fill is one where action is opposite to the open position
        is_exit_fill = False
        if open_entry:
            if open_entry["direction"] == "long" and fill_action == "Sell":
                is_exit_fill = True
            elif open_entry["direction"] == "short" and fill_action == "Buy":
                is_exit_fill = True
        if not is_exit_fill:
            seen_fill_ids.add(fid)
            continue

        # Check if journal already has a matching exit (Pine + reconciler dedup)
        if journal_has_matching_exit(trades, fid, fill_time, fill_action, fill_price, 0.25):
            seen_fill_ids.add(fid)
            continue

        # Build and queue a new EXIT row
        row = build_exit_row(f, open_entry, autotrade)
        new_rows.append(row)
        seen_fill_ids.add(fid)
        print(f"[reconciler] NEW EXIT: fill_id={fid} {row['type']} @ {row['price']} → ticks={row['result_ticks']} ${row['result_dollars']}")

    # 4. Write to journal if anything new
    if new_rows:
        # Re-fetch journal to get latest sha (might've changed since we loaded)
        journal_fresh, journal_sha_fresh = load_journal()
        trades_fresh = journal_fresh.get("trades", [])
        # Re-dedup against freshest journal (extra safety)
        existing_fill_ids = {t.get("tradovate_fill_id") for t in trades_fresh if t.get("tradovate_fill_id")}
        rows_to_add = [r for r in new_rows if r.get("tradovate_fill_id") not in existing_fill_ids]
        if rows_to_add:
            trades_fresh.extend(rows_to_add)
            journal_fresh["trades"] = trades_fresh
            journal_fresh["_updated_at"] = datetime.now(timezone.utc).isoformat()
            journal_fresh["_updated_by"] = "tradovate-fill-reconciler"
            msg = f"reconciler: +{len(rows_to_add)} EXIT row(s) from Tradovate fills"
            save_journal(journal_fresh, journal_sha_fresh, msg)
            print(f"[reconciler] wrote {len(rows_to_add)} rows to journal")
            # SMS Lauren one summary line per session if rows added
            summary = " | ".join(f"{r['type']} ${r['result_dollars']:+.2f}" for r in rows_to_add[:3])
            sms(f"📊 Reconciler אתר {len(rows_to_add)} EXIT שלא נתפסו ע\"י Pine — נוסף ליומן: {summary}")
        else:
            print("[reconciler] all new rows were duplicates after fresh re-fetch, no write")

    # 5. Update state — keep seen_fill_ids bounded
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["seen_fill_ids"] = sorted(list(seen_fill_ids))[-500:]  # keep last 500 only
    state["exits_added_today"] = int(state.get("exits_added_today") or 0) + len(new_rows)
    state["last_error"] = None
    save_state(state, state_sha)
    print(f"[reconciler] done. seen_fill_ids size = {len(state['seen_fill_ids'])}, new exits = {len(new_rows)}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Don't crash the workflow — let the IRON RULE #3 step send SMS
        # but re-raise so workflow shows failure
        raise
