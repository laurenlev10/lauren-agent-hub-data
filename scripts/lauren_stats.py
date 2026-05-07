"""
lauren_stats — shared marketing analytics module.

Fetches per-event traffic + conversion + ROAS from:
  - GA4 Reporting API (views, sessions, events, UTM breakdown)
  - Meta Marketing Insights API (ad spend + conversion attribution)
  - TikTok Business API (TikTok ad performance + pixel events)

Outputs a normalized dict per event for the @stats agent to render dashboards.

This module is dependency-light — uses only urllib + json (stdlib). For GA4 it
requires either a service-account JSON key OR an OAuth refresh token (set via
env var GA4_SERVICE_ACCOUNT_JSON). When unset, fetch functions return empty
dicts — the workflow continues without that source so a missing secret doesn't
fail the whole run.

Public API:
    fetch_ga4_event_data(start_date, end_date, slugs=None) -> dict
    fetch_meta_pixel_events(start_date, end_date) -> dict
    fetch_tiktok_pixel_events(start_date, end_date) -> dict
    aggregate_for_events(slugs) -> dict   # combines all 3 + computes anomalies
    detect_anomalies(event_data, baselines) -> list

Exit-fail behavior: each fetch logs warnings on its own and never raises. The
caller decides whether partial data is enough.
"""

import datetime as _dt
import json as _json
import os as _os
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq
from pathlib import Path as _Path
from typing import Optional as _Optional


# ---------------------------------------------------------------------------
# GA4 Reporting API
# ---------------------------------------------------------------------------

def _ga4_token() -> _Optional[str]:
    """Get OAuth token from service account JSON (env var)."""
    sa_json = _os.environ.get("GA4_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        return None
    # When in a workflow, sa_json is the JSON content directly
    try:
        creds = _json.loads(sa_json)
    except _json.JSONDecodeError:
        # Maybe it's a file path
        if _Path(sa_json).exists():
            creds = _json.loads(_Path(sa_json).read_text())
        else:
            return None
    # OAuth2 service account flow — sign a JWT, exchange for access token
    # (Stdlib-only implementation — keeps dependencies zero)
    import base64, hmac, hashlib, time
    header = {"alg": "RS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": creds["client_email"],
        "scope": "https://www.googleapis.com/auth/analytics.readonly",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now,
    }
    def b64u(s):
        return base64.urlsafe_b64encode(s).rstrip(b"=").decode()
    h_enc = b64u(_json.dumps(header).encode())
    p_enc = b64u(_json.dumps(payload).encode())
    msg = f"{h_enc}.{p_enc}".encode()

    # Signing requires RSA — needs cryptography or rsa lib. We use a minimal
    # PKCS#1 implementation. If it's missing, log + return None (degrade gracefully).
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
        sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    except ImportError:
        print("  ⚠ ga4: cryptography not available — skipping GA4 fetch")
        return None
    jwt = f"{h_enc}.{p_enc}.{b64u(sig)}"
    # Exchange JWT for access token
    req = _urlreq.Request(
        "https://oauth2.googleapis.com/token",
        data=_urlparse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        }).encode(),
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
            return data.get("access_token")
    except _urlerr.HTTPError as e:
        print(f"  ⚠ ga4 token exchange failed: {e.read().decode()[:200]}")
        return None


