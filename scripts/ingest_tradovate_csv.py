#!/usr/bin/env python3
"""
ingest_tradovate_csv.py — parse a Tradovate 'Performance' CSV (fill-pairs) and
update a prop-firm account's daily P&L in docs/state/trading.json.

Usage: python3 scripts/ingest_tradovate_csv.py <csv> <account_num> <net_liquidity> [fee_mode]
  fee_mode: 'pair'  -> $3.45 per round-trip trade (Apex Legacy, default)
            'contract:X' -> $X per contract round-turn
The daily NET series is anchored so the LAST day's balance == net_liquidity
(the authoritative current balance), which is what drives the tracker.
"""
import csv, sys, json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

def pnl(s):
    s=s.strip().replace('$','').replace(',',''); neg=s.startswith('(') and s.endswith(')')
    s=s.strip('()')
    try: v=float(s)
    except: v=0.0
    return -v if neg else v

def main():
    csvf, acctnum, netliq = sys.argv[1], sys.argv[2], float(sys.argv[3])
    fee_mode = sys.argv[4] if len(sys.argv)>4 else 'pair'
    rows=list(csv.DictReader(open(csvf,encoding='utf-8-sig')))
    byday=defaultdict(lambda:[0.0,0,0])  # date -> [gross, qty, pairs]
    for r in rows:
        g=pnl(r['pnl']); q=int(float(r['qty']))
        d=datetime.strptime(r['soldTimestamp'].strip(),"%m/%d/%Y %H:%M:%S").strftime("%Y-%m-%d")
        byday[d][0]+=g; byday[d][1]+=q; byday[d][2]+=1
    # net per day (apply fee)
    def fee(qty,pairs):
        if fee_mode.startswith('contract:'): return qty*float(fee_mode.split(':')[1])
        return pairs*3.45
    days=[]
    for k in sorted(byday):
        g,q,pr=byday[k]
        days.append({"date":k,"pnl":round(g-fee(q,pr),2)})
    # anchor final balance to net_liquidity, compute balances backward
    bal=netliq
    for d in reversed(days):
        d["balance"]=round(bal,2); bal=round(bal-d["pnl"],2)
    tot=round(sum(d["pnl"] for d in days),2)
    # write into trading.json
    p=Path("docs/state/trading.json"); data=json.loads(p.read_text())
    updated=False
    for bucket in ("apexAccs","lucidAccs","accs"):
        for a in data.get(bucket,[]):
            if a.get("meta",{}).get("num")==acctnum:
                a["days"]=days; updated=True
    import datetime as _dt
    data["_updated_at"]=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not updated:
        print(f"⚠ account {acctnum} not found in trading.json"); 
    else:
        p.write_text(json.dumps(data,ensure_ascii=False,indent=1)+"\n")
    print(f"{acctnum}: {len(days)} days · net P/L ${tot:,.2f} · final balance ${days[-1]['balance']:,.2f} (target ${netliq:,.2f})")
    for d in days[-8:]: print(f"   {d['date']}  ${d['pnl']:>8,.2f}  bal ${d['balance']:,.2f}")

if __name__=="__main__": main()
