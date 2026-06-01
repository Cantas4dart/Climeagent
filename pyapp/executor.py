# -*- coding: utf-8 -*-
import html
import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from .db import DBManager, User
from .polymarket import PolyMarketAPI, extract_allowance_amount
from .singleton import acquire_process_lock

MIN_TRADE_NOTIONAL_USD = 1.0
MIN_LIMIT_ORDER_SIZE_SHARES = 5.0
SHARE_SIZE_PRECISION = 6
SELL_SIZE_STEP = 0.01
MIN_SELL_SIZE_SHARES = 0.01


def extract_allowance(balance_data: dict[str, Any]) -> float:
    return extract_allowance_amount(balance_data)


def resolve_user_polymarket_account_config(user: User | None) -> dict[str, Any]:
    raw_funder = (os.getenv("POLY_FUNDER_ADDRESS") or "").strip()
    raw_signature_type = (os.getenv("POLY_SIGNATURE_TYPE") or "").strip()
    fallback_signature_type = int(raw_signature_type) if raw_signature_type.isdigit() else None
    return {
        "funderAddress": getattr(user, "funder_address", None) or raw_funder or None,
        "signatureType": getattr(user, "signature_type", None)
        if getattr(user, "signature_type", None) is not None
        else fallback_signature_type,
    }


def escape_html(value: str | None) -> str:
    return html.escape(str(value or ""))


def build_aligned_mono_row(left: str, right: str, left_width: int = 22) -> str:
    return f"{left.ljust(left_width)}{right}"


def format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_forecast_temp(signal: dict[str, Any]) -> str:
    forecast_data = signal.get("forecast_data") if isinstance(signal.get("forecast_data"), dict) else {}
    unit = "F" if signal.get("temperature_unit") == "fahrenheit" else "C"
    values = []
    for source, temp in forecast_data.items():
        try:
            values.append((str(source), float(temp)))
        except (TypeError, ValueError):
            continue
    if not values:
        return "n/a"
    average_temp = sum(temp for _, temp in values) / len(values)
    source_text = ", ".join(f"{source.upper()} {temp:.1f}{unit}" for source, temp in values[:3])
    return f"{average_temp:.1f}{unit} avg ({source_text})"


def build_signal_rationale(signal: dict[str, Any], side: str) -> list[str]:
    side_prob = signal.get("trade_side_model_prob")
    if side_prob is None:
        try:
            adjusted_yes = float(signal.get("adjusted_model_prob"))
            side_prob = adjusted_yes if side == "YES" else 1 - adjusted_yes
        except (TypeError, ValueError):
            side_prob = None
    side_market = signal.get("trade_side_market_price")
    if side_market is None:
        side_market = signal.get("market_price_yes") if side == "YES" else signal.get("market_price_no")

    reasons = [
        f"Model {format_percent(side_prob)} vs market {format_percent(side_market)}",
        f"Edge {format_percent(signal.get('abs_edge'))}, confidence {format_percent(signal.get('confidence_score'))}",
    ]
    mode = signal.get("mode")
    regime = signal.get("regime")
    if mode or regime:
        reasons.append(f"Mode {mode or 'standard'} / regime {regime or 'n/a'}")
    if signal.get("forecast_revision_direction"):
        reasons.append(f"Forecast trend {signal.get('forecast_revision_direction')}")
    return reasons