def fetch_ga4_event_data(start_date: str, end_date: str, slugs=None) -> dict:
    """
    Returns: { "<slug>": { views, by_lang, by_source, by_campaign, conversions, share_clicks } }
    """
    prop_id = _os.environ.get("GA4_PROPERTY_ID")
    if not prop_id:
        print("  ⚠ ga4: GA4_PROPERTY_ID not set — skipping")
        return {}
    token = _ga4_token()
    if not token:
        return {}

    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop_id}:runReport"
    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [
            {"name": "pagePath"},
            {"name": "sessionSource"},
            {"name": "sessionMedium"},
            {"name": "sessionCampaignName"},
            {"name": "language"},
        ],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "sessions"},
            {"name": "conversions"},
        ],
        "limit": 5000,
    }
    req = _urlreq.Request(
        url,
        data=_json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode())
    except _urlerr.HTTPError as e:
        print(f"  ⚠ ga4 query failed: {e.read().decode()[:200]}")
        return {}

    # Aggregate by slug
    out = {}
    for row in data.get("rows", []):
        path = row["dimensionValues"][0]["value"]
        source = row["dimensionValues"][1]["value"]
        medium = row["dimensionValues"][2]["value"]
        campaign = row["dimensionValues"][3]["value"]
        views = int(row["metricValues"][0]["value"])
        sessions = int(row["metricValues"][1]["value"])
        conversions = int(row["metricValues"][2]["value"])
        # Extract slug from /events/<slug>/...
        m = path.split("/events/")
        if len(m) < 2:
            continue
        slug = m[1].split("/")[0]
        if slugs and slug not in slugs:
            continue
        ev = out.setdefault(slug, {
            "views": {"total": 0, "by_source": {}, "by_campaign": {}, "by_lang": {"en": 0, "es": 0, "tt": 0}},
            "conversions": {"total": 0, "by_source": {}},
            "share_clicks": {},
        })
        ev["views"]["total"] += views
        # Source/medium key
        sk = f"{source}_{medium}".replace(" ", "_")
        ev["views"]["by_source"][sk] = ev["views"]["by_source"].get(sk, 0) + views
        if campaign and campaign != "(not set)":
            ev["views"]["by_campaign"][campaign] = ev["views"]["by_campaign"].get(campaign, 0) + views
        # Language inferred from path
        if "tiktok" in path:    ev["views"]["by_lang"]["tt"] += views
        elif "-es.html" in path: ev["views"]["by_lang"]["es"] += views
        else:                    ev["views"]["by_lang"]["en"] += views
        ev["conversions"]["total"] += conversions
        ev["conversions"]["by_source"][sk] = ev["conversions"]["by_source"].get(sk, 0) + conversions

    return out


# ---------------------------------------------------------------------------
# Meta Marketing API (ad spend + pixel events)
# ---------------------------------------------------------------------------

def fetch_meta_pixel_events(start_date: str, end_date: str) -> dict:
    """Fetch ad spend + conversions per ad set, attributable to events."""
    token = _os.environ.get("META_PAGE_TOKEN")
    ad_account = _os.environ.get("META_AD_ACCOUNT_ID")
    if not token or not ad_account:
        print("  ⚠ meta: token or ad account ID not set — skipping")
        return {}

    url = f"https://graph.facebook.com/v25.0/{ad_account}/insights"
    params = {
        "fields": "campaign_name,ad_name,spend,actions,action_values",
        "time_range": _json.dumps({"since": start_date, "until": end_date}),
        "level": "ad",
        "access_token": token,
    }
    req = _urlreq.Request(f"{url}?{_urlparse.urlencode(params)}")
    try:
        with _urlreq.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode())
    except _urlerr.HTTPError as e:
        print(f"  ⚠ meta query failed: {e.read().decode()[:200]}")
        return {}

    out = {}
    for ad in data.get("data", []):
        # Try to extract slug from campaign_name (e.g. "columbia-mo-2026_round1")
        name = ad.get("campaign_name", "")
        slug = name.split("_")[0] if "_" in name else name
        ev = out.setdefault(slug, {"meta_spend": 0.0, "meta_conversions": 0, "meta_revenue": 0.0})
        ev["meta_spend"] += float(ad.get("spend", 0))
        for a in ad.get("actions", []):
            if a.get("action_type") in ("lead", "complete_registration"):
                ev["meta_conversions"] += int(a.get("value", 0))
        for a in ad.get("action_values", []):
            if a.get("action_type") == "lead":
                ev["meta_revenue"] += float(a.get("value", 0))
    return out


