#!/usr/bin/env python3
"""fetch_offers.py: Fetch current price + raw bank/card offer text for
specific Flipkart phone variants, without login or a headless browser.

Phase 1 findings (see investigation notes) that shape this script:
  - Each storage/color combination is a separate Flipkart listing with its
    own `pid`; the `pid` query param alone is authoritative for routing
    (the URL slug and `itm...` path segment are cosmetic).
  - Price and offer data are present in the raw HTML on a plain GET -- no
    JS execution needed. Two embedded JSON blocks carry it:
      * <script id="jsonLD"> - standard schema.org Product/Offer markup
        (name, sku, color, offers.price). Stable, since it's meant for
        Google Shopping, not just internal use.
      * window.__INITIAL_STATE__ - Flipkart's internal page state. The
        bank/card offer widget lives somewhere in
        multiWidgetState.widgetsData.slots[*].slotData.widget.data.offers,
        but its slot INDEX is not fixed (it was 7 on one product page and
        8 on another) -- so it's located by shape, not position.

Writes straight to Supabase (variants + fetch_snapshots) -- no more
offers.json. Each run inserts a fresh snapshot per variant so history
accumulates; offer_parser.py picks up snapshots where parsed_at IS NULL.
"""

import concurrent.futures
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import requests
from postgrest.exceptions import APIError

from pipeline_logging import setup_logging
from supabase_client import get_client

log = logging.getLogger("fetch_offers")

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
MIN_DELAY = 2.0  # seconds between requests
MAX_DELAY = 5.0
MARKETPLACE = "flipkart"

# ScraperAPI routing -- added after Flipkart started returning HTTP 403s at
# volume and Amazon India was confirmed (live test) to hard-block plain
# requests.get() on the very first attempt. Optional: falls back to a
# direct request if the key isn't set, so local dev without a key still
# works against anything not currently being blocked.
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com"
SCRAPERAPI_TIMEOUT = 60  # ScraperAPI's own internal retries take longer than a direct request
# Matches the plan's own concurrencyLimit (confirmed 5 via GET /account) --
# ScraperAPI does its own IP rotation/header spoofing per request, so
# concurrent requests don't present the single-source-hammering pattern
# that got Flipkart to 403 the old direct-fetch approach. Bump this only if
# the ScraperAPI plan's concurrency limit is raised.
SCRAPERAPI_CONCURRENCY = 5

JSONLD_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*id="jsonLD"[^>]*>(.*?)</script>',
    re.DOTALL,
)
INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", re.DOTALL
)
# Matches the "(Color, 128 GB)" suffix on Flipkart product names.
NAME_VARIANT_RE = re.compile(r"\(([^,]+),\s*([\d]+\s*GB)\)\s*$")
# RAM matters for bank-offer tracking -- offers/min-purchase thresholds can
# differ by RAM even at the same color+storage (confirmed live: Vivo T4
# Lite's "Titanium Gold, 128 GB" is two distinct pids/prices, 4GB and 6GB
# RAM). It is NOT present in jsonLD's product name for any model tested
# (POCO C81, Vivo T5x 5G, Motorola g37, Vivo T4 Lite all confirmed absent).
# It IS usually present in the product page's own SEO title:
# "{Model} ({Storage} Storage, {RAM} RAM) Online at Best Price On
# Flipkart.com" -- BUT confirmed live that this title can be stale for a
# given pid (a real Vivo T4r listing's SEO title said "128 GB Storage, 8 GB
# RAM" while its own jsonLD -- the already-trusted source for price/color --
# said 256 GB for the same pid). Captures storage too, not just RAM, so the
# caller can cross-check the title's own internal consistency before
# trusting its RAM figure at all.
SEO_TITLE_RAM_RE = re.compile(
    r"\(\s*(\d+\s*GB)\s+Storage\s*,\s*(\d+\s*GB)\s+RAM\s*\)", re.IGNORECASE
)

DISCOVERED_VARIANTS_FILE = "discovered_variants.json"


def load_targets_from_discovery(path: str = DISCOVERED_VARIANTS_FILE) -> list[dict[str, str]]:
    """Load the target list from discover_variants.py's output instead of a
    hand-curated list -- this is the whole point of Phase 5. Every entry in
    the file is already confidence-scored; only "high" is trusted here (in
    practice that's all of them as of this writing, but this stays a real
    filter, not a formality, in case a future sweep reintroduces "low"
    entries the user hasn't reviewed yet)."""
    with open(path) as f:
        data = json.load(f)
    return [
        {
            "model": r["search_term"],
            "storage": r["storage"],
            "color": r["color"],
            "url": r["url"],
            "pid": r["pid"],
            # Passed through as a fallback for extract_ram() when the page's
            # own SEO title turns out to be stale/unusable for this pid --
            # not used as the primary source when the title checks out.
            "ram": r.get("ram"),
        }
        for r in data["results"]
        if r["confidence"] == "high"
    ]


