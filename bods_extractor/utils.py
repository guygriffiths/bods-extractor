import pyproj

# Set up the coordinate transformer once at the module level so it's lightning fast.
# EPSG:4326  = WGS84 (Google Maps Lat/Lng)
# EPSG:27700 = OSGB36 (UK Ordnance Survey Easting/Northing)
_wgs84_to_osgb36 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

def latlng_to_osgb36(lat, lng):
    """Converts Google Maps Lat/Lng into UK Ordnance Survey Easting/Northing."""
    # Note: pyproj expects (x, y) which is (longitude, latitude)
    easting, northing = _wgs84_to_osgb36.transform(lng, lat)
    return easting, northing