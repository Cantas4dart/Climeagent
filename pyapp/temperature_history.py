# -*- coding: utf-8 -*-
import json
import re
from urllib.parse import urlparse
from datetime import date, datetime
from typing import Any

import requests


class StationHistoryClient:
    def __init__(self):
        self.session = requests.Session()
        self.user_agent = {"User-Agent": "climeagent-temperature-analysis/1.0"}

    def fetch_daily_actual_temperature(self, entry_payload: dict[str, Any]) -> dict[str, Any]:
        market_date = self._coerce_date(entry_payload.get("market_date"))
        if market_date is None:
            return self._missing("missing_entry_snapshot")

        station_url = str(entry_payload.get("station_url") or "").strip()
        if station_url and self._is_allowed_station_url(station_url):
            try:
                resolved = self._fetch_wunderground_history(station_url, market_date, entry_payload)
                if resolved:
                    return resolved
            except Exception:
                pass
        elif station_url:
            return self._missing("station_fetch_error")

        lat = entry_payload.get("location_lat")
        lon = entry_payload.get("location_lon")
        if lat is None or lon is None:
            return self._missing("missing_station_data")

        try:
            resolved = self._fetch_open_meteo_archive(float(lat), float(lon), market_date, entry_payload)
            if resolved:
                return resolved
            return self._missing("missing_station_data")
        except Exception:
            return self._missing("station_fetch_error")

    def _fetch_wunderground_history(self, station_url: str, market_date: date, entry_payload: dict[str, Any]) -> dict[str, Any] | None:
        date_slug = market_date.isoformat()
        candidate_urls = [
            f"{station_url.rstrip('/')}/date/{date_slug}",
            f"{station_url.rstrip('/')}?year={market_date.year}&month={market_date.month}&day={market_date.day}",
        ]
        text = ""
        for url in candidate_urls:
            response = self.session.get(url, headers=self.user_agent, timeout=20)
            response.raise_for_status()
            text = response.text or ""
            resolved = self._parse_wunderground_temperature(text)
            if resolved is not None:
                return {
                    "actual_temperature": resolved["value"],
                    "actual_temperature_unit": resolved["unit"],
                    "actual_observed_at": f"{date_slug}T23:59:59",
                    "actual_source": "wunderground_history",
                    "actual_source_status": "resolved",
                }
        if text:
            return None
        return None

    def _parse_wunderground_temperature(self, html: str) -> dict[str, Any] | None:
        unit = self._detect_wunderground_unit(html)
        if unit is None:
            return None
        patterns = [
            r'"temperatureMax"\s*:\s*{"value"\s*:\s*(-?\d+(?:\.\d+)?)',
            r'"temperatureHigh"\s*:\s*{"value"\s*:\s*(-?\d+(?:\.\d+)?)',
            r'"maxTemperature"\s*:\s*(-?\d+(?:\.\d+)?)',
            r'"temperatureMaxValue"\s*:\s*(-?\d+(?:\.\d+)?)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                try:
                    return {"value": round(float(match.group(1)), 2), "unit": unit}
                except ValueError:
                    continue

        script_match = re.search(r"window\.__data\s*=\s*(\{.*?\})\s*;\s*</script>", html, re.DOTALL)
        if script_match:
            try:
                payload = json.loads(script_match.group(1))
                candidate = self._find_numeric_key(payload, {"temperatureMax", "temperatureHigh", "maxTemperature"})
                if candidate is not None:
                    return {"value": round(float(candidate), 2), "unit": unit}
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
        return None

    def _detect_wunderground_unit(self, html: str) -> str | None:
        patterns = [
            (r'"temperatureUnit"\s*:\s*"F"', "fahrenheit"),
            (r'"temperatureUnit"\s*:\s*"C"', "celsius"),
            (r'"imperial"\s*:\s*true', "fahrenheit"),
            (r'"metric"\s*:\s*true', "celsius"),
        ]
        for pattern, unit in patterns:
            if re.search(pattern, html):
                return unit
        return None

    def _is_allowed_station_url(self, station_url: str) -> bool:
        try:
            parsed = urlparse(station_url)
        except ValueError:
            return False
        hostname = (parsed.hostname or "").lower()
        return hostname in {"www.wunderground.com", "wunderground.com"}

    def _fetch_open_meteo_archive(self, lat: float, lon: float, market_date: date, entry_payload: dict[str, Any]) -> dict[str, Any] | None:
        timezone_candidates = []
        explicit_timezone = str(entry_payload.get("timezone") or "").strip()
        if explicit_timezone:
            timezone_candidates.append(explicit_timezone)
        timezone_candidates.extend(["auto", "UTC"])

        base_params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": market_date.isoformat(),
            "end_date": market_date.isoformat(),
            "daily": "temperature_2m_max",
        }
        if entry_payload.get("temperature_unit") == "fahrenheit":
            base_params["temperature_unit"] = "fahrenheit"

        last_error = None
        for timezone_name in timezone_candidates:
            params = dict(base_params)
            params["timezone"] = timezone_name
            try:
                response = self.session.get(
                    "https://archive-api.open-meteo.com/v1/archive",
                    params=params,
                    headers=self.user_agent,
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                daily = payload.get("daily") or {}
                values = daily.get("temperature_2m_max") or []
                times = daily.get("time") or []
                if not values:
                    continue
                actual_value = float(values[0])
                observed_at = f"{times[0]}T23:59:59" if times else None
                return {
                    "actual_temperature": round(actual_value, 2),
                    "actual_temperature_unit": "fahrenheit" if params.get("temperature_unit") == "fahrenheit" else "celsius",
                    "actual_observed_at": observed_at,
                    "actual_source": "open_meteo_archive_station_coords",
                    "actual_source_status": "resolved",
                }
            except Exception as exc:
                last_error = exc
                continue
        if last_error:
            raise last_error
        return None

    def _find_numeric_key(self, node: Any, keys: set[str]) -> float | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys:
                    if isinstance(value, (int, float)):
                        return float(value)
                    if isinstance(value, dict):
                        inner = value.get("value")
                        if isinstance(inner, (int, float)):
                            return float(inner)
                nested = self._find_numeric_key(value, keys)
                if nested is not None:
                    return nested
        elif isinstance(node, list):
            for item in node:
                nested = self._find_numeric_key(item, keys)
                if nested is not None:
                    return nested
        return None

    def _coerce_date(self, value: Any) -> date | None:
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            return None

    def _missing(self, status: str) -> dict[str, Any]:
        return {
            "actual_temperature": None,
            "actual_temperature_unit": None,
            "actual_observed_at": None,
            "actual_source": None,
            "actual_source_status": status,
        }
