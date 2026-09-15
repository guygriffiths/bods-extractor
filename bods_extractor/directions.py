"""Google Directions adapter. DEMONSTRATION ONLY - not part of the deliverable.

The fares database is routing-engine agnostic: it answers "what does it cost to
travel from stop A to stop B". Something has to decide that A and B are on the
same journey, and this module is one way of doing that, used to demonstrate the
database end to end.

The client has their own journey planning integration, which substitutes for
this module directly. Nothing in :mod:`bods_extractor.fares` imports it, and the
``requests`` dependency is optional (``pip install -e '.[demo]'``).

It returns a plain dict, so replacing it means producing the same shape from
whatever routing source you prefer.
"""
from __future__ import annotations

import datetime
import logging

import requests

log = logging.getLogger(__name__)

#: Seconds to wait for the Directions API before giving up.
REQUEST_TIMEOUT = 20

#: Vehicle types Google reports that this dataset cannot price. Metro, tram and
#: heavy rail fares are not published to BODS, so legs using them are flagged
#: rather than costed.
RAIL_VEHICLE_TYPES = {"TRAIN", "HEAVY_RAIL", "COMMUTER_TRAIN", "SUBWAY",
                      "METRO_RAIL", "MONORAIL", "TRAM", "RAIL", "HIGH_SPEED_TRAIN"}


def get_midweek_commute_timestamp() -> int:
    """Unix timestamp for 07:30 on the next Wednesday.

    Transit routing depends on when you travel. Wednesday morning is used as a
    representative commuting time because it avoids weekends and is the weekday
    least likely to be a bank holiday.
    """
    now = datetime.datetime.now()
    days_ahead = 2 - now.weekday()  # Wednesday is 2 in Python's weekday()
    if days_ahead <= 0:  # today is Wed/Thu/Fri, so target next week
        days_ahead += 7
    next_wednesday = now + datetime.timedelta(days=days_ahead)
    return int(next_wednesday.replace(hour=7, minute=30, second=0,
                                      microsecond=0).timestamp())


def get_transit_route(origin_postcode: str, dest_postcode: str, api_key: str):
    """Fetch a bus transit route, or ``None`` if no route could be obtained."""
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin_postcode}, UK",
        "destination": f"{dest_postcode}, UK",
        "mode": "transit",
        "transit_mode": "bus",
        "departure_time": get_midweek_commute_timestamp(),
        "region": "uk",
        "key": api_key,
    }

    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        log.error("Directions request failed: %s", exc)
        return None

    if data.get("status") != "OK" or not data.get("routes"):
        # Never log the response body: the request URL contains the API key.
        log.error("Directions returned status %s", data.get("status"))
        return None

    route_data = data["routes"][0]
    leg = route_data["legs"][0]
    
    parsed_route = {
        "total_distance_km": leg["distance"]["value"] / 1000.0,
        "duration_mins": leg["duration"]["value"] // 60,
        "steps": [],
    }

    for step in leg.get("steps", []):
        if step.get("travel_mode") != "TRANSIT":
            continue  # walking legs cost nothing
        transit_details = step.get("transit_details", {})
        line = transit_details.get("line", {})
        agencies = line.get("agencies", [])
        vehicle_type = line.get("vehicle", {}).get("type", "")

        parsed_route["steps"].append({
            "departure_location": transit_details.get("departure_stop", {}).get("location"),
            "arrival_location": transit_details.get("arrival_stop", {}).get("location"),
            "departure_stop_name": transit_details.get("departure_stop", {}).get("name"),
            "arrival_stop_name": transit_details.get("arrival_stop", {}).get("name"),
            "route_name": line.get("short_name") or line.get("name") or "Unknown Route",
            "agency_name": agencies[0].get("name") if agencies else "Unknown Operator",
            "is_train": vehicle_type in RAIL_VEHICLE_TYPES,
            "distance_km": step["distance"]["value"] / 1000.0,
        })

    return parsed_route
