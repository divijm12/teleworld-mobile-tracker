#!/usr/bin/env python3
"""complete_variants.py: Phase 5b -- close the gap search-based discovery
leaves behind, using a more reliable source: each Flipkart product page's
own variant-selector widgets, which list every RAM+storage combo for a
color, and every color for a model, straight from Flipkart's own data --
independent of search ranking entirely.

Confirmed live: discover_variants.py's search-based approach silently
missed real variants on multiple models (Vivo T4r, Oppo K14x, and Vivo T4
Lite's Prism Blue color specifically -- search only ever found 2 of its 4
real RAM+storage combos). A user manually checking Flipkart caught the
Prism Blue gap; this script is the systematic fix so gaps like that don't
have to be found by hand one at a time.

Two widgets, found by key name (not slot position -- Phase 1 already
established widget slot positions aren't stable across products):
  - "variant-selector-image-view" -- one representative product URL per
    COLOR (found within widgetsData.slots specifically; the same key
    prefix also appears in unrelated dlsLayouts config and must be
    excluded, or every product falsely "matches").
  - "variant-selector-details-view" -- every (RAM, storage, pid, url,
    price) combo for the CURRENT page's color. Not present at all for
    single-storage-tier phones (e.g. POCO C81, which has no separate RAM
    axis) -- that's expected, not a failure.

For each model already in discovered_variants.json:
  1. Take one known pid, fetch its product page.
  2. Read the color-swatch widget -> one representative url per color.
  3. Fetch each color's own page, read its RAM+storage widget -> every
     real combo for that color (or, if the widget is absent, treat that
     color's own page as a single terminal variant).
  4. Diff against what's already known; add any genuinely new pids found.

Does not replace discover_variants.py -- that's still how a *new* model
gets its first pid. This is a completeness pass over models already
partially known, not a general-purpose search tool.
"""

import json
import logging
import random
import re
import time
from typing import Any, Optional
from urllib.parse import urljoin

import requests

from fetch_offers import (
    HEADERS,
    INITIAL_STATE_RE,
    REQUEST_TIMEOUT,
    extract_pid_from_url,
    parse_jsonld,
)
from pipeline_logging import setup_logging

log = logging.getLogger("complete_variants")

DISCOVERED_VARIANTS_FILE = "discovered_variants.json"
MIN_DELAY = 2.0
MAX_DELAY = 5.0

# Same shape as the "(Titanium Gold 2026)" case discover_variants.py's
# ALT_NAME_VARIANT_RE already strips -- jsonLD's own `color` field carries
# the same trailing year on some models; strip it for consistency with
# every other color string already in discovered_variants.json.
TRAILING_YEAR_RE = re.compile(r"\s+\d{4}$")
# Widget's own "128 GB + 4 GB" contentTitle -- storage first, RAM second.
RAM_STORAGE_TITLE_RE = re.compile(r"(\d+\s*GB)\s*\+\s*(\d+\s*GB)")


