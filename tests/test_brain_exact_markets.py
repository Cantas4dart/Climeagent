import unittest

from brain.model import TradingModel
from brain.signals import SignalGenerator


class ExactMarketProbabilityTests(unittest.TestCase):
    def setUp(self):
        self.model = TradingModel()

    def test_exact_ladder_probability_stays_yes_when_all_forecasts_round_to_target(self):
        target = {"type": "exact", "val": 20.0}

        avg_prob, spread, stats = self.model.calculate_ensemble_probability(
            {"ecmwf": 19.6, "gfs": 19.8},
            target,
        )

        self.assertGreater(avg_prob, 0.50)
        self.assertGreaterEqual(stats["max_prob"], stats["min_prob"])
        self.assertGreaterEqual(spread, 0.0)

    def test_exact_ladder_probability_handles_mixed_target_and_rounding_match(self):
        target = {"type": "exact", "val": 20.0}

        avg_prob, _, _ = self.model.calculate_ensemble_probability(
            {"ecmwf": 20.2, "gfs": 20.0},
            target,
        )

        self.assertGreater(avg_prob, 0.50)

    def test_exact_ladder_consensus_boost_handles_21_point_4_and_21_point_2(self):
        target = {"type": "exact", "val": 21.0}

        avg_prob, _, _ = self.model.calculate_ensemble_probability(
            {"ecmwf": 21.4, "gfs": 21.2},
            target,
        )

        self.assertGreater(avg_prob, 0.5745)

    def test_madrid_style_exact_ladder_stays_on_yes_side(self):
        target = {"type": "exact", "val": 13.0}

        avg_prob, _, _ = self.model.calculate_ensemble_probability(
            {"ecmwf": 13.4, "gfs": 13.3},
            target,
        )

        self.assertGreater(avg_prob, 0.50)

    def test_veto_skips_protected_exact_rounding_consensus(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.74,
            spread=0.01,
            market_price=0.56,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_NEAR_PEAK,
                "market_date": "2026-05-20",
                "target": {"type": "exact", "val": 20.0},
                "temp_dispersion": 0.05,
                "calibration_buckets": {},
                "settlement_risk": 0.0,
                "rounding_risk": 0.0,
                "station_mismatch_risk": 0.0,
                "observation_progress": 0.9,
                "exact_rounding_protected": True,
            },
        )

        self.assertFalse(decision["yes_veto_applied"])
        self.assertFalse(decision["pattern_veto_applied"])

    def test_protected_exact_rounding_skips_bayesian_calibration_flip(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.61,
            spread=0.01,
            market_price=0.17,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_POST_PEAK,
                "market_date": "2026-05-22",
                "target": {"type": "exact", "val": 24.0},
                "temp_dispersion": 0.02,
                "calibration_buckets": {
                    "60-69": {"count": 40, "wins": 0},
                },
                "settlement_risk": 0.0,
                "rounding_risk": 0.0,
                "station_mismatch_risk": 0.0,
                "observation_progress": 0.9,
                "exact_rounding_protected": True,
            },
        )

        self.assertEqual(decision["action"], "BUY_YES")
        self.assertAlmostEqual(decision["calibrated_model_prob"], decision["adjusted_model_prob"], places=6)

    def test_price_band_thresholds_match_retuned_ranges(self):
        self.assertEqual(self.model._price_band(0.23), "preferred")
        self.assertEqual(self.model._price_band(0.85), "preferred")
        self.assertEqual(self.model._price_band(0.20), "selective")
        self.assertEqual(self.model._price_band(0.19), "extreme")

    def test_same_day_exact_near_peak_does_not_buy_no_when_still_on_rung_cluster(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.42,
            spread=0.01,
            market_price=0.39,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_NEAR_PEAK,
                "market_date": "2026-05-22",
                "target": {"type": "exact", "val": 24.0},
                "temp_dispersion": 0.08,
                "calibration_buckets": {},
                "settlement_risk": 0.0,
                "rounding_risk": 0.0,
                "station_mismatch_risk": 0.0,
                "observation_progress": 0.9,
                "exact_rounding_consensus": 1.0,
                "exact_rounding_protected": False,
                "exact_target_distance": 0.12,
            },
        )

        self.assertEqual(decision["action"], "BUY_YES")

    def test_exact_safety_blocks_fractional_danger_zone(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.74,
            spread=0.01,
            market_price=0.45,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_NEAR_PEAK,
                "target": {"type": "exact", "val": 31.0},
                "forecast_avg": 31.45,
                "temp_dispersion": 0.04,
                "calibration_buckets": {},
                "settlement_risk": 0.0,
                "rounding_risk": 0.0,
                "station_mismatch_risk": 0.0,
                "observation_progress": 0.9,
                "exact_target_distance": 0.45,
            },
        )

        self.assertFalse(decision["should_trade"])
        self.assertTrue(any("Exact safety block" in reason for reason in decision["reasons"]))

    def test_exact_safety_blocks_high_dispersion(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.74,
            spread=0.01,
            market_price=0.45,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_NEAR_PEAK,
                "target": {"type": "exact", "val": 31.0},
                "forecast_avg": 31.20,
                "temp_dispersion": 0.26,
                "calibration_buckets": {},
                "settlement_risk": 0.0,
                "rounding_risk": 0.0,
                "station_mismatch_risk": 0.0,
                "observation_progress": 0.9,
                "exact_target_distance": 0.20,
            },
        )

        self.assertFalse(decision["should_trade"])
        self.assertTrue(any("dispersion" in reason for reason in decision["reasons"]))

    def test_integer_range_probability_uses_whole_degree_bucket_semantics(self):
        target = {"type": "range", "low": 86.0, "high": 87.0}

        bucket_prob = self.model.calculate_probability(85.54, target, std_dev=1.5)
        looser_bucket_prob = self.model.calculate_probability(85.54, {"type": "range", "low": 86.0, "high": 88.0}, std_dev=1.5)
        tighter_bucket_prob = self.model.calculate_probability(85.54, {"type": "range", "low": 86.0, "high": 86.0}, std_dev=1.5)

        self.assertGreater(bucket_prob, tighter_bucket_prob)
        self.assertLess(bucket_prob, looser_bucket_prob)

    def test_integer_below_threshold_probability_covers_the_full_degree(self):
        target = {"type": "threshold", "direction": "below", "val": 72.0}

        prob = self.model.calculate_probability(72.6, target, std_dev=0.5)
        next_degree_prob = self.model.calculate_probability(72.6, {"type": "threshold", "direction": "below", "val": 73.0}, std_dev=0.5)

        self.assertGreater(prob, 0.5)
        self.assertLess(prob, next_degree_prob)

    def test_range_threshold_safety_applies_low_dispersion_buffer(self):
        reason, multiplier = self.model._assess_range_threshold_safety(
            {
                "target": {"type": "range", "low": 72.0, "high": 73.0},
                "forecast_avg": 72.6,
                "temp_dispersion": 0.03,
            },
            self.model.REGIME_NEAR_PEAK,
        )

        self.assertEqual(reason, "Low-dispersion range settlement buffer")
        self.assertEqual(multiplier, 1.03)

    def test_threshold_safety_adds_near_peak_boundary_conservatism(self):
        reason, multiplier = self.model._assess_range_threshold_safety(
            {
                "target": {"type": "threshold", "direction": "above", "val": 72.0},
                "forecast_avg": 72.2,
                "temp_dispersion": 0.03,
            },
            self.model.REGIME_NEAR_PEAK,
        )

        self.assertEqual(reason, "Threshold near-boundary late-day conservatism")
        self.assertEqual(multiplier, 0.95)

    def test_same_day_us_range_wrong_side_cluster_does_not_buy_yes(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.4727,
            spread=0.0122,
            market_price=0.2930,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_NEAR_PEAK,
                "target": {"type": "range", "low": 86.0, "high": 87.0},
                "country_code": "US",
                "forecast_avg": 85.54,
                "forecast_min": 85.50,
                "forecast_max": 85.58,
                "temp_dispersion": 0.034,
                "calibration_buckets": {},
                "settlement_risk": 0.282,
                "rounding_risk": 0.18,
                "station_mismatch_risk": 0.06,
                "observation_progress": 0.70,
            },
        )

        self.assertEqual(decision["action"], "BUY_NO")

    def test_same_day_us_above_threshold_wrong_side_cluster_does_not_buy_yes(self):
        decision = self.model.evaluate_market_opportunity(
            model_prob=0.58,
            spread=0.01,
            market_price=0.41,
            market_context={
                "days_to_resolution": 0,
                "local_peak_stage": self.model.REGIME_NEAR_PEAK,
                "target": {"type": "threshold", "direction": "above", "val": 72.0},
                "country_code": "US",
                "forecast_avg": 71.40,
                "forecast_min": 71.20,
                "forecast_max": 71.60,
                "temp_dispersion": 0.03,
                "calibration_buckets": {},
                "settlement_risk": 0.20,
                "rounding_risk": 0.12,
                "station_mismatch_risk": 0.06,
                "observation_progress": 0.70,
            },
        )

        self.assertEqual(decision["action"], "BUY_NO")


class TemperatureTargetParsingTests(unittest.TestCase):
    def setUp(self):
        self.generator = SignalGenerator()

    def test_extract_target_parses_or_above_as_above_threshold(self):
        target = self.generator.extract_target(
            "Will the highest temperature in Dallas be 72F or above on May 23?"
        )

        self.assertEqual(target, {"type": "threshold", "direction": "above", "val": 72.0})


if __name__ == "__main__":
    unittest.main()
