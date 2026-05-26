import unittest

from brain.weather import WeatherClient


class WeatherUnitTests(unittest.TestCase):
    def setUp(self):
        self.client = WeatherClient()

    def test_convert_metar_temperature_to_fahrenheit_for_us_stack(self):
        metar_payload = {
            "temp": 20.0,
            "station_id": "KATL",
        }

        converted = self.client._convert_metar_temperature_to_fahrenheit(metar_payload)

        self.assertEqual(converted["temp"], 68.0)

    def test_us_open_meteo_stack_requests_fahrenheit_and_gfs_endpoint_for_hrrr(self):
        calls = []

        def fake_request(url, params=None, headers=None, timeout=20):
            calls.append((url, dict(params or {})))
            return ({
                "timezone": "America/New_York",
                "utc_offset_seconds": -14400,
                "hourly": {
                    "time": [],
                    "temperature_2m": [],
                },
            }, {"response_received_at": "2026-05-26T00:00:00Z", "server_date": None})

        self.client._request_json_with_meta = fake_request
        self.client.get_noaa_forecast = lambda lat, lon: {"timezone": "America/New_York", "hourly_periods": []}

        self.client._build_us_stack(33.749, -84.388)

        self.assertTrue(any(url.endswith("/v1/gfs") for url, _ in calls))
        self.assertTrue(all(params.get("temperature_unit") == "fahrenheit" for _, params in calls))

    def test_non_us_stack_uses_dedicated_ecmwf_and_gfs_endpoints(self):
        calls = []

        def fake_request(url, params=None, headers=None, timeout=20):
            calls.append((url, dict(params or {})))
            return ({
                "timezone": "America/Argentina/Buenos_Aires",
                "utc_offset_seconds": -10800,
                "hourly": {
                    "time": [],
                    "temperature_2m": [],
                },
            }, {"response_received_at": "2026-05-26T00:00:00Z", "server_date": None})

        self.client._request_json_with_meta = fake_request

        self.client._build_non_us_stack(-34.82222, -58.53583)

        self.assertTrue(any(url.endswith("/v1/ecmwf") for url, _ in calls))
        self.assertTrue(any(url.endswith("/v1/gfs") for url, _ in calls))


if __name__ == "__main__":
    unittest.main()
