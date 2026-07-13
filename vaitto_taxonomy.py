"""
vaitto_taxonomy.py — maps feed values to Vaitto UUID references.
Call load_brands() once at session start, then use resolve_*() functions.
"""

# ── CATEGORIES ─────────────────────────────────────────────────────────────────
CATEGORY_IDS = {
    "Clothing":    "7ce80cf0-8abc-4012-9856-4ae3577010a8",
    "Shoes":       "269335d1-6456-4877-a212-76828272dc2f",
    "Bags":        "50cd953d-522d-4c50-b87b-915a05f1022d",
    "Accessories": "9f1faf56-7d36-443f-8366-c2c5e4512091",
    "Jewelry":     "4516c76f-cab0-4cb1-814c-3cfbf06f3618",
}
_CAT = {
    "clothing":"Clothing","apparel":"Clothing","abbigliamento":"Clothing",
    "jackets":"Clothing","jacket":"Clothing","coats":"Clothing","coat":"Clothing",
    "dresses":"Clothing","dress":"Clothing","pants":"Clothing","trousers":"Clothing",
    "jeans":"Clothing","shorts":"Clothing","skirts":"Clothing","skirt":"Clothing",
    "knitwear":"Clothing","sweaters":"Clothing","shirts":"Clothing","shirt":"Clothing",
    "polos":"Clothing","polo":"Clothing","t-shirts":"Clothing","tops":"Clothing",
    "hoodies":"Clothing","vests":"Clothing","jumpsuits":"Clothing",
    "swimwear":"Clothing","underwear":"Clothing","intimo":"Clothing",
    "shoes":"Shoes","calzature":"Shoes","scarpe":"Shoes",
    "sneakers":"Shoes","boots":"Shoes","sandals":"Shoes",
    "loafers":"Shoes","heels":"Shoes","flats":"Shoes",
    "bags":"Bags","borse":"Bags","handbags":"Bags","tote":"Bags",
    "clutch":"Bags","backpack":"Bags","crossbody":"Bags",
    "accessories":"Accessories","accessori":"Accessories",
    "wallets":"Accessories","belts":"Accessories","scarves":"Accessories",
    "hats":"Accessories","gloves":"Accessories","sunglasses":"Accessories",
    "ties":"Accessories","watches":"Accessories","hosiery":"Accessories",
    "jewelry":"Jewelry","gioielleria":"Jewelry","gioielli":"Jewelry",
    "bracelets":"Jewelry","earrings":"Jewelry","rings":"Jewelry","necklaces":"Jewelry",
}

