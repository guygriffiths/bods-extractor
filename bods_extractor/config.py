"""Filesystem locations and tunable constants.

Every path can be overridden with an environment variable so the package works
unchanged in a checkout, a container, or the client's own deployment. Nothing
here reaches the network.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Repository root (the directory containing the ``bods_extractor`` package).
ROOT = Path(__file__).resolve().parent.parent

#: Where large third-party inputs live. Symlink ``data`` elsewhere if needed.
DATA_DIR = Path(os.environ.get("BODS_DATA_DIR", ROOT / "data"))

RAW_DIR = DATA_DIR / "raw"
OUTPUT_DIR = DATA_DIR / "output"
EXPORT_DIR = Path(os.environ.get("BODS_EXPORT_DIR", DATA_DIR / "export"))

#: The BODS "fares" bulk download (a zip of per-publisher zips of NeTEx XML).
DEFAULT_ARCHIVE = Path(os.environ.get(
    "BODS_ARCHIVE", RAW_DIR / "bodds_fares_archive_20260803.zip"))

#: NaPTAN national stop register, CSV flavour.
DEFAULT_STOPS_CSV = Path(os.environ.get("BODS_STOPS_CSV", RAW_DIR / "Stops.csv"))

#: Ordnance Survey Code-Point Open, GeoPackage flavour.
DEFAULT_CODEPOINT = Path(os.environ.get("BODS_CODEPOINT", RAW_DIR / "codepo_gb.gpkg"))

#: The database this package builds and reads.
DEFAULT_DB = Path(os.environ.get("BODS_DB", OUTPUT_DIR / "fares.db"))

#: Maximum distance a NaPTAN stop may be from a Code-Point postcode centroid
#: before we refuse to associate the two.
POSTCODE_MATCH_RADIUS_M = float(os.environ.get("BODS_POSTCODE_RADIUS_M", 500))

#: In ``fill`` export mode, how far a postcode may be from the nearest
#: fare-zone stop before it is left unassigned.
ZONE_FILL_RADIUS_M = float(os.environ.get("BODS_ZONE_FILL_RADIUS_M", 1000))

#: Radius used when snapping an arbitrary lat/lng to a bus stop.
STOP_MATCH_RADIUS_M = float(os.environ.get("BODS_STOP_RADIUS_M", 1500))

#: ATCO area prefixes worth naming. ATCO codes begin with a 3-digit
#: administrative area code, zero-padded to 4 characters in NaPTAN.
ATCO_AREAS = {
    "4100": "Tyne & Wear (Newcastle, Gateshead, Sunderland, North/South Tyneside)",
    "0900": "Reading / Berkshire",
    "0400": "Buckinghamshire (High Wycombe)",
}
