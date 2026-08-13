// Signed external hook: batch product upsert from the Vaitto sync process.
// Auth: x-hook-secret must match IMPORT_HOOK_SECRET.
// Limit: 100 products per request.

import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";
import { computeWholesalePrice, resolveMarkupPct } from "@/lib/pricing";


const ProductSchema = z.object({
  vaitto_sku: z.string().trim().min(1).max(200),
  name: z.string().trim().min(1).max(500),
  brand: z.string().trim().min(1).max(200).nullable().optional(),
  brand_id: z.string().uuid().nullable().optional(),
  supplier_id: z.string().uuid(),
  category_id: z.string().uuid().nullable().optional(),
  subcategory_id: z.string().uuid().nullable().optional(),
  gender: z.enum(["Women", "Men", "Unisex"]).nullable().optional(),
  supplier_price: z.number().nonnegative().nullable().optional(),
  rrp: z.number().nonnegative().nullable().optional(),
  stock_qty: z.number().int().nonnegative(),
  image_url: z.string().url().nullable().optional(),
  images: z.array(z.string().url()).max(50).nullable().optional(),
  description: z.string().max(20000).nullable().optional(),
  // Variant-level detail. Feeds that are variant-level (one row per size) send
  // this; product-level feeds omit it and nothing about variants is touched.
  variants: z
    .array(
      z.object({
        sku: z.string().trim().min(1).max(200),
        size: z.string().trim().max(100).nullable().optional(),
        colour: z.string().trim().max(100).nullable().optional(),
        barcode: z.string().trim().max(100).nullable().optional(),
        stock_qty: z.number().int().nonnegative(),
      }),
    )
    .max(50)
    .optional(),
});

// Type-only reference to the admin client, which is imported dynamically inside
// the handler so it never reaches the browser bundle.
type SupabaseAdmin = typeof import("@/integrations/supabase/client.server")["supabaseAdmin"];

/**
 * Reconcile a product's variants against the feed.
 *
 * Matched on (product_id, sku). Variants the feed no longer lists are zeroed
 * rather than deleted: order_items.variant_id is ON DELETE NO ACTION, so
 * deleting a sold variant fails, and returned_qty is stock physically held
 * in Hanau that the supplier's listing says nothing about.
 */
async function syncVariants(
  supabaseAdmin: SupabaseAdmin,
  productId: string,
  variants: { sku: string; size?: string | null; colour?: string | null; barcode?: string | null; stock_qty: number }[],
): Promise<void> {
  const { data: existingRows, error: probeErr } = await supabaseAdmin
    .from("product_variants")
    .select("id, sku, returned_qty")
    .eq("product_id", productId);
  if (probeErr) throw new Error(`variant probe: ${probeErr.message}`);

  const existingBySku = new Map<string, { id: string; returned_qty: number | null }>();
  for (const r of (existingRows ?? []) as { id: string; sku: string | null; returned_qty: number | null }[]) {
    if (r.sku) existingBySku.set(r.sku, { id: r.id, returned_qty: r.returned_qty });
  }

  const seen = new Set<string>();
  for (const v of variants) {
    seen.add(v.sku);
    const prior = existingBySku.get(v.sku);
    // Manually booked returns survive the supplier's stock figure, exactly as
    // on the product row.
    const stock = v.stock_qty + (prior?.returned_qty ?? 0);
    if (prior) {
      const patch: Record<string, unknown> = { stock_qty: stock };
      if (v.size != null) patch['size'] = v.size;
      if (v.colour != null) patch['colour'] = v.colour;
      if (v.barcode != null) patch['barcode'] = v.barcode;
      const { error } = await supabaseAdmin
        .from("product_variants")
        .update(patch as never)
        .eq("id", prior.id);
      if (error) throw new Error(`variant update ${v.sku}: ${error.message}`);
    } else {
      const { error } = await supabaseAdmin
        .from("product_variants")
        .insert({
          product_id: productId,
          sku: v.sku,
          size: v.size ?? null,
          colour: v.colour ?? null,
          barcode: v.barcode ?? null,
          stock_qty: stock,
          returned_qty: 0,
        } as never);
      if (error) throw new Error(`variant insert ${v.sku}: ${error.message}`);
    }
  }

  for (const [sku, row] of existingBySku) {
    if (seen.has(sku)) continue;
    const { error } = await supabaseAdmin
      .from("product_variants")
      .update({ stock_qty: row.returned_qty ?? 0 } as never)
      .eq("id", row.id);
    if (error) throw new Error(`variant zero ${sku}: ${error.message}`);
  }
}

