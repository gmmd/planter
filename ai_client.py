"""Yandex AI Studio integration through its OpenAI-compatible Responses API."""

from __future__ import annotations

import asyncio
import base64
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

    return await asyncio.to_thread(_request_weekly_plan, api_key, report, photo_paths)


def _request_weekly_plan(
    api_key: str, report: Dict[str, Any], photo_paths: Sequence[Path]
) -> Dict[str, Any]:
    started_at = datetime.now(TZ)
    started_monotonic = time.monotonic()
    base_url = os.getenv(
        "YANDEX_AI_BASE_URL", "https://ai.api.cloud.yandex.net/v1"
    ).strip()
    project_id = os.getenv("YANDEX_AI_PROJECT_ID", "b1gr38liecpk6mp2g7ul").strip()
    prompt_id = os.getenv("YANDEX_AI_PROMPT_ID", "fvthisds2b24do0qnn7q").strip()
    detail = os.getenv("YANDEX_AI_IMAGE_DETAIL", "auto").strip().lower()
    timeout = float(os.getenv("YANDEX_AI_TIMEOUT_SECONDS", "300"))
    if not project_id or not prompt_id:
        raise RuntimeError("YANDEX_AI_PROJECT_ID and YANDEX_AI_PROMPT_ID are required")
    if detail not in {"low", "high", "auto"}:
        raise RuntimeError("YANDEX_AI_IMAGE_DETAIL must be low, high, or auto")

    schema = _read_json(PLAN_SCHEMA_PATH)
    prompt_input = {
        "task": (
            "Проанализируй недельные фотографии, температуру воздуха и влажность "
            "почвы лимона и перца. Верни только JSON расписания полива и рекомендаций "
            "человеку, строго соответствующий required_response_schema. Не добавляй "
            "Markdown или текст вне JSON. Используй только plant_id/pump_id из схемы. "
            "Всегда соблюдай соответствие: lemon использует только pump_lemon, "
            "а pepper использует только pump_pepper. "
            "week_start должен быть понедельником текущей недели из "
            "report_generated_at, а start_at должен быть в будущем и внутри этой недели."
        ),
        "weekly_report": report,
        "required_response_schema": schema,
    }
    tools = [
        {
            "type": "file_search",
            "vector_store_ids": ["fvtjgb6img000tqpr457"],
            "max_num_results": 5,
        },
        {
            "type": "web_search",
            "filters": {"allowed_domains": []},
            "search_context_size": "medium",
        },
    ]
    log_dir = _create_ai_log_dir(started_at)
    _write_json(
        log_dir / "request.json",
        {
            "request_started_at": started_at.isoformat(),
            "timezone": BOT_TIMEZONE,
            "provider": "Yandex AI Studio",
            "base_url": base_url,
            "project_id": project_id,
            "prompt": {"id": prompt_id},
            "image_detail": detail,
            "timeout_seconds": timeout,
            "input": prompt_input,
            "photos": [_photo_log_entry(path) for path in photo_paths],
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
    for photo_path in photo_paths:
        content.append(
            {
                "type": "input_image",
                "image_url": _jpeg_data_url(photo_path),
                "detail": detail,
            }
        )

    client = openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        project=project_id,
        timeout=timeout,
        max_retries=2,
    )
    logger.info(
        "Sending weekly AI request with %s photos; log: %s",
        len(photo_paths),
        log_dir,
    )
    try:
        response = client.responses.create(
            prompt={"id": prompt_id},
            input=[{"role": "user", "content": content}],
            tools=tools,
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


def _jpeg_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


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
