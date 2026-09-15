"""The deliverable: stop-to-stop fare lookup against the local database.

This module is the product. It has no network access, no Google dependency and
no printing, so it can be embedded in someone else's service. Everything it
needs is in the SQLite file built by :mod:`bods_extractor.ingest`.

The demonstration front-end that calls Google Directions lives separately in
:mod:`bods_extractor.directions` and :mod:`bods_extractor.lookup`, and is not
required to use any of this.

Typical use::

    from bods_extractor.fares import FareLookup

    lookup = FareLookup("data/output/fares.db")
    origin = lookup.nearest_stop(54.9738, -1.6131)
    destination = lookup.nearest_stop(54.9520, -1.6010)
    for quote in lookup.fares_between(origin.atco_code, destination.atco_code):
        print(quote.operator_name, quote.product_type, quote.price)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import STOP_MATCH_RADIUS_M
from .db import get_connection
from .geo import to_osgb36

#: Order in which product types are offered when the caller expresses no
#: preference. A commuter almost always wants the cheapest way to make one
#: trip, so singles come first.
DEFAULT_PRODUCT_PREFERENCE = ("single", "day_return", "period", "carnet")

#: Sentinel used in the ``fares`` table for a network-wide (flat) fare.
ANY_ZONE = "ANY"


@dataclass(frozen=True)
class Stop:
    atco_code: str
    lat: float
    lng: float
    common_name: str
    locality: str
    distance_m: float = 0.0


@dataclass(frozen=True)
class Operator:
    noc: str
    name: str
    trading_name: str
    publisher_folder: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.trading_name or self.noc


@dataclass(frozen=True)
class FareQuote:
    """One priced option for a journey.

    ``source_tier`` is how the price was derived: 1 means it was read straight
    out of the canonical NeTEx element, 4 means it was inferred by fallback
    heuristics. ``zone_match`` distinguishes a genuine point-to-point fare from
    a network-wide flat fare that happens to apply.
    """
    noc: str
    operator_name: str
    product_type: str
    user_type: str
    product_name: str
    price: float
    line_id: str
    origin_zone: str
    destination_zone: str
    zone_match: str          # "point_to_point" | "flat_fare"
    source_tier: int
    source_detail: str
    valid_from: str = ""
    valid_to: str = ""


class FareLookup:
    """Read-only query interface over the fares database."""

    def __init__(self, db_path: Path | str | None = None):
        self.conn = get_connection(db_path, read_only=True)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- stops -------------------------------------------------------------

    def get_stop(self, atco_code: str) -> Stop | None:
        row = self.conn.execute(
            "SELECT atco_code, lat, lng, common_name, locality FROM stops"
            " WHERE atco_code = ?", (atco_code,)).fetchone()
        return Stop(*row) if row else None

    def nearest_stop(self, lat: float, lng: float,
                     radius_m: float = STOP_MATCH_RADIUS_M) -> Stop | None:
        """Snap an arbitrary coordinate to the closest bus stop.

        Uses the OSGB36 grid columns and the ``idx_stops_grid`` index, so this
        is a bounded index scan rather than a scan of the whole stop table.
        """
        easting, northing = to_osgb36(lat, lng)
        rows = self.conn.execute(
            "SELECT atco_code, lat, lng, common_name, locality, easting, northing"
            " FROM stops WHERE easting BETWEEN ? AND ? AND northing BETWEEN ? AND ?",
            (easting - radius_m, easting + radius_m,
             northing - radius_m, northing + radius_m)).fetchall()

        best, best_distance = None, float("inf")
        for row in rows:
            distance = ((row["easting"] - easting) ** 2
                        + (row["northing"] - northing) ** 2) ** 0.5
            if distance < best_distance and distance <= radius_m:
                best_distance, best = distance, row
        if best is None:
            return None
        return Stop(best["atco_code"], best["lat"], best["lng"],
                    best["common_name"], best["locality"], round(best_distance, 1))

    def stops_for_postcode(self, postcode_trunc: str) -> list[Stop]:
        """All stops whose nearest postcode truncates to this value."""
        rows = self.conn.execute(
            "SELECT s.atco_code, s.lat, s.lng, s.common_name, s.locality, p.distance_m"
            " FROM stop_postcodes p JOIN stops s ON s.atco_code = p.atco_code"
            " WHERE p.postcode_trunc = ?", (postcode_trunc.upper(),)).fetchall()
        return [Stop(*row) for row in rows]

    # -- operators ---------------------------------------------------------

    def get_operator(self, noc: str) -> Operator | None:
        row = self.conn.execute(
            "SELECT noc, name, trading_name FROM operators WHERE noc = ?",
            (noc.upper(),)).fetchone()
        return Operator(row["noc"], row["name"], row["trading_name"]) if row else None

    def resolve_operator(self, agency_name: str) -> list[Operator]:
        """Match a free-text operator name to one or more NOCs.

        Mapping a routing engine's agency string onto BODS data is the single
        most error-prone step in the whole pipeline, because BODS publishes
        under corporate accounts. Matching against ``name``, ``trading_name``
        and ``public_code`` recovers, for example, "Carousel Buses" from data
        uploaded by Go-Ahead.

        Returns every plausible match rather than picking one, so the caller
        can decide how to handle ambiguity.
        """
        needle = " ".join(agency_name.lower().split())
        if not needle:
            return []
        matches = []
        for row in self.conn.execute(
                "SELECT noc, name, trading_name FROM operators"):
            candidates = [row["name"] or "", row["trading_name"] or "", row["noc"]]
            for candidate in candidates:
                lowered = candidate.lower()
                if lowered and (lowered == needle or lowered in needle or needle in lowered):
                    matches.append(Operator(row["noc"], row["name"], row["trading_name"]))
                    break
        # Prefer the longest name overlap: "Go North East" should beat "Go".
        matches.sort(key=lambda op: -len(op.display_name))
        return matches

    def operators_serving(self, atco_code: str) -> list[str]:
        """NOCs that place this stop in one of their fare zones."""
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT noc FROM zones WHERE atco_code = ?", (atco_code,))]

    def operators_for_journey(self, origin_atco: str, destination_atco: str) -> list[str]:
        """NOCs whose zones contain both stops, i.e. could carry the whole leg."""
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT a.noc FROM zones a JOIN zones b ON a.noc = b.noc"
            " WHERE a.atco_code = ? AND b.atco_code = ?",
            (origin_atco, destination_atco))]

    # -- fares -------------------------------------------------------------

    def fares_between(self, origin_atco: str, destination_atco: str,
                      noc: str | None = None, user_type: str = "adult",
                      product_types: tuple[str, ...] = DEFAULT_PRODUCT_PREFERENCE,
                      include_flat_fares: bool = True,
                      max_tier: int = 4) -> list[FareQuote]:
        """Every fare that covers travel between two stops.

        Results are ordered by product preference, then by provenance tier
        (most trustworthy first), then by price. The caller picks; this method
        deliberately does not decide what "the" fare is, because that depends
        on whether you want the cheapest option, a specific ticket type, or a
        like-for-like comparison with another data source.
        """
        params = [origin_atco, destination_atco, user_type, max_tier]
        operator_clause = ""
        if noc:
            operator_clause = " AND f.noc = ?"
            params.append(noc.upper())

        # Point-to-point: both stops must sit in the zones the fare joins.
        rows = list(self.conn.execute(
            f"""
            SELECT DISTINCT f.*, o.name AS operator_name
            FROM fares f
            JOIN zones z1 ON z1.noc = f.noc AND z1.zone_id = f.origin_zone
            JOIN zones z2 ON z2.noc = f.noc AND z2.zone_id = f.destination_zone
            LEFT JOIN operators o ON o.noc = f.noc
            WHERE z1.atco_code = ? AND z2.atco_code = ?
              AND f.user_type = ? AND f.source_tier <= ?{operator_clause}
            """, params))
        quotes = [self._to_quote(row, "point_to_point") for row in rows]

        if include_flat_fares:
            # Network-wide fares: valid for any journey the operator runs, so
            # only the origin stop needs to be in their network.
            flat_params = [origin_atco, ANY_ZONE, ANY_ZONE, user_type, max_tier]
            flat_clause = ""
            if noc:
                flat_clause = " AND f.noc = ?"
                flat_params.append(noc.upper())
            flat_rows = list(self.conn.execute(
                f"""
                SELECT DISTINCT f.*, o.name AS operator_name
                FROM fares f
                JOIN zones z ON z.noc = f.noc
                LEFT JOIN operators o ON o.noc = f.noc
                WHERE z.atco_code = ? AND f.origin_zone = ? AND f.destination_zone = ?
                  AND f.user_type = ? AND f.source_tier <= ?{flat_clause}
                """, flat_params))
            quotes.extend(self._to_quote(row, "flat_fare") for row in flat_rows)

        preference = {name: index for index, name in enumerate(product_types)}
        quotes = [q for q in quotes if q.product_type in preference]
        quotes.sort(key=lambda q: (preference[q.product_type], q.source_tier, q.price))
        return quotes

    def cheapest_fare(self, origin_atco: str, destination_atco: str,
                      **kwargs) -> FareQuote | None:
        """The single best-evidenced cheapest option, or ``None``."""
        quotes = self.fares_between(origin_atco, destination_atco, **kwargs)
        return quotes[0] if quotes else None

    @staticmethod
    def _to_quote(row, zone_match: str) -> FareQuote:
        return FareQuote(
            noc=row["noc"],
            operator_name=row["operator_name"] or row["noc"],
            product_type=row["product_type"],
            user_type=row["user_type"],
            product_name=row["product_name"] or "",
            price=row["price"],
            line_id=row["line_id"] or "",
            origin_zone=row["origin_zone"],
            destination_zone=row["destination_zone"],
            zone_match=zone_match,
            source_tier=row["source_tier"],
            source_detail=row["source_detail"] or "",
            valid_from=row["valid_from"] or "",
            valid_to=row["valid_to"] or "",
        )

    # -- reporting ---------------------------------------------------------

    def coverage(self, atco_prefix: str = "") -> dict:
        """Headline numbers for how much of an area the data actually covers."""
        clause = " WHERE atco_code LIKE ?" if atco_prefix else ""
        params = (atco_prefix + "%",) if atco_prefix else ()
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM stops{clause}", params).fetchone()[0]
        covered = self.conn.execute(
            f"SELECT COUNT(DISTINCT atco_code) FROM zones{clause}", params).fetchone()[0]
        return {
            "atco_prefix": atco_prefix or "GB",
            "stops": total,
            "stops_in_a_fare_zone": covered,
            "coverage_pct": round(100 * covered / total, 1) if total else 0.0,
        }
