import tempfile
import unittest
from datetime import date, datetime
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
    def test_market_date_must_match_city_local_date(self):
        generator = SignalGenerator()
        location = {"timezone": "Europe/London"}

        with patch("brain.signals.datetime") as datetime_mock:
            datetime_mock.now.return_value = datetime.fromisoformat("2026-05-21T23:30:00+01:00")

            self.assertTrue(
                generator._is_current_local_market_date(location, date(2026, 5, 21))
            )
            self.assertFalse(
                generator._is_current_local_market_date(location, date(2026, 5, 20))
            )
            self.assertFalse(
                generator._is_current_local_market_date(location, date(2026, 5, 22))
            )

    def test_market_date_uses_city_rollover_when_city_reaches_next_day(self):
        generator = SignalGenerator()
        location = {"timezone": "Asia/Tokyo"}

        with patch("brain.signals.datetime") as datetime_mock:
            datetime_mock.now.return_value = datetime.fromisoformat("2026-05-22T00:30:00+09:00")

            self.assertTrue(
                generator._is_current_local_market_date(location, date(2026, 5, 22))
            )
            self.assertFalse(
                generator._is_current_local_market_date(location, date(2026, 5, 21))
            )

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

    @patch.dict("os.environ", {}, clear=True)
    def test_live_trading_defaults_to_us_only(self):
        generator = SignalGenerator()

        self.assertEqual(generator.live_market_scope, "us")
        self.assertTrue(generator._is_live_tradeable_location({"is_us": True}))
        self.assertFalse(generator._is_live_tradeable_location({"is_us": False}))

    @patch.dict("os.environ", {"BLOCKY_US_ONLY_TRADING": "0"}, clear=False)
    def test_live_trading_can_allow_non_us_when_flag_disabled(self):
        generator = SignalGenerator()
        generator.live_market_scope = "all"
        generator.us_only_trading = False

        self.assertTrue(generator._is_live_tradeable_location({"is_us": False}))

    @patch.dict("os.environ", {"BLOCKY_LIVE_MARKET_SCOPE": "NON_US"}, clear=False)
    def test_live_trading_can_be_limited_to_non_us(self):
        generator = SignalGenerator()

        self.assertEqual(generator.live_market_scope, "non_us")
        self.assertFalse(generator._is_live_tradeable_location({"is_us": True}))
        self.assertTrue(generator._is_live_tradeable_location({"is_us": False}))

    @patch.dict("os.environ", {"BLOCKY_LIVE_MARKET_SCOPE": "ALL"}, clear=False)
    def test_live_trading_can_allow_all_markets(self):
        generator = SignalGenerator()

        self.assertEqual(generator.live_market_scope, "all")
        self.assertTrue(generator._is_live_tradeable_location({"is_us": True}))
        self.assertTrue(generator._is_live_tradeable_location({"is_us": False}))

    @patch.dict("os.environ", {"CLIME_LIVE_MARKET_SCOPE": "NON_US"}, clear=False)
    def test_live_trading_accepts_clime_scope_alias(self):
        generator = SignalGenerator()

        self.assertEqual(generator.live_market_scope, "non_us")
        self.assertFalse(generator._is_live_tradeable_location({"is_us": True}))
        self.assertTrue(generator._is_live_tradeable_location({"is_us": False}))

    def test_trade_price_cap_blocks_prices_above_point_eighty_five(self):
        generator = SignalGenerator()

        self.assertTrue(generator._is_trade_price_within_cap(0.85))
        self.assertFalse(generator._is_trade_price_within_cap(0.8501))
        self.assertTrue(generator._are_market_prices_within_cap(0.85, 0.15))
        self.assertTrue(generator._are_market_prices_within_cap(0.15, 0.85))
        self.assertFalse(generator._are_market_prices_within_cap(0.8501, 0.1499))
        self.assertFalse(generator._are_market_prices_within_cap(0.1499, 0.8501))

    def test_forecast_source_gate_requires_three_sources(self):
        generator = SignalGenerator()

        blocked = generator._forecast_source_gate({"noaa": 82.0})
        allowed = generator._forecast_source_gate({"noaa": 82.0, "hrrr": 81.8, "gfs": 82.3})

        self.assertTrue(blocked["blocked"])
        self.assertIn("minimum 3", blocked["reason"])
        self.assertFalse(allowed["blocked"])


if __name__ == "__main__":
    unittest.main()
