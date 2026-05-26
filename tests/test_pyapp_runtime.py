import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyapp.bot import TelegramPollingBot, relayer_mode_for_signature_type, signature_type_label
from pyapp.executor import TradeExecutor
from pyapp.main import _spawn, run_selected
from pyapp.polymarket import PolyMarketAPI, extract_allowance_amount
from pyapp.relayer import RelayClient
from pyapp.settlement import SettlementMonitor


class SpawnCommandTests(unittest.TestCase):
    @patch("pyapp.main.subprocess.Popen")
    def test_spawn_passes_once_as_cli_flag(self, mock_popen):
        _spawn("pyapp.bot", run_once=True)

        args = mock_popen.call_args.args[0]
        env = mock_popen.call_args.kwargs["env"]
        self.assertEqual(args[1:], ["-m", "pyapp.bot", "--once"])
        self.assertNotIn("BLOCKY_PYAPP_RUN_ONCE", env)


class RunSelectedTests(unittest.TestCase):
    @patch("pyapp.main._spawn")
    def test_run_selected_spawns_selected_components_with_once_flag(self, mock_spawn):
        processes = []

        class DummyProcess:
            def wait(self):
                return 0

            def poll(self):
                return 0

        def fake_spawn(module_name, run_once):
            processes.append((module_name, run_once))
            return DummyProcess()

        mock_spawn.side_effect = fake_spawn

        exit_code = run_selected(run_bot=True, run_executor=True, run_settlement=True, run_once=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            processes,
            [("pyapp.bot", True), ("pyapp.executor", True), ("pyapp.settlement", True)],
        )


class BotOffsetPersistenceTests(unittest.TestCase):
    def test_run_once_saves_offset_even_when_update_handler_fails(self):
        bot = TelegramPollingBot.__new__(TelegramPollingBot)
        bot.offset = 0
        bot.poll = lambda: [{"update_id": 10}, {"update_id": 11}]
        saved_offsets = []
        handled_updates = []

        def handle_update(update):
            handled_updates.append(update["update_id"])
            if update["update_id"] == 10:
                raise RuntimeError("boom")

        bot.handle_update = handle_update
        bot._save_offset = lambda: saved_offsets.append(bot.offset)

        output = io.StringIO()
        with redirect_stdout(output):
            bot.run_once()

        self.assertEqual(handled_updates, [10, 11])
        self.assertEqual(saved_offsets, [11, 12])
        self.assertEqual(bot.offset, 12)
        self.assertIn("Update handling failed", output.getvalue())

    def test_offset_files_are_scoped_per_bot_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = TelegramPollingBot.__new__(TelegramPollingBot)
            bot.token = "prod-token"
            bot.offset = 27
            prod_file = Path(tmpdir) / "prod-offset.txt"
            test_file = Path(tmpdir) / "test-offset.txt"

            bot._offset_file_path = lambda: prod_file
            bot._save_offset()

            other_bot = TelegramPollingBot.__new__(TelegramPollingBot)
            other_bot.token = "test-token"
            other_bot._offset_file_path = lambda: test_file

            self.assertEqual(prod_file.read_text(), "27")
            self.assertFalse(test_file.exists())
            self.assertEqual(other_bot._load_offset(), 0)


class BotDiagnosticsTests(unittest.TestCase):
    def test_signature_type_labels_include_safe_mode(self):
        self.assertEqual(signature_type_label(1), "1 POLY_PROXY")
        self.assertEqual(signature_type_label(2), "2 GNOSIS_SAFE")
        self.assertEqual(relayer_mode_for_signature_type(2), "SAFE")

    def test_diag_reports_stored_mode_without_live_wallet(self):
        bot = TelegramPollingBot.__new__(TelegramPollingBot)
        user = SimpleNamespace(
            tg_id="u1",
            private_key=None,
            api_key=None,
            api_secret=None,
            api_passphrase=None,
            funder_address="0x0000000000000000000000000000000000000008",
            signature_type=2,
            trading_active=1,
            paper_testing_active=0,
        )

        text = bot.build_diagnostics_message("u1", user)

        self.assertIn("2 GNOSIS_SAFE", text)
        self.assertIn("SAFE", text)
        self.assertIn("Wallet Imported", text)
        self.assertIn("NO", text)


