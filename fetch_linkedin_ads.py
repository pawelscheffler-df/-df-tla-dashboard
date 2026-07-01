#!/usr/bin/env python3
"""
Digital Forms — LinkedIn TLA Ads Fetcher
=========================================

Fetches Cezary's Thought Leadership Ads analytics from LinkedIn's
Marketing API and writes a JSON snapshot the dashboard reads.

Runs in two modes:

  1. GitHub Actions (production, headless):
     - Reads LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, LINKEDIN_REFRESH_TOKEN
       from environment variables (mapped from repo secrets in the workflow).
     - No interactive OAuth, no local token file.

  2. Local development (optional):
     - Reads .env file if present.
     - Stores token in .linkedin_token.json so re-runs don't need fresh auth.
     - `python fetch_linkedin_ads.py --auth` runs the OAuth flow once.

Output: data/linkedin_ads.json — read by linkedin_dashboard.html.
"""

from __future__ import annotations
import argparse, json, os, sys, time, urllib.parse, webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Optional .env support for local dev. Not required in CI.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Configuration ─────────────────────────────────────────────────────────────

CLIENT_ID     = os.environ.get("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET")
# When set in CI, this overrides any local .linkedin_token.json. The workflow
# stores it as a GitHub repo secret named LINKEDIN_REFRESH_TOKEN.
ENV_REFRESH_TOKEN = os.environ.get("LINKEDIN_REFRESH_TOKEN")

TOKEN_FILE    = Path(".linkedin_token.json")
OUTPUT_FILE   = Path("data/linkedin_ads.json")
SCOPES        = "r_ads r_ads_reporting"
REDIRECT_URI  = "http://localhost:8765/callback"

API_VERSION = "202604"
API_BASE    = "https://api.linkedin.com/rest"

# Account: Digital Forms.
ACCOUNT_ID    = "519195085"

# IMPORTANT: LinkedIn renamed its hierarchy in 2023.
# What the Campaign Manager UI calls a "Campaign" is internally a Campaign
# Group; what it calls an "Ad Set" is the API's `sponsoredCampaign`. So the
# URN to filter analytics by is the AD SET ID, not the UI's Campaign ID.
#
#   908306483 = Campaign Group "Czarek TLA"        (NOT filterable in API)
#   590603543 = Ad Set "Digital Transformation"    (the right ID — use this)
CAMPAIGN_IDS  = ["590603543"]
CAMPAIGN_NAME = "Czarek TLA › Digital Transformation"
ACCOUNT_NAME  = "Digital Forms"
CURRENCY      = "PLN"

# The 7 known Cezary TLA creatives. The analytics filter narrows queries to
# just these — anything not in this list won't appear on the dashboard.
# When a new TLA ad goes live, add its ID + first ~120 chars of post text here.
AD_TEXT = {
    "1193855093": 'We stopped using the phrase "digital transformation" with clients. Not because it means nothing. But by the time…',
    "1222909983": 'The last three CEOs I spoke to this month all said some version of the same thing. "We\'ve already tried this. Spent…',
    "1211793353": "I spent 4 years trying to sell something I didn't yet know how to sell. Here's what that actually felt like and why I'm…",
    "1253506543": "There's a specific conversation I keep having with CEOs that I find genuinely unsettling. I ask them: how long does…",
    "1200419173": "A strategy is only as good as your ability to articulate it. If your team can't follow you in the same direction, the…",
    "1195167003": "Everyone's racing to build an AI strategy. Meanwhile, the same five systems still don't talk to each other. Dave just…",
    "1260782853": "Something I keep noticing when I walk through a business for the first time. The thing they've documented isn't the…",
    "1263591753": "We run our own business on the same principles we apply to clients. These are screens from our internal dashboard",
    "1271088023": "Most companies struggle to get their people to use AI. That wasn't our problem. At Digital Forms, 80-90% of the team",
    "1271088023": "The most expensive inaction is just the stuff that's too easy to live with. The cost of that normalization is real",
    "1441876993": "PE operating partners taught me something I can't unlearn. So what do they actually do after buying a company?",
}
AD_IDS = list(AD_TEXT.keys())

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_valid_token() -> str:
    """Get a valid access token. Strategy:

    1. If LINKEDIN_REFRESH_TOKEN env var is set (CI mode), use it directly to
       request a fresh access token. No token file involved.
    2. Otherwise, fall back to .linkedin_token.json (local dev mode).
    """
    if ENV_REFRESH_TOKEN:
        if not CLIENT_ID or not CLIENT_SECRET:
            sys.exit("ERROR: LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET not set")
        print("Refreshing access token from env refresh_token…")
        r = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
            "grant_type":    "refresh_token",
            "refresh_token": ENV_REFRESH_TOKEN,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        if not r.ok:
            sys.exit(f"Token refresh failed: HTTP {r.status_code} — {r.text}")
        return r.json()["access_token"]

    # Local fallback
    if not TOKEN_FILE.exists():
        sys.exit("No token. Set LINKEDIN_REFRESH_TOKEN env var, or run: python fetch_linkedin_ads.py --auth")
    token = json.loads(TOKEN_FILE.read_text())
    fetched_at = token.get("fetched_at", 0)
    expires_in = token.get("expires_in", 5183944)
    if time.time() > fetched_at + expires_in - 600:
        print("Refreshing local token…")
        r = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
            "grant_type":    "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        r.raise_for_status()
        token = r.json()
        token["fetched_at"] = int(time.time())
        TOKEN_FILE.write_text(json.dumps(token, indent=2))
    return token["access_token"]

