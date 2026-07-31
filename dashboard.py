#!/usr/bin/env python3
"""dashboard.py: local Flask dashboard for the deal-monitoring pipeline.

Single page showing, per variant, its most recent fetch (price + last
checked) and structured offers, sourced from Supabase via the
`latest_snapshots` view. Local tool only -- no auth.
"""

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from flask import Flask, render_template

from pricing import compute_best_offer
from supabase_client import get_client

app = Flask(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Postgres trims trailing zeros from the fractional-seconds part, so
# Supabase can return e.g. ".90642" (5 digits) -- datetime.fromisoformat
# requires exactly 3 or 6 digits on Python <3.11. Pad to 6 before parsing.
_FRACTION_RE = re.compile(r"\.(\d+)")


def parse_supabase_timestamp(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    value = _FRACTION_RE.sub(lambda m: "." + m.group(1).ljust(6, "0")[:6], value, count=1)
    return datetime.fromisoformat(value)


def format_timestamp(value: str) -> str:
    if not value:
        return "never"
    dt = parse_supabase_timestamp(value)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


CARD_TYPE_LABELS = {"credit": "Credit Card", "debit": "Debit Card", "both": "Credit/Debit Card"}


def format_offer_summary(offer: dict) -> str:
    """"Flipkart Axis Bank (Credit Card) -- 10% off, min purchase Rs.1500"
    style one-liner. Explicitly names credit vs debit rather than leaving it
    implied by the amount -- confirmed live: the same bank (e.g. "Flipkart
    Axis Bank") shows up as both a credit-card offer and a separate,
    differently-priced debit-card offer, and without this label those two
    entries were indistinguishable except by memorizing which amount was
    which."""
    label = offer.get("bank") or ("EMI" if offer.get("card_type") == "EMI" else "Other")

    card_type_label = CARD_TYPE_LABELS.get(offer.get("card_type"))
    if card_type_label:
        label = f"{label} ({card_type_label})"

    amount = offer.get("discount_amount")
    unit = offer.get("discount_unit") or ""
    discount_type = offer.get("discount_type")
    if amount is None:
        amount_str = "amount unclear"
    elif discount_type == "no_cost_emi":
        amount_str = "No Cost EMI"
    elif unit == "%":
        amount_str = f"{amount:g}% off"
    else:
        amount_str = f"{unit}{amount:,.0f} off" if unit else f"{amount:,.0f} off"

    summary = f"{label} — {amount_str}"
    if offer.get("min_purchase_value"):
        summary += f", min {offer['min_purchase_value']:,.0f}"
    return summary


app.jinja_env.filters["timestamp"] = format_timestamp
app.jinja_env.filters["offer_summary"] = format_offer_summary

STALE_RUN_THRESHOLD_MINUTES = 150  # matches the 2-hour GH Actions schedule + buffer


def get_pipeline_health() -> Optional[dict]:
    """Most recent pipeline_runs row, plus whether it counts as healthy --
    either the last completed run wasn't a clean success, or too much time
    has passed since it started (run_pipeline.py fires every 2 hours via
    GitHub Actions -- see .github/workflows/pipeline.yml -- so a longer
    gap means the scheduler itself has likely stopped firing)."""
    supabase = get_client()
    resp = (
        supabase.table("pipeline_runs")
        .select("*")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None

    run = resp.data[0]
    started_at = parse_supabase_timestamp(run["started_at"])
    age_minutes = (datetime.now(timezone.utc) - started_at).total_seconds() / 60

    run["is_stale"] = run["status"] != "success" or age_minutes > STALE_RUN_THRESHOLD_MINUTES
    return run


def get_dashboard_data() -> list[dict]:
    """One entry per variant: latest snapshot fields + its structured offers."""
    supabase = get_client()

    latest = supabase.table("latest_snapshots").select("*").execute().data
    if not latest:
        return []

    snapshot_ids = [row["snapshot_id"] for row in latest]
    offers_resp = (
        supabase.table("structured_offers")
        .select("*")
        .in_("snapshot_id", snapshot_ids)
        .execute()
    )
    offers_by_snapshot: dict[int, list] = defaultdict(list)
    for offer in offers_resp.data:
        offers_by_snapshot[offer["snapshot_id"]].append(offer)

    # variant_historical_low is a DB view (DISTINCT ON over fetch_snapshots,
    # indexed on (variant_id, effective_price, fetched_at)) -- the all-time
    # low is looked up, not recomputed here, so this stays cheap no matter
    # how much history accumulates.
    variant_ids = [row["variant_id"] for row in latest]
    historical_resp = (
        supabase.table("variant_historical_low")
        .select("*")
        .in_("variant_id", variant_ids)
        .execute()
    )
    historical_by_variant = {row["variant_id"]: row for row in historical_resp.data}

    # latest_alerts is the most recent alert *ever* fired for a variant --
    # only show the badge when that alert was actually triggered by the
    # snapshot being displayed right now, not a stale alert from days ago
    # that a since-changed price has already moved past.
    alerts_resp = (
        supabase.table("latest_alerts")
        .select("*")
        .in_("variant_id", variant_ids)
        .execute()
    )
    latest_alert_by_variant = {row["variant_id"]: row for row in alerts_resp.data}

    variants = []
    for row in latest:
        entry = dict(row)
        entry["offers"] = offers_by_snapshot.get(row["snapshot_id"], [])
        entry["has_flagged_offer"] = any(
            not o["is_offer"] or o["confidence"] == "low" for o in entry["offers"]
        )
        # Pre-format here rather than in JS -- reuses the same tested Python
        # logic instead of duplicating date/offer-text formatting client-side.
        entry["fetched_at_display"] = format_timestamp(entry["fetched_at"])
        for o in entry["offers"]:
            o["summary"] = format_offer_summary(o)

        best_offer, effective_price = compute_best_offer(entry["offers"], entry["price"])
        entry["best_offer_summary"] = best_offer["summary"] if best_offer else None
        entry["effective_price"] = round(effective_price) if effective_price is not None else None

        # Strict rule (explicit user choice): only a new/tied all-time-low
        # counts as "best price yet" -- anything else shows the historical
        # low and the most recent date it was hit, rather than a fuzzy
        # "close enough" judgment call.
        historical = historical_by_variant.get(entry["variant_id"])
        if effective_price is not None and historical is not None:
            entry["historical_low_price"] = round(historical["historical_low_price"])
            entry["historical_low_at_display"] = format_timestamp(historical["historical_low_at"])
            entry["is_new_low"] = effective_price <= historical["historical_low_price"]
        else:
            entry["historical_low_price"] = None
            entry["historical_low_at_display"] = None
            entry["is_new_low"] = None

        latest_alert = latest_alert_by_variant.get(entry["variant_id"])
        is_current_alert = latest_alert and latest_alert["snapshot_id"] == entry["snapshot_id"]
        entry["alert_reason"] = latest_alert["reason"] if is_current_alert else None
        entry["alert_reference_price"] = latest_alert["reference_price"] if is_current_alert else None
        entry["alert_created_at_display"] = (
            format_timestamp(latest_alert["created_at"]) if is_current_alert else None
        )

        variants.append(entry)

    variants.sort(key=lambda v: ((v["brand"] or "").lower(), v["model"], v["ram"] or "", v["storage"]))
    return variants


@app.route("/")
def index():
    variants = get_dashboard_data()
    return render_template(
        "index.html",
        variants=variants,
        total_variants=len(variants),
        flagged_count=sum(1 for v in variants if v["has_flagged_offer"]),
        out_of_stock_count=sum(1 for v in variants if v["in_stock"] is False),
        alert_count=sum(1 for v in variants if v["alert_reason"]),
        pipeline_run=get_pipeline_health(),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
