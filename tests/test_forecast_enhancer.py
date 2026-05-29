import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from brain.forecast_enhancer import ForecastEnhancer


class ForecastEnhancerTests(unittest.TestCase):
    def setUp(self):
        self.enhancer = ForecastEnhancer()

    def _hourly_payload(self, timezone_name, base_now, values_by_hour, field_name):
        tz = ZoneInfo(timezone_name)
        start_of_day = base_now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        times = []
        values = []
        for hour in range(24):
            times.append((start_of_day + timedelta(hours=hour)).isoformat())
            values.append(values_by_hour[hour])
        return {
            "timezone": timezone_name,
            "hourly": {
                "time": times,
                field_name: values,
            },
        }

    def _noaa_payload(self, timezone_name, base_now, values_by_hour):
        tz = ZoneInfo(timezone_name)
        start_of_day = base_now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        periods = []
        for hour in range(24):
            dt_value = start_of_day + timedelta(hours=hour)
            periods.append(
                {
                    "startTime": dt_value.isoformat(),
                    "temperature": values_by_hour[hour],
                    "isDaytime": 6 <= hour <= 20,
                }
            )
        return {
            "timezone": timezone_name,
            "hourly_periods": periods,
        }

    def test_us_enhancement_respects_metar_truth_anchor(self):
        timezone_name = "America/New_York"
        reference_now = datetime(2026, 5, 20, 13, 0, tzinfo=ZoneInfo(timezone_name))
        market_date = reference_now.date()

        noaa_hours = [18.0] * 24
        noaa_hours[13] = 22.0
        noaa_hours[15] = 28.5

        hrrr_hours = [17.8] * 24
        hrrr_hours[13] = 21.8
        hrrr_hours[15] = 28.2

        openmeteo_hours = [18.3] * 24
        openmeteo_hours[13] = 21.7
        openmeteo_hours[15] = 27.8

        bundle = {
            "timezone": timezone_name,
            "noaa": self._noaa_payload(timezone_name, reference_now, noaa_hours),
            "hrrr": self._hourly_payload(timezone_name, reference_now, hrrr_hours, "temperature_2m_hrrr"),
            "gfs": self._hourly_payload(timezone_name, reference_now, openmeteo_hours, "temperature_2m_gfs_seamless"),
            "open_meteo": self._hourly_payload(timezone_name, reference_now, openmeteo_hours, "temperature_2m"),
            "metar": {
                "temp": 24.6,
                "obsTime": "2026-05-20T16:30:00+00:00",
            },
        }

        result = self.enhancer.enhance(bundle, market_date, is_us=True, timezone_name=timezone_name, reference_now=reference_now)

        self.assertGreaterEqual(result["adjusted_high_temperature"], 24.6)
        self.assertIn("noaa", result["source_adjustments"])
        self.assertGreater(result["confidence_score"], 0.5)
        self.assertLessEqual(result["confidence_score"], 1.0)

    def test_non_us_enhancement_prefers_ecmwf_with_metar_bias_correction(self):
        timezone_name = "Europe/Paris"
        reference_now = datetime(2026, 5, 20, 11, 0, tzinfo=ZoneInfo(timezone_name))
        market_date = reference_now.date()

        ecmwf_hours = [15.0] * 24
        ecmwf_hours[11] = 18.7
        ecmwf_hours[15] = 21.0

        gfs_hours = [15.0] * 24
        gfs_hours[11] = 17.9
        gfs_hours[15] = 20.1

        openmeteo_hours = [15.0] * 24
        openmeteo_hours[11] = 18.1
        openmeteo_hours[15] = 20.4

        bundle = {
            "timezone": timezone_name,
            "ecmwf": self._hourly_payload(timezone_name, reference_now, ecmwf_hours, "temperature_2m_ecmwf_ifs025"),
            "gfs": self._hourly_payload(timezone_name, reference_now, gfs_hours, "temperature_2m_gfs_seamless"),
            "open_meteo": self._hourly_payload(timezone_name, reference_now, openmeteo_hours, "temperature_2m"),
            "metar": {
                "temp": 19.4,
                "obsTime": "2026-05-20T09:30:00+00:00",
            },
        }

        result = self.enhancer.enhance(bundle, market_date, is_us=False, timezone_name=timezone_name, reference_now=reference_now)

        self.assertGreater(result["adjusted_high_temperature"], 20.4)
        self.assertGreater(result["source_temperatures"]["ecmwf"], result["source_temperatures"]["gfs"])
        self.assertGreater(result["confidence_score"], 0.45)

    def test_empty_sources_return_minimal_payload(self):
        result = self.enhancer.enhance({}, "2026-05-20", is_us=False, timezone_name="UTC")

        self.assertIsNone(result["adjusted_high_temperature"])
        self.assertEqual(result["confidence_score"], 0.0)
        self.assertEqual(result["source_adjustments"], {})

    def test_integer_consensus_drops_one_of_four_outlier(self):
        corrected_sources = {
            "noaa": 88.0,
            "hrrr": 94.4,
            "gfs": 94.4,
            "openmeteo": 94.4,
        }

        selected = self.enhancer._select_integer_consensus_sources(
            corrected_sources,
            self.enhancer.US_SOURCE_WEIGHTS,
        )

        self.assertEqual(selected, {
            "hrrr": 94.4,
            "gfs": 94.4,
            "openmeteo": 94.4,
        })

    def test_integer_consensus_drops_one_of_three_outlier(self):
        corrected_sources = {
            "ecmwf": 16.1,
            "gfs": 18.12,
            "openmeteo": 18.17,
        }

        selected = self.enhancer._select_integer_consensus_sources(
            corrected_sources,
            self.enhancer.NON_US_SOURCE_WEIGHTS,
        )

        self.assertEqual(selected, {
            "gfs": 18.12,
            "openmeteo": 18.17,
        })

    def test_integer_consensus_keeps_all_sources_without_majority(self):
        corrected_sources = {
            "noaa": 78.0,
            "hrrr": 83.52,
            "gfs": 84.1,
            "openmeteo": 85.0,
        }

        selected = self.enhancer._select_integer_consensus_sources(
            corrected_sources,
            self.enhancer.US_SOURCE_WEIGHTS,
        )

        self.assertEqual(selected, corrected_sources)


if __name__ == "__main__":
    unittest.main()