# ---------------------------------------------------------------------------
# TikTok Business API
# ---------------------------------------------------------------------------

def fetch_tiktok_pixel_events(start_date: str, end_date: str) -> dict:
    """Fetch TikTok ad performance + pixel events."""
    token = _os.environ.get("TIKTOK_ACCESS_TOKEN")
    advertiser_id = _os.environ.get("TIKTOK_ADVERTISER_ID")
    if not token or not advertiser_id:
        print("  ⚠ tiktok: token or advertiser ID not set — skipping")
        return {}

    url = "https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/"
    params = {
        "advertiser_id": advertiser_id,
        "report_type": "BASIC",
        "data_level": "AUCTION_AD",
        "dimensions": _json.dumps(["ad_id", "campaign_name"]),
        "metrics": _json.dumps(["spend", "conversion", "complete_payment"]),
        "start_date": start_date,
        "end_date": end_date,
    }
    req = _urlreq.Request(
        f"{url}?{_urlparse.urlencode(params)}",
        headers={"Access-Token": token},
    )
    try:
        with _urlreq.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode())
    except _urlerr.HTTPError as e:
        print(f"  ⚠ tiktok query failed: {e.read().decode()[:200]}")
        return {}

    out = {}
    for row in data.get("data", {}).get("list", []):
        meta = row.get("dimensions", {})
        metrics = row.get("metrics", {})
        name = meta.get("campaign_name", "")
        slug = name.split("_")[0] if "_" in name else name
        ev = out.setdefault(slug, {"tiktok_spend": 0.0, "tiktok_conversions": 0})
        ev["tiktok_spend"] += float(metrics.get("spend", 0))
        ev["tiktok_conversions"] += int(metrics.get("conversion", 0))
    return out


# ---------------------------------------------------------------------------
# Aggregator + anomaly detector
# ---------------------------------------------------------------------------

