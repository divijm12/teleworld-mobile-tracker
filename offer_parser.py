#!/usr/bin/env python3
"""offer_parser.py: Phase 2 -- turn Phase 1's raw bank/card offer text into
structured data.

Hybrid parser (added after auditing 588 real structured offers): the raw
text Flipkart actually produces turned out to be far more templated than
the original all-LLM design assumed -- every real line matched
"{Bank/App} ({CardType} - DiscountType): amount", always "cashback",
never a percentage, never a min-purchase threshold, 0% low-confidence, 0%
non-offers. regex_parse_offer_line() is a fast-path for that exact shape,
using lookup tables for known banks/UPI apps rather than guessing. Claude
is now only called for lines the regex path can't confidently resolve
(unrecognized entity name, unexpected wording, a genuinely novel format)
-- this is a deliberate safety net, not a redundant step: if Flipkart's
copy changes or a new phone's offers use a format not seen yet, those
lines still get handled correctly instead of silently mis-parsed.

Reads snapshots from Supabase where parsed_at IS NULL (written by
fetch_offers.py), resolves each snapshot's raw offer lines via the hybrid
parser, writes rows to structured_offers, and marks the snapshot parsed.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional

import anthropic
from postgrest.exceptions import APIError
from pydantic import BaseModel

from pipeline_logging import setup_logging
from pricing import compute_best_offer
from supabase_client import get_client

log = logging.getLogger("offer_parser")

MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """You extract structured data from raw Flipkart bank/card offer
text. Each line describes one offer as shown on the product page. For each
line, determine:

- bank: the issuing bank (e.g. "ICICI Bank", "HDFC Bank", "SBI"). Use null if
  the offer isn't tied to a specific bank (e.g. a UPI app like Paytm or a
  generic exchange offer). If the line names a Flipkart co-branded card (e.g.
  "Flipkart Axis", "Flipkart SBI"), keep that distinction in the bank name
  (e.g. "Flipkart Axis Bank", "Flipkart SBI") rather than shortening it to
  just the bank name -- a co-branded card offer and a plain bank-card offer
  are different products with different terms, don't collapse them.
- card_type: "credit", "debit", "both", or "EMI". Use null if not card-specific.
- discount_type: "flat", "percentage", "cashback", "instant_discount",
  "no_cost_emi", or "other".
- discount_amount: the numeric discount value (e.g. 1400 for "₹1,400 off",
  5 for "5% off"). Use null if no clear amount is stated.
- discount_unit: "₹" or "%", matching discount_amount. Null if not applicable.
- min_purchase_value: the minimum purchase amount required to qualify, if
  stated. Null if none is mentioned.
- conditions_raw: any other conditions mentioned in the line, as free text
  (don't try to fully structure this -- just capture it). Null if there's
  nothing beyond the core discount.
- confidence: "low" if the line is ambiguous, doesn't look like a real
  discount/cashback offer, or you're not confident in the parse (for example,
  a leftover EMI-plan listing rather than an actual offer). Otherwise "high".