# ---------------------------------------------------------------------------
# Legacy hand-curated targets -- superseded by load_targets_from_discovery()
# above (Phase 5). Kept only as a reference for the 4 originally-manually-
# verified models; run_all() no longer uses this by default.
# ---------------------------------------------------------------------------
TARGETS: list[dict[str, str]] = [
    {
        "model": "Vivo T5x 5G",
        "storage": "128 GB",
        "color": "Cyber Green",
        "url": "https://www.flipkart.com/vivo-t5x-5g-cyber-green-128-gb/p/itm7da8aa253e72b?pid=MOBHH69NM5ERRNFT",
    },
    {
        "model": "Vivo T5x 5G",
        "storage": "256 GB",
        "color": "Cyber Green",
        "url": "https://www.flipkart.com/vivo-t5x-5g-cyber-green-256-gb/p/itm7da8aa253e72b?pid=MOBHH69NQGVVYQSG",
    },
    {
        "model": "POCO C85x",
        "storage": "64 GB",
        "color": "Sunset Gold",
        "url": "https://www.flipkart.com/poco-c85x-sunset-gold-64-gb/p/itm6b8cdd362c306?pid=MOBHMGG5PYZRFMKU",
    },
    {
        "model": "POCO C81",
        "storage": "64 GB",
        "color": "Sunset Gold",
        "url": "https://www.flipkart.com/poco-c81-sunset-gold-64-gb/p/itmd046653de9427?pid=MOBHMYWRFE7GZVJF",
    },
    {
        "model": "realme P4R 5G",
        "storage": "128 GB",
        "color": "Lavender Glare",
        "url": "https://www.flipkart.com/realme-p4r-5g-lavender-glare-128-gb/p/itmb42ae388c1ef6?pid=MOBHMYR3NPFHTFHE",
    },
]


def build_url(target: dict[str, str]) -> str:
    """Resolve a target's URL. A bare pid is enough -- Flipkart routes on
    the pid query param regardless of the path slug/itm segment."""
    if target.get("url"):
        return target["url"]
    if target.get("pid"):
        return f"https://www.flipkart.com/p/x?pid={target['pid']}"
    raise ValueError(f"Target for {target.get('model')} has neither 'url' nor 'pid'")


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


def extract_pid_from_url(url: str) -> Optional[str]:
    """Fallback pid source when the page fetch/parse fails before we can
    read jsonLD's `sku` (the authoritative source when available)."""
    query = parse_qs(urlparse(url).query)
    values = query.get("pid")
    return values[0] if values else None


def derive_brand(model: str) -> str:
    """First word of the model name, e.g. "Vivo T5x 5G" -> "Vivo",
    "POCO C85x" -> "POCO"."""
    return model.split()[0] if model.split() else model


def parse_jsonld(html: str) -> Optional[dict[str, Any]]:
    match = JSONLD_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    # Flipkart's jsonLD is a list with one product dict.
    if isinstance(data, list):
        return data[0] if data else None
    return data


def extract_in_stock(product: dict[str, Any]) -> Optional[bool]:
    """Confirmed live: jsonLD's offers.availability is a standard
    schema.org URI, e.g. "https://schema.org/InStock". No out-of-stock
    example has actually been seen live (none of our tracked variants are
    currently out of stock, and Flipkart's search doesn't surface delisted
    items easily to find one) -- so rather than match an exact
    "OutOfStock" string that hasn't been confirmed, treat anything that
    doesn't end in "InStock" as not in stock. That's the safe reading
    regardless of which of schema.org's several "not available" values
    (OutOfStock, Discontinued, SoldOut, etc.) Flipkart actually uses."""
    availability = product.get("offers", {}).get("availability")
    if not isinstance(availability, str) or not availability:
        return None
    return availability.rstrip("/").endswith("InStock")