def run_auth_flow():
    """One-time interactive OAuth flow. Writes .linkedin_token.json so future
    runs can use the refresh_token. After running this, copy the
    refresh_token value into the GitHub repo secret LINKEDIN_REFRESH_TOKEN.
    """
    import http.server, threading
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("ERROR: set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env")
    auth_code = {"value": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            auth_code["value"] = p.get("code", [None])[0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"<h2>Auth complete. Close this tab.</h2>")
        def log_message(self, *a): pass

    server = http.server.HTTPServer(("localhost", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = (f"https://www.linkedin.com/oauth/v2/authorization?response_type=code"
           f"&client_id={CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
           f"&scope={urllib.parse.quote(SCOPES)}&state=df2026")
    print(f"Opening browser…\nIf it doesn't open: {url}")
    webbrowser.open(url)
    for _ in range(240):
        if auth_code["value"]: break
        time.sleep(0.5)
    server.shutdown()
    if not auth_code["value"]:
        sys.exit("Timed out waiting for OAuth callback.")
    r = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
        "grant_type":   "authorization_code",
        "code":         auth_code["value"],
        "redirect_uri": REDIRECT_URI,
        "client_id":    CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    r.raise_for_status()
    token = r.json()
    token["fetched_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(token, indent=2))
    print("Auth successful!")
    print()
    print("=" * 60)
    print("Refresh token for GitHub secret LINKEDIN_REFRESH_TOKEN:")
    print(token["refresh_token"])
    print("=" * 60)

# ── API ───────────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization":             f"Bearer {token}",
        "LinkedIn-Version":          API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }

_ANALYTICS_FIELDS = ",".join([
    "impressions", "clicks", "landingPageClicks",
    "costInLocalCurrency", "oneClickLeads", "externalWebsiteConversions",
    "dateRange", "pivotValues",
])

def _restli_date_range(start, end) -> str:
    """Rest.li compact syntax — parens, colons, commas must NOT be URL-encoded."""
    return (f"(start:(year:{start.year},month:{start.month},day:{start.day}),"
            f"end:(year:{end.year},month:{end.month},day:{end.day}))")

def _restli_urn_list(urns: list[str]) -> str:
    """List(urn%3Ali%3A...,...) — colons inside URNs must be percent-encoded."""
    encoded = [u.replace(":", "%3A") for u in urns]
    return f"List({','.join(encoded)})"

def _call_analytics(token: str, query_string: str) -> list:
    """Raw GET against /rest/adAnalytics. We manually build the query string
    instead of using requests' params= because that would percent-encode the
    parens/colons/commas that Rest.li needs literal inside dateRange=(…).
    """
    url = f"{API_BASE}/adAnalytics?{query_string}"
    resp = requests.get(url, headers=_headers(token))
    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", 60)))
        return _call_analytics(token, query_string)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json().get("elements", [])

def fetch_daily_creative(token: str, start_date, end_date) -> list:
    """Fetch DAILY × CREATIVE rows, filtered to the known TLA ad IDs.

    This single query gives the dashboard everything it needs — one row per
    (day, ad) — from which summary totals, per-ad rollups, and daily trend
    are all derived client-side.

    Falls back to campaign URN and then account URN if creatives filter fails.
    """
    date_range = _restli_date_range(start_date, end_date)
    base = (f"q=analytics&pivot=CREATIVE&timeGranularity=DAILY"
            f"&dateRange={date_range}&fields={_ANALYTICS_FIELDS}")

    creative_urns = [f"urn:li:sponsoredCreative:{aid}" for aid in AD_IDS]
    campaign_urns = [f"urn:li:sponsoredCampaign:{cid}" for cid in CAMPAIGN_IDS]
    account_urn   = [f"urn:li:sponsoredAccount:{ACCOUNT_ID}"]

    attempts = [
        (base + f"&creatives={_restli_urn_list(creative_urns)}", "creatives"),
        (base + f"&campaigns={_restli_urn_list(campaign_urns)}", "campaigns"),
        (base + f"&accounts={_restli_urn_list(account_urn)}",    "accounts"),
    ]

    for qs, label in attempts:
        try:
            rows = _call_analytics(token, qs)
            warn = " ← contains all account campaigns" if label == "accounts" else ""
            print(f"  ✓ DAILY×CREATIVE via {label}: {len(rows)} rows{warn}")
            if rows:
                return rows
        except Exception as e:
            print(f"  ✗ DAILY×CREATIVE via {label}: {e}")

    return []

# ── Transform ─────────────────────────────────────────────────────────────────

def _s_int(v) -> int:
    try: return int(float(v))
    except (TypeError, ValueError): return 0

def _s_float(v) -> float:
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def rows_to_raw(rows: list) -> list[dict]:
    """Convert API rows into the {date, adId, impressions, clicks, lpClicks, spend}
    format the dashboard's filter engine consumes. Drops rows for ads not in AD_IDS.
    """
    out = []
    for r in rows:
        pvs = r.get("pivotValues") or []
        if not pvs: continue
        ad_id = str(pvs[0]).split(":")[-1]
        if ad_id not in AD_TEXT:
            continue
        dr = r.get("dateRange", {}).get("start", {})
        try:
            date_str = f"{int(dr['year']):04d}-{int(dr['month']):02d}-{int(dr['day']):02d}"
        except (KeyError, ValueError, TypeError):
            continue
        out.append({
            "date":        date_str,
            "adId":        ad_id,
            "impressions": _s_int(r.get("impressions")),
            "clicks":      _s_int(r.get("clicks")),
            "lpClicks":    _s_int(r.get("landingPageClicks")),
            "spend":       round(_s_float(r.get("costInLocalCurrency")), 2),
        })
    return sorted(out, key=lambda x: (x["date"], x["adId"]))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", action="store_true",
                        help="One-time OAuth flow (local only; prints refresh token).")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of history to pull (default 90, LinkedIn caps at ~186).")
    parser.add_argument("--output", type=str, default=str(OUTPUT_FILE))
    args = parser.parse_args()

    if args.auth:
        run_auth_flow()
        return

    print("Digital Forms — LinkedIn Ads Fetcher")
    print("=" * 60)
    token = get_valid_token()
    print("✓ Token valid")

    end_date   = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=args.days)
    print(f"Period : {start_date} → {end_date} ({args.days} days)")
    print(f"Account: {ACCOUNT_NAME} ({ACCOUNT_ID})")
    print(f"Filter : {len(AD_IDS)} creative IDs")
    print()
    print("Fetching analytics…")
    rows = fetch_daily_creative(token, start_date, end_date)
    if not rows:
        sys.exit("ERROR: no analytics rows returned from any fallback.")

    raw = rows_to_raw(rows)
    print(f"  → {len(raw)} day×ad rows (filtered to known TLA creatives)")

    totals = {
        "impressions": sum(r["impressions"] for r in raw),
        "clicks":      sum(r["clicks"]      for r in raw),
        "lpClicks":    sum(r["lpClicks"]    for r in raw),
        "spend":       round(sum(r["spend"] for r in raw), 2),
    }
    full_range = (
        {"start": min(r["date"] for r in raw), "end": max(r["date"] for r in raw)}
        if raw else {"start": str(start_date), "end": str(end_date)}
    )
    output = {
        "meta": {
            "fetchedAt":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "accountId":    ACCOUNT_ID,
            "accountName":  ACCOUNT_NAME,
            "currency":     CURRENCY,
            "campaignName": CAMPAIGN_NAME,
            "fullRange":    full_range,
            "adCount":      len({r["adId"] for r in raw}),
            "rowCount":     len(raw),
        },
        "adText":  AD_TEXT,
        "rawRows": raw,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, separators=(",", ":")))
    print(f"\n✓ Written to {out_path}")
    print()
    print(f"Totals ({full_range['start']} → {full_range['end']}):")
    print(f"  Impressions: {totals['impressions']:>8,}")
    print(f"  Clicks     : {totals['clicks']:>8,}")
    print(f"  LP Clicks  : {totals['lpClicks']:>8,}")
    print(f"  Spend      : {totals['spend']:>8,.2f} {CURRENCY}")

if __name__ == "__main__":
    main()