- is_offer: false if the line doesn't represent an actual discount/cashback
  offer at all (e.g. stray EMI-schedule text that isn't a real offer). true
  otherwise.

Return one structured entry per input line, in the same order, echoing the
exact input line back as raw_text so it can be matched to the source."""


class StructuredOffer(BaseModel):
    raw_text: str
    bank: Optional[str] = None
    card_type: Optional[Literal["credit", "debit", "both", "EMI"]] = None
    discount_type: Optional[
        Literal["flat", "percentage", "cashback", "instant_discount", "no_cost_emi", "other"]
    ] = None
    discount_amount: Optional[float] = None
    discount_unit: Optional[Literal["₹", "%"]] = None
    min_purchase_value: Optional[float] = None
    conditions_raw: Optional[str] = None
    confidence: Literal["high", "low"]
    is_offer: bool


class StructuredOfferBatch(BaseModel):
    offers: list[StructuredOffer]


# --- Regex fast-path (see module docstring) -------------------------------

# Known entities -- bank keyword present -> bank field populated with the
# canonical name (BANK_CANONICAL_NAMES below). Co-branded "Flipkart X" cards
# are kept distinguishable from a plain "X" bank offer (e.g. "Flipkart Axis
# Bank" vs "Axis Bank") -- these are genuinely different card products with
# different terms, not the same offer with cosmetic wording. An earlier
# version of this collapsed both to "Axis Bank"/"SBI" (matching what Claude
# had also been doing originally), which lost that distinction -- explicit
# user request to stop doing that, for every bank, not just Axis/SBI.
# UPI/wallet app -> bank left null (matches the LLM prompt's original rule:
# "Use null if the offer isn't tied to a specific bank e.g. a UPI app like
# Paytm"). Matched as case-insensitive substrings against the raw title text.
FLIPKART_WORD_RE = re.compile(r"\bflipkart\b", re.IGNORECASE)
KNOWN_BANK_KEYWORDS = [
    "Axis", "SBI", "HDFC", "ICICI", "Kotak", "IDFC", "RBL", "Yes Bank",
    "IndusInd", "Citibank", "HSBC", "Standard Chartered", "American Express",
    "AmEx", "Bank of Baroda", "PNB", "Federal Bank", "AU Small Finance Bank",
    "IDBI", "Union Bank", "Canara Bank", "Indian Bank", "Bank of India",
]
# Only "axis" and "sbi" are confirmed against real Claude output (708/708
# real offers checked); the rest are reasonable canonical forms but
# unverified since those banks haven't appeared in real data yet.
BANK_CANONICAL_NAMES = {
    "axis": "Axis Bank",
    "sbi": "SBI",
    "hdfc": "HDFC Bank",
    "icici": "ICICI Bank",
    "kotak": "Kotak Mahindra Bank",
    "idfc": "IDFC FIRST Bank",
    "rbl": "RBL Bank",
    "yes bank": "Yes Bank",
    "indusind": "IndusInd Bank",
    "citibank": "Citibank",
    "hsbc": "HSBC",
    "standard chartered": "Standard Chartered",
    "american express": "American Express",
    "amex": "American Express",
    "bank of baroda": "Bank of Baroda",
    "pnb": "Punjab National Bank",
    "federal bank": "Federal Bank",
    "au small finance bank": "AU Small Finance Bank",
    "idbi": "IDBI Bank",
    "union bank": "Union Bank of India",
    "canara bank": "Canara Bank",
    "indian bank": "Indian Bank",
    "bank of india": "Bank of India",
}
KNOWN_UPI_APPS = ["BHIM", "Mobikwik", "Paytm", "PhonePe", "Google Pay", "GPay", "Amazon Pay", "Cred"]

CARD_TYPE_MAP = {"credit card": "credit", "debit card": "debit"}
DISCOUNT_TYPE_MAP = {
    "cashback": "cashback",
    "instant discount": "instant_discount",
    "no cost emi": "no_cost_emi",
    "flat": "flat",
}

# Matches extract_bank_offers_raw()'s line shape in fetch_offers.py:
# "{title} ({subtitle}): {amount}" -- subtitle is required for the fast
# path (every real example seen has one); lines without it fall through to
# Claude rather than guessing at an unconfirmed shape.
RAW_LINE_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<subtitle>[^()]+)\):\s*(?P<amount>.+)$")
RUPEE_RE = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
MIN_PURCHASE_RE = re.compile(
    r"min(?:imum)?\.?\s*(?:purchase\s*(?:value)?)?\s*(?:of\s*)?₹\s*([\d,]+)", re.IGNORECASE
)


def regex_parse_offer_line(raw_text: str) -> Optional[StructuredOffer]:
    """Fast-path parse for the observed raw-line template. Returns None --
    not a guess -- for anything that doesn't cleanly resolve against a
    known bank/app name and a known card/discount-type wording, so the
    caller falls back to Claude instead of silently mis-parsing a format
    this hasn't been validated against."""
    m = RAW_LINE_RE.match(raw_text.strip())
    if not m:
        return None

    title = m.group("title").strip()
    subtitle = m.group("subtitle").strip()
    amount_text = m.group("amount").strip()

    parts = [p.strip() for p in subtitle.split("•")]
    if len(parts) != 2:
        return None
    card_part, discount_part = parts

    card_type = None
    if card_part.lower() != "upi":
        card_type = CARD_TYPE_MAP.get(card_part.lower())
        if card_type is None:
            return None  # unrecognized card-type wording -- let Claude handle it

    discount_type = DISCOUNT_TYPE_MAP.get(discount_part.lower())
    if discount_type is None:
        return None  # unrecognized discount-type wording

    title_lower = title.lower()
    is_flipkart_cobranded = bool(FLIPKART_WORD_RE.search(title))
    # Match the bank keyword against the title with "Flipkart" stripped out,
    # so e.g. "Flipkart Axis" still matches the "Axis" keyword cleanly --
    # the co-branding is tracked separately via is_flipkart_cobranded, not
    # folded into the keyword match itself.
    bank_search_text = FLIPKART_WORD_RE.sub("", title_lower)
    matched_bank_keyword = next((kw for kw in KNOWN_BANK_KEYWORDS if kw.lower() in bank_search_text), None)
    is_upi_app = any(kw.lower() in title_lower for kw in KNOWN_UPI_APPS)
    if matched_bank_keyword is None and not is_upi_app:
        return None  # unknown entity -- don't guess whether it's a bank

    bank = None
    if matched_bank_keyword:
        canonical = BANK_CANONICAL_NAMES.get(matched_bank_keyword.lower(), matched_bank_keyword)
        bank = f"Flipkart {canonical}" if is_flipkart_cobranded else canonical

    rupee_match = RUPEE_RE.search(amount_text)
    percent_match = PERCENT_RE.search(amount_text)
    if rupee_match:
        discount_amount = float(rupee_match.group(1).replace(",", ""))
        discount_unit = "₹"
    elif percent_match:
        discount_amount = float(percent_match.group(1))
        discount_unit = "%"
    else:
        return None  # no numeric amount found -- let Claude handle it

    min_purchase_match = MIN_PURCHASE_RE.search(raw_text)
    min_purchase_value = float(min_purchase_match.group(1).replace(",", "")) if min_purchase_match else None

    return StructuredOffer(
        raw_text=raw_text,
        bank=bank,
        card_type=card_type,
        discount_type=discount_type,
        discount_amount=discount_amount,
        discount_unit=discount_unit,
        min_purchase_value=min_purchase_value,
        conditions_raw=subtitle,
        confidence="high",
        is_offer=True,
    )


def parse_variant_offers(client: anthropic.Anthropic, raw_lines: list[str]) -> list[StructuredOffer]:
    """Send all raw offer lines for one snapshot to Claude in a single call."""
    numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(raw_lines))
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Extract structured data from these {len(raw_lines)} offer lines:\n\n{numbered}",
            }
        ],
        output_format=StructuredOfferBatch,
    )
    return response.parsed_output.offers


