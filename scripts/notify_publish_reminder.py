#!/usr/bin/env python3
"""Send Lauren a one-line SMS reminder to click 'Update' in Lovable after the
weekly homepage-schedule sync pushed a change. Lauren-only (personal nudge).
Fail-soft: never raise (a reminder must never fail the workflow)."""
import os, sys

LOVABLE_URL = "https://lovable.dev/projects/0b757644-8f54-4d25-9380-f58f7f38386b"
BODY = ("\U0001F5D3️ לוח האירועים בדף הבית עודכן. "
        "לחצי Update ב-Lovable כדי לפרסם: " + LOVABLE_URL)

def main():
    phone = os.environ.get("LAUREN_PHONE", "").strip()
    if not phone:
        print("no LAUREN_PHONE — skipping reminder"); return 0
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from lauren_sms import send_sms
        send_sms(phone, BODY)
        print("reminder SMS sent to Lauren")
    except Exception as e:
        print(f"reminder SMS failed (non-fatal): {e}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
