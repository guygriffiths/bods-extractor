"""Geometry helpers: grid-based nearest-neighbour search and GeoPackage points.

All spatial matching in this package is done in OSGB36 eastings/northings
(EPSG:27700) rather than degrees. Britain's national grid is metric, so a
distance is just Pythagoras, and Code-Point Open is published in it natively.
Only the public API accepts WGS84 lat/lng, because that is what mapping
services hand you.
"""
from __future__ import annotations

import math
import struct
from collections import defaultdict

import pyproj

_WGS84_TO_OSGB = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
_OSGB_TO_WGS84 = pyproj.Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def to_osgb36(lat: float, lng: float) -> tuple[float, float]:
    """WGS84 degrees -> OSGB36 easting/northing in metres."""
    return _WGS84_TO_OSGB.transform(lng, lat)


def to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """OSGB36 easting/northing -> (lat, lng) in degrees."""
    lng, lat = _OSGB_TO_WGS84.transform(easting, northing)
    return lat, lng


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres. Used only where grid coords are absent."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class SpatialGrid:
    """Uniform-bucket nearest-neighbour index over OSGB36 points.

    A full scan of 1.7 million Code-Point postcodes per stop would be
    hopeless, and the previous implementation's degree-bounding-box scan of
    the whole stop table was the main reason lookups were slow. Bucketing by
    a fixed cell size reduces each query to a handful of nearby cells.
    """

    def __init__(self, cell_size_m: float = 1000.0):
        self.cell_size = cell_size_m
        self._cells: dict[tuple[int, int], list] = defaultdict(list)
        self.count = 0

    def _key(self, easting: float, northing: float) -> tuple[int, int]:
        return (int(easting // self.cell_size), int(northing // self.cell_size))

    def add(self, easting: float, northing: float, payload) -> None:
        self._cells[self._key(easting, northing)].append((easting, northing, payload))
        self.count += 1

    def nearest(self, easting: float, northing: float, radius_m: float):
        """Return ``(payload, distance_m)`` for the closest point, or ``(None, inf)``.

        Searches outward ring by ring and stops as soon as the best candidate
        found is closer than the nearest possible point in the next ring, so
        the result is exact rather than approximate.
        """
        cx, cy = self._key(easting, northing)
        max_rings = int(radius_m // self.cell_size) + 1
        best_payload, best_distance = None, float("inf")

        for ring in range(max_rings + 1):
            # Everything in this ring is at least this far away.
            ring_floor = (ring - 1) * self.cell_size
            if best_payload is not None and best_distance <= ring_floor:
                break

            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    # Only the outer shell is new on each iteration.
                    if ring and max(abs(dx), abs(dy)) != ring:
                        continue
                    for px, py, payload in self._cells.get((cx + dx, cy + dy), ()):
                        distance = math.hypot(px - easting, py - northing)
                        if distance < best_distance:
                            best_distance, best_payload = distance, payload

        if best_distance > radius_m:
            return None, float("inf")
        return best_payload, best_distance

    def __iter__(self):
        """Yield every ``(easting, northing, payload)`` added to the grid."""
        for cell in self._cells.values():
            yield from cell


# --------------------------------------------------------------------------
# GeoPackage geometry
# --------------------------------------------------------------------------

#: Bytes taken by the optional envelope, indexed by the 3-bit envelope code in
#: the GeoPackage binary header flags.
_ENVELOPE_SIZES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def decode_gpkg_point(blob: bytes) -> tuple[float, float] | None:
    """Extract (x, y) from a GeoPackage POINT blob.

    A GeoPackage geometry is a small binary header followed by standard WKB.
    Decoding it directly avoids a GDAL/Fiona dependency for what is a dozen
    bytes of struct unpacking.
    """
    if not blob or len(blob) < 8 or blob[0:2] != b"GP":
        return None
    flags = blob[3]
    envelope_size = _ENVELOPE_SIZES.get((flags >> 1) & 0x07)
    if envelope_size is None:
        return None
    offset = 8 + envelope_size
    if len(blob) < offset + 21:
        return None
    byte_order = "<" if blob[offset] == 1 else ">"
    geometry_type = struct.unpack_from(byte_order + "I", blob, offset + 1)[0]
    if geometry_type & 0xFF != 1:  # not a POINT
        return None
    x, y = struct.unpack_from(byte_order + "dd", blob, offset + 5)
    return x, y
