#!/usr/bin/env python3
"""fetch_offers_amazon.py: Amazon India equivalent of fetch_offers.py.

Two genuinely different fetch mechanisms are needed here, confirmed live
during investigation -- unlike Flipkart, where a single plain HTML fetch
gave both price and bank offers:

  - Price / stock / specs: ScraperAPI's structured Amazon product endpoint
    (`/structured/amazon/product/v1`). Plain (non-render) call, ~1 credit,
    returns clean pre-parsed JSON. No HTML parsing needed on our side.

  - Bank/card offers: NOT present anywhere in the static page. They load
    only via a click-triggered AJAX call
    (`/gp/product/ajax/vsxOffersSecondaryView`) that requires a live,
    non-headless-fingerprinted browser session -- confirmed live that
    ScraperAPI's plain proxy (even with session_number for IP stickiness)
    and its render+click instruction set both fail to trigger this, while
    a stealth-patched Playwright Chromium succeeds reliably, at volume
    (10/10 page loads, 9/10 offer captures across different real ASINs,
    same 2-4s politeness pacing as Flipkart), with NO ScraperAPI proxy
    needed for this specific step -- the blocker was headless-browser
    detection (navigator.webdriver + "HeadlessChrome" in the UA), not IP
    reputation. The stealth recipe, in order, all required:
      1. headless=False (a real, non-headless Chromium window)
      2. args=["--disable-blink-features=AutomationControlled"]
      3. ignore_default_args=["--enable-automation"]
      4. A realistic desktop Chrome User-Agent override
      5. An init script patching navigator.webdriver to undefined
    Dropping any one of these silently breaks the click again (the AJAX
    request is never even attempted -- no error, no block page).

  Amazon's offer text format is far more uniform than Flipkart's ever
  was: "Flat INR {amount} Instant Discount on {Bank} Credit Card
  [{N} months and above] EMI Txn. Minimum purchase value INR {amount}"
  -- confirmed on every real offer seen so far. See
  offer_parser_amazon.py for the regex built against this.

Writes to the same Supabase tables as fetch_offers.py (variants,
fetch_snapshots) with marketplace="amazon" -- the schema was already
marketplace-agnostic (variants.marketplace CHECK already allowed
'amazon', variants.pid is a generic unique text column reused here for
ASIN), so no migration was needed.
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright
from postgrest.exceptions import APIError

from pipeline_logging import setup_logging
from supabase_client import get_client

load_dotenv()

log = logging.getLogger("fetch_offers_amazon")

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
STRUCTURED_ENDPOINT = "https://api.scraperapi.com/structured/amazon/product/v1"
REQUEST_TIMEOUT = 60
MARKETPLACE = "amazon"

MIN_DELAY = 2.0  # seconds between product page visits (bank-offer fetch)
MAX_DELAY = 4.0

# Diagnostic capture only -- written when a product's Bank Offer section
# isn't found, to tell "genuinely no bank offers on this product" apart
# from "the page didn't render the real content at all" (bot-check page,
# CAPTCHA, slow-network partial load, etc.), since that distinction can't
# be made from the pipeline's own summary counts alone. Uploaded as a
# GitHub Actions artifact by pipeline_amazon.yml -- see its "Upload debug
# screenshots" step.
DEBUG_SCREENSHOTS_DIR = Path(__file__).parent / "debug_screenshots"

STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DISCOVERED_VARIANTS_FILE = Path(__file__).parent / "discovered_variants_amazon.json"

PRICE_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def load_targets_from_discovery(path: Path = DISCOVERED_VARIANTS_FILE) -> list[dict[str, str]]:
    with open(path) as f:
        data = json.load(f)
    return [
        {
            "model": r["search_term"],
            "asin": r["asin"],
            "color": r.get("color"),
            "storage": r.get("storage"),
            "ram": r.get("ram"),
        }
        for r in data["results"]
        if r["confidence"] == "high"
    ]


def derive_brand(model: str) -> str:
    return model.split()[0] if model.split() else model


def parse_price(text: Optional[str]) -> Optional[int]:
    """"₹41,999" -> 41999. `fetch_snapshots.price` is an integer column --
    PostgREST rejects a float literal like 41999.0 outright, even though
    it's numerically whole (confirmed live: every real write failed with
    "invalid input syntax for type integer" until this returned int).
    Returns None -- not a guess -- if no numeric figure is present."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    return round(float(m.group(0).replace(",", ""))) if m else None


