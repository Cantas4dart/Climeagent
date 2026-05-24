import json
import sqlite3
import unittest
from types import SimpleNamespace

from pyapp.db import DBManager
from pyapp.executor import TradeExecutor
from pyapp.settlement import SettlementMonitor
from pyapp.temperature_history import StationHistoryClient


class TemperatureAnalysisSchemaTests(unittest.TestCase):
    def make_db(self):
        db = DBManager.__new__(DBManager)
        db.conn = sqlite3.connect(":memory:")
        db.conn.row_factory = sqlite3.Row
        db.init()
        return db

    def test_schema_adds_temperature_analysis_storage(self):
        db = self.make_db()

        trade_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(trades)").fetchall()}
        paper_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        analysis_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(temperature_settlement_analysis)").fetchall()}

        self.assertIn("temperature_analysis_entry", trade_columns)
        self.assertIn("temperature_analysis_entry", paper_columns)
        self.assertIn("forecast_error_avg", analysis_columns)
        self.assertIn("actual_temperature", analysis_columns)

    def test_reserve_paper_trade_persists_temperature_analysis_entry(self):
        db = self.make_db()
        payload = json.dumps({"forecast_data": {"ecmwf": 24.1}})

        changes = db.reserve_paper_trade(
            {
                "market_id": "m1",
                "market_date": "2026-05-22",
                "condition_id": "c1",
                "tg_id": "u1",
                "side": "YES",
                "entry_price": 0.45,
                "size": 1,
                "entry_model_prob": 0.61,
                "entry_market_prob": 0.44,
                "entry_confidence": 0.83,
                "entry_spread": 0.07,
                "entry_regime": "near_peak",
                "learning_features": None,
                "temperature_analysis_entry": payload,
            }
        )

        row = db.conn.execute("SELECT temperature_analysis_entry FROM paper_trades WHERE market_id = 'm1'").fetchone()

        self.assertEqual(changes, 1)
        self.assertEqual(row["temperature_analysis_entry"], payload)

    def test_upsert_temperature_analysis_is_idempotent(self):
        db = self.make_db()
        base_record = {
            "trade_source": "paper",
            "trade_id": 7,
            "market_id": "m1",
            "condition_id": "c1",
            "market_date": "2026-05-22",
            "city": "Amsterdam",
            "country_code": "NL",
            "timezone": "Europe/Amsterdam",
            "station_id": "EHAM",
            "station_name": "Amsterdam Airport Schiphol Station",
            "station_url": "https://example.com",
            "target_type": "threshold",
            "target_value_low": 24.0,
            "target_value_high": 24.0,
            "forecast_data_json": "{\"ecmwf\":24.0}",
            "entry_avg_forecast": 24.0,
            "entry_model_prob": 0.6,
            "entry_market_prob": 0.4,
            "entry_confidence": 0.8,
            "entry_spread": 0.1,
            "entry_regime": "near_peak",
            "entry_timestamp": "2026-05-22T10:00:00Z",
            "settled_yes": 1,
            "settled_at": "2026-05-23T00:00:00Z",
            "actual_temperature": 25.0,
            "actual_temperature_unit": "celsius",
            "actual_observed_at": "2026-05-22T23:59:59",
            "actual_source": "open_meteo_archive_station_coords",
            "actual_source_status": "resolved",
            "forecast_error_avg": -1.0,
            "forecast_error_by_source_json": "{\"ecmwf\":-1.0}",
            "rounded_settlement_value": 25.0,
            "target_hit": 1,
            "created_at": None,
        }

        db.upsert_temperature_settlement_analysis(base_record)
        updated = dict(base_record)
        updated["actual_temperature"] = 26.0
        updated["forecast_error_avg"] = -2.0
        db.upsert_temperature_settlement_analysis(updated)

        count = db.conn.execute("SELECT COUNT(*) AS count FROM temperature_settlement_analysis").fetchone()["count"]
        row = db.conn.execute(
            "SELECT actual_temperature, forecast_error_avg FROM temperature_settlement_analysis WHERE trade_source = 'paper' AND trade_id = 7"
        ).fetchone()

        self.assertEqual(count, 1)
        self.assertEqual(row["actual_temperature"], 26.0)
        self.assertEqual(row["forecast_error_avg"], -2.0)


