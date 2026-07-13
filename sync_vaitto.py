#!/usr/bin/env python3
"""
Supplier → Vaitto  (EU-WAR-3)
Env vars: PRICE_FEED, CATALOG_FEED, SUPPLIER_REPO_PATH,
          VAITTO_SUPABASE_URL, VAITTO_SUPABASE_SERVICE_KEY, VAITTO_DRY_RUN
"""
import os, sys, logging, pandas as pd
from datetime import datetime

SUPPLIER_REPO = os.environ.get("SUPPLIER_REPO_PATH", "./supplier-sync")
sys.path.insert(0, SUPPLIER_REPO)
sys.path.insert(0, os.path.dirname(__file__))

from catalog_import import (load_feed, load_parallel_fallback,
                            load_catalog_text_lookup, build_product_group_price_feed)
from translations import translate_gender
from vaitto_upsert import VaittoUpsertSession
from vaitto_taxonomy import resolve_brand, resolve_category, resolve_subcategory, resolve_gender, load_brands

SUPPLIER_ID = "35878fab-1c59-4778-b9f9-671da53e0887"
SUPPLIER_NAME = "Supplier (EU-WAR-3)"
PRICE_FEED = os.environ.get("PRICE_FEED", "")
LIMIT = int(os.environ.get("LIMIT", "0")) or None


logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

def run():
    if not PRICE_FEED:
        sys.exit("Missing PRICE_FEED")

    log.info(f"🚀  Supplier → Vaitto  {datetime.now():%Y-%m-%d %H:%M:%S}")

    k = load_feed(PRICE_FEED, sep=";", dtype=str)
    k["stock_n"] = pd.to_numeric(k["c:stock_level:integer"], errors="coerce").fillna(0)
    instock = k[k["stock_n"] > 0].copy()
    grouped = instock.groupby("Code")
    log.info(f"  PriceFeed: {len(instock)} rows · {grouped.ngroups} products")

    parallel_fb = load_parallel_fallback()
    log.info(f"  parallel report styles (cost source): {len(parallel_fb)}")
    text_lookup = load_catalog_text_lookup()
    log.info(f"  CatalogFeed English-text matches available: {len(text_lookup)} styles")
    session     = VaittoUpsertSession(SUPPLIER_ID, SUPPLIER_NAME)

    n = 0
    skipped_no_cost = 0
    for code, group_df in grouped:
        if LIMIT and n >= LIMIT:
            break
        item = build_product_group_price_feed(group_df, parallel_fb, text_lookup)
        if not item:
            skipped_no_cost += 1
            continue
        n += 1
        p         = item["payload"]["product"]
        metas     = item["metas"]
        stock_qty = sum(m["qty"] for m in metas)
        images    = [img["src"] for img in (p.get("images") or []) if img.get("src")]
        product_type = p.get("product_type", "")
        gender_raw = group_df.iloc[0].get("Gender", "")

        # Brand, wholesale cost, and RRP all come directly from the parallel
        # market report for this exact style code -- Marchio (brand),
        # Prezzo (wholesale), Costo (full retail) -- not from PriceFeed's own
        # price fields (discount_retail_price/retail_price) and not from
        # the Shopify-facing compare_at_price, which goes through a
        # separate repricer (price_decision) meant for the primary store only.
        pf = parallel_fb.get(str(code))
        brand = (pf.get("brand") if pf else None) or "Unknown"
        supplier_price = pf.get("cost") if pf else None
        rrp = pf.get("rrp") if pf else None

        log.info(f"[{n}]  {code}  '{p['title']}'  stock={stock_qty}  "
                 f"rrp={rrp}  brand={brand!r}")
        session.upsert(
            sku=str(code),
            name=p["title"],
            brand=brand,
            brand_id=resolve_brand(brand),
            category_id=resolve_category(product_type),
            subcategory_id=resolve_subcategory(product_type),
            gender=resolve_gender(str(gender_raw)),
            supplier_price=supplier_price,
            rrp=rrp,
            stock_qty=stock_qty,
            description=p.get("body_html") or None,
            image_url=images[0] if images else None,
            images=images[:10],
        )

    session.finish()
    log.info(f"  skipped groups (no cost resolved from parallel report): {skipped_no_cost}")

if __name__ == "__main__":
    run()
