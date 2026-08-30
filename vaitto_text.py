#!/usr/bin/env python3
"""
English product text for the EU-WAR-3 price feed.

Replaces the dead CatalogFeed (a Google Sheets export that has been returning
400 since at least 29.08.2026, silently, leaving all 392 products with a
synthesised title like "Tom Ford Bras Black" and a description containing only
"Material: … / Colour: …").

Everything the CatalogFeed used to supply is already in the price feed — it is
just in Italian. This module turns it into English and hands back the same
`{code: {"title": …, "description": …}}` shape that
`catalog_import.build_product_group_price_feed` already expects, so nothing in
catalog_import.py has to change.

Three strategies, chosen per field by how much translation risk each carries:

  Title        dictionary. `tipologia prodotto` (57 values) and `modello`
               (89 values) are closed vocabularies — the same approach the
               Rewix sync uses in rewix-translate.server.ts. Also fixes the
               garment mislabelling: "Tom Ford Bras Black" is a swimsuit, and
               the feed says so in `tipologia prodotto` = "Costumi da bagno".

  Composition  dictionary. Fabric names are a closed set (~30 words). Parsed
               out of the "Composizione:" line, present on 461 of 584 rows.

  Prose        machine translation with a Supabase-backed cache. Real
               sentences; a word-by-word dictionary would produce broken
               English on a luxury catalogue. Each distinct string is paid for
               once, ever — the cache is keyed by sha256 of the source text, so
               re-runs and unchanged products cost nothing.

Env:
  DEEPL_API_KEY                 optional. Without it, prose is omitted and the
                                structured English (garment, colour, material,
                                composition, origin) is still produced.
  DEEPL_API_URL                 optional override; defaults to the free tier.
  VAITTO_SUPABASE_URL           required for the translation cache
  VAITTO_SUPABASE_SERVICE_KEY   required for the translation cache
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

import requests

log = logging.getLogger(__name__)

# Translation provider. "mymemory" needs no key and no card; "deepl" is better
# quality if a key is available. "none" disables prose translation entirely.
PROVIDER = (os.environ.get("TRANSLATE_PROVIDER", "")
            or ("deepl" if os.environ.get("DEEPL_API_KEY") else "mymemory")).lower()

DEEPL_KEY = os.environ.get("DEEPL_API_KEY", "")
DEEPL_URL = os.environ.get("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")

# MyMemory: free, no signup. Supplying a contact address raises the daily
# allowance from 5,000 to 50,000 characters. Each request takes one short
# string, so long descriptions are split on sentence boundaries first.
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_EMAIL = os.environ.get("MYMEMORY_EMAIL", "")
MYMEMORY_CHUNK = 480          # the API rejects q longer than ~500 bytes
SB_URL = os.environ.get("VAITTO_SUPABASE_URL", "")
SB_KEY = os.environ.get("VAITTO_SUPABASE_SERVICE_KEY", "")

# DeepL accepts up to 50 text params per request.
DEEPL_BATCH = 50
# Supabase PostgREST caps a response at 1000 rows; page the cache reads.
CACHE_PAGE = 500

# ── Garment type: `tipologia prodotto` ───────────────────────────────────────
TIPOLOGIA_EN: dict[str, str] = {
    "abiti eleganti": "Evening Dress", "abiti corti": "Short Dress",
    "abiti lunghi": "Long Dress", "ballerine": "Ballet Flats",
    "bikini": "Bikini", "borse": "Bag", "borse a mano": "Handbag",
    "borse a spalla": "Shoulder Bag", "calze": "Socks", "camicie": "Shirt",
    "canottiere": "Tank Top", "cappelli": "Hat", "cardigan": "Cardigan",
    "cinture": "Belt", "costumi": "Swimwear", "costumi interi": "One-Piece Swimsuit",
    "costumi da bagno": "Swimsuit", "cravatte": "Tie", "decollete": "Pumps",
    "dolcevita": "Turtleneck", "espadrillas": "Espadrilles",
    "fazzoletti": "Pocket Square", "felpe": "Sweatshirt", "giacche": "Jacket",
    "gilet": "Vest", "giubbini": "Jacket", "gonne": "Skirt", "jeans": "Jeans",
    "maglie": "Knitwear", "maglioni": "Sweater", "mocassini": "Loafers",
    "occhiali da sole": "Sunglasses", "pochette": "Clutch",
    "pantaloncini": "Shorts", "pantaloni": "Trousers",
    "pantaloni tuta": "Track Pants", "papillon": "Bow Tie",
    "piumini": "Down Jacket", "polo": "Polo Shirt", "portacarte": "Card Holder",
    "portafogli": "Wallet", "reggiseni": "Bra", "sandali": "Sandals",
    "sandali con tacco": "Heeled Sandals", "sciarpe": "Scarf", "shorts": "Shorts",
    "slip": "Briefs", "slip-on": "Slip-On", "slippers": "Slippers",
    "smanicati": "Sleeveless Jacket", "sneakers": "Sneakers",
    "stivaletti": "Ankle Boots", "stivali": "Boots", "t-shirt": "T-Shirt",
    "top": "Top", "trench": "Trench Coat", "tute": "Tracksuit",
}

# ── Cut / style: `modello`. '' means "recognised but adds nothing" → dropped. ─
MODELLO_EN: dict[str, str] = {
    "a balze": "Ruffled", "a campana": "Bell-Shaped", "a collo alto": "High Neck",
    "a sigaretta": "Cigarette", "a triangolo": "Triangle", "a tubo": "Tube",
    "anfibi": "Combat Boots", "basic": "", "bomber": "Bomber",
    "borse a spalla": "Shoulder Bag", "cargo": "Cargo", "cat-eye": "Cat-Eye",
    "chino": "Chino", "due bottoni": "Two-Button", "espadrillas": "Espadrilles",
    "flare": "Flared", "girocollo": "Crew Neck", "layered": "Layered",
    "lungo": "Long", "maglia": "Knit", "mule": "Mule", "off road": "Off-Road",
    "pantaloni": "Trousers", "parka": "Parka", "penny": "Penny",
    "piumino": "Down Jacket", "pochette": "Clutch",
    "porta carte di credito": "Card Holder", "pullover": "Pullover",
    "relaxed": "Relaxed", "relaxed fit": "Relaxed Fit", "skinny": "Skinny",
    "slim fit": "Slim Fit", "smanicato": "Sleeveless",
    "stivali chelsea": "Chelsea Boots", "t-shirt": "T-Shirt", "thea": "",
    "v-neck": "V-Neck", "a camicia": "Shirt-Style", "ankle boot": "Ankle Boot",
    "baseball": "Baseball", "bermuda": "Bermuda", "bikini": "Bikini",
    "blazer": "Blazer", "boxer": "Swim Shorts", "boxy": "Boxy", "caban": "Peacoat",
    "canotta": "Tank Top", "carcoat": "Car Coat", "cardigan": "Cardigan",
    "casacca": "Tunic", "dolcevita": "Turtleneck", "due pezzi": "Two-Piece",
    "felpa": "Sweatshirt", "field jacket": "Field Jacket",
    "giacca monopetto": "Single-Breasted Jacket", "gioiello": "Embellished",
    "incrociato": "Wrap", "intero": "One-Piece", "jeans": "Jeans",
    "jogger": "Jogger", "jogging": "Jogging", "longuette": "Midi",
    "lupetto": "Mock Neck", "lurex": "Lurex", "maniche corte": "Short-Sleeve",
    "maniche lunghe": "Long-Sleeve", "mezze maniche": "Half-Sleeve",
    "midi": "Midi", "minigonne": "Mini Skirt", "mocassini": "Loafers",
    "over size": "Oversize", "overshirt": "Overshirt", "palazzo": "Palazzo",
    "platform": "Platform", "polo": "Polo", "reggiseno": "Bra",
    "regular": "Regular", "running": "Running", "sartoriale": "Tailored",
    "shorts": "Shorts", "shoulder bag": "Shoulder Bag", "slip on": "Slip-On",
    "slippers": "Slippers", "smoking": "Tuxedo", "sneakers": "Sneakers",
    "sportivo": "Sporty", "tronchetto": "Ankle Boot", "vita alta": "High-Waisted",
}

# ── Fabrics inside the "Composizione:" line ──────────────────────────────────
FABRIC_EN: dict[str, str] = {
    "acetato": "Acetate", "acrilico": "Acrylic", "alpaca": "Alpaca",
    "cashmere": "Cashmere", "cashmer": "Cashmere", "cotone": "Cotton",
    "cupro": "Cupro", "elastan": "Elastane", "elastane": "Elastane",
    "elastomultiestere": "Elastomultiester", "gomma": "Rubber", "lana": "Wool",
    "lino": "Linen", "lyocell": "Lyocell", "modal": "Modal", "mohair": "Mohair",
    "nylon": "Nylon", "pelle": "Leather", "poliamide": "Polyamide",
    "poliammide": "Polyamide", "polyamide": "Polyamide",
    "poliestere": "Polyester", "polyestere": "Polyester",
    "poliuretano": "Polyurethane", "polyurethane": "Polyurethane",
    "seta": "Silk", "viscosa": "Viscose", "metallo": "Metal",
    "metallizzata": "Metallic", "metallizzato": "Metallic",
    "vergine": "Virgin", "fibra": "Fibre", "tessuto": "Fabric",
    # structural labels, not fabrics
    "corpo": "Body", "fodera": "Lining", "interno": "Inner",
    "contrasto": "Contrast", "applicazioni": "Trims",
}

# Garment families, used to decide whether `modello` may override
# `tipologia prodotto`. The supplier's `modello` column is noisy — one men's
# jacket in the current feed carries modello="minigonne" — so a cut is only
# allowed to replace the garment when the two agree on family. A cut with no
# family entry is a pure modifier (Slim Fit, Crew Neck, High-Waisted) and is
# always safe to use as a prefix.
GARMENT_FAMILY: dict[str, str] = {
    # outerwear
    "Jacket": "outerwear", "Down Jacket": "outerwear", "Bomber": "outerwear",
    "Parka": "outerwear", "Peacoat": "outerwear", "Car Coat": "outerwear",
    "Field Jacket": "outerwear", "Trench Coat": "outerwear", "Blazer": "outerwear",
    "Vest": "outerwear", "Sleeveless Jacket": "outerwear", "Overshirt": "outerwear",
    "Single-Breasted Jacket": "outerwear",
    # skirts / dresses
    "Skirt": "skirt", "Mini Skirt": "skirt",
    "Short Dress": "dress", "Long Dress": "dress", "Evening Dress": "dress",
    "Tunic": "dress",
    # legwear
    "Trousers": "trousers", "Jeans": "trousers", "Shorts": "trousers",
    "Bermuda": "trousers", "Track Pants": "trousers", "Chino": "trousers",
    "Jogger": "trousers",
    # shoes
    "Sneakers": "shoes", "Boots": "shoes", "Ankle Boots": "shoes",
    "Ankle Boot": "shoes", "Chelsea Boots": "shoes", "Combat Boots": "shoes",
    "Loafers": "shoes", "Sandals": "shoes", "Heeled Sandals": "shoes",
    "Pumps": "shoes", "Ballet Flats": "shoes", "Espadrilles": "shoes",
    "Slippers": "shoes", "Slip-On": "shoes", "Mule": "shoes",
    # bags / small leather goods
    "Bag": "bag", "Handbag": "bag", "Shoulder Bag": "bag", "Clutch": "bag",
    "Card Holder": "bag", "Wallet": "bag",
    # knitwear
    "Knitwear": "knit", "Sweater": "knit", "Cardigan": "knit",
    "Turtleneck": "knit", "Pullover": "knit",
    # swim
    "Swimwear": "swim", "Swimsuit": "swim", "One-Piece Swimsuit": "swim",
    "Bikini": "swim", "Swim Shorts": "swim",
    # tops
    "Shirt": "top", "T-Shirt": "top", "Polo Shirt": "top", "Top": "top",
    "Tank Top": "top", "Sweatshirt": "top",
    # accessories
    "Pocket Square": "accessory", "Tie": "accessory", "Bow Tie": "accessory",
    "Scarf": "accessory", "Hat": "accessory", "Belt": "accessory",
    "Sunglasses": "accessory", "Socks": "accessory", "Briefs": "accessory",
    "Bra": "accessory", "Tracksuit": "outerwear",
}

GENDER_EN = {"m": "Men", "f": "Women", "u": "Unisex",
             "uomo": "Men", "donna": "Women", "uom": "Men", "unisex": "Unisex"}

SEASON_EN = {"primavera/estate": "Spring/Summer", "autunno/inverno": "Fall/Winter",
             "continuativa": "All Year", "continuativo": "All Year"}

# Lines to strip from the supplier description before anything else.
_DROP_LINE = re.compile(r"^\s*(SKU\s+Interno|SKU\s+Brand)\s*:", re.I)
_COMPOSIZIONE = re.compile(r"Composizione\s*:\s*([^\n]+)", re.I)
_MADE_IN = re.compile(r"\bMade\s+in\s+([A-Za-zÀ-ù\s]+?)\s*\.", re.I)


def _title_case(s: str) -> str:
    return s.strip()


def translate_composition(raw: str) -> str:
    """'Corpo : 100% Poliestere , Fodera : 90% Poliammide, 10%Elastan'
       → 'Body: 100% Polyester, Lining: 90% Polyamide, 10% Elastane'"""
    if not raw:
        return ""
    # The supplier sometimes runs "SKU Interno:" onto the same line as the
    # composition; cut there so internal codes never reach the storefront.
    raw = re.split(r"[-\s]*\bSKU\b", raw, flags=re.I)[0]
    out = raw
    # Normalise the spacing the supplier uses around colons and percentages.
    out = re.sub(r"\s*:\s*", ": ", out)
    out = re.sub(r"(\d)\s*%\s*", r"\1% ", out)
    out = re.sub(r"\s*,\s*", ", ", out)

    def sub_word(m: re.Match) -> str:
        w = m.group(0)
        en = FABRIC_EN.get(w.lower())
        return en if en else w

    out = re.sub(r"[A-Za-zÀ-ù]{3,}", sub_word, out)
    return re.sub(r"\s+", " ", out).strip(" ,;")


def parse_description(raw: str) -> dict[str, str]:
    """Split the supplier's Italian blob into its parts.

    Returns {'prose': …, 'composition': …, 'made_in': …} — all Italian except
    composition, which is translated here because its vocabulary is closed.
    """
    if not raw:
        return {"prose": "", "composition": "", "made_in": ""}

    # The supplier emits U+2028/U+2029 as well as \r\n; str.split("\n")
    # does not treat those as line breaks, which hid the "Composizione:" line
    # on 140 of 360 products.
    text = raw.replace("\r\n", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    kept = [ln for ln in text.split("\n") if not _DROP_LINE.match(ln)]
    text = "\n".join(kept)

    composition = ""
    m = _COMPOSIZIONE.search(text)
    if m:
        composition = translate_composition(m.group(1))
        text = text[: m.start()] + text[m.end():]

    made_in = ""
    m = _MADE_IN.search(text)
    if m:
        made_in = m.group(1).strip().title()
        text = text[: m.start()] + text[m.end():]

    # Internal codes also appear mid-line ("… 4 cm - SKU Interno: 23NOV56"),
    # where the line-based filter above cannot see them. Left in place they get
    # translated too, and surface as "Inner sku:23NOV56" in the storefront.
    # SKU markers only ever trail the copy, so cut everything from the first one.
    text = re.split(r"[-–—\s]*\bSKU\b.*", text, flags=re.I | re.S)[0]

    prose = re.sub(r"\n{2,}", "\n", text).strip(" \n-–—\t")
    return {"prose": prose, "composition": composition, "made_in": made_in}


# ── Translation cache (Supabase) ─────────────────────────────────────────────

def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _cache_headers() -> dict[str, str]:
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json"}


def cache_get(hashes: list[str]) -> dict[str, str]:
    """hash → translated_text for whatever is already cached."""
    if not (SB_URL and SB_KEY and hashes):
        return {}
    found: dict[str, str] = {}
    url = f"{SB_URL.rstrip('/')}/rest/v1/translation_cache"
    for i in range(0, len(hashes), CACHE_PAGE):
        chunk = hashes[i:i + CACHE_PAGE]
        try:
            r = requests.get(
                url,
                headers=_cache_headers(),
                params={"select": "source_hash,translated_text",
                        "source_hash": f"in.({','.join(chunk)})"},
                timeout=30,
            )
            if r.status_code != 200:
                log.warning("  translation cache read failed: %s %s",
                            r.status_code, r.text[:200])
                continue
            for row in r.json():
                found[row["source_hash"]] = row["translated_text"]
        except Exception as exc:            # cache is an optimisation, never fatal
            log.warning("  translation cache read error: %s", exc)
    return found


def cache_put(items: list[tuple[str, str, str]]) -> None:
    """items = [(hash, source_text, translated_text), …]"""
    if not (SB_URL and SB_KEY and items):
        return
    url = f"{SB_URL.rstrip('/')}/rest/v1/translation_cache"
    payload = [{"source_hash": h, "source_text": s, "translated_text": t,
                "source_lang": "IT", "target_lang": "EN", "provider": PROVIDER}
               for h, s, t in items]
    for i in range(0, len(payload), 200):
        try:
            r = requests.post(
                url,
                headers={**_cache_headers(),
                         "Prefer": "resolution=ignore-duplicates,return=minimal"},
                json=payload[i:i + 200],
                timeout=60,
            )
            if r.status_code >= 300:
                log.warning("  translation cache write failed: %s %s",
                            r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("  translation cache write error: %s", exc)


def deepl_translate(texts: list[str]) -> list[str]:
    """Italian → English. Returns [] on any failure so callers degrade to the
    structured-only description rather than blocking the whole import."""
    if not (DEEPL_KEY and texts):
        return []
    out: list[str] = []
    for i in range(0, len(texts), DEEPL_BATCH):
        batch = texts[i:i + DEEPL_BATCH]
        try:
            r = requests.post(
                DEEPL_URL,
                headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
                data=[("text", t) for t in batch]
                     + [("source_lang", "IT"), ("target_lang", "EN-GB")],
                timeout=90,
            )
            if r.status_code != 200:
                log.warning("  DeepL %s: %s", r.status_code, r.text[:200])
                return []
            out.extend(x["text"] for x in r.json().get("translations", []))
        except Exception as exc:
            log.warning("  DeepL request failed: %s", exc)
            return []
    return out



def _split_sentences(text: str, limit: int = MYMEMORY_CHUNK) -> list[str]:
    """Break a description into pieces small enough for MyMemory, preferring
    sentence boundaries so the translator still sees whole thoughts."""
    parts, buf = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > limit:                 # pathological single sentence
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(buf) + len(sentence) + 1 <= limit:
            buf = f"{buf} {sentence}".strip()
        else:
            if buf:
                parts.append(buf)
            buf = sentence
    if buf:
        parts.append(buf)
    return parts


def mymemory_translate(texts: list[str]) -> list[str]:
    """Italian → English via MyMemory. No key required. Returns [] on failure."""
    out: list[str] = []
    for text in texts:
        pieces: list[str] = []
        for chunk in _split_sentences(text):
            params = {"q": chunk, "langpair": "it|en"}
            if MYMEMORY_EMAIL:
                params["de"] = MYMEMORY_EMAIL
            try:
                r = requests.get(MYMEMORY_URL, params=params, timeout=30)
                if r.status_code != 200:
                    log.warning("  MyMemory %s: %s", r.status_code, r.text[:160])
                    return []
                body = r.json()
                translated = (body.get("responseData") or {}).get("translatedText") or ""
                # The free tier answers 200 with a quota message in the payload.
                if "MYMEMORY WARNING" in translated.upper() or not translated:
                    log.warning("  MyMemory quota reached — %s", translated[:120])
                    return []
                pieces.append(translated)
            except Exception as exc:
                log.warning("  MyMemory request failed: %s", exc)
                return []
        out.append(" ".join(pieces))
    return out


def translate_batch(texts: list[str]) -> list[str]:
    """Dispatch to whichever provider is configured."""
    if PROVIDER == "none" or not texts:
        return []
    if PROVIDER == "deepl":
        return deepl_translate(texts)
    return mymemory_translate(texts)


def translate_prose(prose_by_code: dict[str, str]) -> dict[str, str]:
    """code → English prose. Cached strings cost nothing; only genuinely new
    text reaches DeepL."""
    unique: dict[str, str] = {}                 # hash → source
    for text in prose_by_code.values():
        if text.strip():
            unique[_hash(text)] = text

    cached = cache_get(list(unique))
    missing = {h: s for h, s in unique.items() if h not in cached}
    log.info("  prose: %d distinct · %d cached · %d to translate",
             len(unique), len(cached), len(missing))

    if missing:
        if PROVIDER == "none" or (PROVIDER == "deepl" and not DEEPL_KEY):
            log.warning("  prose translation disabled — %d products keep "
                        "structured English only", len(missing))
        else:
            log.info("  translating %d strings via %s", len(missing), PROVIDER)
            hashes = list(missing)
            translated = translate_batch([missing[h] for h in hashes])
            if len(translated) == len(hashes):
                fresh = list(zip(hashes, (missing[h] for h in hashes), translated))
                cache_put(fresh)
                cached.update({h: t for h, _, t in fresh})
                log.info("  translated %d new strings", len(fresh))
            else:
                log.warning("  %s returned %d of %d — keeping structured "
                            "English only for this run", PROVIDER,
                            len(translated), len(hashes))

    return {code: cached.get(_hash(text), "")
            for code, text in prose_by_code.items() if text.strip()}



# ── Supplied English text (tier 1) ───────────────────────────────────────────
# `eu_war_3_text_en.csv` is a trimmed, de-duplicated export of the supplier's
# own BrandsGateway catalogue file: code, title_en, description_en. It covers
# the styles that existed when it was taken, so it is the cheapest and most
# accurate source — no API call, nothing that can expire. Newer styles fall
# through to DeepL, then to structured English.
CATALOG_TEXT_FILE = os.environ.get(
    "EU_WAR_3_TEXT_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "eu_war_3_text_en.csv"),
)


def load_supplied_text() -> dict[str, dict[str, str]]:
    """code → {'title', 'description'} from the committed English export."""
    import csv
    try:
        with open(CATALOG_TEXT_FILE, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        log.info("  %s not found — relying on translation only",
                 os.path.basename(CATALOG_TEXT_FILE))
        return {}
    except Exception as exc:
        log.warning("  could not read %s (%s) — relying on translation only",
                    CATALOG_TEXT_FILE, exc)
        return {}

    out: dict[str, dict[str, str]] = {}
    for r in rows:
        code = (r.get("code") or "").strip()
        if not code:
            continue
        out[code] = {"title": (r.get("title_en") or "").strip(),
                     "description": (r.get("description_en") or "").strip()}
    log.info("  supplied English text: %d styles", len(out))
    return out


# ── Title ────────────────────────────────────────────────────────────────────

def build_title(brand: str, gender_raw: str, tipologia: str, modello: str,
                colour_en: str) -> str:
    """Brand + Gender + Cut + Garment + Colour, e.g.
    'Tom Ford Men Swimsuit Burgundy' — never the raw SKU the feed's own Title
    column leads with ('26AP06 Costumi Tom Ford Uomo')."""
    garment = TIPOLOGIA_EN.get((tipologia or "").strip().lower(), "")
    if not garment and tipologia:
        garment = tipologia.strip().title()

    raw_cut = (modello or "").strip()
    cut = "" if raw_cut in ("", "-") else MODELLO_EN.get(raw_cut.lower(), "")

    # Reconcile cut against garment. `modello` is unreliable: it can name a
    # different garment entirely (a men's jacket tagged "minigonne").
    if cut and garment:
        fam_cut = GARMENT_FAMILY.get(cut)
        fam_garment = GARMENT_FAMILY.get(garment)
        cut_words = set(cut.lower().replace("-", " ").split())
        garment_words = set(garment.lower().replace("-", " ").split())
        if cut_words and cut_words <= garment_words:
            cut = ""                        # "Polo" + "Polo Shirt" → "Polo Shirt"
        elif fam_cut is None:
            pass                            # modifier — keep as a prefix
        elif fam_cut == fam_garment:
            garment, cut = cut, ""          # cut is the more specific garment
        else:
            cut = ""                        # conflict — trust `tipologia`

    gender = GENDER_EN.get((gender_raw or "").strip().lower(), "")

    parts, seen = [], set()
    for p in (brand, gender, cut, garment, colour_en):
        p = (p or "").strip()
        if not p or p.lower() in seen:
            continue
        seen.add(p.lower())
        parts.append(p)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def build_description(prose_en: str, composition: str, made_in: str,
                      material_en: str, colour_en: str, season_raw: str) -> str:
    """HTML body. Prose first when available, then the spec lines the buyer's
    storefront and any marketplace listing actually need."""
    blocks: list[str] = []
    if prose_en:
        # The supplier's own English text often ends with stray fragments
        # ("Brand", an empty bullet) and already states composition/origin.
        lines = [ln.strip(" -\t") for ln in prose_en.split("\n")]
        lines = [ln for ln in lines if ln and ln.lower() not in {"brand", "color", "colour"}]
        prose_en = "\n".join(lines)
        blocks.append(prose_en.replace("\n", "<br>"))

    low = prose_en.lower()
    bits: list[str] = []
    if colour_en and "color :" not in low and "colour:" not in low:
        bits.append(f"<strong>Colour:</strong> {colour_en}")
    if material_en and "material:" not in low:
        bits.append(f"<strong>Material:</strong> {material_en}")
    if composition and "composition" not in low:
        bits.append(f"<strong>Composition:</strong> {composition}")
    season = SEASON_EN.get((season_raw or "").strip().lower(),
                           (season_raw or "").strip())
    if season:
        bits.append(f"<strong>Season:</strong> {season}")
    if made_in and "made in" not in low:
        bits.append(f"<strong>Made in:</strong> {made_in}")
    if bits:
        blocks.append("<br>".join(bits))

    return "<br><br>".join(blocks)


# ── Entry point ──────────────────────────────────────────────────────────────

def build_text_lookup(df: Any, translate_colour=None, translate_material=None) -> dict:
    """Drop-in replacement for catalog_import.load_catalog_text_lookup().

    `df` is the already-loaded price feed (pandas DataFrame, semicolon CSV).
    Returns {code: {'title': …, 'description': …}} keyed on the feed's `Code`,
    exactly what build_product_group_price_feed() expects.
    """
    def clean(v) -> str:
        return "" if v is None else str(v).strip()

    rows_by_code: dict[str, dict] = {}
    for _, row in df.iterrows():
        code = clean(row.get("Code"))
        if code and code not in rows_by_code:
            rows_by_code[code] = row

    supplied = load_supplied_text()

    parsed = {code: parse_description(clean(r.get("Description")))
              for code, r in rows_by_code.items()}
    # Only pay to translate what the supplied file does not already cover.
    needs_translation = {c: p["prose"] for c, p in parsed.items()
                         if not supplied.get(c, {}).get("description")}
    prose_en = translate_prose(needs_translation)

    lookup: dict[str, dict[str, str]] = {}
    for code, row in rows_by_code.items():
        colour_en = (translate_colour(row.get("Color")) if translate_colour
                     else clean(row.get("Color")))
        material_en = (translate_material(row.get("material")) if translate_material
                       else clean(row.get("material")))
        given = supplied.get(code, {})
        title = given.get("title") or build_title(
            brand=clean(row.get("Brand")),
            gender_raw=clean(row.get("reparto")) or clean(row.get("Gender")),
            tipologia=clean(row.get("tipologia prodotto")),
            modello=clean(row.get("modello")),
            colour_en=colour_en,
        )
        description = build_description(
            prose_en=given.get("description") or prose_en.get(code, ""),
            composition=parsed[code]["composition"],
            made_in=parsed[code]["made_in"],
            material_en=material_en,
            colour_en=colour_en,
            season_raw=clean(row.get("Season")),
        )
        if title or description:
            lookup[code] = {"title": title, "description": description}

    log.info("  built English text for %d styles from the price feed", len(lookup))
    return lookup
