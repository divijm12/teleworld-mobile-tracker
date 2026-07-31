#!/usr/bin/env python3
"""pipeline_logging.py: shared structured-logging setup for the pipeline
scripts. Writes timestamped records to both stdout and a log file so a run
on a remote host (no debugger attached) can be diagnosed from the log file
alone.

Idempotent -- safe to call from multiple modules. Whichever caller runs
first wins; later calls are no-ops (checked via root.handlers), so
fetch_offers.py and offer_parser.py can still configure logging when run
standalone, or defer to run_pipeline.py's setup when run as part of it.

Timestamps are always IST, regardless of the host machine's own timezone
(Python logging's default %(asctime)s uses local system time, which is
whatever zone the deploy host happens to be set to -- usually UTC, and
not necessarily consistent across local dev vs Render/Railway).
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_FILE = Path(__file__).parent / "pipeline.log"
IST = ZoneInfo("Asia/Kolkata")


class ISTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=IST)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


def setup_logging(log_file: Path = LOG_FILE, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(level)
    fmt = ISTFormatter(
        "%(asctime)s IST [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
