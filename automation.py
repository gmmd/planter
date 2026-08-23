"""Daily image capture, weekly AI reporting, and watering-plan scheduling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from ai_client import NonJsonAIResponse, request_weekly_plan
from sensors import read_sensor_snapshot


logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data")))
PHOTOS_DIR = DATA_DIR / "photos"
REPORTS_DIR = DATA_DIR / "reports"
PLAN_FILE = DATA_DIR / "watering_plan.json"
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(BOT_TIMEZONE)
MAX_WATERING_SECONDS = float(os.getenv("MAX_WATERING_SECONDS", "30"))
MAX_WATERING_EVENTS = int(os.getenv("MAX_WATERING_EVENTS", "21"))
AI_PHOTO_LIMIT = min(int(os.getenv("AI_PHOTO_LIMIT", "16")), 16)
TELEGRAM_STREAM_STEP_CHARS = int(os.getenv("TELEGRAM_STREAM_STEP_CHARS", "400"))
TELEGRAM_STREAM_INTERVAL_SECONDS = float(
    os.getenv("TELEGRAM_STREAM_INTERVAL_SECONDS", "0.3")
)
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

CapturePhoto = Callable[[Path], None]
WaterWithVideo = Callable[[Path, str, float], None]
PLANT_PUMPS = {
    "lemon": "pump_lemon",
    "pepper": "pump_pepper",
}


def _parse_chat_ids() -> List[int]:
    raw = os.getenv("REPORT_CHAT_IDS", "").strip()
    if not raw:
        raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_photo_times() -> List[time]:
    raw = os.getenv("DAILY_PHOTO_TIMES", "10:30,12:30,15:00")
    result: List[time] = []
    for item in raw.split(","):
        try:
            hour_text, minute_text = item.strip().split(":", 1)
            result.append(time(hour=int(hour_text), minute=int(minute_text)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "DAILY_PHOTO_TIMES must contain comma-separated HH:MM values"
            ) from exc
    if len(result) != 3:
        raise RuntimeError("DAILY_PHOTO_TIMES must contain exactly three times")
    return result


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _latest_photo_paths(limit: int = AI_PHOTO_LIMIT) -> List[Path]:
    if limit < 1:
        raise RuntimeError("AI_PHOTO_LIMIT must be at least 1")
    photos = sorted(PHOTOS_DIR.glob("*.jpg"), key=lambda path: path.stat().st_mtime)
    return photos[-limit:]


def new_photo_path(timestamp: Optional[datetime] = None) -> Path:
    captured_at = timestamp or datetime.now(TZ)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    return PHOTOS_DIR / f"{captured_at:%Y%m%d_%H%M%S_%f}.jpg"


def _migrate_legacy_photo_layout() -> int:
    """Flatten photos previously stored under data/photos/YYYY-MM-DD/."""
    if not PHOTOS_DIR.exists():
        return 0
    moved = 0
    for directory in sorted(PHOTOS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        try:
            directory_date = date.fromisoformat(directory.name)
        except ValueError:
            continue
        for photo in sorted(directory.glob("*.jpg")):
            time_part = photo.stem if re.fullmatch(r"\d{6}", photo.stem) else "000000"
            base_name = f"{directory_date:%Y%m%d}_{time_part}_000000"
            destination = PHOTOS_DIR / f"{base_name}.jpg"
            suffix = 1
            while destination.exists():
                destination = PHOTOS_DIR / f"{base_name}_{suffix}.jpg"
                suffix += 1
            photo.replace(destination)
            moved += 1
        try:
            directory.rmdir()
        except OSError:
            logger.warning("Legacy photo directory is not empty: %s", directory)
    return moved


class PlantAutomation:
    def __init__(
        self,
        bot: Bot,
        camera_lock: asyncio.Lock,
        pump_lock: asyncio.Lock,
        capture_photo: CapturePhoto,
        water_with_video: WaterWithVideo,
        available_pumps: Set[str],
    ) -> None:
        self.bot = bot
        self.camera_lock = camera_lock
        self.pump_lock = pump_lock
        self.capture_photo = capture_photo
        self.water_with_video = water_with_video
        self.available_pumps = available_pumps
        self.chat_ids = _parse_chat_ids()
        self.weekly_cycle_lock = asyncio.Lock()
        self.scheduler = AsyncIOScheduler(timezone=TZ)

    async def start(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        migrated = _migrate_legacy_photo_layout()
        if migrated:
            logger.info("Moved %s legacy photos into %s", migrated, PHOTOS_DIR)
        for index, photo_time in enumerate(_parse_photo_times(), start=1):
            self.scheduler.add_job(
                self.capture_daily_photo,
                CronTrigger(
                    hour=photo_time.hour,
                    minute=photo_time.minute,
                    timezone=TZ,
                ),
                id=f"daily-photo-{index}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )
        self.scheduler.add_job(
            self.run_weekly_cycle,
            CronTrigger(day_of_week="mon", hour=10, minute=0, timezone=TZ),
            id="weekly-ai-report",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )
        self._restore_saved_plan()
        self.scheduler.start()
        logger.info("Automation scheduler started in timezone %s", BOT_TIMEZONE)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def _notify(self, text: str) -> None:
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(chat_id, text[:4096])
            except Exception:
                logger.exception("Could not notify Telegram chat %s", chat_id)

    async def capture_daily_photo(self) -> None:
        now = datetime.now(TZ)
        path = new_photo_path(now)
        try:
            async with self.camera_lock:
                await asyncio.to_thread(self.capture_photo, path)
            logger.info("Daily photo saved to %s", path)
        except Exception:
            logger.exception("Scheduled daily photo failed")
            await self._notify("Не удалось сделать плановую фотографию растения.")

    async def run_weekly_cycle(
        self,
        reply_chat_id: Optional[int] = None,
        notify_configured_chats: bool = True,
        force_ai: bool = False,
    ) -> str:
        if self.weekly_cycle_lock.locked():
            return "Недельный запрос уже выполняется. Дождитесь его завершения."
        async with self.weekly_cycle_lock:
            return await self._run_weekly_cycle(
                reply_chat_id=reply_chat_id,
                notify_configured_chats=notify_configured_chats,
                force_ai=force_ai,
            )

    async def _run_weekly_cycle(
        self,
        reply_chat_id: Optional[int],
        notify_configured_chats: bool,
        force_ai: bool,
    ) -> str:
        now = datetime.now(TZ)
        photos = _latest_photo_paths()
        if not photos:
            message = "Нет фотографий для отправки нейросети."
            if notify_configured_chats:
                await self._notify(message)
            return message

        sensors = read_sensor_snapshot()
        report: Dict[str, Any] = {
            "schema_version": "1.0",
            "report_generated_at": now.isoformat(),
            "timezone": BOT_TIMEZONE,
            "photo_selection": {
                "strategy": "latest",
                "limit": AI_PHOTO_LIMIT,
                "selected_count": len(photos),
            },
            "environment": {
                "temperature_c": sensors["temperature_c"],
                "sensor_status": sensors["temperature_sensor_status"],
            },
            "plants": sensors["plants"],
            "media": {
                "type": "image/jpeg",
                "photo_count": len(photos),
                "photos": [
                    {
                        "filename": photo.name,
                        "captured_at": datetime.fromtimestamp(
                            photo.stat().st_mtime, TZ
                        ).isoformat(),
                    }
                    for photo in photos
                ],
            },
        }
        request_path = REPORTS_DIR / f"latest_{now:%Y%m%d_%H%M%S}.request.json"
        await asyncio.to_thread(_write_json, request_path, report)

        try:
            plan = await request_weekly_plan(report, photos, force_api=force_ai)
        except NonJsonAIResponse as exc:
            targets = [reply_chat_id] if reply_chat_id is not None else self.chat_ids
            delivered = 0
            for chat_id in dict.fromkeys(targets):
                try:
                    await self._stream_ai_text(chat_id, exc.raw_text)
                except Exception:
                    logger.exception(
                        "Could not send non-JSON AI response to chat %s", chat_id
                    )
                else:
                    delivered += 1
            if not targets:
                logger.warning("AI returned non-JSON text, but no Telegram chat is set")
                return "Нейросеть вернула текст вместо JSON, но чат для ответа не задан."
            if not delivered:
                return "Нейросеть вернула текст вместо JSON, но отправить его не удалось."
            return "Нейросеть вернула текст вместо JSON; полный ответ отправлен в Telegram."
        except Exception as exc:
            logger.exception("Weekly AI request failed")
            if notify_configured_chats:
                await self._notify(f"Запрос к нейросети завершился ошибкой: {exc}")
            return f"Ошибка запроса к нейросети: {exc}"

        if plan is None:
            if notify_configured_chats:
                await self._notify(
                    "Недельный отчёт подготовлен, но интеграция с нейросетью пока "
                    f"не настроена. Файл: {request_path.name}"
                )
            return "Отчёт и фотографии готовы; интеграция с нейросетью пока не настроена."

        try:
            accepted = self.apply_plan(plan, now=now)
        except Exception as exc:
            logger.exception("AI returned an invalid watering plan")
            if notify_configured_chats:
                await self._notify(
                    f"Нейросеть вернула некорректный план полива: {exc}"
                )
            return f"Некорректный план полива: {exc}"
        recommendations = str(accepted.get("human_recommendations", "")).strip()
        if notify_configured_chats:
            if recommendations:
                await self._notify("Рекомендации на неделю:\n\n" + recommendations)
            await self._notify(
                f"Принято заданий полива на неделю: {len(accepted['watering'])}."
            )
        if reply_chat_id is not None:
            try:
                await self._send_long_message(reply_chat_id, self._format_plan(accepted))
            except Exception:
                logger.exception("Could not send AI schedule to chat %s", reply_chat_id)
                return "План принят, но не удалось отправить его текст в этот чат."
        return f"Недельный план принят: {len(accepted['watering'])} поливов."

    async def _send_long_message(self, chat_id: int, text: str) -> None:
        remaining = text
        while remaining:
            if len(remaining) <= 4000:
                chunk = remaining
                remaining = ""
            else:
                split_at = remaining.rfind("\n", 0, 4000)
                if split_at <= 0:
                    split_at = 4000
                chunk = remaining[:split_at]
                remaining = remaining[split_at:].lstrip("\n")
            await self.bot.send_message(chat_id, chunk)

    async def _stream_ai_text(self, chat_id: int, text: str) -> None:
        """Animate non-JSON AI text with sendMessageDraft, then persist it."""
        if TELEGRAM_STREAM_STEP_CHARS < 1:
            raise RuntimeError("TELEGRAM_STREAM_STEP_CHARS must be at least 1")
        if TELEGRAM_STREAM_INTERVAL_SECONDS < 0:
            raise RuntimeError("TELEGRAM_STREAM_INTERVAL_SECONDS cannot be negative")
        response_text = text if text else "(Нейросеть вернула пустой ответ)"
        remaining = response_text
        draft_available = True
        while remaining:
            final_chunk = remaining[:4000]
            remaining = remaining[4000:]
            if draft_available:
                draft_id = secrets.randbelow(2**63 - 1) + 1
                for end in range(
                    TELEGRAM_STREAM_STEP_CHARS,
                    len(final_chunk) + TELEGRAM_STREAM_STEP_CHARS,
                    TELEGRAM_STREAM_STEP_CHARS,
                ):
                    partial = final_chunk[: min(end, len(final_chunk))]
                    try:
                        await self.bot.send_message_draft(
                            chat_id=chat_id,
                            draft_id=draft_id,
                            text=partial,
                        )
                    except Exception:
                        logger.info(
                            "sendMessageDraft is unavailable for chat %s; using fallback",
                            chat_id,
                            exc_info=True,
                        )
                        draft_available = False
                        break
                    if len(partial) < len(final_chunk) or remaining:
                        await asyncio.sleep(TELEGRAM_STREAM_INTERVAL_SECONDS)
            # Drafts are ephemeral; a normal message makes the answer permanent.
            await self.bot.send_message(chat_id, final_chunk)

    @staticmethod
    def _format_plan(plan: Dict[str, Any]) -> str:
        plant_names = {"lemon": "лимон", "pepper": "перец"}
        lines = [
            f"Расписание полива на неделю с {plan['week_start']}:",
            "",
        ]
        events = plan["watering"]
        if events:
            for event in events:
                start_at = datetime.fromisoformat(event["start_at"]).astimezone(TZ)
                plant = plant_names.get(event["plant_id"], event["plant_id"])
                state = "запланировано" if event["will_run"] else "время уже прошло"
                lines.append(
                    f"• {start_at:%d.%m %H:%M} — {plant}, "
                    f"{event['duration_seconds']:g} сек. ({state})"
                )
        else:
            lines.append("Поливы не назначены.")

        recommendations = str(plan.get("human_recommendations", "")).strip()
        lines.extend(["", "Рекомендации:", recommendations or "Нет рекомендаций."])
        return "\n".join(lines)

    def _validate_plan(
        self, plan: Dict[str, Any], now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        current = now or datetime.now(TZ)
        if plan.get("schema_version") != "1.0":
            raise ValueError("Unsupported schema_version")
        if plan.get("timezone") != BOT_TIMEZONE:
            raise ValueError(f"Plan timezone must be {BOT_TIMEZONE}")
        try:
            week_start = date.fromisoformat(str(plan["week_start"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("week_start must use YYYY-MM-DD") from exc
        expected_week_start = current.date() - timedelta(days=current.weekday())
        if week_start != expected_week_start:
            raise ValueError(
                f"week_start must be the current Monday: {expected_week_start}"
            )
        watering = plan.get("watering")
        if not isinstance(watering, list):
            raise ValueError("watering must be an array")
        if len(watering) > MAX_WATERING_EVENTS:
            raise ValueError(
                f"watering must not contain more than {MAX_WATERING_EVENTS} events"
            )

        accepted_events: List[Dict[str, Any]] = []
        event_ids = set()
        for event in watering:
            if not isinstance(event, dict):
                raise ValueError("Each watering entry must be an object")
            event_id = str(event.get("id", ""))
            if not EVENT_ID_RE.fullmatch(event_id) or event_id in event_ids:
                raise ValueError(f"Invalid or duplicate watering id: {event_id!r}")
            event_ids.add(event_id)
            plant_id = str(event.get("plant_id", ""))
            pump_id = str(event.get("pump_id", ""))
            expected_pump = PLANT_PUMPS.get(plant_id)
            if expected_pump is None or pump_id != expected_pump:
                raise ValueError(
                    f"Invalid plant_id/pump_id mapping in event {event_id}"
                )
            if pump_id not in self.available_pumps:
                raise ValueError(f"Pump {pump_id} is not configured")
            try:
                start_at = datetime.fromisoformat(str(event["start_at"]))
                duration = float(event["duration_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid watering event {event_id}") from exc
            if start_at.tzinfo is None:
                raise ValueError(f"start_at must include a UTC offset: {event_id}")
            local_start = start_at.astimezone(TZ)
            if not week_start <= local_start.date() < week_start + timedelta(days=7):
                raise ValueError(f"Watering event is outside week_start: {event_id}")
            if not 1 <= duration <= MAX_WATERING_SECONDS:
                raise ValueError(
                    f"duration_seconds for {event_id} must be 1..{MAX_WATERING_SECONDS:g}"
                )
            accepted_events.append(
                {
                    "id": event_id,
                    "plant_id": plant_id,
                    "pump_id": pump_id,
                    "start_at": local_start.isoformat(),
                    "duration_seconds": duration,
                    "will_run": local_start > current,
                }
            )

        recommendations = plan.get("human_recommendations")
        if not isinstance(recommendations, str):
            raise ValueError("human_recommendations must be a string")
        if len(recommendations) > 4000:
            raise ValueError("human_recommendations must not exceed 4000 characters")
        return {
            "schema_version": "1.0",
            "week_start": week_start.isoformat(),
            "timezone": BOT_TIMEZONE,
            "watering": accepted_events,
            "human_recommendations": recommendations,
            "accepted_at": current.isoformat(),
        }

    def apply_plan(
        self, plan: Dict[str, Any], now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        accepted = self._validate_plan(plan, now=now)
        for job in self.scheduler.get_jobs():
            if job.id.startswith("watering:"):
                self.scheduler.remove_job(job.id)
        for event in accepted["watering"]:
            if not event["will_run"]:
                continue
            start_at = datetime.fromisoformat(event["start_at"])
            self.scheduler.add_job(
                self._run_scheduled_watering,
                DateTrigger(run_date=start_at),
                kwargs={"event": event},
                id=f"watering:{event['id']}",
                replace_existing=True,
                misfire_grace_time=300,
            )
        _write_json(PLAN_FILE, accepted)
        return accepted

    def _restore_saved_plan(self) -> None:
        if not PLAN_FILE.exists():
            return
        try:
            self.apply_plan(_read_json(PLAN_FILE))
        except Exception:
            logger.exception("Saved watering plan could not be restored")

    async def _run_scheduled_watering(self, event: Dict[str, Any]) -> None:
        duration = float(event["duration_seconds"])
        plant_names = {"lemon": "Лимон", "pepper": "Перец"}
        plant_name = plant_names.get(str(event["plant_id"]), str(event["plant_id"]))
        with tempfile.TemporaryDirectory(prefix="scheduled-watering-") as temp_dir:
            video_path = Path(temp_dir) / "watering.mp4"
            try:
                async with self.camera_lock:
                    async with self.pump_lock:
                        await asyncio.to_thread(
                            self.water_with_video,
                            video_path,
                            str(event["pump_id"]),
                            duration,
                        )
            except Exception:
                logger.exception("Scheduled watering/video %s failed", event["id"])
                await self._notify(
                    f"Ошибка планового полива или записи видео {event['id']}."
                )
                return

            try:
                await self._send_watering_video(
                    video_path=video_path,
                    caption=(
                        f"Плановый полив: {plant_name}, {duration:g} сек.\n"
                        f"Задание: {event['id']}"
                    ),
                )
            except Exception:
                logger.exception(
                    "Watering %s completed but video delivery failed", event["id"]
                )
                await self._notify(
                    f"Полив {event['id']} выполнен, но отправить видео не удалось."
                )

    async def _send_watering_video(self, video_path: Path, caption: str) -> None:
        if not self.chat_ids:
            logger.warning(
                "Scheduled watering completed, but REPORT_CHAT_IDS and "
                "ALLOWED_USER_IDS are empty; video was not sent"
            )
            return
        delivered = 0
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_video(
                    chat_id=chat_id,
                    video=FSInputFile(video_path),
                    caption=caption,
                    supports_streaming=True,
                )
                delivered += 1
            except Exception:
                logger.exception(
                    "Could not send scheduled watering video to chat %s", chat_id
                )
        if delivered == 0:
            raise RuntimeError("Scheduled watering video was not delivered")
