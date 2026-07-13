#!/usr/bin/env python3
"""
catalog_import.py — TIER 1: full catalog import (run daily/weekly).

HYBRID sourcing (as of 2026-07-04), because freshness and content quality
live in different files:
  - PriceFeed        : the fresh, canonical catalog (same feed Tier 2/3 use).
                   Has stock, RRP, brand, gender, category, colour, images --
                   but NO cost, and NO English title/description.
  - parallel     : cost. Joins to PriceFeed directly on 'Code' <-> 'Cod. Articolo'
                   (same style-code format, ~97% coverage confirmed against
                   real data -- 436/449 PriceFeed codes found in the report).
  - CatalogFeed        : OPTIONAL, English text only (title/description). May be
                   stale (Wholesale Supplier doesn't always refresh it daily) --
                   that's fine, since it's never used for price/stock/margin,
                   only prose. Joined via the same style-code concept:
                     OLD CatalogFeed schema  : 'PRODUCT' column, direct match
                     NEW CatalogFeed schema  : "Internal SKU: <code>" regex-
                                         extracted from 'description'
                   If a PriceFeed style code isn't found in CatalogFeed (e.g. it's a
                   brand-new arrival not in the last CatalogFeed export), the
                   product still gets created -- just with a bare synthesized
                   title (Brand + Category + Colour) and no prose
                   description, rather than being blocked.

PriceFeed schema (columns used):
  variant join key (Tier 2/3) : 'Id'         (numeric)
  product group + parallel-join key : 'Code' (style+colour, e.g. 25NOV39)
  in-stock filter              : 'c:stock_level:integer' > 0
  RRP                          : 'c:retail_price_EUR:integer' (confirmed:
                                  full retail price, cents -> /100)
  size / barcode               : 'Size' / 'Gtin'
  brand / gender                : 'Brand' / 'Gender' ('M'/'F'/'U')
  category (IT)                 : 'tipologia prodotto' (specific, preferred)
                                  or 'Product_type' ("A > B > C" breadcrumb,
                                  fallback -- last segment used)
  colour / material             : 'Color' / 'material'
  images                        : 'image_link' + 'additional_image_link'
                                  (comma-separated)
  cost                          : NOT in this file -- comes from parallel.

parallel report schema (parallelMarketReport .xlsx):
  join key : 'Cod. Articolo'  (matches PriceFeed 'Code' directly)
  cost     : 'Prezzo'         (wholesale price -- what we'd actually pay)
  rrp      : 'Costo'          (full retail price, despite the name -- used
                               here only if PriceFeed's own RRP is somehow blank)

Variant grouping: one Shopify product per PriceFeed 'Code', one variant per size.
Each variant carries its own numeric 'Id' as SKU so Tier 2/3 look it up 1:1.

Only IN-STOCK PriceFeed rows are imported. A group is skipped entirely if no
cost can be resolved for any of its rows (i.e. the style code isn't in the
parallel report) -- tracked in the 'skipped_groups' count so it's visible,
not silently dropped.

Usage:
  python3 catalog_import.py            # DRY RUN (default, no writes)
  python3 catalog_import.py --commit   # apply to Shopify
  python3 catalog_import.py --limit 10 # process only first 10 PRODUCTS
"""

import os
import re
import sys
import time
import pandas as pd

from supplier_common import (log, load_feed, req, API, find_variant_by_sku,
                           get_location_id, set_inventory_cost,
                           set_inventory_quantity, add_tag)