function slugify(input: string): string {
  return input
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function shortHash(input: string): string {
  let h = 0;
  for (let i = 0; i < input.length; i++) {
    h = (h * 31 + input.charCodeAt(i)) | 0;
  }
  return Math.abs(h).toString(36).slice(0, 6);
}

type ErrorItem = { vaitto_sku: string; message: string };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorEnvelope(message: string, status: number, vaitto_sku = ""): Response {
  return json(
    {
      ok: false,
      created: 0,
      updated: 0,
      deactivated: 0,
      skipped: 0,
      errors: [{ vaitto_sku, message }] as ErrorItem[],
    },
    status,
  );
}

export const Route = createFileRoute("/api/public/hooks/vaitto-product-import")({
  server: {
    handlers: {
      POST: async ({ request }) => {
        try {
          const expected = process.env.IMPORT_HOOK_SECRET;
          const provided = request.headers.get("x-hook-secret");
          if (!expected || !provided || provided !== expected) {
            return errorEnvelope("Unauthorized", 401);
          }

          let raw: unknown;
          try {
            raw = await request.json();
          } catch {
            return errorEnvelope("Invalid JSON body", 400);
          }

          // Early oversize check for a friendlier message than Zod's default.
          if (
            raw &&
            typeof raw === "object" &&
            Array.isArray((raw as { products?: unknown }).products) &&
            (raw as { products: unknown[] }).products.length > 100
          ) {
            return errorEnvelope("Batch too large (max 100 products per request)", 400);
          }

          // Validate per product, not per batch. A single malformed row must not
          // discard the other 99: the sync client only sees a 400 and logs
          // zeros, so batch-level rejection loses products silently.
          const rawList = (raw as { products?: unknown })?.products;
          if (!Array.isArray(rawList) || rawList.length === 0) {
            return errorEnvelope("Body must contain a non-empty products array", 400);
          }

          const products: z.infer<typeof ProductSchema>[] = [];
          const rejected: ErrorItem[] = [];
          for (const candidate of rawList) {
            const one = ProductSchema.safeParse(candidate);
            if (one.success) {
              products.push(one.data);
              continue;
            }
            const sku =
              candidate &&
              typeof candidate === "object" &&
              typeof (candidate as { vaitto_sku?: unknown }).vaitto_sku === "string"
                ? (candidate as { vaitto_sku: string }).vaitto_sku
                : "";
            rejected.push({
              vaitto_sku: sku,
              message: one.error.issues
                .map((i) => `${i.path.join(".")}: ${i.message}`)
                .join("; "),
            });
          }

          if (products.length === 0) {
            return json(
              { ok: false, created: 0, updated: 0, deactivated: 0, skipped: 0, errors: rejected },
              400,
            );
          }

          const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

          // Resolve supplier markups once so wholesale_price can be computed.
          const supplierIds = Array.from(new Set(products.map((p) => p.supplier_id)));
          const markupBySupplier = new Map<string, number>();
          if (supplierIds.length > 0) {
            const { data: supplierRows, error: supplierErr } = await supabaseAdmin
              .from("suppliers")
              .select("id, markup_percent")
              .in("id", supplierIds);
            if (supplierErr) {
              return errorEnvelope(`Supplier lookup failed: ${supplierErr.message}`, 500);
            }
            for (const s of supplierRows ?? []) {
              const pct = Number((s as { markup_percent: number | null }).markup_percent ?? 0);
              markupBySupplier.set(
                (s as { id: string }).id,
                Number.isFinite(pct) ? pct : 0,
              );
            }
          }

          // Look up existing rows for this batch in one query.
          const skus = Array.from(new Set(products.map((p) => p.vaitto_sku)));
          type ExistingRow = {
            id: string;
            slug: string;
            markup_percent_override: number | null;
            returned_qty: number | null;
          };
          const existingByKey = new Map<string, ExistingRow>();
          if (skus.length > 0) {
            const { data: existingRows, error: existingErr } = await supabaseAdmin
              .from("products")
              .select(
                "id, slug, vaitto_sku, supplier_id, markup_percent_override, returned_qty",
              )
              .in("vaitto_sku", skus)
              .in("supplier_id", supplierIds);
            if (existingErr) {
              return errorEnvelope(`Existing-product probe failed: ${existingErr.message}`, 500);
            }
            for (const r of existingRows ?? []) {
              const row = r as ExistingRow & { vaitto_sku: string; supplier_id: string };
              existingByKey.set(`${row.supplier_id}:${row.vaitto_sku}`, {
                id: row.id,
                slug: row.slug,
                markup_percent_override: row.markup_percent_override ?? null,
                returned_qty: row.returned_qty ?? 0,
              });
            }

          }

          // Resolve/create 'Unknown' brand lazily for products without brand_id.
          let unknownBrandId: string | null = null;
          const ensureUnknownBrand = async (): Promise<string> => {
            if (unknownBrandId) return unknownBrandId;
            const { data: found } = await supabaseAdmin
              .from("brands")
              .select("id")
              .ilike("name", "Unknown")
              .limit(1);
            if (found && found.length > 0) {
              unknownBrandId = (found[0] as { id: string }).id;
              return unknownBrandId;
            }
            const { data: inserted, error } = await supabaseAdmin
              .from("brands")
              .insert({ name: "Unknown" } as never)
              .select("id")
              .single();
            if (error || !inserted) {
              throw new Error(`Failed to create Unknown brand: ${error?.message ?? "unknown"}`);
            }
            unknownBrandId = (inserted as { id: string }).id;
            return unknownBrandId;
          };

          // Resolve/create a brand by its raw name (e.g. "Tom Ford"), sent by
          // suppliers that don't carry a brand_id. Case-insensitive match
          // against existing brands; creates a new brand row if none found.
          // Cached per-request so repeated names across a batch cost one
          // query instead of one per product.
          const brandByName = new Map<string, string>();
          const resolveBrandByName = async (name: string): Promise<string> => {
            const key = name.trim().toLowerCase();
            const cached = brandByName.get(key);
            if (cached) return cached;
            const { data: found } = await supabaseAdmin
              .from("brands")
              .select("id")
              .ilike("name", name.trim())
              .limit(1);
            if (found && found.length > 0) {
              const id = (found[0] as { id: string }).id;
              brandByName.set(key, id);
              return id;
            }
            const { data: inserted, error } = await supabaseAdmin
              .from("brands")
              .insert({ name: name.trim() } as never)
              .select("id")
              .single();
            if (error || !inserted) {
              throw new Error(`Failed to create brand "${name}": ${error?.message ?? "unknown"}`);
            }
            const id = (inserted as { id: string }).id;
            brandByName.set(key, id);
            return id;
          };

          const summary = {
            ok: true as const,
            created: 0,
            updated: 0,
            deactivated: 0,
            skipped: 0,
            // Products rejected by validation are reported alongside runtime
            // errors so the sync log names the offending SKU and field.
            errors: [...rejected] as ErrorItem[],
          };

          for (const p of products) {
            const key = `${p.supplier_id}:${p.vaitto_sku}`;
            const existing = existingByKey.get(key);

            try {
              if (p.stock_qty === 0 && !existing) {
                summary.skipped += 1;
                continue;
              }

              if (p.stock_qty === 0 && existing) {
                // Keep any manually credited return stock on the row.
                const { error } = await supabaseAdmin
                  .from("products")
                  .update({ active: false, stock_qty: existing.returned_qty ?? 0 })
                  .eq("id", existing.id);
                if (error) throw new Error(error.message);
                summary.deactivated += 1;
                continue;
              }

              const supplierMarkup = markupBySupplier.get(p.supplier_id) ?? 0;
              const markup = resolveMarkupPct(
                existing?.markup_percent_override ?? null,
                supplierMarkup,
              );
              const wholesale_price = computeWholesalePrice(p.supplier_price, markup);

              const brandId =
                p.brand_id ??
                (p.brand ? await resolveBrandByName(p.brand) : await ensureUnknownBrand());
              const mutable = {
                name: p.name,
                brand_id: brandId,
                supplier_id: p.supplier_id,
                category_id: p.category_id ?? null,
                subcategory_id: p.subcategory_id ?? null,
                gender: p.gender ?? null,
                supplier_price: p.supplier_price ?? null,
                wholesale_price,
                rrp: p.rrp ?? null,
                stock_qty: p.stock_qty,
                image_url: p.image_url ?? null,
                images: p.images ?? [],
                description: p.description ?? null,
                active: true,
              };

              let productId: string | null = existing?.id ?? null;

              if (existing) {
                // On update, a field the feed omits must never null out a value
                // maintained by hand in the admin.
                const patch: Record<string, unknown> = {
                  name: p.name,
                  supplier_id: p.supplier_id,
                  active: true,
                  // Manually booked returns survive the supplier's stock figure.
                  stock_qty: p.stock_qty + (existing.returned_qty ?? 0),
                };
                if (p.supplier_price != null) {
                  patch['supplier_price'] = p.supplier_price;
                  if (wholesale_price != null) patch['wholesale_price'] = wholesale_price;
                }
                if (p.gender != null) patch['gender'] = p.gender;
                if (p.category_id != null) patch['category_id'] = p.category_id;
                if (p.subcategory_id != null) patch['subcategory_id'] = p.subcategory_id;
                if (p.brand_id != null || p.brand) patch['brand_id'] = brandId;
                if (p.rrp != null) patch['rrp'] = p.rrp;
                if (p.image_url != null) patch['image_url'] = p.image_url;
                if (p.images != null && p.images.length > 0) patch['images'] = p.images;
                if (p.description != null) patch['description'] = p.description;

                const { error } = await supabaseAdmin
                  .from("products")
                  .update(patch as never)
                  .eq("id", existing.id);
                if (error) throw new Error(error.message);
                summary.updated += 1;
              } else {
                const baseSlug = slugify(p.name) || "product";
                const slug = `${baseSlug}-${shortHash(`${p.supplier_id}:${p.vaitto_sku}`)}`;
                const insertRow = {
                  ...mutable,
                  slug,
                  vaitto_sku: p.vaitto_sku,
                  product_source: "admin" as const,
                  dropship_available: true,
                };
                const { data: createdRow, error } = await supabaseAdmin
                  .from("products")
                  .insert(insertRow as never)
                  .select("id")
                  .single();
                if (error) throw new Error(error.message);
                productId = (createdRow as { id: string } | null)?.id ?? null;
                summary.created += 1;
              }

              if (p.variants && productId) {
                await syncVariants(supabaseAdmin, productId, p.variants);
              }

            } catch (e) {
              summary.errors.push({
                vaitto_sku: p.vaitto_sku,
                message: e instanceof Error ? e.message : String(e),
              });
            }
          }

          return json(summary, 200);
        } catch (e) {
          return errorEnvelope(
            e instanceof Error ? e.message : String(e),
            500,
          );
        }
      },
    },
  },
});
