#!/usr/bin/env python3
"""
Supplier → Vaitto  QUICK SYNC  (EU-WAR-3)
Price + stock only — meant to run every 60 min alongside the full sync
(sync_vaitto.py), same pattern as the Channable quick sync.

Sends a minimal payload per product: sku, name, brand, stock_qty,
supplier_price, rrp. Omits images/category/subcategory/description/gender
since those rarely change and this keeps each run fast and light. New
products are skipped here — only the full sync creates them, since it's
the one that sends images/category/etc. needed for a complete listing.

Env vars: PRICE_FEED, SUPPLIER_FALLBACK_FEED (via SUPPLIER_REPO_PATH's
          load_parallel_fallback), SUPPLIER_REPO_PATH,
          VAITTO_SUPABASE_URL, VAITTO_SUPABASE_SERVICE_KEY, VAITTO_DRY_RUN
"""
import os, sys, logging, requests, pandas as pd
from datetime import datetime

SUPPLIER_REPO = os.environ.get("SUPPLIER_REPO_PATH", "./supplier-sync")
sys.path.insert(0, SUPPLIER_REPO)
sys.path.insert(0, os.path.dirname(__file__))

from catalog_import import load_feed, load_parallel_fallback
from vaitto_upsert import VaittoUpsertSession
from vaitto_taxonomy import resolve_brand, load_brands

SUPPLIER_ID   = "35878fab-1c59-4778-b9f9-671da53e0887"
SUPPLIER_NAME = "Supplier (EU-WAR-3) [quick]"
PRICE_FEED         = os.environ.get("PRICE_FEED", "")
SB_URL        = os.environ.get("VAITTO_SUPABASE_URL", "")
SB_KEY        = os.environ.get("VAITTO_SUPABASE_SERVICE_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def get_existing_skus(supabase_url: str, service_key: str, supplier_id: str) -> set:
    """Fetch the set of vaitto_sku values already in Vaitto for this supplier.
    Used to skip brand-new products in the quick sync -- new products should
    only be created by the full sync, which sends images/category/etc."""
    if not supabase_url or not service_key:
        log.warning("  Missing Supabase creds — cannot check existing SKUs, "
                    "skipping quick sync run to avoid creating incomplete products")
        sys.exit(1)
    skus, offset, page = set(), 0, 1000
    first_page = True
    if first_page:
        # One-time sanity check: what supplier_id values actually exist in
        # this table? Paginate properly -- a single unpaginated request
        # can hit Supabase's implicit 1000-row cap (same issue found in
        # the admin dashboard earlier) and miss rows depending on default
        # ordering, so scan the whole table rather than trust one page.
        req_url = f"{supabase_url.rstrip('/')}/rest/v1/products"
        all_supplier_ids = []
        scan_offset = 0
        while True:
            r_any = requests.get(
                req_url,
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
                params={"select": "supplier_id", "limit": "1000",
                        "offset": str(scan_offset)},
                timeout=30,
            )
            if r_any.status_code != 200:
                log.info(f"  get_existing_skus: sanity check failed -> "
                         f"status={r_any.status_code}, body={r_any.text[:300]!r}")
                break
            rows = r_any.json()
            if not rows:
                break
            all_supplier_ids.extend(row.get("supplier_id") for row in rows)
            if len(rows) < 1000:
                break
            scan_offset += 1000
        distinct = sorted(set(all_supplier_ids))
        log.info(f"  get_existing_skus: sanity check -> {len(all_supplier_ids)} "
                 f"total rows scanned, distinct supplier_id values: {distinct}")
        log.info(f"  get_existing_skus: looking for exact match "
                 f"{supplier_id!r} -- present: {supplier_id in distinct}")
    while True:
        req_url = f"{supabase_url.rstrip('/')}/rest/v1/products"
        req_params = {"supplier_id": f"eq.{supplier_id}", "select": "vaitto_sku",
                      "limit": str(page), "offset": str(offset)}
        r = requests.get(
            req_url,
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            params=req_params,
            timeout=30,
        )
        if first_page:
            log.info(f"  get_existing_skus: GET {req_url} params={req_params} "
                     f"-> status={r.status_code}, content-range="
                     f"{r.headers.get('content-range')}, "
                     f"body preview={r.text[:300]!r}")
            first_page = False
        if r.status_code != 200:
            log.error(f"  Failed to fetch existing SKUs: {r.status_code} {r.text[:300]}")
            sys.exit(1)
        rows = r.json()
        skus.update(row["vaitto_sku"] for row in rows if row.get("vaitto_sku"))
        if len(rows) < page:
            break
        offset += page
    return skus


def run():
    if not PRICE_FEED:
        sys.exit("Missing PRICE_FEED")

    log.info(f"⚡  Supplier → Vaitto  QUICK  {datetime.now():%Y-%m-%d %H:%M:%S}")

    brands = load_brands(SB_URL, SB_KEY)
    log.info(f"  {len(brands)} brands loaded")

    existing_skus = get_existing_skus(SB_URL, SB_KEY, SUPPLIER_ID)
    log.info(f"  {len(existing_skus)} existing SKUs found — new products will be "
             f"skipped (handled by full sync instead)")

    k = load_feed(PRICE_FEED, sep=";", dtype=str)
    k["stock_n"] = pd.to_numeric(k["c:stock_level:integer"], errors="coerce").fillna(0)
    instock = k[k["stock_n"] > 0].copy()
    grouped = instock.groupby("Code")
    log.info(f"  PriceFeed: {len(instock)} rows · {grouped.ngroups} products")

    parallel_fb = load_parallel_fallback()
    log.info(f"  parallel report styles (cost source): {len(parallel_fb)}")

    session = VaittoUpsertSession(SUPPLIER_ID, SUPPLIER_NAME)

    n = 0
    skipped_new = 0
    skipped_no_cost = 0
    for code, group_df in grouped:
        code = str(code)
        if code not in existing_skus:
            skipped_new += 1
            continue

        pf = parallel_fb.get(code)
        if not pf or not pf.get("cost"):
            skipped_no_cost += 1
            continue

        stock_qty = int(group_df["stock_n"].sum())
        brand = pf.get("brand") or "Unknown"
        supplier_price = pf.get("cost")
        rrp = pf.get("rrp")
        title = group_df.iloc[0].get("Title") or code

        n += 1
        # Minimal payload: no images, no category/subcategory/gender/description.
        session.upsert(
            sku=code,
            name=str(title),
            brand=brand,
            brand_id=resolve_brand(brand),
            supplier_price=supplier_price,
            rrp=rrp,
            stock_qty=stock_qty,
            images=[],
        )

    log.info(f"  {skipped_new} new products skipped (will be created by the full sync)")
    log.info(f"  {skipped_no_cost} skipped (no cost resolved from parallel report)")
    log.info(f"  {n} products updated")
    session.finish()


if __name__ == "__main__":
    run()
