"""CSV export of per-operator fare zones and fare matrices.

Output shape
------------
For each operator (NOC) the exporter writes:

``<NOC>_zones.csv``
    Every fare zone and the stops in it, with names, coordinates and the
    nearest postcode. This is the auditable record: it is what lets someone
    check the export against an operator's published zone map. It is also the
    join table, because zone membership is many-to-many (see below).

``<NOC>_postcodes.csv``
    Truncated postcode -> stop. Note *stop*, not zone: a NeTEx fare zone is
    scoped to a fare product rather than to geography, so a single stop belongs
    to many zones at once and "the zone for this postcode" is not a well-formed
    question. Postcode -> stop is unambiguous, and stop -> zone is supplied by
    the zones file.

``<NOC>_fares.csv``
    Zone-to-zone prices with product type, passenger class, validity and
    provenance tier.

``manifest.csv``
    One row per operator: names, counts, coverage and the proportion of fares
    at each provenance tier.

The lookup path is therefore::

    postcode -> stop        (<NOC>_postcodes.csv)
    stop     -> zone(s)     (<NOC>_zones.csv)
    zone pair -> price      (<NOC>_fares.csv)

Postcode assignment modes
-------------------------
``stops``
    Only truncated postcodes that actually contain a bus stop. Precise, sparse,
    and every row is directly evidenced.

``fill``
    Every Code-Point Open postcode within ``ZONE_FILL_RADIUS_M`` of a stop is
    attached to its nearest stop. Dense, and mirrors the client's existing
    "every postcode in the zone" file, but rows away from a stop are inferred
    rather than observed. The ``assignment`` column records which of the two
    produced each row, and inference never overrides direct evidence.
"""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from pathlib import Path

from .config import EXPORT_DIR, ZONE_FILL_RADIUS_M
from .db import get_connection
from .geo import SpatialGrid
from .netex import ANY_ZONE
from .postcodes import load_codepoint, truncate_postcode

log = logging.getLogger(__name__)

#: A fare set is reported as flat when every price for a given product and
#: passenger class is identical across all zone pairs. Some operators emit one
#: zone per stop and then charge the same fare for every combination, which
#: produces hundreds of thousands of identical rows unless collapsed.
FLAT_FARE_MIN_PAIRS = 25