def find_widgets_by_key(node: Any, key_prefix: str, out: list) -> None:
    """Recursively collect every dict value keyed by a name starting with
    `key_prefix` and shaped like an actual widget instance (has a "value"
    key) -- confirmed live that the same key prefix also appears in
    unrelated dlsLayouts config entries without a "value" key, which would
    otherwise falsely match every single product page."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.startswith(key_prefix) and isinstance(v, dict) and "value" in v:
                out.append(v)
            find_widgets_by_key(v, key_prefix, out)
    elif isinstance(node, list):
        for v in node:
            find_widgets_by_key(v, key_prefix, out)


def extract_color_swatch_urls(state: dict[str, Any]) -> list[str]:
    """One representative product URL per color. Scoped to widgetsData.slots
    specifically (not the whole state tree) to avoid the dlsLayouts
    false-match issue described above."""
    try:
        slots = state["multiWidgetState"]["widgetsData"]["slots"]
    except (KeyError, TypeError):
        return []
    widgets: list = []
    find_widgets_by_key(slots, "variant-selector-image-view", widgets)

    urls = []
    for widget in widgets:
        try:
            items = widget["value"]["horizontalListData_0"]["value"]
        except (KeyError, TypeError):
            continue
        for item in items:
            try:
                url = item["value"]["box_0"]["action"]["url"]
            except (KeyError, TypeError):
                continue
            if url:
                urls.append(url)
    return urls


def extract_ram_storage_matrix(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every (ram, storage, pid, url) combo for the CURRENT page's color.
    Returns [] for phones with no separate RAM/storage axis (single
    storage tier) -- that's a normal, expected outcome, not an error."""
    try:
        slots = state["multiWidgetState"]["widgetsData"]["slots"]
    except (KeyError, TypeError):
        return []
    widgets: list = []
    find_widgets_by_key(slots, "variant-selector-details-view", widgets)

    combos = []
    for widget in widgets:
        try:
            items = widget["value"]["scrollToListData_0"]["value"]
        except (KeyError, TypeError):
            continue
        for item in items:
            try:
                url = item["value"]["col_0"]["action"]["url"]
                title = item["value"]["trackerData_0"]["tracking"]["contentTitle"]
            except (KeyError, TypeError):
                continue
            if not url or not title:
                continue
            m = RAM_STORAGE_TITLE_RE.match(title.strip())
            if not m:
                log.warning("Unrecognized RAM/storage widget title format: %r -- skipping this combo", title)
                continue
            storage, ram = m.group(1), m.group(2)
            combos.append({"storage": storage, "ram": ram, "url": url})
    return combos


def clean_pid_url(url: str) -> str:
    """Widget URLs carry extra tracking params after pid (&marketplace=...
    &lid=...) -- strip everything after the first "&" so stored URLs match
    the plain "...?pid=XXX" shape used everywhere else in this file."""
    if url.startswith("/"):
        url = urljoin("https://www.flipkart.com", url)
    return url.split("&")[0]


def fetch_page(url: str) -> Optional[tuple[dict, dict]]:
    """Fetch one product page, return (jsonld_product, initial_state).
    Reuses fetch_offers.py's HEADERS/timeout/regexes/parser for consistency
    with the rest of the pipeline. Returns None only on a hard failure
    (request error or non-200); a missing/unparseable __INITIAL_STATE__
    still returns the jsonLD half with an empty state dict."""
    if url.startswith("/"):
        url = urljoin("https://www.flipkart.com", url)

    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        log.error("Fetch failed for %s: %s", url, e)
        return None
    if response.status_code != 200:
        log.error("Unexpected HTTP status %d for %s", response.status_code, url)
        return None

    html = response.text
    product = parse_jsonld(html) or {}

    state_match = INITIAL_STATE_RE.search(html)
    if not state_match:
        log.warning("__INITIAL_STATE__ not found for %s", url)
        return product, {}
    try:
        state = json.loads(state_match.group(1))
    except json.JSONDecodeError:
        log.warning("__INITIAL_STATE__ failed to parse for %s", url)
        state = {}
    return product, state


