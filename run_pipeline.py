#!/usr/bin/env python3
"""run_pipeline.py: Phase 4 -- single entry point that runs the fetch step
then the parse step in sequence, on one shared Supabase client, with
structured logging (see pipeline_logging.py) and a `pipeline_runs` row in
Supabase per run.

The `pipeline_runs` row is what lets the dashboard show whether scheduled
runs are still happening, since this script is meant to be invoked on an
interval by the *host's* scheduler (Render/Railway cron), not by an
in-process scheduler -- see project memory for the tradeoff writeup.

Usage: `python3 run_pipeline.py` -- same for manual runs and scheduled runs.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import anthropic
from postgrest.exceptions import APIError

import alerting
import fetch_offers
import notify
import offer_parser
from pipeline_logging import setup_logging
from supabase_client import get_client

setup_logging()
log = logging.getLogger("run_pipeline")

EMPTY_FETCH_STATS = {"total": 0, "ok": 0, "warned": 0, "failed": 0}
EMPTY_PARSE_STATS = {
    "snapshots_processed": 0,
    "offers_total": 0,
    "low_confidence": 0,
    "non_offers": 0,
    "failed_snapshots": 0,
}
EMPTY_ALERT_STATS = {"evaluated": 0, "total": 0, "by_reason": {}, "fired_alerts": []}


def try_start_pipeline_run(supabase) -> tuple[Optional[int], bool]:
    """Acquires the pipeline's run lock via the try_start_pipeline_run
    Postgres function -- a single RPC call, so the "is one already
    running?" check and the "insert a running row" write happen in one
    transaction server-side, not two separate round trips a second
    process could slip between. Two run_pipeline.py invocations racing on
    the same unparsed snapshots is exactly what corrupted effective_price
    earlier (see project history) -- this closes that gap.

    Returns (run_id, should_proceed):
    - (id, True): lock acquired, this run should proceed normally.
    - (None, True): the RPC call itself failed (Supabase error) -- don't
      block real work over a health-logging failure, same tradeoff as
      before.
    - (None, False): another run is currently active -- must NOT proceed.
    """
    try:
        resp = supabase.rpc("try_start_pipeline_run", {}).execute()
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
    log.info("=== Pipeline run starting at %s ===", started_at.isoformat())

    supabase = get_client()
    run_id, should_proceed = try_start_pipeline_run(supabase)

    if not should_proceed:
        log.warning(
            "Another pipeline run is already in progress -- exiting without doing any work "
            "(this is the guard against the concurrent-run race that previously corrupted "
            "effective_price)."
        )
        return

    errors: list[str] = []

    fetch_step_start = time.monotonic()
    try:
        fetch_stats = fetch_offers.run_all(supabase)
    except Exception as e:
        log.exception("Fetch step crashed unexpectedly")
        fetch_stats = dict(EMPTY_FETCH_STATS)
        errors.append(f"fetch step crashed: {e}")
    fetch_duration = time.monotonic() - fetch_step_start
    log.info(
        "Fetch step finished in %.1fs: %d/%d succeeded, %d label mismatch warning(s), %d failed",
        fetch_duration, fetch_stats["ok"], fetch_stats["total"], fetch_stats["warned"], fetch_stats["failed"],
    )

    parse_step_start = time.monotonic()
    try:
        claude = anthropic.Anthropic()
        parse_stats = offer_parser.run_all(supabase, claude)
    except Exception as e:
        log.exception("Parse step crashed unexpectedly")
        parse_stats = dict(EMPTY_PARSE_STATS)
        errors.append(f"parse step crashed: {e}")
    parse_duration = time.monotonic() - parse_step_start
    log.info(
        "Parse step finished in %.1fs: %d snapshot(s) processed, %d offer(s) structured, %d snapshot(s) failed",
        parse_duration, parse_stats["snapshots_processed"], parse_stats["offers_total"], parse_stats["failed_snapshots"],
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
        "=== Pipeline run finished in %.1fs -- status=%s | fetch: %d ok / %d failed | parse: %d ok / %d failed | "
        "alerts: %d fired ===",
        total_duration, status,
        fetch_stats["ok"], fetch_stats["failed"],
        parse_ok, parse_stats["failed_snapshots"],
        alert_stats["total"],
    )

    record_run_end(supabase, run_id, status, fetch_stats, parse_stats, alert_stats, error_summary)


if __name__ == "__main__":
    main()
