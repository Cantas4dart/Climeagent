import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .secrets import decrypt_secret, encrypt_secret, has_master_key, is_encrypted_secret


@dataclass
class User:
    id: int
    tg_id: str
    private_key: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    funder_address: str | None
    signature_type: int | None
    trading_active: int
    paper_testing_active: int
    risk_percent: float
    max_trade_amount: float
    auto_claim: int
    max_open_positions: int
    stop_loss_percent: float


@dataclass
class Trade:
    id: int
    market_id: str | None
    market_date: str | None
    condition_id: str | None
    tg_id: str
    side: str
    buy_price: float | None
    size: float | None
    remaining_size: float | None
    entry_model_prob: float | None
    entry_market_prob: float | None
    entry_confidence: float | None
    entry_spread: float | None
    entry_regime: str | None
    learning_features: str | None
    temperature_analysis_entry: str | None
    execution_status: str | None
    order_id: str | None
    position_closed: int
    exit_price: float | None
    exit_reason: str | None
    exited_at: str | None
    settled: int
    outcome: int | None
    pnl: float | None
    claimed: int
    claim_tx: str | None
    claimed_at: str | None
    platform_fee_amount: float | None
    platform_fee_collected: int
    platform_fee_tx: str | None
    platform_fee_collected_at: str | None
    feedback_exported_at: str | None
    timestamp: str


@dataclass
class PaperTrade:
    id: int
    market_id: str | None
    market_date: str | None
    condition_id: str | None
    tg_id: str
    side: str
    entry_price: float | None
    size: float | None
    entry_model_prob: float | None
    entry_market_prob: float | None
    entry_confidence: float | None
    entry_spread: float | None
    entry_regime: str | None
    learning_features: str | None
    temperature_analysis_entry: str | None
    settled: int
    outcome: int | None
    pnl: float | None
    settled_at: str | None
    alert_sent_at: str | None
    feedback_exported_at: str | None
    timestamp: str


@dataclass
class TradeStats:
    total: int
    settled: int
    wins: int
    losses: int
    pnl: float
    winRate: str


@dataclass
class DatedTradeStats(TradeStats):
    dateKey: str


@dataclass
class DateRangeTradeStats(TradeStats):
    startDateKey: str
    endDateKey: str


