"""Demonstration journey planner: Google Directions plus the fares database.

This module exists to *show* what the fares database can do. It is not part of
the deliverable and nothing in :mod:`bods_extractor.fares` depends on it - the
client already has their own routing integration, so the intended production
shape is "your routing engine" plus :class:`bods_extractor.fares.FareLookup`.

Everything here returns data. Presentation lives in the CLI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .directions import get_transit_route
from .fares import FareLookup, FareQuote

log = logging.getLogger(__name__)

#: DEFRA-style greenhouse gas conversion factors, kg CO2e per passenger-km.
CAR_CO2_PER_KM = 0.170
BUS_CO2_PER_KM = 0.082


@dataclass
class JourneyLeg:
    route_name: str
    agency_name: str
    is_train: bool
    distance_km: float
    origin_atco: str | None = None
    destination_atco: str | None = None
    origin_stop_name: str = ""
    destination_stop_name: str = ""
    matched_noc: str | None = None
    match_method: str = ""
    quotes: list[FareQuote] = field(default_factory=list)

    @property
    def best_quote(self) -> FareQuote | None:
        return self.quotes[0] if self.quotes else None


@dataclass
class Journey:
    origin: str
    destination: str
    legs: list[JourneyLeg] = field(default_factory=list)
    distance_km: float = 0.0
    duration_mins: int = 0
    unmatched_stops: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def success(self) -> bool:
        return not self.error

    @property
    def total_cost(self) -> float:
        """One ticket per operator, not per leg.

        Using the same operator twice in a journey usually means one ticket
        covers both legs, so charging per leg would overstate the cost.
        """
        per_operator: dict[str, float] = {}
        for leg in self.legs:
            quote = leg.best_quote
            if quote is None:
                continue
            existing = per_operator.get(quote.noc)
            if existing is None or quote.price > existing:
                per_operator[quote.noc] = quote.price
        return round(sum(per_operator.values()), 2)

    @property
    def is_incomplete(self) -> bool:
        return any(leg.best_quote is None for leg in self.legs if not leg.is_train)

    @property
    def carbon(self) -> dict[str, float]:
        car = round(self.distance_km * CAR_CO2_PER_KM, 2)
        bus = round(self.distance_km * BUS_CO2_PER_KM, 2)
        return {"car_kg": car, "bus_kg": bus, "saved_kg": round(car - bus, 2)}


def _match_operator(lookup: FareLookup, leg: JourneyLeg) -> tuple[str | None, str]:
    """Decide which NOC operates a leg.

    Ordered from strongest to weakest evidence:

    1. An operator whose fare zones contain *both* stops, and whose name also
       matches what the routing engine reported.
    2. Any operator whose zones contain both stops.
    3. Any operator serving the boarding stop.
    4. A name match alone.

    Step 1 matters because a busy corridor is served by several operators, so
    geography alone is ambiguous.
    """
    by_geography = lookup.operators_for_journey(leg.origin_atco, leg.destination_atco) \
        if leg.origin_atco and leg.destination_atco else []
    by_name = {op.noc for op in lookup.resolve_operator(leg.agency_name)}

    for noc in by_geography:
        if noc in by_name:
            return noc, "stops + operator name"
    if by_geography:
        return by_geography[0], "stops only (name did not match)"
    if leg.origin_atco:
        serving = lookup.operators_serving(leg.origin_atco)
        for noc in serving:
            if noc in by_name:
                return noc, "boarding stop + operator name"
        if serving:
            return serving[0], "boarding stop only"
    if by_name:
        return sorted(by_name)[0], "operator name only"
    return None, "no match"


def plan_journey(origin: str, destination: str, api_key: str,
                 db_path=None, user_type: str = "adult") -> Journey:
    """Route with Google, then price each leg from the local fares database."""
    journey = Journey(origin=origin, destination=destination)

    route = get_transit_route(origin, destination, api_key=api_key)
    if route is None:
        journey.error = ("No transit route found. Check the API key, the "
                         "locations, and that a bus route exists between them.")
        return journey

    journey.distance_km = route["total_distance_km"]
    journey.duration_mins = route["duration_mins"]

    with FareLookup(db_path) as lookup:
        for step in route["steps"]:
            leg = JourneyLeg(
                route_name=step["route_name"],
                agency_name=step["agency_name"],
                is_train=step["is_train"],
                distance_km=step["distance_km"],
                origin_stop_name=step.get("departure_stop_name") or "",
                destination_stop_name=step.get("arrival_stop_name") or "",
            )

            for location_key, attribute in (("departure_location", "origin_atco"),
                                            ("arrival_location", "destination_atco")):
                location = step.get(location_key)
                if location:
                    stop = lookup.nearest_stop(location["lat"], location["lng"])
                    if stop:
                        setattr(leg, attribute, stop.atco_code)

            if leg.origin_atco is None and leg.origin_stop_name:
                journey.unmatched_stops.append(leg.origin_stop_name)

            if not leg.is_train:
                leg.matched_noc, leg.match_method = _match_operator(lookup, leg)
                if leg.matched_noc and leg.origin_atco and leg.destination_atco:
                    leg.quotes = lookup.fares_between(
                        leg.origin_atco, leg.destination_atco,
                        noc=leg.matched_noc, user_type=user_type)

            journey.legs.append(leg)

    return journey
