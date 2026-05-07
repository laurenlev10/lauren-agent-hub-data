#!/usr/bin/env python3
"""
upsert_event_analytics.py — updates EVENT_ANALYTICS map in launch_dashboard.html
based on conversion_history.json + event metadata.

Schema written into launch_dashboard:
  EVENT_ANALYTICS["<city-slug>-<start_date>"] = {
    has_data: bool,
    total_views: int,
    sms_registered: int,
    forms_submitted: int,
    conv_rate: float (0..1),     # forms / views
    capture_rate: float (0..1),  # sms / forms
    last_pulled: ISO,
    anomalies_count: int,
    forecast_status: "on_track" | "behind" | null
  }

Reads:
  - docs/state/conversion_history.json
  - docs/launch/index.html
  - https://events.themakeupblowout.com/upcoming-events.json (for slug→evKey map)

Writes:
  - docs/launch/index.html (in-place, only EVENT_ANALYTICS line)
"""
import json, sys, re, urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent

def main():
    history_path = REPO / "docs" / "state" / "conversion_history.json"
    dashboard_path = REPO / "docs" / "launch" / "index.html"

    if not history_path.exists():
        print(f"⚠ {history_path} not found; nothing to upsert")
        return 0
    history = json.loads(history_path.read_text())
    events = history.get("events", {})
    print(f"Loaded {len(events)} events from conversion_history.json")

    # Pull upcoming events for slug → city-state-year + city-start_date mapping
    try:
        with urllib.request.urlopen(
            "https://events.themakeupblowout.com/upcoming-events.json", timeout=15
        ) as r:
            upcoming = json.load(r)
    except Exception as e:
        print(f"⚠ failed to fetch upcoming-events.json: {e}")
        upcoming = {"events": []}

    # Build map: <city>-<state>-<year> → <city-slug>-<start_date> (the launch_dashboard evKey)
    slug_to_evkey = {}
    for ev in upcoming.get("events", []):
        city = (ev.get("city") or "").lower().replace(" ", "-")
        state = (ev.get("state") or "").lower()
        start_date = ev.get("start_date") or ""
        year = start_date[:4] if start_date else ""
        if not (city and state and year):
            continue
        slug = f"{city}-{state}-{year}"
        evkey = f"{city}-{start_date}"
        slug_to_evkey[slug] = evkey

    # Build new EVENT_ANALYTICS map
    new_analytics = {}
    for slug, ev in events.items():
        evkey = slug_to_evkey.get(slug)
        if not evkey:
            print(f"  · {slug} → no evkey mapping (probably a past event)")
            continue
        funnel = ev.get("funnel") or {}
        rates = ev.get("rates") or {}
        forecast = ev.get("forecast") or {}
        total_views = funnel.get("page_views", 0) or (ev.get("views") or {}).get("total", 0)
        sms = funnel.get("sms_registered", 0) or ev.get("sms_registered", 0)
        forms = funnel.get("form_submits", 0) or (ev.get("conversions") or {}).get("total", 0)
        conv_rate = (forms / total_views) if total_views > 0 else 0
        capture_rate = (sms / forms) if forms > 0 else 0
        anomalies = ev.get("anomalies") or []

        new_analytics[evkey] = {
            "has_data": bool(total_views > 0 or sms > 0 or forms > 0),
            "total_views": int(total_views),
            "sms_registered": int(sms),
            "forms_submitted": int(forms),
            "conv_rate": round(conv_rate, 4),
            "capture_rate": round(capture_rate, 4),
            "last_pulled": ev.get("last_pulled") or history.get("_updated_at"),
            "anomalies_count": len(anomalies),
            "forecast_status": forecast.get("status") if forecast else None,
        }
        print(f"  ✓ {evkey}: {total_views}v / {forms}f / {sms}s · has_data={new_analytics[evkey]['has_data']}")

    # Read launch_dashboard.html, find EVENT_ANALYTICS line, splice in new value
    if not dashboard_path.exists():
        print(f"⚠ {dashboard_path} not found; cannot upsert")
        return 1
    text = dashboard_path.read_text(encoding="utf-8")

    # Match: const EVENT_ANALYTICS = {...};   (one line)
    pattern = re.compile(r"const EVENT_ANALYTICS\s*=\s*(\{[^}]*\}|\{.*?\});", re.DOTALL)
    m = pattern.search(text)
    if not m:
        print("⚠ EVENT_ANALYTICS line not found in launch_dashboard.html")
        return 1

    # Preserve any existing entries that aren't being overwritten this run
    try:
        existing = json.loads(m.group(1))
    except Exception:
        existing = {}
    merged = {**existing, **new_analytics}
    new_line = f"const EVENT_ANALYTICS = {json.dumps(merged, ensure_ascii=False)};"

    new_text = text[:m.start()] + new_line + text[m.end():]
    if new_text == text:
        print("· no changes to launch_dashboard.html")
        return 0

    dashboard_path.write_text(new_text, encoding="utf-8")
    print(f"✓ updated EVENT_ANALYTICS in {dashboard_path} ({len(merged)} entries total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
