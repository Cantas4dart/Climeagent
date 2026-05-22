import requests
from datetime import datetime
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv

try:
    from .console import safe_print
    from .forecast_enhancer import ForecastEnhancer
except ImportError:
    from console import safe_print
    from forecast_enhancer import ForecastEnhancer

load_dotenv()


class WeatherClient:
    def __init__(self):
        self.user_agent = {"User-Agent": "(polymarket-weather-bot, contact@example.com)"}
        self.enhancer = ForecastEnhancer()

    @staticmethod
    def _utc_now_iso():
        return datetime.utcnow().isoformat() + "Z"

    def _request_json_with_meta(self, url, params=None, headers=None, timeout=20):
        merged_headers = dict(self.user_agent)
        if headers:
            merged_headers.update(headers)
        response = requests.get(url, params=params, headers=merged_headers, timeout=timeout)
        response.raise_for_status()

        server_date = None
        raw_date = response.headers.get("Date")
        if raw_date:
            try:
                server_date = parsedate_to_datetime(raw_date).isoformat()
            except (TypeError, ValueError, IndexError):
                server_date = None

        return response.json(), {
            "response_received_at": self._utc_now_iso(),
            "server_date": server_date,
        }

    def _request_json(self, url, params=None, headers=None, timeout=20):
        payload, _ = self._request_json_with_meta(url, params=params, headers=headers, timeout=timeout)
        return payload

    @staticmethod
    def _c_to_f(value):
        return (float(value) * 9.0 / 5.0) + 32.0

    def _convert_metar_temperature_to_fahrenheit(self, metar_payload):
        if not isinstance(metar_payload, dict):
            return metar_payload
        converted = dict(metar_payload)
        for key in ("temp", "tempC", "temp_c", "temperature", "temperature_c"):
            if converted.get(key) is None:
                continue
            try:
                converted[key] = round(self._c_to_f(converted[key]), 2)
            except (TypeError, ValueError):
                continue
        return converted

    def get_noaa_forecast(self, lat, lon):
        """Fetch forecast from api.weather.gov (US only)."""
        try:
            points_url = f"https://api.weather.gov/points/{lat},{lon}"
            points_raw, points_meta = self._request_json_with_meta(points_url)
            points_payload = points_raw.get("properties", {})
            forecast_url = points_payload["forecastHourly"]
            timezone_name = points_payload.get("timeZone")

            hourly_raw, hourly_meta = self._request_json_with_meta(forecast_url)
            hourly_props = hourly_raw.get("properties", {})
            provider_issued_at = (
                hourly_props.get("updateTime")
                or hourly_props.get("generatedAt")
                or points_payload.get("updateTime")
                or hourly_meta.get("server_date")
                or points_meta.get("server_date")
            )
            if hourly_props.get("updateTime"):
                provider_issued_at_source = "noaa_update_time"
            elif hourly_props.get("generatedAt"):
                provider_issued_at_source = "noaa_generated_at"
            elif points_payload.get("updateTime"):
                provider_issued_at_source = "noaa_points_update_time"
            elif hourly_meta.get("server_date") or points_meta.get("server_date"):
                provider_issued_at_source = "http_date_header"
            else:
                provider_issued_at_source = "fetch_time_fallback"
            return {
                "source": "noaa",
                "fetched_at": self._utc_now_iso(),
                "provider_issued_at": provider_issued_at or self._utc_now_iso(),
                "provider_issued_at_source": provider_issued_at_source,
                "timezone": timezone_name,
                "hourly_periods": hourly_props.get("periods", []),
            }
        except Exception as exc:
            safe_print(f"Error fetching NOAA forecast: {exc}")
            return None

    def get_open_meteo_forecast(self, lat, lon, temperature_unit=None):
        """Fetch Open-Meteo baseline forecast."""
        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "forecast_days": 7,
                "timezone": "auto",
            }
            if temperature_unit:
                params["temperature_unit"] = temperature_unit
            payload, meta = self._request_json_with_meta(url, params=params)
            return {
                "source": "open-meteo",
                "fetched_at": self._utc_now_iso(),
                "provider_issued_at": meta.get("server_date") or meta.get("response_received_at"),
                "provider_issued_at_source": "http_date_header" if meta.get("server_date") else "fetch_time_fallback",
                "timezone": payload.get("timezone"),
                "utc_offset_seconds": payload.get("utc_offset_seconds"),
                "hourly": payload.get("hourly", {}),
            }
        except Exception as exc:
            safe_print(f"Error fetching Open-Meteo forecast: {exc}")
            return None

    def get_open_meteo_model_forecast(self, lat, lon, models, label, temperature_unit=None, base_url="https://api.open-meteo.com/v1/forecast"):
        """Fetch model-specific Open-Meteo forecast."""
        try:
            params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "forecast_days": 7,
                "timezone": "auto",
            }
            if models:
                params["models"] = models
            if temperature_unit:
                params["temperature_unit"] = temperature_unit
            payload, meta = self._request_json_with_meta(base_url, params=params)
            return {
                "source": label,
                "fetched_at": self._utc_now_iso(),
                "provider_issued_at": meta.get("server_date") or meta.get("response_received_at"),
                "provider_issued_at_source": "http_date_header" if meta.get("server_date") else "fetch_time_fallback",
                "timezone": payload.get("timezone"),
                "utc_offset_seconds": payload.get("utc_offset_seconds"),
                "hourly": payload.get("hourly", {}),
            }
        except Exception as exc:
            safe_print(f"Error fetching {label} forecast: {exc}")
            return None

    def get_metar_observation(self, station_id):
        """Fetch latest METAR observation for the airport station anchor."""
        if not station_id:
            return None
        try:
            payload = self._request_json(
                "https://aviationweather.gov/api/data/metar",
                params={
                    "ids": str(station_id).upper(),
                    "format": "json",
                    "hours": 4,
                },
            )
            if isinstance(payload, list) and payload:
                latest = payload[0]
                latest["station_id"] = str(station_id).upper()
                latest["fetched_at"] = self._utc_now_iso()
                return latest
        except Exception as exc:
            safe_print(f"Error fetching METAR observation: {exc}")
        return None

    def _build_us_stack(self, lat, lon):
        noaa = self.get_noaa_forecast(lat, lon)
        baseline = self.get_open_meteo_forecast(lat, lon, temperature_unit="fahrenheit")
        hrrr = self.get_open_meteo_model_forecast(
            lat,
            lon,
            None,
            "hrrr",
            temperature_unit="fahrenheit",
            base_url="https://api.open-meteo.com/v1/gfs",
        )
        gfs = self.get_open_meteo_model_forecast(
            lat,
            lon,
            "gfs_seamless",
            "gfs",
            temperature_unit="fahrenheit",
        )
        timezone_name = (
            (noaa or {}).get("timezone")
            or (baseline or {}).get("timezone")
            or (hrrr or {}).get("timezone")
        )
        utc_offset_seconds = (
            (baseline or {}).get("utc_offset_seconds")
            or (hrrr or {}).get("utc_offset_seconds")
            or (gfs or {}).get("utc_offset_seconds")
        )
        return {
            "source": "us-stack",
            "timezone": timezone_name,
            "utc_offset_seconds": utc_offset_seconds,
            "noaa": noaa,
            "open_meteo": baseline,
            "hrrr": hrrr,
            "gfs": gfs,
        }

    def _build_non_us_stack(self, lat, lon):
        baseline = self.get_open_meteo_forecast(lat, lon)
        ecmwf = self.get_open_meteo_model_forecast(lat, lon, "ecmwf_ifs025", "ecmwf")
        gfs = self.get_open_meteo_model_forecast(lat, lon, "gfs_seamless", "gfs")
        timezone_name = (
            (ecmwf or {}).get("timezone")
            or (baseline or {}).get("timezone")
            or (gfs or {}).get("timezone")
        )
        utc_offset_seconds = (
            (baseline or {}).get("utc_offset_seconds")
            or (ecmwf or {}).get("utc_offset_seconds")
            or (gfs or {}).get("utc_offset_seconds")
        )
        return {
            "source": "non-us-stack",
            "timezone": timezone_name,
            "utc_offset_seconds": utc_offset_seconds,
            "open_meteo": baseline,
            "ecmwf": ecmwf,
            "gfs": gfs,
        }

    def get_forecast(self, lat, lon, is_us=True, location=None, market_date=None):
        location = location or {}
        station_id = location.get("resolution_station_id") or location.get("station_id")

        if is_us:
            forecast_bundle = self._build_us_stack(lat, lon)
        else:
            forecast_bundle = self._build_non_us_stack(lat, lon)

        metar_payload = self.get_metar_observation(station_id)
        if is_us:
            metar_payload = self._convert_metar_temperature_to_fahrenheit(metar_payload)
        forecast_bundle["metar"] = metar_payload
        forecast_bundle["location"] = {
            "lat": lat,
            "lon": lon,
            "station_id": station_id,
            "city": location.get("city"),
            "timezone": location.get("timezone"),
        }
        if market_date is not None:
            forecast_bundle["market_date"] = market_date.isoformat()
        forecast_bundle["fetched_at"] = self._utc_now_iso()

        enhancement = self.enhancer.enhance(
            forecast_bundle=forecast_bundle,
            market_date=market_date or datetime.utcnow().date(),
            is_us=is_us,
            timezone_name=forecast_bundle.get("timezone") or location.get("timezone"),
        )
        forecast_bundle["enhancement"] = {
            "adjusted_high_temperature": enhancement.get("adjusted_high_temperature"),
            "confidence_score": enhancement.get("confidence_score"),
            "source_adjustments": enhancement.get("source_adjustments", {}),
            "metar_anchor_temperature": enhancement.get("metar_anchor_temperature"),
        }
        forecast_bundle["enhanced_sources"] = enhancement.get("source_temperatures", {})
        return forecast_bundle


if __name__ == "__main__":
    client = WeatherClient()
