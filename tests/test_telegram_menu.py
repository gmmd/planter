"""Telegram command scope synchronization without contacting Telegram."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

from aiogram import Bot
from aiogram.types import Chat, Message, User

import telegram_menu


COMMAND_NAMES = (
    "start", "help", "take_photo", "water_lemon", "water_pepper",
    "get_schedule", "read_schedule",
)
COMMON_SCOPES = (
    "default", "all_private_chats", "all_group_chats", "all_chat_administrators",
)


class TelegramMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=Bot)
        self.client.set_my_commands = AsyncMock(return_value=True)
        self.client.set_chat_menu_button = AsyncMock(return_value=True)
        self.client.delete_my_commands = AsyncMock(return_value=True)
        self.client.get_my_commands = AsyncMock(return_value=[])

    def message(self, chat_type="private", language_code="ru", *, sender=True):
        user = (
            User(id=123, is_bot=False, first_name="Gardener", language_code=language_code)
            if sender else None
        )
        return Message(
            message_id=1,
            date=datetime(2026, 10, 12, tzinfo=timezone.utc),
            chat=Chat(id=123 if chat_type == "private" else -456, type=chat_type),
            from_user=user,
            text="/help",
        ).as_(self.client)

    def assert_current_commands(self):
        for invocation in self.client.set_my_commands.await_args_list:
            commands = invocation.args[0] if invocation.args else invocation.kwargs["commands"]
            self.assertEqual(tuple(item.command for item in commands), COMMAND_NAMES)
            self.assertEqual(list(commands), list(telegram_menu.BOT_COMMANDS))
        self.client.delete_my_commands.assert_not_awaited()
        self.client.get_my_commands.assert_not_awaited()

    async def test_startup_overwrites_old_commands_in_every_common_scope_and_language(self):
        saved = {
            (scope, language): ("weekly_report", "schedule")
            for scope in COMMON_SCOPES
            for language in ("", "ru", "en")
        }

        async def replace(commands, *, scope, language_code):
            saved[(scope.type, language_code)] = tuple(item.command for item in commands)
            return True

        self.client.set_my_commands.side_effect = replace
        self.assertTrue(await telegram_menu.register_commands(self.client))
        self.assertEqual(self.client.set_my_commands.await_count, 12)
        self.assertEqual(len(saved), 12)
        self.assertTrue(all(commands == COMMAND_NAMES for commands in saved.values()))
        self.assert_current_commands()
        self.client.set_chat_menu_button.assert_awaited_once()
        button_call = self.client.set_chat_menu_button.await_args
        self.assertIsNone(button_call.kwargs.get("chat_id"))
        self.assertEqual(button_call.kwargs["menu_button"].type, "commands")

    async def test_failed_startup_target_does_not_skip_other_targets_or_button(self):
        async def fail_one(commands, *, scope, language_code):
            if scope.type == "default" and language_code == "ru":
                raise RuntimeError("one command scope failed")
            return True

        self.client.set_my_commands.side_effect = fail_one
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await telegram_menu.register_commands(self.client))
        self.assertEqual(self.client.set_my_commands.await_count, 12)
        self.client.set_chat_menu_button.assert_awaited_once()
        self.assert_current_commands()

    async def test_failed_default_button_is_reported_after_command_sync(self):
        self.client.set_chat_menu_button.side_effect = RuntimeError("button failed")
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await telegram_menu.register_commands(self.client))
        self.assertEqual(self.client.set_my_commands.await_count, 12)
        self.assert_current_commands()

    async def test_negative_api_acknowledgement_is_reported_as_failure(self):
        for failed_method in ("set_my_commands", "set_chat_menu_button"):
            with self.subTest(method=failed_method):
                for method in (self.client.set_my_commands, self.client.set_chat_menu_button):
                    method.reset_mock()
                    method.return_value = True
                getattr(self.client, failed_method).return_value = False
                with self.assertLogs(level="WARNING"):
                    self.assertFalse(await telegram_menu.register_commands(self.client))
                self.assertEqual(self.client.set_my_commands.await_count, 12)
                self.client.set_chat_menu_button.assert_awaited_once()
                self.assert_current_commands()

    async def test_private_refresh_replaces_actual_chat_language_override(self):
        stale = {
            ("chat", 123, ""): ("weekly_report", "schedule"),
            ("chat", 123, "ru"): ("weekly_report", "schedule"),
        }

        async def replace(commands, *, scope, language_code):
            stale[(scope.type, scope.chat_id, language_code)] = tuple(
                item.command for item in commands
            )
            return True

        self.client.set_my_commands.side_effect = replace
        self.assertTrue(await telegram_menu.refresh_chat_commands(self.message()))
        self.assertEqual(self.client.set_my_commands.await_count, 2)
        self.assertEqual(set(stale.values()), {COMMAND_NAMES})
        self.assert_current_commands()
        self.client.set_chat_menu_button.assert_awaited_once()
        button_call = self.client.set_chat_menu_button.await_args
        self.assertEqual(button_call.kwargs["chat_id"], 123)
        self.assertEqual(button_call.kwargs["menu_button"].type, "commands")

    async def test_refresh_normalizes_user_language_without_duplicate_default(self):
        for original, expected in (
            ("zh-Hans", {"", "zh"}),
            ("EN_us", {"", "en"}),
            ("ru", {"", "ru"}),
            (None, {""}),
            ("", {""}),
            ("eng", {""}),
            ("1x", {""}),
            ("éé", {""}),
        ):
            with self.subTest(language=original):
                self.client.set_my_commands.reset_mock()
                self.assertTrue(await telegram_menu.refresh_chat_commands(
                    self.message(language_code=original)
                ))
                calls = self.client.set_my_commands.await_args_list
                self.assertEqual({item.kwargs["language_code"] for item in calls}, expected)
                self.assertEqual(len(calls), len(expected))
                self.assert_current_commands()

    async def test_group_refresh_targets_calling_member_without_private_menu_button(self):
        for chat_type in ("group", "supergroup"):
            with self.subTest(chat_type=chat_type):
                self.client.set_my_commands.reset_mock()
                self.assertTrue(await telegram_menu.refresh_chat_commands(
                    self.message(chat_type=chat_type)
                ))
                self.assertEqual(self.client.set_my_commands.await_count, 2)
                for item in self.client.set_my_commands.await_args_list:
                    scope = item.kwargs["scope"]
                    self.assertEqual(scope.type, "chat_member")
                    self.assertEqual(scope.chat_id, -456)
                    self.assertEqual(scope.user_id, 123)
                self.client.set_chat_menu_button.assert_not_awaited()
                self.assert_current_commands()

    async def test_group_without_user_refreshes_chat_and_default_language_only(self):
        self.assertTrue(await telegram_menu.refresh_chat_commands(
            self.message(chat_type="supergroup", sender=False)
        ))
        self.client.set_my_commands.assert_awaited_once()
        invocation = self.client.set_my_commands.await_args
        self.assertEqual(invocation.kwargs["scope"].type, "chat")
        self.assertEqual(invocation.kwargs["scope"].chat_id, -456)
        self.assertEqual(invocation.kwargs["language_code"], "")
        self.client.set_chat_menu_button.assert_not_awaited()
        self.assert_current_commands()

    async def test_failed_private_scope_does_not_skip_language_or_menu_button(self):
        async def fail_default(commands, *, scope, language_code):
            if language_code == "":
                raise RuntimeError("default-language update failed")
            return True

        self.client.set_my_commands.side_effect = fail_default
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await telegram_menu.refresh_chat_commands(self.message()))
        self.assertEqual(self.client.set_my_commands.await_count, 2)
        self.client.set_chat_menu_button.assert_awaited_once()
        self.assert_current_commands()

    async def test_failed_private_button_does_not_prevent_command_updates(self):
        self.client.set_chat_menu_button.side_effect = RuntimeError("button failed")
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await telegram_menu.refresh_chat_commands(self.message()))
        self.assertEqual(self.client.set_my_commands.await_count, 2)
        self.assert_current_commands()


if __name__ == "__main__":
    unittest.main()
