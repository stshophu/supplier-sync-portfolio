# Supplier → Shopify Sync

Two-tier import + repricing pipeline for a wholesale supplier feed,
targeting Shopify (and reusable for other channels).

This is a sanitized, portfolio version of a production sync system:
credentials, live feed URLs, and real catalog/margin data have been
removed or replaced with placeholders. See `pricing.py` and the
workflow files for where to plug in your own values.

## Tiers

**Tier 1 — `catalog_import.py`** (daily, `catalog_import.yml`)
Full catalog import from a primary feed (with a fallback file). Creates/
updates products, sets `inventory_item.cost`, sets SKU = supplier SKU
(the Tier-2 join key), translates IT→EN, prices via the formula, and
auto-drafts items that can't clear the configured margin floor.

**Tier 2 — `hourly_refresh.py`** (hourly, `hourly_refresh.yml`)
Pulls cost from Shopify (set by Tier 1), refreshes live stock + RRP,
recomputes price. Creates a `pending-cost-verification` placeholder for
brand-new SKUs not yet through Tier 1.

Only in-stock products are imported.

## Pricing (`pricing.py`)
`price = (cost + shipping) × (1 + VAT) / (1 − margin)`, with a target
margin, a safety-net minimum margin, a hard RRP ceiling, and nearest-€5
rounding. Status: `ok` / `low-margin` / `review-margin`. Margin values in
this repo are placeholders — tune them for your own catalog.

## Translations (`translations.py`)
IT→EN for gender, category, colour, material, country. Filters leaked
header rows.

## Required environment variables / GitHub secrets
- `SHOPIFY_DOMAIN`, `SHOPIFY_TOKEN` — your Shopify Admin API credentials
- `SUPPLIER_LOCATION_ID` — optional; defaults to first active location
- Feed source paths/URLs — configure your own via env vars; none are
  committed to this repo

## Run order
1. `python3 catalog_import.py` (dry run) → review output
2. `python3 catalog_import.py --commit` → live
3. `python3 hourly_refresh.py` (dry run) → review
4. `python3 hourly_refresh.py --commit` → live, then enable the cron

Both scripts are **dry-run by default**; `--commit` is required to write.
Use `--limit N` for a small smoke test first.