def extract_ram(state: dict[str, Any], expected_storage: Optional[str] = None) -> Optional[str]:
    """Pull RAM out of the product page's SEO title. Cross-checks the
    title's own embedded storage figure against `expected_storage`
    (typically the target's already-confirmed storage) before trusting the
    RAM figure -- confirmed live that this title can be stale for a given
    pid (storage disagreed with jsonLD for a real Vivo T4r listing), and a
    stale title can't be trusted for RAM either if it's already wrong about
    storage in the very same string. Returns None -- not a guess -- on any
    mismatch or parse failure; the caller falls back to a supplied value if
    it has one. Unlike price/offers, a miss here doesn't fail the whole
    fetch -- it's an enrichment field, not core to what this script already
    does."""
    try:
        title = state["multiWidgetState"]["pageDataResponse"]["seoData"]["seo"]["title"]
    except (KeyError, TypeError):
        return None
    if not isinstance(title, str):
        return None
    m = SEO_TITLE_RAM_RE.search(title)
    if not m:
        return None
    title_storage, title_ram = m.group(1).strip(), m.group(2).strip()
    if expected_storage and normalize(title_storage) != normalize(expected_storage):
        log.warning(
            "SEO title storage (%s) disagrees with expected storage (%s) -- "
            "treating its RAM figure (%s) as unreliable too",
            title_storage, expected_storage, title_ram,
        )
        return None
    return title_ram


def find_offers_widget_data(state: dict[str, Any]) -> Optional[list]:
    """Locate the offers widget by shape, not by slot index -- the index
    differs between products (seen at 7 and 8 across the models tested)."""
    try:
        slots = state["multiWidgetState"]["widgetsData"]["slots"]
    except (KeyError, TypeError):
        return None
    for slot in slots:
        try:
            widget_data = slot["slotData"]["widget"]["data"]
        except (KeyError, TypeError):
            continue
        if isinstance(widget_data, dict) and isinstance(widget_data.get("offers"), list):
            return widget_data["offers"]
    return None


def find_bank_offer_grids(node: Any, out: list) -> None:
    """Recursively collect every bankOfferGrid list found anywhere under
    `node`. The offers widget nests this at inconsistent depths -- it was
    one level under `offers[i].value` during initial investigation, but a
    later fetch of the *same* product had it two levels deeper inside a
    BANK_TAB/EMI_TAB sub-structure. Searching recursively (instead of a
    fixed path) is what survives that drift."""
    if isinstance(node, dict):
        if isinstance(node.get("bankOfferGrid"), list):
            out.append(node["bankOfferGrid"])
        for v in node.values():
            find_bank_offer_grids(v, out)
    elif isinstance(node, list):
        for v in node:
            find_bank_offer_grids(v, out)


def extract_bank_offers_raw(offers_data: list) -> Optional[str]:
    """Flatten the bank/card offer pills into a raw text block, one offer
    per line. The same "bankOfferGrid" component is reused for an unrelated
    No Cost EMI bank list (e.g. "ICICI Bank -> ₹4,667/m") -- those entries'
    discountedPriceText never contains "off", so filtering on that
    distinguishes real discount/cashback offers from EMI plan rows."""
    grids: list = []
    find_bank_offer_grids(offers_data, grids)

    seen = set()
    lines = []
    for grid in grids:
        for item in grid:
            v = item.get("value", {})
            title = (v.get("offerTitle") or "").strip()
            amount = (v.get("discountedPriceText") or "").strip()
            if "off" not in amount.lower():
                continue  # EMI-plan row, not a discount/cashback offer
            subtitle = ""
            try:
                content_list = v["offerSubTitleRC"]["value"]["contentList"]
                subtitle = " ".join(c.get("contentValue", "") for c in content_list).strip()
            except (KeyError, TypeError):
                pass
            key = (title, subtitle, amount)
            if key in seen:
                continue
            seen.add(key)
            line = f"{title} ({subtitle}): {amount}" if subtitle else f"{title}: {amount}"
            lines.append(line)
    return "\n".join(lines) if lines else None


def cross_check(supplied: dict[str, str], parsed_name: Optional[str], parsed_color: Optional[str]) -> Optional[str]:
    """Compare the page's own jsonLD name/color against the supplied labels.
    Never overrides the supplied values -- only reports a mismatch."""
    mismatches = []

    if parsed_color:
        if normalize(parsed_color) != normalize(supplied["color"]):
            mismatches.append(f"color: supplied='{supplied['color']}' vs page='{parsed_color}'")
    else:
        mismatches.append("color: not found in page jsonLD")

    parsed_storage = None
    if parsed_name:
        m = NAME_VARIANT_RE.search(parsed_name)
        if m:
            parsed_storage = m.group(2)
    if parsed_storage:
        if normalize(parsed_storage) != normalize(supplied["storage"]):
            mismatches.append(f"storage: supplied='{supplied['storage']}' vs page='{parsed_storage}'")
    else:
        mismatches.append("storage: could not parse from page name")

    return "; ".join(mismatches) if mismatches else None