# ── Category-based weight defaults ────────────────────────────────────────────
# Kept in sync with backfill_weights.py / channable_sync.py
_WEIGHT_RULES = [
    (("parka", "down jacket", "puffer", "coat"), 2.5),
    (("suit",), 1.8),
    (("gilet", "vest", "windbreaker"), 1.0),
    (("jacket", "blazer", "bomber", "biker"), 1.5),
    (("boot",), 2.0),
    (("sneaker", "trainer"), 1.5),
    (("flats", "sandal", "pump", "heel", "loafer", "oxford", "derb",
      "slipper", "mule", "slide", "espadrille", "slip-on", "slip on", "shoe"), 1.2),
    (("clutch", "pouch", "purse", "mini bag"), 1.0),
    (("bag", "backpack", "tote", "handbag"), 1.5),
    (("wallet", "cardholder", "card holder", "keyring", "key holder", "case"), 0.5),
    (("belt",), 0.6),
    (("sunglass", "eyewear", "glasses"), 0.5),
    (("bow tie", "pocket square", "necktie", "ties", "tie "), 0.3),
    (("scarf", "glove", "hat", "cap", "beanie"), 0.3),
    (("watch", "jewel", "bracelet", "necklace", "ring", "earring"), 0.5),
    (("swim", "bikini", "underwear", "bra", "brief", "legging", "sock",
      "lingerie", "boxer"), 0.3),
    (("sweater", "knit", "cardigan", "hoodie", "sweatshirt", "pullover",
      "jumper", "turtleneck"), 0.8),
    (("jean", "denim", "trouser", "pant", "chino", "jogger"), 0.8),
    (("dress",), 0.7),
    (("skirt", "short", "bermuda"), 0.5),
    (("shirt", "polo", "t-shirt", "tee", "top", "blouse", "bodysuit"), 0.5),
]

def default_weight_kg(product_type: str = "", title: str = "") -> float:
    """Return a category-default billable weight in kg (never 0)."""
    for haystack in ((product_type or "").lower(), (title or "").lower()):
        for keywords, kg in _WEIGHT_RULES:
            if any(k in haystack for k in keywords):
                return kg
    return 0.8  # fallback
from pricing import price_decision
from translations import (translate_gender, translate_category,
                                 translate_colour, translate_material,
                                 is_header_junk)

PRICE_FEED = os.environ.get("SUPPLIER_PRICE_FEED",
    "/mnt/user-data/uploads/PriceFeed_CMSCustom.csv")
PARALLEL = os.environ.get("SUPPLIER_FALLBACK_FEED",
    "/mnt/user-data/uploads/parallelMarketReport_3786009.xlsx")
# Optional -- text enrichment only, never used for price/stock. May be None.
CATALOG_FEED = os.environ.get("SUPPLIER_CATALOG_FEED",
    "/mnt/user-data/uploads/supplier_catalog_feed_file_21231_-_supplier_catalog_feed_file_21231.csv")

COMMIT = "--commit" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])

STATUS_TAG = {"low-margin": "supplier-low-margin",
              "review-margin": "supplier-review-margin"}

# Tag applied to brand-new products that are held as DRAFT on creation even
# though pricing/margin was fine ("active"-worthy). They only go live once
# hourly_refresh.py (Tier 2) sees the same SKU present in the PriceFeed
# feed and flips them active -- a cross-check that the SKU is still actually
# offered by the time we'd sell it.
PENDING_TAG = "pending-tier2-confirmation"

INTERNAL_SKU_PATTERN = re.compile(r"Internal SKU\s*:?\s*([A-Za-z0-9]+)", re.IGNORECASE)


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan" or is_header_junk(s):
        return None
    return s


def to_float(v):
    """Parse '1,234.56' / '5454.00' / '5454' / '5,454,00' tolerantly."""
    s = clean(v)
    if not s:
        return None
    s = s.replace(" ", "")
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def cents_to_eur(v):
    try:
        c = float(v)
        return round(c / 100.0, 2) if c else None
    except (TypeError, ValueError):
        return None


def extract_internal_sku(description):
    """Pull the 'Internal SKU: <code>' style code out of an CatalogFeed (new
    schema) description string, if present. Returns None if not found."""
    if not isinstance(description, str):
        return None
    m = INTERNAL_SKU_PATTERN.search(description)
    return m.group(1) if m else None


