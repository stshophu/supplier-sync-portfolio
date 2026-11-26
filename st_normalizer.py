#!/usr/bin/env python3
"""
st_normalizer.py — ST system normalizer for Shopify products (DRY RUN ONLY).

Reads the central mapping table (VAITTO Supabase: taxonomy_map / taxonomy_canon)
and computes, for every Shopify product, the target values:
  category, subcategory, colour, size(s), gender, product type "{Gender} {Subcategory}".
Writes a CSV report (before/after) and prints summary counts.
It never writes to Shopify. Writing is a separate, later step after approval.

Env:
  SHOPIFY_DOMAIN, SHOPIFY_TOKEN                 (read access to products)
  VAITTO_SUPABASE_URL, VAITTO_SUPABASE_SERVICE_KEY   (read taxonomy)
Local test:
  ST_TAXONOMY_JSON=snapshot.json  ST_PRODUCTS_JSON=products.json  python st_normalizer.py
Options:
  --out report.csv   --status active|any   --limit N
"""
import csv, json, os, re, sys, argparse, time
from collections import Counter

import requests

API_VERSION = "2024-10"
COLOUR_OPTION_NAMES = {"color", "colour", "colore", "farbe"}
SIZE_OPTION_NAMES = {"size", "taglia", "größe", "grosse"}
GENDER_WORDS = {
    "women": "Women", "woman": "Women", "female": "Women", "donna": "Women",
    "men": "Men", "man": "Men", "male": "Men", "uomo": "Men",
    "unisex": "Unisex", "kids": "Kids", "junior": "Kids", "boy": "Kids", "girl": "Kids",
}


# ---------------------------------------------------------------- taxonomy
def load_taxonomy():
    path = os.environ.get("ST_TAXONOMY_JSON")
    if path:
        return json.load(open(path, encoding="utf-8"))
    url = os.environ.get("VAITTO_SUPABASE_URL")
    key = os.environ.get("VAITTO_SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing VAITTO_SUPABASE_URL / VAITTO_SUPABASE_SERVICE_KEY (or ST_TAXONOMY_JSON).")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    def get(table, params):
        r = requests.get(f"{url}/rest/v1/{table}", params=params, headers=h, timeout=60)
        if r.status_code != 200:
            host = url.split("//")[-1].split(".")[0][:6] + "..."
            try:
                paths = requests.get(f"{url}/rest/v1/", headers=h, timeout=60).json().get("paths", {})
                hint = f"exposed tables: {len(paths)}: {','.join(sorted(p.strip('/') for p in paths))}"
            except Exception as e:
                hint = f"(could not list tables: {e})"
            raise RuntimeError(f"Taxonomy read failed ({table}) host={host}: HTTP {r.status_code} {r.text[:200]} | {hint}")
        return r.json()
    try:
        rows = get("taxonomy_map", {"select": "kind,term,canonical,parent,scope,priority",
                                    "approved": "eq.true", "limit": "5000"})
        canon = get("taxonomy_canon", {"select": "kind,value,sort", "order": "sort"})
    except RuntimeError as e:
        snap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "st_taxonomy_snapshot.json")
        print(f"WARNING: {e}\nWARNING: using snapshot file {snap} instead.", flush=True)
        return json.load(open(snap, encoding="utf-8"))
    colour = {}
    for r in rows:
        if r["kind"] == "colour":
            colour.setdefault(r["canonical"], []).append(r["term"])
    sub = [[r["term"], r["canonical"], r["parent"], r["scope"], r["priority"] or 0]
           for r in rows if r["kind"] == "subcategory"]
    return {"colour": colour, "subcategory": sub,
            "canon_colour": [c["value"] for c in canon if c["kind"] == "colour"]}


class Matcher:
    def __init__(self, tax):
        self.colour_terms = []  # (term_lower, canonical)
        for canon, terms in tax["colour"].items():
            for t in terms:
                self.colour_terms.append((t.lower(), canon))
        self.colour_terms.sort(key=lambda x: -len(x[0]))
        self.colour_re = [(re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])"), c, t)
                          for t, c in self.colour_terms]
        self.sub = sorted(tax["subcategory"], key=lambda r: (-(r[4] or 0), -len(r[0]), r[0]))
        self.sub_re = [(re.compile(r"(?<![a-z])" + re.escape(r[0].lower()) + r"s?(?![a-z])"), r) for r in self.sub]

    def colour(self, text):
        """leftmost match, then longest (same as SQL st_match_colour)."""
        t = (text or "").lower()
        best = None
        for rx, canon, term in self.colour_re:
            m = rx.search(t)
            if m and (best is None or (m.start(), -len(term)) < (best[0], -len(best[2]))):
                best = (m.start(), canon, term)
        return (best[1], best[2]) if best else (None, None)

    def subcategory(self, text):
        """priority desc, length desc, position desc (same tie-break as the SQL trigger)."""
        t = (text or "").lower()
        hits = []
        for rx, r in self.sub_re:
            ms = list(rx.finditer(t))
            if ms:
                hits.append((-(r[4] or 0), -len(r[0]), -ms[-1].start(), r[0], r))
        if not hits:
            return None, None
        hits.sort()
        r = hits[0][4]
        return r[1], r[2]  # subcategory, category(parent)


