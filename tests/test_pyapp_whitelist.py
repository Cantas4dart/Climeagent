import unittest
from types import SimpleNamespace

from pyapp.bot import TelegramPollingBot, WHITELIST_ADMIN_ID, build_setup_keyboard


class BotWhitelistTests(unittest.TestCase):
    def build_bot(self):
        bot = TelegramPollingBot.__new__(TelegramPollingBot)
        bot.sessions = {}
        return bot

    def test_admin_can_whitelist_user(self):
        bot = self.build_bot()
        recorded = {"whitelisted": None, "messages": []}
        bot.db = SimpleNamespace(
            get_user=lambda tg_id: None,
            is_whitelisted=lambda tg_id: False,
            whitelist_user=lambda tg_id: recorded.__setitem__("whitelisted", tg_id),
        )
        bot.send_message = lambda chat_id, text, reply_markup=None: recorded["messages"].append((chat_id, text))

        bot.handle_command(
            {"chat": {"id": 1}, "from": {"id": int(WHITELIST_ADMIN_ID)}},
            "/whitelist 123456789",
        )

        self.assertEqual(recorded["whitelisted"], "123456789")
        self.assertIn("whitelisted", recorded["messages"][0][1].lower())

    def test_non_admin_cannot_whitelist_user(self):
        bot = self.build_bot()
        recorded = {"messages": [], "called": False}
        bot.db = SimpleNamespace(
            get_user=lambda tg_id: None,
            is_whitelisted=lambda tg_id: False,
            whitelist_user=lambda tg_id: recorded.__setitem__("called", True),
        )
        bot.send_message = lambda chat_id, text, reply_markup=None: recorded["messages"].append((chat_id, text))

        bot.handle_command(
            {"chat": {"id": 1}, "from": {"id": 999}},
            "/whitelist 123456789",
        )

        self.assertFalse(recorded["called"])
        self.assertIn("only the whitelist admin", recorded["messages"][0][1].lower())

    def test_unauthorized_user_is_blocked_before_start(self):
        bot = self.build_bot()
        recorded = {"messages": []}
        bot.db = SimpleNamespace(
            get_user=lambda tg_id: None,
            is_whitelisted=lambda tg_id: False,
        )
        bot.send_message = lambda chat_id, text, reply_markup=None: recorded["messages"].append((chat_id, text))
        bot.render_dashboard_page = lambda *args, **kwargs: self.fail("Unauthorized users should not reach dashboard rendering.")

        bot.handle_command(
            {"chat": {"id": 1}, "from": {"id": 999}},
            "/start",
        )

        self.assertIn("access denied", recorded["messages"][0][1].lower())

    def test_whitelisted_user_can_reach_start(self):
        bot = self.build_bot()
        recorded = {"messages": []}
        users = {}
        bot.db = SimpleNamespace(
            get_user=lambda tg_id: users.get(tg_id),
            is_whitelisted=lambda tg_id: tg_id == "123",
            ensure_user=lambda tg_id: users.setdefault(
                tg_id,
                SimpleNamespace(
                    tg_id=tg_id,
                    trading_active=0,
                    paper_testing_active=0,
                    auto_claim=1,
                    private_key=None,
                    api_key=None,
                    api_secret=None,
                    api_passphrase=None,
                    funder_address=None,
                    signature_type=None,
                ),
            ),
        )
        bot.send_message = lambda chat_id, text, reply_markup=None: recorded["messages"].append((chat_id, text, reply_markup))
        bot.render_dashboard_page = lambda user_id, user, page: {"text": "welcome", "keyboard": None}

        bot.handle_command(
            {"chat": {"id": 1}, "from": {"id": 123}},
            "/start",
        )

        self.assertEqual(recorded["messages"][0][1], "welcome")

    def test_whitelisted_user_callback_creates_profile_before_rendering(self):
        bot = self.build_bot()
        recorded = {"edited": [], "answered": []}
        users = {}
        bot.db = SimpleNamespace(
            get_user=lambda tg_id: users.get(tg_id),
            is_whitelisted=lambda tg_id: tg_id == "123",
            ensure_user=lambda tg_id: users.setdefault(
                tg_id,
                SimpleNamespace(
                    tg_id=tg_id,
                    trading_active=0,
                    paper_testing_active=0,
                    auto_claim=1,
                    private_key=None,
                    api_key=None,
                    api_secret=None,
                    api_passphrase=None,
                    funder_address=None,
                    signature_type=None,
                ),
            ),
        )
        bot.edit_message_text = lambda chat_id, message_id, text, reply_markup=None: recorded["edited"].append((chat_id, message_id, text)) or True
        bot.answer_callback = lambda callback_query_id, text, show_alert=False: recorded["answered"].append((callback_query_id, text, show_alert))
        bot.render_dashboard_page = lambda user_id, user, page: {"text": f"{page}:{user_id}:{user is not None}", "keyboard": None}

        bot.handle_callback(
            {
                "id": "cb1",
                "data": "positions:help",
                "from": {"id": 123},
                "message": {"chat": {"id": 1}, "message_id": 5},
            }
        )

        self.assertIn("123", users)
        self.assertTrue(recorded["edited"])
        self.assertEqual(recorded["edited"][0][2], "help:123:True")

    def test_admin_real_trade_callback_creates_profile_before_rendering(self):
        bot = self.build_bot()
        recorded = {"edited": [], "answered": [], "paper_updates": []}
        users = {}
        admin_id = WHITELIST_ADMIN_ID

        def ensure_user(tg_id):
            return users.setdefault(
                tg_id,
                SimpleNamespace(
                    tg_id=tg_id,
                    trading_active=0,
                    paper_testing_active=0,
                    auto_claim=1,
                    private_key=None,
                    api_key=None,
                    api_secret=None,
                    api_passphrase=None,
                    funder_address=None,
                    signature_type=None,
                ),
            )

        def update_paper_testing_status(tg_id, active):
            recorded["paper_updates"].append((tg_id, active))
            ensure_user(tg_id).paper_testing_active = 1 if active else 0

        bot.db = SimpleNamespace(
            get_user=lambda tg_id: users.get(tg_id),
            is_whitelisted=lambda tg_id: False,
            ensure_user=ensure_user,
            update_paper_testing_status=update_paper_testing_status,
        )
        bot.edit_message_text = lambda chat_id, message_id, text, reply_markup=None: recorded["edited"].append((chat_id, message_id, text)) or True
        bot.answer_callback = lambda callback_query_id, text, show_alert=False: recorded["answered"].append((callback_query_id, text, show_alert))
        bot.render_dashboard_page = lambda user_id, user, page, notice=None: {"text": f"{page}:{user_id}:{user is not None}:{notice}", "keyboard": None}

        bot.handle_callback(
            {
                "id": "cb-admin",
                "data": "positions:real_trade",
                "from": {"id": int(admin_id)},
                "message": {"chat": {"id": 1}, "message_id": 7},
            }
        )

        self.assertIn(admin_id, users)
        self.assertEqual(recorded["paper_updates"], [])
        self.assertTrue(recorded["edited"])
        self.assertEqual(
            recorded["edited"][0][2],
            f"setup:{admin_id}:True:Real Trade selected. Import a wallet, approve pUSD, and fund the trading wallet to continue.",
        )

    def test_help_command_includes_support_contact(self):
        bot = self.build_bot()
        recorded = {"messages": []}
        bot.db = SimpleNamespace(
            get_user=lambda tg_id: None,
            is_whitelisted=lambda tg_id: tg_id == "123",
        )
        bot.send_message = lambda chat_id, text, reply_markup=None: recorded["messages"].append((chat_id, text, reply_markup))

        bot.handle_command(
            {"chat": {"id": 1}, "from": {"id": 123}},
            "/help",
        )

        self.assertIn("@epsilon_dev1", recorded["messages"][0][1])


class SetupKeyboardTests(unittest.TestCase):
    def test_setup_keyboard_keeps_remove_wallet_label_without_wallet(self):
        keyboard = build_setup_keyboard(has_user=True, has_wallet=False)
        rows = keyboard["inline_keyboard"]
        labels = [button["text"] for row in rows for button in row]

        self.assertEqual(labels.count("📥 Import Wallet"), 1)
        self.assertIn("🗑️ Remove Wallet", labels)


if __name__ == "__main__":
    unittest.main()