def fetch_variant(target: dict[str, str]) -> dict[str, Any]:
    label = f"{target['model']} ({target['storage']}, {target['color']})"
    url = build_url(target)
    timestamp = datetime.now(timezone.utc).isoformat()

    result: dict[str, Any] = {
        "model": target["model"],
        "storage": target["storage"],
        "color": target["color"],
        # Best guess until/unless jsonLD's `sku` (authoritative) overrides it below.
        "pid": target.get("pid") or extract_pid_from_url(url),
        "price": None,
        "ram": None,
        "in_stock": None,
        "offer_text_raw": None,
        "timestamp": timestamp,
        "url": url,
        "match_warning": None,
        "error": None,
    }

    # --- Network fetch ---
    # ScraperAPI manages its own target-site headers/UA/IP rotation
    # internally, so our own HEADERS are only meaningful on the direct
    # path -- sending them to ScraperAPI's own endpoint wouldn't reach
    # Flipkart/Amazon anyway.
    if SCRAPERAPI_KEY:
        request_url = SCRAPERAPI_ENDPOINT
        request_params = {"api_key": SCRAPERAPI_KEY, "url": url}
        request_headers = {}
        timeout = SCRAPERAPI_TIMEOUT
    else:
        request_url = url
        request_params = {}
        request_headers = HEADERS
        timeout = REQUEST_TIMEOUT

    try:
        response = requests.get(request_url, headers=request_headers, params=request_params, timeout=timeout)
    except requests.exceptions.Timeout:
        result["error"] = f"Request timed out after {timeout}s"
        log.error("%s: %s", label, result["error"])
        return result
    except requests.exceptions.RequestException as e:
        result["error"] = f"Request failed: {e}"
        log.error("%s: %s", label, result["error"])
        return result

    if response.status_code != 200:
        result["error"] = f"Unexpected HTTP status {response.status_code}"
        log.error("%s: %s", label, result["error"])
        return result

    html = response.text

    # --- Price + variant identity (jsonLD) ---
    product = parse_jsonld(html)
    parse_errors = []
    if product is None:
        parse_errors.append("jsonLD block not found or unparseable (possible layout change)")
    else:
        price = product.get("offers", {}).get("price")
        if price is None:
            parse_errors.append("jsonLD found but 'offers.price' missing (possible layout change)")
        else:
            result["price"] = price
        if product.get("sku"):
            result["pid"] = product["sku"]  # authoritative, straight from the live page
        result["match_warning"] = cross_check(target, product.get("name"), product.get("color"))
        if result["match_warning"]:
            log.warning("%s: label mismatch -- %s", label, result["match_warning"])
        result["in_stock"] = extract_in_stock(product)
        if result["in_stock"] is False:
            log.warning("%s: currently out of stock on Flipkart", label)

    # --- Bank/card offer text (__INITIAL_STATE__) ---
    state_match = INITIAL_STATE_RE.search(html)
    if not state_match:
        parse_errors.append("__INITIAL_STATE__ block not found (possible layout change)")
    else:
        try:
            state = json.loads(state_match.group(1))
        except json.JSONDecodeError:
            parse_errors.append("__INITIAL_STATE__ found but failed to parse as JSON")
            state = None
        if state is not None:
            offers_data = find_offers_widget_data(state)
            if offers_data is None:
                parse_errors.append("offers widget not found in page state (possible layout change)")
            else:
                result["offer_text_raw"] = extract_bank_offers_raw(offers_data)
                if result["offer_text_raw"] is None:
                    parse_errors.append("offers widget found but contained no bank/card offers")

            result["ram"] = extract_ram(state, expected_storage=target["storage"])
            if result["ram"] is None and target.get("ram"):
                # SEO title missing/stale for this pid -- fall back to
                # discovery's own RAM value (e.g. from complete_variants.py's
                # per-color widget read) rather than leaving it blank when a
                # trustworthy value is already known.
                result["ram"] = target["ram"]
                log.info("%s: using discovery-supplied RAM (%s) -- page's own SEO title was unusable", label, target["ram"])
            elif result["ram"] is None:
                # Enrichment field, not core -- doesn't go in parse_errors
                # (wouldn't fail the fetch), just a heads-up in the log.
                log.warning("%s: could not extract RAM from SEO title (possible layout change)", label)

    if parse_errors:
        result["error"] = "; ".join(parse_errors)
        log.error("%s: %s", label, result["error"])
    else:
        log.info("%s: price=%s, ram=%s, %d offer line(s)", label, result["price"], result["ram"],
                  len(result["offer_text_raw"].splitlines()) if result["offer_text_raw"] else 0)

    return result