def load_parallel_fallback():
    """'Cod. Articolo' (style) -> {'cost', 'rrp'} from the parallel feed.

    Note: 'Prezzo' = wholesale cost, 'Costo' = full
    retail price (despite the name). 'Codice Modello' is NOT the join key
    (it's a separate brand/EAN-style code) -- 'Cod. Articolo' is, and it
    matches PriceFeed's 'Code' column directly (verified: 436/449 overlap).
    """
    p = load_feed(PARALLEL, dtype=str)
    required = {"Cod. Articolo", "Prezzo", "Costo"}
    # Verified against the LIVE feed directly (headerless CSV), not the
    # local xlsx snapshot -- the live source's column order differs from
    # the xlsx: Costo comes before an unidentified extra numeric column,
    # then Prezzo, rather than Prezzo/Costo adjacent as in the xlsx.
    KNOWN_SCHEMA = [
        "Unnamed: 0", "Marchio", "Cod. Articolo", "Codice Modello",
        "Descrizione", "Composizione", "Paese/Regione di origine",
        "Gender", "Colore", "Taglia", "Disponib.", "Costo",
        "Unnamed: 12", "Prezzo",
    ]
    if not required.issubset(set(p.columns)):
        # Confirmed via raw preview: the source is comma-delimited with
        # correct quoting (embedded commas in Descrizione are NOT split),
        # but has NO header row -- pandas was using the first data row as
        # column names. Re-read headerless and assign the known schema by
        # position instead of guessing delimiters.
        from supplier_common import log
        log(f"  load_parallel_fallback: expected columns {required} not "
            f"found ({len(p.columns)} cols read as header) -- source "
            f"appears to be missing its header row. Re-reading headerless.")
        try:
            raw = load_feed(PARALLEL, dtype=str, header=None)
            if raw.shape[1] == len(KNOWN_SCHEMA):
                raw.columns = KNOWN_SCHEMA
                p = raw
                log(f"  load_parallel_fallback: headerless read matched "
                    f"the known {len(KNOWN_SCHEMA)}-column schema -- "
                    f"{len(p)} rows recovered.")
            else:
                log(f"  load_parallel_fallback: headerless read has "
                    f"{raw.shape[1]} columns, expected {len(KNOWN_SCHEMA)} "
                    f"-- schema has changed, not just missing a header. "
                    f"Row 0 sample: {list(raw.iloc[0])}")
        except Exception as exc:
            log(f"  load_parallel_fallback: headerless re-read failed "
                f"({exc}).")
    fb = {}
    for _, row in p.iterrows():
        style = clean(row.get("Cod. Articolo"))
        if not style:
            continue
        cost = to_float(row.get("Prezzo"))
        rrp = to_float(row.get("Costo"))
        brand = clean(row.get("Marchio")) or None
        if cost or rrp:
            fb[style] = {"cost": cost, "rrp": rrp, "brand": brand}
    return fb


def _detect_catalog_schema(columns):
    cols = set(columns)
    if {"Model ID", "SKU FAMILY", "PRICE", "Market_PRICE"} <= cols:
        return "old"
    if {"sku", "wholesale_price", "rrp", "stock_qty"} <= cols:
        return "new"
    return None


def load_catalog_text_lookup():
    """style code -> {'title', 'description'} (English), sourced from
    whichever CatalogFeed schema is configured. English text ONLY -- never used
    for price/stock/margin. Returns {} (and logs why) if CATALOG_FEED isn't set,
    can't be loaded, or doesn't match a known schema -- callers must handle
    an empty lookup gracefully (bare synthesized titles).
    """
    if not CATALOG_FEED:
        log("SUPPLIER_CATALOG_FEED not set -- no English text enrichment; "
            "unmatched SKUs get a bare synthesized title.")
        return {}
    try:
        e = load_feed(CATALOG_FEED, dtype=str)
    except Exception as exc:
        log(f"Could not load CatalogFeed for text enrichment ({exc}) -- continuing "
            f"without it.")
        return {}

    schema = _detect_catalog_schema(e.columns)
    if schema is None:
        log("CatalogFeed file present but columns don't match a known schema -- "
            "skipping text enrichment.")
        return {}

    lookup = {}
    if schema == "old":
        for _, row in e.iterrows():
            style = clean(row.get("PRODUCT"))
            if not style:
                continue
            title = clean(row.get("Titel_EN"))
            desc = clean(row.get("Description_EN"))
            if title or desc:
                lookup[style] = {"title": title, "description": desc}
    else:
        for _, row in e.iterrows():
            style = extract_internal_sku(row.get("description"))
            if not style:
                continue
            title = clean(row.get("product_name"))
            desc = clean(row.get("description"))
            if title or desc:
                lookup[style] = {"title": title, "description": desc}
    return lookup


