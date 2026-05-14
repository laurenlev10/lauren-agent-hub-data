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

def fetch_meta_pixel_events(start_date: str, end_date: str, slugs: list = None) -> dict:
    """Fetch ad performance per event from Meta Marketing Insights.

    Returns per-slug dicts mirroring the TikTok shape so stats.html.tpl can
    render Meta and TikTok side-by-side without special-casing.

    Returns dict keyed by event-slug with:
      - meta_spend, meta_revenue, meta_impressions, meta_clicks,
        meta_landing_page_views, meta_conversions
      - meta_ctr, meta_cpc, meta_cpm, meta_cost_per_lpv (derived per-event)
      - meta_top_ads: [{ad_id, ad_name, spend, impressions, clicks, lpv}]
                     sorted by LPV desc, max 5

    Lauren's Meta campaign names (e.g. "Traffic English 2026 Cleveland, OH",
    "NEW Reel 2026 Roseville, MN", "Traffic Spanish 2026 Milwaukee, WI - Copy")
    don't follow a kebab-slug convention. We match by city+year using the same
    matcher as TikTok (_match_tiktok_slug). Ads whose name doesn't contain a
    recognizable city+year are skipped rather than mis-attributed.

    Rewritten 2026-05-13 — previous version used `slug = name.split("_")[0]`
    which never matched Lauren's naming, returning 0 for every event despite
    $4,897 of real spend in last 7d. Also fetched only `spend, actions,
    action_values` — missing impressions/clicks/CTR/CPL entirely. See
    CLAUDE.md change-log for full incident notes.
    """
    token = _os.environ.get("META_PAGE_TOKEN")
    ad_account = _os.environ.get("META_AD_ACCOUNT_ID")
    if not token or not ad_account:
        print("  ⚠ meta: token or ad account ID not set — skipping")
        return {}

    slugs = slugs or []

    url = f"https://graph.facebook.com/v25.0/{ad_account}/insights"
    params = {
        "fields": "campaign_name,adset_name,ad_name,ad_id,spend,impressions,clicks,ctr,cpc,cpm,actions,action_values",
        "time_range": _json.dumps({"since": start_date, "until": end_date}),
        "level": "ad",
        "limit": "500",
        "access_token": token,
    }
    req = _urlreq.Request(f"{url}?{_urlparse.urlencode(params)}")
    try:
        with _urlreq.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode())
    except _urlerr.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        print(f"  ⚠ meta query failed (HTTP {e.code}): {body}")
        return {}
    except Exception as e:
        print(f"  ⚠ meta query failed: {e}")
        return {}

    out = {}
    for ad in data.get("data", []):
        combined = " ".join([
            str(ad.get("campaign_name", "")),
            str(ad.get("adset_name", "")),
            str(ad.get("ad_name", "")),
        ])
        slug = _match_tiktok_slug(combined, slugs)
        if not slug:
            # No event-slug match in the name — skip rather than guess.
            continue
        ev = out.setdefault(slug, {
            "meta_spend": 0.0,
            "meta_revenue": 0.0,
            "meta_impressions": 0,
            "meta_clicks": 0,
            "meta_landing_page_views": 0,
            "meta_conversions": 0,
            "meta_top_ads": [],
        })
        spend = float(ad.get("spend", 0) or 0)
        imp = int(float(ad.get("impressions", 0) or 0))
        clk = int(float(ad.get("clicks", 0) or 0))
        lpv = 0
        for a in ad.get("actions", []) or []:
            at = a.get("action_type")
            v = int(float(a.get("value", 0) or 0))
            if at == "landing_page_view":
                lpv += v
            elif at in ("lead", "complete_registration"):
                ev["meta_conversions"] += v
        for a in ad.get("action_values", []) or []:
            if a.get("action_type") == "lead":
                ev["meta_revenue"] += float(a.get("value", 0) or 0)
        ev["meta_spend"] += spend
        ev["meta_impressions"] += imp
        ev["meta_clicks"] += clk
        ev["meta_landing_page_views"] += lpv
        ev["meta_top_ads"].append({
            "ad_id": str(ad.get("ad_id", "")),
            "ad_name": str(ad.get("ad_name", ""))[:60],
            "campaign_name": str(ad.get("campaign_name", ""))[:60],
            "spend": round(spend, 2),
            "impressions": imp,
            "clicks": clk,
            "lpv": lpv,
        })

    # Compute derived metrics + sort top_ads
    for slug, ev in out.items():
        imp = ev["meta_impressions"]; clk = ev["meta_clicks"]
        spend = ev["meta_spend"]; lpv = ev["meta_landing_page_views"]
        ev["meta_ctr"] = round(clk / imp * 100, 2) if imp else 0
        ev["meta_cpc"] = round(spend / clk, 3) if clk else 0
        ev["meta_cpm"] = round(spend / imp * 1000, 2) if imp else 0
        ev["meta_cost_per_lpv"] = round(spend / lpv, 3) if lpv else 0
        # Sort by CPL ascending — Lauren's 2026-05-13 PM directive: "תדרג כל
        # מודעה לפי המצליחה ביותר לפחות". Ranking tiers:
        #   Tier 0: meaningful data (LPV >= 100 AND spend >= $20) — ranked by CPL
        #   Tier 1: some data (LPV >= 20)                          — ranked by CPL
        #   Tier 2: barely any data                                — ranked by spend desc
        # Without tiers, a $3 ad with 25 LPV (random luck) outranks a $794 ad
        # with 4,161 LPV. Lauren wants real winners, not statistical noise.
        def _ad_rank(a):
            lpv = a.get("lpv", 0)
            spend = a.get("spend", 0)
            cpl = spend / lpv if lpv else 999
            if lpv >= 100 and spend >= 20:
                return (0, cpl)
            if lpv >= 20:
                return (1, cpl)
            return (2, -spend)
        ev["meta_top_ads"].sort(key=_ad_rank)
        ev["meta_top_ads"] = ev["meta_top_ads"][:5]
    return out


