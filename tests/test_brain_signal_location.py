import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from brain.markets import MarketClient
from brain.signals import SignalGenerator


class MarketClientStationOverrideTests(unittest.TestCase):
    def test_station_id_is_extracted_from_station_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            station_file = Path(tmpdir) / "station.md"
            station_file.write_text(
                "\n".join(
                    [
                        "Los Angeles: Los Angeles International Airport Station: https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX",
                        "Istanbul: Istanbul Airport Station: https://www.weather.gov/wrh/timeseries?site=LTFM",
                        "Hong Kong: Hong Kong Observatory: https://www.weather.gov.hk/en/cis/climat.htm",
                        "",
                        "## Airport Coordinates Reference",
                        "",
                        "| City / Airport | ICAO | Latitude | Longitude | Notes |",
                        "|---|---|---|---|---|",
                        "| Los Angeles | KLAX | 33.94250 | -118.40806 | Good |",
                        "| Istanbul | LTFM | 41.27487 | 28.73214 | Good |",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            client = MarketClient(station_file_path=station_file)

            self.assertEqual(client.station_overrides["los angeles"]["station_id"], "KLAX")
            self.assertEqual(client.station_overrides["istanbul"]["station_id"], "LTFM")
            self.assertIsNone(client.station_overrides["hong kong"]["station_id"])
            self.assertAlmostEqual(client.station_overrides["los angeles"]["station_lat"], 33.94250)
            self.assertAlmostEqual(client.station_overrides["los angeles"]["station_lon"], -118.40806)

    def test_station_override_applies_reference_coordinates_without_geocoding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            station_file = Path(tmpdir) / "station.md"
            station_file.write_text(
                "\n".join(
                    [
                        "New York City: LaGuardia Airport Station: https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
                        "",
                        "## Airport Coordinates Reference",
                        "",
                        "| City / Airport | ICAO | Latitude | Longitude | Notes |",
                        "|---|---|---|---|---|",
                        "| New York City | KLGA | 40.77693 | -73.87397 | Good |",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            client = MarketClient(station_file_path=station_file)

            with patch.object(client, "_geocode_city") as geocode_mock:
                location = client.parse_market_location(
                    "Highest temperature in NYC on May 17? :: Will the highest temperature in New York City be between 86-87F on May 17?"
                )

            self.assertIsNotNone(location)
            self.assertEqual(location["city"], "New York")
            self.assertAlmostEqual(location["lat"], 40.77693)
            self.assertAlmostEqual(location["lon"], -73.87397)
            self.assertEqual(location["resolution_source"], "station.md")
            self.assertEqual(location["resolution_station_name"], "LaGuardia Airport Station")
            self.assertEqual(location["resolution_station_id"], "KLGA")
            self.assertTrue(location["resolution_coordinates_applied"])
            geocode_mock.assert_not_called()

    def test_station_override_falls_back_to_geocoding_without_matching_reference_coordinates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            station_file = Path(tmpdir) / "station.md"
            station_file.write_text(
                "\n".join(
                    [
                        "New York City: LaGuardia Airport Station: https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
                        "",
                        "## Airport Coordinates Reference",
                        "",
                        "| City / Airport | ICAO | Latitude | Longitude | Notes |",
                        "|---|---|---|---|---|",
                        "| Los Angeles | KLAX | 33.94250 | -118.40806 | Good |",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            client = MarketClient(station_file_path=station_file)
            station_location = {
                "city": "LaGuardia Airport",
                "lat": 40.7769,
                "lon": -73.874,
                "is_us": True,
                "country_code": "US",
                "country": "United States",
                "continent": "North America",
                "timezone": "America/New_York",
            }

            with patch.object(client, "_geocode_city", return_value=station_location) as geocode_mock:
                location = client.parse_market_location(
                    "Highest temperature in NYC on May 17? :: Will the highest temperature in New York City be between 86-87F on May 17?"
                )

            self.assertIsNotNone(location)
            self.assertAlmostEqual(location["lat"], 40.7769)
            self.assertAlmostEqual(location["lon"], -73.874)
            geocode_mock.assert_called_once()


class SignalTimingGateTests(unittest.TestCase):
    def test_us_same_day_entries_are_blocked_before_8am_local(self):
        generator = SignalGenerator()

        gate = generator._entry_timing_gate(
            {
                "country_code": "US",
                "local_date": "2026-05-17",
                "local_hour": 7,
                "timezone": "America/New_York",
            },
            date(2026, 5, 17),
        )

        self.assertTrue(gate["blocked"])
        self.assertIn("8:00 AM", gate["reason"])

    def test_non_us_or_post_8am_entries_are_not_blocked(self):
        generator = SignalGenerator()

        self.assertFalse(
            generator._entry_timing_gate(
                {
                    "country_code": "US",
                    "local_date": "2026-05-17",
                    "local_hour": 8,
                    "timezone": "America/New_York",
                },
                date(2026, 5, 17),
            )["blocked"]
        )
        self.assertFalse(
            generator._entry_timing_gate(
                {
                    "country_code": "CA",
                    "local_date": "2026-05-17",
                    "local_hour": 3,
                    "timezone": "America/Toronto",
                },
                date(2026, 5, 17),
            )["blocked"]
        )


if __name__ == "__main__":
    unittest.main()
