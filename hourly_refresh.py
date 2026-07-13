#!/usr/bin/env python3
"""
hourly_refresh.py — TIER 2: hourly stock + price refresh.

Source: PriceFeed_CMSCustom only (the feed that updates hourly).
PriceFeed has NO wholesale cost — and doesn't need it. Exactly like your
rewix_repricer.py / pricing.js, cost is pulled from Shopify's
inventory_item.cost (set once by Tier 1), never from the feed.

Per IN-STOCK PriceFeed row (stock_level > 0), joined to Shopify by SKU = Id:
  - EXISTING product (normal case):
        * cost     <- Shopify inventory_item.cost  (from Tier 1)
        * rrp      <- PriceFeed retail_price (live, in case brand changed it)
        * price    <- recomputed via price_decision(cost, rrp, brand)
        * quantity <- PriceFeed stock_level (live)
        * review-margin items get drafted + tagged, like Tier 1
  - NEW SKU not yet through Tier 1 (edge case):
        * create a minimal placeholder (Italian title is fine for now)
        * price conservatively off PriceFeed discount_retail_price (Supplier's own
          suggested retail price -- a discount off c:retail_price_EUR, not a
          cost figure), rounded to EUR 5, capped at c:retail_price_EUR (RRP)
        * tag 'pending-cost-verification' so Tier 1 fixes price + English
          content + real cost on its next run
        * left ACTIVE so it's immediately sellable

No images / descriptions are touched here — that's Tier 1's job. Fast + cheap.

Sold-out handling (required for the WSNL marketplace):
  PriceFeed rows with stock_level = 0 are NOT skipped. For any sold-out SKU that
  already exists in Shopify, the Shopify inventory is set to 0. The downstream
  WSNL sync disables any variant whose Shopify stock is <= 0 (it sets
  enabled=false + quantity=0 on WSNL, then removes the listing), so pushing 0
  here is what makes a Supplier item disappear from WSNL when it sells out.
  Note: this catches items the feed reports as 0. Items that vanish from the
  feed entirely are not caught here — run Tier 1 (full catalog) or a periodic
  reconciliation sweep to zero those.

PriceFeed prices are stored in CENTS (e.g. 55000 = EUR 550.00) and divided by 100.

Usage:
  python3 hourly_refresh.py            # DRY RUN (default)
  python3 hourly_refresh.py --commit   # apply to Shopify
  python3 hourly_refresh.py --limit 25
"""

import os
import sys
import time
import pandas as pd

from supplier_common import (log, load_feed, req, API, find_variant_by_sku, get_location_id,
                           set_inventory_cost, set_inventory_quantity, add_tag, remove_tag)
from pricing import price_decision, _round5
from translations import (translate_category, translate_gender,
                                 is_header_junk)

# Must match PENDING_TAG in catalog_import.py -- products created there are
# held as DRAFT + this tag until a PriceFeed row confirms the SKU is genuinely
# still offered, at which point this script activates them.
PENDING_TAG = "pending-tier2-confirmation"

# Feed source: env var in production (S3 URL), local path for tests.
PRICE_FEED = os.environ.get("SUPPLIER_PRICE_FEED",
                       "/mnt/user-data/uploads/PriceFeed_CMSCustom__1_.csv")

COMMIT = "--commit" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan" or is_header_junk(s):
        return None
    return s


def cents_to_eur(v):
    try:
        c = float(v)
        return round(c / 100.0, 2) if c else None
    except (TypeError, ValueError):
        return None


def update_existing(ev, row, brand, rrp, qty):
    """Reprice + restock an existing Shopify variant using its stored cost."""
    cost = ev.get("cost")

    activate = (ev.get("product_status") == "DRAFT"
               and PENDING_TAG in (ev.get("product_tags") or [])
               and qty > 0)

    if cost is None or cost <= 0:
        # Tier 1 hasn't set a cost yet -> leave price, just refresh stock,
        # and make sure it's flagged for Tier 1 to complete.
        set_inventory_quantity(ev["inventory_item_id"], qty, COMMIT)
        return "stock-only-no-cost"

    price, compare_at, status = price_decision(
        cost, rrp, brand, cents_to_eur(row.get("c:discount_retail_price_EUR:integer")))
    if price is None:
        return "no-price"

    # Confirmed against the feed -> release the pending-tier2 hold, unless
    # margin now says it should be drafted for review instead.
    if activate and status == "review-margin":
        activate = False

    new_status = "draft" if status == "review-margin" else None
    if COMMIT:
        # price + compare-at
        v_update = {"id": ev["variant_id"], "price": f"{price:.2f}"}
        if compare_at:
            v_update["compare_at_price"] = f"{compare_at:.2f}"
        req("PUT", f"{API}/variants/{ev['variant_id']}.json",
            json={"variant": v_update})
        time.sleep(0.4)
        # status/tag change only when it crosses into review-margin, or
        # when releasing a pending-tier2 hold
        if new_status == "draft" and ev.get("product_status") != "DRAFT":
            tags = add_tag(ev.get("product_tags", []), "supplier-review-margin")
            req("PUT", f"{API}/products/{ev['product_id']}.json",
                json={"product": {"id": ev["product_id"], "status": "draft",
                                  "tags": ", ".join(tags)}})
            time.sleep(0.4)
        elif activate:
            tags = remove_tag(ev.get("product_tags", []), PENDING_TAG)
            req("PUT", f"{API}/products/{ev['product_id']}.json",
                json={"product": {"id": ev["product_id"], "status": "active",
                                  "tags": ", ".join(tags)}})
            time.sleep(0.4)
        set_inventory_quantity(ev["inventory_item_id"], qty, COMMIT)
    return f"activated/{status}" if activate else f"repriced/{status}"


