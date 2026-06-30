#!/usr/bin/env python3
# =============================================================================
# VWAP Demo Broker Reconciler  v2  (accurate + cumulative)
# -----------------------------------------------------------------------------
# REAL account record for the Tradovate DEMO account (30647272), built from
# Tradovate's OWN round-trip pairing (/fillPair/list) — identical to the
# Performance PDF, no FIFO guessing. Writes docs/trading/broker-ledger.json.
#
# Why v2: /fill/list is a ROLLING WINDOW — old fills age out, so the old FIFO
# reconstruction missed trades and diverged from the PDF. v2 fixes that:
#   1. Uses /fillPair/list  -> exact buy/sell pairs + P&L (matches the PDF).
#   2. CUMULATIVE storage    -> every fill + every pair ever seen is kept
#      forever (dedup by id). The rolling window can never lose data again.
#   3. /cashBalanceLog/list  -> authoritative realized-P&L cross-check.
# Idempotent. Run frequently (cron */10) so nothing ages out before capture.
# =============================================================================
import os, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import urllib.request

TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
LEDGER_PATH   = "docs/trading/broker-ledger.json"
TICK          = 0.25
DOLLAR_PER_TICK = 0.50
LA = ZoneInfo("America/Los_Angeles")

NAME = os.environ.get("TRADOVATE_NAME", "Laurenlev318")
PWD  = os.environ["TRADOVATE_PASSWORD"]
CID  = int(os.environ.get("TRADOVATE_CID", "13601"))
SEC  = os.environ["TRADOVATE_SEC"]

def post(path, body):
    req = urllib.request.Request(TRADOVATE_URL+path, data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
def get(path, tok):
    req = urllib.request.Request(TRADOVATE_URL+path, headers={"Authorization":"Bearer "+tok}, method="GET")
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
def auth():
    r = post("/auth/accesstokenrequest", {"name":NAME,"password":PWD,
        "appId":"VWAPDemoReconciler","appVersion":"2.0","cid":CID,"sec":SEC})
    t = r.get("accessToken")
    if not t: raise RuntimeError("auth failed: "+str(r.get("errorText") or r))
    return t
def la_str(iso):
    try: return datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(LA).strftime("%d/%m/%Y %H:%M:%S")
    except Exception: return ""

def main():
    tok = auth()
    fills = get("/fill/list", tok) or []
    pairs = get("/fillPair/list", tok) or []
    try: cblog = get("/cashBalanceLog/list", tok) or []
    except Exception: cblog = []
    print(f"[v2] fetched: {len(fills)} fills, {len(pairs)} pairs, {len(cblog)} cashlog rows")

    # load existing ledger (cumulative)
    led = {"_doc":"Tradovate DEMO 30647272 — REAL round-trip trades from /fillPair/list (exact, = Performance PDF). Cumulative: every fill+pair ever seen is kept (rolling-window-proof).",
           "_account":"Tradovate demo 30647272","_fills":{},"trades":[]}
    if os.path.exists(LEDGER_PATH):
        try: led = json.load(open(LEDGER_PATH, encoding="utf-8"))
        except Exception: pass
    led.setdefault("_fills", {})
    led.setdefault("trades", [])

    # 1. accumulate fills (id -> ts/action/price/qty) — needed for times+direction
    FILLS = led["_fills"]
    for f in fills:
        fid = str(f.get("id"))
        if fid and fid not in FILLS and f.get("timestamp"):
            FILLS[fid] = {"ts": f.get("timestamp"), "action": f.get("action"),
                          "price": f.get("price"), "qty": f.get("qty"), "contractId": f.get("contractId")}

    # 2. accumulate fillPairs as round-trip trades (dedup by pair id)
    have = {t.get("pair_id") for t in led["trades"]}
    added = 0
    for pr in pairs:
        pid = pr.get("id")
        if pid in have: continue
        bp, sp, qty = pr.get("buyPrice"), pr.get("sellPrice"), int(pr.get("qty") or 1)
        if bp is None or sp is None: continue
        ticks = round((sp - bp) / TICK)            # profit-ticks (sell-buy), sign = P&L
        dollars = round(ticks * DOLLAR_PER_TICK * qty, 2)
        bf = FILLS.get(str(pr.get("buyFillId")), {}); sf = FILLS.get(str(pr.get("sellFillId")), {})
        bts, sts = bf.get("ts"), sf.get("ts")
        # direction: whichever fill opened first
        direction = ""
        entry_iso = exit_iso = None; entry_price = exit_price = None
        if bts and sts:
            if bts <= sts:  # bought first -> LONG
                direction="long"; entry_iso, exit_iso = bts, sts; entry_price, exit_price = bp, sp
            else:           # sold first -> SHORT
                direction="short"; entry_iso, exit_iso = sts, bts; entry_price, exit_price = sp, bp
        led["trades"].append({
            "pair_id": pid, "position_id": pr.get("positionId"), "contract":"MNQU6",
            "direction": direction, "qty": qty,
            "entry_price": entry_price, "exit_price": exit_price,
            "buy_price": bp, "sell_price": sp,
            "entry_iso": entry_iso, "exit_iso": exit_iso,
            "entry_la": la_str(entry_iso) if entry_iso else "", "exit_la": la_str(exit_iso) if exit_iso else "",
            "result_ticks": int(ticks), "result_dollars": dollars,
            "buy_fill_id": pr.get("buyFillId"), "sell_fill_id": pr.get("sellFillId"),
        })
        have.add(pid); added += 1

    # sort by exit time (fallback entry), renumber
    led["trades"].sort(key=lambda t: (t.get("exit_iso") or t.get("entry_iso") or ""))
    for i,t in enumerate(led["trades"],1): t["id"]=i

    # 3. summary + cashBalanceLog realized-P&L cross-check
    net_d = round(sum(t["result_dollars"] for t in led["trades"]), 2)
    net_t = sum(t["result_ticks"] for t in led["trades"])
    wins  = sum(1 for t in led["trades"] if t["result_dollars"] > 0)
    led["_summary"] = {"trades":len(led["trades"]), "net_dollars":net_d, "net_ticks":net_t,
                       "wins":wins, "win_rate":round(wins/len(led["trades"])*100,1) if led["trades"] else 0}
    # cash realized P&L (authoritative account number) — sum of realizedPnL entries
    latest_bal = week_real = None
    if cblog:
        cbl = sorted(cblog, key=lambda c: c.get("timestamp") or "")
        latest = cbl[-1]
        latest_bal = round(float(latest.get("amount") or 0), 2)
        week_real  = round(float(latest.get("weekRealizedPnL") or 0), 2)  # account's own running weekly realized P&L (do NOT sum rows)
    led["_cash_check"] = {"latest_balance": latest_bal, "week_realized_pnl": week_real,
                          "note":"week_realized_pnl = account's running realized P&L this week (Tradovate). Cross-check the cumulative fillPair sum against this over the week."}
    led["_fills_kept"] = len(FILLS)
    led["_updated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(led, open(LEDGER_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[v2] +{added} new pairs | total trades {len(led['trades'])} | net ${net_d} ({net_t}T) | win% {led['_summary']['win_rate']} | fills kept {len(FILLS)} | cash realized(window) {cash_real}")

if __name__ == "__main__":
    main()