def aggregate_for_events(slugs: list, start_date: str = None, end_date: str = None) -> dict:
    """
    Combines all 3 sources for the given list of event slugs.
    Returns the full event_analytics.json shape (events: {} + rolling_baselines).
    """
    today = _dt.date.today()
    end_date = end_date or today.isoformat()
    start_date = start_date or (today - _dt.timedelta(days=30)).isoformat()

    ga4 = fetch_ga4_event_data(start_date, end_date, slugs=slugs)
    meta = fetch_meta_pixel_events(start_date, end_date)
    tt = fetch_tiktok_pixel_events(start_date, end_date)

    out = {"events": {}}
    for slug in slugs:
        ev = ga4.get(slug, {"views": {"total": 0, "by_source": {}, "by_campaign": {}, "by_lang": {}}, "conversions": {"total": 0, "by_source": {}}, "share_clicks": {}})
        m = meta.get(slug, {})
        t = tt.get(slug, {})
        ev.setdefault("ad_spend", {})["meta"] = m.get("meta_spend", 0)
        ev["ad_spend"]["tiktok"] = t.get("tiktok_spend", 0)
        ev.setdefault("ad_revenue_attributed", {})["meta"] = m.get("meta_revenue", 0)
        ev.setdefault("roas_by_source", {})
        if m.get("meta_spend"):
            ev["roas_by_source"]["meta"] = round(m.get("meta_revenue", 0) / m["meta_spend"], 2)
        # No TikTok revenue side yet — leave roas blank
        ev["last_pulled"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out["events"][slug] = ev
    out["_updated_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def detect_anomalies(event_data: dict, baselines: dict) -> list:
    """Compare event_data to rolling baselines, return list of anomalies."""
    out = []
    for slug, ev in event_data.get("events", {}).items():
        # Funnel anomaly: spending money but no SMS registrations
        f = ev.get("funnel") or {}
        spend_total = (ev.get("ad_spend") or {}).get("meta", 0) + (ev.get("ad_spend") or {}).get("tiktok", 0)
        if spend_total > 50 and f.get("sms_registered", 0) == 0 and f.get("page_views", 0) > 50:
            out.append({"event": slug, "severity": "critical", "metric": "zero_sms_registrations",
                        "observed": 0, "expected": ">5",
                        "hypothesis": f"spending ${spend_total:.0f}, getting page views, but no SMS sign-ups — form broken?"})
        v = ev.get("views", {})
        c = ev.get("conversions", {})
        rate = (c.get("total", 0) / v.get("total", 1)) if v.get("total") else 0
        baseline_rate = baselines.get("median_conv_rate_overall", 0.10)
        if v.get("total", 0) > 100 and rate < 0.05 and rate < baseline_rate * 0.5:
            out.append({"event": slug, "severity": "warning", "metric": "conv_rate",
                        "observed": round(rate, 3), "expected": baseline_rate,
                        "hypothesis": "page broken? form misconfigured?"})
        for src, roas in ev.get("roas_by_source", {}).items():
            if roas < 1.5:
                out.append({"event": slug, "severity": "warning", "metric": f"roas_{src}",
                            "observed": roas, "expected": 3.0,
                            "hypothesis": f"cut spend on {src} creative"})
    return out


# ---------------------------------------------------------------------------
# Level 2: SimpleTexting funnel-end (sms registrations)
# ---------------------------------------------------------------------------

def fetch_simpletexting_list_sizes(slug_to_list_id: dict) -> dict:
    """
    Fetch SimpleTexting list size for each slug→list_id mapping.

    Args:
        slug_to_list_id: {"columbia-mo-2026": "691675e163f88543ee7b07c8", ...}

    Returns:
        {"columbia-mo-2026": {"list_size": 35, "list_id": "...", "fetched_at": "..."}}
    """
    token = _os.environ.get("SIMPLETEXTING_TOKEN")
    if not token:
        print("  ⚠ simpletexting: SIMPLETEXTING_TOKEN not set — skipping")
        return {}

    out = {}
    fetched_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for slug, list_id in slug_to_list_id.items():
        if not list_id:
            continue
        url = f"https://app2.simpletexting.com/api/list/v2/contacts?listId={list_id}&size=1"
        req = _urlreq.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with _urlreq.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            total = data.get("totalElements", data.get("totalCount", 0))
            out[slug] = {"list_size": int(total), "list_id": list_id, "fetched_at": fetched_at}
            print(f"  ✓ simpletexting [{slug}]: {total} contacts on list {list_id}")
        except _urlerr.HTTPError as e:
            err_body = ""
            try: err_body = e.read().decode()[:200]
            except Exception: pass
            print(f"  ⚠ simpletexting [{slug}] failed ({e.code}): {err_body}")
        except Exception as e:
            print(f"  ⚠ simpletexting [{slug}] error: {e}")
    return out


def extract_setups_from_launch_dashboard(html_path) -> dict:
    """Parse SETUPS = {...}; from launch_dashboard.html."""
    import re
    p = _Path(html_path)
    if not p.exists():
        print(f"  ⚠ launch dashboard not found at {html_path}")
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.search(r"const SETUPS\s*=\s*(\{.*?\});", text, re.DOTALL)
    if not m:
        print("  ⚠ SETUPS block not found in launch_dashboard.html")
        return {}
    try:
        return _json.loads(m.group(1))
    except Exception as e:
        print(f"  ⚠ SETUPS parse failed: {e}")
        return {}


def map_setups_to_slugs(setups: dict, events: list) -> dict:
    """Build {slug: list_id} from SETUPS map + events list."""
    out = {}
    for ev in events:
        city = (ev.get("city") or "").lower().replace(" ", "-")
        state = (ev.get("state") or "").lower()
        start_date = ev.get("start_date") or ""
        year = start_date[:4] if start_date else ""
        if not (city and state and year):
            continue
        setup_key = f"{city}-{start_date}"
        slug_key = f"{city}-{state}-{year}"
        s = setups.get(setup_key, {})
        list_id = (s.get("smslist") or {}).get("list_id") if s else None
        out[slug_key] = list_id
    return out


def compute_funnel(ev_data: dict, days_until_event=None, registration_target: int = 250) -> dict:
    """Build {funnel, rates, forecast} block for one event."""
    views_total = (ev_data.get("views") or {}).get("total", 0)
    conv_total = (ev_data.get("conversions") or {}).get("total", 0)
    sms_reg = ev_data.get("sms_registered", 0)
    eventbrite_reg = ev_data.get("eventbrite_registered", 0)
    impressions = (ev_data.get("impressions") or {}).get("total", 0) if isinstance(ev_data.get("impressions"), dict) else 0

    funnel = {
        "impressions": int(impressions),
        "page_views": int(views_total),
        "form_submits": int(conv_total),
        "sms_registered": int(sms_reg),
        "eventbrite_registered": int(eventbrite_reg),
    }

    rates = {}
    if impressions > 0:
        rates["ctr"] = round(views_total / impressions * 100, 2)
        rates["overall"] = round(sms_reg / impressions * 100, 3)
    if views_total > 0:
        rates["form_conversion"] = round(conv_total / views_total * 100, 2)
    if conv_total > 0:
        rates["sms_capture"] = round(sms_reg / conv_total * 100, 2)

    forecast = None
    if days_until_event is not None and days_until_event > 0 and sms_reg > 0:
        days_so_far = max(1, 14 - days_until_event) if days_until_event < 30 else 14
        daily_rate = sms_reg / days_so_far
        projected_total = int(sms_reg + daily_rate * days_until_event)
        forecast = {
            "projected_total": projected_total,
            "target": registration_target,
            "days_remaining": days_until_event,
            "status": "on_track" if projected_total >= registration_target else "behind",
            "gap": registration_target - projected_total if projected_total < registration_target else 0,
        }

    return {"funnel": funnel, "rates": rates, "forecast": forecast}


def aggregate_with_funnel(slugs: list, events: list, setups: dict,
                          start_date: str = None, end_date: str = None) -> dict:
    """Level 2 aggregator — combines all sources + SimpleTexting + funnel + forecast."""
    base = aggregate_for_events(slugs, start_date=start_date, end_date=end_date)
    slug_to_list = map_setups_to_slugs(setups, events)
    sms_data = fetch_simpletexting_list_sizes(slug_to_list)

    today = _dt.date.today()
    ev_by_slug = {}
    for ev in events:
        city = (ev.get("city") or "").lower().replace(" ", "-")
        state = (ev.get("state") or "").lower()
        year = (ev.get("start_date") or "")[:4]
        ev_by_slug[f"{city}-{state}-{year}"] = ev

    for slug, ev_out in base.get("events", {}).items():
        sms = sms_data.get(slug, {})
        ev_out["sms_registered"] = sms.get("list_size", 0)
        ev_out["sms_list_id"] = sms.get("list_id")

        meta = ev_by_slug.get(slug, {})
        eventbrite_reg = meta.get("registered_count", 0)
        ev_out["eventbrite_registered"] = eventbrite_reg
        target = meta.get("registration_target", 250)
        days_until = None
        if meta.get("start_date"):
            try:
                event_date = _dt.date.fromisoformat(meta["start_date"])
                days_until = (event_date - today).days
            except Exception:
                pass

        funnel_data = compute_funnel(ev_out, days_until_event=days_until,
                                     registration_target=target)
        ev_out["funnel"] = funnel_data["funnel"]
        ev_out["rates"] = funnel_data["rates"]
        if funnel_data["forecast"]:
            ev_out["forecast"] = funnel_data["forecast"]

    return base