def create_placeholder(sku, row, brand, rrp, qty):
    """Minimal sellable placeholder for a brand-new SKU not yet in Shopify."""
    # c:discount_retail_price_EUR:integer = Supplier's own suggested retail
    # price (a discount off c:retail_price_EUR, the full RRP) -- not a cost
    # figure. Used here only as a conservative starting price for a
    # brand-new SKU until Tier 1 sets the real wholesale cost.
    suggested_retail = cents_to_eur(row.get("c:discount_retail_price_EUR:integer"))
    # conservative: use Supplier's suggested retail, round to 5, cap at RRP
    price = suggested_retail or rrp
    if not price:
        return "no-price-skip"
    price = _round5(price)
    if rrp:
        price = min(price, rrp)

    gender_en = translate_gender(row.get("Gender"))
    product_type = translate_category(row.get("tipologia prodotto")) or "Apparel"
    title = clean(row.get("Title")) or f"{brand} {product_type}"
    size = clean(row.get("Size")) or "OS"
    gtin = clean(row.get("Gtin"))

    tags = ["SupplierSync", "pending-cost-verification"]
    if gender_en:
        tags.append(gender_en)

    variant = {"sku": sku, "price": f"{price:.2f}", "option1": size,
               "inventory_management": "shopify"}
    if rrp and rrp > price:
        variant["compare_at_price"] = f"{rrp:.2f}"
    if gtin:
        variant["barcode"] = gtin

    payload = {"product": {
        "title": title, "vendor": brand, "product_type": product_type,
        "status": "active", "tags": ", ".join(tags),
        "options": [{"name": "Size"}], "variants": [variant],
    }}
    if COMMIT:
        r = req("POST", f"{API}/products.json", json=payload)
        inv_id = r.json()["product"]["variants"][0]["inventory_item_id"]
        time.sleep(0.5)
        set_inventory_quantity(inv_id, qty, COMMIT)
    return "placeholder-created"


def main():
    log(f"Mode: {'COMMIT' if COMMIT else 'DRY RUN'}" + (f"  (limit {LIMIT})" if LIMIT else ""))
    k = load_feed(PRICE_FEED, sep=";", dtype=str)
    k["stock_n"] = pd.to_numeric(k["c:stock_level:integer"], errors="coerce").fillna(0)
    # Process ALL rows, not just in-stock: sold-out rows must be pushed to 0 in
    # Shopify so the downstream WSNL sync disables them (WSNL disables any
    # variant whose Shopify inventory is <= 0). Skipping them would leave stale
    # stock on Shopify and the item would never get disabled on WSNL.
    n_instock = int((k["stock_n"] > 0).sum())
    log(f"PriceFeed rows: {len(k)}, in-stock: {n_instock}, sold-out: {len(k) - n_instock}")

    if COMMIT:
        get_location_id()

    counts = {}
    n = 0
    for _, row in k.iterrows():
        if LIMIT and n >= LIMIT:
            break
        sku = clean(row.get("Id"))
        if not sku:
            continue
        n += 1
        brand = clean(row.get("Brand")) or "Unknown"
        rrp = cents_to_eur(row.get("c:retail_price_EUR:integer"))
        qty = int(row["stock_n"])

        ev = find_variant_by_sku(sku)

        if qty <= 0:
            # Sold out -> zero the Shopify stock so WSNL disables it next run.
            if ev:
                set_inventory_quantity(ev["inventory_item_id"], 0, COMMIT)
                result = "zeroed-out (WSNL will disable)"
            else:
                # Never imported and already out of stock -> nothing to do.
                result = "skip-oos-new"
            counts[result] = counts.get(result, 0) + 1
            log(f"  {sku:14} {result:30} {brand[:18]:18}")
            continue

        if ev:
            result = update_existing(ev, row, brand, rrp, qty)
        else:
            result = create_placeholder(sku, row, brand, rrp, qty)
        counts[result] = counts.get(result, 0) + 1
        log(f"  {sku:14} {result:30} {brand[:18]:18} rrp={rrp}")

    log("\n=== DONE ===")
    for kx, vx in sorted(counts.items()):
        log(f"  {kx:26} {vx}")
    if not COMMIT:
        log("\nDry run only. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
