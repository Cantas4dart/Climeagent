import math
from collections import Counter


class TradingModel:
    """
    Calibrated Trading Model focused on consistency first.

    Strategy:
    - Detect contract regime from time-to-resolution, probability location, and ensemble spread.
    - Use bust-risk-adjusted probabilities instead of hard blocking extreme probabilities.
    - Keep spread as a hard quality filter in noisy regimes and relax it when variance collapses.
    - Preserve confidence-weighted sizing while allowing more late-stage edge capture.
    - Apply harsh penalties for high temperature dispersion (forecast model disagreement).
    - Use Bayesian calibration buckets to adjust final probabilities based on historical accuracy.
    """

    REGIME_PRE_PEAK = "pre_peak"
    REGIME_NEAR_PEAK = "near_peak"
    REGIME_POST_PEAK = "post_peak"

    STANDARD_EDGE_MID_RANGE = 0.10
    STANDARD_EDGE_TAIL_RANGE = 0.12
    STANDARD_SPREAD_MAX = 0.12
    EXTREME_EDGE_THRESHOLD = 0.25
    EXTREME_EDGE_FLOOR = 0.15
    EXTREME_SPREAD_MAX = 0.18
    MIN_CONFIDENCE_SCORE = 0.80
    SELECTIVE_CONFIDENCE_SCORE = 0.85
    EXTREME_CONFIDENCE_SCORE = 0.90
    EXACT_MIN_CONFIDENCE_SCORE = 0.89
    EXACT_SELECTIVE_CONFIDENCE_SCORE = 0.93
    RANGE_THRESHOLD_CONFIDENCE_BUMP = 0.02
    PREFERRED_PRICE_LOW = 0.23
    PREFERRED_PRICE_HIGH = 0.85
    SELECTIVE_PRICE_LOW = 0.20
    SELECTIVE_PRICE_HIGH = 0.87
    
    # Dispersion penalty thresholds
    HIGH_DISPERSION_THRESHOLD = 0.30
    EXTREME_DISPERSION_THRESHOLD = 0.50

    def __init__(self, risk_percent=0.01, stats_log_interval=50):
        self.risk_percent = risk_percent
        self.stats_log_interval = max(int(stats_log_interval or 0), 0)
        self.decision_stats = Counter()
        self.reject_reason_stats = Counter()

    def _reason_category(self, reason):
        if "Temperature dispersion" in reason:
            return "temperature_dispersion"
        if "Reporting-station mismatch" in reason:
            return "station_mismatch"
        if "Settlement sensitivity" in reason:
            return "settlement_risk"
        if "Threshold/rounding risk" in reason:
            return "rounding_risk"
        if "Confidence" in reason:
            return "confidence"
        if "Edge" in reason:
            return "edge"
        if "Market price" in reason:
            return "extreme_price"
        if "Calibrated prob" in reason:
            return "probability_bounds"
        if "Forecast spread" in reason:
            return "spread"
        if "Forecast drift" in reason:
            return "forecast_drift"
        if "Observation progress" in reason:
            return "observation_progress"
        return reason.split(":")[0]

    def _record_decision_stats(self, should_trade, reasons, regime, trade_side):
        self.decision_stats["evaluated"] += 1
        self.decision_stats[f"regime:{regime}"] += 1
        self.decision_stats[f"side:{trade_side}"] += 1
        self.decision_stats["accepted" if should_trade else "rejected"] += 1

        categories = []
        if not should_trade:
            for reason in reasons:
                category = self._reason_category(reason)
                self.reject_reason_stats[category] += 1
                categories.append(category)

        if self.stats_log_interval and self.decision_stats["evaluated"] % self.stats_log_interval == 0:
            top_reasons = ", ".join(
                f"{name}={count}" for name, count in self.reject_reason_stats.most_common(5)
            ) or "none"
            print(
                "[MODEL]   Decision stats: "
                f"evaluated={self.decision_stats['evaluated']}, "
                f"accepted={self.decision_stats['accepted']}, "
                f"rejected={self.decision_stats['rejected']}, "
                f"top_rejects={top_reasons}"
            )

        return categories

    def bayesian_calibration_adjustment(self, prob, calibration_buckets):
        """
        Adjust probability based on historical win rates in calibration buckets.
        Pulls extreme probabilities toward the empirical frequency they actually resolved to.
        """
        if not calibration_buckets:
            return prob
        
        # Find the appropriate bucket (0-9, 10-19, etc.)
        bucket_index = int(prob * 100) // 10
        bucket_key = f"{bucket_index * 10:02d}-{bucket_index * 10 + 9:02d}"
        
        bucket_data = calibration_buckets.get(bucket_key, {})
        count = int(bucket_data.get("count", 0))
        wins = float(bucket_data.get("wins", 0))
        
        if count < 5:  # Not enough data for this bucket
            return prob
        
        empirical_rate = wins / count
        midpoint = (bucket_index * 10 + 5) / 100.0
        
        # Blend: higher count = more trust the empirical rate
        # But keep most of the model's view - historical losses may reflect market mispricing, not forecast error
        # CRITICAL FIX: Reduced from 0.50 to 0.20 to prevent calibration from overriding valid forecasts
        # Example: Denver 69°F >= 68°F forecast shouldn't be pulled down 19% by historical market losses
        blend_weight = min(count / 40.0, 0.20)  # Cap at 20% weight from empirical (was 50%)
        adjusted = prob * (1 - blend_weight) + empirical_rate * blend_weight
        
        return adjusted

    def _dispersion_adjustment_penalty(self, spread, temp_dispersion):
        """
        Apply harsh penalties when temperature models disagree (high dispersion).
        High dispersion trades have been losing significantly.
        """
        if temp_dispersion is None or temp_dispersion <= 0.02:
            return 1.0  # No penalty
        
        # Quadratic penalty: small dispersion = small penalty, high dispersion = severe penalty
        if temp_dispersion >= self.EXTREME_DISPERSION_THRESHOLD:
            return 0.40  # Severe - only take if edge is massive
        elif temp_dispersion >= self.HIGH_DISPERSION_THRESHOLD:
            return 0.55  # Harsh - reduce confidence significantly
        else:
            # Linear ramp from 1.0 at 0.02 to 0.55 at 0.10
            return 1.0 - (0.45 * (temp_dispersion - 0.02) / (self.HIGH_DISPERSION_THRESHOLD - 0.02))

    @staticmethod
    def _round_half_away_from_zero(value):
        value = float(value)
        if value >= 0:
            return math.floor(value + 0.5)
        return math.ceil(value - 0.5)

    def _is_whole_degree_exact_target(self, target):
        target = target or {}
        if str(target.get("type") or "") != "exact":
            return False
        target_val = float(target.get("val", 0.0))
        return math.isclose(target_val, round(target_val), abs_tol=1e-9)

    def _exact_rounding_alignment(self, forecast_temp, target):
        """
        Detect whole-degree exact ladders where the forecast already rounds to the
        exact settlement rung.
        """
        if not self._is_whole_degree_exact_target(target):
            return 0.0

        target_val = float(target.get("val", 0.0))
        if float(self._round_half_away_from_zero(forecast_temp)) != target_val:
            return 0.0

        distance = abs(float(forecast_temp) - target_val)
        return max(0.0, 1.0 - min(distance / 0.75, 1.0))

    def _exact_rounding_consensus_bonus(self, forecast_data, target):
        """
        Add a narrow ensemble-level boost when all forecast sources independently
        round to the same whole-degree exact rung and remain tightly clustered
        around it.
        """
        if not self._is_whole_degree_exact_target(target):
            return 0.0
        if not forecast_data:
            return 0.0

        target_val = float(target.get("val", 0.0))
        distances = []
        for temp in forecast_data.values():
            temp = float(temp)
            if float(self._round_half_away_from_zero(temp)) != target_val:
                return 0.0
            distance = abs(temp - target_val)
            if distance > 0.5:
                return 0.0
            distances.append(distance)

        if not distances:
            return 0.0

        avg_distance = sum(distances) / len(distances)
        closeness = max(0.0, 1.0 - min(avg_distance / 0.75, 1.0))
        consensus = min(len(distances) / 2.0, 1.0)
        return 0.11 * closeness * consensus

    def normal_cdf(self, x, mean, std_dev):
        """Standard normal CDF using the error function."""
        std_dev = max(float(std_dev), 0.25)
        return 0.5 * (1 + math.erf((x - mean) / (std_dev * math.sqrt(2))))

    def _continuous_probability(self, mean, target, std_dev):
        if target["type"] == "threshold":
            val = float(target["val"])
            direction = target.get("direction", "above")
            if direction == "below":
                # Integer "or below" ladders stop at .9, not the next whole degree.
                boundary = val + 0.9 if float(val).is_integer() else val
                return self.normal_cdf(boundary, mean, std_dev)
            return 1 - self.normal_cdf(val, mean, std_dev)

        if target["type"] == "range":
            low = float(target["low"])
            high = float(target["high"])
            if float(low).is_integer() and float(high).is_integer():
                # Integer range ladders settle on whole-degree buckets:
                # 86-87 means [86.0, 87.9], not a rounded bucket into 88.
                return self.normal_cdf(high + 0.9, mean, std_dev) - self.normal_cdf(low, mean, std_dev)
            return self.normal_cdf(high, mean, std_dev) - self.normal_cdf(low, mean, std_dev)

        if target["type"] == "exact":
            val = float(target["val"])
            half_width = 0.5
            if abs(mean - val) <= 0.5:
                # When the forecast is very close to the exact integer, give the
                # exact-settlement probability a slightly wider effective window.
                half_width = 0.75
            return self.normal_cdf(val + half_width, mean, std_dev) - self.normal_cdf(val - half_width, mean, std_dev)

        return 0.0

    def _discrete_probability(self, mean, target, std_dev):
        """
        Concentrate probability mass on whole-number outcomes to reflect how
        official temperature reports often round or cluster around integers.
        """
        support = range(math.floor(mean - 5), math.ceil(mean + 6))
        masses = []
        total_mass = 0.0
        for value in support:
            upper = value + 0.5
            lower = value - 0.5
            mass = self.normal_cdf(upper, mean, std_dev) - self.normal_cdf(lower, mean, std_dev)
            mass = max(0.0, mass)
            masses.append((float(value), mass))
            total_mass += mass

        if total_mass <= 0:
            return self._continuous_probability(mean, target, std_dev)

        discrete_prob = 0.0
        for value, mass in masses:
            normalized_mass = mass / total_mass
            if target["type"] == "threshold":
                direction = target.get("direction", "above")
                if direction == "below" and value <= float(target["val"]):
                    discrete_prob += normalized_mass
                elif direction != "below" and value >= float(target["val"]):
                    discrete_prob += normalized_mass
            elif target["type"] == "range":
                if float(target["low"]) <= value <= float(target["high"]):
                    discrete_prob += normalized_mass
            elif target["type"] == "exact":
                if value == float(target["val"]):
                    discrete_prob += normalized_mass

        return discrete_prob

    def calculate_probability(self, forecast_temp, target, std_dev=1.5):
        """
        Blend continuous and discrete temperature distributions so threshold and
        exact/range markets benefit from integer clustering around report values.
        """
        mean = float(forecast_temp)
        exact_rounding_alignment = self._exact_rounding_alignment(mean, target)
        effective_std_dev = float(std_dev)
        if exact_rounding_alignment > 0.0:
            # Whole-degree exact ladders settle on the rounded official report,
            # so forecasts already rounding to the target should keep more mass
            # on that rung instead of being diluted like generic decimal exacts.
            effective_std_dev *= 0.55

        continuous_prob = self._continuous_probability(mean, target, effective_std_dev)
        discrete_prob = self._discrete_probability(mean, target, effective_std_dev)

        distance_to_integer = abs(mean - round(mean))
        discrete_weight = 0.30 if distance_to_integer <= 0.20 else 0.18
        if target["type"] in {"range", "threshold"} and distance_to_integer <= 0.15:
            discrete_weight += 0.04
        if target["type"] in {"range", "exact"}:
            discrete_weight += 0.10
        if target["type"] == "exact" and round(mean) == float(target["val"]):
            # For exact markets, if the forecast rounds to the exact integer,
            # give integer clustering a bit more influence on the probability.
            discrete_weight += 0.05
        if exact_rounding_alignment > 0.0:
            discrete_weight += 0.20 * exact_rounding_alignment

        blended = ((1 - discrete_weight) * continuous_prob) + (discrete_weight * discrete_prob)
        return min(max(blended, 0.0), 1.0)

    def calculate_ensemble_probability(self, forecast_data, target, default_std_dev=1.5):
        """Consensus logic from multiple models."""
        probs = []

        for model_name, temp in forecast_data.items():
            prob = self.calculate_probability(temp, target, std_dev=default_std_dev)
            probs.append(prob)
            print(f"[MODEL]   {model_name}: temp={temp}, prob={prob:.2%}")

        if not probs:
            return 0.0, 1.0, {}

        avg_prob = sum(probs) / len(probs)
        avg_prob += self._exact_rounding_consensus_bonus(forecast_data, target)
        avg_prob = min(max(avg_prob, 0.0), 1.0)
        spread = 0.0
        stats = {
            "count": len(probs),
            "min_prob": min(probs),
            "max_prob": max(probs),
        }

        if len(probs) > 1:
            spread = max(probs) - min(probs)
        print(f"[MODEL]   Ensemble spread: {spread:.2%}")
        stats["spread"] = spread

        return avg_prob, spread, stats

    def detect_regime(self, model_prob, spread, days_to_resolution, local_peak_stage=None):
        tail_confidence = max(model_prob, 1 - model_prob)

        # If local_peak_stage is explicitly provided from signal classification, respect it
        # unless we have strong evidence to override it (e.g., days_to_resolution <= 0)
        if local_peak_stage and days_to_resolution > 0:
            # If signal explicitly classifies stage and market is in future, trust the signal stage
            if local_peak_stage in {self.REGIME_PRE_PEAK, self.REGIME_NEAR_PEAK, self.REGIME_POST_PEAK}:
                return local_peak_stage

        if days_to_resolution <= 0:
            if local_peak_stage == self.REGIME_POST_PEAK:
                return self.REGIME_POST_PEAK
            if local_peak_stage == self.REGIME_NEAR_PEAK:
                return self.REGIME_NEAR_PEAK
            if local_peak_stage == self.REGIME_PRE_PEAK and spread > 0.05 and tail_confidence < 0.94:
                return self.REGIME_NEAR_PEAK

        if days_to_resolution <= 1 and (spread <= 0.05 or tail_confidence >= 0.94):
            return self.REGIME_POST_PEAK
        if days_to_resolution <= 2 and (spread <= 0.09 or tail_confidence >= 0.86):
            return self.REGIME_NEAR_PEAK
        return self.REGIME_PRE_PEAK

    def probability_bounds(self, regime):
        if regime == self.REGIME_POST_PEAK:
            return 0.05, 0.95
        if regime == self.REGIME_NEAR_PEAK:
            return 0.10, 0.90
        return 0.20, 0.80

    def spread_limit(self, regime):
        if regime == self.REGIME_POST_PEAK:
            return 0.24
        if regime == self.REGIME_NEAR_PEAK:
            return 0.16
        return 0.10

    def _probability_region(self, model_prob):
        if 0.30 <= model_prob <= 0.70:
            return "mid"
        if 0.10 <= model_prob <= 0.90:
            return "tail"
        return "extreme_tail"

    def _price_band(self, market_price):
        """
        Keep price-banding aligned with the stored signal metadata buckets so
        model-side veto patterns and paper-trade review are speaking the same
        language.
        """
        if self.PREFERRED_PRICE_LOW <= market_price <= self.PREFERRED_PRICE_HIGH:
            return "preferred"
        if self.SELECTIVE_PRICE_LOW <= market_price <= self.SELECTIVE_PRICE_HIGH:
            return "selective"
        return "extreme"

    def _should_block_exact_no_near_peak(self, calibrated_prob, market_context):
        target = (market_context or {}).get("target") or {}
        if str(target.get("type") or "") != "exact":
            return False, None

        local_peak_stage = str((market_context or {}).get("local_peak_stage") or "")
        if local_peak_stage not in {self.REGIME_NEAR_PEAK, self.REGIME_POST_PEAK}:
            return False, None

        days_to_resolution = max(int((market_context or {}).get("days_to_resolution", 3)), 0)
        if days_to_resolution > 0:
            return False, None

        exact_rounding_protected = bool((market_context or {}).get("exact_rounding_protected", False))
        exact_rounding_consensus = float((market_context or {}).get("exact_rounding_consensus", 0.0) or 0.0)
        exact_target_distance = abs(float((market_context or {}).get("exact_target_distance", 999.0) or 999.0))
        temp_dispersion = float((market_context or {}).get("temp_dispersion", 0.0) or 0.0)

        no_prob = 1.0 - float(calibrated_prob)
        if no_prob < 0.50:
            return False, None

        if exact_rounding_protected:
            return True, "Protected exact-rounding consensus keeps this same-day ladder on the YES side"

        if exact_rounding_consensus >= 0.67 and exact_target_distance <= 0.35 and temp_dispersion <= 0.20:
            return True, (
                "Exact ladder is too close to the settlement rung during local peak/post-peak "
                "to justify a NO entry"
            )

        return False, None

    def _should_block_same_day_temperature_yes_wrong_side(self, calibrated_prob, market_price, market_context):
        market_context = market_context or {}
        target = market_context.get("target") or {}
        target_type = str(target.get("type") or "")
        if target_type not in {"range", "threshold"}:
            return False, None

        country_code = str(market_context.get("country_code") or "").upper()
        if country_code != "US":
            return False, None

        days_to_resolution = max(int(market_context.get("days_to_resolution", 3)), 0)
        if days_to_resolution > 0:
            return False, None

        forecast_avg = market_context.get("forecast_avg")
        forecast_min = market_context.get("forecast_min")
        forecast_max = market_context.get("forecast_max")
        if forecast_avg is None or forecast_min is None or forecast_max is None:
            return False, None

        forecast_avg = float(forecast_avg)
        forecast_min = float(forecast_min)
        forecast_max = float(forecast_max)
        yes_prob = float(calibrated_prob)
        if yes_prob <= float(market_price):
            return False, None

        if target_type == "range":
            low = target.get("low")
            high = target.get("high")
            if low is None or high is None:
                return False, None
            low = float(low)
            high = float(high)
            range_ceiling = high + 0.9 if float(high).is_integer() else high

            if forecast_max < low and forecast_avg <= (low - 0.30):
                return True, (
                    f"All forecasts remain below the {low:.1f} range floor for this same-day US ladder"
                )
            if forecast_min > range_ceiling and forecast_avg >= (range_ceiling + 0.30):
                return True, (
                    f"All forecasts remain above the {range_ceiling:.1f} range ceiling for this same-day US ladder"
                )
            return False, None

        threshold_val = target.get("val")
        if threshold_val is None:
            return False, None
        threshold_val = float(threshold_val)
        direction = str(target.get("direction") or "above")
        if direction == "below":
            threshold_ceiling = threshold_val + 0.9 if float(threshold_val).is_integer() else threshold_val
            if forecast_min > threshold_ceiling and forecast_avg >= (threshold_ceiling + 0.30):
                return True, (
                    f"All forecasts remain above the {threshold_ceiling:.1f} threshold ceiling for this same-day US ladder"
                )
            return False, None

        if forecast_max < threshold_val and forecast_avg <= (threshold_val - 0.30):
            return True, (
            f"All forecasts remain below the {threshold_val:.1f} threshold floor for this same-day US ladder"
            )
        return False, None

    def _assess_exact_safety(self, market_context, spread):
        target = (market_context or {}).get("target") or {}
        if str(target.get("type") or "") != "exact":
            return None, 1.0

        forecast_mean = market_context.get("forecast_avg")
        if forecast_mean is None:
            return None, 1.0

        forecast_mean = float(forecast_mean)
        target_val = float(target.get("val", 0.0))
        raw_distance = (market_context or {}).get("exact_target_distance")
        distance_to_target = abs(float(raw_distance)) if raw_distance is not None else abs(forecast_mean - target_val)
        if math.isclose(distance_to_target, 0.0, abs_tol=1e-12):
            distance_to_target = abs(forecast_mean - target_val)
        fractional_part = forecast_mean - math.floor(forecast_mean)
        distance_to_integer = abs(forecast_mean - round(forecast_mean))
        temp_dispersion = float((market_context or {}).get("temp_dispersion", 0.0) or 0.0)

        if 0.35 <= fractional_part <= 0.65 and distance_to_target < 0.85:
            return (
                f"Exact safety block: fractional forecast {fractional_part:.2f} is in the adjacent-rung danger zone",
                0.0,
            )
        if temp_dispersion >= 0.26:
            return f"Exact safety block: temperature dispersion {temp_dispersion:.2f} is too high", 0.0
        if distance_to_target > 0.50:
            return f"Exact safety block: forecast is {distance_to_target:.2f} away from target rung", 0.0

        if float(spread or 0.0) > 0.18 and 0.30 < fractional_part < 0.70:
            return "Exact safety reduction: ensemble spread is high near a rounding boundary", 0.30

        safe_fraction = fractional_part <= 0.30 or fractional_part >= 0.70
        low_spread = float(spread or 0.0) <= 0.18
        if distance_to_integer > 0.50 or not low_spread or not safe_fraction:
            return (
                f"Exact safety block: setup is not clean_exact "
                f"(distance={distance_to_integer:.2f}, spread={float(spread or 0.0):.2%}, frac={fractional_part:.2f})",
                0.0,
            )

        return "clean_exact", 1.0

    def _assess_range_threshold_safety(self, market_context, regime):
        target = (market_context or {}).get("target") or {}
        target_type = str(target.get("type") or "")
        if target_type not in {"range", "threshold"}:
            return None, 1.0

        forecast_mean = market_context.get("forecast_avg")
        if forecast_mean is None:
            return None, 1.0

        forecast_mean = float(forecast_mean)
        temp_dispersion = float((market_context or {}).get("temp_dispersion", 0.0) or 0.0)
        if temp_dispersion > 0.08:
            return None, 1.0

        if target_type == "threshold":
            threshold_val = float(target.get("val", 0.0))
            direction = str(target.get("direction") or "above")
            boundary = threshold_val + 0.9 if direction == "below" and threshold_val.is_integer() else threshold_val
            distance_to_boundary = abs(forecast_mean - boundary)
            if regime == self.REGIME_NEAR_PEAK and distance_to_boundary <= 0.35:
                return "Threshold near-boundary late-day conservatism", 0.95
            if distance_to_boundary >= 0.70:
                return "Low-dispersion threshold settlement buffer", 1.03

        if target_type == "range":
            low = float(target.get("low", 0.0))
            high = float(target.get("high", low))
            ceiling = high + 0.9 if low.is_integer() and high.is_integer() else high
            if low + 0.25 <= forecast_mean <= ceiling - 0.25:
                return "Low-dispersion range settlement buffer", 1.03

        return None, 1.0

    def _required_edge(self, model_prob, regime, market_price):
        region = self._probability_region(model_prob)
        price_band = self._price_band(market_price)
        
        # BEST ZONE BOOST: 10-19% probability range has 50% win rate
        # Relax edge requirements for this proven high-accuracy zone
        if 0.10 <= model_prob <= 0.19:
            base_edge = 0.06  # Reduced from 0.10-0.12 for mid-range
            if price_band == "preferred":
                return max(0.04, round(base_edge, 4)), region, price_band
        
        if region == "mid":
            base_edge = self.STANDARD_EDGE_MID_RANGE
        elif region == "tail":
            base_edge = self.STANDARD_EDGE_TAIL_RANGE
        else:
            base_edge = 0.16

        if region == "extreme_tail":
            if regime == self.REGIME_NEAR_PEAK:
                base_edge *= 1.00
            elif regime == self.REGIME_POST_PEAK:
                base_edge *= 0.90
            elif regime == self.REGIME_PRE_PEAK:
                base_edge *= 1.15
        else:
            if regime == self.REGIME_NEAR_PEAK:
                base_edge *= 0.80
            elif regime == self.REGIME_POST_PEAK:
                base_edge *= 0.60
            elif regime == self.REGIME_PRE_PEAK:
                base_edge *= 1.10

        if price_band == "selective":
            base_edge *= 1.10
        elif price_band == "extreme":
            base_edge *= 1.35

        return max(0.04, round(base_edge, 4)), region, price_band

    def _confidence_score(self, abs_edge, spread, mode, regime, market_price, temp_dispersion=None):
        edge_anchor = self.EXTREME_EDGE_THRESHOLD if mode == "extreme_mispricing" else 0.16
        spread_limit = self.EXTREME_SPREAD_MAX if mode == "extreme_mispricing" else self.spread_limit(regime)

        edge_score = min(abs_edge / edge_anchor, 1.0)
        spread_score = max(0.0, 1.0 - (spread / spread_limit)) if spread_limit > 0 else 0.0
        price_band = self._price_band(market_price)
        price_penalty = {
            "preferred": 0.0,
            "selective": 0.04,
            "extreme": 0.10,
        }[price_band]

        regime_bonus = {
            self.REGIME_PRE_PEAK: -0.03,
            self.REGIME_NEAR_PEAK: 0.02,
            self.REGIME_POST_PEAK: 0.05,
        }.get(regime, 0.0)

        score = (0.65 * edge_score) + (0.35 * spread_score) + regime_bonus - price_penalty
        
        # Apply dispersion penalty if temperature models disagreed significantly
        if temp_dispersion is not None:
            dispersion_multiplier = self._dispersion_adjustment_penalty(spread, temp_dispersion)
            score = score * dispersion_multiplier
        
        return round(min(max(score, 0.0), 1.0), 3)

    def _segment_confidence_floor(self, trade_side, regime, mode, price_band):
        # CRITICAL FIX: Data shows high confidence (0.96-1.0) has only 28.9% win rate
        # while confidence 0.89 has 66.7% win rate. Reduce overconfidence penalty.
        # FIX 2: Boost YES confidence floors by regime (+0.07 NEAR_PEAK, +0.03 POST_PEAK, +0.02 extreme)
        floor = {
            "preferred": self.MIN_CONFIDENCE_SCORE,
            "selective": self.SELECTIVE_CONFIDENCE_SCORE,
            "extreme": self.EXTREME_CONFIDENCE_SCORE,
        }[price_band]

        if trade_side == "YES":
            if regime == self.REGIME_POST_PEAK:
                floor = max(floor, 0.96 if price_band == "preferred" else 0.96)  # +0.03 from 0.93
            elif regime == self.REGIME_NEAR_PEAK:
                floor = max(floor, 0.94 if price_band == "preferred" else 0.92)  # +0.07 from 0.87

            if mode == "extreme_mispricing":
                floor = max(floor, 0.97 if price_band == "preferred" else 0.97)  # +0.02 from 0.95
        else:
            if regime == self.REGIME_NEAR_PEAK and price_band == "preferred":
                floor = max(floor, 0.84 if mode == "standard" else 0.86)
            elif regime == self.REGIME_POST_PEAK and price_band == "preferred":
                floor = max(floor, 0.82 if mode == "standard" else 0.88)
            elif price_band == "selective":
                floor = max(floor, 0.86 if mode == "standard" else 0.89)
            elif regime == self.REGIME_POST_PEAK and mode == "extreme_mispricing":
                floor = max(floor, 0.91)

            # Keep NO entries meaningfully selective but not overconfident
            floor = max(floor, 0.80)  # Reduced from 0.85 - lower confidence is actually better

        # Preserve stricter regime/side thresholds while keeping the floor bounded.
        floor = round(min(max(floor, 0.0), 0.995), 3)
        return floor

    def _segment_required_edge(self, base_required_edge, trade_side, regime, mode, price_band):
        adjustment = 0.0

        if trade_side == "YES":
            if regime == self.REGIME_POST_PEAK:
                adjustment += 0.03
            elif regime == self.REGIME_NEAR_PEAK:
                adjustment += 0.02

            if mode == "extreme_mispricing":
                adjustment += 0.04

            if price_band == "selective":
                adjustment += 0.03
            elif price_band == "extreme":
                adjustment += 0.05
        else:
            if regime == self.REGIME_NEAR_PEAK and price_band == "preferred":
                adjustment -= 0.005 if mode == "standard" else 0.0
            elif regime == self.REGIME_POST_PEAK and price_band == "preferred" and mode == "standard":
                adjustment += 0.01

            if price_band == "selective":
                adjustment += 0.01
            elif price_band == "extreme":
                adjustment += 0.02

        return round(max(0.04, base_required_edge + adjustment), 4)

    def _segment_spread_cap(self, base_spread_limit, trade_side, regime, mode, price_band):
        cap = base_spread_limit

        if trade_side == "YES":
            if regime == self.REGIME_POST_PEAK:
                cap = min(cap, 0.05 if price_band == "preferred" else 0.03)
            elif regime == self.REGIME_NEAR_PEAK and mode == "extreme_mispricing":
                cap = min(cap, 0.08 if price_band == "preferred" else 0.05)
        else:
            if regime == self.REGIME_NEAR_PEAK and price_band == "preferred":
                cap = min(cap, 0.10 if mode == "extreme_mispricing" else 0.08)
            elif regime == self.REGIME_POST_PEAK and price_band == "preferred":
                cap = min(cap, 0.11 if mode == "extreme_mispricing" else 0.09)
            elif price_band == "selective":
                cap = min(cap, 0.09 if mode == "extreme_mispricing" else 0.07)

        return round(max(0.02, cap), 4)

    def _estimate_bust_risk(self, model_prob, spread, confidence_score, days_to_resolution, regime):
        regime_base = {
            self.REGIME_PRE_PEAK: 0.075,
            self.REGIME_NEAR_PEAK: 0.040,
            self.REGIME_POST_PEAK: 0.018,
        }.get(regime, 0.05)

        time_factor = min(max((days_to_resolution + 1) / 4.0, 0.35), 1.25)
        spread_factor = 0.85 + min(spread / 0.20, 1.0) * 0.60
        confidence_factor = max(0.35, 1.10 - (0.60 * confidence_score))

        tail_confidence = max(model_prob, 1 - model_prob)
        reversal_factor = 1.15 if tail_confidence >= 0.90 else 1.0
        tail_factor = 1.0
        if tail_confidence >= 0.80:
            tail_factor += min((tail_confidence - 0.80) / 0.20, 1.0) * 0.50
        if tail_confidence >= 0.95:
            tail_factor += 0.20

        bust_risk = regime_base * time_factor * spread_factor * confidence_factor * reversal_factor * tail_factor
        return min(max(bust_risk, 0.005), 0.12)

    def _apply_bust_risk(self, model_prob, bust_risk):
        """
        Pull probability modestly back toward 50% to reflect reporting error,
        late revisions, or hidden settlement frictions.
        """
        adjusted_prob = (model_prob * (1 - bust_risk)) + (0.5 * bust_risk)
        return min(max(adjusted_prob, 0.0), 1.0)

    def _size_multiplier(self, confidence_score, regime):
        multiplier = 0.65
        if confidence_score >= 0.92:
            multiplier = 1.40
        elif confidence_score >= 0.84:
            multiplier = 1.12
        elif confidence_score >= 0.72:
            multiplier = 0.90

        if regime == self.REGIME_PRE_PEAK:
            multiplier *= 0.90
        elif regime == self.REGIME_POST_PEAK:
            multiplier *= 1.08

        return round(multiplier, 2)

    def get_edge(self, model_prob, market_price):
        return model_prob - market_price

    def _should_apply_temperature_pattern_veto(self, target, market_context=None):
        """
        Pattern vetoes are reserved for exact-temperature markets only.
        Threshold and range contracts should follow the forecast direction
        directly instead of being flipped by historical exact-market patterns.
        """
        target = target or {}
        market_context = market_context or {}
        if market_context.get("exact_rounding_protected"):
            return False
        return str(target.get("type") or "") == "exact"

    def _temperature_yes_overconfidence_veto(self, adjusted_prob, market_context):
        """
        CRITICAL FIX: YES trades are losing at 19.8% vs NO at 41.8%.
        Analysis of 136 trades shows YES is a consistent loser across all probability ranges.
        
        Strategy: 
        - Heavily discourage YES trades on EXACT markets only (ambiguous settlement risk)
        - Thresholds are clearer and don't need veto
        - Block YES at many probability ranges (they all lost 0%)
        - Only allow YES when edge is very large (>60%) and confidence lower
        """
        target = (market_context or {}).get("target") or {}
        regime = str((market_context or {}).get("regime") or "")
        if not regime:
            days_to_resolution = max(int((market_context or {}).get("days_to_resolution", 3)), 0)
            regime = self.detect_regime(
                adjusted_prob,
                0.0,
                days_to_resolution,
                local_peak_stage=(market_context or {}).get("local_peak_stage"),
            )
        target_type = str(target.get("type") or "")
        direction = str(target.get("direction") or "above")
        market_price = float((market_context or {}).get("market_price", 0.5))
        price_band = self._price_band(market_price)
        if not regime:
            return adjusted_prob, None

        if not self._should_apply_temperature_pattern_veto(target, market_context) or direction == "below":
            return adjusted_prob, None

        matched_pattern = None
        
        # AGGRESSIVE YES BLOCKING - All these patterns lost 0/X trades
        if regime == self.REGIME_NEAR_PEAK and 0.60 <= adjusted_prob <= 0.80:
            matched_pattern = "near_peak_YES_60-80_blocked"
        elif regime == self.REGIME_NEAR_PEAK and 0.30 <= adjusted_prob <= 0.50:
            matched_pattern = "near_peak_YES_30-50_blocked"
        elif regime == self.REGIME_POST_PEAK and 0.20 <= adjusted_prob <= 0.50:
            matched_pattern = "post_peak_YES_20-50_blocked"
        
        # Original veto patterns
        elif price_band == "preferred" and 0.70 <= adjusted_prob < 0.80 and regime in {
            self.REGIME_NEAR_PEAK,
            self.REGIME_POST_PEAK,
        }:
            matched_pattern = f"{regime}/preferred/70-79"
        elif price_band == "preferred" and regime == self.REGIME_NEAR_PEAK and 0.60 <= adjusted_prob < 0.70:
            matched_pattern = "near_peak/preferred/60-69"
        elif price_band == "preferred" and regime == self.REGIME_NEAR_PEAK and 0.80 <= adjusted_prob < 0.90:
            matched_pattern = "near_peak/preferred/80-89"
        elif price_band == "preferred" and regime == self.REGIME_POST_PEAK and 0.40 <= adjusted_prob < 0.50:
            matched_pattern = "post_peak/preferred/40-49"
        elif price_band == "selective" and regime == self.REGIME_NEAR_PEAK and adjusted_prob >= 0.70:
            matched_pattern = "near_peak/selective/70+"
        elif price_band == "selective" and regime == self.REGIME_NEAR_PEAK and 0.30 <= adjusted_prob < 0.40:
            matched_pattern = "near_peak/selective/30-39"
        elif price_band == "selective" and regime == self.REGIME_POST_PEAK and 0.60 <= adjusted_prob < 0.70:
            matched_pattern = "post_peak/selective/60-69"
        elif price_band == "selective" and regime == self.REGIME_POST_PEAK and 0.20 <= adjusted_prob < 0.30:
            matched_pattern = "post_peak/selective/20-29"
        elif price_band == "extreme" and regime == self.REGIME_NEAR_PEAK and adjusted_prob >= 0.60:
            matched_pattern = "near_peak/extreme/60+"
        elif price_band == "extreme" and regime == self.REGIME_POST_PEAK and 0.20 <= adjusted_prob < 0.60:
            matched_pattern = "post_peak/extreme/20-59"
        elif price_band == "extreme" and regime == self.REGIME_POST_PEAK and adjusted_prob >= 0.80:
            matched_pattern = "post_peak/extreme/80+"
        elif price_band == "selective" and regime == self.REGIME_POST_PEAK and adjusted_prob >= 0.80:
            matched_pattern = "post_peak/selective/80+"
        # Mid-range zone
        elif price_band == "preferred" and regime == self.REGIME_NEAR_PEAK and 0.45 <= adjusted_prob <= 0.55:
            matched_pattern = "near_peak/preferred/mid-range-45-55"
        elif price_band == "preferred" and regime == self.REGIME_POST_PEAK and 0.45 <= adjusted_prob <= 0.55:
            matched_pattern = "post_peak/preferred/mid-range-45-55"

        if not matched_pattern:
            return adjusted_prob, None

        flipped_prob = 1.0 - adjusted_prob
        forced_no_prob = min(flipped_prob, max(market_price - 0.02, 0.01))
        prob_bucket = f"{int(adjusted_prob * 10) * 10:02d}-{int(adjusted_prob * 10) * 10 + 9:02d}"
        return forced_no_prob, (
            f"YES veto applied for repeated losing pattern {matched_pattern}: "
            f"exact temperature YES in bucket {prob_bucket} is treated as NO"
        )

    def _temperature_no_overconfidence_veto(self, adjusted_prob, market_context):
        """
        Mirror the YES-side veto for recurring losing NO patterns from paper trades.
        Current stable losers (EXACT markets only - thresholds are clearer):
        - post_peak + preferred pricing + 70-79% NO: 0/4 NO wins
        - post_peak + selective pricing + 70%+ NO: 0/2 NO wins
        - post_peak + extreme pricing + 60%+ NO: 0/1 NO wins
        """
        target = (market_context or {}).get("target") or {}
        regime = str((market_context or {}).get("regime") or "")
        if not regime:
            days_to_resolution = max(int((market_context or {}).get("days_to_resolution", 3)), 0)
            regime = self.detect_regime(
                adjusted_prob,
                0.0,
                days_to_resolution,
                local_peak_stage=(market_context or {}).get("local_peak_stage"),
            )
        target_type = str(target.get("type") or "")
        direction = str(target.get("direction") or "above")
        market_price = float((market_context or {}).get("market_price", 0.5))
        no_market_price = 1.0 - market_price
        price_band = self._price_band(no_market_price)
        no_prob = 1.0 - adjusted_prob

        if not self._should_apply_temperature_pattern_veto(target, market_context) or direction == "below":
            return adjusted_prob, None

        matched_pattern = None
        if price_band == "preferred" and regime == self.REGIME_POST_PEAK and 0.70 <= no_prob < 0.80:
            matched_pattern = "post_peak/preferred/70-79"
        elif price_band == "selective" and regime == self.REGIME_POST_PEAK and no_prob >= 0.70:
            matched_pattern = "post_peak/selective/70+"
        elif price_band == "extreme" and regime == self.REGIME_POST_PEAK and no_prob >= 0.60:
            matched_pattern = "post_peak/extreme/60+"

        if not matched_pattern:
            return adjusted_prob, None

        forced_yes_prob = max(adjusted_prob, min(market_price + 0.02, 0.99))
        prob_bucket = f"{int(no_prob * 10) * 10:02d}-{int(no_prob * 10) * 10 + 9:02d}"
        return forced_yes_prob, (
            f"NO veto applied for repeated losing pattern {matched_pattern}: "
            f"exact temperature NO in bucket {prob_bucket} is treated as YES"
        )

    def evaluate_market_opportunity(self, model_prob, spread, market_price, market_context=None):
        market_context = market_context or {}
        days_to_resolution = max(int(market_context.get("days_to_resolution", 3)), 0)
        regime = self.detect_regime(
            model_prob,
            spread,
            days_to_resolution,
            local_peak_stage=market_context.get("local_peak_stage"),
        )

        provisional_edge = self.get_edge(model_prob, market_price)
        provisional_mode = "extreme_mispricing" if abs(provisional_edge) >= self.EXTREME_EDGE_THRESHOLD else "standard"
        provisional_confidence = self._confidence_score(
            abs(provisional_edge), spread, provisional_mode, regime, market_price
        )

        bust_risk = self._estimate_bust_risk(
            model_prob=model_prob,
            spread=spread,
            confidence_score=provisional_confidence,
            days_to_resolution=days_to_resolution,
            regime=regime,
        )
        adjusted_prob = self._apply_bust_risk(model_prob, bust_risk)
        pre_veto_prob = adjusted_prob
        pre_veto_edge = self.get_edge(pre_veto_prob, market_price)
        veto_context = {
            **market_context,
            "regime": regime,
            "market_price": market_price,
        }
        yes_veto_reason = None
        no_veto_reason = None
        if self._should_apply_temperature_pattern_veto(market_context.get("target"), market_context):
            if pre_veto_edge > 0:
                adjusted_prob, yes_veto_reason = self._temperature_yes_overconfidence_veto(adjusted_prob, veto_context)
            else:
                adjusted_prob, no_veto_reason = self._temperature_no_overconfidence_veto(adjusted_prob, veto_context)
        edge = self.get_edge(adjusted_prob, market_price)
        abs_edge = abs(edge)
        trade_side = "YES" if edge > 0 else "NO"
        mode = "extreme_mispricing" if abs_edge >= self.EXTREME_EDGE_THRESHOLD else "standard"
        
        # Extract dispersion for confidence scoring
        temp_dispersion = float(market_context.get("temp_dispersion", 0.0) or 0.0)
        confidence_score = self._confidence_score(abs_edge, spread, mode, regime, market_price, temp_dispersion)

        # Apply Bayesian calibration adjustment using historical buckets.
        # Protected exact-rounding consensus means every source is already sitting
        # on the settlement rung, so historical bucket drag should not flip the side.
        calibration_buckets = market_context.get("calibration_buckets", {})
        exact_rounding_protected = bool(market_context.get("exact_rounding_protected", False))
        if yes_veto_reason or no_veto_reason or exact_rounding_protected:
            calibrated_prob = adjusted_prob
        else:
            calibrated_prob = self.bayesian_calibration_adjustment(adjusted_prob, calibration_buckets)

        range_threshold_safety_reason, range_threshold_multiplier = self._assess_range_threshold_safety(
            market_context,
            regime,
        )
        if range_threshold_multiplier != 1.0:
            calibrated_prob = 0.5 + ((calibrated_prob - 0.5) * range_threshold_multiplier)
            calibrated_prob = min(max(calibrated_prob, 0.0), 1.0)
        
        # Use calibrated probability for final edge calculation
        final_edge = self.get_edge(calibrated_prob, market_price)
        final_abs_edge = abs(final_edge)
        final_trade_side = "YES" if final_edge > 0 else "NO"
        final_mode = "extreme_mispricing" if final_abs_edge >= self.EXTREME_EDGE_THRESHOLD else "standard"

        exact_safety_reason, exact_safety_multiplier = self._assess_exact_safety(market_context, spread)
        exact_no_blocked, exact_no_block_reason = self._should_block_exact_no_near_peak(calibrated_prob, market_context)
        if exact_no_blocked:
            calibrated_prob = max(calibrated_prob, min(market_price + 0.02, 0.99))
            final_edge = self.get_edge(calibrated_prob, market_price)
            final_abs_edge = abs(final_edge)
            final_trade_side = "YES" if final_edge > 0 else "NO"
            final_mode = "extreme_mispricing" if final_abs_edge >= self.EXTREME_EDGE_THRESHOLD else "standard"

        wrong_side_yes_blocked, wrong_side_yes_block_reason = self._should_block_same_day_temperature_yes_wrong_side(
            calibrated_prob,
            market_price,
            market_context,
        )
        forced_wrong_side_no_trade = False
        if wrong_side_yes_blocked:
            forced_wrong_side_no_trade = True
            calibrated_prob = min(calibrated_prob, max(market_price - 0.02, 0.01))
            final_trade_side = "NO"
            final_edge = (1.0 - calibrated_prob) - (1.0 - market_price)
            final_abs_edge = abs(final_edge)
            final_mode = "extreme_mispricing" if final_abs_edge >= self.EXTREME_EDGE_THRESHOLD else "standard"
        
        # Recalculate confidence with final edge if it changed significantly
        if abs(final_abs_edge - abs_edge) > 0.02:
            confidence_score = self._confidence_score(final_abs_edge, spread, final_mode, regime, market_price, temp_dispersion)

        prob_floor, prob_ceiling = self.probability_bounds(regime)
        base_spread_limit = self.spread_limit(regime)
        base_required_edge, probability_region, price_band = self._required_edge(
            calibrated_prob, regime, market_price
        )
        spread_limit = self._segment_spread_cap(base_spread_limit, final_trade_side, regime, final_mode, price_band)
        required_edge = self._segment_required_edge(base_required_edge, final_trade_side, regime, final_mode, price_band)
        required_confidence = self._segment_confidence_floor(final_trade_side, regime, final_mode, price_band)
        target_type = str((market_context.get("target") or {}).get("type") or "")
        if target_type == "exact":
            if price_band == "preferred":
                required_confidence = max(required_confidence, self.EXACT_MIN_CONFIDENCE_SCORE)
            else:
                required_confidence = max(required_confidence, self.EXACT_SELECTIVE_CONFIDENCE_SCORE)
        elif target_type in {"range", "threshold"}:
            required_confidence = min(required_confidence + self.RANGE_THRESHOLD_CONFIDENCE_BUMP, 0.995)
            if (
                target_type == "threshold"
                and regime == self.REGIME_NEAR_PEAK
                and range_threshold_multiplier < 1.0
            ):
                required_edge = round(required_edge + 0.02, 4)
                required_confidence = min(required_confidence + 0.01, 0.995)
        settlement_risk = float(market_context.get("settlement_risk", 0.0) or 0.0)
        rounding_risk = float(market_context.get("rounding_risk", 0.0) or 0.0)
        station_mismatch_risk = float(market_context.get("station_mismatch_risk", 0.0) or 0.0)
        revision_delta = float(market_context.get("forecast_revision_delta", 0.0) or 0.0)
        revision_volatility = float(market_context.get("forecast_revision_volatility", 0.0) or 0.0)
        revision_direction = str(market_context.get("forecast_revision_direction") or "flat")
        observation_progress = float(market_context.get("observation_progress", 1.0) or 1.0)

        reasons = []
        if calibrated_prob < prob_floor:
            reasons.append(
                f"Calibrated prob {calibrated_prob:.1%} below dynamic floor {prob_floor:.0%} for {regime}"
            )
        if calibrated_prob > prob_ceiling:
            reasons.append(
                f"Calibrated prob {calibrated_prob:.1%} above dynamic ceiling {prob_ceiling:.0%} for {regime}"
            )
        if spread > spread_limit:
            reasons.append(
                f"Forecast spread {spread:.1%} exceeds {regime} limit {spread_limit:.0%}"
            )
        if temp_dispersion >= self.HIGH_DISPERSION_THRESHOLD:
            reasons.append(
                f"Temperature dispersion {temp_dispersion:.2%} is high - model ensemble disagrees too much"
            )
        if price_band == "extreme":
            reasons.append(
                f"Market price {market_price:.1%} outside selective trading band "
                f"{self.SELECTIVE_PRICE_LOW:.0%}-{self.SELECTIVE_PRICE_HIGH:.0%}"
            )
        # FIX 1: Edge guardrail - reject if edge < 0.15 (too small, not meaningful mispricing)
        if final_abs_edge < 0.15:
            reasons.append(
                f"Edge {final_abs_edge:.1%} too small - insufficient mispricing for execution (minimum 0.15)"
            )
        
        if final_mode == "extreme_mispricing" and final_abs_edge < self.EXTREME_EDGE_FLOOR:
            reasons.append(
                f"Edge {final_abs_edge:.1%} below extreme minimum {self.EXTREME_EDGE_FLOOR:.0%}"
            )
        elif final_abs_edge < required_edge:
            reasons.append(
                f"Edge {final_abs_edge:.1%} below dynamic {price_band}/{probability_region} requirement {required_edge:.0%}"
            )
        
        # CRITICAL: Avoid dead-zone edges (30-34% range had 0% win rate in 136 trades)
        if 0.30 <= final_abs_edge <= 0.34:
            reasons.append(
                f"Edge {final_abs_edge:.1%} in dead-zone range (30-34%) - historically 0% win rate"
            )
        if confidence_score < required_confidence:
            reasons.append(
                f"Confidence {confidence_score:.2f} must be >= {required_confidence:.2f} for {price_band} pricing"
            )
        if settlement_risk > 0.46:
            reasons.append(
                f"Settlement sensitivity {settlement_risk:.2f} is too high for v1 execution"
            )
        if rounding_risk > 0.18:
            reasons.append(
                f"Threshold/rounding risk {rounding_risk:.2f} is too high near settlement boundary"
            )
        if station_mismatch_risk >= 0.18:
            reasons.append(
                f"Reporting-station mismatch risk {station_mismatch_risk:.2f} is too high"
            )
        if revision_volatility >= 1.2:
            reasons.append(
                f"Forecast revision volatility {revision_volatility:.2f} is too unstable"
            )
        if final_trade_side == "YES" and revision_direction == "down" and revision_delta <= -0.35:
            reasons.append(
                f"Forecast drift {revision_delta:.2f} moved against YES setup"
            )
        if final_trade_side == "NO" and revision_direction == "up" and revision_delta >= 0.35:
            reasons.append(
                f"Forecast drift {revision_delta:.2f} moved against NO setup"
            )
        if observation_progress < 0.35 and price_band != "preferred":
            reasons.append(
                f"Observation progress {observation_progress:.2f} is too early for non-preferred pricing"
            )
        if exact_no_block_reason and final_trade_side == "NO":
            reasons.append(exact_no_block_reason)
        if exact_safety_reason and exact_safety_multiplier <= 0.0:
            reasons.append(exact_safety_reason)
        if wrong_side_yes_block_reason and final_trade_side == "YES":
            reasons.append(wrong_side_yes_block_reason)

        if forced_wrong_side_no_trade:
            reasons = []

        should_trade = forced_wrong_side_no_trade or len(reasons) == 0
        reject_categories = self._record_decision_stats(
            should_trade=should_trade,
            reasons=reasons,
            regime=regime,
            trade_side=final_trade_side,
        )
        action = "BUY_YES" if final_trade_side == "YES" else "BUY_NO"
        size_multiplier = self._size_multiplier(confidence_score, regime) if should_trade else 0.0
        if should_trade:
            if final_trade_side == "YES":
                size_multiplier *= 0.72 if final_mode == "extreme_mispricing" else 0.82
            elif regime == self.REGIME_NEAR_PEAK and price_band == "preferred":
                size_multiplier *= 1.08
            if target_type == "exact":
                size_multiplier *= exact_safety_multiplier
            elif target_type in {"range", "threshold"}:
                size_multiplier *= range_threshold_multiplier
            size_multiplier = round(size_multiplier, 2)

        # FIX 3: Debug logging - track edge vs confidence priority
        edge_priority = "EDGE_OK" if final_abs_edge >= 0.15 else "EDGE_LOW"
        conf_priority = "CONF_OK" if confidence_score >= required_confidence else "CONF_LOW"
        
        print(
            f"[MODEL]   {edge_priority}/{conf_priority} edge={final_abs_edge:.2%} req_edge={required_edge:.2%} "
            f"conf={confidence_score:.3f} req_conf={required_confidence:.3f} "
            f"regime={regime}, raw_prob={model_prob:.2%}, adjusted_prob={adjusted_prob:.2%}, "
            f"calibrated_prob={calibrated_prob:.2%}, "
            f"bust_risk={bust_risk:.2%}, spread={spread:.2%}, "
            f"dispersion={temp_dispersion:.2%}, trade={should_trade}"
        )
        if yes_veto_reason:
            print(f"[MODEL]   YES VETO: {yes_veto_reason}")
        if no_veto_reason:
            print(f"[MODEL]   NO VETO: {no_veto_reason}")
        for reason in reasons:
            print(f"[MODEL]   REJECT: {reason}")

        return {
            "should_trade": should_trade,
            "mode": final_mode,
            "action": action,
            "confidence_score": confidence_score,
            "size_multiplier": size_multiplier,
            "spread": spread,
            "reasons": reasons,
            "reject_categories": reject_categories,
            "regime": regime,
            "local_peak_stage": market_context.get("local_peak_stage"),
            "local_peak_stage_detail": market_context.get("local_peak_stage_detail"),
            "local_hour": market_context.get("local_hour"),
            "timezone": market_context.get("timezone"),
            "utc_offset_hours": market_context.get("utc_offset_hours"),
            "continent": market_context.get("continent"),
            "city": market_context.get("city"),
            "days_to_resolution": days_to_resolution,
            "raw_model_prob": round(model_prob, 4),
            "pre_yes_veto_prob": round(pre_veto_prob, 4),
            "pre_pattern_veto_prob": round(pre_veto_prob, 4),
            "adjusted_model_prob": round(adjusted_prob, 4),
            "calibrated_model_prob": round(calibrated_prob, 4),
            "market_price": round(market_price, 4),
            "edge": round(final_edge, 4),
            "abs_edge": round(final_abs_edge, 4),
            "bust_risk": round(bust_risk, 4),
            "spread_limit": round(spread_limit, 4),
            "prob_floor": round(prob_floor, 4),
            "prob_ceiling": round(prob_ceiling, 4),
            "required_edge": round(required_edge, 4),
            "base_required_edge": round(base_required_edge, 4),
            "probability_region": probability_region,
            "price_band": price_band,
            "required_confidence": round(required_confidence, 4),
            "trade_side": final_trade_side,
            "yes_veto_applied": bool(yes_veto_reason),
            "no_veto_applied": bool(no_veto_reason),
            "pattern_veto_applied": bool(yes_veto_reason or no_veto_reason),
            "settlement_risk": round(settlement_risk, 4),
            "rounding_risk": round(rounding_risk, 4),
            "station_mismatch_risk": round(station_mismatch_risk, 4),
            "forecast_revision_delta": round(revision_delta, 4),
            "forecast_revision_volatility": round(revision_volatility, 4),
            "forecast_revision_direction": revision_direction,
            "observation_progress": round(observation_progress, 4),
        }

    def should_trade(self, edge, model_prob, conviction):
        spread = 0.0 if conviction else 1.0
        decision = self.evaluate_market_opportunity(
            model_prob=model_prob,
            spread=spread,
            market_price=max(min(model_prob - edge, 0.99), 0.01),
            market_context={"days_to_resolution": 3},
        )
        return decision["should_trade"]
