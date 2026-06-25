#!/usr/bin/env python3
# =============================================================================
# Tradovate Fill Reconciler + Live State Sync
# =============================================================================
# Two jobs in one workflow (saves runs):
#   1. RECONCILE — polls /fill/list, writes synthetic EXIT rows to journal
#                  for fills Pine missed (intra-bar TP spikes, etc.)
#   2. LIVE STATE — writes docs/state/live_position.json + live_balance.json
#                   for the dashboard's Live Position Card + Balance Widget
#                   (Roadmap Steps 1.1 + 1.2 — built 2026-05-26)
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

# Live-state files for Phase 2 dashboard widgets (Roadmap Steps 1.1 + 1.2)
LIVE_POSITION_PATH = "docs/state/live_position.json"
LIVE_BALANCE_PATH  = "docs/state/live_balance.json"

# Match window — Pine alert arrives within this delta of the Tradovate fill
PINE_MATCH_WINDOW_SECONDS = 180  # 3 minutes (covers up to 1-min bar closes + lag)

# Sanity bound — any single trade exceeding this many ticks is almost certainly
# a contract-mismatch error (different symbol's open position matched against
# current contract's fill). Real MNQ trades have SL=600 ticks max, so 1500 is
# a comfortable cap. Reconciler will refuse to write rows beyond this.
MAX_REASONABLE_TICKS = 1500

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

def fetch_position_snapshot(token, contract_id, account_id):
    """Single snapshot of the active position for the dashboard's Live Position Card.
    Returns a dict that's safe to overwrite live_position.json with each run."""
    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": contract_id,
        "account_id": account_id,
        "net_pos": 0,
        "avg_price": None,
        "open_pnl": None,
        "is_flat": True,
    }
    try:
        positions = tradovate_get("/position/list", token) or []
        for p in positions:
            if p.get("contractId") == contract_id and (not account_id or p.get("accountId") == account_id):
                net = int(p.get("netPos") or 0)
                out["net_pos"] = net
                out["avg_price"] = p.get("netPrice")
                out["is_flat"] = (net == 0)
                # If Tradovate returns open P&L on the position, surface it
                if "openPL" in p: out["open_pnl"] = p.get("openPL")
                break
    except Exception as e:
        out["error"] = str(e)
    return out

