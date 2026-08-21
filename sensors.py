"""Sensor access layer.

Real temperature and soil-moisture drivers will replace the environment-backed
placeholders in ``read_sensor_snapshot`` when the sensors are connected.
"""

from __future__ import annotations

import os
from typing import Any, Dict


def read_sensor_snapshot() -> Dict[str, Any]:
    temperature = os.getenv("MOCK_AIR_TEMPERATURE_C") or os.getenv(
        "MOCK_TEMPERATURE_C"
    )
    lemon_moisture = os.getenv("MOCK_LEMON_SOIL_MOISTURE_PERCENT") or os.getenv(
        "MOCK_SOIL_MOISTURE_PERCENT"
    )
    pepper_moisture = os.getenv("MOCK_PEPPER_SOIL_MOISTURE_PERCENT")
    return {
        "temperature_c": float(temperature) if temperature else None,
        "temperature_sensor_status": "mock" if temperature else "not_connected",
        "plants": [
            {
                "plant_id": "lemon",
                "soil_moisture_percent": (
                    float(lemon_moisture) if lemon_moisture else None
                ),
                "sensor_status": "mock" if lemon_moisture else "not_connected",
            },
            {
                "plant_id": "pepper",
                "soil_moisture_percent": (
                    float(pepper_moisture) if pepper_moisture else None
                ),
                "sensor_status": "mock" if pepper_moisture else "not_connected",
            },
        ],
    }
