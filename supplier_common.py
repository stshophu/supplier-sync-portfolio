#!/usr/bin/env python3
"""
supplier_common.py — shared Shopify client + helpers for the Supplier sync jobs.

Mirrors the conventions in your rewix_repricer.py:
  - SHOPIFY_TOKEN / SHOPIFY_DOMAIN from environment (never hardcoded)
  - REST Admin API 2024-10 with 429 / 5xx retry
  - cursor pagination via Link header
Adds a small GraphQL helper so Tier 2 can resolve a variant by SKU cheaply.

Required env vars:
  SHOPIFY_DOMAIN        e.g. your-store.myshopify.com
  SHOPIFY_TOKEN         Admin API access token
Optional:
  SUPPLIER_LOCATION_ID    inventory location id (numeric). If unset, the first
                        active location is used.
"""

import os
import sys
import io
import time
import json
import requests
import pandas as pd

API_VERSION = "2024-10"

DOMAIN = os.environ.get("SHOPIFY_DOMAIN")
TOKEN = os.environ.get("SHOPIFY_TOKEN")
if not DOMAIN or not TOKEN:
    sys.exit("Missing SHOPIFY_DOMAIN or SHOPIFY_TOKEN environment variable.")

API = f"https://{DOMAIN}/admin/api/{API_VERSION}"
GRAPHQL = f"{API}/graphql.json"
HEADERS = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}


def log(msg):
    print(msg, flush=True)


def _is_excel(path_or_url):
    return str(path_or_url).lower().split("?")[0].endswith((".xlsx", ".xls"))


def _looks_like_excel_bytes(content):
    """.xlsx files are zip archives and start with the 'PK' zip signature.
    Some hosts (e.g. Google Drive direct-download links) strip the file
    extension from the URL, so we can't always tell from the URL alone."""
    return content[:4] == b"PK\x03\x04"


def load_feed(src, **read_csv_kwargs):
    """Read a CSV or Excel feed from an http(s) URL (with retry/timeout) or a
    local path.

    Production (GitHub Actions) passes an S3/HTTP URL via the SUPPLIER_*_FEED
    secrets; local runs pass a file path. read_csv kwargs (sep, dtype, header)
    are forwarded unchanged for CSV sources, so per-feed quirks (e.g. PriceFeed's
    sep=';') still work. Excel sources (.xlsx/.xls) ignore CSV-only kwargs
    like `sep` and are read with pandas.read_excel instead.
    """
    excel = _is_excel(src)
    s = str(src)
    if s.startswith(("http://", "https://")):
        last = None
        for attempt in range(5):
            try:
                r = requests.get(s, timeout=60)
                if r.status_code == 200:
                    log(f"  load_feed: HTTP 200, {len(r.content)} bytes, "
                        f"content-type={r.headers.get('content-type')}")
                    excel = excel or _looks_like_excel_bytes(r.content)
                    if excel:
                        try:
                            df = pd.read_excel(io.BytesIO(r.content), dtype=read_csv_kwargs.get("dtype"))
                        except Exception as parse_exc:
                            preview = r.content[:300]
                            log(f"  load_feed: FAILED to parse as Excel ({parse_exc}). "
                                f"First 300 bytes: {preview!r}")
                            raise
                        if df.empty:
                            preview = r.content[:300]
                            log(f"  load_feed: parsed as Excel but got 0 rows. "
                                f"First 300 bytes of raw response: {preview!r}")
                        return df
                    csv_df = pd.read_csv(io.StringIO(r.text), **read_csv_kwargs)
                    if len(csv_df.columns) <= 1:
                        log(f"  load_feed: CSV parsed into only "
                            f"{len(csv_df.columns)} column(s) -- likely wrong "
                            f"separator. Columns: {list(csv_df.columns)}. "
                            f"First 200 chars: {r.text[:200]!r}")
                    return csv_df
                if r.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Failed to fetch feed after retries: {s} ({last})")
    if excel:
        return pd.read_excel(s, dtype=read_csv_kwargs.get("dtype"))
    return pd.read_csv(s, **read_csv_kwargs)



def req(method, url, **kw):
    """REST request with rate-limit + 5xx retry (same as rewix_repricer)."""
    for attempt in range(10):
        r = requests.request(method, url, headers=HEADERS, timeout=30, **kw)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", 2 ** min(attempt, 5)))
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r
    r.raise_for_status()
    return r


