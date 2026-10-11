"""Regression checks for saved schedules and AI plan installation.

All I/O goes to temporary directories or a mocked Telegram client.  The real
APScheduler stays paused so these tests never operate a pump or camera.
"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import automation


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=automation.TZ)


class FrozenDatetime(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


def plan_at(now=NOW, *, event_id="new-plan", offset=timedelta(hours=1)):
    return {
        "schema_version": "1.0",
        "week_start": (now.date() - timedelta(days=now.weekday())).isoformat(),
        "timezone": automation.BOT_TIMEZONE,
        "watering": [
            {
                "id": event_id,
                "plant_id": "lemon",
                "pump_id": "pump_lemon",
                "start_at": (now + offset).isoformat(),
                "duration_seconds": 10,
            }
        ],
        "human_recommendations": "Проверьте влажность почвы.",
    }


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planter-automation-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {
            "DATA_DIR": self.root,
            "PLAN_FILE": self.root / "watering_plan.json",
            "PHOTOS_DIR": self.root / "photos",
            "REPORTS_DIR": self.root / "reports",
            "datetime": FrozenDatetime,
        }.items():
            patcher = patch.object(automation, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        FrozenDatetime.current = NOW
        self.bot = Mock()
        self.bot.send_message = AsyncMock()
        self.bot.send_message_draft = AsyncMock()
        self.bot.send_video = AsyncMock()
        with patch.object(automation, "_parse_chat_ids", return_value=[111, 222]):
            self.controller = automation.PlantAutomation(
                self.bot,
                asyncio.Lock(),
                asyncio.Lock(),
                Mock(),
                Mock(),
                {"pump_lemon", "pump_pepper"},
            )
        self.controller.scheduler.start(paused=True)
        self.photo = self.root / "20261011_150000_000000.jpg"
        self.photo.write_bytes(b"fake photo; the AI call is mocked")

    async def asyncTearDown(self):
        self.controller.shutdown()
        await asyncio.sleep(0)

    def watering_jobs(self):
        return {
            job.kwargs["event"]["id"]: job
            for job in self.controller.scheduler.get_jobs()
            if job.id.startswith("watering:")
        }

    def sent_messages(self):
        return [
            (
                call.kwargs.get("chat_id", call.args[0] if call.args else None),
                call.kwargs.get("text", call.args[1] if len(call.args) > 1 else ""),
            )
            for call in self.bot.send_message.await_args_list
        ]

    async def test_read_previous_week_only_sends_saved_plan_to_caller(self):
        old_now = NOW - timedelta(days=7)
        accepted = self.controller._validate_plan(plan_at(old_now), now=old_now)
        automation._write_json(automation.PLAN_FILE, accepted)
        saved_bytes = automation.PLAN_FILE.read_bytes()
        self.controller.apply_plan(plan_at(event_id="existing"), now=NOW)
        # Keep an active plan in memory while inspecting last week's saved plan.
        automation.PLAN_FILE.write_bytes(saved_bytes)
        with (
            patch.object(self.controller, "apply_plan") as apply,
            patch.object(automation, "request_weekly_plan", new_callable=AsyncMock) as ai,
            patch.object(automation, "_write_json") as write,
        ):
            await self.controller.read_schedule(333)
        apply.assert_not_called()
        ai.assert_not_awaited()
        write.assert_not_called()
        self.assertEqual(automation.PLAN_FILE.read_bytes(), saved_bytes)
        self.assertEqual(set(self.watering_jobs()), {"existing"})
        messages = self.sent_messages()
        self.assertTrue(messages)
        self.assertEqual({chat for chat, _ in messages}, {333})
        text = "\n".join(text for _, text in messages)
        self.assertIn(accepted["week_start"], text)
        self.assertIn(accepted["human_recommendations"], text)
        self.assertIn("время уже прошло", text)
        self.assertNotIn("запланировано", text)

    async def test_read_missing_schedule_does_not_create_a_plan_or_call_ai(self):
        with patch.object(automation, "request_weekly_plan", new_callable=AsyncMock) as ai:
            await self.controller.read_schedule(333)
        ai.assert_not_awaited()
        self.assertFalse(automation.PLAN_FILE.exists())
        self.assertEqual(self.watering_jobs(), {})
        self.assertEqual(len(self.sent_messages()), 1)
        self.assertEqual(self.sent_messages()[0][0], 333)
        self.assertTrue(self.sent_messages()[0][1])

    async def test_read_corrupt_schedule_reports_error_without_overwriting_it(self):
        corrupt_text = '{"watering":'
        automation.PLAN_FILE.write_text(corrupt_text, encoding="utf-8")
        with (
            patch.object(automation, "request_weekly_plan", new_callable=AsyncMock) as ai,
            self.assertLogs(automation.logger, level="ERROR"),
        ):
            await self.controller.read_schedule(333)
        ai.assert_not_awaited()
        self.assertEqual(automation.PLAN_FILE.read_text(encoding="utf-8"), corrupt_text)
        self.assertEqual(len(self.sent_messages()), 1)
        self.assertEqual(self.sent_messages()[0][0], 333)

    async def test_get_cycle_replaces_plan_and_replies_only_to_caller(self):
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        with (
            patch.object(automation, "_weekly_photo_paths", return_value=[self.photo]),
            patch.object(
                automation, "request_weekly_plan", new_callable=AsyncMock,
                return_value=plan_at(),
            ) as ai,
        ):
            await self.controller.run_weekly_cycle(
                reply_chat_id=333, notify_configured_chats=False, force_ai=True
            )
        ai.assert_awaited_once()
        self.assertTrue(ai.await_args.kwargs["force_api"])
        self.assertEqual(set(self.watering_jobs()), {"new-plan"})
        saved = json.loads(automation.PLAN_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["watering"][0]["id"], "new-plan")
        self.assertEqual({chat for chat, _ in self.sent_messages()}, {333})
        self.assertIn("Расписание полива", self.sent_messages()[0][1])
        self.assertIn("Рекомендации:", self.sent_messages()[0][1])

    async def test_slow_ai_does_not_install_event_that_passed_during_request(self):
        soon_plan = plan_at(offset=timedelta(seconds=30))

        async def slow_ai(*args, **kwargs):
            FrozenDatetime.current = NOW + timedelta(minutes=2)
            return soon_plan

        with (
            patch.object(automation, "_weekly_photo_paths", return_value=[self.photo]),
            patch.object(automation, "request_weekly_plan", side_effect=slow_ai),
        ):
            await self.controller.run_weekly_cycle(
                reply_chat_id=333, notify_configured_chats=False, force_ai=True
            )
        self.assertEqual(self.watering_jobs(), {})
        saved = json.loads(automation.PLAN_FILE.read_text(encoding="utf-8"))
        self.assertFalse(saved["watering"][0]["will_run"])

    async def test_ai_error_redacts_credentials_from_logs_and_notifications(self):
        secret = "test-provider-credential-not-for-telegram"
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        saved_bytes = automation.PLAN_FILE.read_bytes()
        with (
            patch.dict(automation.os.environ, {"YANDEX_AI_API_KEY": secret}),
            patch.object(automation, "_weekly_photo_paths", return_value=[self.photo]),
            patch.object(
                automation, "request_weekly_plan", new_callable=AsyncMock,
                side_effect=RuntimeError(f"Provider failed; api_key={secret}"),
            ),
            self.assertLogs(automation.logger, level="ERROR") as logs,
        ):
            result = await self.controller.run_weekly_cycle()
        outputs = [result, *logs.output, *(text for _, text in self.sent_messages())]
        self.assertTrue(outputs)
        for output in outputs:
            self.assertNotIn(secret, output)
            self.assertIn("[REDACTED]", output)
        self.assertEqual({chat for chat, _ in self.sent_messages()}, {111, 222})
        self.assertEqual(set(self.watering_jobs()), {"old-plan"})
        self.assertEqual(automation.PLAN_FILE.read_bytes(), saved_bytes)

    async def test_plan_write_failure_preserves_existing_schedule(self):
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        saved_bytes = automation.PLAN_FILE.read_bytes()
        revision = self.controller._plan_revision
        with patch.object(automation, "_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.controller.apply_plan(plan_at(), now=NOW)
        self.assertEqual(set(self.watering_jobs()), {"old-plan"})
        self.assertEqual(automation.PLAN_FILE.read_bytes(), saved_bytes)
        self.assertEqual(self.controller._plan_revision, revision)
        self.assertEqual(self.watering_jobs()["old-plan"].kwargs["revision"], revision)

    async def test_partial_scheduler_failure_restores_existing_schedule(self):
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        saved_bytes = automation.PLAN_FILE.read_bytes()
        revision = self.controller._plan_revision
        replacement = plan_at(event_id="first-new")
        second = copy.deepcopy(replacement["watering"][0])
        second["id"] = "second-new"
        replacement["watering"].append(second)
        original_add_job = self.controller.scheduler.add_job

        def add_job(*args, **kwargs):
            if kwargs.get("kwargs", {}).get("event", {}).get("id") == "second-new":
                raise RuntimeError("job store unavailable")
            return original_add_job(*args, **kwargs)

        with patch.object(self.controller.scheduler, "add_job", side_effect=add_job):
            with self.assertRaises(RuntimeError):
                self.controller.apply_plan(replacement, now=NOW)
        self.assertEqual(set(self.watering_jobs()), {"old-plan"})
        self.assertEqual(automation.PLAN_FILE.read_bytes(), saved_bytes)
        self.assertEqual(self.controller._plan_revision, revision)

    async def test_replacing_plan_leaves_recurring_automation_jobs_intact(self):
        self.controller.scheduler.add_job(
            self.controller.capture_daily_photo,
            "cron", hour=12, id="daily-photo-test",
        )
        self.controller.scheduler.add_job(
            self.controller.run_weekly_cycle,
            "cron", day_of_week="mon", hour=10, id="weekly-ai-report",
        )
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        self.controller.apply_plan(plan_at(), now=NOW)
        ids = {job.id for job in self.controller.scheduler.get_jobs()}
        self.assertIn("daily-photo-test", ids)
        self.assertIn("weekly-ai-report", ids)
        self.assertEqual(set(self.watering_jobs()), {"new-plan"})

    async def test_invalid_plan_preserves_existing_schedule(self):
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        saved_bytes = automation.PLAN_FILE.read_bytes()
        invalid = plan_at()
        invalid["watering"][0]["pump_id"] = "pump_pepper"
        with self.assertRaises(ValueError):
            self.controller.apply_plan(invalid, now=NOW)
        self.assertEqual(set(self.watering_jobs()), {"old-plan"})
        self.assertEqual(automation.PLAN_FILE.read_bytes(), saved_bytes)

    async def test_queued_old_watering_is_skipped_after_plan_replacement(self):
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        old_job = self.watering_jobs()["old-plan"]
        with (
            patch.object(automation.hardware_tasks, "run", new_callable=AsyncMock) as worker,
            patch.object(self.controller, "_send_watering_video", new_callable=AsyncMock) as video,
        ):
            async with self.controller.camera_lock:
                old_task = asyncio.create_task(
                    self.controller._run_scheduled_watering(**old_job.kwargs)
                )
                await asyncio.sleep(0)
                self.assertFalse(old_task.done())
                self.controller.apply_plan(plan_at(), now=NOW)
            await old_task
            worker.assert_not_awaited()
            video.assert_not_awaited()
            # Jobs from the accepted replacement must still operate normally.
            await self.controller._run_scheduled_watering(
                **self.watering_jobs()["new-plan"].kwargs
            )
            worker.assert_awaited_once()
            self.assertEqual(worker.await_args.args[2:], ("pump_lemon", 10.0))
            video.assert_awaited_once()

    async def test_start_keeps_hourly_photos_and_monday_report_schedule(self):
        # asyncSetUp already started the scheduler paused; retain that state
        # while exercising start()'s real job registration and CronTriggers.
        with (
            patch.dict(automation.os.environ, {}, clear=True),
            patch.object(self.controller.scheduler, "start"),
        ):
            await self.controller.start()
        photo_jobs = [
            job for job in self.controller.scheduler.get_jobs()
            if job.id.startswith("daily-photo-")
        ]
        self.assertEqual(len(photo_jobs), 13)
        first_morning = NOW.replace(hour=7)
        self.assertEqual(
            sorted(job.trigger.get_next_fire_time(None, first_morning) for job in photo_jobs),
            [NOW.replace(hour=hour) for hour in range(8, 21)],
        )
        for job in photo_jobs:
            first = job.trigger.get_next_fire_time(None, first_morning)
            self.assertEqual(
                job.trigger.get_next_fire_time(first, first),
                first + timedelta(days=1),
            )
        weekly = self.controller.scheduler.get_job("weekly-ai-report")
        self.assertEqual(weekly.trigger.get_next_fire_time(None, first_morning), NOW)
        self.assertEqual(weekly.trigger.get_next_fire_time(NOW, NOW), NOW + timedelta(days=7))

    async def test_non_numeric_or_non_finite_watering_durations_are_rejected(self):
        self.controller.apply_plan(plan_at(event_id="old-plan"), now=NOW)
        saved_bytes = automation.PLAN_FILE.read_bytes()
        revision = self.controller._plan_revision
        for duration in (True, False, "10", float("nan"), float("inf"), -float("inf")):
            with self.subTest(duration=duration):
                invalid = plan_at()
                invalid["watering"][0]["duration_seconds"] = duration
                with self.assertRaises(ValueError):
                    self.controller.apply_plan(invalid, now=NOW)
                self.assertEqual(set(self.watering_jobs()), {"old-plan"})
                self.assertEqual(automation.PLAN_FILE.read_bytes(), saved_bytes)
                self.assertEqual(self.controller._plan_revision, revision)

    async def test_long_telegram_messages_preserve_entire_plain_text(self):
        for text in (
            "а" * 3999 + "\n\nРекомендации:\n" + "б" * 6000 + "\n",
            "\n\n<b>Это обычный текст</b>\n" + "🌱" * 8500,
        ):
            with self.subTest(length=len(text)):
                self.bot.send_message.reset_mock()
                await self.controller._send_long_message(333, text)
                sent = self.sent_messages()
                self.assertGreater(len(sent), 1)
                self.assertEqual({chat for chat, _ in sent}, {333})
                self.assertEqual("".join(chunk for _, chunk in sent), text)
                self.assertTrue(all(0 < len(chunk) <= 4000 for _, chunk in sent))
                self.assertTrue(all(
                    call.kwargs.get("parse_mode") is None
                    for call in self.bot.send_message.await_args_list
                ))

    async def test_photo_selection_uses_configured_time_and_one_photo_per_day(self):
        current = NOW.replace(hour=12, minute=30, second=20)
        automation.PHOTOS_DIR.mkdir()

        def save(timestamp, suffix=""):
            path = automation.PHOTOS_DIR / f"{timestamp:%Y%m%d_%H%M%S_%f}{suffix}.jpg"
            path.touch()
            return path

        expected = []
        for age in range(8):
            captured = (current - timedelta(days=age)).replace(second=0)
            earliest = save(captured)
            save(captured, "_1")  # Migrated duplicate of the same timestamp.
            save(captured + timedelta(seconds=10))
            save(captured - timedelta(minutes=1))
            if age < 7:
                expected.append(earliest)
        save(current + timedelta(seconds=1))
        (automation.PHOTOS_DIR / "20269999_123000_000000.jpg").touch()
        (automation.PHOTOS_DIR / "unknown.jpg").touch()
        with patch.object(automation, "PICK_DAILY_PHOTO_TIME", "12:30"):
            selected = automation._weekly_photo_paths(current)
        self.assertEqual(selected, sorted(expected))
        self.assertEqual(len(selected), 7)

    async def test_photo_window_excludes_lower_boundary_and_future_photos(self):
        current = NOW.replace(hour=15)
        automation.PHOTOS_DIR.mkdir()

        def save(timestamp):
            path = automation.PHOTOS_DIR / f"{timestamp:%Y%m%d_%H%M%S_%f}.jpg"
            path.touch()
            return path

        lower_boundary = current - timedelta(days=7)
        save(lower_boundary)
        within = save(lower_boundary + timedelta(seconds=1))
        latest = save(current)
        save(current + timedelta(seconds=1))
        with patch.object(automation, "PICK_DAILY_PHOTO_TIME", "15:00"):
            self.assertEqual(automation._weekly_photo_paths(current), [within, latest])

    async def test_invalid_photo_selection_time_is_reported(self):
        for value in ("15:70", "25:00", "1:00", "bad", ""):
            with self.subTest(value=value), patch.object(automation, "PICK_DAILY_PHOTO_TIME", value):
                with self.assertRaisesRegex(RuntimeError, "PICK_DAILY_PHOTO_TIME"):
                    automation._weekly_photo_paths(NOW)


if __name__ == "__main__":
    unittest.main()