class TradeExecutor:
    def __init__(self):
        self.db = DBManager()
        self.signal_path = Path(__file__).resolve().parent.parent / "data" / "signals.json"
        self.reserved_capital_by_user: dict[str, float] = {}
        self.telegram_bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

    def run_loop(self):
        print("-----------------------------------------")
        print("Climeagent Python Execution Loop Started (24/7 Mode)")
        print("-----------------------------------------")
        while True:
            try:
                self.process_signals()
            except Exception as exc:
                print(f"[PYEXEC ERROR] Loop Error: {exc}")
            time.sleep(120)

    def run_once(self):
        print("[PYEXEC] Running single-pass executor check.")
        self.process_signals()

    def process_signals(self):
        if not self.signal_path.exists():
            print("[PYEXEC] No signals file found. Waiting...")
            return

        data = json.loads(self.signal_path.read_text(encoding="utf-8"))
        signals = data.get("signals") or []
        market_states = data.get("market_states") or []

        self.process_open_trades(market_states)
        self.process_paper_signals(signals)

        if not signals:
            print("[PYEXEC] No active signals in file.")
            return

        active_users = self.db.get_active_users()
        print(f"[PYEXEC] Found {len(active_users)} active traders.")
        self.reserved_capital_by_user.clear()

        for user in active_users:
            self.process_real_user_signals(user, signals)

    def process_real_user_signals(self, user: User, signals: list[dict[str, Any]]):
        account_config = resolve_user_polymarket_account_config(user)
        poly = self.build_poly_client(user, account_config)
        open_position_count = self.db.get_unsettled_trade_count(user.tg_id)

        for signal in signals:
            if open_position_count >= user.max_open_positions:
                print(
                    f"[PYEXEC] User {user.tg_id} is at max open positions "
                    f"({open_position_count}/{user.max_open_positions}). Skipping further signals."
                )
                break

            market_id = str(signal.get("market_id") or "")
            if not market_id or self.db.has_traded(user.tg_id, market_id):
                continue

            print(
                f"[PYEXEC] New Signal for {user.tg_id}: "
                f"{signal.get('question')} | {signal.get('action')} | "
                f"mode={signal.get('mode', 'standard')} | conf={signal.get('confidence_score', 'n/a')}"
            )
            try:
                balance_data = poly.get_balance()
                balance = float(balance_data.get("balance", 0.0)) / 1_000_000
                allowance = extract_allowance(balance_data) / 1_000_000
                already_reserved = self.reserved_capital_by_user.get(user.tg_id, 0.0)
                spendable_balance = max(0.0, balance - already_reserved)

                print(
                    f"[PYEXEC] User {user.tg_id} - Balance: {balance:.2f}, "
                    f"Reserved: {already_reserved:.2f}, Spendable: {spendable_balance:.2f}, "
                    f"Allowance: {allowance:.2f}"
                )

                if balance > 0.1 and allowance < 1.0:
                    print(f"[PYEXEC] Auto-approving pUSD allowance (Master Approval) for user {user.tg_id}...")
                    poly.approve_collateral()
                    print("[PYEXEC] Master Auto-approval transactions sent.")
                    continue

                max_trade_amount = float(user.max_trade_amount)
                target_usd = min(spendable_balance, max_trade_amount)
                size_multiplier = max(0.25, float(signal.get("size_multiplier") or 1))
                target_usd *= size_multiplier

                entry_price = float(signal.get("entry_price") or signal.get("market_price"))
                if target_usd < MIN_TRADE_NOTIONAL_USD and spendable_balance >= MIN_TRADE_NOTIONAL_USD and max_trade_amount >= MIN_TRADE_NOTIONAL_USD:
                    target_usd = MIN_TRADE_NOTIONAL_USD
                size = round(target_usd / entry_price, SHARE_SIZE_PRECISION) if entry_price > 0 else 0.0
                reserved_cost = target_usd
                if target_usd < MIN_TRADE_NOTIONAL_USD or size <= 0:
                    print(
                        f"[PYEXEC] Balance too low to place trade for {user.tg_id} "
                        f"(target=${target_usd:.2f}, minimum=${MIN_TRADE_NOTIONAL_USD:.2f})"
                    )
                    continue

                market_data = poly.get_market_by_id(market_id)
                clob_token_ids = json.loads(market_data.get("clobTokenIds") or "[]")
                token_id = clob_token_ids[0] if signal.get("action") == "BUY_YES" else clob_token_ids[1]
                side = str(signal.get("action") or "").split("_", 1)[1]

                changes = self.db.reserve_trade(
                    {
                        "market_id": market_id,
                        "market_date": signal.get("market_date"),
                        "condition_id": signal.get("condition_id"),
                        "tg_id": user.tg_id,
                        "side": side,
                        "buy_price": entry_price,
                        "size": size,
                        "entry_model_prob": signal.get("adjusted_model_prob", signal.get("avg_model_prob")),
                        "entry_market_prob": signal.get("market_price_yes")
                        if signal.get("action") == "BUY_YES"
                        else signal.get("market_price_no"),
                        "entry_confidence": signal.get("confidence_score"),
                        "entry_spread": signal.get("ensemble_spread"),
                        "entry_regime": signal.get("regime"),
                        "learning_features": json.dumps(signal.get("learning_features"))
                        if signal.get("learning_features") is not None
                        else None,
                        "temperature_analysis_entry": self._temperature_analysis_entry_json(signal),
                    }
                )
                if changes == 0:
                    print(
                        f"[PYEXEC] Trade already reserved or recorded for {user.tg_id} "
                        f"on market {market_id}. Skipping."
                    )
                    continue

                try:
                    if size < MIN_LIMIT_ORDER_SIZE_SHARES:
                        print(
                            f"[PYEXEC] Size {size:.6f} is below Polymarket limit-order minimum "
                            f"({MIN_LIMIT_ORDER_SIZE_SHARES:g} shares). Using market buy for ${target_usd:.2f}."
                        )
                        order_response = poly.place_market_order(token_id, "BUY", target_usd)
                    else:
                        order_response = poly.place_limit_order(token_id, "BUY", entry_price, size)
                    order_id = str(order_response.get("orderID")) if order_response.get("orderID") is not None else None
                    self.db.mark_trade_submitted(user.tg_id, market_id, order_id)
                    self.send_trade_alert(user.tg_id, signal, side, entry_price, size, order_response)
                except Exception:
                    self.db.release_trade_reservation(user.tg_id, market_id)
                    raise

                self.reserved_capital_by_user[user.tg_id] = already_reserved + reserved_cost
                open_position_count += 1
                print(f"[PYEXEC] Trade order submitted and saved for {user.tg_id}")
            except Exception as exc:
                print(f"[PYEXEC ERROR] User {user.tg_id} failed trade: {exc}")

    def process_paper_signals(self, signals: list[dict[str, Any]]):
        if not isinstance(signals, list) or not signals:
            return

        paper_users = self.db.get_paper_testing_users()
        if not paper_users:
            return

        for user in paper_users:
            for signal in signals:
                market_id = str(signal.get("market_id") or "")
                if not market_id or self.db.has_paper_trade(user.tg_id, market_id):
                    continue

                action = str(signal.get("action") or "")
                if not action.startswith("BUY_"):
                    continue

                side = action.split("_", 1)[1]
                try:
                    entry_price = float(signal.get("entry_price") or signal.get("market_price"))
                except (TypeError, ValueError):
                    continue

                if entry_price <= 0:
                    continue

                changes = self.db.reserve_paper_trade(
                    {
                        "market_id": market_id,
                        "market_date": signal.get("market_date"),
                        "condition_id": signal.get("condition_id"),
                        "tg_id": user.tg_id,
                        "side": side,
                        "entry_price": entry_price,
                        "size": 1,
                        "entry_model_prob": signal.get("adjusted_model_prob", signal.get("avg_model_prob")),
                        "entry_market_prob": signal.get("market_price_yes")
                        if action == "BUY_YES"
                        else signal.get("market_price_no"),
                        "entry_confidence": signal.get("confidence_score"),
                        "entry_spread": signal.get("ensemble_spread"),
                        "entry_regime": signal.get("regime"),
                        "learning_features": json.dumps(signal.get("learning_features"))
                        if signal.get("learning_features") is not None
                        else None,
                        "temperature_analysis_entry": self._temperature_analysis_entry_json(signal),
                    }
                )

                if changes > 0:
                    self.send_paper_trade_alert(user.tg_id, signal, side, entry_price)
                    print(f"[PYEXEC] Paper trade logged for {user.tg_id} on {market_id}")

    def process_open_trades(self, market_states: list[dict[str, Any]]):
        if not isinstance(market_states, list) or not market_states:
            return
        active_trades = self.db.get_active_trades_for_monitoring()
        if not active_trades:
            return

        state_by_market = {
            str(state.get("market_id")): state
            for state in market_states
            if state.get("market_id") is not None
        }
        open_order_ids_by_user: dict[str, set[str]] = {}

        for trade in active_trades:
            state = state_by_market.get(str(trade.market_id))
            if not state:
                continue

            user = self.db.get_user(trade.tg_id)
            if not user or not user.trading_active:
                continue

            assessment = self.assess_conflict(trade, state)
            if assessment["exitFraction"] <= 0:
                continue

            remaining_size = max(0.0, float(trade.remaining_size or trade.size or 0))
            if remaining_size <= 0:
                continue

            shares_to_sell = min(
                remaining_size,
                round(remaining_size * assessment["exitFraction"], SHARE_SIZE_PRECISION),
            )
            if shares_to_sell <= 0:
                continue
            poly = self.build_poly_client(user, resolve_user_polymarket_account_config(user))

            if trade.tg_id not in open_order_ids_by_user:
                try:
                    open_orders = poly.get_open_orders() or []
                    open_ids = set()
                    for order in open_orders:
                        raw_id = order.get("id") or order.get("orderID") or order.get("orderId")
                        if raw_id is not None:
                            open_ids.add(str(raw_id))
                    open_order_ids_by_user[trade.tg_id] = open_ids
                except Exception as exc:
                    print(f"[PYEXEC] Could not refresh open orders for conflict monitor {trade.tg_id}: {exc}")
                    open_order_ids_by_user[trade.tg_id] = set()

            if trade.order_id and str(trade.order_id) in open_order_ids_by_user.get(trade.tg_id, set()):
                print(
                    f"[PYEXEC] Conflict monitor is skipping {trade.market_id} for {trade.tg_id} "
                    f"because entry order {trade.order_id} is still open on Polymarket."
                )
                continue

            try:
                market_data = poly.get_market_by_id(str(trade.market_id))
                clob_token_ids = json.loads(market_data.get("clobTokenIds") or "[]")
                token_id = clob_token_ids[0] if trade.side == "YES" else clob_token_ids[1]
                live_size = self.get_live_token_position_size(poly, user, token_id, trade)
                if live_size <= 0.01:
                    self.db.record_trade_exit(trade.id, 0.0, None, "no live token balance", True)
                    print(
                        f"[PYEXEC] Marked {trade.market_id} closed for {trade.tg_id}: "
                        f"no live {trade.side} token balance found."
                    )
                    continue
                shares_to_sell = min(shares_to_sell, live_size)
                shares_to_sell = self.floor_sell_size(shares_to_sell)
                if shares_to_sell < MIN_SELL_SIZE_SHARES:
                    print(
                        f"[PYEXEC] Auto-exit skipped for {trade.tg_id} / {trade.market_id}: "
                        f"live balance {live_size:.6f} is below sellable precision."
                    )
                    continue
                order_response = poly.place_market_order(token_id, "SELL", shares_to_sell)
                new_remaining_size = max(0.0, round(min(remaining_size, live_size) - shares_to_sell, SHARE_SIZE_PRECISION))
                exit_price = (
                    float(state.get("market_price_yes", trade.buy_price or 0))
                    if trade.side == "YES"
                    else float(state.get("market_price_no", trade.buy_price or 0))
                )
                fully_closed = new_remaining_size <= MIN_SELL_SIZE_SHARES
                if fully_closed:
                    new_remaining_size = 0.0
                self.db.record_trade_exit(trade.id, new_remaining_size, exit_price, assessment["reason"], fully_closed)
                self.send_exit_alert(
                    trade.tg_id,
                    trade,
                    state,
                    shares_to_sell,
                    new_remaining_size,
                    assessment,
                    order_response,
                )
                print(
                    f"[PYEXEC] Auto-exit submitted for {trade.tg_id} on market {trade.market_id}: "
                    f"{shares_to_sell}/{remaining_size} shares, remaining={new_remaining_size}"
                )
            except Exception as exc:
                print(f"[PYEXEC ERROR] Auto-exit failed for {trade.tg_id} / {trade.market_id}: {exc}")

    @staticmethod
    def floor_sell_size(size: float) -> float:
        return round(int(max(0.0, size) / SELL_SIZE_STEP) * SELL_SIZE_STEP, 3)

    def get_live_token_position_size(
        self,
        poly: PolyMarketAPI,
        user: User,
        token_id: str,
        trade: Any,
    ) -> float:
        account_config = resolve_user_polymarket_account_config(user)
        positions_address = account_config["funderAddress"] or poly.get_signer_address()
        positions = poly.get_positions(positions_address) or []
        trade_condition_id = str(trade.condition_id or "").lower()
        trade_side = str(trade.side or "").upper()
        token_id = str(token_id)

        for position in positions:
            position_token_id = str(
                position.get("asset")
                or position.get("asset_id")
                or position.get("assetId")
                or position.get("token_id")
                or position.get("tokenId")
                or position.get("clobTokenId")
                or ""
            )
            position_condition_id = str(position.get("conditionId") or "").lower()
            position_outcome = str(position.get("outcome") or "").upper()
            token_matches = position_token_id == token_id
            condition_matches = (
                trade_condition_id
                and position_condition_id == trade_condition_id
                and position_outcome == trade_side
            )
            if not token_matches and not condition_matches:
                continue
            try:
                return float(position.get("size") or position.get("balance") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return 0.0

    def build_poly_client(self, user: User, account_config: dict[str, Any]) -> PolyMarketAPI:
        return PolyMarketAPI(
            {
                "key": user.api_key or "",
                "secret": user.api_secret or "",
                "passphrase": user.api_passphrase or "",
            },
            user.private_key or "",
            account_config,
        )

    def _temperature_analysis_entry_json(self, signal: dict[str, Any]) -> str | None:
        forecast_data = signal.get("forecast_data")
        target = signal.get("target") if isinstance(signal.get("target"), dict) else {}
        payload = {
            "market_id": signal.get("market_id"),
            "condition_id": signal.get("condition_id"),
            "market_date": signal.get("market_date"),
            "city": signal.get("city"),
            "country_code": signal.get("country_code"),
            "timezone": signal.get("timezone"),
            "station_id": signal.get("resolution_station_id"),
            "station_name": signal.get("resolution_station_name"),
            "station_url": signal.get("resolution_station_url"),
            "location_lat": signal.get("location_lat"),
            "location_lon": signal.get("location_lon"),
            "temperature_unit": signal.get("temperature_unit"),
            "target": target,
            "forecast_data": forecast_data if isinstance(forecast_data, dict) else {},
            "entry_timestamp": signal.get("timestamp"),
        }
        has_resolution_context = any(
            payload.get(key)
            for key in ("station_url", "station_id", "city", "location_lat", "location_lon", "market_date")
        )
        has_target = bool(target)
        has_forecast = bool(payload["forecast_data"])
        if not (has_resolution_context or has_target or has_forecast):
            return None
        return json.dumps(payload)

    def assess_conflict(self, trade: Any, state: dict[str, Any]) -> dict[str, Any]:
        entry_model_prob = float(trade.entry_model_prob or trade.buy_price or 0.0)
        entry_market_prob = float(trade.entry_market_prob or trade.buy_price or 0.0)
        entry_confidence = float(trade.entry_confidence or 0.80)
        entry_spread = float(trade.entry_spread or 0.10)
        current_adjusted = float(state.get("adjusted_model_prob", 0.5))
        current_model_prob = current_adjusted if trade.side == "YES" else 1 - current_adjusted
        current_market_prob = (
            float(state.get("market_price_yes", trade.buy_price or 0.0))
            if trade.side == "YES"
            else float(state.get("market_price_no", trade.buy_price or 0.0))
        )
        current_confidence = float(state.get("confidence_score", entry_confidence))
        current_spread = float(state.get("ensemble_spread", entry_spread))

        entry_gap = max(0.0, entry_model_prob - entry_market_prob)
        current_gap = current_model_prob - current_market_prob
        gap_deterioration = max(0.0, entry_gap - current_gap)
        adverse_momentum = max(0.0, entry_market_prob - current_market_prob)
        spread_widening = max(0.0, current_spread - entry_spread)
        confidence_drop = max(0.0, entry_confidence - current_confidence)
        thesis_flip = 0.35 if state.get("action") and state.get("action") != f"BUY_{trade.side}" else 0.0

        regime_shift = 0.0
        if trade.entry_regime == "post_peak" and state.get("regime") == "pre_peak":
            regime_shift = 0.25
        elif trade.entry_regime == "near_peak" and state.get("regime") == "pre_peak":
            regime_shift = 0.18

        model_conflict = (
            min((current_market_prob - current_model_prob) * 2.5, 0.35)
            if current_model_prob < current_market_prob
            else 0.0
        )
        bust_stress = min(float(state.get("bust_risk", 0.0)) * 1.4, 0.15)

        conflict_score = min(
            1.0,
            (gap_deterioration * 1.8)
            + (adverse_momentum * 1.3)
            + (spread_widening * 1.0)
            + (confidence_drop * 0.9)
            + regime_shift
            + thesis_flip
            + model_conflict
            + bust_stress,
        )

        exit_fraction = 0.0
        if conflict_score >= 0.85 or current_model_prob <= 0.50:
            exit_fraction = 1.0
        elif conflict_score >= 0.70:
            exit_fraction = 0.75
        elif conflict_score >= 0.55:
            exit_fraction = 0.50
        elif conflict_score >= 0.35:
            exit_fraction = 0.25

        reasons = []
        if gap_deterioration > 0.03:
            reasons.append("edge deterioration")
        if adverse_momentum > 0.04:
            reasons.append("adverse price momentum")
        if spread_widening > 0.04:
            reasons.append("spread widening")
        if confidence_drop > 0.08:
            reasons.append("confidence weakening")
        if regime_shift > 0:
            reasons.append("regime shift")
        if thesis_flip > 0:
            reasons.append("action flip")
        if model_conflict > 0.10:
            reasons.append("market/model divergence")
        if bust_stress > 0.08:
            reasons.append("bust risk increase")

        return {
            "conflictScore": round(conflict_score, 4),
            "exitFraction": exit_fraction,
            "reason": ", ".join(reasons) if reasons else "conflict monitor triggered",
        }

    def send_paper_trade_alert(self, tg_id: str, signal: dict[str, Any], side: str, entry_price: float):
        if not self.telegram_bot_token:
            return

        rationale = build_signal_rationale(signal, side)
        lines = [
            "📊 <b>Paper Signal</b>",
            "",
            "<b>🪙 Market</b>",
            escape_html(signal.get("question") or signal.get("market_id")),
            "",
            "<b>🌡️ Forecast</b>",
            f"├ Temp {escape_html(format_forecast_temp(signal))}",
            f"└ Mode {escape_html(str(signal.get('mode') or 'standard'))}",
            "",
            "<b>🎯 Position</b>",
            f"├ {side}",
            f"├ Entry @{entry_price:.4f}",
            f"└ 1 Shares (Amount in pUSD)",
            "",
            "<b>Why This Side</b>",
            *[f"├ {escape_html(reason)}" if index < len(rationale) - 1 else f"└ {escape_html(reason)}" for index, reason in enumerate(rationale)],
        ]
        self.send_telegram_alert(tg_id, "\n".join(lines), "paper trade")

    def send_trade_alert(
        self,
        tg_id: str,
        signal: dict[str, Any],
        side: str,
        entry_price: float,
        size: float,
        order_response: dict[str, Any],
    ):
        lines = [
            "📈 <b>Live Signal</b>",
            "",
            "<b>🪙 Market</b>",
            escape_html(signal.get("question") or signal.get("market_id")),
            "",
            "<b>🎯 Position</b>",
            f"├ {side}",
            f"├ Entry @{entry_price:.4f}",
            f"└ {size} Shares (Amount in pUSD)",
        ]
        lines.extend(["", "<b>Status</b>", "Successful"])
        lines.extend(["", "<i>Order accepted by Polymarket.</i>"])
        self.send_telegram_alert(tg_id, "\n".join(lines), "trade")

    def send_exit_alert(
        self,
        tg_id: str,
        trade: Any,
        state: dict[str, Any],
        shares_sold: float,
        remaining_size: float,
        assessment: dict[str, Any],
        order_response: dict[str, Any],
    ):
        lines = [
            "<b>Conflict Exit</b>",
            "",
            "<b>Market</b>",
            escape_html(state.get("question") or trade.market_id),
            "",
            "<b>Side Reduced</b>          <b>Reason</b>",
            f"{trade.side}{' ' * max(1, 16 - len(str(trade.side)))}{assessment['reason']}",
            f"Sold {shares_sold} | Left {remaining_size}",
            "",
            "<b>Conflict Score</b>",
            f"<b>{float(assessment['conflictScore']):.2f}</b>",
        ]
        if order_response.get("orderID"):
            lines.append(f"<b>Order ID</b>  {escape_html(str(order_response.get('orderID')))}")
        if order_response.get("status"):
            lines.append(f"<b>Status</b>  {escape_html(str(order_response.get('status')))}")
        self.send_telegram_alert(tg_id, "\n".join(lines), "exit")

    def send_telegram_alert(self, tg_id: str, message: str, kind: str):
        if not self.telegram_bot_token:
            return

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": tg_id,
            "text": message,
            "parse_mode": "HTML",
        }

        last_error = None
        for attempt in range(1, 4):
            try:
                response = requests.post(url, json=payload, timeout=20)
                response.raise_for_status()
                body = response.json()
                if body.get("ok"):
                    return
                raise RuntimeError(body)
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    print(
                        f"[PYEXEC] Telegram {kind} alert failed for {tg_id} on attempt "
                        f"{attempt}/3: {exc}. Retrying..."
                    )
                    time.sleep(attempt)
                    continue
                print(f"[PYEXEC] Could not send {kind} alert to {tg_id}: {exc}")

        if last_error:
            print(f"[PYEXEC] Telegram {kind} alert gave up for {tg_id}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--once", action="store_true")
    args, _ = parser.parse_known_args()
    release_lock = acquire_process_lock("python-trade-executor")
    if not release_lock:
        raise SystemExit(0)
    executor = TradeExecutor()
    if args.once:
        executor.run_once()
    else:
        executor.run_loop()
