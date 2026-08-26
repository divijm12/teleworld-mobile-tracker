#!/usr/bin/env python3
"""offer_parser_amazon.py: turn fetch_offers_amazon.py's raw bank-offer
lines into structured_offers rows.

Unlike Flipkart, this started as -- and for now stays -- a pure regex
parser, no LLM fallback. Investigation confirmed every real offer line
(70/70 across the first 10 variants fetched) matches exactly one
template:

    "Flat INR {amount} Instant Discount on {bank}[ ({condition})] Credit
    Card[ {N} months and above] EMI Txn. Minimum purchase value INR
    {min_purchase}"

If a future fetch surfaces a line that doesn't match this shape, it's
logged and dropped rather than guessed at -- add a Claude fallback
(mirroring offer_parser.py's hybrid design) if/when that actually
happens, not preemptively for a format we've only seen one shape of.

One real parsing wrinkle, confirmed live: the ICICI line embeds a second
"Credit Card" substring *inside* its own parenthetical exclusion clause
("ICICI Bank (Excluding Amazon Pay ICICI Bank Credit Card) Credit
Card..."), so a naive first-match split on "Credit Card" truncates the
bank name. Fixed by matching the fixed head/tail of the line first, then
stripping the trailing tenure phrase and trailing "Credit Card" from the
middle chunk -- both anchored at the *end* of the remaining text, not the
first occurrence.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from postgrest.exceptions import APIError

from offer_parser import StructuredOffer
from pipeline_logging import setup_logging
from pricing import compute_best_offer
from supabase_client import get_client

log = logging.getLogger("offer_parser_amazon")

# Only "indusind bank ltd" has been seen with inconsistent casing in real
# data; every other bank already arrives correctly capitalized.
BANK_CANONICAL_NAMES = {"indusind bank ltd": "IndusInd Bank"}

OFFER_LINE_RE = re.compile(
    r"^Flat INR (?P<amount>[\d,]+) Instant Discount on (?P<rest>.+) EMI Txn\.\s*"
    r"Minimum purchase value INR (?P<min_purchase>[\d,]+)$"
)
TENURE_SUFFIX_RE = re.compile(r"\s*(?P<tenure>\d+ months and above)$")
CREDIT_CARD_SUFFIX_RE = re.compile(r"\s*Credit Card$")
BANK_PAREN_RE = re.compile(r"^(?P<bank>[^(]+?)\s*(?:\((?P<condition>[^)]+)\))?$")


def regex_parse_offer_line(raw_text: str) -> Optional[StructuredOffer]:
    m = OFFER_LINE_RE.match(raw_text.strip())
    if not m:
        return None  # unrecognized shape -- don't guess, let the caller flag it

    amount = float(m.group("amount").replace(",", ""))
    min_purchase = float(m.group("min_purchase").replace(",", ""))
    rest = m.group("rest")

    tenure = None
    tenure_match = TENURE_SUFFIX_RE.search(rest)
    if tenure_match:
        tenure = tenure_match.group("tenure")
        rest = TENURE_SUFFIX_RE.sub("", rest)

    # Strip only the *trailing* "Credit Card" -- the ICICI line has a
    # second, earlier occurrence inside its own parenthetical, which must
    # stay untouched here.
    if not CREDIT_CARD_SUFFIX_RE.search(rest):
        return None  # doesn't end in "Credit Card" -- not the confirmed shape
    bank_and_condition = CREDIT_CARD_SUFFIX_RE.sub("", rest)

    paren_match = BANK_PAREN_RE.match(bank_and_condition)
    if not paren_match:
        return None
    bank_raw = paren_match.group("bank").strip()
    paren_condition = paren_match.group("condition")

    bank = BANK_CANONICAL_NAMES.get(bank_raw.lower(), bank_raw)

    conditions = [c for c in (tenure, paren_condition) if c]
    conditions_raw = "; ".join(conditions) if conditions else None

    return StructuredOffer(
        raw_text=raw_text,
        bank=bank,
        card_type="credit",  # confirmed on every real line -- "Credit Card" is always present
        discount_type="instant_discount",  # matches Amazon's own "Instant Discount" wording
        discount_amount=amount,
        discount_unit="₹",
        min_purchase_value=min_purchase,
        conditions_raw=conditions_raw,
        confidence="high",
        is_offer=True,
    )


def fetch_unparsed_amazon_snapshots(supabase) -> list[dict]:
    resp = (
        supabase.table("fetch_snapshots")
        .select("id, price, raw_offer_text, variants!inner(model, storage, color, marketplace)")
        .is_("parsed_at", "null")
        .eq("variants.marketplace", "amazon")
        .execute()
    )
    return resp.data


def run_all(supabase) -> dict[str, int]:
    empty_stats = {"snapshots_processed": 0, "offers_total": 0, "unresolved_lines": 0, "failed_snapshots": 0}

    try:
        snapshots = fetch_unparsed_amazon_snapshots(supabase)
    except APIError as e:
        log.error("Failed to query unparsed Amazon snapshots from Supabase: %s", e.message)
        return empty_stats

    if not snapshots:
        log.info("No unparsed Amazon snapshots found -- nothing to do.")
        return empty_stats

    total_offers = 0
    unresolved_lines = 0
    failed_snapshots = 0

    for snapshot in snapshots:
        variant = snapshot["variants"]
        label = f"{variant['model']} ({variant['storage']}, {variant['color']})"
        snapshot_id = snapshot["id"]
        raw_lines = [line.strip() for line in (snapshot["raw_offer_text"] or "").split("\n") if line.strip()]

        structured = []
        for line in raw_lines:
            offer = regex_parse_offer_line(line)
            if offer is None:
                unresolved_lines += 1
                log.warning("%s: unrecognized offer line format -- %r", label, line)
            else:
                structured.append(offer)

        rows = [
            {
                "snapshot_id": snapshot_id,
                "raw_text": o.raw_text,
                "bank": o.bank,
                "card_type": o.card_type,
                "discount_type": o.discount_type,
                "discount_amount": o.discount_amount,
                "discount_unit": o.discount_unit,
                "min_purchase_value": o.min_purchase_value,
                "conditions_raw": o.conditions_raw,
                "confidence": o.confidence,
                "is_offer": o.is_offer,
            }
            for o in structured
        ]

        _, effective_price = compute_best_offer(rows, snapshot["price"])

        try:
            if rows:
                supabase.table("structured_offers").insert(rows).execute()
            supabase.table("fetch_snapshots").update(
                {"parsed_at": datetime.now(timezone.utc).isoformat(), "effective_price": effective_price}
            ).eq("id", snapshot_id).execute()
        except APIError as e:
            log.error("%s: Supabase write failed, snapshot left unparsed for retry: %s", label, e.message)
            failed_snapshots += 1
            continue

        total_offers += len(structured)
        log.info("%s: %d/%d line(s) resolved", label, len(structured), len(raw_lines))

    log.info(
        "%d offers structured, %d unresolved line(s), %d snapshot(s) failed.",
        total_offers, unresolved_lines, failed_snapshots,
    )
    return {
        "snapshots_processed": len(snapshots),
        "offers_total": total_offers,
        "unresolved_lines": unresolved_lines,
        "failed_snapshots": failed_snapshots,
    }


def main() -> None:
    setup_logging()
    supabase = get_client()
    run_all(supabase)


if __name__ == "__main__":
    main()