# ---------------------------------------------------------------- sizes
LETTER = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL", "6XL"}


def normalize_size(raw, category=None):
    """Return (normalized, size_type). Format only, no IT/EU conversion."""
    if raw is None:
        return None, "empty"
    s = str(raw).strip()
    if not s:
        return None, "empty"
    s = s.replace("½", ".5").replace(",", ".")
    u = s.upper().strip()
    if u in {"UNI", "OS", "ONE SIZE", "ONESIZE", "TU", "U", "UNICA", "UNIQUE", "NOSIZE", "NO SIZE"}:
        return "One Size", "one"
    if re.fullmatch(r"\d+(\.\d+)?\s*(CM|MM)", u):
        return u.lower().replace(" ", ""), "measure"      # lens / length, not a size filter value
    if re.fullmatch(r"[IVX]+", u) and u not in {"X"}:
        return u, "roman"
    if re.fullmatch(r"W\d{2}(L\d{2})?", u):
        return u, "waist"
    u = {"XXXL": "3XL", "XXXXL": "4XL", "2XL": "XXL", "SM": "S-M", "ML": "M-L"}.get(u, u)
    u = re.sub(r"\s*[/]\s*", "-", u)
    m = re.fullmatch(r"(IT|EU|US|UK|FR|DE)\s*(\d+(\.\d+)?)", u)
    if m:
        return f"{m.group(1)} {m.group(2)}", "prefixed"
    if re.fullmatch(r"\d{1,2}Y", u) or re.fullmatch(r"\d{1,2}M", u):
        return u, "kids"
    if u in LETTER:
        return u, "letter"
    if re.fullmatch(r"(XXS|XS|S|M|L|XL|XXL|3XL)-(XXS|XS|S|M|L|XL|XXL|3XL)", u):
        return u, "letter-range"
    if re.fullmatch(r"\d+(\.\d+)?-\d+(\.\d+)?", u):
        return u, "number-range"
    if re.fullmatch(r"\d+(\.\d+)?", u):
        n = float(u)
        if category == "Shoes":
            return (f"{n:g}", "shoe")
        if category == "Clothing":
            return (f"{n:g}", "clothing-number")
        return (f"{n:g}", "number")
    return u, "unknown"


# ---------------------------------------------------------------- shopify
def shopify_products(status="active", limit=None):
    path = os.environ.get("ST_PRODUCTS_JSON")
    if path:
        data = json.load(open(path, encoding="utf-8"))
        yield from (data[:limit] if limit else data)
        return
    dom, tok = os.environ.get("SHOPIFY_DOMAIN"), os.environ.get("SHOPIFY_TOKEN")
    if not dom or not tok:
        sys.exit("Missing SHOPIFY_DOMAIN / SHOPIFY_TOKEN (or ST_PRODUCTS_JSON).")
    url = f"https://{dom}/admin/api/{API_VERSION}/graphql.json"
    hdr = {"X-Shopify-Access-Token": tok, "Content-Type": "application/json"}
    q = """query($after:String,$q:String){ products(first:100, after:$after, query:$q){
      pageInfo{hasNextPage endCursor}
      nodes{ id title productType tags status options{name values}
        fcat: metafield(namespace:"filter",key:"category"){value}
        fsub: metafield(namespace:"filter",key:"subcategory"){value} } } }"""
    after, n = None, 0
    while True:
        for attempt in range(6):
            r = requests.post(url, headers=hdr, timeout=90,
                              json={"query": q, "variables": {"after": after,
                                    "q": "status:active" if status == "active" else None}})
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1)); continue
            break
        d = r.json()
        if "errors" in d:
            sys.exit(f"Shopify error: {d['errors']}")
        p = d["data"]["products"]
        for node in p["nodes"]:
            yield node
            n += 1
            if limit and n >= limit:
                return
        if not p["pageInfo"]["hasNextPage"]:
            return
        after = p["pageInfo"]["endCursor"]
        time.sleep(0.3)