def parse_variant_offers_hybrid(
    client: anthropic.Anthropic, raw_lines: list[str]
) -> tuple[list[StructuredOffer], int, int]:
    """Try the regex fast-path on every line first; only the lines it can't
    confidently resolve go to Claude, still batched into a single call (or
    skipped entirely if the regex path handled everything). Returns
    (offers_in_original_order, regex_resolved_count, claude_resolved_count)
    so the caller can report the split."""
    results: list[Optional[StructuredOffer]] = []
    unresolved_indices: list[int] = []
    unresolved_lines: list[str] = []

    for i, line in enumerate(raw_lines):
        parsed = regex_parse_offer_line(line)
        results.append(parsed)
        if parsed is None:
            unresolved_indices.append(i)
            unresolved_lines.append(line)

    if unresolved_lines:
        claude_results = parse_variant_offers(client, unresolved_lines)
        if len(claude_results) != len(unresolved_lines):
            log.warning(
                "Sent %d unresolved line(s) to Claude but got %d back -- "
                "some lines may be unmatched",
                len(unresolved_lines), len(claude_results),
            )
        for idx, offer in zip(unresolved_indices, claude_results):
            results[idx] = offer

    regex_resolved = len(raw_lines) - len(unresolved_lines)
    return [o for o in results if o is not None], regex_resolved, len(unresolved_lines)


