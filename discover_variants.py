#!/usr/bin/env python3
"""discover_variants.py: auto-discover Flipkart product URLs/pids for target
phone models via Flipkart's search results page, instead of hand-curating
them the way fetch_offers.py's TARGETS list currently is.

Investigation findings (load-bearing, don't re-derive):
  - Flipkart's search results page is server-side rendered exactly like a
    product page -- the same <script id="jsonLD"> block is present, but as
    a schema.org ItemList instead of a single Product. Each ListItem gives
    {name, url, position}; no product-page fetch is needed at discovery
    time. `pid` is embedded in the url's query string, and (for most
    results) so is color/storage, inside the `name` string's trailing
    "(Color, NNN GB)" suffix -- but not every result follows that exact
    shape (see parse_variant_label), so that field is explicitly flagged
    null + noted, never guessed, when it can't be cleanly parsed.
  - Flipkart's search broadens past exact matches: it returns sibling
    models, similarly-named models, and even unrelated accessories for
    *other* phones (tempered glass, screen guards, etc. -- confirmed live
    against "POCO C81").
  - Critically, a raw result is scored against *every* canonical target in
    SEARCH_TERMS, not just the search term that happened to fetch it (see
    assign_best_match). Confirmed live: real "Vivo T4 Lite" listings
    (Titanium Gold across 3 storage tiers, plus Prism Blue) only ever
    turned up inside a "Vivo T5x 5G" search's raw results -- Vivo T4 Lite's
    own dedicated search barely returns anything on one page. Scoring only
    against the origin search term would silently discard these (T4 Lite
    doesn't even fuzzy-match "T5x" at all, so they'd never even reach the
    "low" tier, let alone get reconsidered) -- a first version of this
    script did exactly that, and a user manually checking Flipkart caught
    the resulting undercount. Checking every result against every target
    fixes this class of bug generally, not just for this one case.
  - Match confidence has three tiers (see match_confidence):
      "high" -- every token of the search term appears as a whole token in
        the result's name (e.g. "POCO C81" matches "POCO C81 (Elite Black,
        64 GB)").
      "low"  -- not a clean token match, but the search term (all
        whitespace/punctuation stripped) still appears as a contiguous
        substring of the result name stripped the same way. This is what
        catches real matches spelled differently than our target list
        (target list says "Vivo T4lite", Flipkart's actual listing says
        "Vivo T4 Lite") -- but it *also* catches genuine near-miss
        collisions (target "POCO C81" is a stripped-substring of the
        unrelated "POCO C81x ..."). Both look identical under this test,
        which is exactly why these are written out flagged rather than
        trusted automatically.
      no match against *any* canonical target -- dropped entirely, not
        written out (unrelated accessories, different phone lines with no
        meaningful overlap anywhere). Including every such result would
        just be noise in the review list.
  - Confirmed live: searching "Nothing 2a" currently returns zero matching
    results at all (Flipkart's live listings are Phone 4a/4a Pro/4b/3) --
    the model may no longer be sold, or the search term doesn't match
    Flipkart's actual naming. Surfaced as a warning, not silently dropped.
  - RAM matters for bank-offer tracking (offers/min-purchase thresholds can
    differ by RAM even at the same color+storage -- confirmed live: "Vivo
    T4 Lite, Titanium Gold, 128 GB" is two distinct pids, 4GB and 6GB RAM,
    at different prices) but is only present in *search-card* text for one
    model (Vivo T4 Lite's unusual card format -- see parse_variant_label).
    Investigated the full product page for several other models (POCO C81,
    Vivo T5x 5G, Motorola g37): none expose RAM in the search-card name,
    but *every* product page's own SEO title does, in a consistent format:
    "{Model} ({Storage} Storage, {RAM} RAM) Online at Best Price On
    Flipkart.com" (state['multiWidgetState']['pageDataResponse']['seoData']
    ['seo']['title'] in __INITIAL_STATE__). Getting RAM for every model
    (not just Vivo T4 Lite) therefore needs a fetch_offers.py change -- it
    already visits each product's full page -- not a discover_variants.py
    change; discovery only has RAM when the search card happens to show it.

Writes to discovered_variants.json, not Supabase -- these are unverified
until reviewed, and the existing `variants` table has no way to distinguish
trusted (manually-curated) rows from these yet. Feed reviewed entries into
fetch_offers.py's TARGETS (or a future Supabase pending-review path) by
hand once you've checked them.
"""