# ---------------------------------------------------------------- logic
def size_group(g, cat, types):
    """Size filter group: Women/Men Shoes|Clothing|Accessories; Bags (and one-size items) = One Size."""
    if cat == "Bags" or types == {"one"}:
        return "One Size"
    if cat in ("Shoes", "Clothing", "Accessories") and g in ("Women", "Men", "Unisex", "Kids"):
        return f"{g} {cat}"
    return ""


def gender_of(p):
    for t in p.get("tags") or []:
        g = GENDER_WORDS.get(t.strip().lower())
        if g:
            return g
    first = (p.get("productType") or "").split(" ")[0].lower()
    return GENDER_WORDS.get(first)


def process(p, mt):
    title = p.get("title") or ""
    opts = {(o["name"] or "").lower(): o["values"] for o in p.get("options") or []}
    colour_vals = next((v for k, v in opts.items() if k in COLOUR_OPTION_NAMES), [])
    size_vals = next((v for k, v in opts.items() if k in SIZE_OPTION_NAMES), [])

    sub, cat = mt.subcategory(title)
    if not sub:
        sub, cat = mt.subcategory(p.get("productType") or "")
    flags = []
    if not sub:
        flags.append("no_subcategory")
    if sub == "Hosiery" and re.search(r"(?<![a-z])(socks?|calze|calzini)(?![a-z])", title.lower()):
        cat = "Accessories"   # decision 2026-11-26: socks count as Accessories (VAITTO keeps Clothing)

    colours, how = [], ""
    for v in colour_vals:
        parts = [x for x in re.split(r"\s*/\s*", v) if x] if "/" in v else [v]
        found = []
        for part in parts:
            c, term = mt.colour(part)
            if c:
                found.append(c)
            else:
                flags.append(f"unmapped_colour:{part}")
        found = list(dict.fromkeys(found))
        if len(found) >= 3:
            found = ["Multicolor"]
        colours.extend(found)
        if len(parts) > 1:
            flags.append("multi_colour_value")
    if not colour_vals:
        c, term = mt.colour(" ".join(title.split()[-2:]))
        if c:
            colours.append(c); how = "from_title"
        else:
            flags.append("no_colour")
    colours = list(dict.fromkeys(colours))

    sizes, types = [], set()
    for v in size_vals:
        n, ty = normalize_size(v, cat)
        types.add(ty)
        if n:
            sizes.append(n)
        if ty in ("unknown", "measure", "roman", "waist"):
            flags.append(f"size_{ty}:{v}")
    if cat == "Shoes" and "letter" in types:
        flags.append("shoe_with_letter_sizes")
    if cat == "Clothing" and "shoe" in types:
        flags.append("clothing_with_shoe_sizes")

    g = gender_of(p)
    if not g:
        flags.append("no_gender")
    new_type = f"{g} {sub}" if (g and sub) else ""
    cur_cat = (p.get("fcat") or {}).get("value") if p.get("fcat") else None
    cur_sub = (p.get("fsub") or {}).get("value") if p.get("fsub") else None
    return {
        "id": p["id"].split("/")[-1], "title": title,
        "type_before": p.get("productType"), "type_after": new_type,
        "category_before": cur_cat, "category_after": cat,
        "subcategory_before": cur_sub, "subcategory_after": sub,
        "colour_before": "|".join(colour_vals), "colour_after": "|".join(colours),
        "size_before": "|".join(size_vals), "size_after": "|".join(sizes),
        "size_types": "|".join(sorted(types)), "size_group": size_group(g, cat, types), "gender": g or "",
        "changes": "|".join(k for k, a, b in [
            ("type", p.get("productType"), new_type),
            ("category", cur_cat, cat), ("subcategory", cur_sub, sub)] if b and a != b),
        "flags": ";".join(dict.fromkeys(flags)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="st_normalizer_report.csv")
    ap.add_argument("--status", default="active", choices=["active", "any"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    mt = Matcher(load_taxonomy())
    rows = [process(p, mt) for p in shopify_products(a.status, a.limit)]
    if not rows:
        sys.exit("No products read.")
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    n = len(rows)
    flagc = Counter(fl.split(":")[0] for r in rows for fl in r["flags"].split(";") if fl)
    chg = Counter(c for r in rows for c in r["changes"].split("|") if c)
    print(f"Products: {n}  (DRY RUN, nothing written to Shopify)")
    print(f"Would change: type={chg['type']} category={chg['category']} subcategory={chg['subcategory']}")
    print("Flags:", dict(flagc.most_common()))
    print(f"Report: {a.out}")


if __name__ == "__main__":
    main()
