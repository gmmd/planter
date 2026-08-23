"""Yandex AI Studio integration through its OpenAI-compatible Responses API."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

import openai
from PIL import Image, ImageOps


logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent
PLAN_SCHEMA_PATH = PROJECT_DIR / "schemas" / "weekly_plan.schema.json"
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data")))
AI_LOGS_DIR = DATA_DIR / "ai_logs"
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(BOT_TIMEZONE)


class NonJsonAIResponse(ValueError):
    """Raised when AI returned text that cannot be accepted as a JSON plan."""

    def __init__(self, raw_text: str) -> None:
        super().__init__("AI response is not a JSON object")
        self.raw_text = raw_text


class AIProviderResponseError(RuntimeError):
    """Raised when Responses API returned a response with failed status."""

    def __init__(self, code: str, message: str, response_id: Optional[str]) -> None:
        self.code = code
        self.provider_message = message
        self.response_id = response_id
        suffix = f"; response_id={response_id}" if response_id else ""
        super().__init__(f"AI provider error {code}: {message}{suffix}")


async def request_weekly_plan(
    report: Dict[str, Any],
    photo_paths: Sequence[Path],
    force_api: bool = False,
) -> Optional[Dict[str, Any]]:
    """Send one report with all weekly JPEGs and parse the returned JSON plan."""
    response_file = os.getenv("AI_RESPONSE_FILE", "").strip()
    if response_file and not force_api:
        return await asyncio.to_thread(_read_json, Path(response_file))

    api_key = os.getenv("YANDEX_AI_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "YANDEX_AI_API_KEY is not configured; weekly request was saved locally"
        )
        return None

    retries = int(os.getenv("YANDEX_AI_REQUEST_RETRIES", "1"))
    if not 0 <= retries <= 3:
        raise RuntimeError("YANDEX_AI_REQUEST_RETRIES must be between 0 and 3")
    for attempt in range(1, retries + 2):
        try:
            return await asyncio.to_thread(
                _request_weekly_plan,
                api_key,
                report,
                photo_paths,
                attempt,
            )
        except Exception as exc:
            if attempt > retries or not _is_retryable_ai_error(exc):
                raise
            delay = min(2 ** (attempt - 1), 4)
            logger.warning(
                "Retrying transient AI error in %s second(s), attempt %s/%s: %s",
                delay,
                attempt + 1,
                retries + 1,
                exc,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("AI retry loop ended unexpectedly")


def _request_weekly_plan(
    api_key: str,
    report: Dict[str, Any],
    photo_paths: Sequence[Path],
    attempt_number: int = 1,
) -> Dict[str, Any]:
    started_at = datetime.now(TZ)
    started_monotonic = time.monotonic()
    base_url = os.getenv(
        "YANDEX_AI_BASE_URL", "https://ai.api.cloud.yandex.net/v1"
    ).strip()
    project_id = os.getenv("YANDEX_AI_PROJECT_ID", "b1gr38liecpk6mp2g7ul").strip()
    prompt_id = os.getenv("YANDEX_AI_PROMPT_ID", "fvthisds2b24do0qnn7q").strip()
    detail = os.getenv("YANDEX_AI_IMAGE_DETAIL", "low").strip().lower()
    timeout = float(os.getenv("YANDEX_AI_TIMEOUT_SECONDS", "300"))
    image_max_width = int(os.getenv("YANDEX_AI_IMAGE_MAX_WIDTH", "1280"))
    image_max_height = int(os.getenv("YANDEX_AI_IMAGE_MAX_HEIGHT", "720"))
    image_quality = int(os.getenv("YANDEX_AI_IMAGE_JPEG_QUALITY", "75"))
    max_output_tokens = int(os.getenv("YANDEX_AI_MAX_OUTPUT_TOKENS", "4000"))
    if not project_id or not prompt_id:
        raise RuntimeError("YANDEX_AI_PROJECT_ID and YANDEX_AI_PROMPT_ID are required")
    if detail not in {"low", "high", "auto"}:
        raise RuntimeError("YANDEX_AI_IMAGE_DETAIL must be low, high, or auto")
    if image_max_width < 320 or image_max_height < 240:
        raise RuntimeError("YANDEX_AI image dimensions are too small")
    if not 40 <= image_quality <= 95:
        raise RuntimeError("YANDEX_AI_IMAGE_JPEG_QUALITY must be between 40 and 95")
    if not 256 <= max_output_tokens <= 81920:
        raise RuntimeError("YANDEX_AI_MAX_OUTPUT_TOKENS must be between 256 and 81920")

    schema = _read_json(PLAN_SCHEMA_PATH)
    prompt_input = {
        "task": (
            "Проанализируй недельные фотографии, температуру воздуха и влажность "
            "почвы лимона и перца. Верни только JSON расписания полива и рекомендаций "
            "человеку в формате, заданном JSON Schema запроса. Не добавляй Markdown "
            "или текст вне JSON. Используй только plant_id/pump_id из схемы. "
            "Всегда соблюдай соответствие: lemon использует только pump_lemon, "
            "а pepper использует только pump_pepper. "
            "week_start должен быть понедельником текущей недели из "
            "report_generated_at, а start_at должен быть в будущем и внутри этой недели."
        ),
        "weekly_report": report,
    }
    tools = _build_optional_tools()
    prepared_images = [
        _prepare_image(path, image_max_width, image_max_height, image_quality)
        for path in photo_paths
    ]
    log_dir = _create_ai_log_dir(started_at)
    _write_json(
        log_dir / "request.json",
        {
            "request_started_at": started_at.isoformat(),
            "attempt_number": attempt_number,
            "timezone": BOT_TIMEZONE,
            "provider": "Yandex AI Studio",
            "base_url": base_url,
            "project_id": project_id,
            "prompt": {"id": prompt_id},
            "image_detail": detail,
            "image_optimization": {
                "max_width": image_max_width,
                "max_height": image_max_height,
                "jpeg_quality": image_quality,
            },
            "timeout_seconds": timeout,
            "max_output_tokens": max_output_tokens,
            "input": prompt_input,
            "response_schema": schema,
            "photos": [image["log"] for image in prepared_images],
            "tools": tools,
        },
    )
    _write_status(
        log_dir,
        status="requesting",
        request_started_at=started_at,
    )
    content: list[Dict[str, Any]] = [
        {
            "type": "input_text",
            "text": json.dumps(prompt_input, ensure_ascii=False),
        }
    ]
    for image in prepared_images:
        content.append(
            {
                "type": "input_image",
                "image_url": image["data_url"],
                "detail": detail,
            }
        )

    client = openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        project=project_id,
        timeout=timeout,
        # Retries are handled above so every physical API call gets its own log.
        max_retries=0,
    )
    logger.info(
        "Sending weekly AI request attempt %s with %s optimized photos; log: %s",
        attempt_number,
        len(photo_paths),
        log_dir,
    )
    try:
        response = client.responses.create(
            prompt={"id": prompt_id},
            input=[{"role": "user", "content": content}],
            tools=tools,
            max_output_tokens=max_output_tokens,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "weekly_plant_care_plan",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
    except Exception as exc:
        failed_at = datetime.now(TZ)
        duration = time.monotonic() - started_monotonic
        _write_json(
            log_dir / "error.json",
            {
                "failed_at": failed_at.isoformat(),
                "duration_seconds": round(duration, 3),
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        _write_status(
            log_dir,
            status="api_error",
            request_started_at=started_at,
            finished_at=failed_at,
            duration_seconds=duration,
        )
        logger.exception("Weekly AI request failed; log: %s", log_dir)
        raise

    received_at = datetime.now(TZ)
    duration = time.monotonic() - started_monotonic
    raw_text = getattr(response, "output_text", "") or ""
    _write_text(log_dir / "response.txt", raw_text)
    _write_json(
        log_dir / "response.json",
        {
            "response_received_at": received_at.isoformat(),
            "duration_seconds": round(duration, 3),
            "response_id": getattr(response, "id", None),
            "output_text": raw_text,
            "response": _serialize_ai_response(response),
        },
    )
    logger.info(
        "Weekly AI response received: %s; log: %s",
        getattr(response, "id", "unknown"),
        log_dir,
    )
    response_status = _object_field(response, "status", "")
    provider_error = _object_field(response, "error", None)
    if provider_error is not None or (
        response_status and response_status != "completed"
    ):
        error_code = str(
            _object_field(provider_error, "code", response_status or "failed")
        )
        error_message = str(
            _object_field(
                provider_error,
                "message",
                f"Responses API finished with status {response_status or 'failed'}",
            )
        )
        response_id_value = getattr(response, "id", None)
        response_id = str(response_id_value) if response_id_value else None
        _write_json(
            log_dir / "error.json",
            {
                "failed_at": received_at.isoformat(),
                "duration_seconds": round(duration, 3),
                "response_id": response_id,
                "response_status": response_status,
                "provider_error": provider_error,
                "code": error_code,
                "message": error_message,
            },
        )
        _write_status(
            log_dir,
            status="provider_error",
            request_started_at=started_at,
            finished_at=received_at,
            duration_seconds=duration,
        )
        raise AIProviderResponseError(error_code, error_message, response_id)
    try:
        parsed = _parse_response_json(raw_text)
    except NonJsonAIResponse:
        _write_status(
            log_dir,
            status="non_json_response",
            request_started_at=started_at,
            finished_at=received_at,
            duration_seconds=duration,
        )
        raise
    _write_json(log_dir / "parsed_plan.json", parsed)
    _write_status(
        log_dir,
        status="json_response",
        request_started_at=started_at,
        finished_at=received_at,
        duration_seconds=duration,
    )
    return parsed


def _create_ai_log_dir(timestamp: datetime) -> Path:
    directory = (
        AI_LOGS_DIR
        / f"{timestamp:%Y-%m-%d}"
        / f"{timestamp:%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:8]}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _photo_log_entry(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    try:
        logged_path = str(path.resolve().relative_to(PROJECT_DIR.resolve()))
    except ValueError:
        logged_path = str(path.resolve())
    return {
        "filename": path.name,
        "path": logged_path,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(),
    }


def _prepare_image(
    path: Path,
    max_width: int,
    max_height: int,
    jpeg_quality: int,
) -> Dict[str, Any]:
    log_entry = _photo_log_entry(path)
    with Image.open(path) as source:
        original_width, original_height = source.size
        image = ImageOps.exif_transpose(source).convert("RGB")
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image.thumbnail((max_width, max_height), resampling)
        transmitted_width, transmitted_height = image.size
        buffer = io.BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=jpeg_quality,
            optimize=True,
        )
    payload = buffer.getvalue()
    encoded = base64.b64encode(payload).decode("ascii")
    log_entry.update(
        {
            "original_width": original_width,
            "original_height": original_height,
            "transmitted_width": transmitted_width,
            "transmitted_height": transmitted_height,
            "transmitted_size_bytes": len(payload),
        }
    )
    return {
        "data_url": f"data:image/jpeg;base64,{encoded}",
        "log": log_entry,
    }


def _build_optional_tools() -> list[Dict[str, Any]]:
    tools: list[Dict[str, Any]] = []
    if _env_flag("YANDEX_AI_ENABLE_FILE_SEARCH"):
        vector_store_id = os.getenv(
            "YANDEX_AI_VECTOR_STORE_ID", "fvtjgb6img000tqpr457"
        ).strip()
        if not vector_store_id:
            raise RuntimeError(
                "YANDEX_AI_VECTOR_STORE_ID is required when file search is enabled"
            )
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": [vector_store_id],
                "max_num_results": 5,
            }
        )
    if _env_flag("YANDEX_AI_ENABLE_WEB_SEARCH"):
        tools.append(
            {
                "type": "web_search",
                "search_context_size": "low",
            }
        )
    return tools


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _object_field(value: Any, name: str, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, dict):
        result = value.get(name, default)
    else:
        result = getattr(value, name, default)
    enum_value = getattr(result, "value", None)
    return enum_value if enum_value is not None else result


def _is_retryable_ai_error(exc: Exception) -> bool:
    if isinstance(exc, AIProviderResponseError):
        return exc.code in {
            "model_call_error",
            "server_error",
            "rate_limit_exceeded",
            "vector_store_timeout",
        }
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
        return True
    return type(exc).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    }


def _serialize_ai_response(response: Any) -> Any:
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except Exception:
            try:
                return model_dump()
            except Exception:
                logger.warning("Could not serialize AI response with model_dump")
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            logger.warning("Could not serialize AI response with to_dict")
    return {"representation": repr(response)}


def _write_status(
    log_dir: Path,
    status: str,
    request_started_at: datetime,
    finished_at: Optional[datetime] = None,
    duration_seconds: Optional[float] = None,
) -> None:
    value: Dict[str, Any] = {
        "status": status,
        "request_started_at": request_started_at.isoformat(),
        "updated_at": datetime.now(TZ).isoformat(),
    }
    if finished_at is not None:
        value["finished_at"] = finished_at.isoformat()
    if duration_seconds is not None:
        value["duration_seconds"] = round(duration_seconds, 3)
    _write_json(log_dir / "status.json", value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, default=str)
        file.write("\n")
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _parse_response_json(text: str) -> Dict[str, Any]:
    raw_text = text or ""
    value = raw_text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise NonJsonAIResponse(raw_text)
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise NonJsonAIResponse(raw_text) from exc
    if not isinstance(parsed, dict):
        raise NonJsonAIResponse(raw_text)
    return parsed


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError("AI response must be a JSON object")
    return value
