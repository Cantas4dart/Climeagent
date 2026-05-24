# -*- coding: utf-8 -*-
import argparse
import hashlib
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from .crypto import CryptoManager
from .db import DBManager, DateRangeTradeStats, DatedTradeStats, Trade, TradeStats, User
from .platform_fee import (
    PLATFORM_FEE_ADMIN_TG_ID,
    PLATFORM_FEE_RECIPIENT,
    calculate_platform_fee_from_pnl,
    calculate_platform_fee_from_trade,
    calculate_total_platform_fee,
    is_platform_fee_exempt,
)
from .polymarket import PolyMarketAPI
from .singleton import acquire_process_lock

load_dotenv()
WHITELIST_ADMIN_ID = PLATFORM_FEE_ADMIN_TG_ID


def get_default_polymarket_account_config() -> dict[str, Any]:
    raw_funder = (os.getenv("POLY_FUNDER_ADDRESS") or "").strip()
    raw_signature_type = (os.getenv("POLY_SIGNATURE_TYPE") or "").strip()
    signature_type = int(raw_signature_type) if raw_signature_type.isdigit() else None
    funder_address = raw_funder if raw_funder and "your_polymarket" not in raw_funder else None
    return {"funderAddress": funder_address, "signatureType": signature_type}


def resolve_user_polymarket_account_config(user: User | None) -> dict[str, Any]:
    defaults = get_default_polymarket_account_config()
    return {
        "funderAddress": getattr(user, "funder_address", None) or defaults["funderAddress"],
        "signatureType": getattr(user, "signature_type", None)
        if getattr(user, "signature_type", None) is not None
        else defaults["signatureType"],
    }


def extract_allowance(balance_data: dict[str, Any]) -> float:
    try:
        direct = balance_data.get("allowance")
        if direct is not None:
            return float(direct)
    except (TypeError, ValueError):
        pass
    standard_ex = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    try:
        return float((balance_data.get("allowances") or {}).get(standard_ex))
    except (TypeError, ValueError):
        return 0.0


def has_imported_wallet(user: User | None) -> bool:
    return bool(
        user
        and isinstance(user.private_key, str)
        and user.private_key.strip()
        and isinstance(user.api_key, str)
        and user.api_key.strip()
        and isinstance(user.api_secret, str)
        and user.api_secret.strip()
        and isinstance(user.api_passphrase, str)
        and user.api_passphrase.strip()
    )


def truncate_middle(value: str, start: int = 8, end: int = 6) -> str:
    if not value or len(value) <= start + end + 3:
        return value
    return f"{value[:start]}...{value[-end:]}"


def format_key_value(label: str, value: Any, width: int = 17) -> str:
    return f"{label.ljust(width)} {value}"


def wrap_code_block(lines: list[str]) -> str:
    return "\n".join(lines)


def format_position_summary(position: dict[str, Any]) -> str:
    asset = position.get("displayLabel") or position.get("title") or position.get("asset") or "Unknown asset"
    size = position.get("size") or position.get("balance") or "?"
    avg_price = position.get("avgPrice") or position.get("averagePrice") or "?"
    return f"- {asset}: {size} @ {avg_price}"


def format_order_summary(order: dict[str, Any]) -> str:
    label = order.get("outcome") or order.get("asset_id") or order.get("market") or "Order"
    size = order.get("original_size") or order.get("size") or "?"
    price = order.get("price") or "?"
    status = order.get("status") or "open"
    return f"{label}: {order.get('side')} {size} @ {price} ({status})"


def format_real_trade_history(trade: Trade) -> str:
    result = "OPEN" if not trade.settled else ("WIN" if trade.outcome == 1 else "LOSS")
    pnl = f" | PnL {float(trade.pnl or 0):.2f} pUSD" if trade.settled else ""
    return f"#{trade.id} {trade.side} {trade.market_id} | {result}{pnl}"


def format_paper_trade_history(trade: Any) -> str:
    result = "OPEN" if not trade.settled else ("WIN" if trade.outcome == 1 else "LOSS")
    pnl = f" | PnL {float(trade.pnl or 0):.4f} pUSD" if trade.settled else ""
    return f"#{trade.id} {trade.side} {trade.market_id} | {result}{pnl}"


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def build_positions_keyboard(auto_claim: bool, has_claimables: bool) -> dict[str, Any]:
    return keyboard(
        [
            [("🏠 Home", "positions:main"), ("🔄 Refresh", "positions:refresh")],
            [("🏆 Claimable", "positions:claimable"), ("⚙️ Setup", "positions:setup")],
            [("📋 Orders", "positions:orders"), ("📜 History", "positions:history")],
            [("💰 Balance", "positions:balance"), ("📈 Status", "positions:status")],
            [("📊 Stats", "positions:stats"), ("📅 Daily", "positions:daily"), ("📆 Weekly", "positions:weekly")],
            [("📊 All Time", "positions:all_time")],
            [("🎛️ Controls", "positions:controls"), ("❓ Help", "positions:help")],
            [
                ("🎉 Claim All", "positions:claim_all" if has_claimables else "positions:claimable"),
                ("✋ Auto-Claim Off" if auto_claim else "🤖 Auto-Claim On", "positions:auto_claim_off" if auto_claim else "positions:auto_claim_on"),
            ],
        ]
    )


def build_onboarding_keyboard() -> dict[str, Any]:
    return keyboard([[("💰 Real Trade", "positions:real_trade"), ("📝 Paper Trade", "positions:paper_trade")], [("❓ Help", "positions:help")]])


def build_setup_keyboard(has_user: bool, has_wallet: bool) -> dict[str, Any]:
    return keyboard(
        [
            [("🏠 Home", "positions:main"), ("🔄 Refresh", "positions:setup")],
            [("📥 Import Wallet", "positions:import_start"), ("✓ Approve", "positions:approve" if has_wallet else "positions:import_start")],
            [("🔍 Wallet Check", "positions:wallet_check" if has_wallet else "positions:import_start"), ("💵 Fund Wallet", "positions:fund_prompt" if has_wallet else "positions:import_start")],
            [("⚙️ Risk Settings", "positions:risk_settings" if has_user else "positions:help"), ("🎛️ Controls", "positions:controls" if has_user else "positions:help")],
            [("🗑️ Remove Wallet", "positions:remove_wallet"), ("❓ Help", "positions:help")],
        ]
    )


def build_detail_keyboard(auto_claim: bool, has_claimables: bool, page: str) -> dict[str, Any]:
    return keyboard(
        [
            [("🏠 Home", "positions:main"), ("📊 Dashboard", "positions:refresh"), ("🔄 Refresh", f"positions:{page}")],
            [("🏆 Claimable", "positions:claimable"), ("⚙️ Setup", "positions:setup")],
            [("💰 Balance", "positions:balance"), ("📈 Status", "positions:status")],
            [("📊 Overall", "positions:stats"), ("📅 Daily", "positions:daily")],
            [("📆 Weekly", "positions:weekly")],
            [("🎛️ Controls", "positions:controls"), ("❓ Help", "positions:help")],
            [
                ("🎉 Claim All", "positions:claim_all" if has_claimables else "positions:claimable"),
                ("✋ Auto-Claim Off" if auto_claim else "🤖 Auto-Claim On", "positions:auto_claim_off" if auto_claim else "positions:auto_claim_on"),
            ],
        ]
    )


def build_risk_keyboard() -> dict[str, Any]:
    return keyboard(
        [
            [("⚙️ Setup", "positions:setup"), ("🔄 Refresh", "positions:risk_settings")],
            [("📊 Set Risk %", "positions:risk_prompt"), ("💵 Set Max $", "positions:max_prompt")],
            [("📈 Set Max Open", "positions:max_open_prompt"), ("🎛️ Controls", "positions:controls")],
        ]
    )


def build_report_keyboard() -> dict[str, Any]:
    return keyboard(
        [
            [("🏠 Home", "positions:main"), ("🔄 Refresh", "positions:reports")],
            [("📅 Daily Report", "positions:report_daily"), ("📆 Weekly Report", "positions:report_weekly")],
            [("📊 All-Time Stats", "positions:report_alltime")],
            [("🎛️ Controls", "positions:controls"), ("❓ Help", "positions:help")],
        ]
    )