class TemperatureAnalysisEntryTests(unittest.TestCase):
    def test_executor_builds_small_temperature_analysis_entry(self):
        executor = TradeExecutor.__new__(TradeExecutor)

        payload_json = executor._temperature_analysis_entry_json(
            {
                "market_id": "m1",
                "condition_id": "c1",
                "market_date": "2026-05-22",
                "city": "Dallas",
                "country_code": "US",
                "timezone": "America/Chicago",
                "resolution_station_id": "KDAL",
                "resolution_station_name": "Dallas Love Field Station",
                "resolution_station_url": "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
                "location_lat": 32.84722,
                "location_lon": -96.85167,
                "temperature_unit": "fahrenheit",
                "target": {"type": "threshold", "direction": "above", "val": 90},
                "forecast_data": {"noaa": 91.0, "gfs": 89.5},
                "timestamp": "2026-05-22 11:00:00",
            }
        )
        payload = json.loads(payload_json)

        self.assertEqual(payload["station_id"], "KDAL")
        self.assertEqual(payload["forecast_data"]["noaa"], 91.0)
        self.assertEqual(payload["target"]["val"], 90)

    def test_executor_builds_partial_temperature_analysis_entry_without_forecast_data(self):
        executor = TradeExecutor.__new__(TradeExecutor)

        payload_json = executor._temperature_analysis_entry_json(
            {
                "market_id": "m2",
                "condition_id": "c2",
                "market_date": "2026-05-22",
                "city": "Helsinki",
                "country_code": "FI",
                "timezone": "Europe/Helsinki",
                "resolution_station_id": "EFHK",
                "resolution_station_name": "Helsinki Vantaa Airport Station",
                "resolution_station_url": "https://www.wunderground.com/history/daily/fi/vantaa/EFHK",
                "target": {"type": "exact", "val": 17},
                "timestamp": "2026-05-22 08:01:54",
            }
        )
        payload = json.loads(payload_json)

        self.assertEqual(payload["station_id"], "EFHK")
        self.assertEqual(payload["forecast_data"], {})
        self.assertEqual(payload["target"]["val"], 17)


