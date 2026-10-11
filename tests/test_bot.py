"""Command, lifecycle and pump regression checks with hardware-only stubs."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import telegram_menu


def load_bot():
    """Use real aiogram/APScheduler while replacing unavailable Pi drivers."""
    hardware = {}
    for module_name, attributes in {
        "gpiozero": ["OutputDevice"],
        "picamera2": ["Picamera2"],
        "picamera2.encoders": ["H264Encoder"],
        "picamera2.outputs": ["FfmpegOutput"],
    }.items():
        module = ModuleType(module_name)
        for name in attributes:
            setattr(module, name, Mock(name=name))
        hardware[module_name] = module
    source = Path(__file__).resolve().parents[1] / "bot.py"
    spec = importlib.util.spec_from_file_location("planter_bot_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, hardware):
        spec.loader.exec_module(module)
    return module


bot = load_bot()


class BotCommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = SimpleNamespace(
            run_weekly_cycle=AsyncMock(return_value="План принят."),
            read_schedule=AsyncMock(),
        )
        self.message = Mock()
        self.message.chat.id = -456
        self.message.from_user.id = 123
        self.message.answer = AsyncMock()
        self.message.answer.return_value.edit_text = AsyncMock()
        self.refresh_menu = AsyncMock(return_value=True)
        for patcher in (
            patch.object(bot, "automation_controller", self.controller),
            patch.object(bot, "refresh_chat_commands", self.refresh_menu),
            patch.dict(os.environ, {"ALLOWED_USER_IDS": "123"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_get_schedule_forces_ai_and_replies_only_in_calling_chat(self):
        await bot.get_schedule(self.message)
        self.controller.run_weekly_cycle.assert_awaited_once_with(
            reply_chat_id=-456, notify_configured_chats=False, force_ai=True
        )
        self.controller.read_schedule.assert_not_awaited()
        self.message.answer.return_value.edit_text.assert_awaited_once_with("План принят.")

    async def test_read_schedule_only_reads_saved_plan_in_calling_chat(self):
        await bot.read_schedule(self.message)
        self.controller.read_schedule.assert_awaited_once_with(-456)
        self.controller.run_weekly_cycle.assert_not_awaited()

    async def test_both_schedule_commands_reject_unauthorized_user(self):
        self.message.from_user.id = 999
        for handler in (bot.get_schedule, bot.read_schedule):
            with self.subTest(handler=handler.__name__):
                self.message.answer.reset_mock()
                await handler(self.message)
                self.message.answer.assert_awaited_once_with("Access denied.")
        self.controller.read_schedule.assert_not_awaited()
        self.controller.run_weekly_cycle.assert_not_awaited()

    async def test_both_schedule_commands_handle_unavailable_automation(self):
        with patch.object(bot, "automation_controller", None):
            for handler in (bot.get_schedule, bot.read_schedule):
                with self.subTest(handler=handler.__name__):
                    self.message.answer.reset_mock()
                    await handler(self.message)
                    self.message.answer.assert_awaited_once_with("Automation scheduler is not ready.")

    async def test_help_lists_new_commands_and_removes_old_ones(self):
        await bot.help_command(self.message)
        self.refresh_menu.assert_awaited_once_with(self.message)
        text = self.message.answer.await_args.args[0]
        self.assertIn("/get_schedule", text)
        self.assertIn("/read_schedule", text)
        self.assertNotIn("/weekly_report", text)
        self.assertNotIn("/schedule", text)
        handlers = {handler.callback.__name__ for handler in bot.router.message.handlers}
        self.assertTrue({"get_schedule", "read_schedule"} <= handlers)
        self.assertTrue({"weekly_report", "schedule"}.isdisjoint(handlers))

    async def test_start_refreshes_menu_for_calling_chat(self):
        await bot.start(self.message)
        self.refresh_menu.assert_awaited_once_with(self.message)
        self.message.answer.assert_awaited_once()

    async def test_start_and_help_do_not_refresh_menu_for_unauthorized_user(self):
        self.message.from_user.id = 999
        for handler in (bot.start, bot.help_command):
            with self.subTest(handler=handler.__name__):
                self.message.answer.reset_mock()
                await handler(self.message)
                self.message.answer.assert_awaited_once_with("Access denied.")
        self.refresh_menu.assert_not_awaited()

    async def test_menu_failure_does_not_hide_help_or_claim_success(self):
        self.refresh_menu.return_value = False
        await bot.help_command(self.message)
        replies = [call.args[0] for call in self.message.answer.await_args_list]
        self.assertIn("/get_schedule", replies[0])
        self.assertIn("Не удалось обновить Menu", replies[1])
        self.controller.run_weekly_cycle.assert_not_awaited()


class BotHardwareTests(unittest.TestCase):
    def test_import_does_not_allocate_gpio_or_camera(self):
        imported = load_bot()
        imported.OutputDevice.assert_not_called()
        imported.Picamera2.assert_not_called()
        self.assertEqual(imported.pumps, {})

    def test_lemon_gets_two_pulses_around_full_watering_interval(self):
        pump = Mock()
        clock = SimpleNamespace(now=0.0)
        events = []
        pump.on.side_effect = lambda: events.append(("on", clock.now))
        pump.off.side_effect = lambda: events.append(("off", clock.now))

        def sleep(seconds):
            clock.now += seconds

        with patch.object(bot.time, "sleep", side_effect=sleep), patch.object(
            bot.time, "monotonic", side_effect=lambda: clock.now
        ), patch.object(bot, "LEMON_TOGGLE_PULSE_SECONDS", 0.2):
            bot._run_pump_cycle(pump, "pump_lemon", 10)
        self.assertEqual([event for event, _ in events], ["off", "on", "off", "off", "on", "off"])
        self.assertAlmostEqual(events[2][1], 0.2)
        self.assertAlmostEqual(events[4][1] - events[2][1], 10)
        self.assertAlmostEqual(events[-1][1], 10.4)

    def test_pepper_stays_on_for_full_interval(self):
        pump = Mock()
        clock = SimpleNamespace(now=0.0)
        events = []
        pump.on.side_effect = lambda: events.append(("on", clock.now))
        pump.off.side_effect = lambda: events.append(("off", clock.now))

        def sleep(seconds):
            clock.now += seconds

        with patch.object(bot.time, "sleep", side_effect=sleep), patch.object(
            bot.time, "monotonic", side_effect=lambda: clock.now
        ):
            bot._run_pump_cycle(pump, "pump_pepper", 10)
        self.assertEqual(events, [("on", 0.0), ("off", 10.0)])

    def test_camera_closes_even_if_finishing_recording_fails(self):
        camera = Mock()
        camera.stop_recording.side_effect = RuntimeError("encoder stop failed")
        pump = Mock()
        with patch.object(bot, "Picamera2", return_value=camera), patch.object(
            bot, "pumps", {"pump_lemon": pump}
        ), patch.object(bot, "_run_pump_cycle") as cycle, patch.object(bot.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "encoder stop failed"):
                bot.run_watering_with_video(Path("unused-video.mp4"), "pump_lemon", 10)
        cycle.assert_called_once_with(pump, "pump_lemon", 10)
        pump.off.assert_called_once_with()
        camera.close.assert_called_once_with()

    def test_camera_failure_before_recording_does_not_start_pump(self):
        camera = Mock()
        camera.start_recording.side_effect = RuntimeError("camera unavailable")
        with patch.object(bot, "Picamera2", return_value=camera), patch.object(
            bot, "pumps", {"pump_lemon": Mock()}
        ), patch.object(bot, "_run_pump_cycle") as cycle:
            with self.assertRaisesRegex(RuntimeError, "camera unavailable"):
                bot.run_watering_with_video(Path("unused-video.mp4"), "pump_lemon", 10)
        cycle.assert_not_called()
        camera.close.assert_called_once_with()

    def test_failed_photo_does_not_publish_partial_file(self):
        with tempfile.TemporaryDirectory(prefix="planter-photo-test-") as directory:
            photo = Path(directory) / "20261012_150000_000000.jpg"

            def fail_during_watermark(temporary):
                temporary.write_bytes(b"partially processed image")
                self.assertFalse(photo.exists())
                raise OSError("watermark failed")

            with patch.object(bot, "_capture_photo", side_effect=fail_during_watermark):
                with self.assertRaisesRegex(OSError, "watermark failed"):
                    bot.capture_photo(photo)
            self.assertEqual(list(Path(directory).iterdir()), [])


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cleanup_events = []
        self.telegram = SimpleNamespace(
            set_my_commands=AsyncMock(return_value=True),
            set_chat_menu_button=AsyncMock(return_value=True),
            session=SimpleNamespace(close=AsyncMock(
                side_effect=lambda: self.cleanup_events.append("session")
            )),
        )
        self.dispatcher = SimpleNamespace(
            include_router=Mock(), start_polling=AsyncMock()
        )
        self.automation = SimpleNamespace(start=AsyncMock(), shutdown=Mock())
        self.hardware = SimpleNamespace(close=AsyncMock(
            side_effect=lambda: self.cleanup_events.append("workers")
        ))
        self.initialize = Mock()
        self.close_pumps = Mock(side_effect=lambda: self.cleanup_events.append("gpio"))
        for patcher in (
            patch.dict(os.environ, {"BOT_TOKEN": "123456:test", "ALLOWED_USER_IDS": "123"}),
            patch.object(bot, "Bot", return_value=self.telegram),
            patch.object(bot, "Dispatcher", return_value=self.dispatcher),
            patch.object(bot, "PlantAutomation", return_value=self.automation),
            patch.object(bot, "hardware_tasks", self.hardware),
            patch.object(bot, "automation_controller", None),
            patch.object(bot, "initialize_pumps", self.initialize),
            patch.object(bot, "close_pumps", self.close_pumps),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_startup_registers_updated_menu_and_closes_resources_in_order(self):
        await bot.main()
        self.initialize.assert_called_once_with()
        self.automation.start.assert_awaited_once_with()
        commands = self.telegram.set_my_commands.await_args.args[0]
        self.assertEqual(
            {command.command for command in commands},
            {"start", "help", "take_photo", "water_lemon", "water_pepper", "get_schedule", "read_schedule"},
        )
        self.dispatcher.start_polling.assert_awaited_once_with(
            self.telegram, close_bot_session=False
        )
        self.automation.shutdown.assert_called_once_with()
        self.assertEqual(self.cleanup_events, ["workers", "gpio", "session"])
        self.assertIsNone(bot.automation_controller)

    async def test_invalid_automation_config_still_closes_resources(self):
        self.automation.start.side_effect = RuntimeError("invalid photo schedule")
        with self.assertRaisesRegex(RuntimeError, "invalid photo schedule"):
            await bot.main()
        self.dispatcher.start_polling.assert_not_awaited()
        self.telegram.set_my_commands.assert_not_awaited()
        self.assertEqual(self.cleanup_events, ["workers", "gpio", "session"])

    async def test_partial_gpio_initialization_failure_still_cleans_up(self):
        self.initialize.side_effect = RuntimeError("pepper GPIO busy")
        with self.assertRaisesRegex(RuntimeError, "pepper GPIO busy"):
            await bot.main()
        self.automation.start.assert_not_awaited()
        self.assertEqual(self.cleanup_events, ["workers", "gpio", "session"])

    async def test_menu_error_does_not_stop_polling(self):
        self.telegram.set_my_commands.side_effect = RuntimeError("temporary network error")
        with self.assertLogs(telegram_menu.logger, level="ERROR"):
            await bot.main()
        self.dispatcher.start_polling.assert_awaited_once()
        self.assertEqual(self.cleanup_events, ["workers", "gpio", "session"])

    async def test_shutdown_error_still_drains_hardware_before_gpio_close(self):
        self.automation.shutdown.side_effect = RuntimeError("scheduler shutdown error")
        with self.assertRaisesRegex(RuntimeError, "scheduler shutdown error"):
            await bot.main()
        self.assertEqual(self.cleanup_events, ["workers", "gpio", "session"])


if __name__ == "__main__":
    unittest.main()
