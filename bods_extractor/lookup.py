import math
import sqlite3
import time
from .db import get_connection
from .directions import get_transit_route

TICKET_PREFERENCE = ["Day Pass", "Return", "Weekly/Period", "Single"]

class LookupEngine:
    def __init__(self):
        self.conn = get_connection()

    @staticmethod
    def _distance(e1, n1, e2, n2):
        return math.sqrt((e1 - e2)**2 + (n1 - n2)**2)

    def _nearest_stop(self, lat, lng, radius_m=1500):
        from .utils import latlng_to_osgb36
        target_e, target_n = latlng_to_osgb36(lat, lng)
        cursor = self.conn.cursor()
        
        # We search a generous bounding box first, then calculate exact math distance
        cursor.execute('''
            SELECT atco_code, easting, northing FROM stops 
            WHERE easting BETWEEN ? AND ? AND northing BETWEEN ? AND ?
        ''', (target_e - radius_m, target_e + radius_m, target_n - radius_m, target_n + radius_m))
        
        best_stop = None
        best_dist = float('inf')
        for atco, e, n in cursor.fetchall():
            d = self._distance(target_e, target_n, e, n)
            if d < best_dist and d <= radius_m:
                best_dist = d
                best_stop = atco
        return best_stop

    def _match_operator_by_name(self, agency_name):
        if not agency_name:
            return None
        agency_lower = agency_name.lower()
        cursor = self.conn.cursor()
        cursor.execute('SELECT DISTINCT operator_id FROM fare_matrix')
        
        for row in cursor.fetchall():
            op = row[0]
            if op.lower() in agency_lower or agency_lower in op.lower():
                return op
        return None

    def _operators_for_stop(self, atco_code):
        if not atco_code:
            return []
        cursor = self.conn.cursor()
        cursor.execute('SELECT DISTINCT operator_id FROM zones WHERE atco_code = ?', (atco_code,))
        return [row[0] for row in cursor.fetchall()]

    def _operators_for_leg(self, dep_atco, arr_atco):
        """Finds operators that serve BOTH the departure and arrival stops of a leg."""
        if not dep_atco or not arr_atco:
            return []
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT DISTINCT z1.operator_id 
            FROM zones z1
            JOIN zones z2 ON z1.operator_id = z2.operator_id
            WHERE z1.atco_code = ? AND z2.atco_code = ?
        ''', (dep_atco, arr_atco))
        return [row[0] for row in cursor.fetchall()]

    def _cheapest_fare_for_operator(self, operator_id):
        if not operator_id:
            return None, None
        cursor = self.conn.cursor()
        
        for ticket_type in TICKET_PREFERENCE:
            cursor.execute('''
                SELECT price, commuter_score FROM fare_matrix
                WHERE operator_id = ? AND ticket_type = ? AND price >= 1.00
                ORDER BY commuter_score DESC
            ''', (operator_id, ticket_type))
            rows = cursor.fetchall()
            
            if not rows:
                continue
                
            best_score = rows[0][1]
            
            # If the score is negative, it means ONLY kid/dog tickets exist for this type. Skip it.
            if best_score < 0:
                continue 
                
            # Get all prices that tied for the absolute best score
            top_tier_prices = [r[0] for r in rows if r[1] == best_score]
            
            # NEVER use min(). Always take the median of the top tier to 
            # bypass weird local restrictions and find the "standard" commuter price.
            top_tier_prices.sort()
            median_price = top_tier_prices[len(top_tier_prices) // 2]
            return ticket_type, median_price

        return None, None

    def find_commuter_fare(self, origin_postcode, dest_postcode, api_key=None):
        print(f"\n[~] Querying Google Directions API (Departing 07:30 AM next Monday)...")
        t0 = time.time()
        route = get_transit_route(origin_postcode, dest_postcode, api_key=api_key)
        t_api = time.time()
        print(f"[~] Google API returned in {t_api - t0:.2f}s")

        result = {
            "success": False,
            "origin_postcode": origin_postcode.upper(),
            "dest_postcode": dest_postcode.upper(),
        }

        if not route:
            result["error"] = "No transit route found. Check the Google Maps API key and postcodes."
            return result

        legs = []
        operators_seen = {} 
        unmatched_stops = []

        print(f"[~] Spatially matching {len(route['steps'])} route legs to local ATCO stops...")
        for idx, step in enumerate(route["steps"]):
            t_leg_start = time.time()
            
            # 1. Find the ATCO codes for BOTH ends of the journey leg
            dep_stop_atco = None
            if step["departure_location"]:
                dep_stop_atco = self._nearest_stop(step["departure_location"]["lat"], step["departure_location"]["lng"])
            
            arr_stop_atco = None
            if step.get("arrival_location"):
                arr_stop_atco = self._nearest_stop(step["arrival_location"]["lat"], step["arrival_location"]["lng"])

            if dep_stop_atco is None:
                unmatched_stops.append(step.get("departure_stop_name"))

            # 2. Try the physical route intersection first
            matched_operators = self._operators_for_leg(dep_stop_atco, arr_stop_atco)
            
            # 3. Fallback to just the departure stop if the destination couldn't be matched
            if not matched_operators:
                matched_operators = self._operators_for_stop(dep_stop_atco)

            # 4. Final fallback to Google's text string
            if not matched_operators:
                fallback = self._match_operator_by_name(step["agency_name"])
                matched_operators = [fallback] if fallback else []

            # 5. Resolve the operator
            leg_operator = None
            google_agency = (step["agency_name"] or "").lower()
            
            if matched_operators:
                # If multiple operators share the route, check if Google's name matches one
                for op in matched_operators:
                    if google_agency in op.lower() or op.lower() in google_agency:
                        leg_operator = op
                        break
                # Edge case: If name match fails, pick the first one
                if not leg_operator:
                    leg_operator = matched_operators[0]

            legs.append({
                "route_name": step["route_name"],
                "agency_name": step["agency_name"],
                "operator_id": leg_operator,
                "is_train": step["is_train"],
                "distance_km": step["distance_km"],
                "matched_stop": dep_stop_atco,
            })

            agency = step["agency_name"] or "Unknown Operator"
            if agency not in operators_seen:
                operators_seen[agency] = leg_operator
                
            t_leg_end = time.time()
            print(f"  -> Leg {idx+1} ({step['route_name']}): Matched in {t_leg_end - t_leg_start:.3f}s")

        print("[~] Extracting optimal fares from database...")
        t_fare_start = time.time()
        
        fare_breakdown = []
        total_cost = 0.0
        is_incomplete = False

        for agency_name, operator_id in operators_seen.items():
            ticket_type, price = self._cheapest_fare_for_operator(operator_id)
            fare_breakdown.append({
                "operator_id": operator_id,
                "agency_name": agency_name,
                "ticket_type": ticket_type,
                "price": price,
            })
            if price is not None:
                total_cost += price
            else:
                is_incomplete = True

        dist_km = route["total_distance_km"]
        car_co2 = round(dist_km * 0.170, 2)
        bus_co2 = round(dist_km * 0.082, 2)

        result.update({
            "success": True,
            "legs": legs,
            "fare_breakdown": fare_breakdown,
            "total_cost": round(total_cost, 2),
            "is_incomplete": is_incomplete,
            "distance_km": dist_km,
            "duration_mins": route["duration_mins"],
            "car_co2": car_co2,
            "bus_co2": bus_co2,
            "saved_co2": round(car_co2 - bus_co2, 2),
            "unmatched_stops": [s for s in unmatched_stops if s],
        })
        
        print(f"[~] Fares calculated in {time.time() - t_fare_start:.3f}s\n")
        return result