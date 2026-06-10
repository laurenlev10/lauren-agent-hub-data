#!/usr/bin/env python3
"""
tracking-check — live shipment status for every tracking number saved on
supplier orders in docs/state/inventory_orders.json.

Reads:  docs/state/inventory_orders.json
          events[evkey].local_orders[i].tracking_numbers[]   (per-order, main path)
          events[evkey].suppliers[code].tracking_number      (legacy supplier-level)
Writes: docs/state/tracking_status.json
          { _updated_at, ups_api, trackings: { "<num>": {carrier, status, status_type,
            description, location, est_delivery, delivered_at, activity_at,
            evkey, supplier_code, source, error, checked_at} } }

Carriers:
  UPS   — official Track API (OAuth client-credentials; secrets UPS_CLIENT_ID /
          UPS_CLIENT_SECRET). Full live status.
  USPS / FedEx / unknown — recognized + recorded so the dashboard can render a
          tracking link, but no live status (no API credentials yet).

Fail-soft EVERYWHERE: missing creds or a per-number API error is recorded on
that tracking entry; the run still succeeds and writes the file. Only a real
crash triggers the IRON RULE #3 failure SMS in the workflow.

Only events whose end_date is within the last WINDOW_DAYS (or in the future)
are scanned, so old events stop consuming API calls automatically.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORDERS_PATH = ROOT / "docs" / "state" / "inventory_orders.json"
OUT_PATH = ROOT / "docs" / "state" / "tracking_status.json"
UPS_BASE = "https://onlinetools.ups.com"
WINDOW_DAYS = 30
TIMEOUT = 30


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_carrier(num: str) -> str:
    n = re.sub(r"\s+", "", num or "").upper()
    if re.match(r"^1Z[0-9A-Z]{16}$", n):
        return "ups"
    if re.match(r"^9[2-5]\d{18,24}$", n) or re.match(r"^\d{20,22}$", n):
        return "usps"
    if re.match(r"^\d{12}$", n) or re.match(r"^\d{15}$", n):
        return "fedex"
    return "unknown"


def collect_trackings(orders: dict):
    """Yield (number, evkey, supplier_code, source) for every saved tracking number."""
    found = []
    seen = set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    for evkey, ev in (orders.get("events") or {}).items():
        end = ev.get("end_date") or ev.get("start_date") or ""
        if end and end < cutoff:
            continue
        for i, o in enumerate(ev.get("local_orders") or []):
            if o.get("cancelled_at") or o.get("moved_to"):
                continue
            for tn in (o.get("tracking_numbers") or []):
                tn = (tn or "").strip()
                if tn and tn not in seen:
                    seen.add(tn)
                    found.append((tn, evkey, o.get("supplier_code") or "", "order#%d" % (i + 1)))
        for code, s in (ev.get("suppliers") or {}).items():
            tn = ((s or {}).get("tracking_number") or "").strip()
            if tn and tn not in seen:
                seen.add(tn)
                found.append((tn, evkey, code, "supplier"))
    return found


def ups_token():
    """OAuth client-credentials. Returns (token, status_string)."""
    cid = os.environ.get("UPS_CLIENT_ID", "").strip()
    sec = os.environ.get("UPS_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        return None, "no_credentials"
    req = urllib.request.Request(
        UPS_BASE + "/security/v1/oauth/token",
        data=b"grant_type=client_credentials",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            tok = json.load(r).get("access_token")
            return (tok, "ok") if tok else (None, "auth_failed")
    except Exception as e:
        print(f"  UPS auth failed: {e}", file=sys.stderr)
        return None, "auth_failed"


def fmt_ups_date(d: str) -> str:
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) >= 8 else (d or "")


def ups_track(token: str, num: str) -> dict:
    """Query UPS Track API for one number. Returns parsed status dict (fail-soft)."""
    out = {
        "status": None, "status_type": None, "description": None, "location": None,
        "est_delivery": None, "delivered_at": None, "activity_at": None, "error": None,
    }
    url = (UPS_BASE + "/api/track/v1/details/" + urllib.parse.quote(num)
           + "?locale=en_US&returnSignature=false&returnMilestones=false")
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "transId": uuid.uuid4().hex,
        "transactionSrc": "lauren-inventory-tracking",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        out["error"] = f"http_{e.code}: {body}"
        return out
    except Exception as e:
        out["error"] = f"request_failed: {e}"
        return out

    try:
        shp = (resp.get("trackResponse", {}).get("shipment") or [{}])[0]
        pkg = (shp.get("package") or [{}])[0]
        acts = pkg.get("activity") or []
        if acts:
            a = acts[0]
            st = a.get("status") or {}
            out["status_type"] = st.get("type")
            out["description"] = (st.get("description") or "").strip()
            addr = (a.get("location") or {}).get("address") or {}
            city = (addr.get("city") or "").strip().title()
            region = (addr.get("stateProvince") or addr.get("country") or "").strip()
            out["location"] = ", ".join(x for x in (city, region) if x)
            d, t = a.get("date") or "", a.get("time") or ""
            if d:
                out["activity_at"] = fmt_ups_date(d) + (f" {t[0:2]}:{t[2:4]}" if len(t) >= 4 else "")
        cur = pkg.get("currentStatus") or {}
        out["status"] = (cur.get("description") or "").strip() or out["description"]
        for dd in pkg.get("deliveryDate") or []:
            typ, dt = dd.get("type"), fmt_ups_date(dd.get("date") or "")
            if typ == "DEL" and dt:
                out["delivered_at"] = dt
            elif typ in ("SDD", "RDD") and dt and not out["est_delivery"]:
                out["est_delivery"] = dt
        if out["status_type"] == "D" and not out["delivered_at"]:
            out["delivered_at"] = out["activity_at"]
        if not out["status"] and not out["error"]:
            out["error"] = "no_status_in_response"
    except Exception as e:
        out["error"] = f"parse_failed: {e}"
    return out


def main():
    orders = json.loads(ORDERS_PATH.read_text(encoding="utf-8"))
    found = collect_trackings(orders)
    print(f"Found {len(found)} tracking numbers in window ({WINDOW_DAYS}d)")

    # Preserve previous statuses so a one-off API failure doesn't blank the dashboard.
    prev = {}
    if OUT_PATH.exists():
        try:
            prev = json.loads(OUT_PATH.read_text(encoding="utf-8")).get("trackings") or {}
        except Exception:
            prev = {}

    token, api_state = ups_token()
    print(f"UPS API: {api_state}")

    trackings = {}
    for num, evkey, sup, source in found:
        carrier = detect_carrier(num)
        entry = {
            "carrier": carrier, "evkey": evkey, "supplier_code": sup, "source": source,
            "status": None, "status_type": None, "description": None, "location": None,
            "est_delivery": None, "delivered_at": None, "activity_at": None,
            "error": None, "checked_at": now_iso(),
        }
        p = prev.get(num) or {}
        if carrier == "ups" and token:
            entry.update(ups_track(token, num))
            entry["checked_at"] = now_iso()
            if entry.get("error") and p.get("status"):
                # keep last good status, surface the error alongside
                for k in ("status", "status_type", "description", "location",
                          "est_delivery", "delivered_at", "activity_at"):
                    entry[k] = p.get(k)
            label = entry.get("status") or entry.get("error") or "?"
            print(f"  {num} [{carrier}] -> {label}")
        else:
            # No live API for this carrier (or UPS creds missing) — keep any
            # previously-known status, mark why.
            for k in ("status", "status_type", "description", "location",
                      "est_delivery", "delivered_at", "activity_at"):
                entry[k] = p.get(k)
            entry["error"] = ("no_api_credentials" if carrier == "ups"
                              else f"no_live_api_for_{carrier}")
            print(f"  {num} [{carrier}] -> skipped ({entry['error']})")
        trackings[num] = entry

    out = {"_updated_at": now_iso(), "ups_api": api_state, "trackings": trackings}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(trackings)} trackings)")


if __name__ == "__main__":
    main()
