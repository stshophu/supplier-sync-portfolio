#!/usr/bin/env python3
"""
pricing.py — shared pricing logic for the Supplier Tier 1 + Tier 2 jobs.

Pricing formula:
    target_price = (cost + shipping) * (1 + VAT) / (1 - TARGET_MARGIN)
    price = max(round_to_5(target_price), supplier_suggested_price)
    price = min(price, RRP)   -- RRP is only ever a ceiling, never a floor

Key points:
- "Never price below the supplier's own suggested retail price" is a hard
  rule, and it takes priority over rounding -- if the supplier's price is
  what wins, it's used EXACTLY (unrounded), not rounded down below it.
  Rounding only applies to the target-margin price.
- RRP is purely a ceiling (never price above the brand's full retail price).
- MIN_ACCEPTABLE_MARGIN is a safety-net check: if RRP caps the price so low
  that even that can't clear it, flag for manual review rather than
  silently selling at a bad margin.

Tune TARGET_MARGIN / MIN_ACCEPTABLE_MARGIN / SHIPPING for your own catalog
and margin targets -- the values below are placeholders, not production
figures.
"""

VAT_RATE = 0.19
TARGET_MARGIN = 0.30           # placeholder -- set to your own target
MIN_ACCEPTABLE_MARGIN = 0.20   # placeholder -- safety-net floor

# Net shipping absorbed (full cost minus customer-paid portion)
SHIP_NET_DE = 10 - 4
SHIP_NET_EU = 15 - 8
SHIPPING = SHIP_NET_EU         # worst case -> margin guaranteed everywhere


def _round5(p):
    return round(p / 5) * 5


def price_decision(cost, rrp, vendor="", supplier_suggested=None):
    """Return (price, compare_at, status).

    status:
      'ok'            -> active, priced at target margin or the supplier's
                         own suggested price (whichever is higher), capped
                         at RRP
      'low-margin'    -> RRP capped the price below target but still clears
                         the MIN_ACCEPTABLE_MARGIN safety floor; active but
                         tagged
      'review-margin' -> even RRP can't clear the safety floor; draft + tagged

    supplier_suggested: the supplier's own suggested retail price for this
    SKU, in EUR. Optional -- if not provided, behaves as a pure
    cost+margin formula capped at RRP.
    """
    try:
        cost = float(cost)
        rrp = float(rrp) if rrp else 0.0
        supplier_suggested = float(supplier_suggested) if supplier_suggested else 0.0
    except (TypeError, ValueError):
        return None, None, "review-margin"

    if cost <= 0:
        return None, None, "review-margin"

    landed = cost + SHIPPING
    target_price = landed * (1 + VAT_RATE) / (1 - TARGET_MARGIN)
    floor_price = landed * (1 + VAT_RATE) / (1 - MIN_ACCEPTABLE_MARGIN)

    # Never price below the supplier's own suggestion -- and if that's what
    # wins, use it EXACTLY (not rounded down below it). Only our own target
    # price gets rounded to the nearest EUR 5.
    price = max(_round5(target_price), supplier_suggested)

    if rrp <= 0:
        return round(price, 2), None, "ok"

    if price > rrp:
        # RRP caps us. Check the safety floor before accepting a lower price.
        if rrp < floor_price:
            return round(rrp, 2), None, "review-margin"
        capped_status = "ok" if rrp >= target_price else "low-margin"
        return round(rrp, 2), None, capped_status

    compare_at = round(rrp, 2) if rrp > price else None
    return round(price, 2), compare_at, "ok"


if __name__ == "__main__":
    # Synthetic examples -- not real catalog data.
    tests = [
        ("Example brand A jacket", 150, 500, 220),
        ("Example brand B bag", 300, 900, 650),
        ("Example brand C shoes", 120, 300, None),
        ("Example brand D bag", 2000, 2400, None),
        ("No RRP item", 80, 0, None),
    ]
    print(f"{'item':26} {'cost':>8} {'rrp':>8} {'suggested':>10} {'price':>8}  status")
    for name, c, r, sug in tests:
        p, cmp_, st = price_decision(c, r, name.split()[0], sug)
        print(f"{name:26} {c:>8} {r:>8} {str(sug):>10} {str(p):>8}  {st}")
