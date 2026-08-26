#!/usr/bin/env python3
"""discover_variants_amazon.py: find Amazon India product ASINs for the
target phone models, analogous to discover_variants.py's role for Flipkart.

Investigation findings (load-bearing, don't re-derive):
  - Amazon's search results page (amazon.in/s?k=...) is server-side
    rendered with each product card carrying a `data-asin="..."`
    attribute -- reliable, no JS execution needed to harvest ASINs.
  - Search-card *titles*, unlike Flipkart's, are NOT reliably parseable
    from the search HTML (Amazon's markup nests the title text across
    multiple spans in a way that resists simple regex extraction).
    Instead of fighting that, every candidate ASIN is looked up
    individually via ScraperAPI's structured Amazon product endpoint
    (`/structured/amazon/product/v1`), which returns a clean, reliable
    `name` plus already-parsed `product_information` fields (colour,
    memory_storage_capacity, ram_memory_installed) -- no title-regex
    parsing needed at all, unlike Flipkart's ALT_NAME_VARIANT_RE dance.
  - This structured endpoint is a plain (non-render) ScraperAPI call --
    same ~1-credit cost as a normal fetch, confirmed via account credit
    deltas during investigation, not the much pricier render/browser mode.
  - Only 2 target models for this first Amazon build, both with
    unambiguous names -- so this file deliberately skips the
    cross-search-term attribution complexity discover_variants.py needed
    for its 16 Flipkart models. Add that back only if a future model
    turns out to need it (e.g. real variants hiding in a sibling
    search's results).

Output: discovered_variants_amazon.json, not Supabase directly -- same
review-before-promotion pattern as the Flipkart discovery script.
"""

import argparse
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from pipeline_logging import setup_logging

load_dotenv()

log = logging.getLogger("discover_variants_amazon")

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com"
STRUCTURED_ENDPOINT = "https://api.scraperapi.com/structured/amazon/product/v1"
REQUEST_TIMEOUT = 60

MIN_DELAY = 2.0
MAX_DELAY = 4.0

# Canonical target list -- every token must appear in a candidate's real
# title (case-insensitive) for it to count as a match. Order matters only
# for readability; matching itself is order-independent token containment.
SEARCH_TERMS: list[str] = [
    "OnePlus Nord CE6",
    "OnePlus Nord CE6 Lite",
]

OUTPUT_FILE = Path(__file__).parent / "discovered_variants_amazon.json"

ASIN_RE = re.compile(r'data-asin="([A-Z0-9]{10})"')


def search_asins(term: str) -> list[str]:
    """Harvest candidate ASINs from one Amazon India search results page.
    Returns unique ASINs in the order first seen; empty-slot noise (Amazon
    emits a handful of data-asin="" placeholders) is filtered out by the
    regex itself (it requires exactly 10 alnum chars)."""
    url = f"https://www.amazon.in/s?k={requests.utils.quote(term)}"
    resp = requests.get(
        SCRAPERAPI_ENDPOINT,
        params={"api_key": SCRAPERAPI_KEY, "url": url},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        log.error("Search failed for %r: HTTP %s", term, resp.status_code)
        return []
    asins = ASIN_RE.findall(resp.text)
    seen: list[str] = []
    for a in asins:
        if a not in seen:
            seen.append(a)
    log.info("%r: %d unique candidate ASIN(s) found", term, len(seen))
    return seen


def fetch_structured(asin: str) -> Optional[dict[str, Any]]:
    """Look up one ASIN's real title/specs via ScraperAPI's structured
    Amazon product endpoint. Returns None on failure -- caller treats a
    missing lookup as "skip this candidate", not a guess."""
    resp = requests.get(
        STRUCTURED_ENDPOINT,
        params={
            "api_key": SCRAPERAPI_KEY,
            "asin": asin,
            "country_code": "in",
            "tld": "in",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        log.warning("%s: structured lookup failed (HTTP %s)", asin, resp.status_code)
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        log.warning("%s: structured lookup returned non-JSON", asin)
        return None


def match_confidence(term: str, title: str) -> Optional[str]:
    """"high" if every token of the target term appears in the real title
    (case-insensitive whole-token containment, same rule Flipkart's
    discovery used for its "high" tier) -- else None (not "low"; with
    only 2 unambiguous target models there's no fuzzy middle tier to
    populate yet)."""
    term_tokens = term.lower().split()
    title_tokens = re.findall(r"[a-z0-9]+", title.lower())
    if all(tok in title_tokens for tok in term_tokens):
        return "high"
    return None


def run(terms: list[str]) -> dict[str, Any]:
    all_asins: dict[str, list[str]] = {}
    for term in terms:
        all_asins[term] = search_asins(term)
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Dedupe candidate ASINs across all searches before doing the (paid)
    # structured lookup -- the same real product can surface under more
    # than one search term.
    candidate_asins: list[str] = []
    for asins in all_asins.values():
        for a in asins:
            if a not in candidate_asins:
                candidate_asins.append(a)
    log.info("%d unique candidate ASIN(s) across all searches", len(candidate_asins))

    results = []
    for i, asin in enumerate(candidate_asins):
        data = fetch_structured(asin)
        if data is None:
            continue
        title = data.get("name") or ""
        info = data.get("product_information") or {}

        matched_term = None
        # Check more specific (more-token) terms first -- "OnePlus Nord
        # CE6"'s tokens are a strict subset of "OnePlus Nord CE6 Lite"'s,
        # so a Lite listing would otherwise match the plain term too and
        # get misattributed to it. Confirmed live: 5 real CE6 Lite
        # listings were mislabeled as plain CE6 before this fix.
        for term in sorted(terms, key=lambda t: len(t.split()), reverse=True):
            if match_confidence(term, title):
                matched_term = term
                break
        if matched_term is None:
            log.info("%s: no target term matched -- dropping (%r)", asin, title[:80])
        else:
            results.append(
                {
                    "search_term": matched_term,
                    "asin": asin,
                    "name": title,
                    "color": info.get("colour"),
                    "storage": info.get("memory_storage_capacity"),
                    "ram": info.get("ram_memory_installed"),
                    "confidence": "high",
                }
            )
            log.info("%s: matched %r -- %s / %s / %s", asin, matched_term,
                      info.get("colour"), info.get("memory_storage_capacity"),
                      info.get("ram_memory_installed"))

        if i < len(candidate_asins) - 1:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    by_term: dict[str, int] = {}
    for r in results:
        by_term[r["search_term"]] = by_term.get(r["search_term"], 0) + 1
    log.info("Done: %d matched variant(s) -- %s", len(results), by_term)

    return {"results": results}


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--terms", nargs="+", help="Override SEARCH_TERMS with specific model names")
    args = parser.parse_args()

    if not SCRAPERAPI_KEY:
        raise SystemExit("SCRAPERAPI_KEY must be set (see .env.example)")

    terms = args.terms if args.terms else SEARCH_TERMS
    data = run(terms)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Wrote %d result(s) to %s", len(data["results"]), OUTPUT_FILE)


if __name__ == "__main__":
    main()
