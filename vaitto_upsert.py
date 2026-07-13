"""
vaitto_upsert.py — shared write layer for all Vaitto supplier pipelines.
POSTs product batches to the Vaitto app webhook instead of writing to DB directly.

Env vars:
  VAITTO_HOOK_URL     https://vaitto.com/api/public/hooks/vaitto-product-import
  IMPORT_HOOK_SECRET  shared secret
  VAITTO_DRY_RUN      '1' to log without writing
"""
import os, sys, logging, time, json, requests
from typing import Optional

log = logging.getLogger(__name__)

HOOK_URL   = os.environ.get("VAITTO_HOOK_URL", "https://vaitto.com/api/public/hooks/vaitto-product-import")
SECRET     = os.environ.get("IMPORT_HOOK_SECRET", "")
DRY_RUN    = os.environ.get("VAITTO_DRY_RUN", "0") == "1"
BATCH_SIZE = 50  # stay well under the 100 limit

if not SECRET:
    sys.exit("Missing IMPORT_HOOK_SECRET")


def _post_batch(products: list) -> dict:
    """POST one batch to the Vaitto webhook. Returns summary dict."""
    if DRY_RUN:
        log.info(f"  [DRY RUN] would POST {len(products)} products")
        return {"created": 0, "updated": 0, "deactivated": 0, "skipped": len(products), "errors": []}

    for attempt in range(4):
        try:
            r = requests.post(
                HOOK_URL,
                headers={"x-hook-secret": SECRET, "Content-Type": "application/json"},
                json={"products": products},
                timeout=60,
            )
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 5))
                log.warning(f"  Rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning(f"  Server error {r.status_code}, retrying")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401:
                sys.exit("IMPORT_HOOK_SECRET is wrong — got 401")
            if r.status_code == 404:
                sys.exit(f"Vaitto webhook returned 404 at {HOOK_URL} — the "
                          f"endpoint isn't resolving (wrong path, unpublished, "
                          f"or VAITTO_HOOK_URL is stale). Not treating this as "
                          f"an empty/successful run.")
            if r.status_code == 400:
                log.error(f"  Bad request: {r.text[:200]}")
                return {"created": 0, "updated": 0, "deactivated": 0, "skipped": 0, "errors": []}
            if not r.text or not r.text.strip():
                log.warning(f"  Empty response from webhook (status {r.status_code})")
                return {"created": 0, "updated": 0, "deactivated": 0, "skipped": 0, "errors": []}
            try:
                return r.json()
            except Exception:
                sys.exit(f"Non-JSON response from Vaitto webhook (status "
                          f"{r.status_code}): {r.text[:200]!r} -- stopping "
                          f"instead of silently reporting an empty success.")
        except requests.RequestException as e:
            log.warning(f"  Network error (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    return {"created": 0, "updated": 0, "deactivated": 0, "skipped": 0, "errors": []}


class VaittoUpsertSession:
    def __init__(self, supplier_id: str, supplier_name: str):
        self.supplier_id   = supplier_id
        self.supplier_name = supplier_name
        self.batch         = []
        self.counts        = {"created": 0, "updated": 0, "deactivated": 0, "skipped": 0, "errors": 0}
        log.info(f"  {supplier_name}: webhook mode → {HOOK_URL}")

    def upsert(self, *, sku: str, name: str,
               brand: Optional[str] = None,
               brand_id: Optional[str] = None,
               category_id: Optional[str] = None,
               subcategory_id: Optional[str] = None,
               gender: Optional[str] = None,
               supplier_price: Optional[float] = None,
               rrp: Optional[float] = None,
               stock_qty: int,
               description: Optional[str] = None,
               image_url: Optional[str] = None,
               images: list = None):

        product = {
            "vaitto_sku":     sku,
            "name":           name,
            "supplier_id":    self.supplier_id,
            "stock_qty":      stock_qty,
            "images":         images or [],
        }
        # Only include optional fields if they have a value (avoid sending null).
        # Use "is not None" for numeric fields so a legitimate 0 isn't dropped.
        if brand:                       product["brand"]          = brand
        if brand_id:                    product["brand_id"]       = brand_id
        if category_id:                 product["category_id"]    = category_id
        if subcategory_id:              product["subcategory_id"] = subcategory_id
        if gender:                      product["gender"]          = gender
        if supplier_price is not None:  product["supplier_price"] = supplier_price
        if rrp is not None:             product["rrp"]             = rrp
        if description:                 product["description"]    = description
        if image_url:                   product["image_url"]      = image_url
        self.batch.append(product)

        if len(self.batch) >= BATCH_SIZE:
            self._flush()

    def _flush(self):
        if not self.batch:
            return
        log.info(f"  → POSTing batch of {len(self.batch)} products…")
        result = _post_batch(self.batch)
        self.counts["created"]    += result.get("created", 0)
        self.counts["updated"]    += result.get("updated", 0)
        self.counts["deactivated"]+= result.get("deactivated", 0)
        self.counts["skipped"]    += result.get("skipped", 0)
        self.counts["errors"]     += len(result.get("errors", []))
        if result.get("errors"):
            for e in result["errors"][:5]:
                log.warning(f"    ⚠️  {e.get('vaitto_sku')}: {e.get('message')}")
        self.batch = []

    def finish(self):
        self._flush()  # flush any remaining
        c = self.counts
        log.info(f"\n  {self.supplier_name} done — "
                 f"✅{c['created']} created  🔄{c['updated']} updated  "
                 f"🔴{c['deactivated']} deactivated  "
                 f"⏭{c['skipped']} skipped  ❌{c['errors']} errors")
        return c
