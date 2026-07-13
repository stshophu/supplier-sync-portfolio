#!/usr/bin/env python3
"""
reconcile.py — TIER 3: catch SKUs that vanished from the feed entirely.

Neither Tier 1 (catalog_import.py) nor Tier 2 (hourly_refresh.py) ever
asks "which Shopify SKUs are no longer in the feed at all?" — both only loop
over rows that ARE present. Tier 2 zeroes stock for rows with
c:stock_level:integer == 0, but a SKU that Supplier drops from the feed
completely (discontinued, pulled by brand, etc.) never shows up as a row, so
it's invisible to both tiers and stays live on Shopify forever.

This script closes that gap:
  1. Pull every Shopify variant tagged SupplierSync (paginated).
  2. Pull today's full PriceFeed feed (same source Tier 2 uses — it carries a row
     for every SKU Supplier currently offers, in-stock or sold-out).
  3. Any SupplierSync SKU on Shopify NOT present as a row in PriceFeed at all ->
     zero its Shopify inventory (same "zeroed-out" mechanism Tier 2 uses for
     stock_level=0) and tag it 'supplier-missing-from-feed' so it's easy to
     find and distinguish from a normal sell-out.

Does NOT archive/delete/unpublish anything — zeroing stock is enough to let
the existing WSNL sync disable the listing, and it's reversible if the SKU
reappears in the feed later (next Tier 2 run will restock it automatically).

Usage:
  python3 reconcile.py            # DRY RUN (default)
  python3 reconcile.py --commit   # apply to Shopify
  python3 reconcile.py --limit 25 # cap how many missing SKUs to act on
"""

import os
import sys
import time
import pandas as pd

from supplier_common import log, load_feed, graphql, set_inventory_quantity, add_tag, req, API

PRICE_FEED = os.environ.get("SUPPLIER_PRICE_FEED",
                       "/mnt/user-data/uploads/PriceFeed_CMSCustom__1_.csv")

COMMIT = "--commit" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])

TAG_MISSING = "supplier-missing-from-feed"

PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 25, after: $cursor, query: "tag:SupplierSync") {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        legacyResourceId
        title
        status
        tags
        variants(first: 50) {
          edges {
            node {
              id
              legacyResourceId
              sku
              inventoryQuantity
              inventoryItem { id legacyResourceId }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_supplier_variants():
    """Yield one dict per Shopify variant tagged SupplierSync (via product tag)."""
    cursor = None
    while True:
        data = graphql(PRODUCTS_QUERY, {"cursor": cursor})
        products = ((data or {}).get("products") or {})
        edges = products.get("edges") or []
        for edge in edges:
            p = edge["node"]
            for v_edge in (p.get("variants", {}).get("edges") or []):
                v = v_edge["node"]
                inv = v.get("inventoryItem") or {}
                if not v.get("sku") or not inv.get("legacyResourceId"):
                    continue
                yield {
                    "sku": v["sku"].strip(),
                    "variant_id": int(v["legacyResourceId"]),
                    "inventory_item_id": int(inv["legacyResourceId"]),
                    "inventory_quantity": v.get("inventoryQuantity") or 0,
                    "product_id": int(p["legacyResourceId"]),
                    "product_title": p.get("title"),
                    "product_status": p.get("status"),
                    "product_tags": p.get("tags") or [],
                }
        page_info = products.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")


def main():
    log(f"Mode: {'COMMIT' if COMMIT else 'DRY RUN'}" + (f"  (limit {LIMIT})" if LIMIT else ""))

    log("Fetching PriceFeed feed (full pull, all stock levels)...")
    k = load_feed(PRICE_FEED, sep=";", dtype=str)
    feed_skus = set(k["Id"].dropna().str.strip())
    log(f"PriceFeed feed SKUs: {len(feed_skus)}")

    log("Fetching Shopify variants tagged SupplierSync...")
    shopify_variants = list(fetch_supplier_variants())
    log(f"Shopify SupplierSync variants: {len(shopify_variants)}")

    missing = [v for v in shopify_variants if v["sku"] not in feed_skus]
    log(f"Missing from feed entirely: {len(missing)}")

    if LIMIT:
        missing = missing[:LIMIT]

    already_zero = 0
    zeroed = 0
    for v in missing:
        if v["inventory_quantity"] <= 0:
            already_zero += 1
            log(f"  {v['sku']:14} already zero        {v['product_title'][:40]}")
            continue

        log(f"  {v['sku']:14} zeroing (was {v['inventory_quantity']})  {v['product_title'][:40]}")
        if COMMIT:
            set_inventory_quantity(v["inventory_item_id"], 0, COMMIT)
            if TAG_MISSING not in v["product_tags"]:
                tags = add_tag(v["product_tags"], TAG_MISSING)
                req("PUT", f"{API}/products/{v['product_id']}.json",
                    json={"product": {"id": v["product_id"], "tags": ", ".join(tags)}})
                time.sleep(0.4)
        zeroed += 1

    log("\n=== DONE ===")
    log(f"  missing-from-feed     {len(missing)}")
    log(f"  already-zero          {already_zero}")
    log(f"  zeroed-this-run       {zeroed}")
    if not COMMIT:
        log("\nDry run only. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
