"""Checks for the optional sensor configuration; no hardware is accessed."""

import json
import unittest
from unittest.mock import patch

from sensors import read_sensor_snapshot


class SensorSnapshotTests(unittest.TestCase):
    def test_disconnected_sensors_are_null(self):
        with patch.dict("os.environ", {}, clear=True):
            snapshot = read_sensor_snapshot()
        self.assertIsNone(snapshot["temperature_c"])
        self.assertEqual(snapshot["temperature_sensor_status"], "not_connected")
        for plant in snapshot["plants"]:
            self.assertIsNone(plant["soil_moisture_percent"])
            self.assertEqual(plant["sensor_status"], "not_connected")

    def test_zero_and_boundaries_remain_connected(self):
        with patch.dict("os.environ", {
            "MOCK_AIR_TEMPERATURE_C": "0",
            "MOCK_LEMON_SOIL_MOISTURE_PERCENT": "0",
            "MOCK_PEPPER_SOIL_MOISTURE_PERCENT": "100",
        }, clear=True):
            snapshot = read_sensor_snapshot()
        self.assertEqual(snapshot["temperature_c"], 0)
        self.assertEqual(snapshot["temperature_sensor_status"], "mock")
        self.assertEqual([p["soil_moisture_percent"] for p in snapshot["plants"]], [0, 100])
        self.assertTrue(all(p["sensor_status"] == "mock" for p in snapshot["plants"]))
        json.dumps(snapshot, allow_nan=False)

    def test_legacy_settings_and_primary_override(self):
        with patch.dict("os.environ", {
            "MOCK_TEMPERATURE_C": "21.5",
            "MOCK_SOIL_MOISTURE_PERCENT": "40",
            "MOCK_LEMON_SOIL_MOISTURE_PERCENT": " 50 ",
        }, clear=True):
            snapshot = read_sensor_snapshot()
        self.assertEqual(snapshot["temperature_c"], 21.5)
        self.assertEqual(snapshot["plants"][0]["soil_moisture_percent"], 50)

    def test_nonfinite_or_invalid_values_are_rejected(self):
        for value in ("NaN", "inf", "-inf", "broken"):
            with self.subTest(value=value):
                with patch.dict("os.environ", {"MOCK_AIR_TEMPERATURE_C": value}, clear=True):
                    with self.assertRaisesRegex(ValueError, "MOCK_AIR_TEMPERATURE_C"):
                        read_sensor_snapshot()

    def test_moisture_must_be_a_percentage(self):
        for name in ("MOCK_LEMON_SOIL_MOISTURE_PERCENT", "MOCK_PEPPER_SOIL_MOISTURE_PERCENT"):
            for value in ("-1", "101", "nan"):
                with self.subTest(name=name, value=value):
                    with patch.dict("os.environ", {name: value}, clear=True):
                        with self.assertRaisesRegex(ValueError, name):
                            read_sensor_snapshot()


if __name__ == "__main__":
    unittest.main()
