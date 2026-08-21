#!/usr/bin/env python3
"""Telegram bot that takes a Raspberry Pi camera photo on request."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, Message
from gpiozero import OutputDevice
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

from automation import PlantAutomation, TZ, new_photo_path
from photo_watermark import add_photo_watermark
from sensors import read_sensor_snapshot


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
router = Router()
camera_lock = asyncio.Lock()
pump_lock = asyncio.Lock()
automation_controller: Optional[PlantAutomation] = None
LEMON_PUMP_GPIO = int(os.getenv("LEMON_PUMP_GPIO", os.getenv("PUMP_GPIO", "17")))
PEPPER_PUMP_GPIO_TEXT = os.getenv("PEPPER_PUMP_GPIO", "").strip()
LEMON_WATERING_SECONDS = float(
    os.getenv("LEMON_WATERING_SECONDS", os.getenv("WATERING_SECONDS", "5"))
)
PEPPER_WATERING_SECONDS = float(os.getenv("PEPPER_WATERING_SECONDS", "5"))
VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "640"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "480"))
VIDEO_FRAMERATE = float(os.getenv("VIDEO_FRAMERATE", "24"))
VIDEO_BITRATE = int(os.getenv("VIDEO_BITRATE", "2000000"))
VIDEO_LEAD_IN_SECONDS = float(os.getenv("VIDEO_LEAD_IN_SECONDS", "1"))
VIDEO_LEAD_OUT_SECONDS = float(os.getenv("VIDEO_LEAD_OUT_SECONDS", "0.5"))
# active_high=False means on() drives GPIO LOW and off() drives it HIGH.
pumps = {
    "pump_lemon": OutputDevice(
        LEMON_PUMP_GPIO, active_high=False, initial_value=False
    )
}
if PEPPER_PUMP_GPIO_TEXT:
    if int(PEPPER_PUMP_GPIO_TEXT) == LEMON_PUMP_GPIO:
        raise RuntimeError("LEMON_PUMP_GPIO and PEPPER_PUMP_GPIO must be different")
    pumps["pump_pepper"] = OutputDevice(
        int(PEPPER_PUMP_GPIO_TEXT), active_high=False, initial_value=False
    )


def _sleep_until(deadline: float) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(remaining)


def stop_all_pumps() -> None:
    for configured_pump in pumps.values():
        configured_pump.off()


def allowed_user_ids() -> set[int]:
    """Return allowed Telegram user IDs; an empty value allows everyone."""
    value = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not value:
        return set()
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_IDS must contain comma-separated numbers") from exc


def is_allowed(message: Message) -> bool:
    allowed = allowed_user_ids()
    return not allowed or (message.from_user is not None and message.from_user.id in allowed)


def capture_photo(path: Path) -> None:
    """Capture one JPEG with the first available Raspberry Pi camera."""
    camera = Picamera2()
    try:
        width = int(os.getenv("PHOTO_WIDTH", "1920"))
        height = int(os.getenv("PHOTO_HEIGHT", "1080"))
        config = camera.create_still_configuration(main={"size": (width, height)})
        camera.configure(config)
        camera.start()
        # Let auto-exposure and auto-white-balance settle.
        time.sleep(2)
        camera.capture_file(str(path))
    finally:
        camera.close()
    add_photo_watermark(path, datetime.now(TZ), read_sensor_snapshot())


def run_watering_with_video(
    path: Path, pump_id: str, duration_seconds: float
) -> None:
    """Record video before, during, and after one active-low pump cycle."""
    camera = Picamera2()
    recording = False
    try:
        frame_duration = int(1_000_000 / VIDEO_FRAMERATE)
        config = camera.create_video_configuration(
            main={"size": (VIDEO_WIDTH, VIDEO_HEIGHT), "format": "YUV420"},
            controls={"FrameDurationLimits": (frame_duration, frame_duration)},
            buffer_count=4,
        )
        camera.configure(config)
        encoder = H264Encoder(bitrate=VIDEO_BITRATE)
        output = FfmpegOutput(str(path), audio=False)
        camera.start_recording(encoder, output)
        recording = True

        # Give the encoder time to produce frames before watering begins.
        time.sleep(VIDEO_LEAD_IN_SECONDS)

        selected_pump = pumps.get(pump_id)
        if selected_pump is None:
            raise RuntimeError(f"Pump {pump_id} is not configured")
        selected_pump.on()
        pump_started = time.monotonic()
        _sleep_until(pump_started + duration_seconds)

        selected_pump.off()
        logger.info(
            "Pump %s ran for %.3f seconds",
            pump_id,
            time.monotonic() - pump_started,
        )
        time.sleep(VIDEO_LEAD_OUT_SECONDS)
    finally:
        # The pump must be turned off even if camera recording fails.
        stop_all_pumps()
        if recording:
            camera.stop_recording()
        camera.close()


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("Access denied.")
        return
    await message.answer(
        "Бот управления растением готов. Команды доступны через кнопку Menu. "
        "Для подробностей используйте /help."
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("Access denied.")
        return
    await message.answer(
        "Доступные команды:\n\n"
        "/take_photo — сделать фотографию, добавить показания датчиков и "
        "сохранить её в архиве.\n"
        f"/water_lemon — поливать лимон {LEMON_WATERING_SECONDS:g} сек. и отправить "
        "видео полива.\n"
        f"/water_pepper — поливать перец {PEPPER_WATERING_SECONDS:g} сек. и отправить "
        "видео полива.\n"
        "/weekly_report — вручную подготовить недельный набор фотографий и "
        "запрос к нейросети.\n"
        "/schedule — принудительно запросить у нейросети расписание и получить "
        "его в этом чате.\n"
        "/help — показать эту справку.\n\n"
        "Автоматически бот делает три фотографии ежедневно, каждый понедельник "
        "в 10:00 запрашивает недельный план и выполняет разрешённые поливы. "
        "Каждый плановый полив записывается на видео и отправляется в Telegram."
    )


@router.message(Command("take_photo"))
async def take_photo(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("Access denied.")
        return

    if camera_lock.locked():
        await message.answer("The camera is busy. Please try again in a moment.")
        return

    status = await message.answer("Taking a photo…")
    try:
        async with camera_lock:
            photo_path = new_photo_path()
            await asyncio.to_thread(capture_photo, photo_path)
            await message.answer_photo(
                FSInputFile(photo_path),
                caption="Photo from Raspberry Pi",
            )
        await status.delete()
    except Exception:
        logger.exception("Could not capture or send photo")
        await status.edit_text("Could not take the photo. Check the service logs.")


async def _water_plant(
    message: Message,
    plant_name: str,
    pump_id: str,
    duration_seconds: float,
) -> None:
    if not is_allowed(message):
        await message.answer("Access denied.")
        return

    if pump_id not in pumps:
        await message.answer(
            f"Помпа для растения «{plant_name}» не настроена. Укажите её BCM GPIO в .env."
        )
        return

    if camera_lock.locked() or pump_lock.locked():
        await message.answer("The camera or pump is busy. Please try again in a moment.")
        return

    status = await message.answer(f"Поливаю {plant_name} и записываю видео…")
    try:
        async with camera_lock:
            async with pump_lock:
                with tempfile.TemporaryDirectory(prefix="telegram-watering-") as temp_dir:
                    video_path = Path(temp_dir) / "watering.mp4"
                    await asyncio.to_thread(
                        run_watering_with_video,
                        video_path,
                        pump_id,
                        duration_seconds,
                    )
                    await message.answer_video(
                        FSInputFile(video_path),
                        caption=f"{plant_name.capitalize()}: полив {duration_seconds:g} сек.",
                        supports_streaming=True,
                    )
        await status.delete()
    except Exception:
        logger.exception("Could not water plant or send video")
        stop_all_pumps()
        await status.edit_text("Watering or recording failed. The pump was switched off.")


@router.message(Command("water_lemon"))
async def water_lemon(message: Message) -> None:
    await _water_plant(
        message, "лимон", "pump_lemon", LEMON_WATERING_SECONDS
    )


@router.message(Command("water_pepper"))
async def water_pepper(message: Message) -> None:
    await _water_plant(
        message, "перец", "pump_pepper", PEPPER_WATERING_SECONDS
    )


@router.message(Command("weekly_report"))
async def weekly_report(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("Access denied.")
        return
    if automation_controller is None:
        await message.answer("Automation scheduler is not ready.")
        return
    status = await message.answer("Preparing the weekly photos and AI request…")
    result = await automation_controller.run_weekly_cycle()
    await status.edit_text(result)


@router.message(Command("schedule"))
async def schedule(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("Access denied.")
        return
    if automation_controller is None:
        await message.answer("Automation scheduler is not ready.")
        return
    status = await message.answer(
        "Отправляю недельные фотографии и показания датчиков нейросети…"
    )
    result = await automation_controller.run_weekly_cycle(
        reply_chat_id=message.chat.id,
        notify_configured_chats=False,
        force_ai=True,
    )
    await status.edit_text(result)


async def main() -> None:
    global automation_controller
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(token=token)
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить бота"),
                BotCommand(command="help", description="Справка по командам"),
                BotCommand(command="take_photo", description="Сделать фотографию"),
                BotCommand(
                    command="water_lemon", description="Полить лимон и снять видео"
                ),
                BotCommand(
                    command="water_pepper", description="Полить перец и снять видео"
                ),
                BotCommand(
                    command="weekly_report", description="Запустить недельный отчёт"
                ),
                BotCommand(
                    command="schedule", description="Получить расписание от AI"
                ),
            ]
        )
    except Exception:
        logger.exception("Could not register Telegram command menu")
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    automation_controller = PlantAutomation(
        bot=bot,
        camera_lock=camera_lock,
        pump_lock=pump_lock,
        capture_photo=capture_photo,
        water_with_video=run_watering_with_video,
        available_pumps=set(pumps),
    )
    await automation_controller.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        automation_controller.shutdown()
        stop_all_pumps()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
