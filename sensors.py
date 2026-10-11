"""Sensor access layer.

Real temperature and soil-moisture drivers will replace the environment-backed
placeholders in ``read_sensor_snapshot`` when the sensors are connected.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict


def _mock_value(
    name: str, legacy_name: str = "", *, percentage: bool = False
) -> float | None:
    """Read an optional mock value without emitting invalid JSON sensor data."""
    setting = name
    raw = os.getenv(name, "").strip()
    if not raw and legacy_name:
        setting = legacy_name
        raw = os.getenv(legacy_name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{setting} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{setting} must be a finite number")
    if percentage and not 0 <= value <= 100:
        raise ValueError(f"{setting} must be between 0 and 100")
    return value


def read_sensor_snapshot() -> Dict[str, Any]:
    temperature = _mock_value(
        "MOCK_AIR_TEMPERATURE_C", "MOCK_TEMPERATURE_C"
    )
    lemon_moisture = _mock_value(
        "MOCK_LEMON_SOIL_MOISTURE_PERCENT", "MOCK_SOIL_MOISTURE_PERCENT", percentage=True
    )
    pepper_moisture = _mock_value("MOCK_PEPPER_SOIL_MOISTURE_PERCENT", percentage=True)
    return {
        "temperature_c": temperature,
        "temperature_sensor_status": (
            "mock" if temperature is not None else "not_connected"
        ),
        "plants": [
            {
                "plant_id": "lemon",
                "soil_moisture_percent": lemon_moisture,
                "sensor_status": (
                    "mock" if lemon_moisture is not None else "not_connected"
                ),
            },
            {
                "plant_id": "pepper",
                "soil_moisture_percent": pepper_moisture,
                "sensor_status": (
                    "mock" if pepper_moisture is not None else "not_connected"
                ),
            },
        ],
    }
