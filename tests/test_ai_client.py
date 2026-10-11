"""AI boundary tests: local images and mocked Responses API, no paid requests."""

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

import ai_client


class AIClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.logs = self.root / "ai_logs"
        self.addCleanup(patch.stopall)
        patch.object(ai_client, "AI_LOGS_DIR", self.logs).start()
        patch.dict(os.environ, {"YANDEX_AI_API_KEY": "test-secret-key"}, clear=True).start()

    def client(self, text='{"watering": []}', status="completed", error=None):
        response = SimpleNamespace(
            id="test-response", output_text=text, status=status, error=error,
            model_dump=lambda **kwargs: {
                "id": "test-response", "output_text": text,
                "status": status, "error": error,
            },
        )
        return SimpleNamespace(
            responses=SimpleNamespace(create=Mock(return_value=response)), close=Mock(),
        )

    def log_dirs(self):
        return sorted(self.logs.glob("*/*"))

    def test_success_closes_client_and_keeps_images_out_of_logs(self):
        photo = self.root / "plant.jpg"
        with Image.new("RGB", (1920, 1080), "green") as source:
            source.save(photo)
        original = photo.read_bytes()
        client = self.client()
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            result = ai_client._request_weekly_plan("test-secret-key", {}, [photo])
        self.assertEqual(result, {"watering": []})
        client.close.assert_called_once_with()
        payload = client.responses.create.call_args.kwargs
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        image_url = payload["input"][0]["content"][1]["image_url"]
        with Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as image:
            self.assertEqual(image.size, (1280, 720))
        self.assertEqual(photo.read_bytes(), original)
        log_dir, = self.log_dirs()
        request_text = (log_dir / "request.json").read_text()
        self.assertNotIn("test-secret-key", request_text)
        self.assertNotIn("base64", request_text)
        self.assertEqual(json.loads(request_text)["photos"][0]["transmitted_width"], 1280)
        self.assertEqual(json.loads((log_dir / "status.json").read_text())["status"], "json_response")

    def test_text_response_is_preserved_and_client_closed(self):
        text = "Растению нужно больше света.\nПодробный ответ без JSON."
        client = self.client(text=text)
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            with self.assertRaises(ai_client.NonJsonAIResponse) as caught:
                ai_client._request_weekly_plan("test-secret-key", {}, [])
        client.close.assert_called_once_with()
        self.assertEqual(caught.exception.raw_text, text)
        log_dir, = self.log_dirs()
        self.assertEqual((log_dir / "response.txt").read_text(), text)
        self.assertEqual(json.loads((log_dir / "status.json").read_text())["status"], "non_json_response")

    def test_api_error_keeps_body_and_redacts_credentials(self):
        client = self.client()
        error = RuntimeError("Rejected test-secret-key")
        error.status_code = 401
        error.request_id = "request-123"
        error.body = {"error": {"message": "Rejected test-secret-key", "code": "unauthorized"}}
        client.responses.create.side_effect = error
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            with self.assertLogs(ai_client.logger, level="ERROR") as logs:
                with self.assertRaises(RuntimeError):
                    ai_client._request_weekly_plan("test-secret-key", {}, [])
        client.close.assert_called_once_with()
        log_dir, = self.log_dirs()
        error_text = (log_dir / "error.json").read_text()
        self.assertNotIn("test-secret-key", error_text)
        self.assertNotIn("test-secret-key", "\n".join(logs.output))
        logged = json.loads(error_text)
        self.assertEqual(logged["http_status_code"], 401)
        self.assertEqual(logged["request_id"], "request-123")
        self.assertEqual(logged["response_body"]["error"]["code"], "unauthorized")
        self.assertEqual(json.loads((log_dir / "status.json").read_text())["status"], "api_error")

    def test_preparation_error_is_logged_before_api_call(self):
        with patch.object(ai_client.openai, "OpenAI") as factory:
            with self.assertRaises(FileNotFoundError):
                ai_client._request_weekly_plan("test-secret-key", {}, [self.root / "missing.jpg"])
        factory.assert_not_called()
        log_dir, = self.log_dirs()
        self.assertEqual(json.loads((log_dir / "error.json").read_text())["exception_type"], "FileNotFoundError")
        self.assertEqual(json.loads((log_dir / "status.json").read_text())["status"], "preparation_error")

    def test_provider_failure_keeps_full_response(self):
        client = self.client(text="", status="failed", error={"code": "model_call_error", "message": "500"})
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            with self.assertRaises(ai_client.AIProviderResponseError) as caught:
                ai_client._request_weekly_plan("test-secret-key", {}, [])
        self.assertEqual(caught.exception.response_id, "test-response")
        client.close.assert_called_once_with()
        log_dir, = self.log_dirs()
        logged = json.loads((log_dir / "response.json").read_text())
        self.assertEqual(logged["response"]["error"]["code"], "model_call_error")
        self.assertEqual(json.loads((log_dir / "status.json").read_text())["status"], "provider_error")

    def test_failed_response_redacts_echoed_key_in_every_archive(self):
        error_fields = {"code": "model_call_error", "message": "Rejected test-secret-key", "detail": "full detail"}
        provider_error = SimpleNamespace(
            **error_fields, model_dump=lambda **kwargs: error_fields,
        )
        client = self.client(text="Echo test-secret-key", status="failed", error=provider_error)
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            with self.assertRaises(ai_client.AIProviderResponseError) as caught:
                ai_client._request_weekly_plan("test-secret-key", {}, [])
        self.assertNotIn("test-secret-key", str(caught.exception))
        log_dir, = self.log_dirs()
        for path in log_dir.iterdir():
            with self.subTest(filename=path.name):
                self.assertNotIn("test-secret-key", path.read_text())
        self.assertEqual((log_dir / "response.txt").read_text(), "Echo [REDACTED]")
        logged = json.loads((log_dir / "response.json").read_text())
        self.assertEqual(logged["response"]["error"]["detail"], "full detail")
        logged_error = json.loads((log_dir / "error.json").read_text())
        self.assertEqual(logged_error["provider_error"]["message"], "Rejected [REDACTED]")

    def test_archive_redaction_does_not_change_non_json_returned_text(self):
        text = "Echo test-secret-key"
        client = self.client(text=text)
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            with self.assertRaises(ai_client.NonJsonAIResponse) as caught:
                ai_client._request_weekly_plan("test-secret-key", {}, [])
        self.assertEqual(caught.exception.raw_text, text)
        log_dir, = self.log_dirs()
        self.assertEqual((log_dir / "response.txt").read_text(), "Echo [REDACTED]")

    def test_retry_uses_separate_logs_and_closes_each_client(self):
        failed = self.client(text="", status="failed", error={"code": "model_call_error", "message": "500"})
        succeeded = self.client()
        with patch.object(ai_client.openai, "OpenAI", side_effect=[failed, succeeded]):
            with patch.object(ai_client.asyncio, "sleep", new_callable=AsyncMock):
                result = asyncio.run(ai_client.request_weekly_plan({}, []))
        self.assertEqual(result, {"watering": []})
        failed.close.assert_called_once_with()
        succeeded.close.assert_called_once_with()
        self.assertEqual(len(self.log_dirs()), 2)
        attempts = [json.loads((path / "request.json").read_text())["attempt_number"] for path in self.log_dirs()]
        self.assertEqual(attempts, [1, 2])

    def test_force_api_ignores_response_fixture(self):
        os.environ["AI_RESPONSE_FILE"] = str(self.root / "unused.json")
        client = self.client()
        with patch.object(ai_client.openai, "OpenAI", return_value=client):
            result = asyncio.run(ai_client.request_weekly_plan({}, [], force_api=True))
        self.assertEqual(result, {"watering": []})
        client.responses.create.assert_called_once()

    def test_schema_uses_runtime_limits(self):
        with patch.dict(os.environ, {"MAX_WATERING_SECONDS": "12.5", "MAX_WATERING_EVENTS": "8"}):
            with patch.object(ai_client, "BOT_TIMEZONE", "UTC"):
                schema = ai_client._load_plan_schema()
        properties = schema["properties"]
        self.assertEqual(properties["timezone"]["enum"], ["UTC"])
        self.assertEqual(properties["watering"]["maxItems"], 8)
        self.assertEqual(properties["watering"]["items"]["properties"]["duration_seconds"]["maximum"], 12.5)

    def test_non_finite_timeout_never_reaches_api(self):
        with patch.dict(os.environ, {"YANDEX_AI_TIMEOUT_SECONDS": "nan"}):
            with patch.object(ai_client.openai, "OpenAI") as factory:
                with self.assertRaisesRegex(RuntimeError, "positive finite"):
                    ai_client._request_weekly_plan("test-secret-key", {}, [])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
