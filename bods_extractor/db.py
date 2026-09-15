"""SQLite schema and connection handling.

Design notes
------------
*Operators are keyed on NOC*, the National Operator Code carried by every
NeTEx file as ``<Operator id="noc:XXXX">``. The previous schema keyed on the
BODS *publisher account* folder name, which is a commercial grouping rather
than an operator: the "Go-Ahead Group plc_10" account alone publishes for 24
distinct NOCs, so Carousel Buses in High Wycombe appeared as "Go-Ahead". The
``operator_sources`` table preserves that folder name for traceability.

*Every fare row records its provenance.* ``source_tier`` says how confidently
the price was derived, from 1 (read straight out of a canonical NeTEx element)
to 4 (inferred by the legacy heuristics). This lets us ship data with an
honest confidence signal instead of pretending all rows are equal, and makes
"this fare looks wrong" answerable in one query.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DEFAULT_DB

SCHEMA = """
-- ---------------------------------------------------------------- reference
-- NaPTAN stop register. Coordinates are stored in both WGS84 (for Google-style
-- lat/lng input) and OSGB36 eastings/northings (for metre-accurate matching
-- against Code-Point Open, which is natively OSGB36).
CREATE TABLE IF NOT EXISTS stops (
    atco_code    TEXT PRIMARY KEY,
    lat          REAL NOT NULL,
    lng          REAL NOT NULL,
    easting      REAL,
    northing     REAL,
    common_name  TEXT,
    locality     TEXT,
    stop_type    TEXT,
    status       TEXT
);

-- Nearest Code-Point Open postcode to each stop. `postcode_trunc` is the unit
-- postcode with its final character removed, matching the convention used by
-- the client's hand-compiled fare-zone spreadsheet.
CREATE TABLE IF NOT EXISTS stop_postcodes (
    atco_code       TEXT PRIMARY KEY,
    postcode        TEXT NOT NULL,
    postcode_trunc  TEXT NOT NULL,
    distance_m      REAL NOT NULL
);

-- ---------------------------------------------------------------- operators
CREATE TABLE IF NOT EXISTS operators (
    noc            TEXT PRIMARY KEY,
    public_code    TEXT,
    name           TEXT,
    trading_name   TEXT,
    town           TEXT,
    postcode       TEXT
);

-- Which BODS publisher account a NOC's data arrived under. Many-to-many
-- because group accounts publish for many operators, and a few operators
-- appear under more than one account.
CREATE TABLE IF NOT EXISTS operator_sources (
    noc               TEXT NOT NULL,
    publisher_folder  TEXT NOT NULL,
    file_count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (noc, publisher_folder)
);

CREATE TABLE IF NOT EXISTS lines (
    noc          TEXT NOT NULL,
    line_id      TEXT NOT NULL,
    public_code  TEXT,
    line_name    TEXT,
    description  TEXT,
    PRIMARY KEY (noc, line_id)
);

-- ------------------------------------------------------------------- fares
-- A fare zone is a named set of stops. Note that "zone" is used loosely by
-- operators: some draw real geographic zones, others emit one zone per stop
-- and then price every pair identically (an effective flat fare).
CREATE TABLE IF NOT EXISTS zones (
    noc        TEXT NOT NULL,
    zone_id    TEXT NOT NULL,
    zone_name  TEXT,
    atco_code  TEXT NOT NULL,
    PRIMARY KEY (noc, zone_id, atco_code)
);

CREATE TABLE IF NOT EXISTS fares (
    noc              TEXT NOT NULL,
    line_id          TEXT NOT NULL DEFAULT '',
    origin_zone      TEXT NOT NULL,
    destination_zone TEXT NOT NULL,
    product_type     TEXT NOT NULL,   -- single | day_return | period | carnet | unknown
    user_type        TEXT NOT NULL,   -- adult | child | senior | student | ...
    product_name     TEXT,
    price            REAL NOT NULL,
    valid_from       TEXT,
    valid_to         TEXT,
    source_tier      INTEGER NOT NULL,
    source_detail    TEXT,
    PRIMARY KEY (noc, line_id, origin_zone, destination_zone,
                 product_type, user_type, product_name, price)
);

