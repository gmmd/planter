"""JPEG watermark rendering for archived plant photos."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from PIL import Image, ImageDraw, ImageFont, ImageOps


def _display_sensor(value: object, suffix: str) -> str:
    if value is None:
        return f"n/a {suffix}"
    return f"{float(value):.1f} {suffix}"


def add_photo_watermark(
    path: Path, captured_at: datetime, sensors: Dict[str, Any]
) -> None:
    """Add capture time and the latest sensor readings to a JPEG."""
    moisture = {
        plant["plant_id"]: plant.get("soil_moisture_percent")
        for plant in sensors["plants"]
    }
    lines = [
        f"{captured_at:%Y-%m-%d %H:%M:%S}",
        f"Air: {_display_sensor(sensors['temperature_c'], 'C')}",
        (
            f"Lemon soil: {_display_sensor(moisture.get('lemon'), '%')}  |  "
            f"Pepper soil: {_display_sensor(moisture.get('pepper'), '%')}"
        ),
    ]
    text = "\n".join(lines)
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    font_size = max(18, image.width // 55)
    font_path = os.getenv(
        "WATERMARK_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    try:
        font = ImageFont.truetype(font_path, font_size)
    except OSError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(image, "RGBA")
    margin = max(12, image.width // 100)
    padding = max(8, font_size // 3)
    spacing = max(4, font_size // 5)
    text_box = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    left = margin
    top = image.height - margin - text_height - padding * 2
    draw.rectangle(
        (left, top, left + text_width + padding * 2, image.height - margin),
        fill=(0, 0, 0, 155),
    )
    draw.multiline_text(
        (left + padding, top + padding - text_box[1]),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        spacing=spacing,
    )
    image.save(path, format="JPEG", quality=92, optimize=True)
