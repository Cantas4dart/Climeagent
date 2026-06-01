# -*- coding: utf-8 -*-
import json
import argparse
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from .db import DBManager, PaperTrade, Trade
from .platform_fee import (
    PLATFORM_FEE_RECIPIENT,
    calculate_platform_fee_from_pnl,
    calculate_total_platform_fee,
    is_platform_fee_exempt,
)
from .polymarket import PolyMarketAPI
from .singleton import acquire_process_lock
from .temperature_history import StationHistoryClient

load_dotenv()

STARTUP_SETTLEMENT_DELAY_SECONDS = 5
SETTLEMENT_CHECK_INTERVAL_SECONDS = 5 * 60


def escape_html(value: str | None) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class SettlementMonitor:
    def __init__(self):
        self.db = DBManager()
        self.temperature_history = StationHistoryClient()
        self.telegram_bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        self.state_file = Path(__file__).resolve().parent.parent / "data" / "settlement_state.json"
        self.learning_feedback_file = Path(__file__).resolve().parent.parent / "data" / "learning_feedback.jsonl"
        self.last_daily_report = ""
        self.settlement_in_flight = False
        self.load_state()

    def load_state(self):
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.last_daily_report = data.get("lastDailyReport", "")
                print(f"[PYSETTLE] Loaded state: lastDailyReport = {self.last_daily_report}")
        except Exception as exc:
            print(f"[PYSETTLE] Could not load state: {exc}")

    def save_state(self):
        try:
            payload = {
                "lastDailyReport": self.last_daily_report,
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[PYSETTLE] Could not save state: {exc}")

    def fetch_market_snapshot(self, poly: PolyMarketAPI, trade: dict[str, Any] | Trade | PaperTrade):
        market_id = str(getattr(trade, "market_id", None) or trade.get("market_id") or "").strip()
        condition_id = str(getattr(trade, "condition_id", None) or trade.get("condition_id") or "").strip()

        if market_id:
            for attempt in range(1, 4):
                try:
                    market = poly.get_market_by_id(market_id)
                    if market and str(market.get("id", "")) == market_id:
                        return market
                    break
                except Exception as exc:
                    if attempt == 3:
                        break
                    print(f"[PYSETTLE] Market lookup by market_id {market_id} failed ({attempt}/3): {exc}")
                    time.sleep(attempt)

        if condition_id:
            for attempt in range(1, 3):
                try:
                    market = poly.get_market_by_condition_id(condition_id)
                    if market and str(market.get("conditionId", "")).lower() == condition_id.lower():
                        return market
                    break
                except Exception as exc:
                    if attempt == 2:
                        break
                    print(f"[PYSETTLE] Market lookup by condition_id {condition_id} failed ({attempt}/2): {exc}")
                    time.sleep(attempt)

        return None

    def _resolved_yes_from_winner(self, winner: str) -> int:
        return 1 if str(winner or "").upper() == "YES" else 0

    def _load_temperature_analysis_entry(self, trade: Trade | PaperTrade) -> dict[str, Any] | None:
        raw = getattr(trade, "temperature_analysis_entry", None)
        if not raw:
            return self._reconstruct_temperature_analysis_entry(trade)
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        return self._reconstruct_temperature_analysis_entry(trade)

    def _reconstruct_temperature_analysis_entry(self, trade: Trade | PaperTrade) -> dict[str, Any] | None:
        raw_learning = getattr(trade, "learning_features", None)
        if not raw_learning:
            return None
        try:
            payload = json.loads(raw_learning)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        city = meta.get("city")
        station_url = meta.get("resolution_station_url")
        station_name = meta.get("resolution_station_name")
        station_id = meta.get("resolution_station_id")
        timezone_name = meta.get("timezone")
        lat = meta.get("location_lat")
        lon = meta.get("location_lon")
        country_code = meta.get("country_code")

        if not any([city, station_url, station_id, timezone_name, lat, lon, country_code]):
            return None

        return {
            "market_id": getattr(trade, "market_id", None),
            "condition_id": getattr(trade, "condition_id", None),
            "market_date": getattr(trade, "market_date", None),
            "city": city,
            "country_code": country_code,
            "timezone": timezone_name,
            "station_id": station_id,
            "station_name": station_name,
            "station_url": station_url,
            "location_lat": lat,
            "location_lon": lon,
            "temperature_unit": meta.get("temperature_unit"),
            "target": {},
            "forecast_data": {},
            "entry_timestamp": getattr(trade, "timestamp", None),
            "reconstructed_from": "learning_features",
        }

    def _target_bounds(self, target: dict[str, Any]) -> tuple[str | None, float | None, float | None]:
        target_type = str(target.get("type") or "").strip().lower() or None
        if target_type == "range":
            return target_type, self._safe_float(target.get("low")), self._safe_float(target.get("high"))
        if target_type in {"threshold", "exact"}:
            value = self._safe_float(target.get("val"))
            return target_type, value, value
        return target_type, None, None

    def _evaluate_target_hit(self, target: dict[str, Any], actual_temperature: float | None) -> int | None:
        if actual_temperature is None:
            return None

        target_type = str(target.get("type") or "").strip().lower()
        if target_type == "range":
            low = self._safe_float(target.get("low"))
            high = self._safe_float(target.get("high"))
            if low is None or high is None:
                return None
            if float(low).is_integer() and float(high).is_integer():
                return 1 if low <= actual_temperature <= (high + 0.9) else 0
            return 1 if low <= actual_temperature <= high else 0

        if target_type == "exact":
            value = self._safe_float(target.get("val"))
            if value is None:
                return None
            rounded = self._round_half_away_from_zero(actual_temperature)
            return 1 if rounded == value else 0

        if target_type == "threshold":
            value = self._safe_float(target.get("val"))
            direction = str(target.get("direction") or "above").strip().lower()
            if value is None:
                return None
            if direction == "below":
                if float(value).is_integer():
                    return 1 if actual_temperature <= (value + 0.9) else 0
                return 1 if actual_temperature <= value else 0
            return 1 if actual_temperature >= value else 0

        return None

    def _safe_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _round_half_away_from_zero(self, value: float) -> float:
        return float(int(value + 0.5)) if value >= 0 else float(int(value - 0.5))

    def record_temperature_analysis(self, trade_source: str, trade: Trade | PaperTrade, settled_yes: int, settled_at: str):
        entry = self._load_temperature_analysis_entry(trade)
        if not entry:
            self.db.upsert_temperature_settlement_analysis(
                {
                    "trade_source": trade_source,
                    "trade_id": trade.id,
                    "market_id": trade.market_id,
                    "condition_id": trade.condition_id,
                    "market_date": trade.market_date,
                    "city": None,
                    "country_code": None,
                    "timezone": None,
                    "station_id": None,
                    "station_name": None,
                    "station_url": None,
                    "target_type": None,
                    "target_value_low": None,
                    "target_value_high": None,
                    "forecast_data_json": None,
                    "entry_avg_forecast": None,
                    "entry_model_prob": trade.entry_model_prob,
                    "entry_market_prob": trade.entry_market_prob,
                    "entry_confidence": trade.entry_confidence,
                    "entry_spread": trade.entry_spread,
                    "entry_regime": trade.entry_regime,
                    "entry_timestamp": getattr(trade, "timestamp", None),
                    "settled_yes": settled_yes,
                    "settled_at": settled_at,
                    "actual_temperature": None,
                    "actual_temperature_unit": None,
                    "actual_observed_at": None,
                    "actual_source": None,
                    "actual_source_status": "missing_entry_snapshot",
                    "forecast_error_avg": None,
                    "forecast_error_by_source_json": None,
                    "rounded_settlement_value": None,
                    "target_hit": None,
                    "created_at": None,
                }
            )
            return

        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        target_type, target_value_low, target_value_high = self._target_bounds(target)
        supported_target = target_type in {"threshold", "exact", "range"}
        forecast_data = entry.get("forecast_data") if isinstance(entry.get("forecast_data"), dict) else {}
        numeric_forecasts = {
            str(name): float(value)
            for name, value in forecast_data.items()
            if self._safe_float(value) is not None
        }
        entry_avg_forecast = (
            round(sum(numeric_forecasts.values()) / len(numeric_forecasts), 4)
            if numeric_forecasts
            else None
        )

        resolution = self.temperature_history.fetch_daily_actual_temperature(entry)
        actual_temperature = self._safe_float(resolution.get("actual_temperature"))
        actual_temperature_unit = resolution.get("actual_temperature_unit")
        actual_observed_at = resolution.get("actual_observed_at")
        actual_source = resolution.get("actual_source")
        actual_source_status = resolution.get("actual_source_status") or "missing_station_data"

        forecast_error_avg = None
        forecast_error_by_source_json = None
        rounded_settlement_value = None
        target_hit = None

        if actual_temperature is not None:
            rounded_settlement_value = self._round_half_away_from_zero(actual_temperature)
            target_hit = self._evaluate_target_hit(target, actual_temperature) if supported_target else None
            if numeric_forecasts:
                error_by_source = {
                    name: round(value - actual_temperature, 4)
                    for name, value in numeric_forecasts.items()
                }
                forecast_error_by_source_json = json.dumps(error_by_source, sort_keys=True)
                forecast_error_avg = round(
                    (sum(numeric_forecasts.values()) / len(numeric_forecasts)) - actual_temperature,
                    4,
                )

        stored_actual_source_status = actual_source_status
        if target_type is not None and not supported_target:
            stored_actual_source_status = "unsupported_market_shape"

        self.db.upsert_temperature_settlement_analysis(
            {
                "trade_source": trade_source,
                "trade_id": trade.id,
                "market_id": trade.market_id,
                "condition_id": trade.condition_id,
                "market_date": trade.market_date or entry.get("market_date"),
                "city": entry.get("city"),
                "country_code": entry.get("country_code"),
                "timezone": entry.get("timezone"),
                "station_id": entry.get("station_id"),
                "station_name": entry.get("station_name"),
                "station_url": entry.get("station_url"),
                "target_type": target_type,
                "target_value_low": target_value_low,
                "target_value_high": target_value_high,
                "forecast_data_json": json.dumps(numeric_forecasts, sort_keys=True) if numeric_forecasts else None,
                "entry_avg_forecast": entry_avg_forecast,
                "entry_model_prob": trade.entry_model_prob,
                "entry_market_prob": trade.entry_market_prob,
                "entry_confidence": trade.entry_confidence,
                "entry_spread": trade.entry_spread,
                "entry_regime": trade.entry_regime,
                "entry_timestamp": entry.get("entry_timestamp") or getattr(trade, "timestamp", None),
                "settled_yes": settled_yes,
                "settled_at": settled_at,
                "actual_temperature": actual_temperature,
                "actual_temperature_unit": actual_temperature_unit,
                "actual_observed_at": actual_observed_at,
                "actual_source": actual_source,
                "actual_source_status": stored_actual_source_status,
                "forecast_error_avg": forecast_error_avg,
                "forecast_error_by_source_json": forecast_error_by_source_json,
                "rounded_settlement_value": rounded_settlement_value,
                "target_hit": target_hit,
                "created_at": None,
            }
        )

    def run_loop(self):
        print("-----------------------------------------")
        print("Climeagent Python Settlement Monitor Started (24/7)")
        print("-----------------------------------------")
        print(f"[PYSETTLE] Startup check in {STARTUP_SETTLEMENT_DELAY_SECONDS}s.")
        print(f"[PYSETTLE] Recurring settlement checks every {SETTLEMENT_CHECK_INTERVAL_SECONDS // 60} minutes.")
        time.sleep(STARTUP_SETTLEMENT_DELAY_SECONDS)
        while True:
            try:
                self.run_settlement_pass("recurring")
            except Exception as exc:
                print(f"[PYSETTLE ERROR] Loop Error: {exc}")
            time.sleep(SETTLEMENT_CHECK_INTERVAL_SECONDS)

    def run_once(self):
        print("[PYSETTLE] Running single-pass settlement check.")
        self.run_settlement_pass("manual")

    def run_settlement_pass(self, mode: str):
        if self.settlement_in_flight:
            print(f"[PYSETTLE] Skipping {mode} settlement check because a previous pass is still running.")
            return

        self.settlement_in_flight = True
        print(f"[PYSETTLE] Running {mode} settlement check...")
        try:
            self.repair_stale_open_trades()
            self.check_settlements()
            print(f"[PYSETTLE] {mode.capitalize()} settlement pass complete.")
        finally:
            self.settlement_in_flight = False

    def check_settlements(self):
        print("[PYSETTLE] Settlement pass started.")
        self.repair_stale_open_trades()
        self.check_paper_settlements()

        unsettled = self.db.get_unsettled_trades()
        if not unsettled:
            self.export_learning_feedback()
            return

        print(f"[PYSETTLE] Checking {len(unsettled)} unsettled trades...")
        for trade in unsettled:
            try:
                poly = PolyMarketAPI({"key": "", "secret": "", "passphrase": ""})
                market = self.fetch_market_snapshot(poly, trade)
                if not market or not market.get("closed"):
                    continue

                prices = json.loads(market.get("outcomePrices") or "[]")
                if not isinstance(prices, list) or len(prices) < 2:
                    continue

                winner = "YES" if str(prices[0]) == "1" else "NO"
                win = trade.side == winner
                pnl = (trade.size * (1 - trade.buy_price)) if win else -(trade.size * trade.buy_price)
                self.db.mark_settled(trade.id, 1 if win else 0, pnl)
                settled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                try:
                    self.record_temperature_analysis("live", trade, self._resolved_yes_from_winner(winner), settled_at)
                except Exception as exc:
                    print(f"[PYSETTLE] Temperature analysis failed for live trade {trade.id}: {exc}")

                claim_message = "Manual redeem available with /redeem or /redeem_all."
                if win:
                    claim_result = self.try_auto_claim(trade)
                    if claim_result.get("claimed") and claim_result.get("txHash"):
                        claim_message = f"Auto-redeemed: https://polygonscan.com/tx/{claim_result['txHash']}"
                        fee_result = claim_result.get("fee") or {}
                        if fee_result.get("applied") and fee_result.get("txHash"):
                            claim_message += (
                                f"\nPlatform fee: {float(fee_result.get('amount', 0.0)):.6f} pUSD "
                                f"(https://polygonscan.com/tx/{fee_result['txHash']})"
                            )
                    elif claim_result.get("reason"):
                        claim_message = f"Auto-redeem skipped: {claim_result['reason']}"

                status = "WIN" if win else "LOSS"
                roi = (
                    f"+{(((1 - trade.buy_price) / trade.buy_price) * 100):.1f}%"
                    if win
                    else "-100%"
                )
                self.send_real_settlement_alert(
                    trade,
                    market.get("question") or f"Market {trade.market_id}",
                    status,
                    pnl,
                    roi,
                    claim_message,
                )
                print(f"[PYSETTLE] Alerted user {trade.tg_id} for market {trade.market_id} ({status})")
            except Exception as exc:
                print(f"[PYSETTLE ERROR] Market {trade.market_id}: {exc}")

        self.export_learning_feedback()
        print("[PYSETTLE] Settlement pass complete.")

    def check_paper_settlements(self):
        unsettled_paper_trades = self.db.get_unsettled_paper_trades()
        if not unsettled_paper_trades:
            self.send_pending_paper_settlement_alerts()
            self.export_paper_learning_feedback()
            return

        print(f"[PYSETTLE] Checking {len(unsettled_paper_trades)} paper trade(s)...")
        poly = PolyMarketAPI({"key": "", "secret": "", "passphrase": ""})

        for trade in unsettled_paper_trades:
            try:
                market = self.fetch_market_snapshot(poly, trade)
                if not market or not market.get("closed"):
                    continue

                prices = json.loads(market.get("outcomePrices") or "[]")
                if not isinstance(prices, list) or len(prices) < 2:
                    continue

                winner = "YES" if str(prices[0]) == "1" else "NO"
                win = trade.side == winner
                pnl = (trade.size * (1 - trade.entry_price)) if win else -(trade.size * trade.entry_price)
                self.db.mark_paper_trade_settled(trade.id, 1 if win else 0, pnl)
                settled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                try:
                    self.record_temperature_analysis("paper", trade, self._resolved_yes_from_winner(winner), settled_at)
                except Exception as exc:
                    print(f"[PYSETTLE] Temperature analysis failed for paper trade {trade.id}: {exc}")
                self.send_paper_settlement_alert(
                    PaperTrade(
                        **{
                            **trade.__dict__,
                            "outcome": 1 if win else 0,
                            "pnl": pnl,
                        }
                    ),
                    market.get("question") or f"Market {trade.market_id}",
                )
            except Exception as exc:
                print(f"[PYSETTLE] Paper settlement check failed for {trade.id} / {trade.market_id}: {exc}")

        self.send_pending_paper_settlement_alerts()
        self.export_paper_learning_feedback()

    def repair_stale_open_trades(self):
        stale_trades = self.db.get_stale_open_trades()
        if not stale_trades:
            return
        print(f"[PYSETTLE] Repair scan: checking {len(stale_trades)} potentially stale open trade(s)...")
        poly = PolyMarketAPI({"key": "", "secret": "", "passphrase": ""})
        for trade in stale_trades:
            try:
                market = self.fetch_market_snapshot(poly, trade)
                if not market or not market.get("closed"):
                    continue

                prices = json.loads(market.get("outcomePrices") or "[]")
                if not isinstance(prices, list) or len(prices) < 2:
                    continue

                winner = "YES" if str(prices[0]) == "1" else "NO"
                win = trade.side == winner
                pnl = (trade.size * (1 - trade.buy_price)) if win else -(trade.size * trade.buy_price)
                self.db.mark_settled(trade.id, 1 if win else 0, pnl)
                print(
                    f"[PYSETTLE] Repaired stale open trade {trade.id} / {trade.market_id} "
                    f"as {'WIN' if win else 'LOSS'}."
                )
            except Exception as exc:
                print(f"[PYSETTLE] Could not repair stale trade {trade.id} / {trade.market_id}: {exc}")

    def try_auto_claim(self, trade: Trade) -> dict[str, Any]:
        if not trade.condition_id:
            return {"claimed": False, "reason": "missing condition id"}

        user = self.db.get_user(trade.tg_id)
        if not user:
            return {"claimed": False, "reason": "user not found"}
        if not user.auto_claim:
            return {"claimed": False, "reason": "auto-claim disabled for this user"}

        try:
            poly = PolyMarketAPI(
                {
                    "key": user.api_key or "",
                    "secret": user.api_secret or "",
                    "passphrase": user.api_passphrase or "",
                },
                user.private_key or "",
                {
                    "funderAddress": user.funder_address or (os.getenv("POLY_FUNDER_ADDRESS") or "").strip() or None,
                    "signatureType": user.signature_type
                    if user.signature_type is not None
                    else (int((os.getenv("POLY_SIGNATURE_TYPE") or "").strip()) if (os.getenv("POLY_SIGNATURE_TYPE") or "").strip().isdigit() else None),
                },
            )
            tx_hash = poly.redeem_winnings(str(trade.condition_id))
            self.db.mark_claimed_by_condition(trade.tg_id, str(trade.condition_id), tx_hash)
            fee_result = self.collect_platform_fee_for_trades(trade.tg_id, poly, [trade])
            print(f"[PYSETTLE] Auto-redeemed condition {trade.condition_id} for {trade.tg_id}: {tx_hash}")
            return {"claimed": True, "txHash": tx_hash, "fee": fee_result}
        except Exception as exc:
            print(f"[PYSETTLE] Auto-redeem failed for {trade.tg_id} / {trade.condition_id}: {exc}")
            return {"claimed": False, "reason": str(exc)}

    def collect_platform_fee_for_trades(self, user_id: str, poly: PolyMarketAPI, trades: list[Trade]) -> dict[str, Any]:
        if not trades:
            return {"applied": False, "reason": "no_trades"}
        if is_platform_fee_exempt(user_id):
            return {"applied": False, "reason": "admin_exempt"}

        fee_amount = calculate_total_platform_fee(trades, user_id)
        if fee_amount <= 0:
            return {"applied": False, "reason": "no_fee_due"}

        for trade in trades:
            self.db.record_platform_fee_amount(int(trade.id), calculate_platform_fee_from_pnl(trade.pnl))

        tx_hash = poly.transfer_pusd(PLATFORM_FEE_RECIPIENT, fee_amount)
        for trade in trades:
            self.db.mark_platform_fee_collected(
                int(trade.id),
                calculate_platform_fee_from_pnl(trade.pnl),
                tx_hash,
            )
        return {"applied": True, "amount": fee_amount, "txHash": tx_hash}

    def export_learning_feedback(self):
        pending = self.db.get_settled_trades_missing_feedback()
        if not pending:
            return
        self.learning_feedback_file.parent.mkdir(parents=True, exist_ok=True)

        for trade in pending:
            try:
                payload = json.loads(trade.learning_features) if trade.learning_features else None
                if not payload:
                    self.db.mark_feedback_exported(trade.id)
                    continue

                resolved_yes = int(trade.outcome or 0) if trade.side == "YES" else int(0 if trade.outcome is None else 1 - trade.outcome)
                feedback = {
                    "feedback_id": f"trade:{trade.id}",
                    "trade_id": trade.id,
                    "market_id": trade.market_id,
                    "condition_id": trade.condition_id,
                    "side": trade.side,
                    "resolved_yes": resolved_yes,
                    "trade_won": int(trade.outcome or 0),
                    "pnl": float(trade.pnl or 0),
                    "entry_model_prob": trade.entry_model_prob,
                    "entry_market_prob": trade.entry_market_prob,
                    "entry_confidence": trade.entry_confidence,
                    "entry_spread": trade.entry_spread,
                    "entry_regime": trade.entry_regime,
                    "city": payload.get("meta", {}).get("city"),
                    "country": payload.get("meta", {}).get("country"),
                    "country_code": payload.get("meta", {}).get("country_code"),
                    "continent": payload.get("meta", {}).get("continent"),
                    "timezone": payload.get("meta", {}).get("timezone"),
                    "utc_offset_hours": payload.get("meta", {}).get("utc_offset_hours"),
                    "local_now": payload.get("meta", {}).get("local_now"),
                    "local_date": payload.get("meta", {}).get("local_date"),
                    "local_hour": payload.get("meta", {}).get("local_hour"),
                    "local_peak_stage": payload.get("meta", {}).get("local_peak_stage"),
                    "local_peak_stage_detail": payload.get("meta", {}).get("local_peak_stage_detail"),
                    "pattern_veto_applied": bool(payload.get("decision", {}).get("pattern_veto_applied")),
                    "yes_veto_applied": bool(payload.get("decision", {}).get("yes_veto_applied")),
                    "no_veto_applied": bool(payload.get("decision", {}).get("no_veto_applied")),
                    "learning_payload": payload,
                    "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                with self.learning_feedback_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(feedback) + "\n")
                self.db.mark_feedback_exported(trade.id)
            except Exception as exc:
                print(f"[PYSETTLE] Could not export learning feedback for trade {trade.id}: {exc}")

    def export_paper_learning_feedback(self):
        pending = self.db.get_settled_paper_trades_missing_feedback()
        if not pending:
            return
        self.learning_feedback_file.parent.mkdir(parents=True, exist_ok=True)

        for trade in pending:
            try:
                payload = json.loads(trade.learning_features) if trade.learning_features else None
                if not payload:
                    self.db.mark_paper_feedback_exported(trade.id)
                    continue

                resolved_yes = int(trade.outcome or 0) if trade.side == "YES" else int(0 if trade.outcome is None else 1 - trade.outcome)
                feedback = {
                    "feedback_id": f"paper_trade:{trade.id}",
                    "trade_id": trade.id,
                    "market_id": trade.market_id,
                    "condition_id": trade.condition_id,
                    "side": trade.side,
                    "resolved_yes": resolved_yes,
                    "trade_won": int(trade.outcome or 0),
                    "pnl": float(trade.pnl or 0),
                    "entry_model_prob": trade.entry_model_prob,
                    "entry_market_prob": trade.entry_market_prob,
                    "entry_confidence": trade.entry_confidence,
                    "entry_spread": trade.entry_spread,
                    "entry_regime": trade.entry_regime,
                    "city": payload.get("meta", {}).get("city"),
                    "country": payload.get("meta", {}).get("country"),
                    "country_code": payload.get("meta", {}).get("country_code"),
                    "continent": payload.get("meta", {}).get("continent"),
                    "timezone": payload.get("meta", {}).get("timezone"),
                    "utc_offset_hours": payload.get("meta", {}).get("utc_offset_hours"),
                    "local_now": payload.get("meta", {}).get("local_now"),
                    "local_date": payload.get("meta", {}).get("local_date"),
                    "local_hour": payload.get("meta", {}).get("local_hour"),
                    "local_peak_stage": payload.get("meta", {}).get("local_peak_stage"),
                    "local_peak_stage_detail": payload.get("meta", {}).get("local_peak_stage_detail"),
                    "pattern_veto_applied": bool(payload.get("decision", {}).get("pattern_veto_applied")),
                    "yes_veto_applied": bool(payload.get("decision", {}).get("yes_veto_applied")),
                    "no_veto_applied": bool(payload.get("decision", {}).get("no_veto_applied")),
                    "learning_payload": payload,
                    "source": "paper_trade",
                    "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                with self.learning_feedback_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(feedback) + "\n")
                self.db.mark_paper_feedback_exported(trade.id)
            except Exception as exc:
                print(f"[PYSETTLE] Could not export paper learning feedback for trade {trade.id}: {exc}")

    def build_paper_settlement_message(self, trade: PaperTrade, market_question: str) -> str:
        win = int(trade.outcome or 0) == 1
        market_title = escape_html(market_question or f"Market {trade.market_id}")
        paper_pnl = float(trade.pnl or 0)
        roi = f"+{(((1 - trade.entry_price) / trade.entry_price) * 100):.1f}%" if trade.entry_price and win else "-100%" if trade.entry_price else "N/A"
        status_emoji = "✅" if win else "❌"
        return "\n".join(
            [
                "📄 <b>Paper Settlement</b>",
                "",
                "<b>🪙 Market</b>",
                market_title,
                "",
                "<b>💭 Outcome</b>",
                f"├ {status_emoji} {trade.side}",
                f"├ {trade.size} Shares (Amount in pUSD)",
                f"├ Position {trade.side} @{trade.entry_price:.4f}",
                f"└ Return: {roi}",
                "",
                "<i>Simulation only - no real funds.</i>",
            ]
        )

    def build_real_settlement_message(
        self,
        trade: Trade,
        market_question: str,
        status: str,
        pnl: float,
        roi: str,
        claim_message: str,
    ) -> str:
        market_title = escape_html(market_question or f"Market {trade.market_id}")
        safe_claim_message = escape_html(claim_message)
        position_text = f"{trade.side} @{float(trade.buy_price or 0):.4f}"
        status_emoji = "✅ WIN" if status == "WIN" else "❌ LOSS"
        return "\n".join(
            [
                "📊 <b>Live Settlement</b>",
                "",
                "<b>🪙 Market</b>",
                market_title,
                "",
                "<b>💭 Outcome</b>",
                f"├ {status_emoji}",
                f"├ {trade.size} Shares (Amount in pUSD)",
                f"├ Position {position_text}",
                f"└ Return: {roi}",
                "",
                "<b>📌 Redeem Status</b>",
                safe_claim_message,
                "",
                "<i>Use /stats for performance and /daily for the latest summary.</i>",
            ]
        )

    def send_real_settlement_alert(
        self,
        trade: Trade,
        market_question: str,
        status: str,
        pnl: float,
        roi: str,
        claim_message: str,
    ):
        if not self.telegram_bot_token:
            return
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": trade.tg_id,
            "text": self.build_real_settlement_message(trade, market_question, status, pnl, roi, claim_message),
            "parse_mode": "HTML",
        }
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()

    def send_paper_settlement_alert(self, trade: PaperTrade, market_question: str):
        if not self.telegram_bot_token:
            return
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": trade.tg_id,
            "text": self.build_paper_settlement_message(trade, market_question),
            "parse_mode": "HTML",
        }
        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            self.db.mark_paper_alert_sent(trade.id)
            print(f"[PYSETTLE] Sent paper settlement alert for {trade.id} / {trade.market_id}.")
        except Exception as exc:
            print(f"[PYSETTLE] Could not send paper settlement alert to {trade.tg_id}: {exc}")

    def send_pending_paper_settlement_alerts(self):
        pending_alerts = self.db.get_settled_paper_trades_pending_alert()
        if not pending_alerts:
            return

        print(f"[PYSETTLE] Retrying {len(pending_alerts)} pending paper settlement alert(s)...")
        poly = PolyMarketAPI({"key": "", "secret": "", "passphrase": ""})
        for trade in pending_alerts:
            try:
                market = self.fetch_market_snapshot(poly, trade)
                market_question = (market or {}).get("question") or f"Market {trade.market_id}"
                self.send_paper_settlement_alert(trade, market_question)
            except Exception as exc:
                print(f"[PYSETTLE] Could not refresh paper market question for {trade.id} / {trade.market_id}: {exc}")
                self.send_paper_settlement_alert(trade, f"Market {trade.market_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--once", action="store_true")
    args, _ = parser.parse_known_args()
    release_lock = acquire_process_lock("python-settlement-monitor")
    if not release_lock:
        raise SystemExit(0)
    monitor = SettlementMonitor()
    if args.once:
        monitor.run_once()
    else:
        monitor.run_loop()