def complete_model_variants(model: str, known_entries: list[dict]) -> list[dict]:
    """Given a model's already-known entries, walk Flipkart's own
    color/RAM/storage widget data to find every real variant, and return
    entries for any pid not already known. Every returned entry's color
    comes straight from that color's own product page -- never guessed.

    Also backfills `ram` on already-known entries that are missing it
    (mutates them in place, since `known_entries` are the same dict objects
    held in the caller's `data["results"]` list) -- a pid found via old
    search-based discovery never had RAM in the first place, and the first
    version of this function skipped already-known pids entirely, so those
    silently kept a null RAM forever even when this same walk was passing
    right by their widget data."""
    if not known_entries:
        return []

    known_by_pid = {e["pid"]: e for e in known_entries}
    seed_url = known_entries[0]["url"]

    seed_result = fetch_page(seed_url)
    if seed_result is None:
        log.error("%s: could not fetch seed page (%s), skipping completeness check", model, seed_url)
        return []
    _, seed_state = seed_result

    color_urls = extract_color_swatch_urls(seed_state)
    if not color_urls:
        # No color-swatch widget -- either a single-color model, or this
        # page's own color already covers everything. Still worth checking
        # this one page's own RAM/storage matrix before giving up.
        color_urls = [seed_url]

    new_entries: list[dict] = []
    seen_pids_this_model = set(known_by_pid.keys())
    backfilled = 0

    for i, color_url in enumerate(color_urls):
        if i > 0:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        result = fetch_page(color_url)
        if result is None:
            continue
        product, state = result

        raw_color = product.get("color")
        if not raw_color:
            log.warning("%s: could not determine color name for %s -- skipping this color", model, color_url)
            continue
        color_name = TRAILING_YEAR_RE.sub("", raw_color).strip()

        combos = extract_ram_storage_matrix(state)
        if not combos:
            # Normal for single-storage-tier phones (e.g. POCO C81) -- the
            # color page itself IS the one variant for this color.
            pid = extract_pid_from_url(color_url)
            if pid and pid not in seen_pids_this_model:
                seen_pids_this_model.add(pid)
                new_entries.append({
                    "search_rank": None,
                    "name": f"{model} ({color_name})",
                    "url": clean_pid_url(color_url),
                    "pid": pid,
                    "color": color_name,
                    "ram": None,
                    "storage": None,
                    "label_parsed": False,
                    "search_term": model,
                    "confidence": "high",
                })
            continue

        for combo in combos:
            pid = extract_pid_from_url(combo["url"])
            if not pid:
                continue
            if pid in known_by_pid:
                existing = known_by_pid[pid]
                if not existing.get("ram") and combo["ram"]:
                    existing["ram"] = combo["ram"]
                    backfilled += 1
                    log.info(
                        "%s: backfilled RAM (%s) for already-known %s %s",
                        model, combo["ram"], color_name, combo["storage"],
                    )
                continue
            if pid in seen_pids_this_model:
                continue
            seen_pids_this_model.add(pid)
            new_entries.append({
                "search_rank": None,
                "name": f"{model} ({color_name}, {combo['ram']}, {combo['storage']})",
                "url": clean_pid_url(combo["url"]),
                "pid": pid,
                "color": color_name,
                "ram": combo["ram"],
                "storage": combo["storage"],
                "label_parsed": True,
                "search_term": model,
                "confidence": "high",
            })

    if backfilled:
        log.info("%s: backfilled RAM for %d already-known variant(s)", model, backfilled)

    return new_entries


def main() -> None:
    setup_logging()

    with open(DISCOVERED_VARIANTS_FILE) as f:
        data = json.load(f)

    by_model: dict[str, list[dict]] = {}
    for r in data["results"]:
        by_model.setdefault(r["search_term"], []).append(r)

    models = sorted(by_model.keys())
    log.info("Running completeness audit for %d model(s): %s", len(models), ", ".join(models))

    total_new = 0
    for i, model in enumerate(models):
        new_entries = complete_model_variants(model, by_model[model])
        if new_entries:
            summary = ", ".join(f"{e['color']} {e['ram']}/{e['storage']}" for e in new_entries)
            log.info("%s: found %d new variant(s) search missed -- %s", model, len(new_entries), summary)
            data["results"].extend(new_entries)
            total_new += len(new_entries)
        else:
            log.info("%s: complete, no gaps found (%d already known)", model, len(by_model[model]))

        if i < len(models) - 1:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Always write -- backfilled RAM values (mutated in place on the same
    # dict objects held in data["results"]) can happen even when
    # new_entries is empty for every model, and wouldn't otherwise persist.
    with open(DISCOVERED_VARIANTS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log.info(
        "Done: %d new variant(s) added to %s across %d model(s) (RAM backfills, if any, logged above per model).",
        total_new, DISCOVERED_VARIANTS_FILE, len(models),
    )


if __name__ == "__main__":
    main()
