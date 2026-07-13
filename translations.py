#!/usr/bin/env python3
"""
translations.py — Italian -> English translation for Supplier feeds.

Grounded in the actual distinct values found in the CatalogFeed feed across
gender / category_1 / category_2 / colours / material / country columns.

Design notes:
- gender / category / colour are small closed sets  -> direct dictionaries.
- category_2 uses a "PrimaryType - Subtype" pattern; we translate the
  PRIMARY type (part before " - ") since that maps to Shopify product_type.
- material is mostly composition strings ("95% cotone 5% elastane"), so we
  TOKEN-REPLACE base Italian material words; percentages pass through.
- country_of_origin is ISO-3166 alpha-2 codes -> expanded to English names.
- Header-row leakage ("SEX","Category","Color","Material","Made In"...) is
  filtered via is_header_junk().
"""

import re

HEADER_JUNK = {
    "sex", "category", "subcategory", "color", "colour", "material",
    "made in", "brand", "model", "add",
}

def is_header_junk(value):
    if value is None:
        return True
    return str(value).strip().lower() in HEADER_JUNK


# ── GENDER ──────────────────────────────────────────────────────────────
GENDER_MAP = {
    "uomo": "Men", "donna": "Women", "bambino": "Kids", "bambina": "Kids",
    "unisex": "Unisex", "men": "Men", "women": "Women", "kids": "Kids",
    # PriceFeed single-letter codes
    "m": "Men", "f": "Women", "u": "Unisex",
}

def translate_gender(value):
    if is_header_junk(value):
        return None
    return GENDER_MAP.get(str(value).strip().lower(), str(value).strip())


# ── CATEGORY (primary type -> Shopify product_type) ─────────────────────
CATEGORY_MAP = {
    "abbigliamento": "Clothing", "accessori": "Accessories",
    "calzature": "Footwear", "gioielleria": "Jewelry",
    "intimo": "Underwear", "valigeria": "Luggage",
    "abiti": "Dresses", "abiti eleganti": "Evening Dresses",
    "abiti corti": "Short Dresses", "abiti lunghi": "Long Dresses",
    "ballerine": "Ballet Flats", "bermuda": "Bermuda Shorts",
    "bikini": "Bikinis", "borsa a mano": "Handbags", "borse": "Bags",
    "borse a mano": "Handbags", "borse a spalla": "Shoulder Bags",
    "bracciali": "Bracelets", "calze": "Socks", "camicie": "Shirts",
    "canottiere": "Tank Tops", "cappelli": "Hats", "cappotti": "Coats",
    "cardigan": "Cardigans", "ciabatte": "Slippers", "cinture": "Belts",
    "costumi": "Swimwear", "costumi interi": "One-Piece Swimsuits",
    "costumi da bagno": "Swimwear", "cravatte": "Ties",
    "decollete": "Pumps", "dolcevita": "Turtlenecks",
    "espadrillas": "Espadrilles", "fazzoletti": "Pocket Squares",
    "felpe": "Sweatshirts", "giacche": "Jackets", "gilet": "Vests",
    "gioielli (altro)": "Jewelry", "giubbini": "Jackets",
    "gonne": "Skirts", "guanti": "Gloves", "jeans": "Jeans",
    "lupetti": "Turtlenecks", "maglie": "Knitwear",
    "maglieria": "Knitwear", "maglioni": "Sweaters",
    "maglioni dolcevita": "Turtleneck Sweaters", "mocassini": "Loafers",
    "occhiali da sole": "Sunglasses", "orecchini": "Earrings",
    "pochette": "Clutches", "pantaloncini": "Shorts",
    "pantaloni": "Trousers", "pantaloni tuta": "Sweatpants",
    "papillon": "Bow Ties", "piumini": "Down Jackets",
    "polo": "Polo Shirts", "portacarte": "Card Holders",
    "portafogli": "Wallets", "reggiseni": "Bras", "sandali": "Sandals",
    "sandali con tacco": "Heeled Sandals", "scarpe": "Shoes",
    "sciarpe": "Scarves", "shorts": "Shorts", "slip": "Briefs",
    "slip-on": "Slip-Ons", "slippers": "Slippers",
    "smanicati": "Sleeveless Jackets", "sneakers": "Sneakers",
    "sneakers difettato": "Sneakers", "stivaletti": "Ankle Boots",
    "stivali": "Boots", "stivali difettato": "Boots",
    "t-shirt": "T-Shirts", "t-shirt e polo": "T-Shirts & Polos",
    "telo mare": "Beach Towels", "top": "Tops", "tote": "Tote Bags",
    "trench": "Trench Coats", "tute": "Jumpsuits",
    "infradito": "Flip-Flops", "marsupio": "Belt Bags",
}

def translate_category(value):
    if is_header_junk(value):
        return None
    raw = str(value).strip()
    if not raw or raw == "-":
        return None
    primary = raw.split(" - ")[0].strip()
    key = primary.lower().strip(" -")
    return CATEGORY_MAP.get(key, primary)


