#!/usr/bin/env python3
"""db.py: SQLite storage layer for the deal-monitoring pipeline.

Replaces the flat offers.json / structured_offers.json files with a proper
history-capable store:

  variants   -- one row per (model, storage, color) SKU, keyed by Flipkart's
                stable `pid`. Created/updated by fetch_offers.py.
  snapshots  -- one row per fetch_offers.py run for a variant: price, raw
                offer text, and whether it's been parsed yet.
  offers     -- structured offers for a snapshot, written by offer_parser.py.

fetch_offers.py writes variants + snapshots (raw_offer_text, parsed_at=NULL).
offer_parser.py finds snapshots with parsed_at IS NULL, structures their
raw_offer_text via Claude, writes rows to `offers`, and marks the snapshot
parsed. The dashboard reads the latest snapshot + offers per variant.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(__file__).parent / "deal_check.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS variants (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    pid      TEXT NOT NULL UNIQUE,   -- Flipkart's variant SKU id (stable identifier)
    model    TEXT NOT NULL,
    storage  TEXT NOT NULL,
    color    TEXT NOT NULL,
    url      TEXT                     -- most recently seen URL for this pid
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id      INTEGER NOT NULL REFERENCES variants(id),
    fetched_at      TEXT NOT NULL,    -- ISO 8601 UTC
    price           INTEGER,
    fetch_error     TEXT,             -- Phase 1 request/parse error, if any
    match_warning   TEXT,             -- Phase 1 label cross-check mismatch, if any
    raw_offer_text  TEXT,             -- Phase 1's raw bank/card offer block
    parsed_at       TEXT              -- set by Phase 2 once structured; NULL = pending
);

CREATE TABLE IF NOT EXISTS offers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id         INTEGER NOT NULL REFERENCES snapshots(id),
    raw_text            TEXT NOT NULL,
    bank                TEXT,
    card_type           TEXT,
    discount_type       TEXT,
    discount_amount     REAL,
    discount_unit       TEXT,
    min_purchase_value  REAL,
    conditions_raw      TEXT,
    confidence          TEXT NOT NULL,
    is_offer            INTEGER NOT NULL  -- 0/1
);

CREATE INDEX IF NOT EXISTS idx_snapshots_variant_time
    ON snapshots(variant_id, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_unparsed
    ON snapshots(parsed_at) WHERE parsed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_offers_snapshot
    ON offers(snapshot_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def upsert_variant(conn: sqlite3.Connection, *, pid: str, model: str, storage: str, color: str, url: str) -> int:
    """Find-or-create a variant by its stable `pid`; keeps `url` fresh."""
    conn.execute(
        """
        INSERT INTO variants (pid, model, storage, color, url)
        VALUES (:pid, :model, :storage, :color, :url)
        ON CONFLICT(pid) DO UPDATE SET url = excluded.url
        """,
        {"pid": pid, "model": model, "storage": storage, "color": color, "url": url},
    )
    row = conn.execute("SELECT id FROM variants WHERE pid = ?", (pid,)).fetchone()
    return row["id"]


def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    variant_id: int,
    fetched_at: str,
    price: Optional[int],
    fetch_error: Optional[str] = None,
    match_warning: Optional[str] = None,
    raw_offer_text: Optional[str] = None,
    parsed_at: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO snapshots (variant_id, fetched_at, price, fetch_error, match_warning, raw_offer_text, parsed_at)
        VALUES (:variant_id, :fetched_at, :price, :fetch_error, :match_warning, :raw_offer_text, :parsed_at)
        """,
        {
            "variant_id": variant_id,
            "fetched_at": fetched_at,
            "price": price,
            "fetch_error": fetch_error,
            "match_warning": match_warning,
            "raw_offer_text": raw_offer_text,
            "parsed_at": parsed_at,
        },
    )
    return cur.lastrowid


def get_unparsed_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, v.model, v.storage, v.color
        FROM snapshots s
        JOIN variants v ON v.id = s.variant_id
        WHERE s.parsed_at IS NULL AND s.raw_offer_text IS NOT NULL
        ORDER BY s.fetched_at
        """
    ).fetchall()


def mark_snapshot_parsed(conn: sqlite3.Connection, snapshot_id: int, parsed_at: str) -> None:
    conn.execute("UPDATE snapshots SET parsed_at = ? WHERE id = ?", (parsed_at, snapshot_id))


def insert_offer(conn: sqlite3.Connection, *, snapshot_id: int, raw_text: str, bank: Optional[str],
                  card_type: Optional[str], discount_type: Optional[str], discount_amount: Optional[float],
                  discount_unit: Optional[str], min_purchase_value: Optional[float],
                  conditions_raw: Optional[str], confidence: str, is_offer: bool) -> int:
    cur = conn.execute(
        """
        INSERT INTO offers (snapshot_id, raw_text, bank, card_type, discount_type, discount_amount,
                             discount_unit, min_purchase_value, conditions_raw, confidence, is_offer)
        VALUES (:snapshot_id, :raw_text, :bank, :card_type, :discount_type, :discount_amount,
                :discount_unit, :min_purchase_value, :conditions_raw, :confidence, :is_offer)
        """,
        {
            "snapshot_id": snapshot_id,
            "raw_text": raw_text,
            "bank": bank,
            "card_type": card_type,
            "discount_type": discount_type,
            "discount_amount": discount_amount,
            "discount_unit": discount_unit,
            "min_purchase_value": min_purchase_value,
            "conditions_raw": conditions_raw,
            "confidence": confidence,
            "is_offer": int(is_offer),
        },
    )
    return cur.lastrowid


def get_latest_snapshots_with_offers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per variant: its most recent snapshot, plus that snapshot's
    structured offers. Used by the dashboard."""
    latest = conn.execute(
        """
        SELECT v.id AS variant_id, v.model, v.storage, v.color, v.pid, v.url,
               s.id AS snapshot_id, s.price, s.fetched_at, s.fetch_error, s.match_warning
        FROM variants v
        JOIN snapshots s ON s.variant_id = v.id
        WHERE s.id = (
            SELECT s2.id FROM snapshots s2
            WHERE s2.variant_id = v.id
            ORDER BY s2.fetched_at DESC, s2.id DESC
            LIMIT 1
        )
        ORDER BY v.model, v.storage
        """
    ).fetchall()

    results = []
    for row in latest:
        offers = conn.execute(
            "SELECT * FROM offers WHERE snapshot_id = ? ORDER BY id",
            (row["snapshot_id"],),
        ).fetchall()
        entry = dict(row)
        entry["offers"] = [dict(o) for o in offers]
        results.append(entry)
    return results
