#!/usr/bin/env python3
"""
ingest_tradovate_csv.py — parse Tradovate 'Performance' CSV(s) (fill-pairs) and
MERGE the daily net P&L into a prop-firm account in docs/state/trading.json.

Usage:
  python3 scripts/ingest_tradovate_csv.py <account_num> <net_liquidity> <csv1> [csv2 ...] [--fee pair|contract:X]

- Multiple CSVs are combined.
- MERGES with the account's existing days (new dates override, old dates kept) —
  never overwrites history. Upload month-by-month and it accumulates.
- The full merged series is re-anchored so the LAST day's balance == net_liquidity
  (the authoritative current balance from the broker widget).
- fee: 'pair' = $3.45/round-trip trade (Apex Legacy, default) | 'contract:X' = $X/contract.
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
    args=sys.argv[1:]
    fee_mode='pair'
    if '--fee' in args:
        i=args.index('--fee'); fee_mode=args[i+1]; del args[i:i+2]
    acctnum, netliq = args[0], float(args[1])
    csvs=args[2:]
    def fee(qty,pairs):
        return qty*float(fee_mode.split(':')[1]) if fee_mode.startswith('contract:') else pairs*3.45
    byday=defaultdict(lambda:[0.0,0,0])
    for f in csvs:
        for r in csv.DictReader(open(f,encoding='utf-8-sig')):
            g=pnl(r['pnl']); q=int(float(r['qty']))
            d=datetime.strptime(r['soldTimestamp'].strip(),"%m/%d/%Y %H:%M:%S").strftime("%Y-%m-%d")
            byday[d][0]+=g; byday[d][1]+=q; byday[d][2]+=1
    new_net={k:round(v[0]-fee(v[1],v[2]),2) for k,v in byday.items()}
    p=Path("docs/state/trading.json"); data=json.loads(p.read_text())
    acc=None
    for b in ("apexAccs","lucidAccs","accs"):
        for a in data.get(b,[]):
            if a.get("meta",{}).get("num")==acctnum: acc=a
    if not acc: print(f"⚠ account {acctnum} not found"); return
    # merge daily pnl (new overrides same date)
    merged={d["date"]:d["pnl"] for d in acc.get("days",[])}
    merged.update(new_net)
    days=[{"date":k,"pnl":merged[k]} for k in sorted(merged)]
    # re-anchor balances to current net_liquidity
    bal=netliq
    for d in reversed(days):
        d["balance"]=round(bal,2); bal=round(bal-d["pnl"],2)
    acc["days"]=days
    import datetime as _dt
    data["_updated_at"]=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(data,ensure_ascii=False,indent=1)+"\n")
    ds=[d["date"] for d in days]
    print(f"{acctnum}: {len(days)} days ({ds[0]}→{ds[-1]}) · final ${days[-1]['balance']:,.2f} (target ${netliq:,.2f}) · added {len(new_net)} dates")

if __name__=="__main__": main()
