import json
import math
import os
from typing import Dict, List


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class WeatherIntelligenceAgent:
    """
    Online learning layer that refines weather-market probabilities from
    resolved outcomes, observed market pricing, and persistent bias tracking.
    """

    VERSION = 1
    BLEND_RAMP = (
        (0, 0.0),
        (50, 0.05),
        (150, 0.15),
        (250, 0.25),
    )
    DECISION_DELTA_CAP = 0.10
    DECISION_WEIGHT = 0.35
    SEGMENT_ADJUSTMENT_CAP = 0.04
    RECENT_BIAS_ADJUSTMENT_CAP = 0.05
    def __init__(
        self,
        state_path="../data/intelligence_state.json",
        feedback_path="../data/learning_feedback.jsonl",
    ):
        base_dir = os.path.dirname(__file__)
        self.state_path = os.path.join(base_dir, state_path)
        self.feedback_path = os.path.join(base_dir, feedback_path)
        self.state = self._default_state()
        self._load_state()

    def _default_state(self):
        return {
            "version": self.VERSION,
            "trained_samples": 0,
            "processed_feedback_ids": [],
            "weights": {
                "bias": 0.0,
                "prior_logit": 1.0,
                "market_logit": -0.08,
                "edge": 0.18,
                "abs_edge": 0.05,
                "spread": -0.10,
                "days_urgency": 0.06,
                "liquidity_score": 0.04,
                "volume_score": 0.03,
                "source_count": 0.02,
                "temp_dispersion": -0.04,
                "pricing_extreme": -0.06,
                "regime_post_peak": 0.02,
                "regime_pre_peak": -0.02,
                "target_range": 0.02,
                "target_exact": -0.03,
                "direction_below": -0.01,
            },
            "calibration_buckets": {},
            "segment_stats": {},
            "recent_metrics": {
                "win_rate": 0.0,
                "avg_predicted_yes": 0.0,
                "avg_brier": 0.25,
            },
        }

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                merged = self._default_state()
                merged.update(loaded)
                merged["weights"] = {**self._default_state()["weights"], **loaded.get("weights", {})}
                merged["recent_metrics"] = {
                    **self._default_state()["recent_metrics"],
                    **loaded.get("recent_metrics", {}),
                }
                self.state = merged
        except (OSError, json.JSONDecodeError):
            pass

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)

    def refresh_from_feedback(self):
        if not os.path.exists(self.feedback_path):
            return

        processed_ids = set(self.state.get("processed_feedback_ids", []))
        changed = False

        try:
            with open(self.feedback_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        sample = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    feedback_id = str(sample.get("feedback_id") or "").strip()
                    if not feedback_id or feedback_id in processed_ids:
                        continue

                    self._ingest_feedback_sample(sample)
                    processed_ids.add(feedback_id)
                    changed = True
        except OSError:
            return

        if changed:
            processed_list = list(processed_ids)
            self.state["processed_feedback_ids"] = processed_list[-5000:]
            self._save_state()

    def summarize(self):
        metrics = self.state.get("recent_metrics", {})
        return {
            "trained_samples": int(self.state.get("trained_samples", 0)),
            "recent_win_rate": round(_safe_float(metrics.get("win_rate")), 4),
            "recent_avg_predicted_yes": round(_safe_float(metrics.get("avg_predicted_yes")), 4),
            "recent_avg_brier": round(_safe_float(metrics.get("avg_brier"), 0.25), 4),
        }

    def generate_signal_adjustment(self, context):
        base_prob = _clamp(_safe_float(context.get("base_prob"), 0.5))
        market_yes_price = _clamp(_safe_float(context.get("market_yes_price"), 0.5))
        features = self._build_features(context)
        raw_score = 0.0
        for name, value in features.items():
            raw_score += _safe_float(self.state["weights"].get(name), 0.0) * value

        ml_prob = _clamp(self._sigmoid(raw_score))
        trained_samples = max(int(self.state.get("trained_samples", 0)), 0)
        model_blend = self._blend_factor(trained_samples)
        blended_prob = ((1 - model_blend) * base_prob) + (model_blend * ml_prob)

        meta = self._extract_meta(context)
        segment_adjustment, segment_confidence = self._segment_adjustment(meta)
        segmented_prob = _clamp(blended_prob + segment_adjustment)

        calibration_prob, calibration_weight = self._calibrate(segmented_prob)
        recent_metrics = self.state.get("recent_metrics", {})
        recent_win_rate = _safe_float(recent_metrics.get("win_rate"), 0.0)
        recent_avg_predicted_yes = _safe_float(recent_metrics.get("avg_predicted_yes"), 0.0)
        recent_bias_gap = recent_win_rate - recent_avg_predicted_yes
        recent_bias_adjustment = _clamp(
            recent_bias_gap * 0.40,
            -self.RECENT_BIAS_ADJUSTMENT_CAP,
            self.RECENT_BIAS_ADJUSTMENT_CAP,
        )
        calibration_prob = _clamp(calibration_prob + recent_bias_adjustment)
        decision_delta = _clamp(
            calibration_prob - base_prob,
            -self.DECISION_DELTA_CAP,
            self.DECISION_DELTA_CAP,
        )
        decision_prob = _clamp(base_prob + (decision_delta * self.DECISION_WEIGHT))
        inefficiency_score = abs(calibration_prob - market_yes_price) * (0.7 + (0.3 * max(segment_confidence, model_blend)))
        learning_confidence = _clamp(0.25 + model_blend + calibration_weight + (0.5 * segment_confidence))

        training_payload = {
            "features": {key: round(value, 6) for key, value in features.items()},
            "meta": meta,
            "base_prob": round(base_prob, 6),
            "market_yes_price": round(market_yes_price, 6),
            "intelligence_prob": round(calibration_prob, 6),
            "decision_prob": round(decision_prob, 6),
            "decision_delta": round(decision_delta, 6),
            "model_blend": round(model_blend, 6),
        }

        return {
            "base_prob": round(base_prob, 4),
            "ml_prob": round(ml_prob, 4),
            "blended_prob": round(blended_prob, 4),
            "calibrated_prob": round(calibration_prob, 4),
            "decision_prob": round(decision_prob, 4),
            "decision_delta": round(decision_delta, 4),
            "model_blend": round(model_blend, 4),
            "learning_confidence": round(learning_confidence, 4),
            "segment_adjustment": round(segment_adjustment, 4),
            "recent_bias_adjustment": round(recent_bias_adjustment, 4),
            "segment_confidence": round(segment_confidence, 4),
            "calibration_weight": round(calibration_weight, 4),
            "inefficiency_score": round(inefficiency_score, 4),
            "training_payload": training_payload,
        }

    def _build_features(self, context):
        base_prob = _clamp(_safe_float(context.get("base_prob"), 0.5))
        market_yes_price = _clamp(_safe_float(context.get("market_yes_price"), 0.5))
        spread = max(_safe_float(context.get("spread"), 0.0), 0.0)
        days_to_resolution = max(int(_safe_float(context.get("days_to_resolution"), 0)), 0)
        liquidity = max(_safe_float(context.get("liquidity"), 0.0), 0.0)
        volume_24h = max(_safe_float(context.get("volume_24h"), 0.0), 0.0)
        forecast_data = context.get("forecast_data") or {}
        forecast_values = [float(v) for v in forecast_data.values()] if forecast_data else []
        temp_dispersion = (max(forecast_values) - min(forecast_values)) if len(forecast_values) >= 2 else 0.0
        source_count = len(forecast_values)
        regime = str(context.get("regime") or "pre_peak")
        target = context.get("target") or {}
        target_type = str(target.get("type") or "threshold")
        direction = str(target.get("direction") or "above")

        return {
            "bias": 1.0,
            "prior_logit": self._logit(base_prob),
            "market_logit": self._logit(market_yes_price),
            "edge": (base_prob - market_yes_price) * 2.0,
            "abs_edge": abs(base_prob - market_yes_price) * 2.0,
            "spread": min(spread / 0.25, 2.0),
            "days_urgency": 1.0 / (days_to_resolution + 1.0),
            "liquidity_score": min(math.log1p(liquidity) / 8.0, 1.5),
            "volume_score": min(math.log1p(volume_24h) / 8.0, 1.5),
            "source_count": min(source_count / 4.0, 1.0),
            "temp_dispersion": min(temp_dispersion / 8.0, 2.0),
            "pricing_extreme": 1.0 if market_yes_price <= 0.12 or market_yes_price >= 0.88 else 0.0,
            "regime_post_peak": 1.0 if regime == "post_peak" else 0.0,
            "regime_pre_peak": 1.0 if regime == "pre_peak" else 0.0,
            "target_range": 1.0 if target_type == "range" else 0.0,
            "target_exact": 1.0 if target_type == "exact" else 0.0,
            "direction_below": 1.0 if direction == "below" else 0.0,
        }

    def _extract_meta(self, context):
        target = context.get("target") or {}
        market_yes_price = _clamp(_safe_float(context.get("market_yes_price"), 0.5))
        source_count = len((context.get("forecast_data") or {}).keys())
        local_hour = int(_safe_float(context.get("local_hour"), -1))
        if market_yes_price <= 0.12 or market_yes_price >= 0.88:
            pricing_bucket = "extreme"
        elif market_yes_price <= 0.22 or market_yes_price >= 0.78:
            pricing_bucket = "selective"
        else:
            pricing_bucket = "preferred"

        if local_hour < 0:
            local_hour_bucket = "unknown"
        elif local_hour < 9:
            local_hour_bucket = "overnight"
        elif local_hour < 12:
            local_hour_bucket = "morning"
        elif local_hour < 16:
            local_hour_bucket = "midday"
        elif local_hour < 20:
            local_hour_bucket = "afternoon"
        else:
            local_hour_bucket = "evening"

        return {
            "regime": str(context.get("regime") or "pre_peak"),
            "target_type": str(target.get("type") or "threshold"),
            "direction": str(target.get("direction") or "above"),
            "pricing_bucket": pricing_bucket,
            "source_count_bucket": str(min(source_count, 4)),
            "local_peak_stage": str(context.get("local_peak_stage") or "unknown"),
            "local_peak_stage_detail": str(context.get("local_peak_stage_detail") or "unknown"),
            "local_hour_bucket": local_hour_bucket,
            "timezone": str(context.get("timezone") or "UTC"),
            "continent": str(context.get("continent") or "Unknown"),
            "city": str(context.get("city") or "Unknown"),
        }

    def _segment_adjustment(self, meta):
        segment_keys = [
            f"regime:{meta['regime']}",
            f"target:{meta['target_type']}",
            f"direction:{meta['direction']}",
            f"pricing:{meta['pricing_bucket']}",
            f"sources:{meta['source_count_bucket']}",
        ]
        if meta.get("local_peak_stage"):
            segment_keys.append(f"local_peak:{meta['local_peak_stage']}")
        if meta.get("local_hour_bucket"):
            segment_keys.append(f"local_hour:{meta['local_hour_bucket']}")
        if meta.get("continent"):
            segment_keys.append(f"continent:{meta['continent']}")

        weighted_residual = 0.0
        total_weight = 0.0
        for key in segment_keys:
            stats = self.state.get("segment_stats", {}).get(key)
            if not stats:
                continue

            count = int(stats.get("count", 0))
            if count < 6:
                continue

            avg_pred = _safe_float(stats.get("pred_sum"), 0.0) / max(count, 1)
            win_rate = _safe_float(stats.get("wins"), 0.0) / max(count, 1)
            residual = win_rate - avg_pred
            weight = min(count / 40.0, 1.0)
            weighted_residual += residual * weight
            total_weight += weight

        if total_weight <= 0:
            return 0.0, 0.0

        adjustment = _clamp(
            weighted_residual / total_weight,
            -self.SEGMENT_ADJUSTMENT_CAP,
            self.SEGMENT_ADJUSTMENT_CAP,
        )
        confidence = _clamp(total_weight / len(segment_keys), 0.0, 1.0)
        return adjustment, confidence

    def _calibrate(self, probability):
        bucket_key = self._bucket_key(probability)
        stats = self.state.get("calibration_buckets", {}).get(bucket_key)
        if not stats:
            return probability, 0.0

        count = int(stats.get("count", 0))
        if count < 5:
            return probability, 0.0

        win_rate = _safe_float(stats.get("wins"), 0.0) / max(count, 1)
        weight = min(count / 50.0, 0.30)
        calibrated = ((1 - weight) * probability) + (weight * win_rate)
        return _clamp(calibrated), weight

    def _ingest_feedback_sample(self, sample):
        payload = sample.get("learning_payload") or {}
        features = payload.get("features") or {}
        predicted_yes = _clamp(_safe_float(payload.get("intelligence_prob"), _safe_float(sample.get("entry_model_prob"), 0.5)))
        label = 1.0 if int(_safe_float(sample.get("resolved_yes"), 0)) == 1 else 0.0

        if features:
            score = 0.0
            for name, value in features.items():
                score += _safe_float(self.state["weights"].get(name), 0.0) * _safe_float(value, 0.0)

            pred = self._sigmoid(score)
            sample_count = max(int(self.state.get("trained_samples", 0)) + 1, 1)
            learning_rate = 0.045 / math.sqrt(sample_count)
            error = label - pred

            for name, value in features.items():
                numeric_value = _safe_float(value, 0.0)
                current_weight = _safe_float(self.state["weights"].get(name), 0.0)
                self.state["weights"][name] = round(current_weight + (learning_rate * error * numeric_value), 6)

            self.state["trained_samples"] = sample_count

        self._update_calibration_bucket(predicted_yes, label)
        self._update_segment_stats(payload.get("meta") or {}, predicted_yes, label, _safe_float(sample.get("pnl"), 0.0))
        self._update_recent_metrics(predicted_yes, label)

    def _update_calibration_bucket(self, probability, label):
        bucket_key = self._bucket_key(probability)
        bucket = self.state.setdefault("calibration_buckets", {}).setdefault(bucket_key, {
            "count": 0,
            "wins": 0.0,
        })
        bucket["count"] += 1
        bucket["wins"] += label

    def _update_segment_stats(self, meta, predicted_yes, label, pnl):
        segment_keys = [
            f"regime:{meta.get('regime', 'unknown')}",
            f"target:{meta.get('target_type', 'unknown')}",
            f"direction:{meta.get('direction', 'unknown')}",
            f"pricing:{meta.get('pricing_bucket', 'unknown')}",
            f"sources:{meta.get('source_count_bucket', 'unknown')}",
        ]
        if "pattern_veto" in meta:
            segment_keys.append(f"pattern_veto:{meta.get('pattern_veto', 'unknown')}")
        if "veto_side" in meta:
            segment_keys.append(f"veto_side:{meta.get('veto_side', 'none')}")
        if "local_peak_stage" in meta:
            segment_keys.append(f"local_peak:{meta.get('local_peak_stage', 'unknown')}")
        if "local_hour_bucket" in meta:
            segment_keys.append(f"local_hour:{meta.get('local_hour_bucket', 'unknown')}")
        if "continent" in meta:
            segment_keys.append(f"continent:{meta.get('continent', 'Unknown')}")

        for key in segment_keys:
            stats = self.state.setdefault("segment_stats", {}).setdefault(key, {
                "count": 0,
                "wins": 0.0,
                "pred_sum": 0.0,
                "pnl_sum": 0.0,
            })
            stats["count"] += 1
            stats["wins"] += label
            stats["pred_sum"] += predicted_yes
            stats["pnl_sum"] += pnl

    def _update_recent_metrics(self, predicted_yes, label):
        metrics = self.state.setdefault("recent_metrics", {})
        sample_count = max(int(self.state.get("trained_samples", 0)), 1)
        smoothing = min(0.12, 2.0 / (sample_count + 1.0))

        prior_win_rate = _safe_float(metrics.get("win_rate"), 0.0)
        prior_avg_pred = _safe_float(metrics.get("avg_predicted_yes"), 0.0)
        prior_brier = _safe_float(metrics.get("avg_brier"), 0.25)
        brier = (predicted_yes - label) ** 2

        metrics["win_rate"] = round(((1 - smoothing) * prior_win_rate) + (smoothing * label), 6)
        metrics["avg_predicted_yes"] = round(((1 - smoothing) * prior_avg_pred) + (smoothing * predicted_yes), 6)
        metrics["avg_brier"] = round(((1 - smoothing) * prior_brier) + (smoothing * brier), 6)

    def _bucket_key(self, probability):
        bucket = int(_clamp(probability) * 10)
        bucket = min(max(bucket, 0), 9)
        return f"{bucket * 10:02d}-{(bucket * 10) + 9:02d}"

    def _blend_factor(self, trained_samples):
        ramp = self.BLEND_RAMP
        if trained_samples <= ramp[0][0]:
            return ramp[0][1]

        for index in range(1, len(ramp)):
            left_samples, left_blend = ramp[index - 1]
            right_samples, right_blend = ramp[index]
            if trained_samples <= right_samples:
                span = max(right_samples - left_samples, 1)
                progress = (trained_samples - left_samples) / span
                return left_blend + ((right_blend - left_blend) * progress)

        return ramp[-1][1]

    def _logit(self, probability):
        probability = _clamp(probability, 0.01, 0.99)
        return math.log(probability / (1.0 - probability))

    def _sigmoid(self, value):
        value = max(min(value, 8.0), -8.0)
        return 1.0 / (1.0 + math.exp(-value))