def _write_csv(path: Path, header: list[str], rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def select_operators(conn, nocs=None, atco_prefix: str = "") -> list[dict]:
    """Choose which operators to export.

    With ``atco_prefix`` this returns every operator with at least one zoned
    stop in that ATCO administrative area, which is how "all the Newcastle
    operators" is expressed without having to know their names in advance.
    """
    if nocs:
        placeholders = ",".join("?" * len(nocs))
        query = (f"SELECT * FROM operators WHERE noc IN ({placeholders})")
        params = [n.upper() for n in nocs]
    elif atco_prefix:
        query = ("SELECT o.* FROM operators o WHERE EXISTS ("
                 " SELECT 1 FROM zones z WHERE z.noc = o.noc AND z.atco_code LIKE ?)")
        params = [atco_prefix + "%"]
    else:
        query = "SELECT * FROM operators"
        params = []
    return [dict(row) for row in conn.execute(query, params)]


def _zone_rows(conn, noc: str):
    """Zone membership joined to stop detail and postcode.

    Deliberately *not* clipped to an ATCO area. ``--atco-prefix`` chooses which
    operators to export; it must not truncate their zone lists, because the
    fare rows reference every zone the operator defines. Clipping Stanley
    Travel to Tyne & Wear, for example, left 3 zones in the zones file and fares
    referencing 28 of them - rows the client could not resolve.
    """
    return conn.execute(
        """
        SELECT z.zone_id, z.zone_name, z.atco_code,
               s.common_name, s.locality, s.lat, s.lng, s.easting, s.northing,
               p.postcode, p.postcode_trunc, p.distance_m
        FROM zones z
        LEFT JOIN stops s ON s.atco_code = z.atco_code
        LEFT JOIN stop_postcodes p ON p.atco_code = z.atco_code
        WHERE z.noc = ?
        ORDER BY z.zone_id, z.atco_code
        """, (noc,)).fetchall()


def _fare_rows(conn, noc: str, user_types=None, product_types=None, max_tier: int = 4):
    clauses = ["f.noc = ?", "f.source_tier <= ?"]
    params: list = [noc, max_tier]
    if user_types:
        clauses.append("f.user_type IN (%s)" % ",".join("?" * len(user_types)))
        params += list(user_types)
    if product_types:
        clauses.append("f.product_type IN (%s)" % ",".join("?" * len(product_types)))
        params += list(product_types)
    # The zone-name lookups must yield at most one row per (noc, zone_id).
    # A zone id can carry more than one spelling of its name across files, and
    # joining on a plain DISTINCT would multiply every fare row by the number
    # of spellings. MIN() collapses them deterministically.
    return conn.execute(
        f"""
        SELECT f.*, zo.zone_name AS origin_zone_name, zd.zone_name AS destination_zone_name
        FROM fares f
        LEFT JOIN (SELECT noc, zone_id, MIN(zone_name) AS zone_name
                     FROM zones GROUP BY noc, zone_id) zo
               ON zo.noc = f.noc AND zo.zone_id = f.origin_zone
        LEFT JOIN (SELECT noc, zone_id, MIN(zone_name) AS zone_name
                     FROM zones GROUP BY noc, zone_id) zd
               ON zd.noc = f.noc AND zd.zone_id = f.destination_zone
        WHERE {' AND '.join(clauses)}
        ORDER BY f.product_type, f.user_type, f.origin_zone, f.destination_zone
        """, params).fetchall()


def _detect_flat(rows) -> dict[tuple[str, str], float]:
    """Return {(product_type, user_type): price} for sets priced identically.

    Reading Buses is the canonical example: a zone per stop pair, then a single
    flat price for every combination.
    """
    grouped = defaultdict(set)
    pair_counts = Counter()
    for row in rows:
        key = (row["product_type"], row["user_type"])
        grouped[key].add(row["price"])
        pair_counts[key] += 1
    return {
        key: next(iter(prices))
        for key, prices in grouped.items()
        if len(prices) == 1 and pair_counts[key] >= FLAT_FARE_MIN_PAIRS
    }


def _postcode_stops(zone_rows, mode: str, codepoint_path, radius_m: float):
    """Map truncated postcodes to the stops they can reach.

    **Why stops and not zones.** A NeTEx fare zone belongs to a *fare product*,
    not to geography. Every product an operator sells defines its own zone set,
    so one stop belongs to many zones at once - a Go North East stop averages 26
    and reaches 47. Some of those zones are single fare stages (``fs@11110``,
    "St Marys Place"); others are network-wide (``fs@1DDiscoveryAD@Network``,
    covering the entire operator). Asking "which zone is this postcode in?"
    therefore has no single answer, and any file that gave one would be picking
    arbitrarily between a fare stage and a network pass.

    A stop, by contrast, is stable: it has one location and one ATCO code. So
    the postcode file resolves postcode -> stop, and the fare file is joined via
    the zones file, which records the full stop-to-zone membership. That join is
    many-to-many and is exactly what the fare data means.

    Returns a list of ``(postcode_trunc, atco_code, stop_name, locality,
    distance_m, assignment)`` rows.
    """
    stops: dict[str, dict] = {}
    for row in zone_rows:
        stops.setdefault(row["atco_code"], row)

    # (postcode, stop) -> (distance, how). A truncated postcode legitimately
    # covers several stops; all of them are valid boarding points, so all are
    # kept and the client can take whichever is nearest or cheapest.
    pairs: dict[tuple[str, str], tuple[float, str]] = {}
    direct: set[str] = set()

    for atco, row in stops.items():
        trunc = row["postcode_trunc"]
        if trunc:
            pairs[(trunc, atco)] = (row["distance_m"], "stop")
            direct.add(trunc)

    if mode == "fill":
        # Inferred evidence: attach nearby postcodes to their closest stop, so
        # that a lookup does not fail merely because no stop shares the
        # postcode. Never applied to a postcode that already has a stop at it.
        grid = SpatialGrid(500.0)
        eastings, northings = [], []
        for atco, row in stops.items():
            if row["easting"] is None:
                continue
            grid.add(row["easting"], row["northing"], atco)
            eastings.append(row["easting"])
            northings.append(row["northing"])
        if grid.count:
            bbox = (min(eastings) - radius_m, min(northings) - radius_m,
                    max(eastings) + radius_m, max(northings) + radius_m)
            for easting, northing, postcode in load_codepoint(codepoint_path, bbox=bbox):
                trunc = truncate_postcode(postcode)
                if trunc in direct:
                    continue
                atco, distance = grid.nearest(easting, northing, radius_m)
                if atco is None:
                    continue
                existing = pairs.get((trunc, atco))
                if existing is None or distance < existing[0]:
                    pairs[(trunc, atco)] = (distance, "fill")

    rows = []
    for (trunc, atco), (distance, how) in pairs.items():
        stop = stops[atco]
        rows.append([trunc, atco, stop["common_name"], stop["locality"],
                     None if distance is None else round(distance), how])
    rows.sort(key=lambda r: (r[0], r[4] if r[4] is not None else 1e9))
    return rows


def export_operator(conn, operator: dict, out_dir: Path, mode: str,
                    codepoint_path, radius_m: float,
                    user_types, product_types, max_tier: int) -> dict:
    """Write the CSV set for one operator and return its manifest row."""
    noc = operator["noc"]
    zone_rows = _zone_rows(conn, noc)
    fare_rows = _fare_rows(conn, noc, user_types, product_types, max_tier)

    # ---- zones ---------------------------------------------------------
    _write_csv(out_dir / f"{noc}_zones.csv",
               ["zone_id", "zone_name", "atco_code", "stop_name", "locality",
                "latitude", "longitude", "postcode", "postcode_trunc",
                "postcode_distance_m"],
               ([r["zone_id"], r["zone_name"], r["atco_code"], r["common_name"],
                 r["locality"], r["lat"], r["lng"], r["postcode"],
                 r["postcode_trunc"], r["distance_m"]] for r in zone_rows))

    # ---- postcodes -----------------------------------------------------
    postcode_rows = _postcode_stops(zone_rows, mode, codepoint_path, radius_m)
    _write_csv(out_dir / f"{noc}_postcodes.csv",
               ["postcode_trunc", "atco_code", "stop_name", "locality",
                "distance_m", "assignment"],
               postcode_rows)

    # ---- fares ---------------------------------------------------------
    fare_header = ["origin_zone", "origin_zone_name", "destination_zone",
                   "destination_zone_name", "product_type", "user_type",
                   "product_name", "price", "line_id", "valid_from", "valid_to",
                   "source_tier", "source_detail"]

    flat = _detect_flat(fare_rows)
    all_products = {(r["product_type"], r["user_type"]) for r in fare_rows}
    fully_flat = bool(flat) and set(flat) == all_products

    if fully_flat:
        # Every product this operator sells is priced the same regardless of
        # journey, so the zone matrix carries no information. Emit one row per
        # product instead of the full cross-product of zones.
        fare_body = ([ANY_ZONE, "(flat fare: applies to all journeys)", ANY_ZONE, "",
                      product_type, user_type, "", price, "", "", "", 1,
                      "collapsed: every zone pair priced identically"]
                     for (product_type, user_type), price in sorted(flat.items()))
    else:
        fare_body = ([r["origin_zone"], r["origin_zone_name"], r["destination_zone"],
                      r["destination_zone_name"], r["product_type"], r["user_type"],
                      r["product_name"], r["price"], r["line_id"], r["valid_from"],
                      r["valid_to"], r["source_tier"], r["source_detail"]]
                     for r in fare_rows)

    written_fares = _write_csv(out_dir / f"{noc}_fares.csv", fare_header, fare_body)

    # ---- manifest row --------------------------------------------------
    tiers = Counter(r["source_tier"] for r in fare_rows)
    total = sum(tiers.values()) or 1
    sources = conn.execute(
        "SELECT GROUP_CONCAT(publisher_folder, ' | ') FROM operator_sources"
        " WHERE noc = ?", (noc,)).fetchone()[0] or ""
    zone_count = len({r["zone_id"] for r in zone_rows})
    stop_count = len({r["atco_code"] for r in zone_rows})
    postcodes = {r[0] for r in postcode_rows}
    return {
        "noc": noc,
        "operator_name": operator.get("name") or "",
        "trading_name": operator.get("trading_name") or "",
        "bods_publisher_account": sources,
        "zones": zone_count,
        "stops": stop_count,
        # How many zones an average stop belongs to. Above ~1 this confirms
        # that zones are product-scoped, which is why postcodes map to stops.
        "zones_per_stop": round(len(zone_rows) / stop_count, 1) if stop_count else 0,
        "stops_with_postcode": sum(1 for r in zone_rows if r["postcode_trunc"]),
        "postcodes": len(postcodes),
        "postcode_stop_rows": len(postcode_rows),
        "fare_rows": written_fares,
        "flat_fare_products": ("; ".join(f"{p}/{u}=£{v:.2f}"
                                        for (p, u), v in sorted(flat.items()))
                               if fully_flat else ""),
        "tier1_pct": round(100 * tiers[1] / total, 1),
        "tier2_pct": round(100 * tiers[2] / total, 1),
        "tier3_pct": round(100 * tiers[3] / total, 1),
        "tier4_pct": round(100 * tiers[4] / total, 1),
    }


def export(db_path=None, out_dir: Path | None = None, nocs=None,
           atco_prefix: str = "", mode: str = "stops",
           codepoint_path=None, radius_m: float = ZONE_FILL_RADIUS_M,
           user_types=None, product_types=None, max_tier: int = 4) -> Path:
    """Export CSVs for the selected operators and write a manifest.

    Returns the output directory.
    """
    out_dir = Path(out_dir or EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path, read_only=True)

    operators = select_operators(conn, nocs, atco_prefix)
    if not operators:
        log.warning("no operators matched; has the archive been ingested?")
    log.info("exporting %d operator(s) to %s", len(operators), out_dir)

    manifest = []
    for operator in operators:
        row = export_operator(conn, operator, out_dir, mode,
                              codepoint_path, radius_m, user_types,
                              product_types, max_tier)
        manifest.append(row)
        log.info("  %-6s %-34s zones=%-5d fares=%-7d postcodes=%-6d tier1=%.0f%%",
                 row["noc"], (row["operator_name"] or "")[:34], row["zones"],
                 row["fare_rows"], row["postcodes"], row["tier1_pct"])

    if manifest:
        header = list(manifest[0].keys())
        _write_csv(out_dir / "manifest.csv", header,
                   ([row[key] for key in header]
                    for row in sorted(manifest, key=lambda r: -r["fare_rows"])))
    conn.close()
    return out_dir
