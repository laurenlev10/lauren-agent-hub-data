#!/usr/bin/env python3
"""events_index_build.py — extracts SCHEDULE from docs/launch/index.html into
docs/state/events_index.json so state-served dashboards (bookkeeping) can map
a DATE → the event + its QuickBooks Class name ("{City} {Year}").
Runs daily inside qb-untagged-refresh.yml."""
from __future__ import annotations
import datetime as dt, json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def main():
    html = (ROOT/"docs/launch/index.html").read_text(encoding="utf-8")
    m = re.search(r"const SCHEDULE = (\{.*?\});\n", html, re.S)
    if not m:
        print("SCHEDULE not found", file=sys.stderr); return 1
    sched = json.loads(m.group(1))
    events = []
    for year, rows in sched.items():
        if year.startswith("_") or not isinstance(rows, list): continue
        for ev in rows:
            city, start, end = ev.get("city") or "", ev.get("start_date") or "", ev.get("end_date") or ""
            if not (city and start and end): continue
            slug = re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", city.lower()))
            events.append({"evkey": f"{slug}-{start}", "city": city, "state": ev.get("state") or "",
                           "start_date": start, "end_date": end,
                           "class_name": f"{city} {start[:4]}",
                           "venue": ev.get("venue") or "", "address": ev.get("address") or ""})
    events.sort(key=lambda e: e["start_date"])
    out = {"_updated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "events": events}
    (ROOT/"docs/state/events_index.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"events_index: {len(events)} events ({events[0]['start_date']} .. {events[-1]['start_date']})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
