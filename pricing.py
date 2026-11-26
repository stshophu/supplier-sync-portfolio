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

    if PRICE_RULE == "floor":
        # No hold, no RRP cap: the min-profit floor always wins. UVP overshoot is
        # only reported (info["above_rrp"]) by the dry-run CSV.
        return float(floor_price_decision(cost, rrp, supplier_suggested)[0]), None, "ok"

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


# ── Floor pricing rule (opt-in; default OFF so live prices are unchanged) ─────
#   PRICE_RULE=floor -> price = max(supplier suggested price, min-profit price)
# Every cost below is an ASSUMPTION until confirmed against real invoices.
import os as _os
def _envf(name, default):
    v = _os.getenv(name, "").strip()
    return float(v) if v else float(default)


PRICE_RULE        = (_os.getenv("PRICE_RULE", "") or "legacy").strip().lower()
MIN_PROFIT_EUR    = _envf("MIN_PROFIT_EUR", "40")
PAY_FEE_PCT       = _envf("PAY_FEE_PCT", "0.025")  # of gross
PAY_FEE_FIXED     = _envf("PAY_FEE_FIXED", "0.30")
SHIP_COST_DE      = _envf("SHIP_COST_DE", "10")     # what DHL costs us
SHIP_PAID_DE      = _envf("SHIP_PAID_DE", "6.95")   # what the customer pays
FREE_SHIP_FROM    = _envf("FREE_SHIP_FROM", "150")
EXTRA_COST_ORDER  = _envf("EXTRA_COST_ORDER", "0")      # packaging etc.
BEYOND_COMMISSION = _envf("BEYOND_COMMISSION", "0.15")
BEYOND_SHORTFALL  = _envf("BEYOND_SHORTFALL", "0")      # DHL cost to HK minus the 50 EUR paid
MAX_OVER_UVP      = _envf("MAX_OVER_UVP", "0.30")   # report only: flag items whose floor price is > UVP x (1+this)
BEYOND_ENABLED    = _os.getenv("BEYOND_ENABLED", "1").strip().lower() in ("1", "true", "yes")


def min_profit_price(cost):
    """Lowest shop price (gross, EUR) that still earns MIN_PROFIT_EUR on this
    item in Germany and, if BEYOND_ENABLED, on Beyond (same price everywhere)."""
    base = cost + MIN_PROFIT_EUR + PAY_FEE_FIXED + EXTRA_COST_ORDER
    k = 1 / (1 + VAT_RATE) - PAY_FEE_PCT
    p = (base + SHIP_COST_DE - SHIP_PAID_DE / (1 + VAT_RATE)) / k
    if p >= FREE_SHIP_FROM:
        p = (base + SHIP_COST_DE) / k
    if BEYOND_ENABLED:
        p = max(p, (cost + MIN_PROFIT_EUR + EXTRA_COST_ORDER + BEYOND_SHORTFALL) / (1 - BEYOND_COMMISSION))
    return p


def floor_price_decision(cost, rrp, supplier_suggested):
    """Returns (price, info): max(supplier price, min-profit price), rounded UP to 5."""
    floor = min_profit_price(cost)
    base = supplier_suggested if supplier_suggested and supplier_suggested > 0 else None
    # The supplier price is used EXACTLY when it already clears the floor;
    # only a raised (floor) price is rounded up to the next 5 EUR.
    price = float(base) if base and base >= floor else int(-(-floor // 5) * 5)
    return price, {"supplier_price": base, "floor_price": round(floor, 2), "new_price": round(price, 2),
                   "decision": "no-supplier-price" if base is None else ("raised" if price > base + 0.005 else "keep"),
                   "raise_pct": round((price / base - 1) * 100, 1) if base else None,
                   "above_rrp": bool(rrp and price > rrp),
                   "over_uvp_ceiling": bool(rrp and MAX_OVER_UVP > 0 and price > rrp * (1 + MAX_OVER_UVP))}


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