class TemperatureAnalysisSettlementTests(unittest.TestCase):
    def test_threshold_and_range_hits_use_market_semantics(self):
        monitor = SettlementMonitor.__new__(SettlementMonitor)

        self.assertEqual(
            monitor._evaluate_target_hit({"type": "threshold", "direction": "above", "val": 65}, 65.0),
            1,
        )
        self.assertEqual(
            monitor._evaluate_target_hit({"type": "threshold", "direction": "below", "val": 65}, 65.0),
            1,
        )
        self.assertEqual(
            monitor._evaluate_target_hit({"type": "threshold", "direction": "below", "val": 65}, 65.9),
            1,
        )
        self.assertEqual(
            monitor._evaluate_target_hit({"type": "threshold", "direction": "below", "val": 65}, 65.95),
            0,
        )
        self.assertEqual(
            monitor._evaluate_target_hit({"type": "range", "low": 65, "high": 66}, 66.9),
            1,
        )
        self.assertEqual(
            monitor._evaluate_target_hit({"type": "range", "low": 65, "high": 66}, 66.95),
            0,
        )
        self.assertEqual(
            monitor._evaluate_target_hit({"type": "range", "low": 65, "high": 66}, 67.0),
            0,
        )

    def test_record_temperature_analysis_computes_errors_and_hit(self):
        captured = []
        monitor = SettlementMonitor.__new__(SettlementMonitor)
        monitor.db = SimpleNamespace(upsert_temperature_settlement_analysis=lambda record: captured.append(record))
        monitor.temperature_history = SimpleNamespace(
            fetch_daily_actual_temperature=lambda entry: {
                "actual_temperature": 26.0,
                "actual_temperature_unit": "celsius",
                "actual_observed_at": "2026-05-22T23:59:59",
                "actual_source": "open_meteo_archive_station_coords",
                "actual_source_status": "resolved",
            }
        )

        trade = SimpleNamespace(
            id=1,
            market_id="m1",
            condition_id="c1",
            market_date="2026-05-22",
            entry_model_prob=0.61,
            entry_market_prob=0.44,
            entry_confidence=0.83,
            entry_spread=0.07,
            entry_regime="near_peak",
            timestamp="2026-05-22 11:00:00",
            temperature_analysis_entry=json.dumps(
                {
                    "market_id": "m1",
                    "condition_id": "c1",
                    "market_date": "2026-05-22",
                    "city": "Amsterdam",
                    "country_code": "NL",
                    "timezone": "Europe/Amsterdam",
                    "station_id": "EHAM",
                    "station_name": "Amsterdam Airport Schiphol Station",
                    "station_url": "https://example.com",
                    "temperature_unit": "celsius",
                    "target": {"type": "threshold", "direction": "above", "val": 24},
                    "forecast_data": {"ecmwf": 24.0, "gfs": 25.0},
                    "entry_timestamp": "2026-05-22T10:00:00Z",
                }
            ),
        )

        monitor.record_temperature_analysis("paper", trade, 1, "2026-05-23T00:00:00Z")
        record = captured[0]

        self.assertEqual(record["actual_temperature"], 26.0)
        self.assertEqual(record["target_hit"], 1)
        self.assertAlmostEqual(record["entry_avg_forecast"], 24.5)
        self.assertAlmostEqual(record["forecast_error_avg"], -1.5)
        self.assertEqual(record["station_id"], "EHAM")

    def test_record_temperature_analysis_marks_missing_entry_snapshot(self):
        captured = []
        monitor = SettlementMonitor.__new__(SettlementMonitor)
        monitor.db = SimpleNamespace(upsert_temperature_settlement_analysis=lambda record: captured.append(record))
        monitor.temperature_history = SimpleNamespace(fetch_daily_actual_temperature=lambda entry: None)

        trade = SimpleNamespace(
            id=2,
            market_id="m2",
            condition_id="c2",
            market_date="2026-05-22",
            entry_model_prob=0.52,
            entry_market_prob=0.48,
            entry_confidence=0.7,
            entry_spread=0.05,
            entry_regime="pre_peak",
            timestamp="2026-05-22 09:00:00",
            temperature_analysis_entry=None,
        )

        monitor.record_temperature_analysis("live", trade, 0, "2026-05-23T00:00:00Z")

        self.assertEqual(captured[0]["actual_source_status"], "missing_entry_snapshot")

    def test_record_temperature_analysis_reconstructs_snapshot_from_learning_features(self):
        captured = []
        monitor = SettlementMonitor.__new__(SettlementMonitor)
        monitor.db = SimpleNamespace(upsert_temperature_settlement_analysis=lambda record: captured.append(record))
        monitor.temperature_history = SimpleNamespace(
            fetch_daily_actual_temperature=lambda entry: {
                "actual_temperature": 17.0,
                "actual_temperature_unit": "celsius",
                "actual_observed_at": "2026-05-22T23:59:59",
                "actual_source": "wunderground_history",
                "actual_source_status": "resolved",
            }
        )

        trade = SimpleNamespace(
            id=3,
            market_id="m3",
            condition_id="c3",
            market_date="2026-05-22",
            entry_model_prob=0.55,
            entry_market_prob=0.24,
            entry_confidence=0.99,
            entry_spread=0.01,
            entry_regime="near_peak",
            timestamp="2026-05-22 08:01:54",
            temperature_analysis_entry=None,
            learning_features=json.dumps(
                {
                    "meta": {
                        "city": "Helsinki",
                        "country_code": "FI",
                        "timezone": "Europe/Helsinki",
                        "resolution_station_id": "EFHK",
                        "resolution_station_name": "Helsinki Vantaa Airport Station",
                        "resolution_station_url": "https://www.wunderground.com/history/daily/fi/vantaa/EFHK",
                        "location_lat": 60.31722,
                        "location_lon": 24.96333,
                        "temperature_unit": "celsius",
                    }
                }
            ),
        )

        monitor.record_temperature_analysis("paper", trade, 1, "2026-05-23T00:00:00Z")
        record = captured[0]

        self.assertEqual(record["city"], "Helsinki")
        self.assertEqual(record["station_id"], "EFHK")
        self.assertEqual(record["actual_temperature"], 17.0)
        self.assertEqual(record["actual_source_status"], "resolved")


class TemperatureHistorySafetyTests(unittest.TestCase):
    def test_wunderground_url_is_allowlisted(self):
        client = StationHistoryClient()
        self.assertTrue(client._is_allowed_station_url("https://www.wunderground.com/history/daily/us/tx/dallas/KDAL"))
        self.assertFalse(client._is_allowed_station_url("https://example.com/history/daily/us/tx/dallas/KDAL"))

    def test_wunderground_parse_requires_explicit_unit(self):
        client = StationHistoryClient()
        html_without_unit = '{"temperatureMax":{"value":25.0}}'
        self.assertIsNone(client._parse_wunderground_temperature(html_without_unit))

        html_with_unit = '{"temperatureUnit":"C","temperatureMax":{"value":25.0}}'
        parsed = client._parse_wunderground_temperature(html_with_unit)
        self.assertEqual(parsed["value"], 25.0)
        self.assertEqual(parsed["unit"], "celsius")


if __name__ == "__main__":
    unittest.main()