# ── COLOUR ──────────────────────────────────────────────────────────────
COLOUR_MAP = {
    "antracite": "Anthracite", "arancione": "Orange", "argento": "Silver",
    "avorio": "Ivory", "azzurro": "Light Blue", "beige": "Beige",
    "bianco": "White", "bianco crema": "Cream White",
    "bianco perla": "Pearl White", "blu": "Blue", "blu avio": "Avio Blue",
    "blu chiaro": "Light Blue", "blu navy": "Navy Blue",
    "blu notte": "Midnight Blue", "blu scuro": "Dark Blue",
    "bordeaux": "Burgundy", "burgundy": "Burgundy", "bronzo": "Bronze",
    "celeste": "Sky Blue", "fucsia": "Fuchsia", "fuchsia": "Fuchsia",
    "fuscia": "Fuchsia", "giallo": "Yellow", "giallo chiaro": "Light Yellow",
    "grigio": "Grey", "grigio perla": "Pearl Grey",
    "grigio chiaro": "Light Grey", "grigio scuro": "Dark Grey",
    "leopardato": "Leopard Print", "lilla": "Lilac", "lime": "Lime",
    "marrone": "Brown", "marrone scuro": "Dark Brown",
    "multicolor": "Multicolor", "multicolore": "Multicolor",
    "nero": "Black", "nero e oro": "Black and Gold", "oro": "Gold",
    "prugna": "Plum", "rosa": "Pink", "rosa antico": "Antique Pink",
    "rosa fluo": "Fluo Pink", "rosso": "Red", "rosso scuro": "Dark Red",
    "senape": "Mustard", "tabacco": "Tobacco", "tortora": "Taupe",
    "turchese": "Turquoise", "verde": "Green", "verde fluo": "Fluo Green",
    "verde kaki": "Khaki Green", "verde scuro": "Dark Green",
    "verde acido": "Acid Green", "verde acqua": "Aqua Green",
    "verde militare": "Military Green", "verde petrolio": "Petrol Green",
    "viola": "Purple", "viola chiaro": "Light Purple",
}

def _translate_colour_token(tok):
    return COLOUR_MAP.get(tok.strip().lower(), tok.strip().capitalize())

def translate_colour(value):
    if is_header_junk(value):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parts = re.split(r"\s*/\s*|\s+e\s+", raw)
    return "/".join(_translate_colour_token(p) for p in parts if p.strip())


# ── MATERIAL (token replacement inside composition strings) ─────────────
MATERIAL_TOKENS = {
    "cotone": "Cotton", "seta": "Silk", "lana vergine": "Virgin Wool",
    "lana": "Wool", "pelle": "Leather", "poliestere": "Polyester",
    "poliammide": "Polyamide", "poliammidica": "Polyamide",
    "viscosa": "Viscose", "lino": "Linen", "elastane": "Elastane",
    "elastan": "Elastane", "elstan": "Elastane", "camoscio": "Suede",
    "cashmere": "Cashmere", "cashemere": "Cashmere", "cashemre": "Cashmere",
    "nappa": "Nappa Leather", "nylon": "Nylon", "acetato": "Acetate",
    "triacetato": "Triacetate", "raso": "Satin",
    "vernice": "Patent Leather", "gomma": "Rubber", "metallo": "Metal",
    "acciaio": "Steel", "acrilico": "Acrylic", "montone": "Shearling",
    "tessuto": "Fabric", "tecnico": "Technical Fabric",
    "scamosciata": "Suede", "scamociata": "Suede", "vitello": "Calfskin",
    "pitone": "Python Leather", "neoprene": "Neoprene",
    "lyocell": "Lyocell", "flax": "Linen", "suede": "Suede", "mesh": "Mesh",
    "polietilene": "Polyethylene", "polietilenica": "Polyethylene",
    "polivincloruro": "PVC", "fibrametallizzata": "Metallic Fiber",
    "metallizzato": "Metallic", "metallica": "Metallic",
    "patent": "Patent", "nabuk": "Nubuck", "fibra": "Fiber",
}

_MATERIAL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(MATERIAL_TOKENS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

def translate_material(value):
    if is_header_junk(value):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return _MATERIAL_PATTERN.sub(lambda m: MATERIAL_TOKENS[m.group(1).lower()], raw)


# ── COUNTRY (ISO-3166 alpha-2 -> English name) ──────────────────────────
COUNTRY_MAP = {
    "al": "Albania", "am": "Armenia", "bg": "Bulgaria", "ca": "Canada",
    "cn": "China", "fr": "France", "hu": "Hungary", "in": "India",
    "it": "Italy", "jp": "Japan", "mg": "Madagascar", "mn": "Mongolia",
    "pt": "Portugal", "ro": "Romania", "tn": "Tunisia", "tr": "Turkey",
    "ua": "Ukraine", "us": "United States", "vn": "Vietnam",
}

def translate_country(value):
    if is_header_junk(value):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return COUNTRY_MAP.get(raw.lower(), raw)


if __name__ == "__main__":
    samples = {
        "gender":   ["Uomo", "Donna", "Unisex", "SEX", "Men"],
        "category": ["Borse a Spalla - Le Bambino", "Abbigliamento",
                     "Giubbini - Piumino", "Category", "-"],
        "colour":   ["Nero", "Verde petrolio", "Nero/rosso", "Bianco Crema"],
        "material": ["95% cotone 5% elastane", "100% Pelle", "Cotone pelle",
                     "Pelle scamosciata"],
        "country":  ["IT", "CN", "Made In", "TR"],
    }
    fns = {"gender": translate_gender, "category": translate_category,
           "colour": translate_colour, "material": translate_material,
           "country": translate_country}
    for field, vals in samples.items():
        print(f"\n[{field}]")
        for v in vals:
            print(f"  {v!r:40} -> {fns[field](v)!r}")
