"""
fb_event_stats.py — Fetch attending_count + interested_count for every FB event
in FB_EVENTS map, write to docs/state/fb_event_stats.json.

Created 2026-05-23 — per Lauren: "אני רוצה להוסיף בחלק הזה מתחת על בסיס אותו הרעיון:
כמה GOING / INTERESTED".

Pattern follows octopos_live_sync.py + slow_movers_run.py:
- Single batched Graph API call (cheap — 1 call per run for all 40+ events)
- Writes per-evkey stats to docs/state/fb_event_stats.json
- Dashboard reads on page load + renders per-row card

Required env: META_PAGE_TOKEN.
"""

import os, re, json, urllib.request, urllib.parse, datetime, sys, pathlib

TOKEN = os.environ.get("META_PAGE_TOKEN", "").strip()
if not TOKEN:
    print("⚠ META_PAGE_TOKEN missing — cannot fetch FB event stats")
    sys.exit(1)

repo = pathlib.Path(".")
launch = (repo / "docs/launch/index.html").read_text()
m = re.search(r"const FB_EVENTS\s*=\s*(\{[^;]*?\});", launch, re.S)
if not m:
    print("⚠ FB_EVENTS map not found in launch/index.html")
    sys.exit(1)
fb_events = json.loads(m.group(1))
print(f"FB_EVENTS map has {len(fb_events)} entries")

# Build batched id list
ids = []
ek_by_id = {}
for ek, slot in fb_events.items():
    fb_id = (slot or {}).get("fb_event_id")
    if fb_id:
        ids.append(str(fb_id))
        ek_by_id[str(fb_id)] = ek

if not ids:
    print("⚠ No fb_event_ids in FB_EVENTS — nothing to fetch")
    sys.exit(0)

# Graph API supports batch — but max ~50 ids per call to stay under URL length.
# Chunk into batches of 50.
def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

all_stats = {}
fields = "name,attending_count,interested_count,declined_count,maybe_count,is_canceled"
for batch_ids in chunks(ids, 50):
    url = "https://graph.facebook.com/v22.0/?" + urllib.parse.urlencode({
        "ids": ",".join(batch_ids),
        "fields": fields,
        "access_token": TOKEN,
    })
    print(f"Fetching batch of {len(batch_ids)} events…")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"  ERROR: {e}")
        continue
    for fb_id, payload in data.items():
        if not isinstance(payload, dict): continue
        if "error" in payload: continue
        ek = ek_by_id.get(fb_id)
        if not ek: continue
        all_stats[ek] = {
            "fb_event_id":     fb_id,
            "name":            payload.get("name",""),
            "going":           int(payload.get("attending_count", 0) or 0),
            "interested":      int(payload.get("interested_count", 0) or 0),
            "declined":        int(payload.get("declined_count", 0) or 0),
            "maybe":           int(payload.get("maybe_count", 0) or 0),
            "is_canceled":     bool(payload.get("is_canceled", False)),
            "fetched_at":      datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

if not all_stats:
    print("⚠ No stats fetched")
    sys.exit(1)

# Write state file
out_path = repo / "docs/state/fb_event_stats.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
state = {
    "_updated_at":  datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "_event_count": len(all_stats),
    "events":       all_stats,
}
out_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
print(f"✓ Wrote {len(all_stats)} event stats to {out_path}")

# Print headline summary
total_going = sum(s["going"] for s in all_stats.values())
total_interested = sum(s["interested"] for s in all_stats.values())
print(f"  Total Going: {total_going}, Total Interested: {total_interested}")
# Top 5 by going+interested
top = sorted(all_stats.items(), key=lambda kv: kv[1]["going"] + kv[1]["interested"], reverse=True)[:5]
print("  Top 5 by Going+Interested:")
for ek, s in top:
    print(f"    {ek:35s} {s['going']:>4} going · {s['interested']:>4} interested")
