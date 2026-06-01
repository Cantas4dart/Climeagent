import json
import math
import os
import re
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# Ensure sibling module imports work when run directly
sys.path.insert(0, os.path.dirname(__file__))

from weather import WeatherClient
from markets import MarketClient
from model import TradingModel
try:
    from .console import safe_print
except ImportError:
    from console import safe_print
DEGREE_OPTIONAL_PATTERN = f"(?:{chr(176)}\\s*)?"
MAX_SIGNAL_ENTRY_PRICE = 0.85
MIN_FORECAST_SOURCE_COUNT = 3



class SignalGenerator:
    def __init__(self, data_path="../data/signals.json"):
        self.weather = WeatherClient()
        self.markets = MarketClient()
        self.model = TradingModel()
        self.data_path = os.path.join(os.path.dirname(__file__), data_path)
        self.forecast_history_path = os.path.join(os.path.dirname(__file__), "../data/forecast_history.json")
        self.forecast_history = self._load_forecast_history()
        self.run_count = 0
        self._run_forecast_cache = {}
        self.live_market_scope = self._resolve_live_market_scope()
        self.us_only_trading = self.live_market_scope == "us"
        self.month_map = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }

    def log(self, message=""):
        try:
            safe_print(message)
        except OSError:
            pass

    @staticmethod
    def _resolve_live_market_scope():
        raw_scope = (os.getenv("CLIME_LIVE_MARKET_SCOPE") or os.getenv("BLOCKY_LIVE_MARKET_SCOPE") or "").strip().lower()
        if raw_scope:
            normalized_scope = raw_scope.replace("-", "_").replace(" ", "_")
            if normalized_scope in {"us", "non_us", "all"}:
                return normalized_scope
        return "us" if (os.getenv("CLIME_US_ONLY_TRADING") or os.getenv("BLOCKY_US_ONLY_TRADING") or "1").strip().lower() not in {"0", "false", "no"} else "all"

    @staticmethod
    def _is_trade_price_within_cap(entry_price):
        try:
            return float(entry_price) <= MAX_SIGNAL_ENTRY_PRICE
        except (TypeError, ValueError):
            return False

    @classmethod
    def _are_market_prices_within_cap(cls, yes_price, no_price):
        return cls._is_trade_price_within_cap(yes_price) and cls._is_trade_price_within_cap(no_price)

    def run(self, event_filter=None, max_events=None):
        self.run_count += 1
        start_time = datetime.now()
        self.forecast_history = self._load_forecast_history()
        self._run_forecast_cache = {}
        self.log(f"\n[SIGNAL] {'='*55}")
        self.log(f"[SIGNAL]   SCAN #{self.run_count} -- {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log(f"[SIGNAL] {'='*55}")
        self.log("[SIGNAL] Live decision layer: fresh forecast intelligence enabled.")
        scope_labels = {
            "us": "US-only",
            "non_us": "non-US-only",
            "all": "all markets",
        }
        self.log(f"[SIGNAL] Live trading region filter: {scope_labels.get(self.live_market_scope, self.live_market_scope)}.")

        # Step 0: Fetch markets
        self.log(f"\n[SIGNAL] --- Phase 1: Market Discovery ---")
        active_markets = self.markets.get_weather_markets()
        self.log(f"[SIGNAL] Found {len(active_markets)} active temperature markets after filtering.")
        discovery_diagnostics = self._summarize_market_dates(active_markets)
        if discovery_diagnostics["dates"]:
            self.log(f"[SIGNAL] Discovery dates: {discovery_diagnostics['counts_by_date']}")

        if len(active_markets) == 0:
            self.log("[SIGNAL] WARNING: No temperature markets available. Nothing to analyze.")
            self.log("[SIGNAL]    Temperature markets are created periodically on Polymarket.")
            self.log("[SIGNAL]    The bot will automatically pick them up on the next scan.")
            self.save_signals([], diagnostics={
                "reason": "no_temperature_markets",
                "raw_search_completed": True,
                "discovery": discovery_diagnostics,
            })
            return

        signals = []
        market_states = []
        skipped = {
            "no_location": 0,
            "no_market_date": 0,
            "no_forecast": 0,
            "no_target": 0,
            "no_prices": 0,
            "sanity_blocked": 0,
            "no_edge": 0,
            "forecast_timing": 0,
            "market_date_mismatch": 0,
            "error": 0
        }

        self.log(f"\n[SIGNAL] --- Phase 2: Signal Analysis ({len(active_markets)} markets) ---")

        grouped_markets = self.group_markets_by_event(active_markets)
        if event_filter:
            grouped_markets = {
                label: markets for label, markets in grouped_markets.items()
                if event_filter.lower() in label.lower()
            }
        if max_events is not None:
            grouped_markets = dict(list(grouped_markets.items())[:max_events])
        total_events = len(grouped_markets)
        market_counter = 0

        for event_index, (event_label, markets) in enumerate(grouped_markets.items(), start=1):
            self.log(f"\n[SIGNAL] ===== Event {event_index}/{total_events}: {event_label} =====")
            ordered_markets = self.sort_markets_by_rung(markets)
            ordered_markets, date_filtered_count = self._filter_markets_for_current_local_date(ordered_markets)
            if date_filtered_count:
                skipped["market_date_mismatch"] += date_filtered_count
                self.log(
                    f"[SIGNAL] Filtered {date_filtered_count} stale/future market(s) "
                    "outside each city's local scan date."
                )
            if not ordered_markets:
                continue
            event_best_signal = None
            event_best_score = None

            for rung_index, market in enumerate(ordered_markets, start=1):
                market_counter += 1
                question = market.get("_display_question") or market.get("question", "Unknown")
                market_id = market.get("id", "?")
                condition_id = market.get("conditionId", "?")
                self.log(f"\n[SIGNAL] +-- Rung {rung_index}/{len(ordered_markets)} | Market {market_counter}/{len(active_markets)} ------------------")
                self.log(f"[SIGNAL] | ID:        {market_id}")
                self.log(f"[SIGNAL] | Condition: {condition_id[:20]}...")
                self.log(f"[SIGNAL] | Question:  {question}")

                # Step 1: Parse location
                location = self.markets.parse_market_location(question)
                if not location:
                    self.log(f"[SIGNAL] | >> SKIP: No matching city found in question.")
                    skipped["no_location"] += 1
                    self.log(f"[SIGNAL] +------------------------------------------")
                    continue

                lat = location["lat"]
                lon = location["lon"]
                is_us = location["is_us"]
                self.log(
                    f"[SIGNAL] | Location: {location['city']}, {location.get('country') or '?'} "
                    f"| ({lat}, {lon}), US={is_us}, TZ={location.get('timezone') or 'UTC'}"
                )
                if not self._is_live_tradeable_location(location):
                    self.log("[SIGNAL] | >> SKIP: Non-US market blocked while live trading remains US-only.")
                    skipped["no_location"] += 1
                    self.log(f"[SIGNAL] +------------------------------------------")
                    continue

                # Step 2: Parse exact market date
                market_date = self.extract_market_date(question, market)
                if not market_date:
                    self.log(f"[SIGNAL] | >> SKIP: Could not determine market date.")
                    skipped["no_market_date"] += 1
                    self.log(f"[SIGNAL] +------------------------------------------")
                    continue
                self.log(f"[SIGNAL] | Market Date: {market_date.isoformat()}")
                if not self._is_current_local_market_date(location, market_date):
                    local_date = self._current_local_date(location)
                    self.log(
                        f"[SIGNAL] | >> SKIP: Market date {market_date.isoformat()} "
                        f"does not match local scan date {local_date.isoformat()}."
                    )
                    skipped["market_date_mismatch"] += 1
                    self.log(f"[SIGNAL] +-------------------------------------------")
                    continue

                # Step 3: Get forecast
                forecast = self._get_cached_forecast(
                    lat,
                    lon,
                    is_us,
                    location,
                    market_date,
                )
                if not forecast:
                    self.log(f"[SIGNAL] | >> SKIP: Weather forecast fetch failed.")
                    skipped["no_forecast"] += 1
                    self.log(f"[SIGNAL] +------------------------------------------")
                    continue

                try:
                    # Step 4: Extract target
                    target = self.extract_target(question)
                    if not target:
                        self.log(f"[SIGNAL] | >> SKIP: Could not extract target from question.")
                        skipped["no_target"] += 1
                        self.log(f"[SIGNAL] +------------------------------------------")
                        continue
                    self.log(f"[SIGNAL] | Target: {target}")

                    # Step 5: Extract predicted temperatures for the exact market day
                    forecast_data = self.extract_predicted_temps(forecast, is_us, market_date)
                    if not forecast_data:
                        self.log(f"[SIGNAL] | >> SKIP: No forecast temperatures available for market date.")
                        skipped["no_forecast"] += 1
                        self.log(f"[SIGNAL] +------------------------------------------")
                        continue
                    source_gate = self._forecast_source_gate(forecast_data)
                    if source_gate["blocked"]:
                        self.log(f"[SIGNAL] | >> SKIP: {source_gate['reason']}")
                        skipped["no_forecast"] += 1
                        self.log(f"[SIGNAL] +------------------------------------------")
                        continue
                    self.log(f"[SIGNAL] | Forecasts: {forecast_data}")
                    enhancement = forecast.get("enhancement", {}) if isinstance(forecast, dict) else {}
                    if enhancement.get("adjusted_high_temperature") is not None:
                        self.log(
                            f"[SIGNAL] | Enhanced High: {enhancement['adjusted_high_temperature']:.2f}"
                            f" | Forecast Conf: {enhancement.get('confidence_score', 0.0):.2f}"
                        )

                    market_context = self.build_market_context(location, forecast, market_date)
                    market_context = self._enrich_market_context(
                        market_context,
                        forecast,
                        forecast_data,
                        target,
                        market_date,
                        is_us,
                    )
                    freshness = self._forecast_freshness_snapshot(forecast, market_context, market_date)
                    market_context.update(freshness)
                    market_context["market_date"] = market_date.isoformat()
                    self.log(
                        f"[SIGNAL] | Local Time: {market_context['local_now']} "
                        f"| Hour: {market_context['local_hour']} "
                        f"| Stage: {market_context['local_peak_stage_detail']}"
                    )
                    self.log(
                        f"[SIGNAL] | Forecast Freshness: bundle_age={market_context['forecast_bundle_age_minutes']:.1f}m "
                        f"| {self._format_metar_freshness(market_context)}"
                    )
                    if market_context.get("provider_primary_source"):
                        self.log(
                            f"[SIGNAL] | Provider Freshness: {market_context['provider_primary_source']} "
                            f"age={market_context['provider_issue_age_minutes']:.1f}m "
                            f"({market_context['provider_issue_source']})"
                        )
                    self.log(
                        f"[SIGNAL] | Drift: {market_context['forecast_revision_direction']} "
                        f"({market_context['forecast_revision_delta']:+.2f}) | "
                        f"Settlement Risk: {market_context['settlement_risk']:.2f}"
                    )
                    if market_context.get("resolution_station_name"):
                        station_mode = "applied" if market_context.get("resolution_coordinates_applied") else "referenced"
                        self.log(
                            f"[SIGNAL] | Resolution Station: {market_context['resolution_station_name']} "
                            f"({station_mode})"
                        )
                    freshness_gate = self._freshness_gate(market_context, market_date)
                    if freshness_gate["blocked"]:
                        self.log(f"[SIGNAL] | >> SKIP: {freshness_gate['reason']}")
                        skipped["no_forecast"] += 1
                        self.log(f"[SIGNAL] +------------------------------------------")
                        continue

                    # Step 6: Calculate ensemble probability
                    avg_prob, spread, ensemble_stats = self.model.calculate_ensemble_probability(forecast_data, target)
                    self.log(
                        f"[SIGNAL] | Raw Model Prob: {avg_prob:.2%}, Spread: {spread:.2%}, "
                        f"Models: {ensemble_stats.get('count', 0)}"
                    )

                    # Step 7: Get market price
                    raw_prices = market.get("outcomePrices", "[]")
                    if isinstance(raw_prices, str):
                        raw_prices = json.loads(raw_prices)

                    if not raw_prices or len(raw_prices) < 1:
                        self.log(f"[SIGNAL] | >> SKIP: No outcome prices available.")
                        skipped["no_prices"] += 1
                        self.log(f"[SIGNAL] +------------------------------------------")
                        continue

                    sanity = self._run_market_sanity_checks(market, question, target, raw_prices)
                    if sanity["issues"]:
                        self.log(f"[SIGNAL] | >> SKIP: {'; '.join(sanity['issues'])}")
                        skipped["sanity_blocked"] += 1
                        self.log(f"[SIGNAL] +------------------------------------------")
                        continue

                    market_price = sanity["yes_price"]
                    self.log(
                        f"[SIGNAL] | Market Price (Yes): {sanity['yes_price']:.4f} | "
                        f"Market Price (No): {sanity['no_price']:.4f}"
                    )

                    days_to_resolution = market_context["days_to_resolution"]
                    provisional_regime = self.model.detect_regime(
                        avg_prob,
                        spread,
                        days_to_resolution,
                        local_peak_stage=market_context["local_peak_stage"],
                    )
                    intelligence = self._fresh_forecast_intelligence(
                        avg_prob=avg_prob,
                        spread=spread,
                        forecast_data=forecast_data,
                        target=target,
                        market_context=market_context,
                        sanity=sanity,
                    )
                    learned_prob = intelligence["probability"]
                    self.log(
                        f"[SIGNAL] | Intelligence Prob: {learned_prob:.2%} "
                        f"(raw={avg_prob:.2%}, blend={intelligence['blend_weight']:.2f}, "
                        f"fresh={intelligence['freshness_score']:.2f}, "
                        f"edge score={intelligence['inefficiency_score']:.2%})"
                    )

                    decision = self.model.evaluate_market_opportunity(
                        model_prob=learned_prob,
                        spread=spread,
                        market_price=market_price,
                        market_context={
                            "days_to_resolution": days_to_resolution,
                            "market_date": market_date.isoformat(),
                            "target": target,
                            "intelligence_prob": learned_prob,
                            "local_peak_stage": market_context["local_peak_stage"],
                            "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                            "local_hour": market_context["local_hour"],
                            "timezone": market_context["timezone"],
                            "utc_offset_hours": market_context["utc_offset_hours"],
                            "continent": market_context["continent"],
                            "city": market_context["city"],
                            "country_code": market_context.get("country_code"),
                            "temp_dispersion": market_context.get("temp_dispersion", 0.0),
                            "forecast_avg": market_context.get("forecast_avg"),
                            "forecast_min": market_context.get("forecast_min"),
                            "forecast_max": market_context.get("forecast_max"),
                            "calibration_buckets": market_context.get("calibration_buckets", {}),
                            "forecast_revision_delta": market_context["forecast_revision_delta"],
                            "forecast_revision_volatility": market_context["forecast_revision_volatility"],
                            "forecast_revision_direction": market_context["forecast_revision_direction"],
                            "settlement_risk": market_context["settlement_risk"],
                            "rounding_risk": market_context["rounding_risk"],
                            "station_mismatch_risk": market_context["station_mismatch_risk"],
                            "observation_progress": market_context["observation_progress"],
                            "exact_rounding_consensus": market_context.get("exact_rounding_consensus", 0.0),
                            "exact_rounding_protected": bool(market_context.get("exact_rounding_protected", False)),
                            "exact_target_distance": market_context.get("exact_target_distance"),
                        }
                    )
                    self.log(
                        f"[SIGNAL] | Adjusted Prob: {decision['adjusted_model_prob']:.2%}, "
                        f"Bust Risk: {decision['bust_risk']:.2%}, Regime: {decision['regime']}"
                    )
                    self.log(f"[SIGNAL] | Edge: {decision['edge']:.2%} (abs: {decision['abs_edge']:.2%})")
                    entry_timing_gate = self._entry_timing_gate(market_context, market_date)
                    learning_payload = self._build_learning_payload(
                        avg_prob=avg_prob,
                        market_price=market_price,
                        spread=spread,
                        days_to_resolution=days_to_resolution,
                        provisional_regime=provisional_regime,
                        target=target,
                        market_context=market_context,
                        sanity=sanity,
                        entry_timing_gate=entry_timing_gate,
                        decision=decision,
                        intelligence=intelligence,
                    )

                    final_yes_prob = float(decision.get("calibrated_model_prob", decision["adjusted_model_prob"]))
                    trade_side_price = sanity["yes_price"] if decision["action"] == "BUY_YES" else sanity["no_price"]
                    trade_side_market_prob = final_yes_prob if decision["action"] == "BUY_YES" else (1 - final_yes_prob)
                    market_states.append({
                        "market_id": market_id,
                        "condition_id": condition_id,
                        "question": question,
                        "market_date": market_date.isoformat(),
                        "city": market_context["city"],
                        "country": market_context["country"],
                        "country_code": market_context["country_code"],
                        "continent": market_context["continent"],
                        "timezone": market_context["timezone"],
                        "utc_offset_hours": round(market_context["utc_offset_hours"], 2),
                        "local_now": market_context["local_now"],
                        "local_date": market_context["local_date"],
                        "local_hour": market_context["local_hour"],
                        "local_peak_stage": market_context["local_peak_stage"],
                        "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                        "forecast_revision_delta": round(market_context["forecast_revision_delta"], 4),
                        "forecast_revision_volatility": round(market_context["forecast_revision_volatility"], 4),
                        "forecast_revision_direction": market_context["forecast_revision_direction"],
                        "forecast_fetched_at": market_context.get("forecast_fetched_at"),
                        "forecast_bundle_age_minutes": round(market_context.get("forecast_bundle_age_minutes", 0.0), 4),
                        "provider_primary_source": market_context.get("provider_primary_source"),
                        "provider_issued_at": market_context.get("provider_issued_at"),
                        "provider_issue_age_minutes": round(market_context.get("provider_issue_age_minutes", 0.0), 4),
                        "provider_issue_source": market_context.get("provider_issue_source"),
                        "metar_observed_at": market_context.get("metar_observed_at"),
                        "metar_age_hours": round(market_context.get("metar_age_hours", 0.0), 4),
                        "settlement_risk": round(market_context["settlement_risk"], 4),
                        "rounding_risk": round(market_context["rounding_risk"], 4),
                        "station_mismatch_risk": round(market_context["station_mismatch_risk"], 4),
                        "observation_progress": round(market_context["observation_progress"], 4),
                        "exact_target_distance": round(market_context.get("exact_target_distance", 0.0), 4) if market_context.get("exact_target_distance") is not None else None,
                        "resolution_source": market_context.get("resolution_source"),
                        "resolution_station_name": market_context.get("resolution_station_name"),
                        "resolution_station_url": market_context.get("resolution_station_url"),
                        "resolution_station_id": market_context.get("resolution_station_id"),
                        "resolution_coordinates_applied": bool(market_context.get("resolution_coordinates_applied")),
                        "entry_timing_blocked": bool(entry_timing_gate["blocked"]),
                        "entry_timing_reason": entry_timing_gate["reason"],
                        "raw_model_prob": round(avg_prob, 4),
                        "intelligence_prob": round(learned_prob, 4),
                        "adjusted_model_prob": round(final_yes_prob, 4),
                        "pre_calibration_model_prob": round(decision["adjusted_model_prob"], 4),
                        "calibrated_model_prob": round(final_yes_prob, 4),
                        "market_price_yes": round(sanity["yes_price"], 4),
                        "market_price_no": round(sanity["no_price"], 4),
                        "trade_side_market_price": round(trade_side_price, 4),
                        "trade_side_model_prob": round(trade_side_market_prob, 4),
                        "trade_side": decision["trade_side"],
                        "ensemble_spread": round(spread, 4),
                        "confidence_score": round(decision["confidence_score"], 4),
                        "regime": decision["regime"],
                        "bust_risk": round(decision["bust_risk"], 4),
                        "pre_yes_veto_prob": round(decision.get("pre_yes_veto_prob", decision["adjusted_model_prob"]), 4),
                        "pre_pattern_veto_prob": round(decision.get("pre_pattern_veto_prob", decision["adjusted_model_prob"]), 4),
                        "yes_veto_applied": bool(decision.get("yes_veto_applied", False)),
                        "no_veto_applied": bool(decision.get("no_veto_applied", False)),
                        "pattern_veto_applied": bool(decision.get("pattern_veto_applied", False)),
                        "spread_limit": round(decision["spread_limit"], 4),
                        "required_edge": round(decision["required_edge"], 4),
                        "base_required_edge": round(decision.get("base_required_edge", decision["required_edge"]), 4),
                        "price_band": decision.get("price_band"),
                        "required_confidence": round(decision.get("required_confidence", 0.0), 4),
                        "learning_confidence": round(intelligence["freshness_score"], 4),
                        "inefficiency_score": round(intelligence["inefficiency_score"], 4),
                        "segment_adjustment": round(intelligence["probability_delta"], 4),
                        "action": decision["action"],
                        "should_trade": decision["should_trade"],
                        "days_to_resolution": days_to_resolution,
                        "market_snapshot": sanity["snapshot"],
                        "learning_features": learning_payload,
                        "timestamp": str(datetime.now()),
                    })

                    can_trade_now = bool(decision["should_trade"]) and not entry_timing_gate["blocked"]
                    if can_trade_now:
                        action = decision["action"]
                        entry_price = sanity["yes_price"] if action == "BUY_YES" else sanity["no_price"]
                        if not self._are_market_prices_within_cap(sanity["yes_price"], sanity["no_price"]):
                            market_states[-1]["should_trade"] = False
                            market_states[-1]["price_cap_blocked"] = True
                            market_states[-1]["price_cap_reason"] = (
                                f"YES/NO price ladder {sanity['yes_price']:.4f}/{sanity['no_price']:.4f} "
                                f"exceeds hard cap {MAX_SIGNAL_ENTRY_PRICE:.2f}"
                            )
                            skipped["no_edge"] += 1
                            self.log(
                                f"[SIGNAL] | [X] NO TRADE: YES/NO price ladder "
                                f"{sanity['yes_price']:.4f}/{sanity['no_price']:.4f} exceeds hard cap "
                                f"{MAX_SIGNAL_ENTRY_PRICE:.2f}"
                            )
                            self.flush_signals(signals, market_states)
                            self.log(f"[SIGNAL] +------------------------------------------")
                            continue
                        self.log(
                            f"[SIGNAL] | >>> TRADE SIGNAL: {action} | Mode: {decision['mode']} | "
                            f"Conf: {decision['confidence_score']:.2f} | Size x{decision['size_multiplier']:.2f} <<<"
                        )
                        candidate_signal = {
                            "market_id": market_id,
                            "condition_id": condition_id,
                            "question": question,
                            "market_date": market_date.isoformat(),
                            "city": market_context["city"],
                            "country": market_context["country"],
                            "country_code": market_context["country_code"],
                            "continent": market_context["continent"],
                            "timezone": market_context["timezone"],
                            "utc_offset_hours": round(market_context["utc_offset_hours"], 2),
                            "local_now": market_context["local_now"],
                            "local_date": market_context["local_date"],
                            "local_hour": market_context["local_hour"],
                            "local_peak_stage": market_context["local_peak_stage"],
                            "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                            "forecast_revision_delta": round(market_context["forecast_revision_delta"], 4),
                            "forecast_revision_volatility": round(market_context["forecast_revision_volatility"], 4),
                            "forecast_revision_direction": market_context["forecast_revision_direction"],
                            "settlement_risk": round(market_context["settlement_risk"], 4),
                            "rounding_risk": round(market_context["rounding_risk"], 4),
                            "station_mismatch_risk": round(market_context["station_mismatch_risk"], 4),
                            "observation_progress": round(market_context["observation_progress"], 4),
                            "resolution_source": market_context.get("resolution_source"),
                            "resolution_station_name": market_context.get("resolution_station_name"),
                            "resolution_station_url": market_context.get("resolution_station_url"),
                            "resolution_station_id": market_context.get("resolution_station_id"),
                            "resolution_coordinates_applied": bool(market_context.get("resolution_coordinates_applied")),
                            "entry_timing_blocked": bool(entry_timing_gate["blocked"]),
                            "entry_timing_reason": entry_timing_gate["reason"],
                            "target": target,
                            "forecast_data": {k: round(v, 2) for k, v in forecast_data.items()},
                            "exact_target_distance": round(market_context.get("exact_target_distance", 0.0), 4) if market_context.get("exact_target_distance") is not None else None,
                            "temperature_unit": "fahrenheit" if market_context.get("country_code") == "US" else "celsius",
                            "location_lat": round(float(market_context.get("lat")), 5) if market_context.get("lat") is not None else None,
                            "location_lon": round(float(market_context.get("lon")), 5) if market_context.get("lon") is not None else None,
                            "avg_model_prob": round(avg_prob, 4),
                            "intelligence_prob": round(learned_prob, 4),
                            "adjusted_model_prob": round(final_yes_prob, 4),
                            "pre_calibration_model_prob": round(decision["adjusted_model_prob"], 4),
                            "calibrated_model_prob": round(final_yes_prob, 4),
                            "market_price_yes": round(sanity["yes_price"], 4),
                            "market_price_no": round(sanity["no_price"], 4),
                            "market_price": round(market_price, 4),
                            "entry_price": round(entry_price, 4),
                            "trade_side": decision["trade_side"],
                            "edge": round(decision["edge"], 4),
                            "abs_edge": round(decision["abs_edge"], 4),
                            "action": action,
                            "mode": decision["mode"],
                            "ensemble_spread": round(spread, 4),
                            "confidence_score": round(decision["confidence_score"], 4),
                            "size_multiplier": round(decision["size_multiplier"], 4),
                            "conviction": spread <= decision["spread_limit"],
                            "regime": decision["regime"],
                            "bust_risk": round(decision["bust_risk"], 4),
                            "exact_target_distance": round(market_context.get("exact_target_distance", 0.0), 4) if market_context.get("exact_target_distance") is not None else None,
                            "pre_yes_veto_prob": round(decision.get("pre_yes_veto_prob", decision["adjusted_model_prob"]), 4),
                            "pre_pattern_veto_prob": round(decision.get("pre_pattern_veto_prob", decision["adjusted_model_prob"]), 4),
                            "yes_veto_applied": bool(decision.get("yes_veto_applied", False)),
                            "no_veto_applied": bool(decision.get("no_veto_applied", False)),
                            "pattern_veto_applied": bool(decision.get("pattern_veto_applied", False)),
                            "days_to_resolution": days_to_resolution,
                            "required_edge": round(decision["required_edge"], 4),
                            "base_required_edge": round(decision.get("base_required_edge", decision["required_edge"]), 4),
                            "price_band": decision.get("price_band"),
                            "required_confidence": round(decision.get("required_confidence", 0.0), 4),
                            "learning_confidence": round(intelligence["freshness_score"], 4),
                            "inefficiency_score": round(intelligence["inefficiency_score"], 4),
                            "segment_adjustment": round(intelligence["probability_delta"], 4),
                            "learning_features": learning_payload,
                            "market_snapshot": sanity["snapshot"],
                            "timestamp": str(datetime.now())
                        }
                        candidate_score = self._signal_strength(candidate_signal)
                        self.log(
                            f"[SIGNAL] | Candidate strength: edge={candidate_signal['abs_edge']:.4f}, "
                            f"conf={candidate_signal['confidence_score']:.4f}, "
                            f"settlement_risk={candidate_signal['settlement_risk']:.4f}, "
                            f"score={candidate_score}"
                        )
                        if event_best_score is None or candidate_score > event_best_score:
                            event_best_signal = candidate_signal
                            event_best_score = candidate_score
                            self.log(f"[SIGNAL] | [BEST] Promoted current rung as top candidate for this ladder.")
                    else:
                        if entry_timing_gate["blocked"] and decision["should_trade"]:
                            reason = entry_timing_gate["reason"]
                            skipped["forecast_timing"] += 1
                        else:
                            reason = self._skip_reason(decision)
                            skipped["no_edge"] += 1
                        self.log(f"[SIGNAL] | [X] NO TRADE: {reason}")

                except Exception as e:
                    self.log(f"[SIGNAL] | ERROR processing market {market_id}: {e}")
                    import traceback
                    try:
                        traceback.print_exc()
                    except OSError:
                        pass
                    skipped["error"] += 1

                # Keep the on-disk snapshot fresh for the executor and open-trade monitor.
                self.flush_signals(signals, market_states)
                self.log(f"[SIGNAL] +------------------------------------------")

            if event_best_signal is not None:
                signals.append(event_best_signal)
                self.log(
                    f"[SIGNAL] | Selected strongest rung for ladder: "
                    f"{event_best_signal['action']} | {event_best_signal['question']}"
                )
                self.flush_signals(signals, market_states)
            else:
                self.log(f"[SIGNAL] | No valid trade candidate survived for this ladder.")

        # Summary
        elapsed = (datetime.now() - start_time).total_seconds()
        self.log(f"\n[SIGNAL] {'='*55}")
        self.log(f"[SIGNAL]   SCAN #{self.run_count} COMPLETE -- {elapsed:.1f}s elapsed")
        self.log(f"[SIGNAL]   Markets analyzed: {len(active_markets)}")
        self.log(f"[SIGNAL]   Signals generated: {len(signals)}")
        self.log(f"[SIGNAL]   Skipped breakdown: {skipped}")
        self.log(f"[SIGNAL] {'='*55}")

        self.save_signals(signals, diagnostics={
            "markets_found": len(active_markets),
            "discovery": discovery_diagnostics,
            "skipped": skipped,
            "elapsed_seconds": round(elapsed, 1),
        }, market_states=market_states)
        self._save_forecast_history()

    def _skip_reason(self, decision):
        """Human-readable reason for skipping a trade."""
        reasons = decision.get("reasons", [])
        if not reasons:
            return "Filtered by risk controls"
        return "; ".join(reasons)

    def _signal_strength(self, signal):
        """
        Rank sibling ladder candidates by edge quality first, then confidence and
        execution cleanliness. Higher tuples win.
        """
        return (
            float(signal.get("abs_edge", 0.0)),
            float(signal.get("confidence_score", 0.0)),
            float(signal.get("learning_confidence", 0.0)),
            -float(signal.get("settlement_risk", 0.0)),
            -float(signal.get("rounding_risk", 0.0)),
            -float(signal.get("ensemble_spread", 0.0)),
            -float(signal.get("entry_price", 1.0)),
        )

    def _summarize_market_dates(self, markets):
        counts_by_date = {}
        sample_events_by_date = {}

        for market in markets:
            question = market.get("_display_question") or market.get("question", "")
            market_date = self.extract_market_date(question, market)
            if not market_date:
                continue

            iso_date = market_date.isoformat()
            counts_by_date[iso_date] = counts_by_date.get(iso_date, 0) + 1

            event_label = question.split(" :: ")[0] if " :: " in question else question
            samples = sample_events_by_date.setdefault(iso_date, [])
            if event_label and event_label not in samples and len(samples) < 5:
                samples.append(event_label)

        return {
            "dates": sorted(counts_by_date.keys()),
            "counts_by_date": {key: counts_by_date[key] for key in sorted(counts_by_date.keys())},
            "sample_events_by_date": {key: sample_events_by_date[key] for key in sorted(sample_events_by_date.keys())},
        }

    def _run_market_sanity_checks(self, market, question, target, raw_prices):
        issues = []
        yes_price = self._safe_float(raw_prices[0]) if len(raw_prices) >= 1 else None
        no_price = self._safe_float(raw_prices[1]) if len(raw_prices) >= 2 else None

        if yes_price is None or no_price is None:
            issues.append("Market is missing a full YES/NO price ladder")
            return {"issues": issues, "yes_price": 0.0, "no_price": 0.0, "snapshot": {}}

        if not self._is_prob(yes_price) or not self._is_prob(no_price):
            issues.append("Outcome prices are outside valid probability bounds")

        if abs((yes_price + no_price) - 1.0) > 0.06:
            issues.append("Outcome prices look inconsistent or stale")

        if yes_price <= 0.03 or yes_price >= 0.97 or no_price <= 0.03 or no_price >= 0.97:
            issues.append("Price is too close to 0 or 1 for safe execution")

        liquidity = self._extract_market_metric(market, ["liquidityClob", "liquidity", "liquidityNum"])
        if liquidity is not None and liquidity < 250:
            issues.append(f"Illiquid market (liquidity={liquidity:.2f})")

        volume_24h = self._extract_market_metric(market, ["volume24hr", "volume24hrClob", "volumeNum", "volume"])
        if volume_24h is not None and volume_24h < 100:
            issues.append(f"Low recent volume (24h volume={volume_24h:.2f})")

        best_bid = self._extract_market_metric(market, ["bestBid", "best_bid"])
        best_ask = self._extract_market_metric(market, ["bestAsk", "best_ask"])
        if best_bid is not None and best_ask is not None and best_ask > best_bid:
            quoted_spread = best_ask - best_bid
            if quoted_spread > 0.10:
                issues.append(f"Quoted spread too wide ({quoted_spread:.2%})")

        if self._has_settlement_ambiguity(market, question, target):
            issues.append("Settlement rules look ambiguous for this market")

        snapshot = {
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "liquidity": round(liquidity, 2) if liquidity is not None else None,
            "volume_24h": round(volume_24h, 2) if volume_24h is not None else None,
            "best_bid": round(best_bid, 4) if best_bid is not None else None,
            "best_ask": round(best_ask, 4) if best_ask is not None else None,
        }
        return {"issues": issues, "yes_price": yes_price, "no_price": no_price, "snapshot": snapshot}

    def _has_settlement_ambiguity(self, market, question, target):
        text_parts = [
            question,
            market.get("description", ""),
            market.get("rules", ""),
            market.get("resolutionSource", ""),
            market.get("title", ""),
            market.get("groupItemTitle", ""),
        ]
        rules_blob = " ".join(part for part in text_parts if isinstance(part, str)).lower()

        ambiguous_terms = [
            "subject to interpretation",
            "discretion",
            "manual review",
            "clarification",
            "unclear",
            "revised later",
        ]
        if any(term in rules_blob for term in ambiguous_terms):
            return True

        if target["type"] not in {"exact", "range"}:
            return False

        clarity_terms = [
            "rounded",
            "rounding",
            "nearest degree",
            "nearest whole degree",
            "official high temperature",
            "official low temperature",
        ]
        if any(term in rules_blob for term in clarity_terms):
            return False

        return not self._is_standard_temperature_ladder(question, target)

    def _is_standard_temperature_ladder(self, question, target):
        """
        Allow routine Polymarket temperature ladder rungs to pass even when the
        market text omits older settlement wording like "rounded".
        """
        q_lower = (question or "").lower()
        if not any(phrase in q_lower for phrase in ("highest temperature", "lowest temperature")):
            return False

        if not re.search(r'\bon\s+[a-z]+\s+\d{1,2}\b', q_lower):
            return False

        if target["type"] == "exact":
            return bool(
                re.search(
                    rf'\b(?:be|hit|reach)\s+\d+(?:\.\d+)?\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c|degrees)\b',
                    q_lower,
                )
            )

        if target["type"] == "range":
            return bool(
                re.search(
                    rf'\bbetween\s+\d+(?:\.\d+)?\s*(?:{DEGREE_OPTIONAL_PATTERN}(?:f|c)?)?\s*(?:-|and|to)\s*\d+(?:\.\d+)?',
                    q_lower,
                )
                or re.search(
                    rf'\d+(?:\.\d+)?\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c)?\s*(?:-|to)\s*\d+(?:\.\d+)?',
                    q_lower,
                )
            )

        return False

    def _extract_market_metric(self, market, keys):
        for key in keys:
            value = market.get(key)
            if value is None:
                continue
            numeric = self._safe_float(value)
            if numeric is not None:
                return numeric
        return None

    def _safe_float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _is_prob(self, value):
        return value is not None and 0.0 < value < 1.0

    def extract_target(self, question):
        """
        Extracts target threshold or range from question.
        Returns:
        - {"type": "threshold", "val": X}
        - {"type": "range", "low": X, "high": Y}
        - {"type": "exact", "val": X}
        """
        q_lower = question.lower()

        # 1. Range Pattern (e.g. "78-79F", "between 60 and 61", "60 to 61")
        range_match = re.search(
            rf'(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c)?\s*(?:-|to)\s*(\d+(?:\.\d+)?)',
            q_lower,
        )
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if low < high and low > -50 and high < 150:
                return {"type": "range", "low": low, "high": high}

        # 2. "between X and Y" pattern
        between_match = re.search(
            rf'between\s+(\d+(?:\.\d+)?)\s*(?:{DEGREE_OPTIONAL_PATTERN}(?:f|c)?)?\s+and\s+(\d+(?:\.\d+)?)',
            q_lower,
        )
        if between_match:
            low = float(between_match.group(1))
            high = float(between_match.group(2))
            if low < high:
                return {"type": "range", "low": low, "high": high}

        # 3. Threshold ladder phrasing (e.g. "72F or higher", "72F or above", "53F or below")
        above_rung_match = re.search(
            rf'(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c|degrees)?\s+or (?:higher|above)\b',
            q_lower,
        )
        if above_rung_match:
            return {"type": "threshold", "direction": "above", "val": float(above_rung_match.group(1))}

        below_rung_match = re.search(
            rf'(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c|degrees)?\s+or below\b',
            q_lower,
        )
        if below_rung_match:
            return {"type": "threshold", "direction": "below", "val": float(below_rung_match.group(1))}

        # 4. Exact Pattern (e.g. "exactly 7C", "be exactly 7")
        exact_match = re.search(r'exactly\s+(\d+(?:\.\d+)?)', q_lower)
        if exact_match:
            return {"type": "exact", "val": float(exact_match.group(1))}

        # 5. Exact settlement phrasing common in ladder rungs
        exact_be_match = re.search(
            rf'\b(?:be|hit|reach)\s+(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c|degrees)\b',
            q_lower,
        )
        if exact_be_match:
            return {"type": "exact", "val": float(exact_be_match.group(1))}

        # 6. Threshold patterns
        above_match = re.search(r'(?:above|over|higher than|at least|exceed|>=)\s*(\d+(?:\.\d+)?)', q_lower)
        if above_match:
            return {"type": "threshold", "direction": "above", "val": float(above_match.group(1))}

        below_match = re.search(r'(?:below|under|lower than|at most|<=)\s*(\d+(?:\.\d+)?)', q_lower)
        if below_match:
            return {"type": "threshold", "direction": "below", "val": float(below_match.group(1))}

        # 7. Fallback: find a temperature number near F or C markers
        temp_match = re.search(
            rf'(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c|degrees)',
            q_lower,
        )
        if temp_match:
            return {"type": "threshold", "direction": "above", "val": float(temp_match.group(1))}

        # 8. Last resort: any standalone number (less reliable)
        any_num = re.search(r'(\d+)', question)
        if any_num:
            return {"type": "threshold", "direction": "above", "val": float(any_num.group(1))}

        return None

    def group_markets_by_event(self, markets):
        grouped = {}
        for market in markets:
            display = market.get("_display_question") or market.get("question", "Unknown")
            event_label = display.split(" :: ")[0] if " :: " in display else display
            grouped.setdefault(event_label, []).append(market)
        return dict(sorted(grouped.items(), key=lambda item: item[0]))

    def sort_markets_by_rung(self, markets):
        return sorted(markets, key=self._market_rung_sort_key)

    def _market_rung_sort_key(self, market):
        display = market.get("_display_question") or market.get("question", "")
        question = market.get("question", "")
        combined = f"{display} {question}".lower()

        below_match = re.search(rf'(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c)?\s+or below', combined)
        if below_match:
            return (float(below_match.group(1)), -1)

        range_match = re.search(r'between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)', combined)
        if range_match:
            return (float(range_match.group(1)), 0)

        single_match = re.search(rf'be\s+(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c)\b', combined)
        if single_match:
            return (float(single_match.group(1)), 1)

        above_match = re.search(rf'(\d+(?:\.\d+)?)\s*{DEGREE_OPTIONAL_PATTERN}(?:f|c)?\s+or (?:higher|above)', combined)
        if above_match:
            return (float(above_match.group(1)), 2)

        return (9999.0, 9)

    def extract_market_date(self, question, market=None):
        """Parse the exact market date from the question and fall back to market metadata for year inference."""
        q_lower = question.lower()
        month_names = "|".join(self.month_map.keys())
        match = re.search(rf'\b(?:on|by|for)\s+({month_names})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b', q_lower)

        if match:
            month = self.month_map[match.group(1)]
            day = int(match.group(2))
            year = int(match.group(3)) if match.group(3) else self._infer_market_year(month, day, market)
            try:
                return date(year, month, day)
            except ValueError:
                return None

        return self._market_date_from_metadata(market)

    def _infer_market_year(self, month, day, market=None):
        metadata_date = self._market_date_from_metadata(market)
        if metadata_date:
            return metadata_date.year

        today = datetime.utcnow().date()
        inferred_year = today.year
        candidate = date(inferred_year, month, day)
        if candidate < today and (today - candidate).days > 180:
            inferred_year += 1
        return inferred_year

    def _market_date_from_metadata(self, market=None):
        if not market:
            return None

        for field in ("endDate", "startDate"):
            raw_value = market.get(field)
            if not raw_value:
                continue

            try:
                normalized = raw_value.replace("Z", "+00:00")
                return datetime.fromisoformat(normalized).date()
            except ValueError:
                continue

        return None

    def extract_predicted_temps(self, forecast, is_us, market_date):
        if isinstance(forecast, dict):
            enhanced_sources = forecast.get("enhanced_sources", {})
            if isinstance(enhanced_sources, dict) and enhanced_sources:
                return {
                    str(name): float(value)
                    for name, value in enhanced_sources.items()
                    if value is not None
                }

        if is_us:
            return self._extract_noaa_temperatures(forecast, market_date)
        else:
            return self._extract_open_meteo_temperatures(forecast, market_date)

    def _extract_noaa_temperatures(self, forecast, market_date):
        periods = forecast.get("hourly_periods", []) if isinstance(forecast, dict) else forecast
        if not isinstance(periods, list) or len(periods) == 0:
            return {}

        same_day_periods = []
        for period in periods:
            if not isinstance(period, dict):
                continue

            start_time = period.get("startTime")
            if not start_time:
                continue

            try:
                period_date = datetime.fromisoformat(start_time).date()
            except ValueError:
                continue

            if period_date == market_date:
                same_day_periods.append(period)

        periods_to_use = same_day_periods if same_day_periods else periods[:24]
        daytime_temps = [
            float(period.get("temperature"))
            for period in periods_to_use
            if isinstance(period, dict)
            and period.get("temperature") is not None
            and period.get("isDaytime", True)
        ]

        if daytime_temps:
            return {"noaa": max(daytime_temps)}

        all_temps = [
            float(period.get("temperature"))
            for period in periods_to_use
            if isinstance(period, dict) and period.get("temperature") is not None
        ]
        if all_temps:
            return {"noaa": max(all_temps)}

        return {}

    def _extract_open_meteo_temperatures(self, forecast, market_date):
        hourly = forecast.get("hourly", forecast) if isinstance(forecast, dict) else {}
        if not isinstance(hourly, dict):
            return {}

        times = hourly.get("time", [])
        result = {}

        ecmwf_temps = self._extract_hourly_day_max(times, hourly.get("temperature_2m_ecmwf_ifs025", []), market_date)
        if ecmwf_temps is not None:
            result["ecmwf"] = ecmwf_temps

        gfs_temps = self._extract_hourly_day_max(times, hourly.get("temperature_2m_gfs_seamless", []), market_date)
        if gfs_temps is not None:
            result["gfs"] = gfs_temps

        if not result:
            generic_temp = self._extract_hourly_day_max(times, hourly.get("temperature_2m", []), market_date)
            if generic_temp is not None:
                result["generic"] = generic_temp

        return result

    def build_market_context(self, location, forecast, market_date):
        timezone_name = self._resolve_timezone_name(location, forecast)
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            timezone_name = "UTC"
            tz = timezone.utc

        local_now_dt = datetime.now(tz)
        days_to_resolution = max((market_date - local_now_dt.date()).days, 0)
        local_hour = int(local_now_dt.hour)
        local_peak_stage, local_peak_stage_detail = self._classify_local_peak_stage(
            local_now_dt.date(),
            local_hour,
            market_date,
        )
        utc_offset = local_now_dt.utcoffset()
        utc_offset_hours = (utc_offset.total_seconds() / 3600.0) if utc_offset is not None else 0.0

        return {
            "city": location.get("city"),
            "lat": location.get("lat"),
            "lon": location.get("lon"),
            "country": location.get("country"),
            "country_code": location.get("country_code"),
            "continent": location.get("continent", "Unknown"),
            "timezone": timezone_name,
            "utc_offset_hours": utc_offset_hours,
            "local_now": local_now_dt.isoformat(),
            "local_date": local_now_dt.date().isoformat(),
            "local_hour": local_hour,
            "local_peak_stage": local_peak_stage,
            "local_peak_stage_detail": local_peak_stage_detail,
            "days_to_resolution": days_to_resolution,
            "resolution_source": location.get("resolution_source"),
            "resolution_station_name": location.get("resolution_station_name"),
            "resolution_station_url": location.get("resolution_station_url"),
            "resolution_station_id": location.get("resolution_station_id") or location.get("station_id"),
            "resolution_station_city": location.get("resolution_station_city"),
            "resolution_coordinates_applied": bool(location.get("resolution_coordinates_applied", False)),
        }

    def _current_local_date(self, location):
        timezone_name = location.get("timezone") or "UTC"
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz).date()

    def _is_current_local_market_date(self, location, market_date):
        return market_date == self._current_local_date(location)

    def _enrich_market_context(self, market_context, forecast, forecast_data, target, market_date, is_us):
        history_key = self._forecast_history_key(market_context, market_date)
        avg_forecast = sum(float(v) for v in forecast_data.values()) / max(len(forecast_data), 1)
        history = self.forecast_history.get(history_key, [])
        prior_values = [float(item.get("avg_forecast", avg_forecast)) for item in history if item.get("avg_forecast") is not None]
        prior_avg = prior_values[-1] if prior_values else avg_forecast
        revision_delta = avg_forecast - prior_avg
        revision_volatility = 0.0
        if prior_values:
            revision_volatility = max(prior_values + [avg_forecast]) - min(prior_values + [avg_forecast])

        forecast_revision_direction = "flat"
        if revision_delta >= 0.35:
            forecast_revision_direction = "up"
        elif revision_delta <= -0.35:
            forecast_revision_direction = "down"

        target_val = float(target.get("val", avg_forecast)) if target.get("type") in {"threshold", "exact"} else avg_forecast
        avg_distance = abs(avg_forecast - target_val)
        station_mismatch_risk = 0.06 if is_us else 0.16
        if len(forecast_data) <= 1:
            station_mismatch_risk += 0.05
        if market_context["days_to_resolution"] == 0 and market_context["local_hour"] < 11:
            station_mismatch_risk += 0.04

        rounding_risk = 0.0
        if target.get("type") == "threshold":
            rounding_risk = max(0.0, 1.0 - min(avg_distance / 1.0, 1.0)) * 0.22
        elif target.get("type") in {"exact", "range"}:
            rounding_risk = 0.18

        observation_progress = self._observation_progress(market_context)
        settlement_risk = min(
            0.95,
            station_mismatch_risk
            + rounding_risk
            + min(revision_volatility / 3.0, 0.18)
            + ((1.0 - observation_progress) * 0.14),
        )

        # Calculate temperature dispersion (std dev of forecasts)
        if forecast_data and len(forecast_data) > 1:
            forecasts = [float(v) for v in forecast_data.values()]
            mean_forecast = sum(forecasts) / len(forecasts)
            variance = sum((x - mean_forecast) ** 2 for x in forecasts) / len(forecasts)
            temp_dispersion = variance ** 0.5
        else:
            temp_dispersion = 0.0
        forecast_values = [float(v) for v in forecast_data.values()] if forecast_data else []
        forecast_min = min(forecast_values) if forecast_values else avg_forecast
        forecast_max = max(forecast_values) if forecast_values else avg_forecast

        exact_rounding_consensus = 0.0
        exact_rounding_protected = False
        exact_target_distance = abs(avg_forecast - target_val) if target.get("type") == "exact" else None
        if target.get("type") == "exact" and forecast_data:
            target_val = float(target.get("val", avg_forecast))
            exact_target_distance = abs(avg_forecast - target_val)
            if math.isclose(target_val, round(target_val), abs_tol=1e-9):
                forecasts = [float(v) for v in forecast_data.values()]
                rounded_hits = [
                    1.0
                    if float(self.model._round_half_away_from_zero(temp)) == target_val
                    else 0.0
                    for temp in forecasts
                ]
                exact_rounding_consensus = sum(rounded_hits) / len(rounded_hits)
                exact_rounding_protected = (
                    exact_rounding_consensus >= 0.999
                    and max(abs(temp - target_val) for temp in forecasts) <= 0.5
                )

        history.append({
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "avg_forecast": round(avg_forecast, 4),
            "forecast_revision_delta": round(revision_delta, 4),
        })
        self.forecast_history[history_key] = history[-12:]

        return {
            **market_context,
            "forecast_revision_delta": revision_delta,
            "forecast_revision_volatility": revision_volatility,
            "forecast_revision_direction": forecast_revision_direction,
            "station_mismatch_risk": station_mismatch_risk,
            "rounding_risk": rounding_risk,
            "settlement_risk": settlement_risk,
            "observation_progress": observation_progress,
            "temp_dispersion": temp_dispersion,
            "forecast_avg": round(avg_forecast, 4),
            "forecast_min": round(forecast_min, 4),
            "forecast_max": round(forecast_max, 4),
            "forecast_data": forecast_data,
            "exact_rounding_consensus": exact_rounding_consensus,
            "exact_rounding_protected": exact_rounding_protected,
            "exact_target_distance": round(exact_target_distance, 4) if exact_target_distance is not None else None,
            "calibration_buckets": {},
        }

    def _forecast_freshness_snapshot(self, forecast, market_context, market_date):
        local_tz_name = market_context.get("timezone") or "UTC"
        try:
            local_tz = ZoneInfo(local_tz_name)
        except Exception:
            local_tz = timezone.utc

        local_now = self._parse_timestamp(market_context.get("local_now"), default_tz=local_tz)
        if local_now is None:
            local_now = datetime.now(local_tz)

        fetched_at = self._parse_timestamp((forecast or {}).get("fetched_at"))
        bundle_age_minutes = 9999.0
        if fetched_at is not None:
            bundle_age_minutes = max(0.0, (local_now - fetched_at.astimezone(local_tz)).total_seconds() / 60.0)

        metar = (forecast or {}).get("metar") or {}
        metar_observed_at = None
        for key in ("obsTime", "observation_time", "timestamp", "date"):
            metar_observed_at = self._parse_timestamp(metar.get(key))
            if metar_observed_at is not None:
                break

        metar_age_hours = 9999.0
        if metar_observed_at is not None:
            metar_age_hours = max(0.0, (local_now - metar_observed_at.astimezone(local_tz)).total_seconds() / 3600.0)

        provider_snapshot = self._provider_freshness_snapshot(forecast, local_now, local_tz)

        return {
            "forecast_fetched_at": fetched_at.astimezone(local_tz).isoformat() if fetched_at is not None else None,
            "forecast_bundle_age_minutes": bundle_age_minutes,
            "metar_observed_at": metar_observed_at.astimezone(local_tz).isoformat() if metar_observed_at is not None else None,
            "metar_age_hours": metar_age_hours,
            "metar_available": bool(metar_observed_at is not None),
            "same_day_market": bool(market_context.get("local_date") == market_date.isoformat()),
            **provider_snapshot,
        }

    def _freshness_gate(self, market_context, market_date):
        if market_context.get("local_date") != market_date.isoformat():
            return {"blocked": False, "reason": ""}

        bundle_age_minutes = float(market_context.get("forecast_bundle_age_minutes", 9999.0))
        local_hour = int(market_context.get("local_hour", -1))
        has_station = bool(market_context.get("resolution_station_name"))
        metar_age_hours = float(market_context.get("metar_age_hours", 9999.0))
        metar_available = bool(market_context.get("metar_available", False))
        provider_issue_age_minutes = float(market_context.get("provider_issue_age_minutes", 9999.0))
        provider_issue_source = market_context.get("provider_issue_source") or ""

        if bundle_age_minutes > 15.0:
            return {
                "blocked": True,
                "reason": f"Forecast bundle is too old for same-day trading ({bundle_age_minutes:.1f} minutes old).",
            }

        if provider_issue_source in {"noaa_update_time", "noaa_generated_at", "noaa_points_update_time"} and provider_issue_age_minutes > 240.0:
            return {
                "blocked": True,
                "reason": (
                    f"Upstream forecast issue time looks stale for same-day trading "
                    f"({provider_issue_age_minutes:.1f} minutes since provider update)."
                ),
            }

        if has_station and metar_available and local_hour >= 15 and metar_age_hours > 2.0:
            return {
                "blocked": True,
                "reason": f"Station observation is too stale for late same-day trading ({metar_age_hours:.2f} hours old).",
            }

        if has_station and metar_available and local_hour >= 12 and metar_age_hours > 3.0:
            return {
                "blocked": True,
                "reason": f"Station observation is too stale for same-day trading ({metar_age_hours:.2f} hours old).",
            }

        return {"blocked": False, "reason": ""}

    def _provider_freshness_snapshot(self, forecast, local_now, local_tz):
        source_priority = ["noaa", "ecmwf", "hrrr", "gfs", "open_meteo"]
        explicit_issue_sources = {"noaa_update_time", "noaa_generated_at", "noaa_points_update_time"}
        selected = None

        for source_name in source_priority:
            payload = (forecast or {}).get(source_name)
            if not isinstance(payload, dict):
                continue
            issued_at = self._parse_timestamp(payload.get("provider_issued_at"))
            if issued_at is None:
                continue
            issue_source = payload.get("provider_issued_at_source") or "unknown"
            candidate = {
                "provider_primary_source": source_name,
                "provider_issued_at": issued_at.astimezone(local_tz).isoformat(),
                "provider_issue_age_minutes": max(
                    0.0,
                    (local_now - issued_at.astimezone(local_tz)).total_seconds() / 60.0,
                ),
                "provider_issue_source": issue_source,
            }
            if issue_source in explicit_issue_sources:
                return candidate
            if selected is None:
                selected = candidate

        if selected is not None:
            return selected

        return {
            "provider_primary_source": None,
            "provider_issued_at": None,
            "provider_issue_age_minutes": 9999.0,
            "provider_issue_source": None,
        }

    def _format_metar_freshness(self, market_context):
        if not market_context.get("metar_available"):
            return "METAR unavailable"
        return f"METAR age={float(market_context.get('metar_age_hours', 9999.0)):.2f}h"

    def _parse_timestamp(self, value, default_tz=timezone.utc):
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value
            return value.replace(tzinfo=default_tz)
        if isinstance(value, (int, float)):
            try:
                numeric = float(value)
                if numeric > 1_000_000_000_000:
                    numeric /= 1000.0
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if not value:
            return None
        raw_value = str(value).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw_value):
            try:
                numeric = float(raw_value)
                if numeric > 1_000_000_000_000:
                    numeric /= 1000.0
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            normalized = raw_value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed
        return parsed.replace(tzinfo=default_tz)

    def _observation_progress(self, market_context):
        if market_context["days_to_resolution"] > 0:
            return 0.15
        if market_context["local_peak_stage"] == "pre_peak":
            return 0.30 if market_context["local_hour"] < 9 else 0.45
        if market_context["local_peak_stage"] == "near_peak":
            return 0.70
        return 0.90

    def _forecast_history_key(self, market_context, market_date):
        return f"{market_context.get('city','?')}|{market_context.get('country_code','?')}|{market_date.isoformat()}"

    def _forecast_cache_key(self, lat, lon, is_us, location, market_date):
        station_id = location.get("resolution_station_id") or location.get("station_id") or ""
        timezone_name = location.get("timezone") or "UTC"
        return (
            round(float(lat), 4),
            round(float(lon), 4),
            bool(is_us),
            str(market_date.isoformat()),
            str(station_id).upper(),
            str(timezone_name),
        )

    def _get_cached_forecast(self, lat, lon, is_us, location, market_date):
        cache_key = self._forecast_cache_key(lat, lon, is_us, location, market_date)
        if cache_key in self._run_forecast_cache:
            return self._run_forecast_cache[cache_key]

        forecast = self.weather.get_forecast(
            lat,
            lon,
            is_us,
            location=location,
            market_date=market_date,
        )
        self._run_forecast_cache[cache_key] = forecast
        return forecast

    def _load_forecast_history(self):
        try:
            with open(self.forecast_history_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_forecast_history(self):
        try:
            os.makedirs(os.path.dirname(self.forecast_history_path), exist_ok=True)
            with open(self.forecast_history_path, "w", encoding="utf-8") as handle:
                json.dump(self.forecast_history, handle, indent=2)
        except OSError:
            pass

    def _resolve_timezone_name(self, location, forecast):
        if isinstance(forecast, dict):
            forecast_timezone = forecast.get("timezone")
            if forecast_timezone:
                return forecast_timezone
        return location.get("timezone") or "UTC"

    def _filter_markets_for_current_local_date(self, markets):
        filtered_markets = []
        filtered_count = 0

        for market in markets:
            question = market.get("_display_question") or market.get("question", "")
            location = self.markets.parse_market_location(question)
            market_date = self.extract_market_date(question, market)

            # Keep ambiguous markets so downstream skip accounting stays accurate.
            if not location or not market_date:
                filtered_markets.append(market)
                continue

            if self._is_current_local_market_date(location, market_date):
                filtered_markets.append(market)
                continue

            filtered_count += 1

        return filtered_markets, filtered_count

    def _classify_local_peak_stage(self, local_date, local_hour, market_date):
        if local_date < market_date:
            return "pre_peak", "pre_market_day"
        if local_date > market_date:
            return "post_peak", "post_market_day"
        if local_hour < 9:
            return "pre_peak", "overnight_pre_peak"
        if local_hour < 11:
            return "pre_peak", "morning_pre_peak"
        if local_hour < 15:
            return "near_peak", "midday_peak_window"
        if local_hour < 18:
            return "post_peak", "afternoon_post_peak"
        return "post_peak", "late_post_peak"

    def _extract_hourly_day_max(self, times, values, market_date):
        if not times or not values:
            return None

        matching_values = []
        for idx, time_str in enumerate(times):
            if idx >= len(values):
                break

            value = values[idx]
            if value is None:
                continue

            try:
                forecast_date = datetime.fromisoformat(time_str).date()
            except ValueError:
                continue

            if forecast_date == market_date:
                matching_values.append(float(value))

        if matching_values:
            return max(matching_values)

        fallback_values = [float(value) for value in values[:24] if value is not None]
        if fallback_values:
            return max(fallback_values)

        return None

    def _entry_timing_gate(self, market_context, market_date):
        if (
            market_context.get("country_code") == "US"
            and market_context.get("local_date") == market_date.isoformat()
            and int(market_context.get("local_hour", -1)) < 8
        ):
            timezone_name = market_context.get("timezone") or "local timezone"
            return {
                "blocked": True,
                "reason": f"U.S. same-day entries wait until 8:00 AM local time ({timezone_name}) for forecast stability.",
            }
        return {"blocked": False, "reason": ""}

    def _forecast_source_gate(self, forecast_data):
        source_count = len(forecast_data or {})
        if source_count < MIN_FORECAST_SOURCE_COUNT:
            return {
                "blocked": True,
                "reason": (
                    f"Forecast has only {source_count} source(s); "
                    f"minimum {MIN_FORECAST_SOURCE_COUNT} independent sources required."
                ),
            }
        return {"blocked": False, "reason": ""}

    def _fresh_forecast_intelligence(self, avg_prob, spread, forecast_data, target, market_context, sanity):
        """
        Stateless decision nudge that only consumes the forecast bundle from this
        scan. Historical trades, saved forecast history, and calibration buckets
        are intentionally excluded so stale data cannot pollute the live layer.
        """
        avg_prob = float(avg_prob)
        market_price = float((sanity or {}).get("yes_price", 0.5))
        source_count = len(forecast_data or {})
        bundle_age = float((market_context or {}).get("forecast_bundle_age_minutes", 9999.0) or 9999.0)
        provider_age = float((market_context or {}).get("provider_issue_age_minutes", 9999.0) or 9999.0)
        metar_age = float((market_context or {}).get("metar_age_hours", 9999.0) or 9999.0)
        local_date = str((market_context or {}).get("local_date") or "")
        market_date = str((market_context or {}).get("market_date") or "")
        same_day = bool(local_date and market_date and local_date == market_date)

        stale_reason = ""
        if same_day and bundle_age > 15.0:
            stale_reason = f"forecast bundle age {bundle_age:.1f}m exceeds fresh-only limit"
        elif same_day and provider_age > 240.0:
            stale_reason = f"provider issue age {provider_age:.1f}m exceeds fresh-only limit"
        elif same_day and bool((market_context or {}).get("metar_available")) and metar_age > 3.0:
            stale_reason = f"station observation age {metar_age:.2f}h exceeds fresh-only limit"

        if stale_reason:
            return {
                "enabled": False,
                "fresh_forecast_only": True,
                "used_history": False,
                "probability": avg_prob,
                "raw_probability": avg_prob,
                "evidence_probability": avg_prob,
                "probability_delta": 0.0,
                "blend_weight": 0.0,
                "freshness_score": 0.0,
                "inefficiency_score": abs(avg_prob - market_price),
                "reason": stale_reason,
            }

        source_score = min(source_count / 4.0, 1.0)
        bundle_score = max(0.0, 1.0 - min(bundle_age / 15.0, 1.0)) if same_day else 0.70
        provider_score = max(0.0, 1.0 - min(provider_age / 240.0, 1.0)) if same_day else 0.70
        station_score = 0.70
        if bool((market_context or {}).get("metar_available")):
            station_score = max(0.0, 1.0 - min(metar_age / 3.0, 1.0))

        freshness_score = max(
            0.0,
            min(
                1.0,
                (bundle_score * 0.35)
                + (provider_score * 0.25)
                + (station_score * 0.20)
                + (source_score * 0.20),
            ),
        )
        temp_dispersion = float((market_context or {}).get("temp_dispersion", 0.0) or 0.0)
        dispersion_penalty = min(max(temp_dispersion / 0.30, 0.0), 1.0) * 0.18
        evidence_prob = avg_prob
        reasons = ["fresh forecast only"]

        target = target or {}
        if target.get("type") == "exact" and bool((market_context or {}).get("exact_rounding_protected", False)):
            target_distance = float((market_context or {}).get("exact_target_distance", 0.0) or 0.0)
            closeness = max(0.0, 1.0 - min(target_distance / 0.50, 1.0))
            consensus = float((market_context or {}).get("exact_rounding_consensus", 0.0) or 0.0)
            protected_floor = 0.58 + (0.14 * closeness) + (0.04 * min(consensus, 1.0))
            evidence_prob = max(evidence_prob, protected_floor - dispersion_penalty)
            reasons.append("protected exact-rung consensus")

        if target.get("type") in {"range", "threshold"}:
            forecast_avg = (market_context or {}).get("forecast_avg")
            if forecast_avg is not None and target.get("type") == "range":
                low = float(target.get("low", forecast_avg))
                high = float(target.get("high", forecast_avg))
                if low <= float(forecast_avg) <= high + 0.9 and temp_dispersion <= 0.08:
                    evidence_prob = min(0.92, evidence_prob + (0.04 * freshness_score))
                    reasons.append("low-dispersion range support")
            elif forecast_avg is not None and target.get("type") == "threshold":
                direction = str(target.get("direction") or "above")
                threshold = float(target.get("val", forecast_avg))
                margin = float(forecast_avg) - threshold
                aligned = (direction == "above" and margin >= 0.35) or (direction == "below" and margin <= -0.35)
                if aligned and temp_dispersion <= 0.08:
                    evidence_prob = min(0.92, evidence_prob + (0.03 * freshness_score))
                    reasons.append("low-dispersion threshold support")

        evidence_prob = min(max(evidence_prob, 0.01), 0.99)
        blend_weight = min(0.45, max(0.0, freshness_score * (0.22 + (source_score * 0.23))))
        decision_prob = avg_prob + ((evidence_prob - avg_prob) * blend_weight)
        decision_prob = min(max(decision_prob, 0.01), 0.99)

        return {
            "enabled": True,
            "fresh_forecast_only": True,
            "used_history": False,
            "probability": decision_prob,
            "raw_probability": avg_prob,
            "evidence_probability": evidence_prob,
            "probability_delta": decision_prob - avg_prob,
            "blend_weight": blend_weight,
            "freshness_score": freshness_score,
            "inefficiency_score": abs(decision_prob - market_price),
            "reason": "; ".join(reasons),
        }

    def _is_live_tradeable_location(self, location):
        is_us = bool((location or {}).get("is_us"))
        if self.live_market_scope == "all":
            return True
        if self.live_market_scope == "non_us":
            return not is_us
        return is_us

    def _build_learning_payload(
        self,
        avg_prob,
        market_price,
        spread,
        days_to_resolution,
        provisional_regime,
        target,
        market_context,
        sanity,
        entry_timing_gate,
        decision,
        intelligence=None,
    ):
        intelligence = intelligence or {
            "enabled": False,
            "fresh_forecast_only": True,
            "used_history": False,
            "probability": avg_prob,
            "raw_probability": avg_prob,
            "evidence_probability": avg_prob,
            "probability_delta": 0.0,
            "blend_weight": 0.0,
            "freshness_score": 0.0,
            "inefficiency_score": 0.0,
            "reason": "not evaluated",
        }
        target_type = str((target or {}).get("type") or "threshold")
        direction = str((target or {}).get("direction") or "above")
        source_count = len((market_context or {}).get("forecast_data", {}) or [])
        if market_price <= 0.12 or market_price >= 0.88:
            pricing_bucket = "extreme"
        elif market_price <= 0.22 or market_price >= 0.78:
            pricing_bucket = "selective"
        else:
            pricing_bucket = "preferred"

        return {
            "features": {
                "spread": round(spread, 6),
                "days_urgency": round(1.0 / (days_to_resolution + 1.0), 6),
                "temp_dispersion": round(float(market_context.get("temp_dispersion", 0.0) or 0.0) / 8.0, 6),
                "source_count": round(min(source_count / 4.0, 1.0), 6),
                "target_range": 1.0 if target_type == "range" else 0.0,
                "target_exact": 1.0 if target_type == "exact" else 0.0,
                "direction_below": 1.0 if direction == "below" else 0.0,
            },
            "meta": {
                "regime": provisional_regime,
                "target_type": target_type,
                "direction": direction,
                "pricing_bucket": pricing_bucket,
                "source_count_bucket": str(min(source_count, 4)),
                "local_peak_stage": market_context["local_peak_stage"],
                "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                "timezone": market_context["timezone"],
                "continent": market_context["continent"],
                "city": market_context["city"],
                "country": market_context["country"],
                "country_code": market_context["country_code"],
                "utc_offset_hours": market_context["utc_offset_hours"],
                "local_now": market_context["local_now"],
                "local_hour": market_context["local_hour"],
                "local_date": market_context["local_date"],
                "forecast_revision_direction": market_context["forecast_revision_direction"],
                "forecast_fetched_at": market_context.get("forecast_fetched_at"),
                "forecast_bundle_age_minutes": round(market_context.get("forecast_bundle_age_minutes", 0.0), 3),
                "provider_primary_source": market_context.get("provider_primary_source"),
                "provider_issued_at": market_context.get("provider_issued_at"),
                "provider_issue_age_minutes": round(market_context.get("provider_issue_age_minutes", 0.0), 3),
                "provider_issue_source": market_context.get("provider_issue_source"),
                "metar_observed_at": market_context.get("metar_observed_at"),
                "metar_age_hours": round(market_context.get("metar_age_hours", 0.0), 3),
                "resolution_source": market_context.get("resolution_source"),
                "resolution_station_name": market_context.get("resolution_station_name"),
                "resolution_station_url": market_context.get("resolution_station_url"),
                "resolution_station_id": market_context.get("resolution_station_id"),
                "resolution_coordinates_applied": bool(market_context.get("resolution_coordinates_applied")),
                "location_lat": round(float(market_context.get("lat")), 5) if market_context.get("lat") is not None else None,
                "location_lon": round(float(market_context.get("lon")), 5) if market_context.get("lon") is not None else None,
                "temperature_unit": "fahrenheit" if market_context.get("country_code") == "US" else "celsius",
                "entry_timing_blocked": bool(entry_timing_gate["blocked"]),
                "entry_timing_reason": entry_timing_gate["reason"],
                "pattern_veto": "yes" if decision.get("yes_veto_applied") else ("no" if decision.get("no_veto_applied") else "none"),
                "veto_side": "YES" if decision.get("yes_veto_applied") else ("NO" if decision.get("no_veto_applied") else "NONE"),
                "intelligence_enabled": bool(intelligence.get("enabled")),
                "fresh_forecast_only": bool(intelligence.get("fresh_forecast_only", True)),
                "intelligence_used_history": bool(intelligence.get("used_history", False)),
                "intelligence_reason": intelligence.get("reason"),
            },
            "base_prob": round(avg_prob, 6),
            "market_yes_price": round(market_price, 6),
            "intelligence_prob": round(float(intelligence.get("probability", avg_prob)), 6),
            "decision_prob": round(float(intelligence.get("probability", avg_prob)), 6),
            "decision_delta": round(float(intelligence.get("probability_delta", 0.0)), 6),
            "model_blend": round(float(intelligence.get("blend_weight", 0.0)), 6),
            "freshness_score": round(float(intelligence.get("freshness_score", 0.0)), 6),
            "inefficiency_score": round(float(intelligence.get("inefficiency_score", 0.0)), 6),
            "fresh_forecast_only": bool(intelligence.get("fresh_forecast_only", True)),
            "intelligence_used_history": bool(intelligence.get("used_history", False)),
            "market_snapshot": sanity.get("snapshot") or {},
            "decision": {
                "action": decision["action"],
                "adjusted_model_prob": round(decision["calibrated_model_prob"], 6),
                "pre_calibration_model_prob": round(decision["adjusted_model_prob"], 6),
                "calibrated_model_prob": round(decision["calibrated_model_prob"], 6),
                "pre_pattern_veto_prob": round(
                    decision.get("pre_pattern_veto_prob", decision["adjusted_model_prob"]),
                    6,
                ),
                "pattern_veto_applied": bool(decision.get("pattern_veto_applied", False)),
                "yes_veto_applied": bool(decision.get("yes_veto_applied", False)),
                "no_veto_applied": bool(decision.get("no_veto_applied", False)),
            },
        }

    def save_signals(self, signals, diagnostics=None, market_states=None):
        self._write_signal_store(signals, diagnostics=diagnostics, market_states=market_states, log=True)

    def flush_signals(self, signals, market_states=None):
        self._write_signal_store(signals, diagnostics=None, market_states=market_states, log=False)

    def _write_signal_store(self, signals, diagnostics=None, market_states=None, log=True):
        os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
        output = {
            "last_run": str(datetime.now()),
            "run_number": self.run_count,
            "signal_count": len(signals),
            "signals": signals,
        }
        if market_states is not None:
            output["market_states"] = market_states
        if diagnostics:
            output["diagnostics"] = diagnostics
        with open(self.data_path, 'w') as f:
            json.dump(output, f, indent=2)
        if log:
            self.log(f"[SIGNAL] Saved {len(signals)} signals to {self.data_path}")


if __name__ == "__main__":
    gen = SignalGenerator()
    gen.run()
