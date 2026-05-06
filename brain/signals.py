import json
import os
import re
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# Ensure sibling module imports work when run directly
sys.path.insert(0, os.path.dirname(__file__))

from weather import WeatherClient
from markets import MarketClient
from intelligence import WeatherIntelligenceAgent
from model import TradingModel


class SignalGenerator:
    def __init__(self, data_path="../data/signals.json"):
        self.weather = WeatherClient()
        self.markets = MarketClient()
        self.model = TradingModel()
        self.intelligence = WeatherIntelligenceAgent()
        self.data_path = os.path.join(os.path.dirname(__file__), data_path)
        self.run_count = 0
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
            print(message)
        except OSError:
            pass

    def run(self, event_filter=None, max_events=None):
        self.run_count += 1
        start_time = datetime.now()
        self.log(f"\n[SIGNAL] {'='*55}")
        self.log(f"[SIGNAL]   SCAN #{self.run_count} -- {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log(f"[SIGNAL] {'='*55}")
        self.intelligence.refresh_from_feedback()
        intelligence_summary = self.intelligence.summarize()
        self.log(
            f"[SIGNAL] Intelligence samples={intelligence_summary['trained_samples']} "
            f"| win_rate={intelligence_summary['recent_win_rate']:.2%} "
            f"| brier={intelligence_summary['recent_avg_brier']:.4f}"
        )

        # Step 0: Fetch markets
        self.log(f"\n[SIGNAL] --- Phase 1: Market Discovery ---")
        active_markets = self.markets.get_weather_markets()
        self.log(f"[SIGNAL] Found {len(active_markets)} active temperature markets after filtering.")

        if len(active_markets) == 0:
            self.log("[SIGNAL] WARNING: No temperature markets available. Nothing to analyze.")
            self.log("[SIGNAL]    Temperature markets are created periodically on Polymarket.")
            self.log("[SIGNAL]    The bot will automatically pick them up on the next scan.")
            self.save_signals([], diagnostics={
                "reason": "no_temperature_markets",
                "raw_search_completed": True,
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
            event_signal_found = False

            for rung_index, market in enumerate(ordered_markets, start=1):
                if event_signal_found:
                    self.log(f"[SIGNAL] | Event already has a valid rung. Moving to next ladder.")
                    break

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

                # Step 2: Parse exact market date
                market_date = self.extract_market_date(question, market)
                if not market_date:
                    self.log(f"[SIGNAL] | >> SKIP: Could not determine market date.")
                    skipped["no_market_date"] += 1
                    self.log(f"[SIGNAL] +------------------------------------------")
                    continue
                self.log(f"[SIGNAL] | Market Date: {market_date.isoformat()}")

                # Step 3: Get forecast
                forecast = self.weather.get_forecast(lat, lon, is_us)
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
                    self.log(f"[SIGNAL] | Forecasts: {forecast_data}")

                    market_context = self.build_market_context(location, forecast, market_date)
                    self.log(
                        f"[SIGNAL] | Local Time: {market_context['local_now']} "
                        f"| Hour: {market_context['local_hour']} "
                        f"| Stage: {market_context['local_peak_stage_detail']}"
                    )

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
                    intelligence = self.intelligence.generate_signal_adjustment({
                        "base_prob": avg_prob,
                        "market_yes_price": market_price,
                        "spread": spread,
                        "days_to_resolution": days_to_resolution,
                        "liquidity": sanity["snapshot"].get("liquidity"),
                        "volume_24h": sanity["snapshot"].get("volume_24h"),
                        "forecast_data": forecast_data,
                        "target": target,
                        "regime": provisional_regime,
                        "local_peak_stage": market_context["local_peak_stage"],
                        "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                        "local_hour": market_context["local_hour"],
                        "timezone": market_context["timezone"],
                        "continent": market_context["continent"],
                        "city": market_context["city"],
                    })
                    learned_prob = intelligence["decision_prob"]
                    self.log(
                        f"[SIGNAL] | Intelligence Prob: {learned_prob:.2%} "
                        f"(raw={intelligence['calibrated_prob']:.2%}, "
                        f"blend={intelligence['model_blend']:.2f}, "
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
                            "intelligence_confidence": intelligence["learning_confidence"],
                            "intelligence_prob": learned_prob,
                            "local_peak_stage": market_context["local_peak_stage"],
                            "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                            "local_hour": market_context["local_hour"],
                            "timezone": market_context["timezone"],
                            "utc_offset_hours": market_context["utc_offset_hours"],
                            "continent": market_context["continent"],
                            "city": market_context["city"],
                        }
                    )
                    self.log(
                        f"[SIGNAL] | Adjusted Prob: {decision['adjusted_model_prob']:.2%}, "
                        f"Bust Risk: {decision['bust_risk']:.2%}, Regime: {decision['regime']}"
                    )
                    self.log(f"[SIGNAL] | Edge: {decision['edge']:.2%} (abs: {decision['abs_edge']:.2%})")
                    learning_payload = {
                        **intelligence["training_payload"],
                        "meta": {
                            **intelligence["training_payload"].get("meta", {}),
                            "pattern_veto": "yes" if decision.get("yes_veto_applied") else ("no" if decision.get("no_veto_applied") else "none"),
                            "veto_side": "YES" if decision.get("yes_veto_applied") else ("NO" if decision.get("no_veto_applied") else "NONE"),
                            "city": market_context["city"],
                            "country": market_context["country"],
                            "country_code": market_context["country_code"],
                            "continent": market_context["continent"],
                            "timezone": market_context["timezone"],
                            "utc_offset_hours": market_context["utc_offset_hours"],
                            "local_now": market_context["local_now"],
                            "local_hour": market_context["local_hour"],
                            "local_date": market_context["local_date"],
                            "local_peak_stage": market_context["local_peak_stage"],
                            "local_peak_stage_detail": market_context["local_peak_stage_detail"],
                        },
                        "decision": {
                            "action": decision["action"],
                            "adjusted_model_prob": round(decision["adjusted_model_prob"], 6),
                            "pre_pattern_veto_prob": round(
                                decision.get("pre_pattern_veto_prob", decision["adjusted_model_prob"]),
                                6,
                            ),
                            "pattern_veto_applied": bool(decision.get("pattern_veto_applied", False)),
                            "yes_veto_applied": bool(decision.get("yes_veto_applied", False)),
                            "no_veto_applied": bool(decision.get("no_veto_applied", False)),
                        },
                    }

                    trade_side_price = sanity["yes_price"] if decision["action"] == "BUY_YES" else sanity["no_price"]
                    trade_side_market_prob = decision["adjusted_model_prob"] if decision["action"] == "BUY_YES" else (1 - decision["adjusted_model_prob"])
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
                        "raw_model_prob": round(avg_prob, 4),
                        "intelligence_prob": round(learned_prob, 4),
                        "adjusted_model_prob": round(decision["adjusted_model_prob"], 4),
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
                        "learning_confidence": round(intelligence["learning_confidence"], 4),
                        "inefficiency_score": round(intelligence["inefficiency_score"], 4),
                        "segment_adjustment": round(intelligence["segment_adjustment"], 4),
                        "action": decision["action"],
                        "should_trade": decision["should_trade"],
                        "days_to_resolution": days_to_resolution,
                        "market_snapshot": sanity["snapshot"],
                        "learning_features": learning_payload,
                        "timestamp": str(datetime.now()),
                    })

                    if decision["should_trade"]:
                        action = decision["action"]
                        entry_price = sanity["yes_price"] if action == "BUY_YES" else sanity["no_price"]
                        self.log(
                            f"[SIGNAL] | >>> TRADE SIGNAL: {action} | Mode: {decision['mode']} | "
                            f"Conf: {decision['confidence_score']:.2f} | Size x{decision['size_multiplier']:.2f} <<<"
                        )
                        signals.append({
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
                            "target": target,
                            "forecast_data": {k: round(v, 2) for k, v in forecast_data.items()},
                            "avg_model_prob": round(avg_prob, 4),
                            "intelligence_prob": round(learned_prob, 4),
                            "adjusted_model_prob": round(decision["adjusted_model_prob"], 4),
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
                            "learning_confidence": round(intelligence["learning_confidence"], 4),
                            "inefficiency_score": round(intelligence["inefficiency_score"], 4),
                            "segment_adjustment": round(intelligence["segment_adjustment"], 4),
                            "learning_features": learning_payload,
                            "market_snapshot": sanity["snapshot"],
                            "timestamp": str(datetime.now())
                        })
                        # Flush immediately so execution can pick up this signal before the scan finishes.
                        self.flush_signals(signals, market_states)
                        event_signal_found = True
                    else:
                        reason = self._skip_reason(decision)
                        self.log(f"[SIGNAL] | [X] NO TRADE: {reason}")
                        skipped["no_edge"] += 1

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
            "skipped": skipped,
            "elapsed_seconds": round(elapsed, 1),
            "intelligence": intelligence_summary,
        }, market_states=market_states)

    def _skip_reason(self, decision):
        """Human-readable reason for skipping a trade."""
        reasons = decision.get("reasons", [])
        if not reasons:
            return "Filtered by risk controls"
        return "; ".join(reasons)

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
        return not any(term in rules_blob for term in clarity_terms)

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
        range_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:°\s*)?(?:f|c)?\s*(?:-|to)\s*(\d+(?:\.\d+)?)', q_lower)
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if low < high and low > -50 and high < 150:
                return {"type": "range", "low": low, "high": high}

        # 2. "between X and Y" pattern
        between_match = re.search(r'between\s+(\d+(?:\.\d+)?)\s*(?:(?:°\s*)?(?:f|c)?)?\s+and\s+(\d+(?:\.\d+)?)', q_lower)
        if between_match:
            low = float(between_match.group(1))
            high = float(between_match.group(2))
            if low < high:
                return {"type": "range", "low": low, "high": high}

        # 3. Exact Pattern (e.g. "exactly 7C", "be exactly 7")
        exact_match = re.search(r'exactly\s+(\d+(?:\.\d+)?)', q_lower)
        if exact_match:
            return {"type": "exact", "val": float(exact_match.group(1))}

        # 4. Threshold patterns
        above_match = re.search(r'(?:above|over|higher than|at least|exceed|>=)\s*(\d+(?:\.\d+)?)', q_lower)
        if above_match:
            return {"type": "threshold", "direction": "above", "val": float(above_match.group(1))}

        below_match = re.search(r'(?:below|under|lower than|at most|<=)\s*(\d+(?:\.\d+)?)', q_lower)
        if below_match:
            return {"type": "threshold", "direction": "below", "val": float(below_match.group(1))}

        # 5. Fallback: find a temperature number near F or C markers
        temp_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:°\s*)?(?:f|c|degrees)', q_lower)
        if temp_match:
            return {"type": "threshold", "direction": "above", "val": float(temp_match.group(1))}

        # 6. Last resort: any standalone number (less reliable)
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

        below_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:°\s*)?(?:f|c)?\s+or below', combined)
        if below_match:
            return (float(below_match.group(1)), -1)

        range_match = re.search(r'between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)', combined)
        if range_match:
            return (float(range_match.group(1)), 0)

        single_match = re.search(r'be\s+(\d+(?:\.\d+)?)\s*(?:°\s*)?(?:f|c)\b', combined)
        if single_match:
            return (float(single_match.group(1)), 1)

        above_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:°\s*)?(?:f|c)?\s+or higher', combined)
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
        }

    def _resolve_timezone_name(self, location, forecast):
        if isinstance(forecast, dict):
            forecast_timezone = forecast.get("timezone")
            if forecast_timezone:
                return forecast_timezone
        return location.get("timezone") or "UTC"

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