def build_controls_keyboard(user: User | None) -> dict[str, Any]:
    return keyboard(
        [
            [("🏠 Home", "positions:main"), ("🔄 Refresh", "positions:controls")],
            [("⏸️ Stop Trading" if user and user.trading_active else "▶️ Start Trading", "positions:stop_trading" if user and user.trading_active else "positions:start_trading"), ("✋ Auto-Claim Off" if user and user.auto_claim else "🤖 Auto-Claim On", "positions:auto_claim_off" if user and user.auto_claim else "positions:auto_claim_on")],
            [("📝 Paper Testing Off" if user and user.paper_testing_active else "📝 Paper Testing On", "positions:paper_testing_off" if user and user.paper_testing_active else "positions:paper_testing_on"), ("⚙️ Risk Settings", "positions:risk_settings")],
            [("⚙️ Setup", "positions:setup"), ("📈 Status", "positions:status")],
            [("❓ Help", "positions:help")],
        ]
    )


def build_claimable_keyboard(claimable_trades: list[Trade], auto_claim: bool) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    for index, trade in enumerate(claimable_trades[:5], start=1):
        rows.append([(f"🏷️ {index}. {truncate_middle(str(trade.market_id or ''), 8, 4)}", f"positions:claim:{trade.id}")])
    if claimable_trades:
        rows.append([("🎉 Claim All", "positions:claim_all")])
    rows.append([("✋ Auto-Claim Off" if auto_claim else "🤖 Auto-Claim On", "positions:auto_claim_off" if auto_claim else "positions:auto_claim_on")])
    rows.append([("⬅️ Back", "positions:refresh"), ("🔄 Refresh", "positions:claimable")])
    return keyboard(rows)


def get_utc_date_key_with_offset(days_offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_offset)).strftime("%Y-%m-%d")


def format_report_date(date_key: str) -> str:
    return datetime.strptime(date_key, "%Y-%m-%d").strftime("%B %-d") if os.name != "nt" else datetime.strptime(date_key, "%Y-%m-%d").strftime("%B %#d")


def build_paper_stats_lines(paper_stats: dict[str, Any]) -> list[str]:
    hit_rate_label = f"{paper_stats['winRate']}% hit rate" if paper_stats["settled"] > 0 else "No settled paper tests yet"
    return [
        "Paper Test Lab",
        format_key_value("Total Signals Tracked", paper_stats["total"]),
        format_key_value("Open Simulations", paper_stats["open"]),
        format_key_value("Settled Simulations", paper_stats["settled"]),
        format_key_value("Scoreline", f"{paper_stats['wins']} wins / {paper_stats['losses']} losses"),
        format_key_value("Paper Edge", hit_rate_label),
        format_key_value("Paper PnL", f"{float(paper_stats['pnl']):.3f} pUSD"),
    ]


def build_report_section(title: str, stats: TradeStats, pnl_digits: int = 2) -> list[str]:
    return [
        title,
        format_key_value("Trades", stats.total),
        format_key_value("Settled", stats.settled),
        format_key_value("Wins", stats.wins),
        format_key_value("Losses", stats.losses),
        format_key_value("Win Rate", f"{stats.winRate}%"),
        format_key_value("PnL", f"{float(stats.pnl):.{pnl_digits}f} pUSD"),
    ]


def build_paper_report_message(period: str, stats: Any, overall: dict[str, Any]) -> str:
    if period == "Daily" and getattr(stats, "dateKey", None):
        title = f"Paper Daily Report - {format_report_date(stats.dateKey)}"
        period_label = format_report_date(stats.dateKey)
    elif period == "Weekly" and getattr(stats, "startDateKey", None):
        title = f"Paper Weekly Report - {format_report_date(stats.startDateKey)} to {format_report_date(stats.endDateKey)}"
        period_label = f"{format_report_date(stats.startDateKey)} - {format_report_date(stats.endDateKey)}"
    else:
        title = "Paper All-Time Statistics"
        period_label = "All-Time"
    lines = [
        title,
        "",
        *build_report_section(f"{period_label} Paper Tests", stats, 3),
        "",
        "Cumulative Paper Testing",
        format_key_value("Total Simulated Trades", overall["total"]),
        format_key_value("Signals Tracked", overall["settled"]),
        format_key_value("Wins", overall["wins"]),
        format_key_value("Losses", overall["losses"]),
        format_key_value("Test Hit Rate", f"{overall['winRate']}%"),
        format_key_value("Cumulative Paper PnL", f"{float(overall['pnl']):.3f} pUSD"),
    ]
    return wrap_code_block([str(line) for line in lines])


def build_real_report_message(period: str, stats: Any, overall: TradeStats) -> str:
    if period == "Daily" and getattr(stats, "dateKey", None):
        title = f"Live Trading Daily Report - {format_report_date(stats.dateKey)}"
        period_label = format_report_date(stats.dateKey)
    elif period == "Weekly" and getattr(stats, "startDateKey", None):
        title = f"Live Trading Weekly Report - {format_report_date(stats.startDateKey)} to {format_report_date(stats.endDateKey)}"
        period_label = f"{format_report_date(stats.startDateKey)} - {format_report_date(stats.endDateKey)}"
    else:
        title = "Live Trading All-Time Statistics"
        period_label = "All-Time"
    lines = [
        title,
        "",
        *build_report_section(f"{period_label} Live Trades", stats, 2),
        "",
        "Cumulative Live Trading",
        format_key_value("Total Trades Executed", overall.total),
        format_key_value("Settled Positions", overall.settled),
        format_key_value("Wins", overall.wins),
        format_key_value("Losses", overall.losses),
        format_key_value("Live Win Rate", f"{overall.winRate}%"),
        format_key_value("Cumulative Live PnL", f"{float(overall.pnl):.2f} pUSD"),
    ]
    return wrap_code_block([str(line) for line in lines])


