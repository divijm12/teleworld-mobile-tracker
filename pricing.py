#!/usr/bin/env python3
"""pricing.py: shared discount math, used by both offer_parser.py (to store
each snapshot's effective_price once, at parse time) and dashboard.py (to
render it). Kept in one place so the two never drift apart.
"""

from typing import Optional


def offer_discount_in_rupees(offer: dict, price: Optional[float]) -> Optional[float]:
    """Convert an offer's discount into a plain rupee amount so offers with
    different units (₹ vs %) can be compared on equal footing. Returns None
    -- not a guess -- if the offer isn't usable: not a real offer, no
    parsed amount, a % discount with no known price to apply it against, or
    a price below the offer's own min_purchase_value threshold."""
    if not offer.get("is_offer") or offer.get("discount_amount") is None:
        return None

    min_purchase = offer.get("min_purchase_value")
    if min_purchase and (price is None or price < min_purchase):
        return None

    amount = offer["discount_amount"]
    unit = offer.get("discount_unit")
    if unit == "%":
        if price is None:
            return None
        return price * amount / 100
    return amount  # "₹" or unspecified -- treat as a flat rupee amount


def compute_best_offer(offers: list[dict], price: Optional[float]) -> tuple[Optional[dict], Optional[float]]:
    """Picks the single offer that saves the most money, not an assumption
    that multiple offers stack -- Flipkart's checkout only lets you apply
    one bank/card offer per order, so "best" means best individual offer,
    not a combined total. effective_price always falls back to the base
    price when no offer beats it (rather than None) so it stays comparable
    across snapshots for historical-low tracking -- a snapshot with no
    usable offer still has a real price you could pay today.
    Returns (best_offer_dict_or_None, effective_price_or_None); the price
    is only None if the base price itself is unknown."""
    if price is None:
        return None, None

    best_offer = None
    best_discount = 0.0
    for offer in offers:
        discount = offer_discount_in_rupees(offer, price)
        if discount is not None and discount > best_discount:
            best_discount = discount
            best_offer = offer

    return best_offer, price - best_discount