# ── SUBCATEGORIES ──────────────────────────────────────────────────────────────
SUBCATEGORY_IDS = {
    "Backpack":"da0dcaa9-4de3-487b-a1d2-baf742287826",
    "Belts":"906516f9-d89e-4956-aba7-cd561592292d",
    "Boots":"20320b52-d447-45b8-b0fa-76e6fe07d9c4",
    "Bracelets":"d6ccd8a5-6c9e-4b1f-83e9-966013ba088e",
    "Clutch":"890e080d-8c38-4d92-aca3-5628bd35edf9",
    "Crossbody":"4aeb3988-0ce5-4101-9d0b-eced6a03c302",
    "Dresses":"d4e7d2fc-d19b-4e73-a9ad-a39e9025eb3e",
    "Earrings":"2dc9e53a-c598-4cb2-9d00-f06394a5d261",
    "Flats":"63602cf1-3319-459d-8eb9-ed0c7cfee9d4",
    "Gloves":"a151dc72-a632-409c-ae0b-14f1f5a414e0",
    "Handbags":"d4a89c3c-b34b-4f82-bb9b-5aa4b7d2601c",
    "Hats":"772c481c-c206-4f72-880e-8dedfae9638c",
    "Heels":"a3193738-36b8-4b69-9b7d-a8b2acc315af",
    "Hoodies":"ed48dd16-5597-445a-9add-7a1c7f954370",
    "Hosiery":"9e7096bc-9d8e-473b-b5c0-c6da8b510c57",
    "Jackets":"0f6e4894-6090-492a-a85b-fee97f92a613",
    "Jeans":"176f5878-c75b-4a3d-aa0b-726b5ea206d8",
    "Jumpsuits":"124e8c0c-e4c3-45ee-a5a0-7963c3f68e62",
    "Knitwear":"bd6e5b49-856c-455f-b76c-5b39e176ebf9",
    "Loafers":"10d127ea-355f-45fe-a1c4-1ebc143dcb30",
    "Necklaces":"7856327d-3ea0-45a0-99aa-7f24bbdbe2d0",
    "Pants":"043e7edb-ad1f-4fbb-864c-65337b529de4",
    "Pocket Squares":"2eb03a1f-64cd-4fa6-afba-e1acc5de4f98",
    "Polos":"ccb98e23-83eb-46da-9526-51c29a056e18",
    "Rings":"687a9fd7-8c10-4416-9328-789e859fcaff",
    "Sandals":"45c7fd13-ac78-401b-a568-126bfdfd9d44",
    "Scarves":"a16f281a-091d-43c6-90b5-f9be8f5e173a",
    "Shirts":"0a680369-7ae0-4983-920b-4c38160a7ac0",
    "Shorts":"4020d90c-5170-4fb5-a981-717d6caf10b5",
    "Shoulder":"b830bdc6-8a30-42c8-a644-843c8334b4ea",
    "Skirts":"458e612c-2ee2-4601-969e-7e71e5c4c7bf",
    "Sneakers":"513c68b5-500f-4cf8-9264-bb09585e0cb3",
    "Sunglasses":"ceeb4d72-9a9c-4220-a512-387933f85dce",
    "Swimwear":"e58b92c6-4428-495f-add1-c303304204df",
    "T-shirts & Tops":"32395cab-4119-4ea7-a2d3-3f5336e8ee02",
    "Ties":"cb768e82-72c0-4974-addd-f83dc153e5cc",
    "Tote":"d93894cb-2a5e-4667-a653-37fe780204b5",
    "Underwear":"a281a0ca-2ee6-4eaa-a165-4140a272cf25",
    "Vests":"4179dd83-8188-4d76-82f6-0d6f1469c2aa",
    "Wallet":"30dfb52f-7eb5-4ce7-934b-dd4c89bd2a68",
    "Watches":"cd2ac6a4-d024-4225-97f8-ceaf8d3d1547",
}
_SUB = {
    "jackets":"Jackets","jacket":"Jackets","giacche":"Jackets","giubbini":"Jackets",
    "coats":"Jackets","coat":"Jackets","cappotti":"Jackets","trench":"Jackets",
    "down jackets":"Jackets","piumini":"Jackets","bomber":"Jackets","blazer":"Jackets",
    "dresses":"Dresses","dress":"Dresses","abiti":"Dresses",
    "pants":"Pants","trousers":"Pants","pantaloni":"Pants","sweatpants":"Pants",
    "jeans":"Jeans",
    "shorts":"Shorts","pantaloncini":"Shorts","bermuda":"Shorts",
    "skirts":"Skirts","skirt":"Skirts","gonne":"Skirts",
    "knitwear":"Knitwear","maglie":"Knitwear","maglieria":"Knitwear",
    "sweaters":"Knitwear","sweater":"Knitwear","maglioni":"Knitwear",
    "cardigans":"Knitwear","cardigan":"Knitwear","turtlenecks":"Knitwear",
    "sweatshirts":"Knitwear","felpe":"Knitwear","pullover":"Knitwear",
    "shirts":"Shirts","shirt":"Shirts","camicie":"Shirts",
    "polo":"Polos","polo shirts":"Polos","polos":"Polos",
    "t-shirts":"T-shirts & Tops","t-shirt":"T-shirts & Tops",
    "tops":"T-shirts & Tops","top":"T-shirts & Tops",
    "t-shirts & tops":"T-shirts & Tops","tank tops":"T-shirts & Tops",
    "hoodies":"Hoodies","hoodie":"Hoodies",
    "vests":"Vests","vest":"Vests","gilet":"Vests","smanicati":"Vests",
    "jumpsuits":"Jumpsuits","tute":"Jumpsuits","suits":"Jumpsuits",
    "swimwear":"Swimwear","costumi":"Swimwear","bikinis":"Swimwear","bikini":"Swimwear",
    "underwear":"Underwear","intimo":"Underwear",
    "sneakers":"Sneakers","sneaker":"Sneakers",
    "boots":"Boots","stivali":"Boots","ankle boots":"Boots","stivaletti":"Boots",
    "sandals":"Sandals","sandali":"Sandals",
    "loafers":"Loafers","mocassini":"Loafers","espadrilles":"Loafers",
    "heels":"Heels","decollete":"Heels","pumps":"Heels",
    "flats":"Flats","ballerine":"Flats","ballet flats":"Flats",
    "handbags":"Handbags","borse a mano":"Handbags",
    "shoulder bags":"Shoulder","borse a spalla":"Shoulder",
    "crossbody":"Crossbody","borse a tracolla":"Crossbody",
    "clutch":"Clutch","clutches":"Clutch","pochette":"Clutch",
    "tote":"Tote","tote bags":"Tote",
    "backpack":"Backpack","backpacks":"Backpack","zaini":"Backpack","belt bags":"Backpack",
    "wallets":"Wallet","wallet":"Wallet","portafogli":"Wallet","card holders":"Wallet",
    "belts":"Belts","belt":"Belts","cinture":"Belts",
    "scarves":"Scarves","scarf":"Scarves","sciarpe":"Scarves",
    "hats":"Hats","hat":"Hats","cappelli":"Hats","caps":"Hats",
    "gloves":"Gloves","guanti":"Gloves",
    "sunglasses":"Sunglasses","occhiali da sole":"Sunglasses",
    "ties":"Ties","tie":"Ties","cravatte":"Ties","bow ties":"Ties",
    "pocket squares":"Pocket Squares","fazzoletti":"Pocket Squares",
    "hosiery":"Hosiery","socks":"Hosiery","calze":"Hosiery",
    "bracelets":"Bracelets","bracciali":"Bracelets",
    "earrings":"Earrings","orecchini":"Earrings",
    "rings":"Rings","necklaces":"Necklaces","watches":"Watches",
}

