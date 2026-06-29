#!/usr/bin/env python3
# =============================================================================
# VWAP Demo Broker Reconciler  (hourly)
# -----------------------------------------------------------------------------
# Pulls REAL fills from the Tradovate DEMO account (30647272), reconstructs
# completed round-trip trades via FIFO, and writes them to
#   docs/trading/broker-ledger.json   (the "real account" ledger)
#
# This is the REAL execution truth (combined / netted account). Per-strategy
# attribution lives in journal-data.json (from the indicator alert webhooks);
# this file is the account-level reality check. Kept in a SEPARATE file so it
# never conflicts with the journal-ingest Worker's writes.
#
# Idempotent: every trade keyed by (entry_fill_id, exit_fill_id) -> reruns
# never duplicate. Written 2026-06-29 per Lauren's request.
# =============================================================================
import os, json, sys
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
    req = urllib.request.Request(TRADOVATE_URL+path,
        headers={"Authorization":"Bearer "+tok}, method="GET")
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())

def auth():
    r = post("/auth/accesstokenrequest", {"name":NAME,"password":PWD,
        "appId":"VWAPDemoReconciler","appVersion":"1.0","cid":CID,"sec":SEC})
    t = r.get("accessToken")
    if not t: raise RuntimeError("auth failed: "+str(r.get("errorText") or r))
    return t

def la_str(iso):
    try:
        d = datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(LA)
        return d.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return ""

def reconstruct(fills):
    """FIFO round-trip reconstruction per contract. Returns list of completed trades."""
    by_c = {}
    for f in fills:
        if f.get("price") is None or not f.get("timestamp"): continue
        by_c.setdefault(f.get("contractId"), []).append(f)
    trades = []
    for cid, fs in by_c.items():
        fs.sort(key=lambda f: f.get("timestamp"))
        lots = []   # open lots: dict(dir, price, ts, fid) each qty 1
        for f in fs:
            qty = int(f.get("qty") or 1)
            price = float(f.get("price"))
            ts = f.get("timestamp")
            fid = f.get("id")
            s = 1 if (f.get("action")=="Buy") else -1
            for _ in range(qty):
                if lots and ((lots[0]["dir"]>0) != (s>0)):
                    # opposite -> closes the oldest lot (one contract)
                    lot = lots.pop(0)
                    d = lot["dir"]
                    ticks = round((price - lot["price"]) * d / TICK)
                    trades.append({
                        "key": f"{lot['fid']}-{fid}",
                        "contract": cid,
                        "direction": "long" if d>0 else "short",
                        "qty": 1,
                        "entry_price": lot["price"], "exit_price": price,
                        "entry_iso": lot["ts"], "exit_iso": ts,
                        "entry_la": la_str(lot["ts"]), "exit_la": la_str(ts),
                        "result_ticks": int(ticks),
                        "result_dollars": round(ticks * DOLLAR_PER_TICK, 2),
                        "entry_fill_id": lot["fid"], "exit_fill_id": fid,
                    })
                else:
                    lots.append({"dir": s, "price": price, "ts": ts, "fid": fid})
        # remaining lots = currently open (not a completed trade)
    return trades, by_c

def main():
    tok = auth()
    fills = get("/fill/list", tok) or []
    print(f"[demo-reconciler] {len(fills)} fills fetched")
    new_trades, _ = reconstruct(fills)

    # load existing ledger
    led = {"_doc":"Tradovate DEMO 30647272 real round-trip trades (FIFO). Account-level reality check; per-strategy lives in journal-data.json.",
           "_account":"Tradovate demo 30647272","trades":[]}
    if os.path.exists(LEDGER_PATH):
        try: led = json.load(open(LEDGER_PATH, encoding="utf-8"))
        except Exception: pass
    led.setdefault("trades", [])
    have = {t.get("key") for t in led["trades"]}
    added = 0
    for t in new_trades:
        if t["key"] not in have:
            led["trades"].append(t); have.add(t["key"]); added += 1
    led["trades"].sort(key=lambda t: t.get("exit_iso") or "")
    # renumber + summary
    for i, t in enumerate(led["trades"], 1): t["id"] = i
    net_ticks = sum(t["result_ticks"] for t in led["trades"])
    net_dol   = round(sum(t["result_dollars"] for t in led["trades"]), 2)
    wins = sum(1 for t in led["trades"] if t["result_dollars"] > 0)
    led["_summary"] = {"trades": len(led["trades"]), "net_ticks": net_ticks,
                       "net_dollars": net_dol, "wins": wins,
                       "win_rate": round(wins/len(led["trades"])*100,1) if led["trades"] else 0}
    led["_updated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(led, open(LEDGER_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[demo-reconciler] +{added} new | total {len(led['trades'])} | net ${net_dol} ({net_ticks}T) | win% {led['_summary']['win_rate']}")

if __name__ == "__main__":
    main()