def parse_in_stock(status: Optional[str]) -> Optional[bool]:
    """Only "In stock" has actually been confirmed live so far. Defensive
    reading, same philosophy as Flipkart's extract_in_stock(): treat
    explicit unavailable/out-of-stock wording as False; anything else
    (including an unconfirmed "Only N left in stock" phrasing, which is
    still buyable) as True rather than guessing it's unavailable. None if
    the field itself is missing."""
    if not status:
        return None
    s = status.strip().lower()
    if "unavailable" in s or "out of stock" in s:
        return False
    return True


def fetch_price_stock(asin: str, retries: int = 1) -> Optional[dict[str, Any]]:
    """Structured product lookup, with one retry on a transient gap --
    confirmed live that this endpoint occasionally comes back with
    product_information fields present but empty for a given ASIN on one
    call and populated on the very next, unrelated to real product state."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                STRUCTURED_ENDPOINT,
                params={"api_key": SCRAPERAPI_KEY, "asin": asin, "country_code": "in", "tld": "in"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            log.warning("%s: structured lookup request failed (attempt %d): %s", asin, attempt + 1, e)
            continue
        if resp.status_code != 200:
            log.warning("%s: structured lookup HTTP %s (attempt %d)", asin, resp.status_code, attempt + 1)
            continue
        try:
            data = resp.json()
        except json.JSONDecodeError:
            log.warning("%s: structured lookup non-JSON response (attempt %d)", asin, attempt + 1)
            continue
        info = data.get("product_information") or {}
        if attempt < retries and not any([info.get("colour"), info.get("memory_storage_capacity")]):
            log.info("%s: structured lookup came back with empty specs, retrying once", asin)
            continue
        return data
    return None


def _log_no_bank_offer_diagnostics(page: Page, asin: str) -> None:
    """Called whenever bank_span.count() == 0. Captures enough to tell
    apart a genuine "this product has no Bank Offer category" (expected,
    normal) from "the page never actually rendered the real product page"
    (bot-check/CAPTCHA/partial load under different network conditions --
    e.g. GitHub Actions' datacenter IP getting treated differently than a
    home ISP IP, the exact class of issue ScraperAPI exists to solve for
    Flipkart)."""
    try:
        title = page.title()
    except Exception as e:
        title = f"<title lookup failed: {e}>"

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""
    bot_markers = [
        m for m in ("Robot Check", "captcha", "Enter the characters you see below",
                     "To discuss automated access", "api-services-support@amazon.com")
        if m.lower() in body_text.lower()
    ]
    offers_carousel_present = "Bank Offer" in body_text  # the plain teaser text, not the clickable span
    productTitle_present = page.locator("#productTitle").count() > 0

    log.warning(
        "%s: no Bank Offer section found -- diagnostics: page title=%r, "
        "productTitle element present=%s, 'Bank Offer' text anywhere on page=%s, bot-check markers=%s",
        asin, title, productTitle_present, offers_carousel_present, bot_markers or "none",
    )

    try:
        DEBUG_SCREENSHOTS_DIR.mkdir(exist_ok=True)
        screenshot_path = DEBUG_SCREENSHOTS_DIR / f"{asin}_no_bank_offer.png"
        page.screenshot(path=str(screenshot_path), full_page=False)
        log.info("%s: saved diagnostic screenshot to %s", asin, screenshot_path)
    except Exception as e:
        log.warning("%s: failed to save diagnostic screenshot: %s", asin, e)


CONTINUE_SHOPPING_TEXT = "Continue shopping"


def _dismiss_continue_shopping_interstitial(page: Page) -> bool:
    """Confirmed live via a CI diagnostic screenshot: GitHub Actions'
    datacenter IP gets served a soft click-through interstitial ("Click
    the button below to continue shopping" / "Continue shopping" button)
    instead of the real product page -- not a CAPTCHA, no puzzle, just a
    button Amazon serves to traffic it's suspicious of. Never reproduced
    from a home IP, so this is IP-reputation-based, not the headless
    fingerprint (already fixed separately). Returns True if the
    interstitial was found and clicked through."""
    try:
        button = page.get_by_text(CONTINUE_SHOPPING_TEXT, exact=False)
        if button.count() == 0:
            return False
        button.first.click(timeout=5000)
        page.wait_for_timeout(2000)
        return True
    except Exception:
        return False


def fetch_bank_offers(page: Page, asin: str) -> tuple[Optional[str], Optional[str]]:
    """Navigate to the product page and, if a Bank Offer category exists,
    click through to capture the real per-bank offer lines via the
    vsxOffersSecondaryView AJAX response. Returns (raw_offer_text,
    error) -- raw_offer_text is None (not an error) when the product
    simply has no Bank Offer category at all, which is a real, normal
    case, not a failure."""
    url = f"https://www.amazon.in/dp/{asin}"
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        return None, f"page load failed: {e}"

    # Check for (and clear) the interstitial up to three times -- confirmed
    # live that it can reappear after a single dismissal on some
    # requests, not just a one-time-per-session gate.
    for attempt in range(3):
        if page.locator("#productTitle").count() > 0:
            break
        if not _dismiss_continue_shopping_interstitial(page):
            break
        log.info("%s: dismissed a 'Continue shopping' interstitial (attempt %d)", asin, attempt + 1)
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            return None, f"page reload after interstitial failed: {e}"

    page.wait_for_timeout(2000)

    bank_span = page.locator('span.a-declarative[data-action="side-sheet"]', has_text="Bank Offer")
    if bank_span.count() == 0:
        _log_no_bank_offer_diagnostics(page, asin)
        return None, None  # no bank offers on this product -- not an error

    try:
        with page.expect_response(lambda r: "vsxOffersSecondaryView" in r.url, timeout=10000) as resp_info:
            bank_span.first.locator("a.vsx-offers-count").click(timeout=10000)
        panel_html = resp_info.value.text()
    except Exception as e:
        return None, f"bank offer click/response failed: {e}"

    panel_text = re.sub(r"<[^>]+>", " ", panel_html)
    lines = extract_offer_lines(panel_text)
    if not lines:
        return None, "bank offer panel opened but no offer lines found (possible layout change)"
    return "\n".join(lines), None


OFFER_LINE_RE = re.compile(r"Flat INR [\d,]+ Instant Discount on .+?Minimum purchase value INR [\d,]+")


def extract_offer_lines(panel_text: str) -> list[str]:
    """Pull just the real offer sentences out of the side-sheet's full
    text (which also contains "Offer N" headers, "See details" links,
    and footer text like "How to avail offer"). Each real offer's text
    is legitimately present twice in the raw markup -- once in the
    visible <p>, once again embedded in the "See details" trigger's own
    data-side-sheet JSON payload -- confirmed live, not a regex bug, so
    dedupe while preserving order rather than treating every match as a
    distinct offer."""
    matches = OFFER_LINE_RE.findall(panel_text.replace("\n", " "))
    seen = set()
    lines = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            lines.append(m)
    return lines


def save_to_supabase(client, target: dict[str, str], price_data: Optional[dict[str, Any]],
                      offer_text: Optional[str], fetch_error: Optional[str]) -> Optional[str]:
    label = f"{target['model']} ({target.get('color')}, {target.get('storage')}, {target.get('ram')})"
    timestamp = datetime.now(timezone.utc).isoformat()

    info = (price_data or {}).get("product_information") or {}
    color = info.get("colour") or target.get("color")
    storage = info.get("memory_storage_capacity") or target.get("storage")
    ram = info.get("ram_memory_installed") or target.get("ram")

    if not color or not storage:
        return f"missing color/storage for {target['asin']} -- cannot write a labeled variant"

    try:
        variant_resp = (
            client.table("variants")
            .upsert(
                {
                    "pid": target["asin"],
                    "model": target["model"],
                    "storage": storage,
                    "color": color,
                    "ram": ram,
                    "brand": derive_brand(target["model"]),
                    "marketplace": MARKETPLACE,
                },
                on_conflict="pid",
            )
            .execute()
        )
        variant_id = variant_resp.data[0]["id"]

        price = parse_price((price_data or {}).get("pricing"))
        in_stock = parse_in_stock((price_data or {}).get("availability_status"))

        client.table("fetch_snapshots").insert(
            {
                "variant_id": variant_id,
                "price": price,
                "in_stock": in_stock,
                "fetched_at": timestamp,
                "match_warning": None,
                "fetch_error": fetch_error,
                "raw_offer_text": offer_text,
                "parsed_at": None if offer_text else timestamp,
            }
        ).execute()
    except APIError as e:
        return f"Supabase write failed: {e.message}"

    log.info("%s: saved to Supabase (variant_id=%s)", label, variant_id)
    return None


def run_all(client, targets: Optional[list[dict[str, str]]] = None) -> dict[str, int]:
    if targets is None:
        targets = load_targets_from_discovery()

    results = {"total": len(targets), "ok": 0, "no_bank_offers": 0, "failed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        for i, target in enumerate(targets):
            label = f"{target['model']} ({target.get('color')}, {target.get('storage')}, {target.get('ram')})"

            price_data = fetch_price_stock(target["asin"])
            if price_data is None:
                log.error("%s: price/stock lookup failed entirely", label)

            # Fresh context (fresh cookies/storage) per product, not one
            # session shared across the whole run. Confirmed live: a
            # shared session's "Continue shopping" interstitial rate
            # climbed across the run (1/10 -> 7/10 blocked on consecutive
            # CI tests) even with more per-page retries, consistent with
            # Amazon scoring session risk cumulatively across rapid
            # back-to-back page loads rather than purely per-IP. A fresh
            # context makes every product look like an independent first
            # visit again.
            context = browser.new_context(user_agent=STEALTH_UA, viewport={"width": 1400, "height": 1000})
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()
            try:
                page.goto("https://www.amazon.in/", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                if _dismiss_continue_shopping_interstitial(page):
                    log.info("%s: dismissed a 'Continue shopping' interstitial during warm-up", label)
            except Exception as e:
                log.warning("%s: session warm-up failed (continuing anyway): %s", label, e)

            offer_text, offer_error = fetch_bank_offers(page, target["asin"])
            context.close()
            if offer_error:
                log.warning("%s: %s", label, offer_error)
            elif offer_text is None:
                results["no_bank_offers"] += 1
                log.info("%s: no Bank Offer category on this product", label)
            else:
                log.info("%s: %d bank offer line(s) captured", label, len(offer_text.splitlines()))

            fetch_error = None
            if price_data is None and offer_error:
                fetch_error = f"price lookup failed; {offer_error}"
            elif price_data is None:
                fetch_error = "price lookup failed"
            elif offer_error:
                fetch_error = offer_error

            supabase_error = save_to_supabase(client, target, price_data, offer_text, fetch_error)
            if supabase_error:
                log.error("%s: %s", label, supabase_error)
                results["failed"] += 1
            else:
                results["ok"] += 1

            if i < len(targets) - 1:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        browser.close()

    log.info(
        "Done: %d/%d succeeded, %d with no bank offers, %d failed.",
        results["ok"], results["total"], results["no_bank_offers"], results["failed"],
    )
    return results


def main() -> None:
    setup_logging()
    if not SCRAPERAPI_KEY:
        raise SystemExit("SCRAPERAPI_KEY must be set (see .env.example)")
    client = get_client()
    run_all(client)


if __name__ == "__main__":
    main()
