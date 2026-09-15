"""Loading NaPTAN stops and BODS NeTEx fares into the local database.

The archive is a zip of per-publisher zips of NeTEx XML: roughly 183,000 fare
files across 337 publisher accounts. Parsing all of it takes a while, so the
ingester supports narrowing by publisher folder, by NOC, and by ATCO area, and
records enough statistics that coverage can be reported afterwards without a
second pass.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyproj

from . import netex
from .config import DEFAULT_ARCHIVE, DEFAULT_STOPS_CSV
from .db import get_connection, init_db, reset_area, reset_operators

log = logging.getLogger(__name__)

#: NaPTAN publishes both WGS84 and OSGB36 columns, but a minority of rows only
#: have the grid reference, so we need to convert in both directions.
_OSGB_TO_WGS84 = pyproj.Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
_WGS84_TO_OSGB = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

#: Rows are flushed to SQLite in batches of this size.
BATCH_SIZE = 20_000

#: NaPTAN's ``Status`` column. Recent releases spell these out in full; older
#: ones used three-letter codes, so both are accepted.
ACTIVE_STATUSES = {"active", "act"}


def latlng_to_osgb36(lat: float, lng: float) -> tuple[float, float]:
    """Convert WGS84 degrees to OSGB36 eastings/northings in metres.

    Distances between British bus stops are far easier to reason about in
    metres than in degrees, and Code-Point Open is natively OSGB36.
    """
    easting, northing = _WGS84_TO_OSGB.transform(lng, lat)
    return easting, northing


# --------------------------------------------------------------------------
# NaPTAN
# --------------------------------------------------------------------------

def ingest_stops(db_path=None, csv_path: Path | None = None,
                 active_only: bool = True, bus_only: bool = False) -> int:
    """Load the NaPTAN stop register.

    ``active_only`` drops stops NaPTAN marks as deleted or pending, which
    otherwise depress every coverage statistic. ``bus_only`` further restricts
    to ``BCT`` (on-street bus/coach/tram stops); left off by default because
    some fare zones legitimately reference interchanges and ferry piers.
    """
    csv_path = Path(csv_path or DEFAULT_STOPS_CSV)
    init_db(db_path)
    conn = get_connection(db_path)
    cursor = conn.cursor()

    batch: list[tuple] = []
    written = 0
    skipped_no_coords = 0
    skipped_status = 0

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            atco = (row.get("ATCOCode") or "").strip()
            if not atco:
                continue

            status = (row.get("Status") or "").strip()
            if active_only and status and status.lower() not in ACTIVE_STATUSES:
                skipped_status += 1
                continue

            stop_type = (row.get("StopType") or "").strip()
            if bus_only and stop_type != "BCT":
                continue

            lat_text = (row.get("Latitude") or "").strip()
            lng_text = (row.get("Longitude") or "").strip()
            easting_text = (row.get("Easting") or "").strip()
            northing_text = (row.get("Northing") or "").strip()

            try:
                if lat_text and lng_text:
                    lat, lng = float(lat_text), float(lng_text)
                    if easting_text and northing_text:
                        easting, northing = float(easting_text), float(northing_text)
                    else:
                        easting, northing = latlng_to_osgb36(lat, lng)
                elif easting_text and northing_text:
                    easting, northing = float(easting_text), float(northing_text)
                    lng, lat = _OSGB_TO_WGS84.transform(easting, northing)
                else:
                    skipped_no_coords += 1
                    continue
            except ValueError:
                skipped_no_coords += 1
                continue

            batch.append((atco, lat, lng, easting, northing,
                          (row.get("CommonName") or "").strip(),
                          (row.get("LocalityName") or "").strip(),
                          stop_type, status))

            if len(batch) >= BATCH_SIZE:
                cursor.executemany(
                    "INSERT OR REPLACE INTO stops (atco_code, lat, lng, easting, northing,"
                    " common_name, locality, stop_type, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    batch)
                written += len(batch)
                batch.clear()

    if batch:
        cursor.executemany(
            "INSERT OR REPLACE INTO stops (atco_code, lat, lng, easting, northing,"
            " common_name, locality, stop_type, status) VALUES (?,?,?,?,?,?,?,?,?)",
            batch)
        written += len(batch)

    conn.commit()
    conn.close()
    log.info("loaded %d stops (%d skipped: no coordinates, %d skipped: inactive)",
             written, skipped_no_coords, skipped_status)
    return written


# --------------------------------------------------------------------------
# BODS NeTEx fares
# --------------------------------------------------------------------------

@dataclass
class IngestSummary:
    """Counts returned to the CLI so it can print a useful report."""
    files_seen: int = 0
    files_parsed: int = 0
    files_skipped_filter: int = 0
    operators: set = None
    zone_rows: int = 0
    fare_rows: int = 0
    tier_counts: Counter = None

    def __post_init__(self):
        if self.operators is None:
            self.operators = set()
        if self.tier_counts is None:
            self.tier_counts = Counter()


class FareIngester:
    """Streams a BODS fares archive into the database.

    Parameters
    ----------
    folders:
        Restrict to these publisher account folder names (exact match).
    nocs:
        Restrict to these National Operator Codes. Applied authoritatively
        after parsing. Unless ``scan_all`` is set, a cheap filename substring
        pre-filter is used first to avoid parsing obviously irrelevant files -
        this is a *superset* test, but note that some publishers (Stagecoach in
        particular) do not put the NOC in their filenames, so pass
        ``scan_all=True`` if an expected operator comes back empty.
    atco_prefix:
        Keep only files whose fare zones contain at least one stop in this ATCO
        administrative area, e.g. ``4100`` for Tyne & Wear.
    """

    def __init__(self, db_path=None, folders=None, nocs=None, atco_prefix=None,
                 scan_all: bool = False, include_capped: bool = False,
                 limit: int | None = None):
        self.db_path = db_path
        self.folders = set(folders or [])
        self.nocs = {n.upper() for n in (nocs or [])}
        self.atco_prefix = atco_prefix or ""
        self.scan_all = scan_all
        self.include_capped = include_capped
        self.limit = limit

        self.summary = IngestSummary()
        self._operators: dict[str, netex.Operator] = {}
        self._sources: Counter = Counter()
        self._stats: dict[str, Counter] = defaultdict(Counter)
        self._zone_batch: list[tuple] = []
        self._fare_batch: list[tuple] = []
        self._line_batch: list[tuple] = []

    # -- archive traversal -------------------------------------------------

    def _iter_files(self, archive_path: Path):
        """Yield ``(publisher_folder, filename, xml_bytes)`` for every fare file."""
        with zipfile.ZipFile(archive_path) as master:
            for entry in master.namelist():
                if "/" not in entry:
                    continue
                folder = entry.split("/")[0]
                if self.folders and folder not in self.folders:
                    continue

                try:
                    if entry.endswith(".zip"):
                        nested_bytes = master.read(entry)
                        with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
                            for inner in nested.namelist():
                                if inner.endswith(".xml"):
                                    yield folder, inner, nested.read(inner)
                    elif entry.endswith(".xml"):
                        yield folder, entry, master.read(entry)
                except (zipfile.BadZipFile, OSError) as exc:
                    log.warning("unreadable archive entry %s: %s", entry, exc)

    def _filename_may_match(self, filename: str) -> bool:
        """Cheap pre-filter before the cost of parsing.

        Only ever used to skip work; the NOC is confirmed from the
        ``<Operator>`` element afterwards.
        """
        if not self.nocs or self.scan_all:
            return True
        upper = filename.upper()
        return any(noc in upper for noc in self.nocs)

    # -- row accumulation --------------------------------------------------

    def _flush(self, conn, force: bool = False) -> None:
        if force or len(self._fare_batch) >= BATCH_SIZE:
            if self._fare_batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO fares (noc, line_id, origin_zone,"
                    " destination_zone, product_type, user_type, product_name, price,"
                    " valid_from, valid_to, source_tier, source_detail)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self._fare_batch)
                self._fare_batch.clear()
        if force or len(self._zone_batch) >= BATCH_SIZE:
            if self._zone_batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO zones (noc, zone_id, zone_name, atco_code)"
                    " VALUES (?,?,?,?)", self._zone_batch)
                self._zone_batch.clear()
        if force or len(self._line_batch) >= BATCH_SIZE:
            if self._line_batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO lines (noc, line_id, public_code, line_name,"
                    " description) VALUES (?,?,?,?,?)", self._line_batch)
                self._line_batch.clear()
        if force:
            conn.commit()

    def _accept(self, parsed: netex.ParsedFile) -> bool:
        """Apply the authoritative NOC and ATCO-area filters."""
        noc = parsed.operator.noc.upper()
        if self.nocs and noc not in self.nocs:
            return False
        if self.atco_prefix:
            in_area = any(
                atco.startswith(self.atco_prefix)
                for _, atcos in parsed.zones.values()
                for atco in atcos
            )
            if not in_area:
                return False
        return True

    # -- main entry point --------------------------------------------------

    def run(self, archive_path: Path | None = None) -> IngestSummary:
        archive_path = Path(archive_path or DEFAULT_ARCHIVE)
        init_db(self.db_path)
        conn = get_connection(self.db_path)

        # Wipe only the operators we are about to rewrite, so a targeted
        # re-ingest cannot destroy unrelated data - but do wipe them, because
        # fares are inserted with OR IGNORE and would otherwise keep their old
        # provenance after a parser change.
        if self.nocs:
            reset_operators(conn, sorted(self.nocs))
        elif self.atco_prefix:
            cleared = reset_area(conn, self.atco_prefix)
            if cleared:
                log.info("cleared %d operator(s) previously ingested for %s*",
                         len(cleared), self.atco_prefix)

        for folder, filename, xml_bytes in self._iter_files(archive_path):
            self.summary.files_seen += 1

            if not self._filename_may_match(filename):
                self.summary.files_skipped_filter += 1
                continue

            parsed = netex.parse_file(xml_bytes, include_capped=self.include_capped)
            if parsed is None or parsed.operator is None:
                continue
            if not self._accept(parsed):
                self.summary.files_skipped_filter += 1
                continue

            self._record(conn, folder, parsed)
            self._flush(conn)

            if self.summary.files_parsed % 5000 == 0:
                log.info("%d files parsed, %d fares, %d operators",
                         self.summary.files_parsed, self.summary.fare_rows,
                         len(self.summary.operators))
            if self.limit and self.summary.files_parsed >= self.limit:
                log.info("stopping early at --limit %d", self.limit)
                break

        self._flush(conn, force=True)
        self._write_operators(conn)
        conn.commit()
        conn.close()
        return self.summary

    def _record(self, conn, folder: str, parsed: netex.ParsedFile) -> None:
        noc = parsed.operator.noc.upper()
        self.summary.files_parsed += 1
        self.summary.operators.add(noc)

        # Keep the richest operator record we have seen. Some files omit the
        # trading name, so prefer one that has it.
        existing = self._operators.get(noc)
        if existing is None or (not existing.trading_name and parsed.operator.trading_name):
            self._operators[noc] = parsed.operator
        self._sources[(noc, folder)] += 1

        stats = self._stats[(noc, folder)]
        stats["files"] += 1
        if parsed.zoneless:
            stats["zoneless_files"] += 1
        if parsed.unpriced:
            stats["unpriced_files"] += 1

        for line_id, (public_code, name, description) in parsed.lines.items():
            self._line_batch.append((noc, line_id, public_code, name, description))

        for zone_id, (zone_name, atcos) in parsed.zones.items():
            for atco in set(atcos):
                self._zone_batch.append((noc, zone_id, zone_name, atco))
                stats["zone_rows"] += 1
                self.summary.zone_rows += 1

        for fare in parsed.fares:
            self._fare_batch.append((
                noc, fare.line_id, fare.origin_zone, fare.destination_zone,
                fare.product_type, fare.user_type, fare.product_name, fare.price,
                parsed.valid_from, parsed.valid_to, fare.source_tier, fare.source_detail,
            ))
            stats[f"tier{fare.source_tier}"] += 1
            stats["fare_rows"] += 1
            self.summary.fare_rows += 1
            self.summary.tier_counts[fare.source_tier] += 1

    def _write_operators(self, conn) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO operators (noc, public_code, name, trading_name,"
            " town, postcode) VALUES (?,?,?,?,?,?)",
            [(op.noc.upper(), op.public_code, op.name, op.trading_name, op.town, op.postcode)
             for op in self._operators.values()])
        conn.executemany(
            "INSERT OR REPLACE INTO operator_sources (noc, publisher_folder, file_count)"
            " VALUES (?,?,?)",
            [(noc, folder, count) for (noc, folder), count in self._sources.items()])
        conn.executemany(
            "INSERT OR REPLACE INTO ingest_stats (noc, publisher_folder, files, zone_rows,"
            " fare_rows, tier1, tier2, tier3, tier4, zoneless_files, unpriced_files)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(noc, folder, s["files"], s["zone_rows"], s["fare_rows"],
              s["tier1"], s["tier2"], s["tier3"], s["tier4"],
              s["zoneless_files"], s["unpriced_files"])
             for (noc, folder), s in self._stats.items()])


def discover_operators(archive_path: Path | None = None, sample_per_folder: int = 60,
                       folders=None) -> dict[str, dict]:
    """Map publisher folders to the NOCs they actually publish for.

    Useful before a targeted ingest, because the folder name is a corporate
    account and tells you nothing about which operators are inside it.
    """
    archive_path = Path(archive_path or DEFAULT_ARCHIVE)
    wanted = set(folders or [])
    found: dict[str, dict] = defaultdict(lambda: {"nocs": {}, "files": 0})

    with zipfile.ZipFile(archive_path) as master:
        entries = defaultdict(list)
        for entry in master.namelist():
            if "/" in entry:
                entries[entry.split("/")[0]].append(entry)

        for folder, names in entries.items():
            if wanted and folder not in wanted:
                continue
            seen = 0
            for entry in names:
                if seen >= sample_per_folder:
                    break
                try:
                    if entry.endswith(".zip"):
                        with zipfile.ZipFile(io.BytesIO(master.read(entry))) as nested:
                            for inner in nested.namelist():
                                if seen >= sample_per_folder:
                                    break
                                if not inner.endswith(".xml"):
                                    continue
                                operator = netex.extract_operator(
                                    _root_of(nested.read(inner)))
                                seen += 1
                                if operator:
                                    found[folder]["nocs"][operator.noc] = (
                                        operator.name or operator.trading_name)
                    elif entry.endswith(".xml"):
                        operator = netex.extract_operator(_root_of(master.read(entry)))
                        seen += 1
                        if operator:
                            found[folder]["nocs"][operator.noc] = (
                                operator.name or operator.trading_name)
                except (zipfile.BadZipFile, OSError):
                    continue
            found[folder]["files"] = len(names)
    return dict(found)


def _root_of(xml_bytes: bytes):
    """Parse bytes to an lxml root, returning an empty element on failure."""
    from lxml import etree
    try:
        return etree.parse(io.BytesIO(xml_bytes)).getroot()
    except etree.XMLSyntaxError:
        return etree.Element("empty")
