import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pyapp.bot import TelegramPollingBot
from pyapp.platform_fee import PLATFORM_FEE_ADMIN_TG_ID


class PlatformFeeTests(unittest.TestCase):
    @patch("pyapp.bot.PolyMarketAPI")
    def test_claim_trade_collects_fee_for_non_admin(self, mock_poly_cls):
        trade = SimpleNamespace(id=1, condition_id="cond-1", pnl=12.0)
        db_calls = {"assessed": [], "collected": [], "claimed": None}

        bot = TelegramPollingBot.__new__(TelegramPollingBot)
        bot.db = SimpleNamespace(
            get_claimable_trades=lambda user_id: [trade],
            mark_claimed_by_condition=lambda user_id, condition_id, tx_hash: db_calls.__setitem__("claimed", (user_id, condition_id, tx_hash)),
            record_platform_fee_amount=lambda trade_id, fee_amount: db_calls["assessed"].append((trade_id, fee_amount)),
            mark_platform_fee_collected=lambda trade_id, fee_amount, tx_hash: db_calls["collected"].append((trade_id, fee_amount, tx_hash)),
        )

        poly = SimpleNamespace(
            get_signer_address=lambda: "0xwallet",
            get_positions=lambda address: [{"conditionId": "cond-1", "size": 1, "redeemable": True}],
            redeem_winnings=lambda condition_id: "claim-tx",
            transfer_pusd=lambda recipient, amount: "fee-tx",
        )
        mock_poly_cls.return_value = poly

        user = SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p", private_key="pk", funder_address=None, signature_type=None)
        receipt = bot.claim_trade_by_id_for_user("123", user, 1)

        self.assertEqual(db_calls["claimed"], ("123", "cond-1", "claim-tx"))
        self.assertEqual(db_calls["assessed"], [(1, 0.12)])
        self.assertEqual(db_calls["collected"], [(1, 0.12, "fee-tx")])
        self.assertIn("Platform fee: 0.120000 pUSD", receipt)

    @patch("pyapp.bot.PolyMarketAPI")
    def test_claim_trade_skips_fee_for_admin(self, mock_poly_cls):
        trade = SimpleNamespace(id=1, condition_id="cond-1", pnl=12.0)
        db_calls = {"assessed": [], "collected": []}

        bot = TelegramPollingBot.__new__(TelegramPollingBot)
        bot.db = SimpleNamespace(
            get_claimable_trades=lambda user_id: [trade],
            mark_claimed_by_condition=lambda user_id, condition_id, tx_hash: None,
            record_platform_fee_amount=lambda trade_id, fee_amount: db_calls["assessed"].append((trade_id, fee_amount)),
            mark_platform_fee_collected=lambda trade_id, fee_amount, tx_hash: db_calls["collected"].append((trade_id, fee_amount, tx_hash)),
        )

        poly = SimpleNamespace(
            get_signer_address=lambda: "0xwallet",
            get_positions=lambda address: [{"conditionId": "cond-1", "size": 1, "redeemable": True}],
            redeem_winnings=lambda condition_id: "claim-tx",
            transfer_pusd=lambda recipient, amount: self.fail("Admin should not pay platform fee."),
        )
        mock_poly_cls.return_value = poly

        user = SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p", private_key="pk", funder_address=None, signature_type=None)
        receipt = bot.claim_trade_by_id_for_user(PLATFORM_FEE_ADMIN_TG_ID, user, 1)

        self.assertEqual(db_calls["assessed"], [])
        self.assertEqual(db_calls["collected"], [])
        self.assertNotIn("Platform fee:", receipt)


if __name__ == "__main__":
    unittest.main()