# ── GENDER ─────────────────────────────────────────────────────────────────────
_GENDER = {
    "men":"Men","man":"Men","uomo":"Men","uoomo":"Men","m":"Men",
    "women":"Women","woman":"Women","donna":"Women","f":"Women",
    "unisex":"Unisex","u":"Unisex",
    "kids":"Kids","junior":"Kids","bambino":"Kids","bambina":"Kids",
}

# ── BRAND CACHE ────────────────────────────────────────────────────────────────
_brands: dict = {}  # name.lower() → uuid
_unknown_id: str = ""

def load_brands(supabase_url: str, service_key: str) -> dict:
    global _brands, _unknown_id
    import requests as req, logging
    log = logging.getLogger(__name__)
    r = req.get(f"{supabase_url}/rest/v1/brands",
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
                params={"select": "id,name", "limit": "2000"}, timeout=15)
    if r.status_code == 200:
        for row in r.json():
            _brands[row["name"].strip().lower()] = row["id"]
            if row["name"].strip().lower() == "unknown":
                _unknown_id = row["id"]
    else:
        log.error(f"  ⚠️  Failed to load brands: {r.status_code} {r.text[:300]}")
    return _brands

# ── PUBLIC RESOLVERS ───────────────────────────────────────────────────────────

def resolve_brand(name: str, *args) -> str | None:
    """Returns brand UUID or None if not found."""
    if not name:
        return _unknown_id or None
    return _brands.get(name.strip().lower()) or _unknown_id or None

def resolve_category(raw: str) -> str | None:
    if not raw: return None
    key = raw.strip().lower()
    name = _CAT.get(key)
    if not name:
        for k, v in _CAT.items():
            if k in key:
                name = v; break
    return CATEGORY_IDS.get(name) if name else None

def resolve_subcategory(raw: str) -> str | None:
    if not raw: return None
    key = raw.strip().lower()
    name = _SUB.get(key)
    if not name:
        for k, v in _SUB.items():
            if k in key:
                name = v; break
    return SUBCATEGORY_IDS.get(name) if name else None

def resolve_gender(raw: str) -> str | None:
    if not raw: return None
    return _GENDER.get(raw.strip().lower())