-- Per-operator record of which extraction tiers fired, so coverage and
-- confidence can be reported without re-parsing the archive.
CREATE TABLE IF NOT EXISTS ingest_stats (
    noc               TEXT NOT NULL,
    publisher_folder  TEXT NOT NULL,
    files             INTEGER NOT NULL DEFAULT 0,
    zone_rows         INTEGER NOT NULL DEFAULT 0,
    fare_rows         INTEGER NOT NULL DEFAULT 0,
    tier1             INTEGER NOT NULL DEFAULT 0,
    tier2             INTEGER NOT NULL DEFAULT 0,
    tier3             INTEGER NOT NULL DEFAULT 0,
    tier4             INTEGER NOT NULL DEFAULT 0,
    zoneless_files    INTEGER NOT NULL DEFAULT 0,
    unpriced_files    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (noc, publisher_folder)
);

-- ----------------------------------------------------------------- indexes
CREATE INDEX IF NOT EXISTS idx_stops_location   ON stops (lat, lng);
CREATE INDEX IF NOT EXISTS idx_stops_grid       ON stops (easting, northing);
CREATE INDEX IF NOT EXISTS idx_zones_atco       ON zones (atco_code);
CREATE INDEX IF NOT EXISTS idx_zones_noc_zone   ON zones (noc, zone_id);
CREATE INDEX IF NOT EXISTS idx_fares_noc        ON fares (noc);
CREATE INDEX IF NOT EXISTS idx_fares_od         ON fares (noc, origin_zone, destination_zone);
CREATE INDEX IF NOT EXISTS idx_postcode_trunc   ON stop_postcodes (postcode_trunc);
"""


def get_connection(db_path: Path | str | None = None,
                   read_only: bool = False) -> sqlite3.Connection:
    """Open the fares database.

    ``read_only`` opens via a URI so a consumer cannot accidentally write to a
    database that an ingest is building.
    """
    path = Path(db_path or DEFAULT_DB)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        # WAL plus a relaxed sync makes bulk-inserting tens of millions of rows
        # roughly an order of magnitude faster. The database is a rebuildable
        # artefact, so durability across a power cut is not a concern.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create any missing tables and indexes. Never drops existing data."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


def reset_operators(conn: sqlite3.Connection, nocs: list[str]) -> None:
    """Remove all data for the given NOCs so they can be re-ingested cleanly.

    Scoped deletion matters: the previous implementation unconditionally
    dropped the whole fare table on every run, so ingesting a single operator
    silently destroyed every other operator's fares.
    """
    if not nocs:
        return
    placeholders = ",".join("?" * len(nocs))
    for table in ("fares", "zones", "lines", "operator_sources",
                  "ingest_stats", "operators"):
        conn.execute(f"DELETE FROM {table} WHERE noc IN ({placeholders})", nocs)
    conn.commit()


def reset_area(conn: sqlite3.Connection, atco_prefix: str) -> list[str]:
    """Remove every operator that has a fare zone in an ATCO area.

    Needed because ``fares`` is written with ``INSERT OR IGNORE`` and
    ``source_tier`` is deliberately *not* part of the primary key - two rows
    that agree on operator, zones, product, passenger class and price are the
    same fare regardless of how it was derived. Without clearing first, a
    re-ingest after a parser improvement would silently keep the old, weaker
    provenance and the improvement would be invisible.

    Returns the NOCs that were cleared.
    """
    if not atco_prefix:
        return []
    nocs = [row[0] for row in conn.execute(
        "SELECT DISTINCT noc FROM zones WHERE atco_code LIKE ?",
        (atco_prefix + "%",))]
    reset_operators(conn, nocs)
    return nocs
