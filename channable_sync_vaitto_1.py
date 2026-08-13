#!/usr/bin/env python3
"""
Channable → Vaitto  (Tluxy / EU-WAR-2)
Env vars: CHANNABLE_URL, VAITTO_SUPABASE_URL, VAITTO_SUPABASE_SERVICE_KEY, VAITTO_DRY_RUN
"""
import os, sys, logging
from io import StringIO
from datetime import datetime
import requests, pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from vaitto_upsert import VaittoUpsertSession
from vaitto_taxonomy import (resolve_category, resolve_subcategory,
                             resolve_gender, load_brands)

SUPPLIER_ID   = "a4d69ebf-8916-440c-9640-3aec9770053e"
SUPPLIER_NAME = "Tluxy (EU-WAR-2)"
CHANNABLE_URL = os.environ.get("CHANNABLE_URL", "")
SB_URL        = os.environ.get("VAITTO_SUPABASE_URL", "")
SB_KEY        = os.environ.get("VAITTO_SUPABASE_SERVICE_KEY", "")


def _clean(v) -> str:
    """CSV cell -> stripped string; pandas NaN becomes empty."""
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() in ("nan", "none") else s



MIN_FEED_RATIO = 0.5   # abort the sweep if the feed shrank by more than half


def deactivate_missing(seen_skus: set, dry_run: bool) -> None:
    """Deactivate this supplier's active products that are no longer in the feed.

    Tluxy drops products from the feed entirely rather than sending them at
    stock 0, so the webhook's own deactivation path never sees them and they
    stay buyable forever. Products carrying manually booked return stock are
    left alone: that stock is physically in Hanau and has nothing to do with
    what the supplier still lists.
    """
    if not SB_URL or not SB_KEY:
        log.warning("  Sweep skipped: no Supabase credentials")
        return

    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    active, offset, page = [], 0, 1000
    while True:
        r = requests.get(
            f"{SB_URL.rstrip('/')}/rest/v1/products",
            headers=headers,
            params={"select": "id,vaitto_sku,name,returned_qty",
                    "supplier_id": f"eq.{SUPPLIER_ID}", "active": "eq.true",
                    "limit": str(page), "offset": str(offset)},
            timeout=60,
        )
        if r.status_code != 200:
            log.error(f"  Sweep aborted — could not list products: "
                      f"{r.status_code} {r.text[:300]}")
            return
        rows = r.json()
        active.extend(rows)
        if len(rows) < page:
            break
        offset += page

    if not active:
        return

    # A truncated download is the one way this does real damage. If the feed
    # carries less than half of what is currently active, something is wrong
    # with the feed rather than with the catalogue — leave it alone.
    ratio = len(seen_skus) / len(active)
    if ratio < MIN_FEED_RATIO:
        log.error(f"  ⛔  Sweep aborted — feed has {len(seen_skus)} products vs "
                  f"{len(active)} active ({ratio:.0%}). Suspected truncated feed.")
        return

    stale = [p for p in active if p["vaitto_sku"] not in seen_skus]
    kept  = [p for p in stale if (p.get("returned_qty") or 0) > 0]
    drop  = [p for p in stale if (p.get("returned_qty") or 0) == 0]

    for p in kept:
        log.info(f"  ↩️  keeping {p['vaitto_sku']}  '{p['name']}'  "
                 f"(returned_qty={p['returned_qty']})")
    for p in drop:
        log.info(f"  🔻 {'[DRY RUN] would deactivate' if dry_run else 'deactivating'}"
                 f"  {p['vaitto_sku']}  '{p['name']}'")

    if not drop:
        log.info("  Sweep: nothing to deactivate")
        return
    if dry_run:
        log.info(f"  [DRY RUN] would deactivate {len(drop)} products")
        return

    failed = 0
    for i in range(0, len(drop), 50):
        chunk = drop[i:i + 50]
        ids = ",".join(p["id"] for p in chunk)
        r = requests.patch(
            f"{SB_URL.rstrip('/')}/rest/v1/products",
            headers={**headers, "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            params={"id": f"in.({ids})"},
            json={"active": False},
            timeout=60,
        )
        if r.status_code not in (200, 204):
            failed += len(chunk)
            log.error(f"  Sweep chunk failed: {r.status_code} {r.text[:300]}")

    log.info(f"  Sweep: {len(drop) - failed} deactivated, {len(kept)} kept "
             f"(return stock), {failed} failed")



logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

def run():
    if not CHANNABLE_URL:
        sys.exit("Missing CHANNABLE_URL")

    log.info(f"🚀  Channable → Vaitto  {datetime.now():%Y-%m-%d %H:%M:%S}")

    # Load brands once
    brands = load_brands(SB_URL, SB_KEY)
    log.info(f"  {len(brands)} brands loaded")

    r = requests.get(CHANNABLE_URL, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    df.columns = df.columns.str.strip()
    df["quantity"]         = pd.to_numeric(df.get("quantity"),         errors="coerce").fillna(0).astype(int)
    df["wholesale_ EUR"]   = pd.to_numeric(df.get("wholesale_ EUR"),   errors="coerce")
    df["retail_price EUR"] = pd.to_numeric(df.get("retail_price EUR"), errors="coerce")
    log.info(f"  Feed: {len(df)} rows · {df['item_group_id'].nunique()} products")

    session = VaittoUpsertSession(SUPPLIER_ID, SUPPLIER_NAME)

    unmapped_cat, unmapped_sub = set(), set()
    seen_skus = set()
    total_variants = 0
    missing_desc = missing_img = missing_barcode = 0
    for i, (igid, group) in enumerate(df.groupby("item_group_id"), 1):
        first     = group.iloc[0]
        stock_qty = int(group["quantity"].sum())
        ref       = group[group["quantity"] > 0].iloc[0] if stock_qty > 0 else first
        cost      = ref.get("wholesale_ EUR")
        rrp       = ref.get("retail_price EUR")

        vendor   = _clean(first.get("vendor"))
        cat      = _clean(first.get("category"))
        subcat   = _clean(first.get("sub_category"))
        gender   = _clean(first.get("gender"))
        desc     = _clean(first.get("description"))
        barcode  = _clean(ref.get("gtin"))

        # One variant per feed row: the feed is variant-level, each row being a
        # single size with its own SKU, barcode and quantity.
        variants, seen_v = [], set()
        for _, r in group.iterrows():
            v_sku = _clean(r.get("sku"))
            if not v_sku or v_sku in seen_v:
                continue
            seen_v.add(v_sku)
            try:
                v_qty = int(float(r.get("quantity") or 0))
            except (TypeError, ValueError):
                v_qty = 0
            variants.append({
                "sku":       v_sku,
                "size":      _clean(r.get("size")) or None,
                "colour":    _clean(r.get("color")) or None,
                "barcode":   _clean(r.get("gtin")) or None,
                "stock_qty": max(0, v_qty),
            })

        images, seen = [], set()
        for _, row in group.iterrows():
            for col in ["image_1","image_2","image_3","image_4","image_5"]:
                u = str(row.get(col, "")).strip()
                if u and u.lower() != "nan" and u not in seen:
                    seen.add(u); images.append(u)

        cat_id    = resolve_category(cat, subcat)
        subcat_id = resolve_subcategory(subcat)

        if not cat_id:    unmapped_cat.add(cat or subcat)
        if not subcat_id: unmapped_sub.add(subcat)

        log.info(f"[{i}]  {igid}  '{first.get('title','')}' stock={stock_qty}")
        session.upsert(
            sku=str(igid),
            name=str(first.get("title", igid)),
            brand=vendor or None,
            category_id=cat_id,
            subcategory_id=subcat_id,
            gender=resolve_gender(gender),
            supplier_price=float(cost) if pd.notna(cost) and cost else None,
            rrp=float(rrp) if pd.notna(rrp) and rrp else None,
            stock_qty=stock_qty,
            description=desc or None,
            variants=variants or None,
            image_url=images[0] if images else None,
            images=images[:10],
        )

        seen_skus.add(igid)

        if not desc:    missing_desc += 1
        if not images:  missing_img += 1
        if not barcode: missing_barcode += 1
        total_variants += len(variants)

    session.finish()

    log.info(f"\n  Feed coverage — no description: {missing_desc}  "
             f"no image: {missing_img}  no barcode: {missing_barcode}  "
             f"variants sent: {total_variants}")
    if unmapped_cat:
        log.warning(f"  Unmapped categories: {sorted(v for v in unmapped_cat if v)}")
    if unmapped_sub:
        log.warning(f"  Unmapped sub_categories: {sorted(v for v in unmapped_sub if v)}")

    log.info(f"\n  Sweeping products no longer in the feed…")
    deactivate_missing(seen_skus, dry_run=bool(os.environ.get("VAITTO_DRY_RUN")))

if __name__ == "__main__":
    run()
