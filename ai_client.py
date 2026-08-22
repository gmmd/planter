"""Yandex AI Studio integration through its OpenAI-compatible Responses API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import openai


logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent
PLAN_SCHEMA_PATH = PROJECT_DIR / "schemas" / "weekly_plan.schema.json"


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
    logger.info("Sending weekly AI request with %s photos", len(photo_paths))
    response = client.responses.create(
        prompt={"id": prompt_id},
        input=[{"role": "user", "content": content}],
        tools=[
            {
                "type": "file_search",
                "vector_store_ids": ["fvtjgb6img000tqpr457"],
                "max_num_results": 5
            },
            {
                "type": "web_search",
                "filters": {"allowed_domains": []},
                "search_context_size": "medium"
            }
	    ],
    )
    logger.info("Weekly AI response received: %s", getattr(response, "id", "unknown"))
    return _parse_response_json(response.output_text)


def _jpeg_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _parse_response_json(text: str) -> Dict[str, Any]:
    value = text.strip()
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
            raise ValueError("AI response does not contain a JSON object")
        parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a JSON object")
    return parsed


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError("AI response must be a JSON object")
    return value