class ExecutorLiveBehaviorTests(unittest.TestCase):
    def test_process_real_user_signals_places_live_order(self):
        executor = TradeExecutor.__new__(TradeExecutor)
        recorded = {"submitted": None, "reserved": None}

        executor.db = SimpleNamespace(
            get_unsettled_trade_count=lambda tg_id: 0,
            has_traded=lambda tg_id, market_id: False,
            reserve_trade=lambda trade: recorded.__setitem__("reserved", trade) or 1,
            mark_trade_submitted=lambda tg_id, market_id, order_id: recorded.__setitem__("submitted", (tg_id, market_id, order_id)),
            release_trade_reservation=lambda tg_id, market_id: self.fail("Reservation should not be released on success."),
        )
        executor.reserved_capital_by_user = {}
        executor.send_trade_alert = lambda *args, **kwargs: None

        poly = SimpleNamespace(
            get_balance=lambda: {"balance": "100000000", "allowance": "100000000"},
            get_market_by_id=lambda market_id: {"clobTokenIds": "[\"yes-token\", \"no-token\"]"},
            place_limit_order=lambda token_id, side, price, size: {"orderID": "ord-1", "status": "live"},
        )
        executor.build_poly_client = lambda user, account_config: poly

        user = SimpleNamespace(
            tg_id="u1",
            max_open_positions=3,
            risk_percent=10,
            max_trade_amount=50,
        )
        signals = [
            {
                "market_id": "m1",
                "question": "Will it rain?",
                "action": "BUY_YES",
                "mode": "standard",
                "confidence_score": 0.77,
                "market_price": 0.5,
                "entry_price": 0.5,
                "market_price_yes": 0.5,
                "market_price_no": 0.5,
                "condition_id": "cond-1",
            }
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            executor.process_real_user_signals(user, signals)

        text = output.getvalue()
        self.assertIn("New Signal for u1", text)
        self.assertIn("Trade order submitted and saved for u1", text)
        self.assertEqual(recorded["submitted"], ("u1", "m1", "ord-1"))
        self.assertEqual(recorded["reserved"]["side"], "YES")

    def test_executor_accepts_v2_allowances_map(self):
        executor = TradeExecutor.__new__(TradeExecutor)
        reserved = {"count": 0}

        executor.db = SimpleNamespace(
            get_unsettled_trade_count=lambda tg_id: 0,
            has_traded=lambda tg_id, market_id: False,
            reserve_trade=lambda trade: reserved.__setitem__("count", reserved["count"] + 1) or 1,
            mark_trade_submitted=lambda tg_id, market_id, order_id: None,
            release_trade_reservation=lambda tg_id, market_id: None,
        )
        executor.reserved_capital_by_user = {}
        executor.send_trade_alert = lambda *args, **kwargs: None

        poly = SimpleNamespace(
            get_balance=lambda: {
                "balance": "100000000",
                "allowances": {"0xE111180000d2663C0091e4f400237545B87B996B": "100000000"},
            },
            get_market_by_id=lambda market_id: {"clobTokenIds": "[\"yes-token\", \"no-token\"]"},
            place_limit_order=lambda token_id, side, price, size: {"orderID": "ord-2", "status": "live"},
        )
        executor.build_poly_client = lambda user, account_config: poly

        user = SimpleNamespace(
            tg_id="u1",
            max_open_positions=3,
            risk_percent=10,
            max_trade_amount=50,
        )
        signals = [
            {
                "market_id": "m1",
                "question": "Will it rain?",
                "action": "BUY_YES",
                "mode": "standard",
                "confidence_score": 0.77,
                "market_price": 0.5,
                "entry_price": 0.5,
                "market_price_yes": 0.5,
                "market_price_no": 0.5,
                "condition_id": "cond-1",
            }
        ]

        executor.process_real_user_signals(user, signals)

        self.assertEqual(reserved["count"], 1)


class SettlementLiveBehaviorTests(unittest.TestCase):
    def test_check_settlements_marks_live_trade_settled(self):
        monitor = SettlementMonitor.__new__(SettlementMonitor)
        settled = {"called": None}
        alerted = {"called": None}
        monitor.db = SimpleNamespace(
            get_unsettled_trades=lambda: [
                SimpleNamespace(
                    id=1,
                    market_id="m1",
                    condition_id="cond-1",
                    tg_id="u1",
                    side="YES",
                    size=2,
                    buy_price=0.4,
                )
            ],
            mark_settled=lambda trade_id, outcome, pnl: settled.__setitem__("called", (trade_id, outcome, pnl)),
        )
        monitor.repair_stale_open_trades = lambda: None
        monitor.check_paper_settlements = lambda: None
        exported = {"called": False}

        def mark_export():
            exported["called"] = True

        monitor.export_learning_feedback = mark_export
        monitor.fetch_market_snapshot = lambda poly, trade: {
            "closed": True,
            "outcomePrices": "[\"1\", \"0\"]",
            "question": "Will it rain?",
        }
        monitor.try_auto_claim = lambda trade: {"claimed": False, "reason": "auto-claim disabled for this user"}
        monitor.send_real_settlement_alert = lambda trade, question, status, pnl, roi, claim_message: alerted.__setitem__(
            "called", (trade.id, question, status, pnl, roi, claim_message)
        )

        output = io.StringIO()
        with redirect_stdout(output):
            monitor.check_settlements()

        text = output.getvalue()
        self.assertIn("Settlement pass started", text)
        self.assertIn("Checking 1 unsettled trades", text)
        self.assertTrue(exported["called"])
        self.assertEqual(settled["called"], (1, 1, 1.2))
        self.assertEqual(alerted["called"][2], "WIN")


class ProxyApprovalMigrationTests(unittest.TestCase):
    @patch("pyapp.polymarket.build_builder_config_from_env")
    @patch("pyapp.polymarket.RelayClient")
    def test_proxy_wallet_approval_uses_python_relayer_path(self, mock_relay_client_cls, mock_builder_config):
        mock_builder_config.return_value = object()

        wait_result = {"transactionHash": "0xtx"}
        response = SimpleNamespace(wait=lambda: wait_result)
        relay_client = SimpleNamespace(
            account=SimpleNamespace(address="0x0000000000000000000000000000000000000009"),
            _derive_safe=lambda address: "0x0000000000000000000000000000000000000010",
            get_deployed=lambda safe: True,
            execute_single=lambda to, data, metadata: response,
        )
        mock_relay_client_cls.return_value = relay_client

        poly = PolyMarketAPI(
            {"key": "k", "secret": "s", "passphrase": "p"},
            private_key="0x" + "11" * 32,
            options={"funderAddress": "0x0000000000000000000000000000000000000008", "signatureType": 1},
        )

        hashes = poly.approve_collateral()

        self.assertEqual(hashes, ["0xtx", "0xtx", "0xtx", "0xtx", "0xtx", "0xtx"])
        mock_relay_client_cls.assert_called_once()

    @patch("pyapp.polymarket.build_builder_config_from_env")
    @patch("pyapp.polymarket.RelayClient")
    def test_safe_signature_type_uses_safe_relayer_path(self, mock_relay_client_cls, mock_builder_config):
        mock_builder_config.return_value = object()

        wait_result = {"transactionHash": "0xtx"}
        response = SimpleNamespace(wait=lambda: wait_result)
        relay_client = SimpleNamespace(
            account=SimpleNamespace(address="0x0000000000000000000000000000000000000009"),
            _derive_safe=lambda address: "0x0000000000000000000000000000000000000010",
            get_deployed=lambda safe: True,
            execute_single=lambda to, data, metadata: response,
        )
        mock_relay_client_cls.return_value = relay_client

        poly = PolyMarketAPI(
            {"key": "k", "secret": "s", "passphrase": "p"},
            private_key="0x" + "11" * 32,
            options={"funderAddress": "0x0000000000000000000000000000000000000008", "signatureType": 2},
        )

        hashes = poly.approve_collateral()

        self.assertEqual(hashes, ["0xtx", "0xtx", "0xtx", "0xtx", "0xtx", "0xtx"])
        self.assertEqual(mock_relay_client_cls.call_args.kwargs["relay_tx_type"], "SAFE")

    @patch("pyapp.polymarket.build_builder_config_from_env")
    @patch("pyapp.polymarket.RelayClient")
    def test_proxy_signature_type_uses_proxy_relayer_path(self, mock_relay_client_cls, mock_builder_config):
        mock_builder_config.return_value = object()

        wait_result = {"transactionHash": "0xtx"}
        response = SimpleNamespace(wait=lambda: wait_result)
        relay_client = SimpleNamespace(
            account=SimpleNamespace(address="0x0000000000000000000000000000000000000009"),
            execute_single=lambda to, data, metadata: response,
        )
        mock_relay_client_cls.return_value = relay_client

        poly = PolyMarketAPI(
            {"key": "k", "secret": "s", "passphrase": "p"},
            private_key="0x" + "11" * 32,
            options={"funderAddress": "0x0000000000000000000000000000000000000008", "signatureType": 1},
        )

        hashes = poly.approve_collateral()

        self.assertEqual(hashes, ["0xtx", "0xtx", "0xtx", "0xtx", "0xtx", "0xtx"])
        self.assertEqual(mock_relay_client_cls.call_args.kwargs["relay_tx_type"], "PROXY")


class RelayClientHeaderTests(unittest.TestCase):
    def test_builder_auth_post_sets_json_headers(self):
        class DummyBuilderConfig:
            def generate_builder_headers(self, method, path, body):
                return SimpleNamespace(to_dict=lambda: {"POLY_BUILDER_API_KEY": "k"})

        client = RelayClient(
            relayer_url="https://relayer-v2.polymarket.com/",
            chain_id=137,
            private_key="0x" + "11" * 32,
            builder_config=DummyBuilderConfig(),
            relay_tx_type="PROXY",
        )

        headers = client._headers("POST", "/submit", "{}")

        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["POLY_BUILDER_API_KEY"], "k")

    def test_relayer_api_key_auth_is_supported(self):
        client = RelayClient(
            relayer_url="https://relayer-v2.polymarket.com/",
            chain_id=137,
            private_key="0x" + "11" * 32,
            builder_config=None,
            relay_tx_type="PROXY",
            relayer_api_key="rk",
            relayer_api_key_address="0x0000000000000000000000000000000000000009",
        )

        headers = client._headers("POST", "/submit", "{}")

        self.assertEqual(headers["RELAYER_API_KEY"], "rk")
        self.assertEqual(headers["RELAYER_API_KEY_ADDRESS"], "0x0000000000000000000000000000000000000009")
        self.assertEqual(headers["Content-Type"], "application/json")


