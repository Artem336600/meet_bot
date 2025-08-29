from __future__ import annotations

import os
from typing import Any, Dict
from datetime import datetime
import zoneinfo

from mistralai.client import MistralClient  # type: ignore
from mistralai.models.chat_completion import ChatMessage  # type: ignore
import re


def get_mistral_client() -> MistralClient:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is not set")
    return MistralClient(api_key=api_key)


def summarize_tasks(transcript: str) -> str:
    """
    Строгое суммирование без домыслов.
    - Саммари: перефразирование без новых фактов.
    - Задачи: только явно сказанные поручения (кто/что/когда). Если нет — "Задачи: нет явных".
    """
    client = get_mistral_client()
    system_prompt = (
        "Ты строго извлекаешь факты из русскоязычного транскрипта. НИЧЕГО НЕ ДОДУМЫВАЙ. "
        "1) Саммари — пересказ без добавления деталей. "
        "2) Перечисли только явные поручения (исполнитель и действие, опционально срок). "
        "3) Если явных задач нет — напиши 'Задачи: нет явных'."
    )
    tz_name = os.getenv("MEETINGS_TZ", os.getenv("TZ", "Europe/Moscow"))
    now_local = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    user_prompt = (
        f"Сейчас: {now_local} ({tz_name}).\n"
        "Транскрипт:\n" + transcript + "\n\n"
        "Верни ответ в таком формате:\n"
        "Саммари: <краткий текст>\n"
        "Задачи:\n"
        "- <исполнитель>: <краткая формулировка>\n"
    )

    model = os.getenv("MISTRAL_MODEL", "mistral-medium")
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]
    resp = client.chat(model=model, messages=messages)
    return resp.choices[0].message.content  # type: ignore[index]


def suggest_meeting_from_transcript(transcript: str) -> dict:
    """
    Возвращает словарь: {"title": str, "start_local": "YYYY-MM-DD HH:MM", "timezone": "Europe/Moscow", "duration_min": int}
    Если предложение не найдено — выбрасывает исключение.
    """
    client = get_mistral_client()
    model = os.getenv("MISTRAL_MODEL", "mistral-medium")
    tz_name = os.getenv("MEETINGS_TZ", os.getenv("TZ", "Europe/Moscow"))
    now_local = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    system = (
        "Ты планировщик встреч. НИЧЕГО НЕ ДОДУМЫВАЙ. "
        "Верни одну встречу в JSON. Если дата произносится относительно (например 'в пятницу'), "
        "интерпретируй её относительно текущего момента и ЧАСОВОГО ПОЯСА. Если год не указан — используй текущий год; "
        "если получилась прошедшая дата — выбери ближайшую будущую. Длительность одна из {15,30,45,60}."
    )
    user = (
        f"Сейчас: {now_local} ({tz_name}).\n"
        "Транскрипт:\n" + transcript + "\n\n"
        "Формат JSON: {\"title\":\"...\", \"start_local\":\"YYYY-MM-DD HH:MM\", \"timezone\":\"Europe/Moscow\", \"duration_min\":30}"
    )
    resp = client.chat(
        model=model,
        messages=[
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ],
        temperature=0.0,
    )
    content = resp.choices[0].message.content or ""  # type: ignore[index]
    import json

    def _extract_json(text: str) -> dict:
        # 1) fenced code block ```json ... ```
        m = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        if m:
            return json.loads(m.group(1).strip())
        # 2) any fenced block ``` ... ```
        m = re.search(r"```\s*([\s\S]*?)```", text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except Exception:
                pass
        # 3) first {...} block
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        # 4) direct parse
        return json.loads(text)

    data = _extract_json(content)
    if not isinstance(data, dict):
        raise ValueError("invalid meeting json")
    return data


def suggest_meetings_from_transcript(transcript: str) -> list[dict]:
    """
    Возвращает список встреч из транскрипта БЕЗ домысливаний.
    Требуемый формат элемента:
    {"title": str, "start_local": "YYYY-MM-DD HH:MM", "timezone": "Europe/Moscow", "duration_min": int}
    Учитывать только явно названные во входе данные. Если информации недостаточно — не включать встречу.
    Возвращай ТОЛЬКО JSON-массив без префиксов/комментариев. Язык входа — русский.
    """
    client = get_mistral_client()
    model = os.getenv("MISTRAL_MODEL", "mistral-medium")
    tz_name = os.getenv("MEETINGS_TZ", os.getenv("TZ", "Europe/Moscow"))
    now_local = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    system = (
        "Ты извлекаешь ВСТРЕЧИ из транскрипта. НИЧЕГО НЕ ДОДУМЫВАЙ. "
        "Интерпретируй относительные даты/дни недели относительно текущего момента и указанного часового пояса. "
        "Если год не указан — используй текущий год; если дата получилась в прошлом — выбери ближайшую будущую. "
        "Добавляй встречу ТОЛЬКО если есть явные данные. Верни JSON-массив объектов с полями title, start_local (YYYY-MM-DD HH:MM), timezone (Europe/Moscow), duration_min (15/30/45/60)."
    )
    user = (
        f"Сейчас: {now_local} ({tz_name}).\n"
        "Транскрипт:\n" + transcript + "\n\n"
        "Возврати JSON-массив без текста вокруг."
    )
    resp = client.chat(
        model=model,
        messages=[
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ],
        temperature=0.0,
    )
    content = resp.choices[0].message.content or ""
    import json

    def _extract_array(text: str) -> list[dict]:
        m = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        if m:
            return json.loads(m.group(1).strip())
        m = re.search(r"```\s*([\s\S]*?)```", text)
        if m:
            return json.loads(m.group(1).strip())
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            return json.loads(m.group(0))
        return json.loads(text)

    data = _extract_array(content)
    if not isinstance(data, list):
        raise ValueError("invalid meetings json")
    # Нормализуем элементы
    cleaned: list[dict] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        start_local = (it.get("start_local") or "").strip()
        tz = (it.get("timezone") or "Europe/Moscow").strip() or "Europe/Moscow"
        duration_min = int(it.get("duration_min") or 30)
        if title and start_local:
            cleaned.append({
                "title": title,
                "start_local": start_local,
                "timezone": tz,
                "duration_min": duration_min,
            })
    return cleaned