def build_images_price_feed(rep):
    urls = []
    main_img = clean(rep.get("image_link"))
    if main_img and main_img.startswith("http"):
        urls.append({"src": main_img})
    extra = rep.get("additional_image_link")
    if isinstance(extra, str):
        for u in extra.split(","):
            u = u.strip()
            if u.startswith("http"):
                urls.append({"src": u})
    return urls


def _category_from_price_feed(rep):
    specific = translate_category(rep.get("tipologia prodotto"))
    if specific:
        return specific
    # fallback: breadcrumb "A > B > C" -- take the last segment
    raw = clean(rep.get("Product_type"))
    if raw:
        last = raw.split(">")[-1].strip()
        return translate_category(last) or last
    return "Apparel"


def build_product_group_price_feed(group_df, parallel_fb, text_lookup):
    rep = group_df.iloc[0]
    code = clean(rep.get("Code"))
    brand = clean(rep.get("Brand"))
    if not brand:
        # PriceFeed's own Brand column is sometimes empty even though the
        # parallel report's 'Marchio' column reliably has it for the same
        # style code -- fall back to that before giving up to "Unknown".
        pf = parallel_fb.get(code) if code else None
        brand = (pf.get("brand") if pf else None) or "Unknown"

    gender_en = translate_gender(rep.get("Gender"))
    product_type = _category_from_price_feed(rep)
    colour_en = translate_colour(rep.get("Color"))
    material_en = translate_material(rep.get("material"))

    text = text_lookup.get(code) if code else None
    if text and text.get("title"):
        title = text["title"]
    else:
        title = " ".join(p for p in [brand, product_type, colour_en] if p) \
                or f"{brand} {product_type}"

    if text and text.get("description"):
        body = text["description"].replace("\r\n", "\n").replace("\n", "<br>")
    else:
        body = ""  # no English prose available -- detail_bits below still
                   # gives the customer something

    detail_bits = []
    if material_en: detail_bits.append(f"<strong>Material:</strong> {material_en}")
    if colour_en:   detail_bits.append(f"<strong>Colour:</strong> {colour_en}")
    if detail_bits:
        sep = "<br><br>" if body else ""
        body = (body + sep + "<br>".join(detail_bits)).strip()

    variants, metas, statuses = [], [], []
    for _, row in group_df.iterrows():
        sku = clean(row.get("Id"))  # numeric Tier-2/3 join key
        if not sku:
            continue
        row_code = clean(row.get("Code")) or code
        fb = parallel_fb.get(row_code) if row_code else None
        cost = fb.get("cost") if fb else None
        rrp = cents_to_eur(row.get("c:retail_price_EUR:integer"))
        if rrp is None and fb:
            rrp = fb.get("rrp")
        supplier_suggested = cents_to_eur(row.get("c:discount_retail_price_EUR:integer"))
        if not cost or cost <= 0:
            continue
        price, compare_at, status = price_decision(cost, rrp, brand, supplier_suggested)
        if price is None:
            continue
        size = clean(row.get("Size")) or "OS"
        gtin = clean(row.get("Gtin"))
        try:
            qty = int(float(row.get("c:stock_level:integer")))
        except (TypeError, ValueError):
            qty = 0

        wkg = default_weight_kg(product_type, title)
        v = {"sku": sku, "price": f"{price:.2f}", "option1": size,
             "inventory_management": "shopify",
             "weight": wkg, "weight_unit": "kg"}
        if compare_at:
            v["compare_at_price"] = f"{compare_at:.2f}"
        if gtin:
            v["barcode"] = gtin
        variants.append(v)
        metas.append({"sku": sku, "cost": cost, "qty": qty, "status": status})
        statuses.append(status)

    return _finalize_group(variants, metas, statuses, title, body, brand,
                           product_type, gender_en, build_images_price_feed(rep))