def fetch_unparsed_snapshots(supabase) -> list[dict]:
    resp = (
        supabase.table("fetch_snapshots")
        .select("id, price, raw_offer_text, variants(model, storage, color)")
        .is_("parsed_at", "null")
        .execute()
    )
    return resp.data


def run_all(supabase, claude: anthropic.Anthropic) -> dict[str, int]:
    """Parse every unparsed snapshot. Returns summary counts so a caller
    (e.g. run_pipeline.py) can log/report them without re-parsing log output."""
    empty_stats = {
        "snapshots_processed": 0,
        "offers_total": 0,
        "low_confidence": 0,
        "non_offers": 0,
        "failed_snapshots": 0,
        "regex_resolved": 0,
        "claude_resolved": 0,
    }

    try:
        snapshots = fetch_unparsed_snapshots(supabase)
    except APIError as e:
        log.error("Failed to query unparsed snapshots from Supabase: %s", e.message)
        return empty_stats

    if not snapshots:
        log.info("No unparsed snapshots found -- nothing to do.")
        return empty_stats

    total_offers = 0
    low_confidence = 0
    non_offers = 0
    failed_snapshots = 0
    regex_resolved_total = 0
    claude_resolved_total = 0

    for snapshot in snapshots:
        variant = snapshot["variants"]
        label = f"{variant['model']} ({variant['storage']}, {variant['color']})"
        snapshot_id = snapshot["id"]
        raw_lines = [line.strip() for line in snapshot["raw_offer_text"].split("\n") if line.strip()]

        try:
            structured, regex_resolved, claude_resolved = parse_variant_offers_hybrid(claude, raw_lines)
        except anthropic.APIStatusError as e:
            log.error("%s: API error parsing offers: %s", label, e)
            failed_snapshots += 1
            continue
        except anthropic.APIConnectionError as e:
            log.error("%s: connection error parsing offers: %s", label, e)
            failed_snapshots += 1
            continue

        regex_resolved_total += regex_resolved
        claude_resolved_total += claude_resolved
        log.info(
            "%s: %d line(s) -- %d resolved by regex, %d sent to Claude",
            label, len(raw_lines), regex_resolved, claude_resolved,
        )

        if len(structured) != len(raw_lines):
            log.warning(
                "%s: sent %d raw lines but got %d structured entries back -- some lines may be unmatched",
                label, len(raw_lines), len(structured),
            )

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

        # Computed once, here, using this snapshot's own price + own offers
        # -- not recomputed later from history, so it stays an accurate
        # record of what the deal actually was at this point in time even
        # as offers/prices change on later snapshots.
        _, effective_price = compute_best_offer(rows, snapshot["price"])

        try:
            if rows:
                supabase.table("structured_offers").insert(rows).execute()
            supabase.table("fetch_snapshots").update(
                {
                    "parsed_at": datetime.now(timezone.utc).isoformat(),
                    "effective_price": effective_price,
                }
            ).eq("id", snapshot_id).execute()
        except APIError as e:
            log.error("%s: Supabase write failed, snapshot left unparsed for retry: %s", label, e.message)
            failed_snapshots += 1
            continue

        for offer in structured:
            total_offers += 1
            if not offer.is_offer:
                non_offers += 1
                log.info("%s: filtered as non-offer -- %r", label, offer.raw_text)
            elif offer.confidence == "low":
                low_confidence += 1
                log.info("%s: low-confidence parse -- %r", label, offer.raw_text)

    log.info(
        "%d offers structured (%d via regex, %d via Claude), %d flagged low-confidence, "
        "%d filtered as non-offer, %d snapshot(s) failed.",
        total_offers, regex_resolved_total, claude_resolved_total,
        low_confidence, non_offers, failed_snapshots,
    )
    return {
        "snapshots_processed": len(snapshots),
        "offers_total": total_offers,
        "low_confidence": low_confidence,
        "non_offers": non_offers,
        "failed_snapshots": failed_snapshots,
        "regex_resolved": regex_resolved_total,
        "claude_resolved": claude_resolved_total,
    }


def main() -> None:
    setup_logging()
    supabase = get_client()
    claude = anthropic.Anthropic()
    run_all(supabase, claude)


if __name__ == "__main__":
    main()
