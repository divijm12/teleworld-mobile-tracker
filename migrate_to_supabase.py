#!/usr/bin/env python3
"""migrate_to_supabase.py: One-time backfill of structured_offers.json
(Phase 1+2 output) into Supabase.

Each JSON entry becomes: one variants row (upserted by pid), one
fetch_snapshots row, and one structured_offers row per parsed offer, linked
to that snapshot. Since this JSON already represents completed Phase 1+2
output, every migrated snapshot is marked parsed_at = its own timestamp.

Run once, after the tables exist and SUPABASE_URL/SUPABASE_KEY are set.
"""

import json
import logging
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from postgrest.exceptions import APIError

from supabase_client import get_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_to_supabase")

INPUT_FILE = "structured_offers.json"
MARKETPLACE = "flipkart"


def extract_pid_from_url(url: str) -> Optional[str]:
    query = parse_qs(urlparse(url).query)
    values = query.get("pid")
    return values[0] if values else None


def derive_brand(model: str) -> str:
    return model.split()[0] if model.split() else model


def migrate_entry(supabase, entry: dict[str, Any]) -> tuple[int, int]:
    """Returns (offers_migrated, 0) on success; raises on Supabase failure
    so the caller can log and skip to the next entry."""
    label = f"{entry['model']} ({entry['storage']}, {entry['color']})"

    pid = extract_pid_from_url(entry["url"])
    if not pid:
        raise ValueError(f"could not extract pid from url: {entry['url']}")

    variant_resp = (
        supabase.table("variants")
        .upsert(
            {
                "pid": pid,
                "model": entry["model"],
                "storage": entry["storage"],
                "color": entry["color"],
                "brand": derive_brand(entry["model"]),
                "marketplace": MARKETPLACE,
            },
            on_conflict="pid",
        )
        .execute()
    )
    variant_id = variant_resp.data[0]["id"]

    snapshot_resp = (
        supabase.table("fetch_snapshots")
        .insert(
            {
                "variant_id": variant_id,
                "price": entry.get("price"),
                "fetched_at": entry["timestamp"],
                "match_warning": entry.get("match_warning"),
                "fetch_error": entry.get("error"),
                "raw_offer_text": entry.get("offer_text_raw"),
                # This JSON is already fully parsed (Phase 2 already ran) --
                # mark it parsed immediately so offer_parser.py doesn't
                # re-process it.
                "parsed_at": entry["timestamp"],
            }
        )
        .execute()
    )
    snapshot_id = snapshot_resp.data[0]["id"]

    structured_offers = entry.get("structured_offers") or []
    rows = [
        {
            "snapshot_id": snapshot_id,
            "raw_text": o["raw_text"],
            "bank": o.get("bank"),
            "card_type": o.get("card_type"),
            "discount_type": o.get("discount_type"),
            "discount_amount": o.get("discount_amount"),
            "discount_unit": o.get("discount_unit"),
            "min_purchase_value": o.get("min_purchase_value"),
            "conditions_raw": o.get("conditions_raw"),
            "confidence": o["confidence"],
            "is_offer": o["is_offer"],
        }
        for o in structured_offers
    ]
    if rows:
        supabase.table("structured_offers").insert(rows).execute()

    log.info("%s: migrated (variant_id=%s, snapshot_id=%s, %d offer(s))",
              label, variant_id, snapshot_id, len(rows))
    return len(rows), 0


def main() -> None:
    with open(INPUT_FILE) as f:
        entries = json.load(f)

    supabase = get_client()

    variants_migrated = 0
    snapshots_migrated = 0
    offers_migrated = 0
    failed = 0

    for entry in entries:
        label = f"{entry['model']} ({entry['storage']}, {entry['color']})"
        try:
            n_offers, _ = migrate_entry(supabase, entry)
        except (APIError, ValueError) as e:
            msg = e.message if isinstance(e, APIError) else str(e)
            log.error("%s: migration failed, skipping -- %s", label, msg)
            failed += 1
            continue

        variants_migrated += 1
        snapshots_migrated += 1
        offers_migrated += n_offers

    log.info(
        "Done: %d variant(s), %d snapshot(s), %d offer(s) migrated, %d entry(ies) failed.",
        variants_migrated, snapshots_migrated, offers_migrated, failed,
    )


if __name__ == "__main__":
    main()
