"""Turn 'City, ST' into coordinates with the Open-Meteo geocoding API."""

import urllib.parse

from src.report import http_get_json


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR", "Guam": "GU", "U.S. Virgin Islands": "VI", "American Samoa": "AS",
    "Northern Mariana Islands": "MP",
}


def geocode(query: str, user_agent: str, fetch=None) -> list:
    """'New York, NY' -> US matches from Open-Meteo (GeoNames data), best match first."""
    fetch = fetch or http_get_json
    params = urllib.parse.urlencode({
        "name": query.strip(), "count": 10, "language": "en", "countryCode": "US", "format": "json",
    })
    data = fetch(f"{GEOCODE_URL}?{params}", user_agent, accept="application/json")
    places = []
    for r in data.get("results") or []:
        if r.get("country_code") != "US" or "latitude" not in r:
            continue
        state = r.get("admin1", "")
        abbr = US_STATES.get(state, state)
        places.append({
            "name": f"{r['name']}, {abbr}" if abbr else r["name"],
            "detail": ", ".join(x for x in (r.get("admin2"), state) if x),
            "lat": round(float(r["latitude"]), 4),
            "lon": round(float(r["longitude"]), 4),
        })
    return places


def describe_place(place: dict) -> str:
    detail = f" ({place['detail']})" if place["detail"] else ""
    return f"{place['name']}{detail}  {place['lat']:.4f}, {place['lon']:.4f}"


def first_place(places: list) -> dict:
    """Non-interactive choice: the best (first) match."""
    return places[0]