class DBManager:
    def __init__(self):
        self.db_path = Path(__file__).resolve().parent.parent / "data" / "users.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init()
        self.backfill_whitelisted_users()
        self.cleanup_legacy_unsigned_submissions()
        self.maybe_migrate_plaintext_secrets()

    def init(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              tg_id TEXT UNIQUE,
              private_key TEXT,
              api_key TEXT,
              api_secret TEXT,
              api_passphrase TEXT,
              funder_address TEXT,
              signature_type INTEGER,
              trading_active INTEGER DEFAULT 0,
              paper_testing_active INTEGER DEFAULT 0,
              risk_percent REAL DEFAULT 1.0,
              max_trade_amount REAL DEFAULT 10.0,
              auto_claim INTEGER DEFAULT 1,
              max_open_positions INTEGER DEFAULT 10,
              stop_loss_percent REAL DEFAULT 10.0
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whitelist (
              tg_id TEXT PRIMARY KEY,
              added_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
              id INTEGER PRIMARY KEY,
              market_id TEXT,
              market_date TEXT,
              condition_id TEXT,
              tg_id TEXT,
              side TEXT,
              buy_price REAL,
              size REAL,
              remaining_size REAL,
              entry_model_prob REAL,
              entry_market_prob REAL,
              entry_confidence REAL,
              entry_spread REAL,
              entry_regime TEXT,
              learning_features TEXT,
              temperature_analysis_entry TEXT,
              execution_status TEXT DEFAULT 'submitted',
              order_id TEXT,
              position_closed INTEGER DEFAULT 0,
              exit_price REAL,
              exit_reason TEXT,
              exited_at DATETIME,
              settled INTEGER DEFAULT 0,
              outcome INTEGER,
              pnl REAL,
              claimed INTEGER DEFAULT 0,
              claim_tx TEXT,
              claimed_at DATETIME,
              platform_fee_amount REAL,
              platform_fee_collected INTEGER DEFAULT 0,
              platform_fee_tx TEXT,
              platform_fee_collected_at DATETIME,
              feedback_exported_at DATETIME,
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
              id INTEGER PRIMARY KEY,
              market_id TEXT,
              market_date TEXT,
              condition_id TEXT,
              tg_id TEXT,
              side TEXT,
              entry_price REAL,
              size REAL,
              entry_model_prob REAL,
              entry_market_prob REAL,
              entry_confidence REAL,
              entry_spread REAL,
              entry_regime TEXT,
              learning_features TEXT,
              temperature_analysis_entry TEXT,
              settled INTEGER DEFAULT 0,
              outcome INTEGER,
              pnl REAL,
              settled_at DATETIME,
              alert_sent_at DATETIME,
              feedback_exported_at DATETIME,
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS temperature_settlement_analysis (
              trade_source TEXT NOT NULL,
              trade_id INTEGER NOT NULL,
              market_id TEXT,
              condition_id TEXT,
              market_date TEXT,
              city TEXT,
              country_code TEXT,
              timezone TEXT,
              station_id TEXT,
              station_name TEXT,
              station_url TEXT,
              target_type TEXT,
              target_value_low REAL,
              target_value_high REAL,
              forecast_data_json TEXT,
              entry_avg_forecast REAL,
              entry_model_prob REAL,
              entry_market_prob REAL,
              entry_confidence REAL,
              entry_spread REAL,
              entry_regime TEXT,
              entry_timestamp TEXT,
              settled_yes INTEGER,
              settled_at TEXT,
              actual_temperature REAL,
              actual_temperature_unit TEXT,
              actual_observed_at TEXT,
              actual_source TEXT,
              actual_source_status TEXT,
              forecast_error_avg REAL,
              forecast_error_by_source_json TEXT,
              rounded_settlement_value REAL,
              target_hit INTEGER,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (trade_source, trade_id)
            )
            """
        )
        for column, definition in [
            ("condition_id", "TEXT"),
            ("market_date", "TEXT"),
            ("remaining_size", "REAL"),
            ("entry_model_prob", "REAL"),
            ("entry_market_prob", "REAL"),
            ("entry_confidence", "REAL"),
            ("entry_spread", "REAL"),
            ("entry_regime", "TEXT"),
            ("learning_features", "TEXT"),
            ("temperature_analysis_entry", "TEXT"),
            ("execution_status", "TEXT DEFAULT 'submitted'"),
            ("order_id", "TEXT"),
            ("position_closed", "INTEGER DEFAULT 0"),
            ("exit_price", "REAL"),
            ("exit_reason", "TEXT"),
            ("exited_at", "DATETIME"),
            ("claimed", "INTEGER DEFAULT 0"),
            ("claim_tx", "TEXT"),
            ("claimed_at", "DATETIME"),
            ("platform_fee_amount", "REAL"),
            ("platform_fee_collected", "INTEGER DEFAULT 0"),
            ("platform_fee_tx", "TEXT"),
            ("platform_fee_collected_at", "DATETIME"),
            ("feedback_exported_at", "DATETIME"),
        ]:
            self.ensure_column("trades", column, definition)
        for column, definition in [("alert_sent_at", "DATETIME"), ("market_date", "TEXT"), ("temperature_analysis_entry", "TEXT")]:
            self.ensure_column("paper_trades", column, definition)
        for column, definition in [
            ("funder_address", "TEXT"),
            ("signature_type", "INTEGER"),
            ("paper_testing_active", "INTEGER DEFAULT 0"),
            ("auto_claim", "INTEGER DEFAULT 1"),
            ("max_open_positions", "INTEGER DEFAULT 10"),
            ("stop_loss_percent", "REAL DEFAULT 10.0"),
        ]:
            self.ensure_column("users", column, definition)
        self.ensure_unique_trade_per_market()
        self.ensure_unique_paper_trade_per_market()
        self.conn.commit()

    def whitelist_user(self, tg_id: str):
        self.conn.execute(
            """
            INSERT OR IGNORE INTO whitelist (tg_id)
            VALUES (?)
            """,
            (str(tg_id),),
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO users (tg_id)
            VALUES (?)
            """,
            (str(tg_id),),
        )
        self.conn.commit()

    def ensure_user(self, tg_id: str):
        self.conn.execute(
            """
            INSERT OR IGNORE INTO users (tg_id)
            VALUES (?)
            """,
            (str(tg_id),),
        )
        self.conn.commit()

    def backfill_whitelisted_users(self):
        self.conn.execute(
            """
            INSERT OR IGNORE INTO users (tg_id)
            SELECT tg_id
            FROM whitelist
            """
        )
        self.conn.commit()

    def is_whitelisted(self, tg_id: str) -> bool:
        row = self.conn.execute(
            "SELECT tg_id FROM whitelist WHERE tg_id = ? LIMIT 1",
            (str(tg_id),),
        ).fetchone()
        return row is not None

    def ensure_column(self, table_name: str, column_name: str, definition: str):
        columns = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(column["name"] == column_name for column in columns):
            return
        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def ensure_unique_trade_per_market(self):
        self.conn.execute(
            """
            DELETE FROM trades
            WHERE id NOT IN (
              SELECT MIN(id)
              FROM trades
              GROUP BY tg_id, market_id
            )
            """
        )
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_user_market
            ON trades (tg_id, market_id)
            """
        )

    def ensure_unique_paper_trade_per_market(self):
        self.conn.execute(
            """
            DELETE FROM paper_trades
            WHERE id NOT IN (
              SELECT MIN(id)
              FROM paper_trades
              GROUP BY tg_id, market_id
            )
            """
        )
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_unique_user_market
            ON paper_trades (tg_id, market_id)
            """
        )

    def cleanup_legacy_unsigned_submissions(self):
        result = self.conn.execute(
            """
            DELETE FROM trades
            WHERE execution_status = 'submitted'
              AND order_id IS NULL
            """
        )
        self.conn.commit()
        if result.rowcount and result.rowcount > 0:
            print(f"[DB] Removed {result.rowcount} legacy trade record(s) with no posted order ID.")

    def save_user(self, user: dict[str, Any]):
        if not has_master_key():
            raise ValueError("MASTER_ENCRYPTION_KEY is not configured.")
        self.conn.execute(
            """
            INSERT INTO users (
              tg_id, private_key, api_key, api_secret, api_passphrase, funder_address, signature_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
              private_key = excluded.private_key,
              api_key = excluded.api_key,
              api_secret = excluded.api_secret,
              api_passphrase = excluded.api_passphrase,
              funder_address = excluded.funder_address,
              signature_type = excluded.signature_type
            """,
            (
                user["tg_id"],
                encrypt_secret(user["private_key"]),
                encrypt_secret(user["api_key"]),
                encrypt_secret(user["api_secret"]),
                encrypt_secret(user["api_passphrase"]),
                user.get("funder_address"),
                user.get("signature_type"),
            ),
        )
        self.conn.commit()

    def _row_to_user(self, row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        record = dict(row)
        if not has_master_key() and any(is_encrypted_secret(record.get(k)) for k in ["private_key", "api_key", "api_secret", "api_passphrase"]):
            raise ValueError("MASTER_ENCRYPTION_KEY is required to unlock stored wallet credentials.")
        for key in ["private_key", "api_key", "api_secret", "api_passphrase"]:
            record[key] = decrypt_secret(record[key]) if record.get(key) else None
        return User(**record)

    def get_user(self, tg_id: str) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        return self._row_to_user(row)

    def get_active_users(self) -> list[User]:
        rows = self.conn.execute(
            """
            SELECT * FROM users
            WHERE trading_active = 1
              AND private_key IS NOT NULL
              AND api_key IS NOT NULL
              AND api_secret IS NOT NULL
              AND api_passphrase IS NOT NULL
            """
        ).fetchall()
        return [self._row_to_user(row) for row in rows if row is not None]

    def get_paper_testing_users(self) -> list[User]:
        rows = self.conn.execute("SELECT * FROM users WHERE paper_testing_active = 1").fetchall()
        users = []
        for row in rows:
            record = dict(row)
            record.update({"private_key": None, "api_key": None, "api_secret": None, "api_passphrase": None})
            users.append(User(**record))
        return users

    def update_trading_status(self, tg_id: str, active: bool):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET trading_active = ? WHERE tg_id = ?", (1 if active else 0, tg_id))
        self.conn.commit()

    def update_paper_testing_status(self, tg_id: str, active: bool):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET paper_testing_active = ? WHERE tg_id = ?", (1 if active else 0, tg_id))
        self.conn.commit()

    def update_risk(self, tg_id: str, risk: float):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET risk_percent = ? WHERE tg_id = ?", (risk, tg_id))
        self.conn.commit()

    def update_max_trade(self, tg_id: str, max_trade: float):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET max_trade_amount = ? WHERE tg_id = ?", (max_trade, tg_id))
        self.conn.commit()

    def update_auto_claim(self, tg_id: str, auto_claim: bool):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET auto_claim = ? WHERE tg_id = ?", (1 if auto_claim else 0, tg_id))
        self.conn.commit()

    def update_max_open_positions(self, tg_id: str, max_open_positions: int):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET max_open_positions = ? WHERE tg_id = ?", (max_open_positions, tg_id))
        self.conn.commit()

    def update_stop_loss_percent(self, tg_id: str, stop_loss_percent: float):
        self.ensure_user(tg_id)
        self.conn.execute("UPDATE users SET stop_loss_percent = ? WHERE tg_id = ?", (stop_loss_percent, tg_id))
        self.conn.commit()

    def update_polymarket_account_config(self, tg_id: str, funder_address: str | None, signature_type: int | None):
        self.ensure_user(tg_id)
        self.conn.execute(
            """
            UPDATE users
            SET funder_address = ?, signature_type = ?
            WHERE tg_id = ?
            """,
            (funder_address, signature_type, tg_id),
        )
        self.conn.commit()

    def clear_user_wallet(self, tg_id: str):
        self.conn.execute(
            """
            UPDATE users
            SET private_key = NULL,
                api_key = NULL,
                api_secret = NULL,
                api_passphrase = NULL,
                funder_address = NULL,
                signature_type = NULL,
                trading_active = 0
            WHERE tg_id = ?
            """,
            (tg_id,),
        )
        self.conn.commit()

    def maybe_migrate_plaintext_secrets(self):
        if not has_master_key():
            row = self.conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            if row and row["count"] > 0:
                print("[DB] MASTER_ENCRYPTION_KEY is not set. Existing wallet records will not be migrated or unlocked yet.")
            return
        rows = self.conn.execute(
            "SELECT id, private_key, api_key, api_secret, api_passphrase FROM users"
        ).fetchall()
        for row in rows:
            if all(is_encrypted_secret(row[key]) for key in ["private_key", "api_key", "api_secret", "api_passphrase"]):
                continue
            self.conn.execute(
                """
                UPDATE users
                SET private_key = ?, api_key = ?, api_secret = ?, api_passphrase = ?
                WHERE id = ?
                """,
                (
                    encrypt_secret(decrypt_secret(row["private_key"] or "")),
                    encrypt_secret(decrypt_secret(row["api_key"] or "")),
                    encrypt_secret(decrypt_secret(row["api_secret"] or "")),
                    encrypt_secret(decrypt_secret(row["api_passphrase"] or "")),
                    row["id"],
                ),
            )
        self.conn.commit()

    def reserve_trade(self, trade: dict[str, Any]) -> int:
        result = self.conn.execute(
            """
            INSERT OR IGNORE INTO trades (
              market_id, market_date, condition_id, tg_id, side, buy_price, size, remaining_size,
              entry_model_prob, entry_market_prob, entry_confidence, entry_spread, entry_regime,
              learning_features, temperature_analysis_entry, execution_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'placing')
            """,
            (
                trade["market_id"],
                trade.get("market_date"),
                trade["condition_id"],
                trade["tg_id"],
                trade["side"],
                trade["buy_price"],
                trade["size"],
                trade["size"],
                trade.get("entry_model_prob"),
                trade.get("entry_market_prob"),
                trade.get("entry_confidence"),
                trade.get("entry_spread"),
                trade.get("entry_regime"),
                trade.get("learning_features"),
                trade.get("temperature_analysis_entry"),
            ),
        )
        self.conn.commit()
        return result.rowcount

    def import_external_trade(self, trade: dict[str, Any]) -> int:
        result = self.conn.execute(
            """
            INSERT OR IGNORE INTO trades (
              market_id, market_date, condition_id, tg_id, side, buy_price, size, remaining_size,
              entry_market_prob, execution_status, position_closed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'external_sync', 0)
            """,
            (
                trade["market_id"],
                trade.get("market_date"),
                trade.get("condition_id"),
                trade["tg_id"],
                trade["side"],
                trade.get("buy_price"),
                trade.get("size"),
                trade.get("remaining_size"),
                trade.get("entry_market_prob"),
            ),
        )
        self.conn.commit()
        return result.rowcount

    def mark_trade_submitted(self, tg_id: str, market_id: str, order_id: str | None):
        self.conn.execute(
            """
            UPDATE trades
            SET execution_status = 'submitted', order_id = ?
            WHERE tg_id = ? AND market_id = ?
            """,
            (order_id, tg_id, market_id),
        )
        self.conn.commit()

    def release_trade_reservation(self, tg_id: str, market_id: str):
        self.conn.execute(
            """
            DELETE FROM trades
            WHERE tg_id = ? AND market_id = ? AND execution_status = 'placing'
            """,
            (tg_id, market_id),
        )
        self.conn.commit()

    def has_traded(self, tg_id: str, market_id: str) -> bool:
        row = self.conn.execute("SELECT id FROM trades WHERE tg_id = ? AND market_id = ? LIMIT 1", (tg_id, market_id)).fetchone()
        return row is not None

    def reserve_paper_trade(self, trade: dict[str, Any]) -> int:
        result = self.conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades (
              market_id, market_date, condition_id, tg_id, side, entry_price, size, entry_model_prob,
              entry_market_prob, entry_confidence, entry_spread, entry_regime, learning_features,
              temperature_analysis_entry
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade["market_id"],
                trade.get("market_date"),
                trade["condition_id"],
                trade["tg_id"],
                trade["side"],
                trade["entry_price"],
                trade["size"],
                trade.get("entry_model_prob"),
                trade.get("entry_market_prob"),
                trade.get("entry_confidence"),
                trade.get("entry_spread"),
                trade.get("entry_regime"),
                trade.get("learning_features"),
                trade.get("temperature_analysis_entry"),
            ),
        )
        self.conn.commit()
        return result.rowcount

    def has_paper_trade(self, tg_id: str, market_id: str) -> bool:
        row = self.conn.execute("SELECT id FROM paper_trades WHERE tg_id = ? AND market_id = ? LIMIT 1", (tg_id, market_id)).fetchone()
        return row is not None

    def _rows_to(self, rows: list[sqlite3.Row], cls):
        return [cls(**dict(row)) for row in rows]

    def get_unsettled_trades(self) -> list[Trade]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE settled = 0 AND position_closed = 0 AND COALESCE(remaining_size, size) > 0
            ORDER BY timestamp ASC
            """
        ).fetchall()
        return self._rows_to(rows, Trade)

    def get_active_trades_for_monitoring(self) -> list[Trade]:
        return self.get_unsettled_trades()

    def get_unsettled_trade_count(self, tg_id: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM trades
            WHERE tg_id = ? AND settled = 0 AND position_closed = 0 AND COALESCE(remaining_size, size) > 0
            """,
            (tg_id,),
        ).fetchone()
        return int(row["count"]) if row else 0

    def get_trades_for_user(self, tg_id: str) -> list[Trade]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE tg_id = ? ORDER BY timestamp DESC",
            (tg_id,),
        ).fetchall()
        return self._rows_to(rows, Trade)

    def get_unsettled_paper_trades(self) -> list[PaperTrade]:
        rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE settled = 0
            ORDER BY timestamp ASC
            """
        ).fetchall()
        return self._rows_to(rows, PaperTrade)

    def get_stale_open_trades(self) -> list[Trade]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE settled = 0
              AND position_closed = 0
              AND execution_status = 'submitted'
              AND COALESCE(remaining_size, size) > 0
            ORDER BY timestamp ASC
            """
        ).fetchall()
        return self._rows_to(rows, Trade)

    def get_settled_trades_missing_feedback(self) -> list[Trade]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE settled = 1
              AND feedback_exported_at IS NULL
              AND learning_features IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        return self._rows_to(rows, Trade)

    def get_settled_paper_trades_missing_feedback(self) -> list[PaperTrade]:
        rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE settled = 1
              AND feedback_exported_at IS NULL
              AND learning_features IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        return self._rows_to(rows, PaperTrade)

    def get_settled_paper_trades_pending_alert(self) -> list[PaperTrade]:
        rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE settled = 1
              AND alert_sent_at IS NULL
            ORDER BY id ASC
            """
        ).fetchall()
        return self._rows_to(rows, PaperTrade)

    def get_claimable_trades(self, tg_id: str) -> list[Trade]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE tg_id = ?
              AND settled = 1
              AND outcome = 1
              AND claimed = 0
            ORDER BY timestamp ASC
            """,
            (tg_id,),
        ).fetchall()
        return self._rows_to(rows, Trade)

    def get_claimable_trades_for_market(self, tg_id: str, market_id: str) -> list[Trade]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE tg_id = ?
              AND market_id = ?
              AND settled = 1
              AND outcome = 1
              AND claimed = 0
            ORDER BY timestamp ASC
            """,
            (tg_id, market_id),
        ).fetchall()
        return self._rows_to(rows, Trade)

    def mark_claimed_by_condition(self, tg_id: str, condition_id: str, tx_hash: str | None):
        self.conn.execute(
            """
            UPDATE trades
            SET claimed = 1,
                claim_tx = ?,
                claimed_at = CURRENT_TIMESTAMP
            WHERE tg_id = ?
              AND condition_id = ?
              AND settled = 1
              AND outcome = 1
              AND claimed = 0
            """,
            (tx_hash, tg_id, condition_id),
        )
        self.conn.commit()

    def record_platform_fee_amount(self, trade_id: int, fee_amount: float):
        self.conn.execute(
            """
            UPDATE trades
            SET platform_fee_amount = ?
            WHERE id = ?
            """,
            (fee_amount, trade_id),
        )
        self.conn.commit()

    def mark_platform_fee_collected(self, trade_id: int, fee_amount: float, tx_hash: str | None):
        self.conn.execute(
            """
            UPDATE trades
            SET platform_fee_amount = ?,
                platform_fee_collected = 1,
                platform_fee_tx = ?,
                platform_fee_collected_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (fee_amount, tx_hash, trade_id),
        )
        self.conn.commit()

    def record_trade_exit(self, trade_id: int, remaining_size: float, exit_price: float | None, exit_reason: str, fully_closed: bool):
        self.conn.execute(
            """
            UPDATE trades
            SET remaining_size = ?, position_closed = ?, exit_price = ?, exit_reason = ?, exited_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (remaining_size, 1 if fully_closed else 0, exit_price, exit_reason, trade_id),
        )
        self.conn.commit()

    def mark_settled(self, trade_id: int, outcome: int, pnl: float):
        self.conn.execute(
            """
            UPDATE trades
            SET settled = 1,
                outcome = ?,
                pnl = ?,
                position_closed = 1,
                remaining_size = 0,
                exit_reason = COALESCE(exit_reason, 'settled'),
                exited_at = COALESCE(exited_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (outcome, pnl, trade_id),
        )
        self.conn.commit()

    def mark_paper_trade_settled(self, trade_id: int, outcome: int, pnl: float):
        self.conn.execute(
            """
            UPDATE paper_trades
            SET settled = 1, outcome = ?, pnl = ?, settled_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outcome, pnl, trade_id),
        )
        self.conn.commit()

    def mark_feedback_exported(self, trade_id: int):
        self.conn.execute(
            """
            UPDATE trades
            SET feedback_exported_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (trade_id,),
        )
        self.conn.commit()

    def mark_paper_feedback_exported(self, trade_id: int):
        self.conn.execute(
            """
            UPDATE paper_trades
            SET feedback_exported_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (trade_id,),
        )
        self.conn.commit()

    def mark_paper_alert_sent(self, trade_id: int):
        self.conn.execute(
            """
            UPDATE paper_trades
            SET alert_sent_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (trade_id,),
        )
        self.conn.commit()

    def upsert_temperature_settlement_analysis(self, record: dict[str, Any]):
        self.conn.execute(
            """
            INSERT INTO temperature_settlement_analysis (
              trade_source, trade_id, market_id, condition_id, market_date, city, country_code, timezone,
              station_id, station_name, station_url, target_type, target_value_low, target_value_high,
              forecast_data_json, entry_avg_forecast, entry_model_prob, entry_market_prob, entry_confidence,
              entry_spread, entry_regime, entry_timestamp, settled_yes, settled_at, actual_temperature,
              actual_temperature_unit, actual_observed_at, actual_source, actual_source_status,
              forecast_error_avg, forecast_error_by_source_json, rounded_settlement_value, target_hit,
              created_at, updated_at
            )
            VALUES (
              :trade_source, :trade_id, :market_id, :condition_id, :market_date, :city, :country_code, :timezone,
              :station_id, :station_name, :station_url, :target_type, :target_value_low, :target_value_high,
              :forecast_data_json, :entry_avg_forecast, :entry_model_prob, :entry_market_prob, :entry_confidence,
              :entry_spread, :entry_regime, :entry_timestamp, :settled_yes, :settled_at, :actual_temperature,
              :actual_temperature_unit, :actual_observed_at, :actual_source, :actual_source_status,
              :forecast_error_avg, :forecast_error_by_source_json, :rounded_settlement_value, :target_hit,
              COALESCE(:created_at, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
            )
            ON CONFLICT(trade_source, trade_id) DO UPDATE SET
              market_id = excluded.market_id,
              condition_id = excluded.condition_id,
              market_date = excluded.market_date,
              city = excluded.city,
              country_code = excluded.country_code,
              timezone = excluded.timezone,
              station_id = excluded.station_id,
              station_name = excluded.station_name,
              station_url = excluded.station_url,
              target_type = excluded.target_type,
              target_value_low = excluded.target_value_low,
              target_value_high = excluded.target_value_high,
              forecast_data_json = excluded.forecast_data_json,
              entry_avg_forecast = excluded.entry_avg_forecast,
              entry_model_prob = excluded.entry_model_prob,
              entry_market_prob = excluded.entry_market_prob,
              entry_confidence = excluded.entry_confidence,
              entry_spread = excluded.entry_spread,
              entry_regime = excluded.entry_regime,
              entry_timestamp = excluded.entry_timestamp,
              settled_yes = excluded.settled_yes,
              settled_at = excluded.settled_at,
              actual_temperature = excluded.actual_temperature,
              actual_temperature_unit = excluded.actual_temperature_unit,
              actual_observed_at = excluded.actual_observed_at,
              actual_source = excluded.actual_source,
              actual_source_status = excluded.actual_source_status,
              forecast_error_avg = excluded.forecast_error_avg,
              forecast_error_by_source_json = excluded.forecast_error_by_source_json,
              rounded_settlement_value = excluded.rounded_settlement_value,
              target_hit = excluded.target_hit,
              updated_at = CURRENT_TIMESTAMP
            """,
            record,
        )
        self.conn.commit()

    def _get_stats_for_trades(self, trades: list[Trade | PaperTrade]) -> TradeStats:
        settled = [trade for trade in trades if trade.settled == 1]
        wins = [trade for trade in settled if trade.outcome == 1]
        losses = [trade for trade in settled if trade.outcome == 0]
        pnl = sum(float(trade.pnl or 0) for trade in settled)
        win_rate = f"{((len(wins) / len(settled)) * 100):.1f}" if settled else "0.0"
        return TradeStats(
            total=len(trades),
            settled=len(settled),
            wins=len(wins),
            losses=len(losses),
            pnl=pnl,
            winRate=win_rate,
        )

    def get_daily_stats(self, tg_id: str, date_key: str) -> DatedTradeStats:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE tg_id = ? AND date(timestamp) = ?",
            (tg_id, date_key),
        ).fetchall()
        stats = self._get_stats_for_trades(self._rows_to(rows, Trade))
        return DatedTradeStats(dateKey=date_key, **stats.__dict__)

    def get_weekly_stats(self, tg_id: str, start_date_key: str, end_date_key: str) -> DateRangeTradeStats:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE tg_id = ? AND date(timestamp) >= ? AND date(timestamp) <= ?",
            (tg_id, start_date_key, end_date_key),
        ).fetchall()
        stats = self._get_stats_for_trades(self._rows_to(rows, Trade))
        return DateRangeTradeStats(startDateKey=start_date_key, endDateKey=end_date_key, **stats.__dict__)

    def get_overall_stats(self, tg_id: str) -> TradeStats:
        rows = self.conn.execute("SELECT * FROM trades WHERE tg_id = ?", (tg_id,)).fetchall()
        return self._get_stats_for_trades(self._rows_to(rows, Trade))

    def get_paper_daily_stats(self, tg_id: str, date_key: str) -> DatedTradeStats:
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE tg_id = ? AND date(timestamp) = ?",
            (tg_id, date_key),
        ).fetchall()
        stats = self._get_stats_for_trades(self._rows_to(rows, PaperTrade))
        return DatedTradeStats(dateKey=date_key, **stats.__dict__)

    def get_paper_weekly_stats(self, tg_id: str, start_date_key: str, end_date_key: str) -> DateRangeTradeStats:
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE tg_id = ? AND date(timestamp) >= ? AND date(timestamp) <= ?",
            (tg_id, start_date_key, end_date_key),
        ).fetchall()
        stats = self._get_stats_for_trades(self._rows_to(rows, PaperTrade))
        return DateRangeTradeStats(startDateKey=start_date_key, endDateKey=end_date_key, **stats.__dict__)

    def get_paper_stats(self, tg_id: str) -> dict[str, Any]:
        trades = self.get_paper_trades_for_user(tg_id)
        open_trades = [trade for trade in trades if trade.settled != 1]
        settled = [trade for trade in trades if trade.settled == 1]
        wins = [trade for trade in settled if trade.outcome == 1]
        losses = [trade for trade in settled if trade.outcome == 0]
        pnl = sum(float(trade.pnl or 0) for trade in settled)
        win_rate = f"{((len(wins) / len(settled)) * 100):.1f}" if settled else "0.0"
        return {
            "total": len(trades),
            "open": len(open_trades),
            "settled": len(settled),
            "wins": len(wins),
            "losses": len(losses),
            "pnl": pnl,
            "winRate": win_rate,
        }

    def get_paper_trades_for_user(self, tg_id: str) -> list[PaperTrade]:
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE tg_id = ? ORDER BY timestamp DESC",
            (tg_id,),
        ).fetchall()
        return self._rows_to(rows, PaperTrade)

    def get_all_active_user_ids(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT tg_id
            FROM users
            WHERE trading_active = 1 OR paper_testing_active = 1
            """
        ).fetchall()
        return [str(row["tg_id"]) for row in rows]