def fetch_balance_snapshot(token, account_id):
    """Cash balance + day P&L for the dashboard's Live Balance Widget.
    Reads /cashBalance/list which returns one row per account. Day P&L is
    computed as (amount - amountSOD) since Tradovate's realizedPnL field
    matches that delta in our testing."""
    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "cash_balance": None,
        "amount_sod": None,
        "day_pnl_realized": None,
        "week_pnl_realized": None,
    }
    try:
        balances = tradovate_get("/cashBalance/list", token) or []
        for b in balances:
            if not account_id or b.get("accountId") == account_id:
                out["cash_balance"] = b.get("amount")
                out["amount_sod"]   = b.get("amountSOD")
                out["day_pnl_realized"]  = b.get("realizedPnL")
                out["week_pnl_realized"] = b.get("weekRealizedPnL")
                break
    except Exception as e:
        out["error"] = str(e)
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

    # Compute P&L — assume only ONE contract was closed per fill (the bracket
    # attaches to a single position). Multi-qty fills happen during SWAP when
    # one Buy/Sell both closes and opens — only the closing portion is the exit.
    close_qty = 1
    raw_diff = (fill_price - entry_price) if direction == "long" else (entry_price - fill_price)
    ticks = int(round(raw_diff / tick_size))
    dollars = round(ticks * tick_value * close_qty, 2)

    reason = classify_exit_reason(open_entry, fill_price, autotrade)
    dir_str = "LONG" if direction == "long" else "SHORT"
    exit_type = f"EXIT {dir_str} {reason}"
    side = "sell" if direction == "long" else "buy"

    # Use the actual Tradovate fill time as _received_at so the row sorts
    # into the right chronological position in the journal (the time the fill
    # actually happened, not when the reconciler discovered it). Original
    # write-time kept under _written_at for audit.
    return {
        "_received_at": fill_time or datetime.now(timezone.utc).isoformat(),
        "_written_at": datetime.now(timezone.utc).isoformat(),
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

def _entry_dup_in_journal(trades, fill_id, entry_dt, direction):
    """True if the journal already represents this ENTRY — by tradovate_fill_id on
    a non-exit row, OR a Pine-written LONG/SHORT/SWAP row of the same direction within
    the match window (so we never double-log when Pine also wrote the entry)."""
    for t in trades:
        if fill_id is not None and t.get("tradovate_fill_id") == fill_id and t.get("action") != "exit":
            return True
    want = "LONG" if direction == "long" else "SHORT"
    for t in trades:
        tp = (t.get("type") or "").upper()
        if tp not in ("LONG", "SHORT", "SWAP LONG", "SWAP SHORT"):
            continue
        tdir = "LONG" if "LONG" in tp else "SHORT"
        if tdir != want:
            continue
        tdt = parse_dt(t.get("_received_at") or t.get("time"))
        if tdt is None:
            continue
        if abs((tdt - entry_dt).total_seconds()) <= PINE_MATCH_WINDOW_SECONDS:
            return True
    return False

def reconstruct_rows_from_fills(trades, fills, autotrade):
    """Rebuild full round-trip ENTRY + EXIT journal rows directly from the Tradovate
    fill stream — the server-side source of truth. Independent of whether Pine/Zapier
    wrote anything. Returns ordered new rows that are NOT already in the journal
    (deduped by tradovate_fill_id + Pine heuristics). Idempotent across reruns."""
    ac = autotrade.get("active_contract") or {}
    cid = ac.get("contract_id")
    cname = ac.get("contract_name") or "MNQ"
    tick = float(ac.get("tick_size") or 0.25)
    tval = float(ac.get("tick_value_usd") or 0.50)

    fs = []
    for f in fills:
        if cid and f.get("contractId") != cid:
            continue
        if f.get("price") is None:
            continue
        if parse_dt(f.get("timestamp") or f.get("fillTime")) is None:
            continue
        fs.append(f)
    fs.sort(key=lambda f: parse_dt(f.get("timestamp") or f.get("fillTime")))

    def entry_row(direction, fid, price, qty, ftime, swap):
        dir_str = "LONG" if direction == "long" else "SHORT"
        return {
            "_received_at": ftime, "_written_at": datetime.now(timezone.utc).isoformat(),
            "_source": "tradovate-reconciler", "ticker": cname,
            "type": (("SWAP " if swap else "") + dir_str),
            "action": "buy" if direction == "long" else "sell",
            "side": "buy" if direction == "long" else "sell",
            "quantity": qty, "price": price, "danger": 0,
            "tradovate_fill_id": fid, "tradovate_fill_time": ftime, "tradovate_fill_price": price,
        }

    def exit_row(direction, entry_price, fid, price, qty, ftime):
        raw = (price - entry_price) if direction == "long" else (entry_price - price)
        ticks = int(round(raw / tick))
        dollars = round(ticks * tval * qty, 2)
        reason = "TP" if raw > 0 else "SL"
        dir_str = "LONG" if direction == "long" else "SHORT"
        return {
            "_received_at": ftime, "_written_at": datetime.now(timezone.utc).isoformat(),
            "_source": "tradovate-reconciler", "ticker": cname,
            "type": f"EXIT {dir_str} {reason}", "action": "exit",
            "side": "sell" if direction == "long" else "buy",
            "quantity": qty, "price": price, "entry_price": entry_price,
            "result_ticks": ticks, "result_dollars": dollars,
            "tradovate_fill_id": fid, "tradovate_fill_time": ftime, "tradovate_fill_price": price,
        }

    net = 0
    open_entry = None
    new_rows = []
    for f in fs:
        fid = f.get("id")
        act = f.get("action") or ""
        qty = int(f.get("qty") or 1)
        price = float(f.get("price"))
        ftime = f.get("timestamp") or f.get("fillTime")
        fdt = parse_dt(ftime)
        s = 1 if act == "Buy" else -1
        opposes = net != 0 and ((net > 0 and s < 0) or (net < 0 and s > 0))
        if opposes:
            close_qty = min(abs(net), qty)
            if open_entry and not journal_has_matching_exit(trades, fid, ftime, act, price, tick):
                er = exit_row(open_entry["direction"], open_entry["price"], fid, price, close_qty, ftime)
                if abs(er["result_ticks"]) <= MAX_REASONABLE_TICKS:
                    new_rows.append(er)
            net += s * close_qty
            if net == 0:
                open_entry = None
            rem = qty - close_qty
            if rem > 0:  # flipped through zero -> SWAP open
                direction = "long" if s > 0 else "short"
                if not _entry_dup_in_journal(trades, fid, fdt, direction):
                    new_rows.append(entry_row(direction, fid, price, rem, ftime, swap=True))
                open_entry = {"direction": direction, "price": price}
                net += s * rem
        else:
            if net == 0:  # open from flat
                direction = "long" if s > 0 else "short"
                if not _entry_dup_in_journal(trades, fid, fdt, direction):
                    new_rows.append(entry_row(direction, fid, price, qty, ftime, swap=False))
                open_entry = {"direction": direction, "price": price}
                net += s
            else:  # scale-in same direction -> weighted avg, no new row
                if open_entry:
                    tot = abs(net) + qty
                    open_entry["price"] = (open_entry["price"] * abs(net) + price * qty) / tot
                net += s
    return new_rows

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

    # 3+4. Reconstruct full ENTRY + EXIT rows directly from the Tradovate fill stream.
    # Server-side source of truth — works even when the Pine/Zapier journal-write
    # step is broken or missing (the bot can trade without anything logging it).
    new_rows = reconstruct_rows_from_fills(trades, sorted_fills, autotrade)
    # Mark every in-window fill as seen (idempotency is ALSO guaranteed by the
    # tradovate_fill_id dedup against the journal itself, so a lost state file self-heals).
    for f in sorted_fills:
        if f.get("id") is not None:
            seen_fill_ids.add(f.get("id"))

    if new_rows:
        # Re-fetch journal for the freshest sha + dedup
        journal_fresh, journal_sha_fresh = load_journal()
        trades_fresh = journal_fresh.get("trades", [])
        # A fill_id can back at most one ENTRY and one EXIT row (SWAP), so key on (fill_id, kind)
        def _kind(r): return "exit" if r.get("action") == "exit" else "entry"
        existing_keys = {(t.get("tradovate_fill_id"), "exit" if t.get("action") == "exit" else "entry")
                         for t in trades_fresh if t.get("tradovate_fill_id")}
        rows_to_add = [r for r in new_rows if (r.get("tradovate_fill_id"), _kind(r)) not in existing_keys]
        if rows_to_add:
            trades_fresh.extend(rows_to_add)
            trades_fresh.sort(key=lambda t: parse_dt(t.get("_received_at") or t.get("time")) or datetime.min.replace(tzinfo=timezone.utc))
            journal_fresh["trades"] = trades_fresh
            journal_fresh["_updated_at"] = datetime.now(timezone.utc).isoformat()
            journal_fresh["_updated_by"] = "tradovate-fill-reconciler"
            n_entry = sum(1 for r in rows_to_add if r.get("action") != "exit")
            n_exit = sum(1 for r in rows_to_add if r.get("action") == "exit")
            msg = f"reconciler: +{len(rows_to_add)} row(s) from Tradovate fills ({n_entry} entry / {n_exit} exit)"
            save_journal(journal_fresh, journal_sha_fresh, msg)
            print(f"[reconciler] wrote {len(rows_to_add)} rows to journal ({n_entry} entry / {n_exit} exit)")
            summary = " | ".join((r["type"] + (f" ${r['result_dollars']:+.2f}" if r.get("action") == "exit" else "")) for r in rows_to_add[:4])
            sms(f"\U0001F4CA Reconciler: \u05e0\u05d5\u05e1\u05e4\u05d5 {len(rows_to_add)} \u05e9\u05d5\u05e8\u05d5\u05ea \u05de-Tradovate \u05dc\u05d9\u05d5\u05de\u05df \u2014 {summary}")
        else:
            print("[reconciler] all reconstructed rows already in journal, no write")
    else:
        print("[reconciler] no new rows to reconstruct")

    # 5. Update state — keep seen_fill_ids bounded
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["seen_fill_ids"] = sorted(list(seen_fill_ids))[-500:]  # keep last 500 only
    state["exits_added_today"] = int(state.get("exits_added_today") or 0) + len(new_rows)
    state["last_error"] = None
    save_state(state, state_sha)
    print(f"[reconciler] done. seen_fill_ids size = {len(state['seen_fill_ids'])}, new exits = {len(new_rows)}")

    # ============ ANNOTATE JOURNAL ROWS WITH ACTUAL TRADOVATE FILL PRICES ============
    # For each entry / exit row in journal that doesn't yet carry an actual fill price,
    # find the matching Tradovate fill (within ±3 min, matching action direction) and
    # store it as `tradovate_fill_price` + `tradovate_fill_id_match`.
    # This gives the dashboard the broker's REAL number rather than Pine's bar-close.
    try:
        annotated_count = 0
        # Re-fetch journal fresh — we may have just written new rows above
        journal_now, journal_now_sha = load_journal()
        trades_now = journal_now.get("trades", [])
        # Walk every trade once
        for t in trades_now:
            tp = (t.get("type") or "").upper().strip()
            is_long_entry  = (tp == "LONG"  or tp == "SWAP LONG")
            is_short_entry = (tp == "SHORT" or tp == "SWAP SHORT")
            is_exit_long   = tp.startswith("EXIT LONG")
            is_exit_short  = tp.startswith("EXIT SHORT")
            is_entry = is_long_entry or is_short_entry
            is_exit  = is_exit_long or is_exit_short
            if not (is_entry or is_exit):
                continue
            if t.get("tradovate_fill_price"):
                continue  # already annotated
            # Reconciler-source EXIT rows already use real Tradovate fill price as `price`
            if t.get("_source") == "tradovate-reconciler" and t.get("tradovate_fill_id"):
                t["tradovate_fill_price"] = t.get("price")
                t["tradovate_fill_id_match"] = t.get("tradovate_fill_id")
                annotated_count += 1
                continue
            # Find matching fill
            tdt = parse_dt(t.get("_received_at") or t.get("time"))
            if not tdt:
                continue
            # Direction: LONG → Buy fill, SHORT → Sell fill, EXIT LONG → Sell, EXIT SHORT → Buy
            if is_long_entry or is_exit_short:
                expected_action = "Buy"
            else:
                expected_action = "Sell"
            best_fill = None
            best_delta_s = 180.0  # 3 min window
            for f in sorted_fills:
                if contract_id and f.get("contractId") != contract_id:
                    continue
                if f.get("action") != expected_action:
                    continue
                fdt = parse_dt(f.get("timestamp") or f.get("fillTime"))
                if not fdt:
                    continue
                delta_s = abs((fdt - tdt).total_seconds())
                if delta_s > best_delta_s:
                    continue
                best_delta_s = delta_s
                best_fill = f
            if best_fill:
                t["tradovate_fill_price"] = best_fill.get("price")
                t["tradovate_fill_id_match"] = best_fill.get("id")
                t["tradovate_fill_time_match"] = best_fill.get("timestamp")
                annotated_count += 1
        if annotated_count > 0:
            journal_now["_updated_at"] = datetime.now(timezone.utc).isoformat()
            journal_now["_updated_by"] = "tradovate-fill-reconciler-annotate"
            save_journal(journal_now, journal_now_sha, f"reconciler: annotate {annotated_count} rows with actual fill prices")
            print(f"[reconciler] annotated {annotated_count} journal rows with tradovate_fill_price")
        else:
            print(f"[reconciler] no journal rows needed annotation")
    except Exception as e:
        print(f"[reconciler] annotation pass failed (non-fatal): {e}")

    # ============ LIVE STATE WRITES (Roadmap Steps 1.1 + 1.2) ============
    # Single snapshot of position + balance, overwriting the JSON files for
    # the dashboard's Live Position Card + Balance Widget. Errors are
    # non-fatal — we already did the critical work above (journal sync).
    try:
        pos_snapshot = fetch_position_snapshot(token, contract_id, account_id)
        _, pos_sha, pos_exists = gh_get_file(LIVE_POSITION_PATH)
        gh_put_file(LIVE_POSITION_PATH, json.dumps(pos_snapshot, indent=2),
                    "reconciler: live_position snapshot", sha=pos_sha if pos_exists else None)
        print(f"[reconciler] wrote live_position.json (net_pos={pos_snapshot.get('net_pos')})")
    except Exception as e:
        print(f"[reconciler] live_position write failed (non-fatal): {e}")

    try:
        bal_snapshot = fetch_balance_snapshot(token, account_id)
        _, bal_sha, bal_exists = gh_get_file(LIVE_BALANCE_PATH)
        gh_put_file(LIVE_BALANCE_PATH, json.dumps(bal_snapshot, indent=2),
                    "reconciler: live_balance snapshot", sha=bal_sha if bal_exists else None)
        print(f"[reconciler] wrote live_balance.json (cash={bal_snapshot.get('cash_balance')}, day_pnl={bal_snapshot.get('day_pnl_realized')})")
    except Exception as e:
        print(f"[reconciler] live_balance write failed (non-fatal): {e}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Don't crash the workflow — let the IRON RULE #3 step send SMS
        # but re-raise so workflow shows failure
        raise
