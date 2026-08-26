#!/usr/bin/env python3
"""run_pipeline_amazon.py: Amazon India equivalent of run_pipeline.py.

Deliberately a separate entry point, not a step folded into
run_pipeline.py -- Amazon's fetch step needs a real Playwright/Chromium
browser (with xvfb in CI, since the stealth fix that makes the bank-offer
click work requires headless=False -- see fetch_offers_amazon.py), a
fundamentally different runtime from Flipkart's plain-requests approach.
Keeping them separate means a bug or slowdown in the new, less-proven
Amazon path can't take down the Flipkart pipeline that's already running
reliably on its own schedule.

Shares the same `pipeline_runs` concurrency-lock table as Flipkart, but
the lock itself is scoped per marketplace (see the
scope_pipeline_lock_by_marketplace migration) -- an Amazon run and a
Flipkart run no longer block each other, only two overlapping runs of
the *same* marketplace do, which is the actual risk each pipeline needs
to guard against now that they run on independent schedules.

Alerting and notification are reused as-is (both were already
marketplace-agnostic -- `latest_snapshots` and `alerting.run_all()` have
no marketplace filter, so a run from either pipeline evaluates the whole
catalogue, Flipkart and Amazon variants together; harmless redundancy,
not a correctness issue, since alerting's own cooldown/dedup logic is
per-variant).
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from postgrest.exceptions import APIError

import alerting
import fetch_offers_amazon
import notify
import offer_parser_amazon
from pipeline_logging import setup_logging
from supabase_client import get_client

setup_logging()
log = logging.getLogger("run_pipeline_amazon")

MARKETPLACE = "amazon"

EMPTY_FETCH_STATS = {"total": 0, "ok": 0, "no_bank_offers": 0, "failed": 0}
EMPTY_PARSE_STATS = {"snapshots_processed": 0, "offers_total": 0, "unresolved_lines": 0, "failed_snapshots": 0}
EMPTY_ALERT_STATS = {"evaluated": 0, "total": 0, "by_reason": {}, "fired_alerts": []}


def try_start_pipeline_run(supabase) -> tuple[Optional[int], bool]:
    """Same shape/contract as run_pipeline.py's version -- see there for
    the full rationale. Only difference: passes p_marketplace="amazon" so
    the lock (and its self-healing stale-row check) only considers other
    Amazon runs, not Flipkart's."""
    try:
        resp = supabase.rpc("try_start_pipeline_run", {"p_marketplace": MARKETPLACE}).execute()
        run_id = resp.data
        if run_id is None:
            return None, False
        return run_id, True
    except APIError as e:
        log.error("Failed to acquire pipeline run lock in Supabase: %s", e.message)
        return None, True


def record_run_end(
    supabase,
    run_id: Optional[int],
    status: str,
    fetch_stats: dict[str, int],
    parse_stats: dict[str, int],
    alert_stats: dict[str, int],
    error_summary: Optional[str],
) -> None:
    if run_id is None:
        return
    try:
        supabase.table("pipeline_runs").update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "fetch_ok": fetch_stats["ok"],
                "fetch_failed": fetch_stats["failed"],
                "parse_ok": parse_stats["snapshots_processed"] - parse_stats["failed_snapshots"],
                "parse_failed": parse_stats["failed_snapshots"],
                "alerts_fired": alert_stats["total"],
                "error_summary": error_summary,
            }
        ).eq("id", run_id).execute()
    except APIError as e:
        log.error("Failed to record run end in Supabase: %s", e.message)


def main() -> None:
    run_start = time.monotonic()
    started_at = datetime.now(timezone.utc)
    log.info("=== Amazon pipeline run starting at %s ===", started_at.isoformat())

    supabase = get_client()
    run_id, should_proceed = try_start_pipeline_run(supabase)

    if not should_proceed:
        log.warning("Another Amazon pipeline run is already in progress -- exiting without doing any work.")
        return

    errors: list[str] = []

    fetch_step_start = time.monotonic()
    try:
        fetch_stats = fetch_offers_amazon.run_all(supabase)
    except Exception as e:
        log.exception("Fetch step crashed unexpectedly")
        fetch_stats = dict(EMPTY_FETCH_STATS)
        errors.append(f"fetch step crashed: {e}")
    fetch_duration = time.monotonic() - fetch_step_start
    log.info(
        "Fetch step finished in %.1fs: %d/%d succeeded, %d with no bank offers, %d failed",
        fetch_duration, fetch_stats["ok"], fetch_stats["total"], fetch_stats["no_bank_offers"], fetch_stats["failed"],
    )

    parse_step_start = time.monotonic()
    try:
        parse_stats = offer_parser_amazon.run_all(supabase)
    except Exception as e:
        log.exception("Parse step crashed unexpectedly")
        parse_stats = dict(EMPTY_PARSE_STATS)
        errors.append(f"parse step crashed: {e}")
    parse_duration = time.monotonic() - parse_step_start
    log.info(
        "Parse step finished in %.1fs: %d snapshot(s) processed, %d offer(s) structured, %d unresolved line(s), %d snapshot(s) failed",
        parse_duration, parse_stats["snapshots_processed"], parse_stats["offers_total"],
        parse_stats["unresolved_lines"], parse_stats["failed_snapshots"],
    )

    alert_step_start = time.monotonic()
    try:
        alert_stats = alerting.run_all(supabase)
    except Exception as e:
        log.exception("Alerting step crashed unexpectedly")
        alert_stats = dict(EMPTY_ALERT_STATS)
        errors.append(f"alerting step crashed: {e}")
    alert_duration = time.monotonic() - alert_step_start
    log.info(
        "Alerting step finished in %.1fs: %d variant(s) evaluated, %d alert(s) fired (%s)",
        alert_duration, alert_stats["evaluated"], alert_stats["total"],
        ", ".join(f"{k}: {v}" for k, v in sorted(alert_stats["by_reason"].items())) or "none",
    )

    try:
        notify.send_alert_email(alert_stats["fired_alerts"])
    except Exception:
        log.exception("Notification step crashed unexpectedly -- alerts were still recorded normally")

    total_duration = time.monotonic() - run_start
    parse_ok = parse_stats["snapshots_processed"] - parse_stats["failed_snapshots"]

    if errors:
        status = "failed" if fetch_stats["ok"] == 0 and parse_ok == 0 else "partial_failure"
    elif fetch_stats["failed"] > 0 or parse_stats["failed_snapshots"] > 0:
        status = "partial_failure" if (fetch_stats["ok"] > 0 or parse_ok > 0) else "failed"
    else:
        status = "success"

    error_summary = "; ".join(errors) if errors else None

    log.info(
        "=== Amazon pipeline run finished in %.1fs -- status=%s | fetch: %d ok / %d failed | parse: %d ok / %d failed | "
        "alerts: %d fired ===",
        total_duration, status,
        fetch_stats["ok"], fetch_stats["failed"],
        parse_ok, parse_stats["failed_snapshots"],
        alert_stats["total"],
    )

    record_run_end(supabase, run_id, status, fetch_stats, parse_stats, alert_stats, error_summary)


if __name__ == "__main__":
    main()