class BalanceAllowanceParsingTests(unittest.TestCase):
    def test_extract_allowance_amount_reads_v2_exchange_allowance(self):
        amount = extract_allowance_amount(
            {
                "allowances": {
                    "0xE111180000d2663C0091e4f400237545B87B996B": "2500000",
                }
            }
        )

        self.assertEqual(amount, 2500000.0)


class DashboardSyncTests(unittest.TestCase):
    def test_sync_live_positions_imports_manual_position_into_trade_tracking(self):
        bot = TelegramPollingBot.__new__(TelegramPollingBot)
        imported = []
        tracked = set()
        bot.db = SimpleNamespace(
            has_traded=lambda tg_id, market_id: market_id in tracked,
            import_external_trade=lambda trade: imported.append(trade) or tracked.add(trade["market_id"]) or 1,
        )

        user = SimpleNamespace(
            paper_testing_active=0,
            private_key="0x" + "11" * 32,
            api_key="k",
            api_secret="s",
            api_passphrase="p",
            funder_address="0x0000000000000000000000000000000000000008",
            signature_type=2,
        )
        poly = SimpleNamespace(
            get_positions=lambda address: [
                {
                    "conditionId": "cond-1",
                    "size": 4,
                    "avgPrice": 0.41,
                    "outcome": "Yes",
                    "endDate": "2026-06-01T00:00:00Z",
                }
            ],
            get_market_by_condition_id=lambda condition_id: {"id": "market-1", "endDate": "2026-06-01T00:00:00Z"},
            get_signer_address=lambda: "0x0000000000000000000000000000000000000009",
        )

        count = bot.sync_live_positions_to_db("u1", user, poly, "0x0000000000000000000000000000000000000008")

        self.assertEqual(count, 1)
        self.assertEqual(imported[0]["market_id"], "market-1")
        self.assertEqual(imported[0]["side"], "YES")


if __name__ == "__main__":
    unittest.main()