def graphql(query, variables=None):
    """GraphQL request with rate-limit retry. Returns the 'data' dict."""
    payload = {"query": query, "variables": variables or {}}
    for attempt in range(6):
        r = requests.post(GRAPHQL, headers=HEADERS, json=payload, timeout=30)
        if r.status_code == 401:
            sys.exit("Shopify rejected the request as unauthorized (401) -- "
                     "check that SHOPIFY_TOKEN is a real, current Admin API "
                     "access token (not a placeholder) and matches SHOPIFY_DOMAIN.")
        if r.status_code == 429:
            time.sleep(2)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        body = r.json()
        errors = body.get("errors")
        # GraphQL throttling shows up inside the body, not the status code.
        # Some error responses (e.g. auth failures) return 'errors' as a
        # plain string rather than a list of {message, extensions} dicts --
        # guard against that shape before assuming dict-like entries.
        if errors:
            if isinstance(errors, list) and all(isinstance(e, dict) for e in errors):
                throttled = any(
                    (e.get("extensions", {}) or {}).get("code") == "THROTTLED"
                    for e in errors
                )
                if throttled:
                    time.sleep(2 ** attempt)
                    continue
            else:
                sys.exit(f"Shopify GraphQL error (not throttling): {errors}")
        return body.get("data", {})
    return {}


# ── location ────────────────────────────────────────────────────────────
_LOCATION_ID = None

def get_location_id():
    """Return the inventory location id to use (env override or first active)."""
    global _LOCATION_ID
    if _LOCATION_ID:
        return _LOCATION_ID
    env = os.environ.get("SUPPLIER_LOCATION_ID")
    if env:
        _LOCATION_ID = int(env)
        return _LOCATION_ID
    r = req("GET", f"{API}/locations.json")
    locs = r.json().get("locations", [])
    active = [l for l in locs if l.get("active")]
    if not active:
        sys.exit("No active Shopify location found; set SUPPLIER_LOCATION_ID.")
    _LOCATION_ID = active[0]["id"]
    log(f"Using location: {active[0]['name']} ({_LOCATION_ID})")
    return _LOCATION_ID


# ── variant lookup by SKU (GraphQL) ─────────────────────────────────────
def find_variant_by_sku(sku):
    """Return dict with variant + product + inventory ids for a SKU, or None.

    Used by Tier 2 to locate the product Tier 1 created, and by Tier 1 to
    decide create-vs-update.
    """
    query = """
    query($q: String!) {
      productVariants(first: 1, query: $q) {
        edges {
          node {
            id
            legacyResourceId
            sku
            price
            compareAtPrice
            inventoryItem { id legacyResourceId unitCost { amount } }
            product { id legacyResourceId status tags }
          }
        }
      }
    }"""
    data = graphql(query, {"q": f"sku:{sku}"})
    edges = (((data or {}).get("productVariants") or {}).get("edges") or [])
    if not edges:
        return None
    n = edges[0]["node"]
    inv = n.get("inventoryItem") or {}
    cost = (inv.get("unitCost") or {}).get("amount")
    return {
        "variant_gid": n["id"],
        "variant_id": int(n["legacyResourceId"]),
        "sku": n.get("sku"),
        "price": float(n["price"]) if n.get("price") else None,
        "compare_at": float(n["compareAtPrice"]) if n.get("compareAtPrice") else None,
        "inventory_item_id": int(inv["legacyResourceId"]) if inv.get("legacyResourceId") else None,
        "cost": float(cost) if cost else None,
        "product_gid": n["product"]["id"],
        "product_id": int(n["product"]["legacyResourceId"]),
        "product_status": n["product"]["status"],
        "product_tags": n["product"].get("tags") or [],
    }


# ── inventory helpers ───────────────────────────────────────────────────
def set_inventory_cost(inventory_item_id, cost, commit):
    """Set the per-unit cost on an inventory item (used as the join-cost
    for Tier 2 repricing)."""
    if not commit:
        return
    req("PUT", f"{API}/inventory_items/{inventory_item_id}.json",
        json={"inventory_item": {"id": inventory_item_id,
                                 "cost": f"{cost:.2f}", "tracked": True}})
    time.sleep(0.5)


def set_inventory_quantity(inventory_item_id, qty, commit):
    """Set available quantity at the active location."""
    if not commit:
        return
    req("POST", f"{API}/inventory_levels/set.json",
        json={"location_id": get_location_id(),
              "inventory_item_id": inventory_item_id,
              "available": int(qty)})
    time.sleep(0.5)


def add_tag(existing_tags, new_tag):
    """Merge a tag into a comma-joined or list tag set, de-duplicated."""
    if isinstance(existing_tags, str):
        tags = [t.strip() for t in existing_tags.split(",") if t.strip()]
    else:
        tags = list(existing_tags or [])
    if new_tag not in tags:
        tags.append(new_tag)
    return tags


def remove_tag(existing_tags, tag_to_remove):
    """Inverse of add_tag: drop a tag from a comma-joined or list tag set."""
    if isinstance(existing_tags, str):
        tags = [t.strip() for t in existing_tags.split(",") if t.strip()]
    else:
        tags = list(existing_tags or [])
    return [t for t in tags if t != tag_to_remove]