# ---------------------------------------------------------------------------
# TikTok Business API
# ---------------------------------------------------------------------------

def _match_tiktok_slug(combined_name: str, slugs: list) -> str | None:
    """Match a TikTok campaign/adgroup/ad name to one of the event slugs.

    Lauren's TikTok campaign names don't follow a slug convention — they're
    descriptive ("Traffic Best Post", "New Link of Roseville, MN 2026 Leads",
    "Copy 1 of Roseville, MN 2026 Traffic"). So we match by looking for
    BOTH the city name AND the year inside the combined campaign+adgroup+ad
    name string. The first slug whose city+year both appear wins.

    Returns None if no slug matches (caller should skip the row).
    """
    name_lower = (combined_name or "").lower()
    if not name_lower:
        return None
    # Try exact slug substring first (e.g., "roseville-mn-2026")
    for slug in slugs:
        if slug.lower() in name_lower:
            return slug
    # Then city + year (handles "Roseville, MN 2026" — note: comma between city and state)
    for slug in slugs:
        parts = slug.split("-")
        if len(parts) >= 3:
            city = parts[0].replace("_", " ")
            year = parts[-1]
            if city in name_lower and year in name_lower:
                return slug
    return None


def fetch_tiktok_pixel_events(start_date: str, end_date: str, slugs: list = None) -> dict:
    """Fetch TikTok ad performance per event.

    Returns per-slug dicts with:
      - tiktok_spend, tiktok_impressions, tiktok_clicks, tiktok_landing_page_views,
        tiktok_conversions, tiktok_complete_payment
      - tiktok_ctr, tiktok_cpc, tiktok_cpm, tiktok_cost_per_lpv (derived)
      - tiktok_top_ads: [{ad_id, ad_name, spend, impressions, clicks, lpv}] sorted by LPV desc, max 5

    Requires Marketing API access (status: PENDING as of 2026-05-12 — see CLAUDE.md
    IRON RULE about TikTok ticket). Returns {} gracefully if token absent.
    """
    token = _os.environ.get("TIKTOK_ACCESS_TOKEN")
    advertiser_id = _os.environ.get("TIKTOK_ADVERTISER_ID")
    if not token or not advertiser_id:
        print("  ⚠ tiktok: token or advertiser ID not set — skipping (API access pending)")
        return {}

    slugs = slugs or []
    url = "https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/"
    params = {
        "advertiser_id": advertiser_id,
        "report_type": "BASIC",
        "data_level": "AUCTION_AD",
        "dimensions": _json.dumps(["ad_id", "campaign_name", "adgroup_name", "ad_name"]),
        # Richer metrics so the stats page can show CTR/CPL/CPM per event,
        # not just spend. landing_page_view is critical because that's the
        # optimization event Lauren picked in the Traffic campaign (2026-05-12).
        "metrics": _json.dumps([
            "spend", "impressions", "clicks", "ctr", "cpc", "cpm",
            "conversion", "cost_per_conversion", "conversion_rate",
            "landing_page_view", "cost_per_landing_page_view",
            "complete_payment",
        ]),
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
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        print(f"  ⚠ tiktok query failed (HTTP {e.code}): {body}")
        return {}
    except Exception as e:
        print(f"  ⚠ tiktok query failed: {e}")
        return {}

    out = {}
    for row in data.get("data", {}).get("list", []):
        meta = row.get("dimensions", {})
        metrics = row.get("metrics", {})
        combined = " ".join([
            str(meta.get("campaign_name", "")),
            str(meta.get("adgroup_name", "")),
            str(meta.get("ad_name", "")),
        ])
        slug = _match_tiktok_slug(combined, slugs)
        if not slug:
            # No event-slug match in the name — skip rather than guess.
            continue
        ev = out.setdefault(slug, {
            "tiktok_spend": 0.0,
            "tiktok_impressions": 0,
            "tiktok_clicks": 0,
            "tiktok_landing_page_views": 0,
            "tiktok_conversions": 0,
            "tiktok_complete_payment": 0,
            "tiktok_top_ads": [],
        })
        spend = float(metrics.get("spend", 0))
        imp = int(float(metrics.get("impressions", 0)))
        clk = int(float(metrics.get("clicks", 0)))
        lpv = int(float(metrics.get("landing_page_view", 0)))
        conv = int(float(metrics.get("conversion", 0)))
        cp = int(float(metrics.get("complete_payment", 0)))
        ev["tiktok_spend"] += spend
        ev["tiktok_impressions"] += imp
        ev["tiktok_clicks"] += clk
        ev["tiktok_landing_page_views"] += lpv
        ev["tiktok_conversions"] += conv
        ev["tiktok_complete_payment"] += cp
        ev["tiktok_top_ads"].append({
            "ad_id": str(meta.get("ad_id", "")),
            "ad_name": str(meta.get("ad_name", ""))[:60],
            "spend": round(spend, 2),
            "impressions": imp,
            "clicks": clk,
            "lpv": lpv,
        })

    # Derived metrics + sort top ads by LPV (proxy for "best creative" per event)
    for slug, ev in out.items():
        imp = ev["tiktok_impressions"]
        clk = ev["tiktok_clicks"]
        lpv = ev["tiktok_landing_page_views"]
        spend = ev["tiktok_spend"]
        ev["tiktok_ctr"] = round(clk / imp * 100, 2) if imp else 0
        ev["tiktok_cpc"] = round(spend / clk, 2) if clk else 0
        ev["tiktok_cpm"] = round(spend / imp * 1000, 2) if imp else 0
        ev["tiktok_cost_per_lpv"] = round(spend / lpv, 2) if lpv else 0
        ev["tiktok_top_ads"].sort(key=lambda x: x["lpv"], reverse=True)
        ev["tiktok_top_ads"] = ev["tiktok_top_ads"][:5]
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
    meta = fetch_meta_pixel_events(start_date, end_date, slugs=slugs)
    tt = fetch_tiktok_pixel_events(start_date, end_date, slugs=slugs)

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

        # Rich TikTok metrics (added 2026-05-12 — feeds the Paid Acquisition section
        # in stats.html.tpl). All zeros when API token absent or no campaigns matched.
        ev["tiktok"] = {
            "spend":              t.get("tiktok_spend", 0),
            "impressions":        t.get("tiktok_impressions", 0),
            "clicks":             t.get("tiktok_clicks", 0),
            "landing_page_views": t.get("tiktok_landing_page_views", 0),
            "conversions":        t.get("tiktok_conversions", 0),
            "ctr":                t.get("tiktok_ctr", 0),
            "cpc":                t.get("tiktok_cpc", 0),
            "cpm":                t.get("tiktok_cpm", 0),
            "cost_per_lpv":       t.get("tiktok_cost_per_lpv", 0),
            "top_ads":            t.get("tiktok_top_ads", []),
        }
        # Mirror Meta into the same shape so stats.html.tpl can render both
        # platforms side-by-side without special-casing.
        ev["meta"] = {
            "spend":   m.get("meta_spend", 0),
            "revenue": m.get("meta_revenue", 0),
            # impressions/clicks/ctr/cpl populated by fetch_meta_pixel_events when
            # Meta API returns them; today the function only returns spend+revenue,
            # so the rest stay zero. Same shape as tiktok = easy to extend later.
            "impressions":        m.get("meta_impressions", 0),
            "clicks":             m.get("meta_clicks", 0),
            "landing_page_views": m.get("meta_landing_page_views", 0),
            "ctr":                m.get("meta_ctr", 0),
            "cpc":                m.get("meta_cpc", 0),
            "cpm":                m.get("meta_cpm", 0),
            "cost_per_lpv":       m.get("meta_cost_per_lpv", 0),
            "top_ads":            m.get("meta_top_ads", []),
        }
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
        url = f"https://app2.simpletexting.com/v2/api/contact-lists/{list_id}"
        req = _urlreq.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with _urlreq.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            active = data.get("activeContactsCount", 0)
            total = data.get("totalContactsCount", 0)
            unsub = data.get("unsubscribedContactsCount", 0)
            out[slug] = {
                "list_size": int(active),       # primary metric — active subscribers
                "total_count": int(total),
                "unsub_count": int(unsub),
                "list_id": list_id,
                "list_name": data.get("name"),
                "fetched_at": fetched_at,
            }
            list_name = data.get("name", "?")
            print(f"  ✓ simpletexting [{slug}]: {active} active / {total} total ({list_name})")
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


def extract_eventbrite_stats_from_launch_dashboard(html_path) -> dict:
    """Parse EVENTBRITE_STATS = {...}; from launch_dashboard.html.

    Returns: {<city-slug>-<start_date>: {eventId, registrations, capacity, weekly_delta, history, updated_at}}
    """
    import re
    p = _Path(html_path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.search(r"const EVENTBRITE_STATS\s*=\s*(\{.*?\});", text, re.DOTALL)
    if not m:
        return {}
    try:
        return _json.loads(m.group(1))
    except Exception as e:
        print(f"  ⚠ EVENTBRITE_STATS parse failed: {e}")
        return {}

def _extract_const_block(text: str, var_name: str) -> dict:
    """Generic balanced-bracket extractor for `const VAR = {...};` in JS source."""
    import re
    m = re.search(rf"const {var_name}\s*=\s*", text)
    if not m:
        return {}
    start = m.end()
    depth, in_str, esc = 0, False, False
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1; break
    raw = text[start:end]
    raw = re.sub(r",(\s*[\]}])", r"\1", raw)
    try:
        return _json.loads(raw)
    except Exception:
        return {}


def extract_schedule_from_launch_dashboard(html_path) -> dict:
    """Parse SCHEDULE = {year: [events]}; from launch_dashboard.html.

    Returns dict by year. Each event has city, state, start_date, end_date, status, etc.
    """
    p = _Path(html_path)
    if not p.exists():
        return {}
    return _extract_const_block(p.read_text(encoding="utf-8"), "SCHEDULE")


def extract_list_stats_from_launch_dashboard(html_path) -> dict:
    """Parse LIST_STATS = {evkey: {active, total, daily_delta, history}}; from launch_dashboard.html."""
    p = _Path(html_path)
    if not p.exists():
        return {}
    return _extract_const_block(p.read_text(encoding="utf-8"), "LIST_STATS")


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


def compute_funnel(ev_data: dict, days_until_event=None, registration_target: int = 250,
                   eventbrite_history: list = None) -> dict:
    """Build {funnel, rates, forecast} block for one event.

    Forecast is calculated against Eventbrite registrations (commit-to-attend) — NOT against
    SMS list size, since SMS lists accumulate marketing reach over many events.
    """
    views_total = (ev_data.get("views") or {}).get("total", 0)
    conv_total = (ev_data.get("conversions") or {}).get("total", 0)
    sms_reg = ev_data.get("sms_registered", 0)
    eventbrite_reg = ev_data.get("eventbrite_registered", 0)
    eventbrite_history = eventbrite_history or ev_data.get("eventbrite_history") or []
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

    # Forecast: project Eventbrite registrations against capacity target
    forecast = None
    if days_until_event is not None and days_until_event > 0 and eventbrite_reg >= 0:
        # Use history to compute daily rate if available; else fallback to assumption
        daily_rate = 0
        if len(eventbrite_history) >= 2:
            try:
                latest = eventbrite_history[-1]
                earlier = eventbrite_history[0]
                lat_date = _dt.date.fromisoformat(latest["date"])
                ear_date = _dt.date.fromisoformat(earlier["date"])
                day_span = max(1, (lat_date - ear_date).days)
                reg_delta = max(0, latest["registrations"] - earlier["registrations"])
                daily_rate = reg_delta / day_span
            except Exception:
                daily_rate = 0
        if daily_rate == 0 and eventbrite_reg > 0 and days_until_event < 365:
            # Fallback: assume registrations accumulated over (event_window - days_until)
            days_so_far = max(1, 30 - days_until_event) if days_until_event < 30 else 14
            daily_rate = eventbrite_reg / days_so_far

        projected_total = int(eventbrite_reg + daily_rate * days_until_event)
        forecast = {
            "metric": "eventbrite_registered",
            "current": eventbrite_reg,
            "daily_rate": round(daily_rate, 2),
            "projected_total": projected_total,
            "target": registration_target,
            "days_remaining": days_until_event,
            "status": "on_track" if projected_total >= registration_target else "behind",
            "gap": max(0, registration_target - projected_total),
        }

    return {"funnel": funnel, "rates": rates, "forecast": forecast}


def aggregate_with_funnel(slugs: list, events: list, setups: dict,
                          eventbrite_stats: dict = None,
                          start_date: str = None, end_date: str = None) -> dict:
    """Level 2 aggregator — combines all sources + SimpleTexting + funnel + forecast."""
    base = aggregate_for_events(slugs, start_date=start_date, end_date=end_date)
    slug_to_list = map_setups_to_slugs(setups, events)
    sms_data = fetch_simpletexting_list_sizes(slug_to_list)
    eventbrite_stats = eventbrite_stats or {}

    today = _dt.date.today()
    ev_by_slug = {}
    for ev in events:
        city = (ev.get("city") or "").lower().replace(" ", "-")
        state = (ev.get("state") or "").lower()
        year = (ev.get("start_date") or "")[:4]
        start_date_full = ev.get("start_date") or ""
        ev_by_slug[f"{city}-{state}-{year}"] = ev
        ev["_evkey"] = f"{city}-{start_date_full}"  # for eventbrite_stats lookup

    for slug, ev_out in base.get("events", {}).items():
        sms = sms_data.get(slug, {})
        ev_out["sms_registered"] = sms.get("list_size", 0)
        ev_out["sms_total_count"] = sms.get("total_count", 0)
        ev_out["sms_list_id"] = sms.get("list_id")
        ev_out["sms_list_name"] = sms.get("list_name")

        meta = ev_by_slug.get(slug, {})
        evkey = meta.get("_evkey")
        ebs = eventbrite_stats.get(evkey, {}) if evkey else {}

        # Eventbrite data — registrations is the actual funnel-end commit metric
        eventbrite_reg = ebs.get("registrations", 0)
        eventbrite_cap = ebs.get("capacity", 250)
        eventbrite_history = ebs.get("history", [])
        ev_out["eventbrite_registered"] = eventbrite_reg
        ev_out["eventbrite_capacity"] = eventbrite_cap
        ev_out["eventbrite_history"] = eventbrite_history

        target = eventbrite_cap or 250
        days_until = None
        if meta.get("start_date"):
            try:
                event_date = _dt.date.fromisoformat(meta["start_date"])
                days_until = (event_date - today).days
            except Exception:
                pass

        # Forecast uses Eventbrite registrations (not SMS list size!)
        funnel_data = compute_funnel(ev_out, days_until_event=days_until,
                                     registration_target=target,
                                     eventbrite_history=eventbrite_history)
        ev_out["funnel"] = funnel_data["funnel"]
        ev_out["rates"] = funnel_data["rates"]
        if funnel_data["forecast"]:
            ev_out["forecast"] = funnel_data["forecast"]

    return base



# ---------------------------------------------------------------------------
# Insights generator (Level 2 ext)
# ---------------------------------------------------------------------------

def fetch_all_simpletexting_lists() -> list:
    """Fetch all SimpleTexting contact lists (paginated). Returns flat list of dicts."""
    token = _os.environ.get("SIMPLETEXTING_TOKEN")
    if not token:
        return []
    out = []
    page = 0
    while True:
        url = f"https://app2.simpletexting.com/v2/api/contact-lists?size=200&page={page}"
        req = _urlreq.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with _urlreq.urlopen(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode())
            content = data.get("content", [])
            if not content:
                break
            out.extend(content)
            if len(content) < 200:
                break
            page += 1
            if page > 5:  # cap at 6 pages = 1200 lists
                break
        except Exception as e:
            print(f"  ⚠ fetch_all_simpletexting_lists page {page} failed: {e}")
            break
    return out


def find_previous_year_lists(city: str, state: str, current_year: int, all_lists: list) -> list:
    """Find prev-year SimpleTexting lists for a city.

    Requires BOTH city + state to match — Roseville MN should NOT match Roseville CA.
    Older lists that don't include state code at all are skipped to avoid false matches.

    Matched name patterns (state required):
      "Columbia, MO 2025"  /  "Columbia MO 2024"  /  "Columbia, MO Tradeshow 2023"
    """
    import re
    city_lower = city.lower()
    state_upper = state.upper()
    out = []
    for lst in all_lists:
        name = (lst.get("name") or "").strip()
        # Year extraction: 4-digit year somewhere in the name
        ym = re.search(r"\b(20\d{2})\b", name)
        if not ym:
            continue
        year = int(ym.group(1))
        if year >= current_year:
            continue
        # Both city AND state must appear (case-insensitive for city, exact uppercase for state)
        if city_lower not in name.lower():
            continue
        # State must be a separate token: " MO ", ", MO " or ", MO 2023"
        if not re.search(rf"[\s,]+{re.escape(state_upper)}\b", name):
            continue
        out.append({
            "list_id": lst.get("listId"),
            "name": name,
            "year": year,
            "active": lst.get("activeContactsCount", 0),
            "total": lst.get("totalContactsCount", 0),
        })
    out.sort(key=lambda x: x["year"], reverse=True)
    return out


def compute_real_averages(financials: dict, eventbrite_stats: dict, current_sms_data: dict) -> dict:
    """Compute YTD real averages from EVENT_FINANCIALS + EVENTBRITE_STATS + active SMS data.

    Args:
        financials: dict of past 2026 events with profit/sales {evkey: {profit, profit_pct, sales}}
        eventbrite_stats: dict of all events with {registrations, capacity, history}
        current_sms_data: dict from fetch_simpletexting_list_sizes (for upcoming events)

    Returns:
        {
          ytd_avg_sales, ytd_avg_profit, ytd_avg_profit_pct, ytd_event_count,
          avg_eventbrite_at_today (across upcoming),
          avg_sms_list_size (across upcoming)
        }
    """
    out = {
        "ytd_event_count": 0,
        "ytd_avg_sales": 0,
        "ytd_avg_profit": 0,
        "ytd_avg_profit_pct": 0,
        "avg_eventbrite_upcoming": 0,
        "avg_sms_list_size": 0,
    }
    if financials:
        ev_count = len(financials)
        sales = [v.get("sales", 0) for v in financials.values()]
        profits = [v.get("profit", 0) for v in financials.values()]
        pcts = [v.get("profit_pct", 0) for v in financials.values()]
        out["ytd_event_count"] = ev_count
        out["ytd_avg_sales"] = round(sum(sales) / ev_count) if ev_count else 0
        out["ytd_avg_profit"] = round(sum(profits) / ev_count) if ev_count else 0
        out["ytd_avg_profit_pct"] = round(sum(pcts) / ev_count, 1) if ev_count else 0

    # Upcoming events Eventbrite avg
    if eventbrite_stats:
        upcoming = [v.get("registrations", 0) for k, v in eventbrite_stats.items()
                    if v.get("registrations", 0) > 0]
        if upcoming:
            out["avg_eventbrite_upcoming"] = round(sum(upcoming) / len(upcoming))
            out["_n_upcoming_eb"] = len(upcoming)

    # SMS avg across active events
    if current_sms_data:
        actives = [v.get("list_size", 0) for v in current_sms_data.values() if v.get("list_size")]
        if actives:
            out["avg_sms_list_size"] = round(sum(actives) / len(actives))
            out["_n_active_sms"] = len(actives)

    return out


def is_event_weekend(date, all_events: list) -> bool:
    """Returns True if `date` falls on Fri/Sat/Sun OF any event (any city).

    Used to exclude POS-driven SMS spikes from marketing growth rate.
    """
    if hasattr(date, "weekday"):
        wday = date.weekday()
    else:
        wday = _dt.date.fromisoformat(str(date)).weekday()
    if wday < 4:  # Mon-Thu
        return False
    for ev in all_events:
        try:
            sd = _dt.date.fromisoformat(ev.get("start_date") or "")
            ed = _dt.date.fromisoformat(ev.get("end_date") or "")
            if sd <= date <= ed:
                return True
        except Exception:
            continue
    return False


def generate_per_event_insights(slug: str, ev: dict, averages: dict,
                                  prev_year_lists: list = None,
                                  prev_snapshot: dict = None,
                                  is_live: bool = False,
                                  days_remaining: int = None) -> dict:
    """Build narrative insight + recommendations for one event.

    prev_snapshot: the last @stats snapshot (ts, eb, sms) — used for delta-since-last-report.

    Returns dict with: bucket (critical/watch/strong), narrative, recommendation
    """
    eb_reg = ev.get("eventbrite_registered", 0)
    eb_cap = ev.get("eventbrite_capacity", 250)
    sms_reg = ev.get("sms_registered", 0)
    forecast = ev.get("forecast", {})
    days = days_remaining if days_remaining is not None else (forecast.get("days_remaining") if forecast else None)
    avg_eb = averages.get("avg_eventbrite_upcoming", 0) or 1
    avg_sms = averages.get("avg_sms_list_size", 0) or 1

    # Bucket logic
    pct_of_avg_eb = round(eb_reg / avg_eb * 100) if avg_eb else 0
    pct_of_avg_sms = round(sms_reg / avg_sms * 100) if avg_sms else 0

    # Bucket by days remaining (no longer uses % of avg)
    if days is None:
        bucket = "watch"
    elif days <= 3:
        bucket = "imminent"
    elif days <= 14:
        bucket = "soon"
    else:
        bucket = "future"

    # YoY comparison
    yoy_text = ""
    if prev_year_lists:
        last = prev_year_lists[0]
        delta_pct = round((sms_reg - last["active"]) / last["active"] * 100) if last["active"] else 0
        sign = "+" if delta_pct >= 0 else ""
        yoy_text = f"vs {last['year']}: {last['active']} ({sign}{delta_pct}% השנה)"

    # Narrative — turn "columbia-mo-2026" into "Columbia, MO"
    parts = slug.rsplit("-", 2)
    if len(parts) == 3:
        city_name = parts[0].replace("-", " ").title() + ", " + parts[1].upper()
    else:
        city_name = slug.replace("-", " ").title()
    days_str = f"{days}d לאירוע" if days is not None else ""
    if is_live:
        narrative = "🟢 LIVE — " + city_name + " (האירוע פעיל עכשיו!)"
    else:
        narrative = city_name + (f" ({days_str})" if days_str else "")
    # Delta since previous report
    eb_delta = None
    sms_delta = None
    if prev_snapshot:
        prev_eb = prev_snapshot.get("eb")
        prev_sms = prev_snapshot.get("sms")
        if prev_eb is not None:
            eb_delta = eb_reg - prev_eb
        if prev_sms is not None:
            sms_delta = sms_reg - prev_sms

    def _fmt_delta(d):
        if d is None: return ""
        if d > 0:    return f" (+{d} מאז הפעם הקודמת)"
        if d < 0:    return f" ({d} מאז הפעם הקודמת)"
        return " (ללא שינוי)"

    eb_part = f"🎟️ Eventbrite: {eb_reg} RSVPs{_fmt_delta(eb_delta)}"
    sms_list_name = ev.get("sms_list_name") or "רשימה שנתית"
    sms_part = f"📲 SMS list ({sms_list_name}): {sms_reg:,} רשומות{_fmt_delta(sms_delta)}"

    # No active recommendations — Lauren prefers raw data + benchmarks for self-judgment
    rec = None

    return {
        "slug": slug,
        "bucket": bucket,
        "narrative": narrative,
        "eb_part": eb_part,
        "sms_part": sms_part,
        "yoy_text": yoy_text,
        "recommendation": rec,
        "is_live": is_live,
        "eb_reg": eb_reg,
        "eb_cap": eb_cap,
        "sms_reg": sms_reg,
        "pct_of_avg_eb": pct_of_avg_eb,
        "pct_of_avg_sms": pct_of_avg_sms,
        "days_remaining": days,
    }


def format_insights_sms(insights: list, averages: dict, ts: str = None) -> str:
    """Format per-event insights into a Hebrew SMS digest.

    Sections: 🚨 Critical / ⚠️ Watch / ✅ Strong / 📈 YTD averages.
    Uses 'Eventbrite N' explicitly so source is unambiguous (vs SMS list reach).
    """
    if ts is None:
        try:
            from zoneinfo import ZoneInfo
            pt_now = _dt.datetime.now(ZoneInfo("America/Los_Angeles"))
        except Exception:
            # Fallback: hardcoded PDT (May = daylight savings, UTC-7)
            pt_now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-7)))
        ts = pt_now.strftime("%b %d · %H:%M PT")
    lines = [f"🧠 @stats Insights · {ts}"]

    # Sort by days remaining (most imminent first)
    sorted_insights = sorted(insights, key=lambda i: (i.get("days_remaining") if i.get("days_remaining") is not None else 999))

    def render_event(i):
        out = [f"📍 {i['narrative']}"]
        out.append(f"  {i['eb_part']}")
        out.append(f"  {i['sms_part']}")
        if i.get("benchmark_text"):
            out.append(f"  📊 {i['benchmark_text']}")
        if i.get("yoy_text"):
            out.append(f"  📅 {i['yoy_text']}")
        return out

    for i in sorted_insights:
        lines.extend(render_event(i))
        lines.append("")  # blank line between events

    lines.append("🔗 https://laurenlev10.github.io/lauren-agent-hub-data/launch/")
    return "\n".join(lines)