class TelegramPollingBot:
    def __init__(self):
        self.token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self.db = DBManager()
        self.crypto = CryptoManager()
        self.sessions: dict[str, dict[str, str]] = {}
        self.offset = self._load_offset()
        if self.offset == 0:
            self.offset = self._bootstrap_offset()

    def _offset_file_path(self) -> Path:
        token_hash = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]
        return Path(__file__).resolve().parent.parent / "data" / "offsets" / f"{token_hash}.txt"

    def _load_offset(self) -> int:
        """Load the last processed update offset for the active bot token."""
        offset_file = self._offset_file_path()
        try:
            if offset_file.exists():
                return int(offset_file.read_text().strip())
        except Exception:
            pass
        return 0

    def _bootstrap_offset(self) -> int:
        """Skip stale queued updates the first time a new bot token is used."""
        try:
            result = requests.get(f"{self.api_base}/getUpdates", params={"timeout": 0, "offset": -1, "limit": 1}, timeout=10)
            result.raise_for_status()
            body = result.json()
            updates = body.get("result", []) if body.get("ok") else []
            if updates:
                return int(updates[-1]["update_id"]) + 1
        except Exception as exc:
            print(f"[BOT WARNING] Could not bootstrap offset: {exc}")
        return 0

    def _save_offset(self):
        """Save the current offset for the active bot token."""
        offset_file = self._offset_file_path()
        try:
            offset_file.parent.mkdir(parents=True, exist_ok=True)
            offset_file.write_text(str(self.offset))
        except Exception as exc:
            print(f"[BOT WARNING] Could not save offset: {exc}")

    @staticmethod
    def whitelist_denied_message() -> str:
        return "Access denied. Your Telegram ID is not authorized for this bot."

    def can_manage_whitelist(self, user_id: str) -> bool:
        return str(user_id or "") == WHITELIST_ADMIN_ID

    def is_authorized_user(self, user_id: str) -> bool:
        normalized = str(user_id or "")
        return self.can_manage_whitelist(normalized) or self.db.is_whitelisted(normalized)

    def ensure_authorized_user_profile(self, user_id: str):
        normalized = str(user_id or "")
        if not normalized or not self.is_authorized_user(normalized):
            return
        try:
            self.db.ensure_user(normalized)
        except Exception as exc:
            print(f"[BOT WARNING] Could not ensure user profile for {normalized}: {exc}")

    @staticmethod
    def parse_command_parts(text: str) -> tuple[str, str]:
        parts = text.split(maxsplit=1)
        raw_command = parts[0] if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        command = raw_command.split("@", 1)[0]
        return command, arg

    def session_for(self, user_id: str) -> dict[str, str]:
        return self.sessions.setdefault(user_id, {"step": "", "pending_private_key": "", "pending_funder_address": ""})

    def api_call(self, method: str, payload: dict[str, Any] | None = None):
        response = requests.post(f"{self.api_base}/{method}", json=payload or {}, timeout=30)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body)
        return body.get("result")

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.api_call("sendMessage", payload)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            self.api_call("editMessageText", payload)
            return True
        except Exception as exc:
            error_str = str(exc)
            if "message is not modified" in error_str or "400" in error_str:
                return False
            raise

    def answer_callback(self, callback_query_id: str, text: str, show_alert: bool = False):
        return self.api_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert})

    def delete_message(self, chat_id: int, message_id: int):
        return self.api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def poll(self):
        result = requests.get(f"{self.api_base}/getUpdates", params={"timeout": 25, "offset": self.offset}, timeout=35)
        result.raise_for_status()
        body = result.json()
        if not body.get("ok"):
            raise RuntimeError(body)
        return body.get("result", [])

    def run(self):
        print("--------------------------")
        print("Climeagent Python Bot Starting...")
        print("--------------------------")
        while True:
            try:
                for update in self.poll():
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    try:
                        self.handle_update(update)
                    except Exception as exc:
                        print(f"[BOT ERROR] Update handling failed: {exc}")
                    finally:
                        # Persist progress per update so stale commands are not replayed.
                        self._save_offset()
            except Exception as exc:
                print(f"[BOT ERROR] {exc}")
                time.sleep(2)

    def run_once(self):
        print("[BOT] Running single-pass polling check.")
        try:
            for update in self.poll():
                self.offset = max(self.offset, int(update["update_id"]) + 1)
                try:
                    self.handle_update(update)
                except Exception as exc:
                    print(f"[BOT ERROR] Update handling failed: {exc}")
                finally:
                    self._save_offset()
        except Exception as exc:
            print(f"[BOT ERROR] {exc}")

    def with_dashboard_notice(self, base_text: str, notice: str | None = None) -> str:
        if not notice:
            return base_text
        return "\n".join(["Dashboard Update", notice, "", base_text])

    def build_welcome_dashboard(self) -> str:
        return wrap_code_block(
            [
                "CLIME | Weather Prediction Trading",
                "",
                "Welcome to Clime Weather Agent - AI-powered temperature forecasting and automated market trading.",
                "",
                "Choose your trading mode:",
                "",
                "Real Trade",
                "Import your wallet and trade real markets with Clime's verified weather forecasts.",
                "",
                "Paper Trade",
                "Test Clime signals and strategies without real capital using simulated positions.",
            ]
        )

    def build_setup_message(self, user: User | None = None) -> str:
        if not user or not has_imported_wallet(user):
            return wrap_code_block(
                [
                    "Setup Center",
                    "",
                    "No wallet is currently attached. Choose Real Trade to import one, or use Paper Trade without a live wallet.",
                    "",
                    "Available After Import",
                    format_key_value("Approve", "trading allowance"),
                    format_key_value("Check", "signer, funder, proxy"),
                    format_key_value("Move", "pUSD into trading wallet"),
                    format_key_value("Tune", "risk, max size, max open"),
                    "",
                    "Testing",
                    format_key_value("Paper Signal", "ON" if user and user.paper_testing_active else "OFF"),
                ]
            )
        account_config = resolve_user_polymarket_account_config(user)
        return wrap_code_block(
            [
                "Setup Center",
                "",
                "Wallet",
                format_key_value("Funder", account_config["funderAddress"] or "wallet address"),
                format_key_value("Signature Type", account_config["signatureType"] if account_config["signatureType"] is not None else "default(EOA)"),
                "",
                "Ready Actions",
                format_key_value("Import", "replace wallet credentials"),
                format_key_value("Approve", "refresh trading allowance"),
                format_key_value("Wallet Check", "verify signer and proxy"),
                format_key_value("Fund Wallet", "move pUSD into trading wallet"),
                format_key_value("Risk Settings", "update limits"),
                format_key_value("Remove Wallet", "wipe live credentials"),
                "",
                "Testing",
                format_key_value("Paper Signal", "ON" if user.paper_testing_active else "OFF"),
            ]
        )

    def build_controls_message(self, user: User) -> str:
        return wrap_code_block(
            [
                "Controls Center",
                "",
                "Trading",
                format_key_value("Status", "Active" if user.trading_active else "Stopped"),
                format_key_value("Auto-Claim", "ON" if user.auto_claim else "OFF"),
                format_key_value("Paper Testing", "ON" if user.paper_testing_active else "OFF"),
                "",
                "Exposure",
                format_key_value("Risk", f"{user.risk_percent}%"),
                format_key_value("Max Trade", f"${user.max_trade_amount}"),
                format_key_value("Max Open", user.max_open_positions),
            ]
        )

    def build_risk_settings_message(self, user: User) -> str:
        return wrap_code_block(
            [
                "Risk Settings",
                "",
                format_key_value("Risk Per Trade", f"{user.risk_percent}%"),
                format_key_value("Max Trade Amount", f"${user.max_trade_amount}"),
                format_key_value("Max Open Positions", user.max_open_positions),
                format_key_value("Paper Testing", "ON" if user.paper_testing_active else "OFF"),
                "",
                "Choose a setting below and then send the new value in chat.",
            ]
        )

    def build_status_message(self, user: User) -> str:
        account_config = resolve_user_polymarket_account_config(user)
        return wrap_code_block(
            [
                "Bot Status",
                "",
                "Controls",
                format_key_value("Trading", "Active" if user.trading_active else "Stopped"),
                format_key_value("Auto-Claim", "ON" if user.auto_claim else "OFF"),
                "",
                "Risk Profile",
                format_key_value("Risk", f"{user.risk_percent}%"),
                format_key_value("Max Trade", f"${user.max_trade_amount}"),
                format_key_value("Max Open Positions", user.max_open_positions),
                "",
                "Account",
                format_key_value("Signature Type", account_config["signatureType"] if account_config["signatureType"] is not None else "default(EOA)"),
                format_key_value("Funder", account_config["funderAddress"] or "wallet address"),
            ]
        )

    def build_stats_message(self, overall: TradeStats, paper_stats: dict[str, Any] | None = None) -> str:
        lines = [
            "Overall Performance",
            "",
            format_key_value("Total Trades", overall.total),
            format_key_value("Settled", overall.settled),
            format_key_value("Wins", overall.wins),
            format_key_value("Losses", overall.losses),
            format_key_value("Win Rate", f"{overall.winRate}%"),
            format_key_value("Total PnL", f"{float(overall.pnl):.2f} pUSD"),
        ]
        if paper_stats:
            lines.extend(["", *build_paper_stats_lines(paper_stats)])
        return wrap_code_block(lines)

    def build_claimable_message(self, claimable_trades: list[Trade]) -> str:
        if not claimable_trades:
            return wrap_code_block(["Claim Center", "", "No settled winning trades are waiting to be claimed."])
        total_size = sum(float(trade.size or 0) for trade in claimable_trades)
        lines = [
            "Claim Center",
            "",
            "Overview",
            format_key_value("Claimable Markets", len(claimable_trades)),
            format_key_value("Claimable Size", f"{total_size:.2f}"),
            "",
            "Ready To Claim",
        ]
        for index, trade in enumerate(claimable_trades[:5], start=1):
            lines.extend([f"{index}. {trade.market_id}", format_key_value("Side", trade.side), format_key_value("Size", trade.size), format_key_value("Entry", f"{float(trade.buy_price or 0):.4f}"), ""])
        return wrap_code_block([str(line) for line in lines]).strip()

    def build_balance_message(self, user: User) -> str:
        account_config = resolve_user_polymarket_account_config(user)
        poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, account_config)
        balance_data = poly.get_balance()
        balance_num = float(balance_data.get("balance", 0))
        allowance_num = extract_allowance(balance_data)
        signer_address = poly.get_signer_address()
        funder_address = poly.get_configured_funder_address()
        signer_wallet_pusd = poly.get_wallet_pusd_balance(signer_address)
        funder_wallet_pusd = signer_wallet_pusd if signer_address.lower() == funder_address.lower() else poly.get_wallet_pusd_balance(funder_address)
        return wrap_code_block(
            [
                "pUSD Status",
                "",
                format_key_value("Trading Balance", f"{balance_num / 1_000_000:.2f} pUSD"),
                format_key_value("Trading Allowance", f"{allowance_num / 1_000_000:.2f} pUSD"),
                format_key_value("Signature Type", account_config["signatureType"] if account_config["signatureType"] is not None else "default(EOA)"),
                format_key_value("Signer", signer_address),
                format_key_value("Signer Wallet pUSD", f"{signer_wallet_pusd / 1_000_000:.2f} pUSD"),
                format_key_value("Funder", funder_address),
                format_key_value("Funder Wallet pUSD", f"{funder_wallet_pusd / 1_000_000:.2f} pUSD"),
                "",
                "Note: If balance is wrong, ensure your wallet holds pUSD on Polygon.",
            ]
        )

    def build_wallet_check_message(self, user: User) -> str:
        account_config = resolve_user_polymarket_account_config(user)
        poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, account_config)
        signer_address = poly.get_signer_address()
        funder_address = poly.get_configured_funder_address()
        profile = poly.get_public_profile_by_wallet(funder_address)
        proxy_wallet = profile.get("proxyWallet") if isinstance(profile, dict) else None
        return wrap_code_block(
            [
                "Wallet Check",
                "",
                format_key_value("Signer", signer_address),
                format_key_value("Configured Funder", funder_address),
                format_key_value("Profile Proxy", proxy_wallet or "not found"),
                format_key_value("Signature Type", account_config["signatureType"] if account_config["signatureType"] is not None else "default(EOA)"),
                "",
                "Funder matches Polymarket profile proxy wallet."
                if proxy_wallet and str(proxy_wallet).lower() == funder_address.lower()
                else "Funder does not match the Polymarket profile proxy wallet, or no profile was found.",
            ]
        )

    def build_positions_dashboard(self, user_id: str, user: User) -> dict[str, Any]:
        tracked_open_positions = self.db.get_unsettled_trade_count(user_id)
        claimable_trades = self.db.get_claimable_trades(user_id)
        paper_stats = self.db.get_paper_stats(user_id)
        if user.paper_testing_active:
            paper_all = self.db.get_paper_trades_for_user(user_id)
            lines = ["Paper Testing Lab", "", "Mode: Paper Trading (Simulation Only)", "", *build_paper_stats_lines(paper_stats)]
            if paper_all:
                lines.extend(["", "Recent Paper Trades"])
                for trade in paper_all[:6]:
                    lines.append(format_paper_trade_history(trade))
            else:
                lines.extend(["", "No paper trades yet."])
            return {"text": wrap_code_block(lines), "keyboard": build_positions_keyboard(bool(user.auto_claim), False)}
        if not has_imported_wallet(user):
            lines = [
                "Position Center",
                "",
                "Overview",
                format_key_value("Open Positions", tracked_open_positions),
                format_key_value("Working Orders", 0),
                format_key_value("Claimable", len(claimable_trades)),
                format_key_value("Auto-Claim", "ON" if user.auto_claim else "OFF"),
                "",
                "Wallet",
                format_key_value("Live Wallet", "not attached"),
            ]
            if tracked_open_positions == 0:
                lines.extend(["", "Activity", "No open positions or working orders right now."])
            return {"text": wrap_code_block(lines), "keyboard": build_positions_keyboard(bool(user.auto_claim), len(claimable_trades) > 0)}
        account_config = resolve_user_polymarket_account_config(user)
        poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, account_config)
        positions_address = account_config["funderAddress"] or poly.get_signer_address()
        positions = poly.get_positions(positions_address)
        open_orders = poly.get_open_orders()
        lines = [
            "Live Trading Center",
            "",
            "Overview",
            format_key_value("Open Positions", tracked_open_positions),
            format_key_value("Working Orders", len(open_orders or [])),
            format_key_value("Claimable", len(claimable_trades)),
            format_key_value("Auto-Claim", "ON" if user.auto_claim else "OFF"),
            "",
            "Live Wallet",
            format_key_value("Dashboard Wallet", truncate_middle(positions_address, 10, 6)),
        ]
        if positions:
            lines.extend(["", "Positions"])
            for position in positions[:6]:
                lines.append(format_position_summary(position))
        if open_orders:
            lines.extend(["", "Working Orders"])
            for order in open_orders[:6]:
                lines.append(format_order_summary(order))
        return {"text": wrap_code_block(lines), "keyboard": build_positions_keyboard(bool(user.auto_claim), len(claimable_trades) > 0)}

    def build_active_orders_page(self, user: User) -> dict[str, Any]:
        if not has_imported_wallet(user):
            return {"text": wrap_code_block(["Active Market Orders", "", "Import a live wallet first to view active orders."]), "keyboard": build_positions_keyboard(bool(user.auto_claim), False)}
        poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
        open_orders = poly.get_open_orders()
        lines = ["Active Market Orders", "", format_key_value("Count", len(open_orders or []))]
        if open_orders:
            lines.append("")
            for order in open_orders[:6]:
                lines.append(format_order_summary(order))
        else:
            lines.extend(["", "No active market orders right now."])
        return {"text": wrap_code_block(lines), "keyboard": build_positions_keyboard(bool(user.auto_claim), False)}

    def build_trade_history_page(self, user_id: str, user: User) -> dict[str, Any]:
        real_all = self.db.get_trades_for_user(user_id)
        paper_all = self.db.get_paper_trades_for_user(user_id)
        lines = ["Trade History", "", "Real Trades"]
        if real_all:
            for trade in real_all[:6]:
                lines.append(format_real_trade_history(trade))
        else:
            lines.append("No real trades yet.")
        lines.extend(["", "Paper Trades"])
        if paper_all:
            for trade in paper_all[:6]:
                lines.append(format_paper_trade_history(trade))
        else:
            lines.append("No paper trades yet.")
        return {"text": wrap_code_block(lines), "keyboard": build_positions_keyboard(bool(user.auto_claim), False)}

    def render_dashboard_page(self, user_id: str, user: User | None, page: str, notice: str | None = None) -> dict[str, Any]:
        claimable_trades = self.db.get_claimable_trades(user_id) if user else []
        auto_claim = bool(user.auto_claim) if user else False
        has_claimables = len(claimable_trades) > 0
        if not user:
            return {"text": self.with_dashboard_notice(self.build_welcome_dashboard(), notice or "No wallet profile found yet. Choose Real Trade or Paper Trade to begin."), "keyboard": build_onboarding_keyboard()}
        if page in {"welcome"}:
            return {"text": self.with_dashboard_notice(self.build_welcome_dashboard(), notice), "keyboard": build_onboarding_keyboard()}
        if page in {"refresh", "main"}:
            dashboard = self.build_positions_dashboard(user_id, user)
            return {"text": self.with_dashboard_notice(dashboard["text"], notice), "keyboard": dashboard["keyboard"]}
        if page == "orders":
            view = self.build_active_orders_page(user)
            return {"text": self.with_dashboard_notice(view["text"], notice), "keyboard": view["keyboard"]}
        if page == "history":
            view = self.build_trade_history_page(user_id, user)
            return {"text": self.with_dashboard_notice(view["text"], notice), "keyboard": view["keyboard"]}
        if page == "setup":
            return {"text": self.with_dashboard_notice(self.build_setup_message(user), notice), "keyboard": build_setup_keyboard(True, has_imported_wallet(user))}
        if page == "claimable":
            return {"text": self.with_dashboard_notice(self.build_claimable_message(claimable_trades), notice), "keyboard": build_claimable_keyboard(claimable_trades, auto_claim)}
        if page == "balance":
            return {"text": self.with_dashboard_notice(self.build_balance_message(user), notice), "keyboard": build_detail_keyboard(auto_claim, has_claimables, "balance")}
        if page == "status":
            return {"text": self.with_dashboard_notice(self.build_status_message(user), notice), "keyboard": build_detail_keyboard(auto_claim, has_claimables, "status")}
        if page == "controls":
            return {"text": self.with_dashboard_notice(self.build_controls_message(user), notice), "keyboard": build_controls_keyboard(user)}
        if page == "risk_settings":
            return {"text": self.with_dashboard_notice(self.build_risk_settings_message(user), notice), "keyboard": build_risk_keyboard()}
        if page == "wallet_check":
            return {"text": self.with_dashboard_notice(self.build_wallet_check_message(user), notice), "keyboard": build_setup_keyboard(True, has_imported_wallet(user))}
        if page == "stats":
            overall = self.db.get_overall_stats(user_id)
            paper_stats = self.db.get_paper_stats(user_id)
            text = "Overall Performance\n\nNo trades recorded yet." if overall.total == 0 and paper_stats["total"] == 0 else self.build_stats_message(overall, paper_stats)
            return {"text": self.with_dashboard_notice(text, notice), "keyboard": build_detail_keyboard(auto_claim, has_claimables, "stats")}
        if page in {"report_daily", "report_weekly", "report_alltime"}:
            if page == "report_daily":
                daily = self.db.get_daily_stats(user_id, get_utc_date_key_with_offset(-1))
                paper_daily = self.db.get_paper_daily_stats(user_id, get_utc_date_key_with_offset(-1))
                overall = self.db.get_overall_stats(user_id)
                paper_overall = self.db.get_paper_stats(user_id)
                text = build_paper_report_message("Daily", paper_daily, paper_overall) if user.paper_testing_active and paper_daily.total > 0 else build_real_report_message("Daily", daily, overall) if (not user.paper_testing_active and daily.total > 0) else ("Paper Daily Report\n\nNo paper trades yet." if user.paper_testing_active else "Real Daily Report\n\nNo real trades yet.")
            elif page == "report_weekly":
                end_date_key = get_utc_date_key_with_offset(-1)
                start_date_key = get_utc_date_key_with_offset(-7)
                weekly = self.db.get_weekly_stats(user_id, start_date_key, end_date_key)
                paper_weekly = self.db.get_paper_weekly_stats(user_id, start_date_key, end_date_key)
                overall = self.db.get_overall_stats(user_id)
                paper_overall = self.db.get_paper_stats(user_id)
                text = build_paper_report_message("Weekly", paper_weekly, paper_overall) if user.paper_testing_active and paper_weekly.total > 0 else build_real_report_message("Weekly", weekly, overall) if (not user.paper_testing_active and weekly.total > 0) else ("Paper Weekly Report\n\nNo paper trades yet." if user.paper_testing_active else "Real Weekly Report\n\nNo real trades yet.")
            else:
                overall = self.db.get_overall_stats(user_id)
                paper_overall = self.db.get_paper_stats(user_id)
                text = build_paper_report_message("All-Time", paper_overall, paper_overall) if user.paper_testing_active and paper_overall["total"] > 0 else build_real_report_message("All-Time", overall, overall) if (not user.paper_testing_active and overall.total > 0) else ("Paper All-Time Stats\n\nNo paper trades yet." if user.paper_testing_active else "Real All-Time Stats\n\nNo real trades yet.")
            return {"text": self.with_dashboard_notice(text, notice), "keyboard": build_report_keyboard()}
        if page == "reports":
            text = "Paper Trade Reports\n\nSelect a report type:" if user.paper_testing_active else "Real Trade Reports\n\nSelect a report type:"
            return {"text": self.with_dashboard_notice(text, notice), "keyboard": build_report_keyboard()}
        if page == "help":
            return {"text": self.with_dashboard_notice("Dashboard Help\n\nUse this dashboard as the main control center for positions, claims, balances, and reports.\n\nUse commands for setup, approvals, funding, wallet checks, and risk-setting flows that need manual input.", notice), "keyboard": build_detail_keyboard(auto_claim, has_claimables, "help")}
        return self.render_dashboard_page(user_id, user, "main", notice)

    def claim_trade_by_id_for_user(self, user_id: str, user: User, trade_id: int) -> str:
        claimable_trades = self.db.get_claimable_trades(user_id)
        trade = next((item for item in claimable_trades if int(item.id) == trade_id), None)
        if not trade:
            raise ValueError("No settled winning trade is waiting to be claimed for that selection.")
        if not trade.condition_id:
            raise ValueError("This trade is missing a condition id, so the bot cannot redeem it automatically.")
        poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
        tx_hash = poly.redeem_winnings(str(trade.condition_id))
        self.db.mark_claimed_by_condition(user_id, str(trade.condition_id), tx_hash)
        grouped_trades = [item for item in claimable_trades if str(item.condition_id or "") == str(trade.condition_id)]
        fee_result = self.collect_platform_fee_for_trades(user_id, poly, grouped_trades)
        return f"https://polygonscan.com/tx/{tx_hash}{self.format_platform_fee_note(fee_result)}"

    def claim_all_for_user(self, user_id: str, user: User) -> str:
        claimable_trades = self.db.get_claimable_trades(user_id)
        if not claimable_trades:
            return "No settled winning trades are waiting to be claimed."
        unique_conditions = {}
        for trade in claimable_trades:
            if trade.condition_id and trade.condition_id not in unique_conditions:
                unique_conditions[trade.condition_id] = trade
        if not unique_conditions:
            return "Claimable trades were found, but none have a usable condition id."
        poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
        receipts: list[str] = []
        failures: list[str] = []
        for trade in unique_conditions.values():
            try:
                tx_hash = poly.redeem_winnings(str(trade.condition_id))
                self.db.mark_claimed_by_condition(user_id, str(trade.condition_id), tx_hash)
                grouped_trades = [item for item in claimable_trades if str(item.condition_id or "") == str(trade.condition_id)]
                fee_result = self.collect_platform_fee_for_trades(user_id, poly, grouped_trades)
                receipts.append(f"{trade.market_id}: https://polygonscan.com/tx/{tx_hash}{self.format_platform_fee_note(fee_result)}")
            except Exception as exc:
                failures.append(f"{trade.market_id}: {exc}")
        return "\n".join((([f"Claims submitted: {len(receipts)}", *receipts] if receipts else []) + ([f"Failures: {len(failures)}", *failures] if failures else [])))

    def collect_platform_fee_for_trades(self, user_id: str, poly: PolyMarketAPI, trades: list[Trade]) -> dict[str, Any]:
        if not trades:
            return {"applied": False, "reason": "no_trades"}
        if is_platform_fee_exempt(user_id):
            return {"applied": False, "reason": "admin_exempt"}

        fee_amount = calculate_total_platform_fee(trades, user_id)
        if fee_amount <= 0:
            return {"applied": False, "reason": "no_fee_due"}

        for trade in trades:
            self.db.record_platform_fee_amount(int(trade.id), calculate_platform_fee_from_trade(trade))

        tx_hash = poly.transfer_pusd(PLATFORM_FEE_RECIPIENT, fee_amount)
        for trade in trades:
            self.db.mark_platform_fee_collected(
                int(trade.id),
                calculate_platform_fee_from_trade(trade),
                tx_hash,
            )
        return {"applied": True, "amount": fee_amount, "txHash": tx_hash}

    @staticmethod
    def format_platform_fee_note(fee_result: dict[str, Any]) -> str:
        if not fee_result.get("applied"):
            return ""
        return (
            f"\nPlatform fee: {float(fee_result.get('amount', 0.0)):.6f} pUSD "
            f"(https://polygonscan.com/tx/{fee_result.get('txHash')})"
        )

    def handle_command(self, message: dict[str, Any], text: str):
        chat_id = int(message["chat"]["id"])
        from_user = message.get("from") or {}
        user_id = str(from_user.get("id"))
        # Protect against DB errors (e.g. missing MASTER_ENCRYPTION_KEY) when reading user
        user = None
        if user_id:
            try:
                user = self.db.get_user(user_id)
            except Exception as exc:
                print(f"[BOT DEBUG] get_user failed for {user_id}: {exc}")
        # ensure session exists for tracking whether /start was used
        session = self.session_for(user_id) if user_id else None
        command, arg = self.parse_command_parts(text)
        if command == "/whitelist":
            if not self.can_manage_whitelist(user_id):
                self.send_message(chat_id, "Only the whitelist admin can authorize Telegram IDs.")
                return
            target_id = arg.strip()
            if not re.fullmatch(r"\d+", target_id):
                self.send_message(chat_id, "Usage: /whitelist <telegram_user_id>")
                return
            self.db.whitelist_user(target_id)
            self.send_message(chat_id, f"Telegram user {target_id} has been whitelisted.")
            return
        if command == "/start":
            # Debug: log authorization state for troubleshooting
            try:
                auth = self.is_authorized_user(user_id)
                wh = False
                try:
                    wh = self.db.is_whitelisted(str(user_id))
                except Exception:
                    wh = False
                admin_flag = self.can_manage_whitelist(user_id)
                print(f"[BOT DEBUG] /start by {user_id}: authorized={auth} whitelisted={wh} admin={admin_flag}")
            except Exception as _:
                print(f"[BOT DEBUG] /start by {user_id}: authorization check failed")

            if not self.is_authorized_user(user_id):
                self.send_message(chat_id, self.whitelist_denied_message())
                return
            try:
                self.ensure_authorized_user_profile(user_id)
                user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, user, "welcome")
                self.send_message(chat_id, view["text"], view["keyboard"]) 
                # mark that this user has started the dashboard session
                if session is not None:
                    session["started"] = True
            except Exception as exc:
                print(f"[BOT ERROR] /start dashboard rendering failed for {user_id}: {exc}")
                import traceback
                traceback.print_exc()
                self.send_message(chat_id, f"Dashboard error: {str(exc)[:100]}\n\nTry /import to setup your wallet first.")
            return
        if not self.is_authorized_user(user_id):
            self.send_message(chat_id, self.whitelist_denied_message())
            return
        if command == "/help":
            self.send_message(chat_id, "Clime Help\n\nUse /start as the main dashboard for portfolio, claims, balances, reports, and setup.\n\nSetup\n/import\n/approve\n/check_wallets\n/fund_funder <amt>\n\nTrading Controls\n/start_trading\n/stop_trading\n/set_risk <%>\n/set_max <amt>\n/set_max_open <count>\n\nAccount\n/remove_wallet")
            return
        if command == "/stats":
            overall = self.db.get_overall_stats(user_id)
            paper_stats = self.db.get_paper_stats(user_id)
            if overall.total == 0 and paper_stats["total"] == 0:
                self.send_message(chat_id, "No trades recorded yet.")
            else:
                self.send_message(chat_id, self.build_stats_message(overall, paper_stats))
            return
        if command == "/daily":
            if user and user.paper_testing_active:
                paper_daily = self.db.get_paper_daily_stats(user_id, get_utc_date_key_with_offset(-1))
                paper_overall = self.db.get_paper_stats(user_id)
                self.send_message(chat_id, "No paper trades yet for today." if paper_daily.total == 0 else build_paper_report_message("Daily", paper_daily, paper_overall))
            else:
                daily = self.db.get_daily_stats(user_id, get_utc_date_key_with_offset(-1))
                overall = self.db.get_overall_stats(user_id)
                self.send_message(chat_id, "No live trades yet for today." if daily.total == 0 else build_real_report_message("Daily", daily, overall))
            return
        if command == "/weekly":
            end_date_key = get_utc_date_key_with_offset(-1)
            start_date_key = get_utc_date_key_with_offset(-7)
            if user and user.paper_testing_active:
                paper_weekly = self.db.get_paper_weekly_stats(user_id, start_date_key, end_date_key)
                paper_overall = self.db.get_paper_stats(user_id)
                self.send_message(chat_id, "No paper trades this week." if paper_weekly.total == 0 else build_paper_report_message("Weekly", paper_weekly, paper_overall))
            else:
                weekly = self.db.get_weekly_stats(user_id, start_date_key, end_date_key)
                overall = self.db.get_overall_stats(user_id)
                self.send_message(chat_id, "No live trades this week." if weekly.total == 0 else build_real_report_message("Weekly", weekly, overall))
            return
        if command == "/all_time":
            if user and user.paper_testing_active:
                paper_overall = self.db.get_paper_stats(user_id)
                self.send_message(chat_id, "No paper trades recorded yet." if paper_overall["total"] == 0 else build_paper_report_message("All-Time", paper_overall, paper_overall))
            else:
                overall = self.db.get_overall_stats(user_id)
                self.send_message(chat_id, "No live trades recorded yet." if overall.total == 0 else build_real_report_message("All-Time", overall, overall))
            return
        if command == "/import":
            session = self.session_for(user_id)
            session["step"] = "awaiting_pk"
            session["pending_private_key"] = ""
            session["pending_funder_address"] = ""
            self.send_message(chat_id, "Send your private key in this private chat.\nWarning: use a dedicated hot wallet only. Your message will be deleted after processing.")
            return
        if command == "/status":
            if not user:
                self.send_message(chat_id, "User data not found. Use /import first.")
            else:
                self.send_message(chat_id, self.build_status_message(user))
            return
        if command == "/balance":
            if not user:
                self.send_message(chat_id, "Use /import first.")
            elif not has_imported_wallet(user):
                self.send_message(chat_id, "No live wallet is attached. Import one to check onchain balance.")
            else:
                self.send_message(chat_id, self.build_balance_message(user))
            return
        if command == "/positions":
            try:
                view = self.render_dashboard_page(user_id, user, "main")
                self.send_message(chat_id, view["text"], view["keyboard"])
            except Exception as exc:
                print(f"[BOT ERROR] /positions dashboard rendering failed for {user_id}: {exc}")
                import traceback
                traceback.print_exc()
                self.send_message(chat_id, f"Positions error: {str(exc)[:100]}\n\nTry /import to setup your wallet first.")
            return
        if command == "/claimable":
            if not user:
                self.send_message(chat_id, "Use /import first.")
            else:
                claimable_trades = self.db.get_claimable_trades(user_id)
                self.send_message(chat_id, "No settled winning trades are waiting to be claimed." if not claimable_trades else self.build_claimable_message(claimable_trades), build_claimable_keyboard(claimable_trades, bool(user.auto_claim)) if claimable_trades else None)
            return
        for known_command, handler in {
            "/auto_claim_on": lambda: (self.db.update_auto_claim(user_id, True), self.send_message(chat_id, "Auto-claim enabled. Winning settled trades will be redeemed automatically when possible.")),
            "/auto_claim_off": lambda: (self.db.update_auto_claim(user_id, False), self.send_message(chat_id, "Auto-claim disabled. Use /claimable, /claim, or /claim_all to redeem winners manually.")),
            "/start_trading": lambda: self.send_message(chat_id, "Import a live wallet before enabling real auto-trading.") if (not user or not has_imported_wallet(user)) else (self.db.update_trading_status(user_id, True), self.send_message(chat_id, "Auto-trading enabled.")),
            "/stop_trading": lambda: (self.db.update_trading_status(user_id, False), self.send_message(chat_id, "Auto-trading disabled.")),
            "/remove_wallet": lambda: (self.db.clear_user_wallet(user_id), self.send_message(chat_id, "Live wallet credentials were removed. Your profile, settings, and paper-testing data were kept.")),
        }.items():
            if command == known_command:
                handler()
                return

        try:
            if command == "/set_risk":
                risk = float(arg)
                if risk <= 0 or risk > 100:
                    raise ValueError
                self.db.update_risk(user_id, risk)
                self.send_message(chat_id, f"Risk set to {risk}% per trade.")
                return
            if command == "/set_max":
                max_trade = float(arg)
                if max_trade <= 0:
                    raise ValueError
                self.db.update_max_trade(user_id, max_trade)
                self.send_message(chat_id, f"Max trade amount set to ${max_trade}.")
                return
            if command == "/set_max_open":
                max_open = int(arg)
                if max_open <= 0:
                    raise ValueError
                self.db.update_max_open_positions(user_id, max_open)
                self.send_message(chat_id, f"Maximum concurrent open positions set to {max_open}.")
                return
            if command == "/fund_funder":
                if not user:
                    self.send_message(chat_id, "Use /import first.")
                    return
                amount = float(arg)
                poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
                tx_hash = poly.transfer_pusd_to_funder(amount)
                self.send_message(chat_id, f"Signer-to-funder transfer submitted for {amount:.2f} pUSD.\nhttps://polygonscan.com/tx/{tx_hash}")
                return
            if command == "/check_wallets":
                if not user:
                    self.send_message(chat_id, "Use /import first.")
                    return
                self.send_message(chat_id, self.build_wallet_check_message(user))
                return
            if command == "/approve":
                if not user:
                    self.send_message(chat_id, "Use /import first.")
                    return
                try:
                    self.send_message(chat_id, "Sending approvals...")
                    poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
                    hashes = poly.approve_collateral()
                    links = "\n".join([f"Tx {index + 1}: https://polygonscan.com/tx/{hash_}" for index, hash_ in enumerate(hashes)])
                    
                    # Refresh balance/allowance after approval confirmation
                    allowance_str = "Allowance updated"
                    try:
                        import time
                        time.sleep(4)  # Wait for blockchain state to update
                        balance_data = poly.get_balance()
                        allowance_num = extract_allowance(balance_data)
                        allowance_str = f"Trading Allowance: {allowance_num / 1_000_000:.2f} pUSD"
                        print(f"[BOT] Post-approval balance refresh for {user_id}: {allowance_str}")
                    except Exception as refresh_err:
                        print(f"[BOT] Balance refresh after approval failed: {refresh_err}")
                    
                    self.send_message(chat_id, f"✅ Approvals confirmed.\n\n{links}\n\n{allowance_str}. Ready to trade!")
                except Exception as e:
                    error_msg = str(e)
                    print(f"[BOT] Approve command failed for {user_id}: {error_msg}")
                    self.send_message(chat_id, f"❌ Approval failed: {error_msg[:400]}\n\nCheck your wallet setup with /check_wallets")
                return
            if command == "/claim":
                if not user:
                    self.send_message(chat_id, "Use /import first.")
                    return
                claimable = self.db.get_claimable_trades_for_market(user_id, arg)
                if not claimable:
                    self.send_message(chat_id, "No settled winning trade is waiting to be claimed for that market.")
                    return
                trade = claimable[0]
                if not trade.condition_id:
                    self.send_message(chat_id, "This trade is missing a condition id, so the bot cannot redeem it automatically.")
                    return
                claim_receipt = self.claim_trade_by_id_for_user(user_id, user, int(trade.id))
                self.send_message(chat_id, f"Claim submitted: {claim_receipt}")
                return
            if command == "/claim_all":
                if not user:
                    self.send_message(chat_id, "Use /import first.")
                    return
                self.send_message(chat_id, self.claim_all_for_user(user_id, user))
                return
        except Exception as exc:
            fallback = {
                "/set_risk": "Please provide a percentage from 1-100. Example: /set_risk 5",
                "/set_max": "Please provide a valid amount. Example: /set_max 50",
                "/set_max_open": "Please provide a whole number greater than 0. Example: /set_max_open 10",
            }
            self.send_message(chat_id, fallback.get(command, f"{command[1:]} failed: {exc}"))

    def handle_callback(self, callback_query: dict[str, Any]):
        data = str(callback_query.get("data") or "")
        message = callback_query.get("message") or {}
        chat = (message.get("chat") or {})
        try:
            chat_id = int(chat.get("id")) if chat.get("id") is not None else None
        except Exception:
            chat_id = None
        try:
            message_id = int(message.get("message_id")) if message.get("message_id") is not None else None
        except Exception:
            message_id = None
        from_user = callback_query.get("from") or {}
        user_id = str(from_user.get("id") or "")
        session = self.session_for(user_id)
        user = self.db.get_user(user_id) if user_id else None
        action = data.split("positions:", 1)[1] if data.startswith("positions:") else ""

        def safe_answer(qid: str | None, text: str, show_alert: bool = False):
            try:
                if qid:
                    self.answer_callback(qid, text, show_alert)
            except Exception:
                # swallow errors from answering callbacks to avoid crashing the handler
                pass

        if not self.is_authorized_user(user_id):
            safe_answer(callback_query.get("id"), self.whitelist_denied_message(), True)
            return
        self.ensure_authorized_user_profile(user_id)
        user = self.db.get_user(user_id) if user_id else None
        try:
            if action in {"main", "refresh", "setup", "help", "welcome"}:
                view = self.render_dashboard_page(user_id, user, action)
                changed = False
                try:
                    if chat_id is not None and message_id is not None:
                        changed = self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    changed = False
                safe_answer(callback_query.get("id"), "Dashboard refreshed." if action == "refresh" and changed else "Dashboard updated." if changed else "Already up to date.")
                return
            if action == "import_start":
                session["step"] = "awaiting_pk"
                session["pending_private_key"] = ""
                session["pending_funder_address"] = ""
                view = self.render_dashboard_page(user_id, user, "setup", "Wallet import started. Send your private key in this chat.")
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                try:
                    if chat_id is not None:
                        self.send_message(chat_id, "Send your private key in this private chat.\nWarning: use a dedicated hot wallet only. Your message will be deleted after processing.")
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Import flow started.")
                return
            if action == "real_trade":
                if user and user.paper_testing_active:
                    self.db.update_paper_testing_status(user_id, False)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "setup", "Real Trade selected. Import a wallet, approve pUSD, and fund the trading wallet to continue.")
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Real trade selected.")
                return
            if action == "paper_trade":
                self.db.update_paper_testing_status(user_id, True)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "controls", "Paper Trade selected. Paper testing is now enabled.")
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Paper trade selected.")
                return
            if not user:
                view = self.render_dashboard_page(user_id, user, "setup", "No wallet profile found yet.")
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Open Setup to get started.")
                return
            if action in {"daily", "weekly", "all_time"}:
                try:
                    if action == "daily":
                        if user and user.paper_testing_active:
                            paper_daily = self.db.get_paper_daily_stats(user_id, get_utc_date_key_with_offset(-1))
                            paper_overall = self.db.get_paper_stats(user_id)
                            try:
                                if chat_id is not None:
                                    self.send_message(chat_id, "No paper trades yet for today." if paper_daily.total == 0 else build_paper_report_message("Daily", paper_daily, paper_overall))
                            except Exception:
                                pass
                        else:
                            daily = self.db.get_daily_stats(user_id, get_utc_date_key_with_offset(-1))
                            overall = self.db.get_overall_stats(user_id)
                            try:
                                if chat_id is not None:
                                    self.send_message(chat_id, "No live trades yet for today." if daily.total == 0 else build_real_report_message("Daily", daily, overall))
                            except Exception:
                                pass
                    elif action == "weekly":
                        end_date_key = get_utc_date_key_with_offset(-1)
                        start_date_key = get_utc_date_key_with_offset(-7)
                        if user and user.paper_testing_active:
                            paper_weekly = self.db.get_paper_weekly_stats(user_id, start_date_key, end_date_key)
                            paper_overall = self.db.get_paper_stats(user_id)
                            self.send_message(chat_id, "No paper trades this week." if paper_weekly.total == 0 else build_paper_report_message("Weekly", paper_weekly, paper_overall))
                        else:
                            weekly = self.db.get_weekly_stats(user_id, start_date_key, end_date_key)
                            overall = self.db.get_overall_stats(user_id)
                            self.send_message(chat_id, "No live trades this week." if weekly.total == 0 else build_real_report_message("Weekly", weekly, overall))
                    else:  # all_time
                        if user and user.paper_testing_active:
                            paper_overall = self.db.get_paper_stats(user_id)
                            self.send_message(chat_id, "No paper trades recorded yet." if paper_overall["total"] == 0 else build_paper_report_message("All-Time", paper_overall, paper_overall))
                        else:
                            overall = self.db.get_overall_stats(user_id)
                            self.send_message(chat_id, "No live trades recorded yet." if overall.total == 0 else build_real_report_message("All-Time", overall, overall))
                    safe_answer(callback_query.get("id"), f"{action.replace('_', '-').title()} report sent.")
                except Exception as exc:
                    print(f"[BOT] Report Error ({action}): {exc}")
                    safe_answer(callback_query.get("id"), f"{action.replace('_', '-').title()} report failed.", True)
                return
            if action in {"claimable", "balance", "status", "stats", "controls", "risk_settings", "wallet_check", "orders", "history", "reports", "report_daily", "report_weekly", "report_alltime"}:
                if action in {"balance", "wallet_check"} and not has_imported_wallet(user):
                    safe_answer(callback_query.get("id"), "Import a live wallet for that action.", True)
                    return
                view = self.render_dashboard_page(user_id, user, action)
                changed = False
                try:
                    if chat_id is not None and message_id is not None:
                        changed = self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    changed = False
                safe_answer(callback_query.get("id"), "Dashboard updated." if changed else "Already up to date.")
                return
            if action == "approve":
                try:
                    safe_answer(callback_query.get("id"), "Sending approvals...")
                    poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
                    hashes = poly.approve_collateral()
                    links = "\n".join([f"Tx {index + 1}: https://polygonscan.com/tx/{hash_}" for index, hash_ in enumerate(hashes)])
                    
                    # Refresh balance/allowance after approval confirmation
                    allowance_str = "Allowance updated"
                    try:
                        import time
                        time.sleep(4)  # Wait for blockchain state to update
                        balance_data = poly.get_balance()
                        allowance_num = extract_allowance(balance_data)
                        allowance_str = f"Trading Allowance: {allowance_num / 1_000_000:.2f} pUSD"
                        print(f"[BOT] Post-approval balance refresh for {user_id}: {allowance_str}")
                    except Exception as refresh_err:
                        print(f"[BOT] Balance refresh after approval failed: {refresh_err}")
                    
                    view = self.render_dashboard_page(user_id, user, "setup", f"✅ Approvals confirmed.\n{links}\n\n{allowance_str}. Ready to trade!")
                    changed = False
                    try:
                        if chat_id is not None and message_id is not None:
                            changed = self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                    except Exception:
                        changed = False
                    if not changed:
                        try:
                            if chat_id is not None:
                                self.send_message(chat_id, f"✅ Approvals confirmed.\n\n{links}\n\n{allowance_str}. Ready to trade!")
                        except Exception:
                            pass
                except Exception as e:
                    error_msg = str(e)
                    print(f"[BOT] Approve failed for {user_id}: {error_msg}")
                    try:
                        safe_answer(callback_query.get("id"), f"❌ Approve failed: {error_msg[:300]}", True)
                    except Exception:
                        pass
                return
            if action == "fund_prompt":
                session["step"] = "awaiting_fund_amount"
                view = self.render_dashboard_page(user_id, user, "setup", "Funding prompt opened. Send the pUSD amount to move.")
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                try:
                    if chat_id is not None:
                        self.send_message(chat_id, "Send the amount of pUSD to move into the trading wallet. Example: 25")
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Send funding amount.")
                return
            if action in {"risk_prompt", "max_prompt", "max_open_prompt"}:
                step_map = {"risk_prompt": "awaiting_set_risk", "max_prompt": "awaiting_set_max", "max_open_prompt": "awaiting_set_max_open"}
                prompt_map = {"risk_prompt": "Risk update opened. Send the new percentage.", "max_prompt": "Max trade update opened. Send the new amount.", "max_open_prompt": "Max open update opened. Send the new count."}
                send_map = {"risk_prompt": "Send the new risk percentage from 1-100. Example: 5", "max_prompt": "Send the new max trade amount. Example: 50", "max_open_prompt": "Send the new maximum open positions count. Example: 10"}
                session["step"] = step_map[action]
                view = self.render_dashboard_page(user_id, user, "risk_settings", prompt_map[action])
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                try:
                    if chat_id is not None:
                        self.send_message(chat_id, send_map[action])
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Input requested.")
                return
            if action in {"start_trading", "stop_trading"}:
                enabled = action == "start_trading"
                if enabled and not has_imported_wallet(user):
                    safe_answer(callback_query.get("id"), "Import a live wallet before enabling real trading.", True)
                    return
                self.db.update_trading_status(user_id, enabled)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "controls", "Auto-trading enabled." if enabled else "Auto-trading disabled.")
                try:
                    if chat_id is not None and message_id is not None:
                        self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                except Exception:
                    pass
                safe_answer(callback_query.get("id"), "Trading enabled." if enabled else "Trading disabled.")
                return
            if action in {"auto_claim_on", "auto_claim_off"}:
                enabled = action == "auto_claim_on"
                self.db.update_auto_claim(user_id, enabled)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "refresh", "Auto-claim enabled." if enabled else "Auto-claim disabled.")
                self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                self.answer_callback(callback_query["id"], "Auto-claim enabled." if enabled else "Auto-claim disabled.")
                return
            if action in {"paper_testing_on", "paper_testing_off"}:
                enabled = action == "paper_testing_on"
                self.db.update_paper_testing_status(user_id, enabled)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "controls", "Paper signal testing enabled." if enabled else "Paper signal testing disabled.")
                self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                self.answer_callback(callback_query["id"], "Paper testing enabled." if enabled else "Paper testing disabled.")
                return
            if action == "remove_wallet":
                if not has_imported_wallet(user):
                    view = self.render_dashboard_page(user_id, user, "setup", "No live wallet is attached yet. Import one to continue.")
                    try:
                        if chat_id is not None and message_id is not None:
                            self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                    except Exception:
                        pass
                    self.answer_callback(callback_query["id"], "No wallet to remove.")
                    return
                self.db.clear_user_wallet(user_id)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "setup", "Live wallet removed. Profile settings and paper-testing data were kept.")
                self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                self.answer_callback(callback_query["id"], "Wallet removed.")
                return
            if action == "claim_all":
                result = self.claim_all_for_user(user_id, user)
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "claimable", result)
                self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                self.answer_callback(callback_query["id"], "Claim all processed.")
                return
            if action.startswith("claim:"):
                tx_hash = self.claim_trade_by_id_for_user(user_id, user, int(action.split("claim:", 1)[1]))
                updated_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, updated_user, "claimable", f"Claim submitted: https://polygonscan.com/tx/{tx_hash}")
                self.edit_message_text(chat_id, message_id, view["text"], view["keyboard"])
                self.answer_callback(callback_query["id"], "Claim submitted.")
                return
            self.answer_callback(callback_query["id"], "Unknown action.")
        except Exception as exc:
            print(f"[BOT] Callback Error ({action}): {exc}")
            import traceback
            traceback.print_exc()
            self.answer_callback(callback_query["id"], "Action failed.", True)

    def handle_text_message(self, message: dict[str, Any]):
        text = str(message.get("text") or "").strip()
        chat_id = int(message["chat"]["id"])
        from_user = message.get("from") or {}
        user_id = str(from_user.get("id"))
        session = self.session_for(user_id)
        if text.startswith("/"):
            self.handle_command(message, text)
            return
        if not self.is_authorized_user(user_id):
            self.send_message(chat_id, self.whitelist_denied_message())
            return
        step = session.get("step") or ""
        if step == "awaiting_pk":
            try:
                self.delete_message(chat_id, int(message["message_id"]))
            except Exception as exc:
                print(f"[BOT] Could not delete sensitive import message for {user_id}: {exc}")
            session["pending_private_key"] = text
            session["step"] = "awaiting_funder"
            self.send_message(chat_id, "Now send your Polymarket displayed wallet address.\nIf your Polymarket account uses the same wallet as the signer, reply skip.")
            return
        if step == "awaiting_funder":
            funder_address = None if text.lower() == "skip" else text
            if funder_address and not re.fullmatch(r"0x[a-fA-F0-9]{40}", funder_address):
                self.send_message(chat_id, "That funder address does not look valid. Send a 0x... wallet address or reply skip.")
                return
            session["pending_funder_address"] = funder_address or ""
            session["step"] = "awaiting_signature_type"
            self.send_message(
                chat_id,
                "Reply with signature type:\n"
                "0 = EOA / same wallet\n"
                "1 = Polymarket proxy wallet\n"
                "2 = Polymarket Gnosis Safe\n"
                "Use your existing Polymarket.com wallet address as the funder for type 1 or 2.",
            )
            return
        if step == "awaiting_signature_type":
            if text not in {"0", "1", "2"}:
                self.send_message(chat_id, "Reply with 0, 1, or 2.")
                return
            try:
                signature_type = int(text)
                account_config = {"funderAddress": session.get("pending_funder_address") or None, "signatureType": signature_type}
                creds = self.crypto.derive_api_keys(session.get("pending_private_key") or "", account_config)
                self.db.save_user({"tg_id": user_id, "private_key": session.get("pending_private_key") or "", "api_key": creds["key"], "api_secret": creds["secret"], "api_passphrase": creds["passphrase"], "funder_address": account_config["funderAddress"], "signature_type": account_config["signatureType"]})
                self.send_message(chat_id, "Wallet imported successfully. Sensitive credentials were encrypted at rest.")
                saved_user = self.db.get_user(user_id)
                view = self.render_dashboard_page(user_id, saved_user, "setup", "Wallet import completed.")
                self.send_message(chat_id, view["text"], view["keyboard"])
            except Exception as exc:
                self.send_message(chat_id, "Wallet import is temporarily unavailable because MASTER_ENCRYPTION_KEY is not configured on the server." if "MASTER_ENCRYPTION_KEY" in str(exc) else "Wallet import failed. Please verify the private key and try again.")
            finally:
                session.update({"step": "", "pending_private_key": "", "pending_funder_address": ""})
            return
        if step == "awaiting_fund_amount":
            user = self.db.get_user(user_id)
            if not user:
                session["step"] = ""
                self.send_message(chat_id, "Use /import first.")
                return
            try:
                amount = float(text)
                poly = PolyMarketAPI({"key": user.api_key or "", "secret": user.api_secret or "", "passphrase": user.api_passphrase or ""}, user.private_key, resolve_user_polymarket_account_config(user))
                tx_hash = poly.transfer_pusd_to_funder(amount)
                self.send_message(chat_id, f"Signer-to-funder transfer submitted for {amount:.2f} pUSD.\nhttps://polygonscan.com/tx/{tx_hash}")
                session["step"] = ""
            except Exception as exc:
                self.send_message(chat_id, f"Funding transfer failed: {exc}")
                session["step"] = ""
            return
        if step == "awaiting_set_risk":
            try:
                risk = float(text)
                if risk <= 0 or risk > 100:
                    raise ValueError
                self.db.update_risk(user_id, risk)
                self.send_message(chat_id, f"Risk set to {risk}% per trade.")
                session["step"] = ""
            except Exception:
                self.send_message(chat_id, "Send a percentage from 1-100. Example: 5")
            return
        if step == "awaiting_set_max":
            try:
                max_trade = float(text)
                if max_trade <= 0:
                    raise ValueError
                self.db.update_max_trade(user_id, max_trade)
                self.send_message(chat_id, f"Max trade amount set to ${max_trade}.")
                session["step"] = ""
            except Exception:
                self.send_message(chat_id, "Send a valid max trade amount. Example: 50")
            return
        if step == "awaiting_set_max_open":
            try:
                max_open = int(text)
                if max_open <= 0:
                    raise ValueError
                self.db.update_max_open_positions(user_id, max_open)
                self.send_message(chat_id, f"Maximum concurrent open positions set to {max_open}.")
                session["step"] = ""
            except Exception:
                self.send_message(chat_id, "Send a whole number greater than 0. Example: 10")

    def handle_update(self, update: dict[str, Any]):
        if update.get("callback_query"):
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if message and "text" in message:
            self.handle_text_message(message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--once", action="store_true")
    args, _ = parser.parse_known_args()
    release_lock = acquire_process_lock("python-telegram-bot")
    if not release_lock:
        raise SystemExit(0)
    bot = TelegramPollingBot()
    if args.once:
        bot.run_once()
    else:
        bot.run()