import argparse
import json
import logging
import random
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from pipeline_logging import setup_logging

log = logging.getLogger("discover_variants")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
REQUEST_TIMEOUT = 15  # seconds
MIN_DELAY = 2.0  # seconds between search requests
MAX_DELAY = 5.0

JSONLD_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*id="jsonLD"[^>]*>(.*?)</script>',
    re.DOTALL,
)
# Matches a trailing "(Color, 128 GB)" suffix -- same shape fetch_offers.py
# relies on for product-page names. Not every search-result name follows
# this; see parse_variant_label.
NAME_VARIANT_RE = re.compile(r"\(([^,()]+),\s*(\d+\s*GB)\)\s*$", re.IGNORECASE)
# Fallback for a second real format confirmed live on every Vivo T4 Lite
# listing: "(Color YYYY) (RAM GB Storage GB)" -- e.g. "(Titanium Gold 2026)
# (6GB 128GB)". Bank offers vary by variant (min-purchase thresholds shift
# with storage tier -- AND, confirmed live, with RAM: "Titanium Gold 128GB"
# exists as two distinct pids, 4GB RAM and 6GB RAM, at different prices), so
# a card using this format still needs a clean (color, ram, storage) label,
# not just a null -- this is not optional polish.
ALT_NAME_VARIANT_RE = re.compile(
    r"\(([^()]+?)(?:\s+\d{4})?\)\s*\(\s*(\d+)\s*GB\s+(\d+)\s*GB\)\s*$", re.IGNORECASE
)

OUTPUT_FILE = "discovered_variants.json"

# The target list for this project. AI-branded models are deliberately out
# of scope -- do not add them here. "Nothing 2a" was dropped entirely after
# discovery confirmed 0 matching Flipkart listings (closest real listings
# are "Nothing Phone (4a)/(4b)/(3)" -- not the same model, not a scraping
# gap) -- do not re-add without a corrected search term. "Vivo T4lite",
# "realme P4lite", and "Nothing CMF1" were renamed to their real Flipkart
# spellings (spaced "T4 Lite"/"P4 Lite", and "CMF by Nothing Phone 1") after
# their original concatenated/guessed forms only produced low-confidence or
# zero matches.
#
# "POCO C81x" was deliberately added as its own target after showing up
# repeatedly as a *low-confidence* near-miss under "POCO C81" -- per explicit
# user direction, a sibling model surfacing this way is worth tracking in
# its own right (broadens coverage), not just noise to discard. Any other
# low-confidence "near-miss sibling" spotted in future sweeps should get the
# same treatment: add it as its own SEARCH_TERMS entry and run a dedicated
# sweep for it, don't just leave it sitting flagged under the original term.
SEARCH_TERMS: list[str] = [
    "Vivo T5x 5G",
    "Vivo T4 Lite",
    "Vivo T4r",
    "POCO C85x",
    "POCO C81",
    "POCO C81x",
    "Oppo K14",
    "Oppo K14x",
    "Motorola g37",
    "Motorola g67",
    "Motorola edge 60 fusion",
    "HMD vibe2",
    "realme P4r",
    "realme P4 Lite",
    "CMF by Nothing Phone 1",
]


# Accessory listings routinely put the phone's exact model name in their own
# title ("Tempered Glass for POCO C81 4G") -- confirmed live, these pass the
# token-containment test in match_confidence() just as cleanly as a real
# phone listing does. Token matching alone can't tell them apart; this is a
# separate, cheap keyword gate applied before match_confidence() is trusted.
ACCESSORY_KEYWORDS = [
    "tempered glass", "screen guard", "screen protector", "camera lens",
    "lens protector", "back cover", "flip cover", "phone case", "glass guard",
    "pouch", "skin", "screen guard", "privacy screen", "back panel",
    "housing", "full panel", "replacement panel", "screen replacement",
    "display replacement", "spare part",
]