def extract_event_financials_from_launch_dashboard(html_path) -> dict:
    """Parse SUMMARIES = {...}; from launch_dashboard.html (mbs-event-summary outputs).

    Uses balanced-bracket walk because regex .*? fails on nested objects, and
    strips trailing commas before json.loads since JS allows them but JSON doesn't.
    """
    import re
    p = _Path(html_path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.search(r"const SUMMARIES\s*=\s*", text)
    if not m:
        return {}
    start = m.end()
    depth, in_str, esc = 0, False, False
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1; break
    raw = text[start:end]
    # Strip trailing commas before } and ]
    raw = re.sub(r",(\s*[\]}])", r"\1", raw)
    try:
        return _json.loads(raw)
    except Exception as e:
        print(f"  ⚠ SUMMARIES parse failed: {e}")
        return {}

def get_next_n_upcoming_events(schedule: dict, n: int = 4,
                                 list_stats: dict = None) -> list:
    """Return list of next N upcoming events sorted by start_date.

    Uses SCHEDULE constant (from launch_dashboard.html) which contains ALL events
    (confirmed + tentative) for current/next year. Returns events that haven't ended yet.
    """
    today = _dt.date.today()
    all_events = []
    for year, year_events in (schedule or {}).items():
        if not isinstance(year_events, list):
            continue
        for ev in year_events:
            all_events.append(ev)

    candidates = []
    seen_evkeys = set()
    for ev in all_events:
        sd_str = ev.get("start_date")
        if not sd_str:
            continue
        try:
            sd = _dt.date.fromisoformat(sd_str)
            ed = _dt.date.fromisoformat(ev.get("end_date") or sd_str)
        except Exception:
            continue
        if ed < today:
            continue
        city = (ev.get("city") or "").lower().replace(" ", "-")
        state = (ev.get("state") or "").lower()
        year = sd_str[:4]
        if not (city and state and year):
            continue
        slug = f"{city}-{state}-{year}"
        evkey = f"{city}-{sd_str}"
        seen_evkeys.add(evkey)
        is_live = (sd <= today <= ed)
        candidates.append({
            "slug": slug,
            "evkey": evkey,
            "city": ev.get("city"),
            "state": ev.get("state"),
            "start_date": sd_str,
            "end_date": ev.get("end_date") or sd_str,
            "venue": ev.get("venue"),
            "is_live": is_live,
            "days_remaining": (sd - today).days,
        })

    # Merge in events from list_stats that aren't yet in SCHEDULE (e.g., Roseville,
    # Fort Collins added after schedule was baked). Derive state from list_name.
    if list_stats:
        import re
        for evkey, ls in list_stats.items():
            if evkey in seen_evkeys:
                continue
            # evkey shape: "city-slug-YYYY-MM-DD"
            m = re.match(r"^(.+)-(\d{4}-\d{2}-\d{2})$", evkey)
            if not m:
                continue
            city_slug, sd_str = m.group(1), m.group(2)
            try:
                sd = _dt.date.fromisoformat(sd_str)
                ed = sd + _dt.timedelta(days=2)  # default 2-day events
            except Exception:
                continue
            if ed < today:
                continue
            # Parse state from list_name like "Roseville, MN 2026"
            list_name = ls.get("list_name") or ""
            sm = re.search(r",\s*([A-Z]{2})\b", list_name)
            if not sm:
                continue
            state = sm.group(1).lower()
            year = sd_str[:4]
            slug = f"{city_slug}-{state}-{year}"
            is_live = (sd <= today <= ed)
            # Reconstruct city display name from list_name (before comma)
            display_city = list_name.split(",")[0].strip() if "," in list_name else city_slug.replace("-", " ").title()
            candidates.append({
                "slug": slug,
                "evkey": evkey,
                "city": display_city,
                "state": state.upper(),
                "start_date": sd_str,
                "end_date": ed.isoformat(),
                "venue": None,
                "is_live": is_live,
                "days_remaining": (sd - today).days,
            })

    candidates.sort(key=lambda x: x["start_date"])
    return candidates[:n]


# ---------------------------------------------------------------------------
# Time-series snapshots (for delta-since-last-report)
# ---------------------------------------------------------------------------

def load_event_timeseries(path: str = "docs/state/event_timeseries.json") -> dict:
    """Load event time-series snapshots. Shape: {events: {slug: {snapshots: [...]}}}."""
    p = _Path(path)
    if not p.exists():
        return {"events": {}}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"events": {}}


def get_previous_snapshot(timeseries: dict, slug: str) -> dict:
    """Return the LAST snapshot for `slug` (the one taken just before now)."""
    snaps = (timeseries.get("events", {}).get(slug) or {}).get("snapshots") or []
    if not snaps:
        return {}
    return snaps[-1]


def append_event_snapshot(timeseries: dict, slug: str, eb_reg: int, sms_reg: int) -> None:
    """Mutate `timeseries` to append a new snapshot for `slug`."""
    ts_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = timeseries.setdefault("events", {})
    ev = events.setdefault(slug, {"snapshots": []})
    ev["snapshots"].append({"ts": ts_iso, "eb": int(eb_reg), "sms": int(sms_reg)})
    # Keep only last 90 snapshots per event (~3 weeks at 4/day or 3 months at 1/day)
    ev["snapshots"] = ev["snapshots"][-90:]


def save_event_timeseries(timeseries: dict, path: str = "docs/state/event_timeseries.json") -> None:
    timeseries["_updated_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _Path(path).parent.mkdir(parents=True, exist_ok=True)
    _Path(path).write_text(_json.dumps(timeseries, indent=2, ensure_ascii=False), encoding="utf-8")