def _finalize_group(variants, metas, statuses, title, body, brand,
                    product_type, gender_en, images):
    if not variants:
        return None

    sellable = [s for s in statuses if s in ("ok", "low-margin")]
    product_status = "active" if sellable else "draft"
    if product_status == "active":
        for m in metas:
            if m["status"] == "review-margin":
                m["qty"] = 0

    worst = ("review-margin" if "review-margin" in statuses
             else "low-margin" if "low-margin" in statuses else "ok")
    tags = ["SupplierSync"]
    if gender_en:
        tags.append(gender_en)
    if worst in STATUS_TAG:
        tags.append(STATUS_TAG[worst])

    payload = {"product": {
        "title": title, "body_html": body, "vendor": brand,
        "product_type": product_type, "status": product_status,
        "tags": ", ".join(tags), "options": [{"name": "Size"}],
        "variants": variants, "images": images,
    }}
    return {"payload": payload, "metas": metas, "title": title,
            "status": product_status, "worst": worst,
            "n_variants": len(variants),
            "n_zeroed": sum(1 for m in metas
                            if m["qty"] == 0 and product_status == "active")}


def apply_variant_inventory(sku_to_inv, metas):
    for m in metas:
        inv_id = sku_to_inv.get(m["sku"])
        if not inv_id:
            continue
        set_inventory_cost(inv_id, m["cost"], COMMIT)
        set_inventory_quantity(inv_id, m["qty"], COMMIT)


def upsert_group(item):
    metas = item["metas"]
    skus = [m["sku"] for m in metas]

    existing = None
    for s in skus:
        existing = find_variant_by_sku(s)
        if existing:
            break

    if existing:
        pid = existing["product_id"]
        prod = item["payload"]["product"]
        still_pending = (existing.get("product_status") == "DRAFT"
                         and PENDING_TAG in (existing.get("product_tags") or []))
        if still_pending and item["status"] == "active":
            prod["status"] = "draft"
            tags = [t.strip() for t in prod["tags"].split(",") if t.strip()]
            if PENDING_TAG not in tags:
                tags.append(PENDING_TAG)
            prod["tags"] = ", ".join(tags)
            item["held_pending_tier2"] = True

        # A placeholder Tier 2 created (create_placeholder) has NO images and
        # a bare/Italian title -- Tier 1 never touched images on updates
        # before, so these stayed image-less forever. This is where Tier 1
        # "graduates" a placeholder to a full record: detected via the tag
        # Tier 2 stamps on those placeholders, backfill images here (once),
        # then drop the tag so this doesn't re-trigger on every future run.
        body = {"id": pid, "title": prod["title"], "body_html": prod["body_html"],
                "vendor": prod["vendor"], "product_type": prod["product_type"],
                "status": prod["status"], "tags": prod["tags"]}
        if "pending-cost-verification" in (existing.get("product_tags") or []):
            tags = [t.strip() for t in prod["tags"].split(",") if t.strip()]
            tags = [t for t in tags if t != "pending-cost-verification"]
            body["tags"] = ", ".join(tags)
            if prod.get("images"):
                body["images"] = prod["images"]
            item["backfilled_placeholder"] = True

        if COMMIT:
            req("PUT", f"{API}/products/{pid}.json", json={"product": body})
            time.sleep(0.5)
            r = req("GET", f"{API}/products/{pid}.json")
            cur = r.json().get("product", {}).get("variants", [])
            by_sku = {v.get("sku"): v for v in cur}
            sku_to_inv = {}
            for v in prod["variants"]:
                ex = by_sku.get(v["sku"])
                if ex:
                    upd = {"id": ex["id"], "price": v["price"]}
                    if "compare_at_price" in v:
                        upd["compare_at_price"] = v["compare_at_price"]
                    req("PUT", f"{API}/variants/{ex['id']}.json",
                        json={"variant": upd})
                    time.sleep(0.4)
                    sku_to_inv[v["sku"]] = ex["inventory_item_id"]
                else:
                    r2 = req("POST", f"{API}/products/{pid}/variants.json",
                             json={"variant": v})
                    created = r2.json().get("variant", {})
                    if created.get("inventory_item_id"):
                        sku_to_inv[v["sku"]] = created["inventory_item_id"]
                    time.sleep(0.4)
            apply_variant_inventory(sku_to_inv, metas)
        return "update"
    else:
        payload = item["payload"]
        held = False
        if item["status"] == "active":
            held = True
            payload["product"]["status"] = "draft"
            tags = [t.strip() for t in payload["product"]["tags"].split(",") if t.strip()]
            if PENDING_TAG not in tags:
                tags.append(PENDING_TAG)
            payload["product"]["tags"] = ", ".join(tags)
        item["held_pending_tier2"] = held
        if COMMIT:
            r = req("POST", f"{API}/products.json", json=payload)
            created = r.json().get("product", {})
            sku_to_inv = {v.get("sku"): v.get("inventory_item_id")
                          for v in created.get("variants", [])}
            time.sleep(0.5)
            apply_variant_inventory(sku_to_inv, metas)
        return "create"


