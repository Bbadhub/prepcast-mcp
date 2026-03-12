"""
Weather forecasting tool using Open-Meteo (free, no API key required).
Fetches weather for the store location and returns a sales impact multiplier.
"""

import json
import os
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("PREPCAST_DATA_DIR", "/tmp/prepcast"))

# Default: Overland Park, KS (near AdventHealth Sports Park)
DEFAULT_LAT = 38.8814
DEFAULT_LON = -94.6806

# Weather condition impact on QSR foot traffic
WEATHER_MULTIPLIERS = {
    "clear":        1.05,   # Nice day = slightly more foot traffic
    "partly_cloudy": 1.02,
    "cloudy":       0.98,
    "drizzle":      0.90,
    "rain":         0.80,   # Rain kills walk-in traffic significantly
    "heavy_rain":   0.70,
    "thunderstorm": 0.65,
    "snow":         0.60,
    "blizzard":     0.45,
    "fog":          0.92,
    "hot":          1.08,   # Hot sunny day near sports park = ice cream / drinks spike
    "cold":         0.95,
}

WMO_CODE_MAP = {
    0:  "clear",
    1:  "clear", 2: "partly_cloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    61: "rain", 63: "rain", 65: "heavy_rain",
    71: "snow", 73: "snow", 75: "blizzard",
    77: "snow",
    80: "rain", 81: "rain", 82: "heavy_rain",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}


def _fetch_weather(lat: float, lon: float, target_date: str) -> dict:
    """Fetch daily weather forecast from Open-Meteo."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&temperature_unit=fahrenheit"
        f"&timezone=America%2FChicago"
        f"&start_date={target_date}&end_date={target_date}"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _condition_from_wmo(code: int, temp_max: float) -> str:
    condition = WMO_CODE_MAP.get(code, "cloudy")
    if condition == "clear" and temp_max >= 88:
        condition = "hot"
    return condition


TOOL_GET_WEATHER_FORECAST = {
    "name": "get_weather_forecast",
    "description": (
        "Get the weather forecast for the store's location on a given date and its "
        "estimated impact on sales. Returns condition, temperature range, precipitation, "
        "and a multiplier to apply to your base revenue forecast."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date to forecast (YYYY-MM-DD). Defaults to today.",
            },
            "lat": {
                "type": "number",
                "description": "Latitude of store location. Defaults to Overland Park, KS.",
            },
            "lon": {
                "type": "number",
                "description": "Longitude of store location. Defaults to Overland Park, KS.",
            },
        },
        "required": [],
    },
}


async def handle_get_weather_forecast(arguments: dict[str, Any]) -> dict:
    target_date = arguments.get("date") or date.today().isoformat()
    lat = float(arguments.get("lat") or DEFAULT_LAT)
    lon = float(arguments.get("lon") or DEFAULT_LON)

    data = _fetch_weather(lat, lon, target_date)
    if "error" in data:
        return {
            "content": [{
                "type": "text",
                "text": f"Could not fetch weather: {data['error']}. Proceeding without weather adjustment (multiplier: 1.0).",
            }]
        }

    try:
        daily = data["daily"]
        wmo_code = daily["weathercode"][0]
        temp_max = daily["temperature_2m_max"][0]
        temp_min = daily["temperature_2m_min"][0]
        precip = daily["precipitation_sum"][0]

        condition = _condition_from_wmo(wmo_code, temp_max)
        multiplier = WEATHER_MULTIPLIERS.get(condition, 1.0)

        impact = "neutral"
        if multiplier > 1.02:
            impact = "positive"
        elif multiplier < 0.90:
            impact = "significant negative"
        elif multiplier < 0.98:
            impact = "slightly negative"

        lines = [
            f"Weather forecast for {target_date}:",
            f"  Condition: {condition.replace('_', ' ').title()}",
            f"  Temperature: {temp_min:.0f}°F – {temp_max:.0f}°F",
            f"  Precipitation: {precip:.1f} mm",
            f"  Sales impact: {impact} (multiplier: {multiplier:.2f}x)",
            "",
            f"Apply this multiplier to your base revenue forecast.",
        ]
        if multiplier <= 0.80:
            lines.append("Note: Heavy rain/storm — consider reducing prep quantities significantly.")
        elif multiplier >= 1.05:
            lines.append("Note: Great weather — consider adding 5-10% buffer to prep quantities.")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    except (KeyError, IndexError) as e:
        return {
            "content": [{
                "type": "text",
                "text": f"Weather data unavailable for {target_date}. Using multiplier: 1.0 (no adjustment).",
            }]
        }
