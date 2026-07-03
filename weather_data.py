"""Open-Meteo forecasts and qualitative park labels for MLB games."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import requests


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 20


# Approximate stadium-center coordinates. The home-team key also gives us a
# reliable fallback when MLB uses an alternate spelling for the venue.
MLB_STADIUMS: dict[str, dict[str, Any]] = {
    "Arizona Diamondbacks": {"name": "Chase Field", "latitude": 33.4455, "longitude": -112.0667},
    "Atlanta Braves": {"name": "Truist Park", "latitude": 33.8908, "longitude": -84.4677},
    "Baltimore Orioles": {"name": "Oriole Park at Camden Yards", "latitude": 39.2839, "longitude": -76.6217},
    "Boston Red Sox": {"name": "Fenway Park", "latitude": 42.3467, "longitude": -71.0972},
    "Chicago Cubs": {"name": "Wrigley Field", "latitude": 41.9484, "longitude": -87.6553},
    "Chicago White Sox": {"name": "Rate Field", "latitude": 41.8300, "longitude": -87.6338},
    "Cincinnati Reds": {"name": "Great American Ball Park", "latitude": 39.0979, "longitude": -84.5082},
    "Cleveland Guardians": {"name": "Progressive Field", "latitude": 41.4962, "longitude": -81.6852},
    "Colorado Rockies": {"name": "Coors Field", "latitude": 39.7559, "longitude": -104.9942},
    "Detroit Tigers": {"name": "Comerica Park", "latitude": 42.3390, "longitude": -83.0485},
    "Houston Astros": {"name": "Daikin Park", "latitude": 29.7573, "longitude": -95.3555},
    "Kansas City Royals": {"name": "Kauffman Stadium", "latitude": 39.0517, "longitude": -94.4803},
    "Los Angeles Angels": {"name": "Angel Stadium", "latitude": 33.8003, "longitude": -117.8827},
    "Los Angeles Dodgers": {"name": "Dodger Stadium", "latitude": 34.0739, "longitude": -118.2400},
    "Miami Marlins": {"name": "loanDepot park", "latitude": 25.7781, "longitude": -80.2197},
    "Milwaukee Brewers": {"name": "American Family Field", "latitude": 43.0280, "longitude": -87.9712},
    "Minnesota Twins": {"name": "Target Field", "latitude": 44.9817, "longitude": -93.2776},
    "New York Mets": {"name": "Citi Field", "latitude": 40.7571, "longitude": -73.8458},
    "New York Yankees": {"name": "Yankee Stadium", "latitude": 40.8296, "longitude": -73.9262},
    "Athletics": {"name": "Sutter Health Park", "latitude": 38.5803, "longitude": -121.5139},
    "Philadelphia Phillies": {"name": "Citizens Bank Park", "latitude": 39.9061, "longitude": -75.1665},
    "Pittsburgh Pirates": {"name": "PNC Park", "latitude": 40.4469, "longitude": -80.0057},
    "San Diego Padres": {"name": "Petco Park", "latitude": 32.7076, "longitude": -117.1570},
    "San Francisco Giants": {"name": "Oracle Park", "latitude": 37.7786, "longitude": -122.3893},
    "Seattle Mariners": {"name": "T-Mobile Park", "latitude": 47.5914, "longitude": -122.3325},
    "St. Louis Cardinals": {"name": "Busch Stadium", "latitude": 38.6226, "longitude": -90.1928},
    "Tampa Bay Rays": {"name": "Tropicana Field", "latitude": 27.7682, "longitude": -82.6534},
    "Texas Rangers": {"name": "Globe Life Field", "latitude": 32.7473, "longitude": -97.0847},
    "Toronto Blue Jays": {"name": "Rogers Centre", "latitude": 43.6414, "longitude": -79.3894},
    "Washington Nationals": {"name": "Nationals Park", "latitude": 38.8730, "longitude": -77.0074},
}


# Broad labels are intentionally used instead of pretending these are live,
# numeric park-factor measurements.
PARK_FACTORS: dict[str, str] = {
    "Chase Field": "hitter-friendly", "Truist Park": "hitter-friendly",
    "Oriole Park at Camden Yards": "neutral", "Fenway Park": "hitter-friendly",
    "Wrigley Field": "neutral", "Rate Field": "hitter-friendly",
    "Great American Ball Park": "hitter-friendly", "Progressive Field": "neutral",
    "Coors Field": "extreme hitter park", "Comerica Park": "pitcher-friendly",
    "Daikin Park": "hitter-friendly", "Kauffman Stadium": "neutral",
    "Angel Stadium": "neutral", "Dodger Stadium": "neutral",
    "loanDepot park": "pitcher-friendly", "American Family Field": "hitter-friendly",
    "Target Field": "neutral", "Citi Field": "pitcher-friendly",
    "Yankee Stadium": "HR-friendly", "Sutter Health Park": "hitter-friendly",
    "Citizens Bank Park": "hitter-friendly", "PNC Park": "pitcher-friendly",
    "Petco Park": "pitcher-friendly", "Oracle Park": "pitcher-friendly",
    "T-Mobile Park": "pitcher-friendly", "Busch Stadium": "neutral",
    "Tropicana Field": "pitcher-friendly", "Globe Life Field": "neutral",
    "Rogers Centre": "hitter-friendly", "Nationals Park": "neutral",
}


class WeatherDataError(Exception):
    """Raised when Open-Meteo cannot provide a usable response."""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _stadium_for_game(game: dict[str, Any]) -> dict[str, Any] | None:
    """Find a venue by MLB venue name, then fall back to the home team."""
    for stadium in MLB_STADIUMS.values():
        if stadium["name"] == game.get("venue"):
            return stadium
    return MLB_STADIUMS.get(str(game.get("home_team", "")))


def _wind_compass_direction(degrees: int | float | None) -> str | None:
    if not isinstance(degrees, (int, float)):
        return None
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(degrees / 45) % 8]


def get_game_weather(game: dict[str, Any]) -> dict[str, Any] | str:
    """Return the hourly forecast nearest to the scheduled first pitch."""
    stadium = _stadium_for_game(game)
    game_time = _parse_time(game.get("game_time"))
    if stadium is None or game_time is None:
        return "unavailable"

    game_time_utc = game_time.astimezone(timezone.utc)
    forecast_date = game_time_utc.date().isoformat()
    try:
        response = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": stadium["latitude"], "longitude": stadium["longitude"],
                "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,precipitation_probability",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "timezone": "UTC", "start_date": forecast_date, "end_date": forecast_date,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise WeatherDataError("Open-Meteo weather is unavailable.") from error

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return "unavailable"

    def distance(index: int) -> float:
        forecast_time = _parse_time(f"{times[index]}+00:00")
        return abs((game_time_utc - forecast_time).total_seconds()) if forecast_time else float("inf")

    index = min(range(len(times)), key=distance)

    def value(field: str) -> Any:
        values = hourly.get(field, [])
        return values[index] if index < len(values) else None

    temperature = value("temperature_2m")
    wind_speed = value("wind_speed_10m")
    wind_degrees = value("wind_direction_10m")
    precipitation = value("precipitation_probability")
    wind_direction = _wind_compass_direction(wind_degrees)
    summary = []
    if isinstance(temperature, (int, float)):
        summary.append(f"{temperature:g}°F")
    if isinstance(wind_speed, (int, float)):
        summary.append(f"wind {wind_speed:g} mph" + (f" from {wind_direction}" if wind_direction else ""))
    if isinstance(precipitation, (int, float)):
        summary.append(f"{precipitation:g}% precipitation chance")
    return {
        "source": "Open-Meteo", "forecast_time_utc": times[index],
        "temperature_f": temperature, "wind_speed_mph": wind_speed,
        "wind_direction_degrees": wind_degrees, "wind_direction": wind_direction,
        "precipitation_probability_pct": precipitation,
        "summary": ", ".join(summary) or "Forecast values unavailable",
    }


def merge_weather_data(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach stadium, park factor, and weather without failing the slate."""
    weather_by_game: dict[Any, dict[str, Any] | str] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_game_weather, game): game.get("game_id") for game in slate}
        for future in as_completed(futures):
            try:
                weather_by_game[futures[future]] = future.result()
            except Exception:
                logging.warning("Weather lookup failed; continuing", exc_info=True)
                weather_by_game[futures[future]] = "unavailable"

    for game in slate:
        stadium = _stadium_for_game(game)
        stadium_name = stadium["name"] if stadium else game.get("venue")
        game["stadium"] = {
            "name": stadium_name,
            "latitude": stadium.get("latitude") if stadium else None,
            "longitude": stadium.get("longitude") if stadium else None,
        }
        game["park_factor"] = PARK_FACTORS.get(str(stadium_name), "neutral")
        game["weather"] = weather_by_game.get(game.get("game_id"), "unavailable")
    return slate
