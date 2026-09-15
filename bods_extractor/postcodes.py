"""Associating bus stops with postcodes, using Ordnance Survey Code-Point Open.

Why this exists
---------------
The client's existing pipeline does not look stops up directly. For each leg of
a journey it reads the postcode, strips the final character, and looks that
truncated postcode up in a hand-compiled spreadsheet mapping postcodes to an
operator's fare zones. To produce a drop-in replacement for that spreadsheet we
need the same key: a truncated postcode.

Code-Point Open gives a coordinate for all 1.75 million GB unit postcodes, so
we can derive the mapping mechanically instead of tracing zone maps by hand.

Truncation convention
---------------------
``"NE1 4XD"`` becomes ``"NE1 4X"``. This is not a standard geography - it sits
between a postcode sector (``NE1 4``) and a unit (``NE1 4XD``) - but it is the
convention the client's file already uses, so we reproduce it exactly.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .config import DEFAULT_CODEPOINT, POSTCODE_MATCH_RADIUS_M
from .db import get_connection, init_db
from .geo import SpatialGrid, decode_gpkg_point

log = logging.getLogger(__name__)

#: Margin added around the bounding box of the stops of interest when loading
#: postcodes, so a stop near the edge can still match a postcode just outside.
BBOX_MARGIN_M = 5_000.0


def normalise_postcode(postcode: str) -> str:
    """Uppercase, single-spaced form, e.g. ``"ne1  4xd"`` -> ``"NE1 4XD"``."""
    return " ".join(postcode.upper().split())


def truncate_postcode(postcode: str) -> str:
    """Drop the final character, matching the client's spreadsheet convention."""
    normalised = normalise_postcode(postcode)
    return normalised[:-1].strip() if len(normalised) > 1 else normalised


def load_codepoint(codepoint_path: Path | None = None,
                   bbox: tuple[float, float, float, float] | None = None,
                   cell_size_m: float = 1000.0) -> SpatialGrid:
    """Build a spatial index of Code-Point Open postcodes.

    ``bbox`` is ``(min_easting, min_northing, max_easting, max_northing)``.
    Supplying it for a regional job keeps memory to a few megabytes instead of
    holding all 1.75 million postcodes at once.
    """
    path = Path(codepoint_path or DEFAULT_CODEPOINT)
    grid = SpatialGrid(cell_size_m)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    skipped = 0
    for postcode, blob in conn.execute("SELECT postcode, geometry FROM codepoint"):
        point = decode_gpkg_point(blob)
        if point is None:
            skipped += 1
            continue
        easting, northing = point
        if bbox and not (bbox[0] <= easting <= bbox[2] and bbox[1] <= northing <= bbox[3]):
            continue
        grid.add(easting, northing, normalise_postcode(postcode))
    conn.close()

    log.info("indexed %d postcodes%s (%d undecodable)", grid.count,
             " within bounding box" if bbox else "", skipped)
    return grid


def _stops_bbox(conn, atco_prefix: str) -> tuple[float, float, float, float] | None:
    row = conn.execute(
        "SELECT MIN(easting), MIN(northing), MAX(easting), MAX(northing) FROM stops"
        " WHERE easting IS NOT NULL" + (" AND atco_code LIKE ?" if atco_prefix else ""),
        (atco_prefix + "%",) if atco_prefix else (),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return (row[0] - BBOX_MARGIN_M, row[1] - BBOX_MARGIN_M,
            row[2] + BBOX_MARGIN_M, row[3] + BBOX_MARGIN_M)


def ingest_stop_postcodes(db_path=None, codepoint_path: Path | None = None,
                          atco_prefix: str = "",
                          radius_m: float = POSTCODE_MATCH_RADIUS_M) -> int:
    """Populate ``stop_postcodes`` with the nearest postcode to every stop.

    Stops further than ``radius_m`` from any postcode centroid are left
    unmapped rather than given a misleading match; rural stops on a road with
    no addresses are the usual cause.
    """
    init_db(db_path)
    conn = get_connection(db_path)

    bbox = _stops_bbox(conn, atco_prefix)
    if bbox is None:
        log.warning("no stops with grid references; run ingest-stops first")
        conn.close()
        return 0

    grid = load_codepoint(codepoint_path, bbox=bbox)

    query = ("SELECT atco_code, easting, northing FROM stops"
             " WHERE easting IS NOT NULL AND northing IS NOT NULL")
    params: tuple = ()
    if atco_prefix:
        query += " AND atco_code LIKE ?"
        params = (atco_prefix + "%",)

    rows, unmatched = [], 0
    for atco, easting, northing in conn.execute(query, params):
        postcode, distance = grid.nearest(easting, northing, radius_m)
        if postcode is None:
            unmatched += 1
            continue
        rows.append((atco, postcode, truncate_postcode(postcode), round(distance, 1)))

    conn.executemany(
        "INSERT OR REPLACE INTO stop_postcodes (atco_code, postcode, postcode_trunc,"
        " distance_m) VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()

    log.info("mapped %d stops to postcodes (%d beyond %.0fm radius)",
             len(rows), unmatched, radius_m)
    return len(rows)
