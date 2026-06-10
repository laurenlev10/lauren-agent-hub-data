#!/usr/bin/env python3
"""mystery_box_eod_sms.py — end-of-event Mystery Box SMS.

Lauren 2026-06-10 (verbatim): "תוכל לשלוח הודעת טקסט בסיום האירוע מתוזמנת של כמה
קופסאות נמכרו - וכמה נשארו במלאי? כמות המיסטרי בוקס שנמכרו במהלך 3 הימים של האירוע?
שלח אותה ביום ראשון בשעה 6PM שעון המקומי של האירוע" + "שלח אותה גם לאלי".

Cron fires Sundays at several UTC hours (covering all US event timezones); this
script GATES on event-local time — it sends only when the local time at the event's
city is 18:00-18:59 on the event's LAST day (end_date). One SMS per event, deduped
via docs/state/mystery_box_eod_sms.json.

Data:
  - units sold Fri-Sun  <- OCTOPOS /get-sales-by-vendor-product-report (pnl_octopos.
    fetch_all_vendor_products + mystery_box_from) — same source as the P&L (IRON RULE #9:
    never infer sales from get-recount-data).
  - remaining stock     <- OCTOPOS v2 /products/<id> live (octopos_sync.authenticate +
    fetch_product -> in_stock_qty).

Recipients: Lauren + Eli, fail-soft per IRON RULE #4.C (content per-run).
"""
from __future__ import annotations
import datetime as dt
import json, os, sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pnl_octopos                      # noqa: E402
import octopos_sync                     # noqa: E402
from lauren_sms import send_sms         # noqa: E402
from insta_reel_scan import STATE_TZ    # noqa: E402 — canonical state->tz map

STATE_FILE = ROOT / "docs/state/mystery_box_eod_sms.json"


def main():
    force_evkey = (os.environ.get("FORCE_EVKEY") or "").strip()  # manual-dispatch testing
    events = json.loads((ROOT / "docs/state/events_index.json").read_text(encoding="utf-8"))["events"]
    now_utc = dt.datetime.now(dt.timezone.utc)

    target = None
    for e in events:
        if force_evkey:
            if e["evkey"] == force_evkey:
                tz = ZoneInfo(STATE_TZ.get((e.get("state") or "").upper(), "America/Los_Angeles"))
                target = (e, now_utc.astimezone(tz))
                break
            continue
        tz = ZoneInfo(STATE_TZ.get((e.get("state") or "").upper(), "America/Los_Angeles"))
        loc = now_utc.astimezone(tz)
        if loc.strftime("%Y-%m-%d") == (e.get("end_date") or "") and loc.hour == 18:
            target = (e, loc)
            break
    if not target:
        print("no event at local 18:00 on its end_date right now — nothing to do")
        return 0

    e, loc = target
    evkey = e["evkey"]
    state = (json.loads(STATE_FILE.read_text(encoding="utf-8"))
             if STATE_FILE.exists() else {"_updated_at": None, "sent": {}})
    if not force_evkey and evkey in (state.get("sent") or {}):
        print(f"already sent for {evkey} — skipping")
        return 0

    # --- units sold during the event (Fri-Sun) ---
    jwt = pnl_octopos.octopos_jwt()
    sales = pnl_octopos.fetch_all_vendor_products(jwt, e["start_date"], e["end_date"])
    mb = pnl_octopos.mystery_box_from(sales)
    units = int(round(float(mb.get("units") or 0)))
    pid = mb.get("product_id")
    rev = mb.get("revenue")

    # --- live remaining stock ---
    stock_txt = "לא ידוע"
    try:
        email = os.environ.get("OCTOPOS_EMAIL", "").strip()
        password = os.environ.get("OCTOPOS_PASSWORD", "").strip()
        token, _locs = octopos_sync.authenticate(email, password)
        if pid:
            prod = octopos_sync.fetch_product(token, int(pid))
            if isinstance(prod, dict) and prod.get("in_stock_qty") is not None:
                stock_txt = f"{int(float(prod['in_stock_qty']))}"
    except Exception as ex:
        print(f"WARN stock fetch failed: {ex}", file=sys.stderr)

    body = (f"🎁 מיסטרי בוקס — סיכום {e['city']}, {e['state']}\n"
            f"נמכרו ב-3 ימי האירוע: {units} קופסאות"
            + (f" · הכנסה ${float(rev):,.0f}" if isinstance(rev, (int, float)) and rev else "")
            + f"\nנשארו במלאי: {stock_txt}\n"
            f"https://laurenlev10.github.io/lauren-agent-hub-data/shopify/?tab=events")

    recipients = []
    for env_key, label in [("LAUREN_PHONE", "Lauren"), ("ELI_PHONE", "Eli")]:
        v = os.environ.get(env_key, "").strip()
        if v:
            recipients.append((label, v))
    sent_any = False
    for name, phone in recipients:
        try:
            send_sms(phone, body)
            sent_any = True
            print(f"  ✓ SMS sent to {name}")
        except Exception as ex:
            print(f"  SMS to {name} failed: {ex}")

    if sent_any:
        state.setdefault("sent", {})[evkey] = {
            "sent_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "local_time": loc.strftime("%Y-%m-%d %H:%M"),
            "units": units, "stock": stock_txt,
        }
        state["_updated_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