def save_to_supabase(client, result: dict[str, Any]) -> Optional[str]:
    """Upsert the variant and insert a new snapshot row. Returns an error
    string on failure (never raises) so one bad write doesn't kill the run."""
    label = f"{result['model']} ({result['storage']}, {result['color']})"

    if not result["pid"]:
        return "no pid available (fetch failed before jsonLD/URL could supply one) -- cannot write to Supabase"

    try:
        variant_resp = (
            client.table("variants")
            .upsert(
                {
                    "pid": result["pid"],
                    "model": result["model"],
                    "storage": result["storage"],
                    "color": result["color"],
                    "ram": result["ram"],
                    "brand": derive_brand(result["model"]),
                    "marketplace": MARKETPLACE,
                },
                on_conflict="pid",
            )
            .execute()
        )
        variant_id = variant_resp.data[0]["id"]

        client.table("fetch_snapshots").insert(
            {
                "variant_id": variant_id,
                "price": result["price"],
                "in_stock": result["in_stock"],
                "fetched_at": result["timestamp"],
                "match_warning": result["match_warning"],
                "fetch_error": result["error"],
                "raw_offer_text": result["offer_text_raw"],
                # Nothing for offer_parser.py to do if there's no raw text --
                # mark it parsed immediately so its `parsed_at IS NULL` query
                # doesn't pick up snapshots with nothing to parse.
                "parsed_at": None if result["offer_text_raw"] else result["timestamp"],
            }
        ).execute()
    except APIError as e:
        return f"Supabase write failed: {e.message}"
    except requests.exceptions.RequestException as e:
        # supabase-py's HTTP transport can raise these on connection issues.
        return f"Supabase connection error: {e}"

    log.info("%s: saved to Supabase (variant_id=%s)", label, variant_id)
    return None


def fetch_and_save(client, target: dict[str, str]) -> dict[str, Any]:
    """One target's full fetch-then-write, bundled as a unit so it can be
    dispatched to a thread pool as a single task."""
    result = fetch_variant(target)
    supabase_error = save_to_supabase(client, result)
    if supabase_error:
        label = f"{result['model']} ({result['storage']}, {result['color']})"
        log.error("%s: %s", label, supabase_error)
    return result


def run_all(client, targets: Optional[list[dict[str, str]]] = None) -> dict[str, int]:
    """Fetch + save every target. Defaults to the full discovered catalogue
    (load_targets_from_discovery()) rather than the old hand-curated
    5-variant TARGETS list -- pass `targets` explicitly to override (e.g.
    for a manual test run against a subset). Returns summary counts so a
    caller (e.g. run_pipeline.py) can log/report them without re-parsing
    log output."""
    if targets is None:
        targets = load_targets_from_discovery()

    results = []
    if SCRAPERAPI_KEY:
        # Concurrent, bounded to the plan's own limit -- see
        # SCRAPERAPI_CONCURRENCY above for why this is safe to parallelize
        # (ScraperAPI owns IP rotation, not us).
        with concurrent.futures.ThreadPoolExecutor(max_workers=SCRAPERAPI_CONCURRENCY) as executor:
            futures = [executor.submit(fetch_and_save, client, target) for target in targets]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
    else:
        # No ScraperAPI key -- going straight at Flipkart/Amazon from one
        # IP, so keep the original serial + randomized-delay pacing (this is
        # the pattern that avoided detection before ScraperAPI was
        # introduced; concurrency here would be the real risk).
        for i, target in enumerate(targets):
            results.append(fetch_and_save(client, target))
            if i < len(targets) - 1:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    ok = sum(1 for r in results if r["error"] is None)
    warned = sum(1 for r in results if r["match_warning"])
    failed = sum(1 for r in results if r["error"] is not None)
    log.info(
        "Done: %d/%d succeeded, %d with label mismatches, %d failed.",
        ok, len(results), warned, failed,
    )
    return {"total": len(results), "ok": ok, "warned": warned, "failed": failed}


def main() -> None:
    setup_logging()
    client = get_client()
    run_all(client)


if __name__ == "__main__":
    main()