def is_accessory(name: str) -> bool:
    lowered = name.lower()
    return any(kw in lowered for kw in ACCESSORY_KEYWORDS)


def normalize_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def stripped(text: str) -> str:
    return "".join(normalize_tokens(text))


def match_confidence(search_term: str, candidate_name: str) -> Optional[str]:
    """Score a search result against the term that produced it. See module
    docstring for the reasoning behind the three tiers."""
    target_tokens = set(normalize_tokens(search_term))
    candidate_tokens = set(normalize_tokens(candidate_name))
    if target_tokens <= candidate_tokens:
        return "high"
    if stripped(search_term) in stripped(candidate_name):
        return "low"
    return None


def parse_variant_label(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Try to pull (color, ram, storage) out of the card's name text, trying
    both known real formats before giving up. RAM is only ever present in
    the second format (confirmed live: no other card text carries it at
    all, at the search-results stage -- see module docstring) so it's None
    whenever the primary format matches. Returns (None, None, None) -- not
    a guess -- when neither shape matches; callers must flag that
    explicitly rather than treat it as an empty-but-fine result."""
    m = NAME_VARIANT_RE.search(name)
    if m:
        return m.group(1).strip(), None, m.group(2).strip()
    m = ALT_NAME_VARIANT_RE.search(name)
    if m:
        return m.group(1).strip(), f"{m.group(2).strip()} GB", f"{m.group(3).strip()} GB"
    return None, None, None


def extract_pid(url: str) -> Optional[str]:
    query = parse_qs(urlparse(url).query)
    values = query.get("pid")
    return values[0] if values else None


def parse_search_jsonld(html: str) -> Optional[list[dict[str, Any]]]:
    match = JSONLD_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("@type") == "ItemList":
            return entry.get("itemListElement", [])
    return None


def search_variants(term: str) -> list[dict[str, Any]]:
    """Fetch one search-results page and return every result item as an
    unattributed raw candidate -- NOT yet scored against any particular
    target. Attribution happens globally afterward via assign_best_match(),
    checked against every canonical target, not just this search term (see
    module docstring for why that matters). A structural failure like 'no
    jsonLD at all' is still reported clearly rather than silently producing
    an empty result."""
    url = f"https://www.flipkart.com/search?q={quote_plus(term)}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        log.error("%s: search request failed: %s", term, e)
        return []

    if response.status_code != 200:
        log.error("%s: unexpected HTTP status %d", term, response.status_code)
        return []

    items = parse_search_jsonld(response.text)
    if items is None:
        log.error(
            "%s: no jsonLD ItemList found on search results page -- Flipkart's "
            "search page structure may have changed; investigate before trusting "
            "any output from this run",
            term,
        )
        return []
    if not items:
        log.warning("%s: jsonLD parsed but contained no result items", term)
        return []

    results = []
    for item in items:
        name = item.get("name")
        item_url = item.get("url")
        if not name or not item_url:
            continue
        color, ram, storage = parse_variant_label(name)
        results.append(
            {
                "origin_search_term": term,
                "search_rank": item.get("position"),
                "name": name,
                "url": item_url,
                "pid": extract_pid(item_url),
                "is_accessory": is_accessory(name),
                "color": color,
                "ram": ram,
                "storage": storage,
                "label_parsed": color is not None and storage is not None,
            }
        )
    return results


def flag_duplicate_variants(entries: list[dict[str, Any]]) -> None:
    """Mutates entries in place: for entries that parsed a (color, storage)
    label, note when more than one distinct pid maps to the same
    (search_term, color, ram, storage) -- Flipkart lists the same variant
    under multiple seller listings, and picking the "right" one isn't
    something this script should guess at.

    RAM is included in the grouping key even though it's None for almost
    every entry (only Vivo T4 Lite's card format exposes it -- see
    parse_variant_label). This matters: confirmed live, "Titanium Gold,
    128 GB" is genuinely two different products at two different prices --
    4GB RAM and 6GB RAM, different pids -- not two sellers of the same
    item. Grouping on (color, storage) alone would have wrongly flagged
    them as duplicate listings of one variant; a first version of this
    function did exactly that."""
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        if not e["label_parsed"]:
            continue
        key = (
            e["search_term"],
            e["color"].lower(),
            (e["ram"] or "").lower(),
            e["storage"].lower(),
        )
        groups.setdefault(key, []).append(e)

    for group in groups.values():
        pids = {e["pid"] for e in group}
        if len(pids) > 1:
            for e in group:
                e["duplicate_listing_pids"] = sorted(pids - {e["pid"]})


def assign_best_match(entries: list[dict[str, Any]], canonical_terms: list[str]) -> None:
    """Attribute each raw candidate to whichever canonical target it best
    matches -- checked against *every* tracked model, not just whatever
    search happened to surface it. This is the fix for the "Vivo T4 Lite"
    undercount: its real listings only ever appeared inside a "Vivo T5x 5G"
    search's raw results, and previously would have been silently discarded
    right there, since T4 Lite doesn't even reach the "low" tier against
    "Vivo T5x 5G" (no fuzzy overlap at all) -- so a fix that only looked at
    already-"low" entries (the original approach, used for the Oppo K14x
    case) wouldn't have caught this. Checking every entry against every
    target, unconditionally, catches both failure modes with one pass.

    Mutates each entry in place, setting "search_term" and "confidence".
    Accessories are always dropped (confidence=None) regardless of any
    token match. Entries matching no canonical term at all also get
    confidence=None -- true noise, dropped by the caller."""
    for e in entries:
        name = e["name"]
        if e.get("is_accessory", False):
            e["search_term"] = e.get("origin_search_term", e.get("search_term"))
            e["confidence"] = None
            continue
        best_term, best_conf = None, None
        for term in canonical_terms:
            c = match_confidence(term, name)
            if c == "high":
                best_term, best_conf = term, "high"
                break
            if c == "low" and best_conf is None:
                best_term, best_conf = term, "low"
        e["search_term"] = best_term or e.get("origin_search_term", e.get("search_term"))
        e["confidence"] = best_conf


def dedupe_by_pid(all_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reclassification can leave the same pid represented twice (once from
    a dedicated search, once re-homed from a sibling search) -- keep one."""
    seen: set[str] = set()
    deduped = []
    for r in all_results:
        if r["pid"] in seen:
            continue
        seen.add(r["pid"])
        deduped.append(r)
    return deduped


def load_existing_output(path: str) -> dict[str, Any]:
    """Load a prior discover_variants.py run's output, if present, so a
    partial/incremental run (e.g. `--terms` on a subset) doesn't clobber
    results for terms that aren't part of this run."""
    if not os.path.exists(path):
        return {"search_terms": [], "results": []}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read existing %s (%s) -- starting fresh instead of merging", path, e)
        return {"search_terms": [], "results": []}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover Flipkart variant URLs/pids for target phone models."
    )
    parser.add_argument(
        "--terms",
        nargs="+",
        help="Override SEARCH_TERMS with specific model names to run "
        "(space-separated, quote each). Default: the full target list.",
    )
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()
    terms = args.terms if args.terms else SEARCH_TERMS

    existing = load_existing_output(OUTPUT_FILE)
    # Only preserve results for terms still in the canonical SEARCH_TERMS
    # list (just not part of *this* run) -- results under a retired or
    # renamed term string (e.g. old "Vivo T4lite" after it was renamed to
    # "Vivo T4 Lite") are dropped here rather than preserved forever, since
    # nothing will ever "re-run" that exact old string again.
    stale = [
        r for r in existing["results"]
        if r["search_term"] not in SEARCH_TERMS
    ]
    if stale:
        log.info(
            "Dropping %d result(s) from retired/renamed search term(s) no longer in "
            "SEARCH_TERMS: %s",
            len(stale),
            ", ".join(sorted({r["search_term"] for r in stale})),
        )
    preserved = [
        r for r in existing["results"]
        if r["search_term"] in SEARCH_TERMS and r["search_term"] not in terms
    ]
    if preserved:
        log.info(
            "Preserving %d previously-discovered result(s) for term(s) not in this run: %s",
            len(preserved),
            ", ".join(sorted({r["search_term"] for r in preserved})),
        )
        for r in preserved:
            r.pop("duplicate_listing_pids", None)  # recomputed fresh below

    log.info("Running discovery for %d search term(s): %s", len(terms), ", ".join(terms))

    raw_pool: list[dict[str, Any]] = []
    for i, term in enumerate(terms):
        raw = search_variants(term)
        raw_pool.extend(raw)
        log.info("%s: %d raw result(s) fetched", term, len(raw))
        if not raw:
            log.warning(
                "%s: zero raw results fetched -- check for request/parsing errors above "
                "before assuming this term is simply unavailable",
                term,
            )
        if i < len(terms) - 1:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Attribute every fetched-this-run candidate AND every preserved entry
    # against the current canonical SEARCH_TERMS -- re-deriving preserved
    # entries too means a newly-added target (e.g. "POCO C81x") retroactively
    # reclaims anything already sitting in the file that matches it, and
    # anything that no longer matches any current target cleanly drops out.
    combined_pool = preserved + raw_pool
    assign_best_match(combined_pool, SEARCH_TERMS)

    accessories_dropped = sum(1 for r in raw_pool if r.get("is_accessory"))
    unmatched_dropped = sum(
        1 for r in raw_pool if not r.get("is_accessory") and r["confidence"] is None
    )
    log.info(
        "This run's fetches: %d raw result(s) total, %d dropped as accessories, "
        "%d dropped as not matching any tracked model",
        len(raw_pool), accessories_dropped, unmatched_dropped,
    )

    all_results = [r for r in combined_pool if r["confidence"] is not None]
    for r in all_results:
        r.pop("is_accessory", None)
        r.pop("origin_search_term", None)

    before = len(all_results)
    all_results = dedupe_by_pid(all_results)
    if len(all_results) < before:
        log.info(
            "Dropped %d exact-duplicate pid entr%s (same real listing attributed twice).",
            before - len(all_results), "y" if before - len(all_results) == 1 else "ies",
        )

    flag_duplicate_variants(all_results)

    for term in SEARCH_TERMS:
        term_results = [r for r in all_results if r["search_term"] == term]
        high = sum(1 for r in term_results if r["confidence"] == "high")
        low = sum(1 for r in term_results if r["confidence"] == "low")
        log.info(
            "%s: %d final result(s) -- %d high-confidence, %d low-confidence (flagged)",
            term, len(term_results), high, low,
        )
        if not term_results and term in terms:
            log.warning(
                "%s: zero matching results -- model may no longer be listed, or the search "
                "term doesn't match Flipkart's naming. Verify manually before assuming it's "
                "just a scraping gap.",
                term,
            )

    unparsed = [r for r in all_results if not r["label_parsed"]]
    if unparsed:
        log.warning(
            "%d matched result(s) had a name Flipkart's search card didn't cleanly break "
            "into (color, storage) -- these need the full product page (fetch_offers.py's "
            "jsonLD parse) to confirm, not just the search card text.",
            len(unparsed),
        )

    # Terms actually represented in the merged output.
    all_search_terms = sorted({r["search_term"] for r in all_results})

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "search_terms": all_search_terms,
        "results": all_results,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    log.info(
        "Done: %d matched result(s) written to %s (%d high-confidence, %d low-confidence -- "
        "review before feeding fetch_offers.py).",
        len(all_results), OUTPUT_FILE,
        sum(1 for r in all_results if r["confidence"] == "high"),
        sum(1 for r in all_results if r["confidence"] == "low"),
    )


if __name__ == "__main__":
    main()
