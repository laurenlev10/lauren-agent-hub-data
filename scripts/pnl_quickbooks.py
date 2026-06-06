#!/usr/bin/env python3
"""pnl_quickbooks.py — QuickBooks expenses-by-Class source for the automated event P&L.

Source E of 5 (event_summary_BUILD_BRIEF.md). Pulls non-cash expenses (travel/venue/
ULINE/Lyft/other) tagged with the event's Class. All cash expenses come from the manager
report (decided 2026-06-03) — QB is non-cash only.

Auth: production OAuth tokens in .claude/secrets/. 🛑 QBO ROTATES the refresh token on
every refresh — ALWAYS persist the new one or the chain dies.
"""
from __future__ import annotations
import base64, datetime as dt, json, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

def _secdir():
    for c in Path("/sessions").glob("*/mnt/Claude/.claude/secrets"):
        return c
    return Path.home()/".claude/secrets"

SEC = _secdir()
API = "https://quickbooks.api.intuit.com"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

def _read(n): return (SEC/n).read_text().strip()
def _write(n,v): (SEC/n).write_text(v)

def refresh_access_token():
    cid,csec = _read("qb_client_id_prod.txt"), _read("qb_client_secret_prod.txt")
    rt = _read("qb_refresh_token.txt")
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":rt}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
        headers={"Authorization":"Basic "+basic,"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    _write("qb_access_token.txt", tok["access_token"])
    if tok.get("refresh_token"):
        _write("qb_refresh_token.txt", tok["refresh_token"])
    return tok["access_token"]

def _get(path, params=None, _retry=True):
    at = _read("qb_access_token.txt")
    url = API+path+("?"+urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization":"Bearer "+at, "Accept":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code==401 and _retry:
            refresh_access_token()
            return _get(path, params, _retry=False)
        raise

def query(q):
    realm = _read("qb_realm_id.txt")
    return _get(f"/v3/company/{realm}/query", {"query": q, "minorversion": "70"})

def list_classes():
    r = query("select * from Class maxresults 200")
    return [(c["Id"], c["Name"]) for c in r.get("QueryResponse",{}).get("Class",[]) if c.get("Active",True)]

def _classify(account, vendor, memo=""):
    a=(account or "").lower(); v=(vendor or "").lower(); m=(memo or "").lower()
    hay=a+" "+v+" "+m
    if "uline" in hay: return "uline"
    if "lyft" in hay or "uber " in hay or v=="uber": return "lyft"
    if any(k in a for k in ("airfare","flight")): return "travel"
    if any(k in a for k in ("accommodation","hotel","lodging")): return "travel"
    if any(k in a for k in ("transportation","car rental","rental car","travel")): return "travel"
    if "venue" in a or "rent" in a: return "venue"
    if "meal" in a or "food" in a: return "meals_noncash"
    return "other_qb"

def fetch_qb_expenses(class_name, date_from=None, date_to=None):
    today = dt.date.today()
    date_from = date_from or (today - dt.timedelta(days=120)).isoformat()
    date_to = date_to or today.isoformat()
    lines_out=[]
    for entity in ("Purchase","Bill"):
        start=1
        while True:
            q=f"select * from {entity} where TxnDate >= '{date_from}' and TxnDate <= '{date_to}' startposition {start} maxresults 200"
            r=query(q).get("QueryResponse",{})
            txns=r.get(entity,[]) or []
            for t in txns:
                vendor=(t.get("EntityRef") or t.get("VendorRef") or {}).get("name","")
                for ln in t.get("Line",[]):
                    det=ln.get("AccountBasedExpenseLineDetail") or ln.get("ItemBasedExpenseLineDetail") or {}
                    cref=(det.get("ClassRef") or {}).get("name","")
                    if cref != class_name: continue
                    acct=(det.get("AccountRef") or {}).get("name","")
                    amt=float(ln.get("Amount") or 0)
                    cat=_classify(acct, vendor, ln.get("Description",""))
                    lines_out.append({"date":t.get("TxnDate"),"vendor":vendor,"account":acct,
                                      "amount":round(amt,2),"category":cat,
                                      "memo":(ln.get("Description") or "")[:80],"txn":entity})
            if len(txns)<200: break
            start+=200
    cats={}
    for l in lines_out:
        cats[l["category"]]=round(cats.get(l["category"],0)+l["amount"],2)
    return {"source":"quickbooks","class":class_name,"window":[date_from,date_to],
            "lines":sorted(lines_out,key=lambda x:x["date"] or ""),
            "by_category":cats,"total":round(sum(l["amount"] for l in lines_out),2)}

if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--class-name"); ap.add_argument("--list-classes",action="store_true")
    ap.add_argument("--json",action="store_true")
    a=ap.parse_args()
    if a.list_classes:
        for cid,nm in list_classes(): print(f"  {cid}: {nm}")
        sys.exit(0)
    d=fetch_qb_expenses(a.class_name)
    if a.json: print(json.dumps(d,indent=2,ensure_ascii=False)); sys.exit(0)
    print(f"\n=== QB expenses for Class '{a.class_name}' ({d['window'][0]}..{d['window'][1]}) ===")
    print(f"  total: ${d['total']:,.2f} · by category: {d['by_category']}")
    for l in d["lines"]:
        print(f"   {l['date']} {l['category']:13} ${l['amount']:>9,.2f}  {l['vendor'][:24]:24} [{l['account'][:30]}]")