def main():
    log(f"Mode: {'COMMIT' if COMMIT else 'DRY RUN'}"
        + (f"  (limit {LIMIT} products)" if LIMIT else ""))
    log("Loading PriceFeed (primary catalog + stock + RRP)...")
    k = load_feed(PRICE_FEED, sep=";", dtype=str)
    k["stock_n"] = pd.to_numeric(k["c:stock_level:integer"], errors="coerce").fillna(0)
    instock = k[k["stock_n"] > 0].copy()
    grouped = instock.groupby("Code")
    n_products = grouped.ngroups
    log(f"PriceFeed in-stock rows: {len(instock)} -> products: {n_products}")

    parallel_fb = load_parallel_fallback()
    log(f"parallel report styles (cost source): {len(parallel_fb)}")

    text_lookup = load_catalog_text_lookup()
    log(f"CatalogFeed English-text matches available: {len(text_lookup)} styles")

    if COMMIT:
        get_location_id()

    counts = {"create": 0, "update": 0, "draft_review": 0,
              "active_with_zeroed": 0, "skipped_groups": 0,
              "held_pending_tier2": 0, "bare_title": 0, "backfilled_placeholder": 0}
    total_variants = 0
    n = 0

    for code, group_df in grouped:
        if LIMIT and n >= LIMIT:
            break
        item = build_product_group_price_feed(group_df, parallel_fb, text_lookup)
        if item is None:
            counts["skipped_groups"] += 1
            continue
        n += 1
        bare = code not in text_lookup
        if bare:
            counts["bare_title"] += 1
        action = upsert_group(item)
        counts[action] += 1
        total_variants += item["n_variants"]
        if item["status"] == "draft":
            counts["draft_review"] += 1
        if item.get("held_pending_tier2"):
            counts["held_pending_tier2"] += 1
        if item.get("backfilled_placeholder"):
            counts["backfilled_placeholder"] += 1
        if item["n_zeroed"] > 0:
            counts["active_with_zeroed"] += 1

        flag = ""
        if bare:
            flag += "  [bare-title]"
        if item.get("held_pending_tier2"):
            flag += "  [HELD/pending-tier2]"
        elif item["status"] == "draft":
            flag += "  [DRAFT/all-review]"
        elif item["n_zeroed"] > 0:
            flag += f"  [{item['n_zeroed']} size(s) zeroed]"
        elif item["worst"] == "low-margin":
            flag += "  [low-margin]"
        if item.get("backfilled_placeholder"):
            flag += "  [images backfilled]"
        log(f"  {action:6} {item['n_variants']}sz  {item['title'][:46]:46}{flag}")

    log("\n=== DONE ===")
    log(f"Products created:        {counts['create']}")
    log(f"  held (pending-tier2): {counts['held_pending_tier2']}")
    log(f"Products updated:        {counts['update']}")
    log(f"  placeholders backfilled (images added): {counts['backfilled_placeholder']}")
    log(f"Total variants:          {total_variants}")
    log(f"  drafted (all-review):  {counts['draft_review']}")
    log(f"  active w/ zeroed size: {counts['active_with_zeroed']}")
    log(f"  bare title (no CatalogFeed match): {counts['bare_title']}")
    log(f"  skipped groups (no cost found in parallel report): {counts['skipped_groups']}")
    if not COMMIT:
        log("\nDry run only. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
