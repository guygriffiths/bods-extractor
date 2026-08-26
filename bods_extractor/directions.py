import requests
import datetime

def get_midweek_commute_timestamp():
    """Returns a Unix timestamp for 07:30 AM on the next upcoming Wednesday to avoid Bank Holidays."""
    now = datetime.datetime.now()
    # Wednesday is index 2 in Python's weekday()
    days_ahead = 2 - now.weekday()
    if days_ahead <= 0:  # If today is Wed/Thu/Fri, target next week
        days_ahead += 7
    
    next_wednesday = now + datetime.timedelta(days=days_ahead)
    target_time = next_wednesday.replace(hour=7, minute=30, second=0, microsecond=0)
    return int(target_time.timestamp())

def get_transit_route(origin_postcode: str, dest_postcode: str, api_key: str):
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
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return None
        
    if data.get("status") != "OK" or not data.get("routes"):
        return None
        
    route_data = data["routes"][0]
    leg = route_data["legs"][0]
    
    parsed_route = {
        "total_distance_km": leg["distance"]["value"] / 1000.0,
        "duration_mins": leg["duration"]["value"] // 60,
        "steps": []
    }
    
    for step in leg.get("steps", []):
        if step.get("travel_mode") == "TRANSIT":
            transit_details = step.get("transit_details", {})
            line = transit_details.get("line", {})
            
            # Safely extract agency name
            agencies = line.get("agencies", [])
            agency_name = agencies[0].get("name") if agencies else "Unknown Operator"
            
            vehicle_type = line.get("vehicle", {}).get("type", "")
            
            parsed_route["steps"].append({
                "departure_location": transit_details.get("departure_stop", {}).get("location"),
                "arrival_location": transit_details.get("arrival_stop", {}).get("location"),
                "departure_stop_name": transit_details.get("departure_stop", {}).get("name"),
                "arrival_stop_name": transit_details.get("arrival_stop", {}).get("name"),
                "route_name": line.get("short_name") or line.get("name") or "Unknown Route",
                "agency_name": agency_name,
                "is_train": vehicle_type in ["TRAIN", "HEAVY_RAIL", "COMMUTER_TRAIN", "SUBWAY", "TRAM"],
                "distance_km": step["distance"]["value"] / 1000.0
            })
            
    return parsed_route