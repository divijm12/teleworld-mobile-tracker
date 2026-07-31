#!/usr/bin/env python3
"""alerting.py: decides whether a variant's latest price/stock status is
worth notifying about, and records that decision -- a rule-based decision,
not an LLM one. Every check here reduces to comparing numbers already
stored (today's effective_price vs. historical low, vs. the last thing we
alerted about, vs. the previous snapshot's stock status), which is
deterministic arithmetic. An LLM would add cost, latency, and
non-determinism (the same inputs could get different verdicts run to run)
to a decision that should be reproducible and explainable.

Two independent signals, combined into one of five reasons:

1. Back in stock ("back_in_stock*") -- fires whenever the *previous*
   snapshot for a variant was confirmed out of stock and the current one
   is confirmed in stock. Always fires, no cooldown or price threshold --
   restocking is inherently a one-time, actionable event regardless of
   price (explicit user requirement: alert regardless).

2. Price-worthy ("new_low" / "price_drop") -- same two rules as before:
   a new/tied all-time-low (cooldown-throttled, see below), or a
   meaningful non-record drop vs. the last alerted price (self-throttling,
   no timer needed).

When both fire at once (a variant restocks *and* its price also happens
to be a new low or meaningful drop), they're reported as a single combined
reason ("back_in_stock_new_low" / "back_in_stock_price_drop") rather than
two separate alerts for the same event.

New-low cooldown: throttled so a price sitting unchanged at its record for
days doesn't re-alert every pipeline run. The cooldown yields immediately
if the price improves *further* during the window, and expires on its own
if the record is re-hit after a long gap (e.g. it was the low in January,
rose for months, and dips back to that same number in July -- that's
worth telling someone about again, not suppressed forever just because it
technically ties an old alert).

Only ever fires when the variant is *currently* in_stock; a great price on
something unbuyable isn't actionable.

No delivery mechanism (email/SMS/push) exists yet -- this module only
decides and records the decision (in the `alerts` table); an external
notification channel is a separate, not-yet-built step.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from postgrest.exceptions import APIError

from pipeline_logging import setup_logging
from supabase_client import get_client

log = logging.getLogger("alerting")

# Postgres trims trailing zeros from fractional seconds (e.g. ".90642"),
# which datetime.fromisoformat rejects on Python <3.11 unless padded to
# exactly 3 or 6 digits -- same fix as dashboard.py's parse_supabase_timestamp.
_FRACTION_RE = re.compile(r"\.(\d+)")


def _parse_timestamp(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    value = _FRACTION_RE.sub(lambda m: "." + m.group(1).ljust(6, "0")[:6], value, count=1)
    return datetime.fromisoformat(value)


COOLDOWN_HOURS = 12
DROP_PCT_THRESHOLD = 5.0
MIN_DROP_RUPEES = 300.0


def _price_worthy_reason(
    effective_price: float,
    historical_low_price: float,
    last_alert: Optional[dict],
    now: datetime,
) -> Optional[str]:
    """The price-only half of the decision: "new_low", "price_drop", or
    None. Shared by both the restocked and not-restocked paths so the same
    thresholds apply either way."""
    is_new_low = effective_price <= historical_low_price

    if is_new_low:
        if last_alert is None:
            return "new_low"
        hours_since_last = (now - last_alert["created_at"]).total_seconds() / 3600
        if hours_since_last >= COOLDOWN_HOURS or effective_price < last_alert["effective_price"]:
            return "new_low"
        return None  # same record we already told them about, recently

    if last_alert is None or not last_alert.get("effective_price"):
        return None  # not a record, and nothing to compare a "drop" against yet

    reference_price = last_alert["effective_price"]
    price_drop = reference_price - effective_price
    price_drop_pct = price_drop / reference_price * 100
    if price_drop_pct >= DROP_PCT_THRESHOLD and price_drop >= MIN_DROP_RUPEES:
        return "price_drop"

    return None


def decide_alert(
    effective_price: Optional[float],
    in_stock: Optional[bool],
    previous_in_stock: Optional[bool],
    historical_low_price: Optional[float],
    last_alert: Optional[dict],
    now: datetime,
) -> Optional[str]:
    """Returns one of "back_in_stock", "back_in_stock_new_low",
    "back_in_stock_price_drop", "new_low", "price_drop", or None. Pure
    function of already-computed values -- no I/O, so it's trivially
    testable and auditable (same inputs always give the same verdict,
    unlike an LLM call)."""
    if not in_stock:
        return None

    # Strictly a False->True transition -- previous_in_stock is None when
    # there's no prior snapshot yet (brand new variant) or it predates
    # stock tracking, neither of which is confidently "just restocked".
    just_restocked = previous_in_stock is False

    price_reason = None
    if effective_price is not None and historical_low_price is not None:
        price_reason = _price_worthy_reason(effective_price, historical_low_price, last_alert, now)

    if just_restocked:
        if price_reason == "new_low":
            return "back_in_stock_new_low"
        if price_reason == "price_drop":
            return "back_in_stock_price_drop"
        return "back_in_stock"

    return price_reason


def fetch_latest_snapshots(supabase) -> list[dict]:
    resp = supabase.table("latest_snapshots").select("*").execute()
    return resp.data


def fetch_previous_in_stock(supabase) -> dict[int, Optional[bool]]:
    resp = supabase.table("previous_snapshots").select("variant_id, in_stock").execute()
    return {row["variant_id"]: row["in_stock"] for row in resp.data}


def fetch_historical_lows(supabase) -> dict[int, float]:
    resp = supabase.table("variant_historical_low").select("*").execute()
    return {row["variant_id"]: row["historical_low_price"] for row in resp.data}


def fetch_last_alerts(supabase) -> dict[int, dict]:
    resp = supabase.table("latest_alerts").select("*").execute()
    result = {}
    for row in resp.data:
        row["created_at"] = _parse_timestamp(row["created_at"])
        result[row["variant_id"]] = row
    return result


def run_all(supabase) -> dict:
    """Evaluates every variant's latest snapshot and records any alerts
    that fire. Returns summary counts for the caller (run_pipeline.py) to
    log/report."""
    now = datetime.now(timezone.utc)
    empty_stats = {"evaluated": 0, "total": 0, "by_reason": {}}

    try:
        snapshots = fetch_latest_snapshots(supabase)
        previous_in_stock = fetch_previous_in_stock(supabase)
        historical_lows = fetch_historical_lows(supabase)
        last_alerts = fetch_last_alerts(supabase)
    except APIError as e:
        log.error("Failed to query alerting inputs from Supabase: %s", e.message)
        return empty_stats

    by_reason: dict[str, int] = {}
    rows_to_insert = []

    for snap in snapshots:
        variant_id = snap["variant_id"]
        effective_price = snap.get("effective_price")
        historical_low_price = historical_lows.get(variant_id)
        last_alert = last_alerts.get(variant_id)

        reason = decide_alert(
            effective_price=effective_price,
            in_stock=snap.get("in_stock"),
            previous_in_stock=previous_in_stock.get(variant_id),
            historical_low_price=historical_low_price,
            last_alert=last_alert,
            now=now,
        )
        if reason is None:
            continue

        by_reason[reason] = by_reason.get(reason, 0) + 1

        label = f"{snap['model']} ({snap['storage']}, {snap['color']}, {snap.get('ram') or '?'})"
        log.info(
            "%s: ALERT (%s) -- effective price %s%s",
            label, reason, effective_price,
            f", down from {last_alert['effective_price']}" if last_alert else "",
        )

        rows_to_insert.append(
            {
                "variant_id": variant_id,
                "snapshot_id": snap["snapshot_id"],
                "reason": reason,
                "effective_price": effective_price,
                "reference_price": last_alert["effective_price"] if last_alert else None,
            }
        )

    if rows_to_insert:
        try:
            supabase.table("alerts").insert(rows_to_insert).execute()
        except APIError as e:
            log.error("Failed to write alert rows to Supabase: %s", e.message)

    total = sum(by_reason.values())
    log.info(
        "%d variant(s) evaluated -- %d alert(s) fired (%s).",
        len(snapshots), total,
        ", ".join(f"{k}: {v}" for k, v in sorted(by_reason.items())) or "none",
    )
    return {"evaluated": len(snapshots), "total": total, "by_reason": by_reason}


def main() -> None:
    setup_logging()
    supabase = get_client()
    run_all(supabase)


if __name__ == "__main__":
    main()
