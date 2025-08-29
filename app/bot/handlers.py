from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from aiogram import Router, types, F
import asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from sqlalchemy import select, or_

from app.db.session import session_factory
from app.db.models import User, Meeting
from app.stt.vosk_engine import recognize_speech_ru
from app.mistral_client import summarize_tasks, suggest_meeting_from_transcript, suggest_meetings_from_transcript


router = Router()


def _public_url() -> str:
    return os.getenv("APP_PUBLIC_URL", "http://localhost:8000")


CREATE_BTN = "➕ Создать встречу"

def _reply_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text=CREATE_BTN)]],
        resize_keyboard=True,
        input_field_placeholder="Быстрые действия",
        selective=False,
        one_time_keyboard=False,
    )


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    base = _public_url().rstrip("/")
    tg_id = message.from_user.id if message.from_user else None
    oauth_url = f"{base}/oauth/google/start?tg_id={tg_id}"

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Подключить календарь", url=oauth_url)]]
    )
    try:
        await message.answer(
            "Привет! Я помогу синхронизировать встречи и напоминания.\n"
            "Нажми кнопку ниже, чтобы подключить Google Календарь.",
            reply_markup=kb,
        )
    except TelegramBadRequest:
        # Фоллбек для localhost/недоступного домена: отправляем ссылку текстом
        await message.answer(
            "Привет! Я помогу синхронизировать встречи и напоминания.\n"
            "Telegram не принимает кнопку с локальным URL. Перейдите по ссылке для подключения: \n"
            f"{oauth_url}\n\n"
            "Подсказка: задайте APP_PUBLIC_URL на публичный HTTPS-домен (ngrok/cloudflared), затем /start."
        )

    # Показать клавиатуру быстрых действий
    try:
        await message.answer("Клавиатура быстрых действий включена.", reply_markup=_reply_kb())
    except Exception:
        pass


@router.message(Command("meetings"))
async def cmd_meetings(message: types.Message) -> None:
    if not message.from_user:
        await message.answer("Не удалось определить пользователя")
        return
    tg_id = message.from_user.id
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=7)

    async with session_factory() as session:
        user_res = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_res.scalar_one_or_none()
        if not user:
            await message.answer("Сначала подключите календарь командой /start")
            return

        # Показываем события, которые начинаются до конца окна и ещё не закончились
        q = (
            select(Meeting)
            .where(
                Meeting.user_id == user.id,
                Meeting.start_at <= until,
                or_(Meeting.end_at == None, Meeting.end_at >= now),
            )
            .order_by(Meeting.start_at.asc())
            .limit(10)
        )
        res = await session.execute(q)
        meetings = res.scalars().all()

    if not meetings:
        await message.answer("Ближайшие встречи не найдены в ближайшую неделю")
        return

    lines = []
    for m in meetings:
        start_str = m.start_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if m.start_at else "?"
        title = m.title or "(без названия)"
        lines.append(f"• {start_str} — {title}")

    await message.answer("Ближайшие встречи:\n" + "\n".join(lines))


@router.message(Command("settings"))
async def cmd_settings(message: types.Message) -> None:
    await message.answer(
        "Настройки напоминаний: по умолчанию за 1 день и 1 час. \n"
        "Изменение настроек будет добавлено позже."
    )
    try:
        await message.answer("Клавиатура быстрых действий включена.", reply_markup=_reply_kb())
    except Exception:
        pass


@router.callback_query(F.data.startswith("snooze:"))
async def on_snooze(callback: types.CallbackQuery) -> None:
    # формат: snooze:<notif_id>:<minutes>
    try:
        _, notif_id, minutes = callback.data.split(":", 2)  # type: ignore[union-attr]
        minutes = int(minutes)
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return

    async with session_factory() as session:
        from sqlalchemy import update, select
        from app.db.models import Notification
        res = await session.execute(select(Notification).where(Notification.id == int(notif_id)))
        notif = res.scalar_one_or_none()
        if not notif:
            await callback.answer("Уведомление не найдено", show_alert=True)
            return
        # переносим на +minutes
        from datetime import timedelta
        notif.scheduled_at = notif.scheduled_at + timedelta(minutes=minutes)  # type: ignore[operator]
        notif.sent_at = None
        await session.commit()

    await callback.answer("Отложено", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)



@router.message(F.voice | F.audio | F.video_note | F.video)
async def on_voice_or_audio(message: types.Message) -> None:
    target = message.voice or message.audio or message.video_note or message.video
    if not target:
        return

    from io import BytesIO

    # Статус распознавания
    progress: types.Message | None = None
    try:
        progress = await message.answer("Получил файл. Шаг 1/4: скачивание…")
    except Exception:
        progress = None

    bio = BytesIO()
    try:
        await message.bot.download(target, destination=bio)
    except Exception:
        await message.answer("Не удалось скачать файл из Telegram")
        return

    if progress:
        try:
            await progress.edit_text("Шаг 2/4: подготовка аудио (ffmpeg)…")
        except Exception:
            pass

    audio_bytes = bio.getvalue()
    try:
        if progress:
            try:
                await progress.edit_text("Шаг 3/4: распознавание…")
            except Exception:
                pass
        text = await asyncio.to_thread(recognize_speech_ru, audio_bytes)
    except RuntimeError as e:
        await message.answer(str(e))
        return
    except Exception as e:
        await message.answer(f"Ошибка распознавания аудио: {e}")
        return

    if text:
        # Саммари через Mistral (в фоне, чтобы не блокировать event loop)
        try:
            if progress:
                try:
                    await progress.edit_text("Шаг 4/4: формирование саммари…")
                except Exception:
                    pass
            summary = await asyncio.to_thread(summarize_tasks, text)
        except Exception as e:
            summary = f"(Ошибка саммаризации: {e})"
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="Создать встречу по этому диалогу", callback_data=f"mkmeet:{message.message_id}")]]
        )
        # Сохраним транскрипт в скрытый reply-to state через message_id
        await message.answer(
            "Распознал:\n" + text + "\n\n" + "Итоги и задачи:\n" + summary,
            reply_markup=kb,
        )
        # Кэшируем транскрипт в памяти процесса на короткое время
        _TRANSCRIPTS[message.message_id] = text
        if progress:
            try:
                await progress.delete()
            except Exception:
                pass
    else:
        await message.answer("Не удалось распознать речь")
        if progress:
            try:
                await progress.delete()
            except Exception:
                pass


@router.message(F.document)
async def on_audio_document(message: types.Message) -> None:
    doc = message.document
    if not doc:
        return
    mt = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    allowed_ext = (".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".webm", ".mp4", ".mov", ".mkv", ".avi")
    if not (mt.startswith("audio/") or mt.startswith("video/") or any(name.endswith(ext) for ext in allowed_ext)):
        return

    from io import BytesIO

    progress: types.Message | None = None
    try:
        progress = await message.answer("Получил файл. Шаг 1/4: скачивание…")
    except Exception:
        progress = None

    bio = BytesIO()
    try:
        await message.bot.download(doc, destination=bio)
    except Exception:
        await message.answer("Не удалось скачать файл из Telegram")
        return

    if progress:
        try:
            await progress.edit_text("Шаг 2/4: подготовка аудио (ffmpeg)…")
        except Exception:
            pass

    audio_bytes = bio.getvalue()
    try:
        if progress:
            try:
                await progress.edit_text("Шаг 3/4: распознавание…")
            except Exception:
                pass
        text = await asyncio.to_thread(recognize_speech_ru, audio_bytes)
    except RuntimeError as e:
        await message.answer(str(e))
        return
    except Exception as e:
        await message.answer(f"Ошибка распознавания аудио: {e}")
        return

    if text:
        try:
            if progress:
                try:
                    await progress.edit_text("Шаг 4/4: формирование саммари…")
                except Exception:
                    pass
            summary = await asyncio.to_thread(summarize_tasks, text)
        except Exception as e:
            summary = f"(Ошибка саммаризации: {e})"
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="Создать встречу по этому диалогу", callback_data=f"mkmeet:{message.message_id}")]]
        )
        await message.answer(
            "Распознал:\n" + text + "\n\n" + "Итоги и задачи:\n" + summary,
            reply_markup=kb,
        )
        _TRANSCRIPTS[message.message_id] = text
        if progress:
            try:
                await progress.delete()
            except Exception:
                pass
    else:
        await message.answer("Не удалось распознать речь")
        if progress:
            try:
                await progress.delete()
            except Exception:
                pass


@router.message(F.text.regexp(r"^.+\|\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|\s*\d+$"))
async def on_edit_submit(message: types.Message) -> None:
    # Проверяем, ждём ли мы редактирование от этого пользователя
    if not message.from_user:
        return
    tok = _EDIT_CONTEXT.pop(message.from_user.id, None)
    if not tok:
        return
    payload = _MEETING_PROPOSALS.get(tok)
    if not payload:
        await message.answer("Истёк кэш редактирования")
        return

    try:
        title_part, dt_part, dur_part = [p.strip() for p in message.text.split("|", 2)]  # type: ignore[union-attr]
        duration_min = int(dur_part)
        from datetime import datetime, timezone, timedelta
        import zoneinfo
        tz = zoneinfo.ZoneInfo(payload.get("tz") or "Europe/Moscow")
        dt_local = datetime.strptime(dt_part, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        start_utc = dt_local.astimezone(timezone.utc)
        end_utc = (dt_local + timedelta(minutes=duration_min)).astimezone(timezone.utc)
    except Exception:
        await message.answer("Неверный формат. Пример: Встреча | 2025-08-30 14:00 | 30")
        return

    payload["title"] = title_part
    payload["start_local"] = dt_part
    payload["duration_min"] = duration_min
    payload["start_utc"] = start_utc
    payload["end_utc"] = end_utc

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"mkmeet_confirm:{tok}")]]
    )
    await message.answer("Обновлено. Нажмите Подтвердить для создания встречи.", reply_markup=kb)


# простейший кэш транскриптов в памяти процесса
_TRANSCRIPTS: dict[int, str] = {}
# кэш подготовленных предложений встреч
_MEETING_PROPOSALS: dict[str, dict] = {}
_EDIT_CONTEXT: dict[int, dict] = {}


@router.callback_query(F.data.startswith("mkmeet:"))
async def on_create_meeting(callback: types.CallbackQuery) -> None:
    try:
        _, mid = callback.data.split(":", 1)  # type: ignore[union-attr]
        mid_i = int(mid)
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return

    transcript = _TRANSCRIPTS.get(mid_i)
    if not transcript:
        await callback.answer("Истёк кэш. Отправьте аудио заново.", show_alert=True)
        return

    # Предложение встречи от Mistral
    try:
        meetings = await asyncio.to_thread(suggest_meetings_from_transcript, transcript)
    except Exception as e:
        await callback.answer(f"Ошибка планировщика: {e}", show_alert=True)
        return

    if not meetings:
        await callback.answer("Не найдено явных встреч в тексте", show_alert=True)
        return

    # Парсим локальное время в UTC
    from datetime import datetime, timedelta, timezone
    import zoneinfo
    # Подготовим список встреч с возможностью редактирования
    import secrets
    import zoneinfo
    preview_lines: list[str] = ["Найдены встречи:"]
    kb_rows: list[list[types.InlineKeyboardButton]] = []
    batch_tokens: list[str] = []
    for idx, m in enumerate(meetings, start=1):
        title = m.get("title") or "Встреча"
        start_local = m.get("start_local")
        tz_name = (m.get("timezone") or "Europe/Moscow")
        duration_min = int(m.get("duration_min") or 30)
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
            dt_local = datetime.strptime(start_local, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            start_utc = dt_local.astimezone(timezone.utc)
            end_utc = (dt_local + timedelta(minutes=duration_min)).astimezone(timezone.utc)
        except Exception:
            continue

        token_id = secrets.token_urlsafe(6)
        _MEETING_PROPOSALS[token_id] = {
            "title": title,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "start_local": start_local,
            "tz": tz_name,
            "duration_min": duration_min,
            "tg_user_id": callback.from_user.id if callback.from_user else None,
            "origin_chat_id": None,
            "origin_message_id": None,
            "order": idx,
        }
        batch_tokens.append(token_id)
        preview_lines.append(
            f"{idx}) {title} — {start_local} МСК ({duration_min} мин)"
        )
        kb_rows.append([
            types.InlineKeyboardButton(text=f"✏️ Название {idx}", callback_data=f"mkmeet_edit_title:{token_id}"),
            types.InlineKeyboardButton(text=f"📅 Дата {idx}", callback_data=f"mkmeet_edit_date:{token_id}"),
        ])
        kb_rows.append([
            types.InlineKeyboardButton(text=f"⏰ Время {idx}", callback_data=f"mkmeet_edit_time:{token_id}"),
            types.InlineKeyboardButton(text=f"⏳ Длит. {idx}", callback_data=f"mkmeet_edit_dur:{token_id}"),
        ])
        kb_rows.append([types.InlineKeyboardButton(text=f"✅ Подтвердить {idx}", callback_data=f"mkmeet_confirm:{token_id}")])

    if len(kb_rows) == 0:
        await callback.answer("Не удалось распарсить встречи", show_alert=True)
        return
    kb_rows.append([types.InlineKeyboardButton(text="Отмена", callback_data="mkmeet_cancel:all")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    sent = await callback.message.answer("\n".join(preview_lines), reply_markup=kb)
    # проставим источник карточки в кэш
    for t in batch_tokens:
        if t in _MEETING_PROPOSALS:
            _MEETING_PROPOSALS[t]["origin_chat_id"] = sent.chat.id
            _MEETING_PROPOSALS[t]["origin_message_id"] = sent.message_id
    await callback.answer()


@router.callback_query(F.data.startswith("mkmeet_cancel:"))
async def on_cancel_meeting(callback: types.CallbackQuery) -> None:
    try:
        _, tok = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Операция отменена", show_alert=False)
        return
    if tok == "all":
        _MEETING_PROPOSALS.clear()
    else:
        _MEETING_PROPOSALS.pop(tok, None)
    await callback.answer("Отменено", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)


@router.message(F.text == CREATE_BTN)
async def on_create_from_keyboard(message: types.Message) -> None:
    # Создаём пустой черновик одной встречи и рисуем карточку как при распознавании
    import secrets
    from datetime import datetime, timedelta, timezone
    import zoneinfo

    tz_name = "Europe/Moscow"
    tz = zoneinfo.ZoneInfo(tz_name)
    now_local = datetime.now(tz).replace(second=0, microsecond=0)
    start_local = now_local.strftime("%Y-%m-%d %H:%M")
    duration_min = 30
    start_utc = now_local.astimezone(timezone.utc)
    end_utc = (now_local + timedelta(minutes=duration_min)).astimezone(timezone.utc)

    token_id = secrets.token_urlsafe(6)
    _MEETING_PROPOSALS[token_id] = {
        "title": "Встреча",
        "start_utc": start_utc,
        "end_utc": end_utc,
        "start_local": start_local,
        "tz": tz_name,
        "duration_min": duration_min,
        "tg_user_id": message.from_user.id if message.from_user else None,
        "origin_chat_id": None,
        "origin_message_id": None,
        "order": 1,
    }

    lines = ["Найдены встречи:", f"1) Встреча — {start_local} МСК ({duration_min} мин)"]
    kb_rows: list[list[types.InlineKeyboardButton]] = []
    kb_rows.append([
        types.InlineKeyboardButton(text="✏️ Название 1", callback_data=f"mkmeet_edit_title:{token_id}"),
        types.InlineKeyboardButton(text="📅 Дата 1", callback_data=f"mkmeet_edit_date:{token_id}"),
    ])
    kb_rows.append([
        types.InlineKeyboardButton(text="⏰ Время 1", callback_data=f"mkmeet_edit_time:{token_id}"),
        types.InlineKeyboardButton(text="⏳ Длит. 1", callback_data=f"mkmeet_edit_dur:{token_id}"),
    ])
    kb_rows.append([types.InlineKeyboardButton(text="✅ Подтвердить 1", callback_data=f"mkmeet_confirm:{token_id}")])
    kb_rows.append([types.InlineKeyboardButton(text="Отмена", callback_data="mkmeet_cancel:all")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    sent = await message.answer("\n".join(lines), reply_markup=kb)
    _MEETING_PROPOSALS[token_id]["origin_chat_id"] = sent.chat.id
    _MEETING_PROPOSALS[token_id]["origin_message_id"] = sent.message_id

@router.callback_query(F.data.startswith("mkmeet_confirm:"))
async def on_confirm_meeting(callback: types.CallbackQuery) -> None:
    try:
        _, tok = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return
    payload = _MEETING_PROPOSALS.pop(tok, None)
    if not payload:
        await callback.answer("Нечего подтверждать (истёк кэш)", show_alert=True)
        return

    title = payload["title"]
    start_utc = payload["start_utc"]
    end_utc = payload["end_utc"]

    # Найти пользователя и создать событие в Google
    if not callback.from_user:
        await callback.answer("Неизвестный пользователь", show_alert=True)
        return
    tg_id = callback.from_user.id

    async with session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        if not user:
            await callback.answer("Сначала подключите Google календарь через /start", show_alert=True)
            return

        # Создание события через Google API
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request as GoogleRequest

        from app.db.models import OAuthToken
        result = await session.execute(
            select(OAuthToken).where(OAuthToken.user_id == user.id, OAuthToken.provider == "google")
        )
        token = result.scalar_one_or_none()
        if not token:
            await callback.answer("Нет подключения Google", show_alert=True)
            return

        creds = Credentials(
            token=token.access_token,
            refresh_token=token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        )
        if creds.expired and creds.refresh_token:
            await asyncio.to_thread(creds.refresh, GoogleRequest())
            token.access_token = creds.token
            if creds.expiry:
                token.expires_at = creds.expiry
            await session.commit()

        def _insert_event() -> str:
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            body = {
                "summary": title,
                "description": "Создано из голосовой заметки",
                "start": {"dateTime": start_utc.isoformat()},
                "end": {"dateTime": end_utc.isoformat()},
            }
            ev = service.events().insert(calendarId="primary", body=body).execute()  # type: ignore[no-untyped-call]
            return ev.get("id", "")

        try:
            _ = await asyncio.to_thread(_insert_event)
        except Exception as e:
            msg = str(e)
            if "Insufficient Permission" in msg or "insufficientPermissions" in msg:
                await callback.answer(
                    "Недостаточно прав Google Calendar. Переподключите календарь через /start",
                    show_alert=True,
                )
            else:
                await callback.answer("Ошибка Google Calendar. Попробуйте позже.", show_alert=True)
            return

        await callback.answer("Встреча создана", show_alert=False)
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("mkmeet_edit_title:"))
async def on_edit_title(callback: types.CallbackQuery) -> None:
    try:
        _, tok = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return
    # удалить прошлый промпт, если был
    ctx_prev = _EDIT_CONTEXT.get(callback.from_user.id or 0)
    if ctx_prev and ctx_prev.get("prompt_chat_id") and ctx_prev.get("prompt_message_id"):
        try:
            await callback.bot.delete_message(chat_id=ctx_prev["prompt_chat_id"], message_id=ctx_prev["prompt_message_id"])  # type: ignore[index]
        except Exception:
            pass
    sent = await callback.message.answer("Введите новое название встречи одним сообщением")
    _EDIT_CONTEXT[callback.from_user.id] = {"tok": tok, "field": "title", "prompt_chat_id": sent.chat.id, "prompt_message_id": sent.message_id}  # type: ignore[index]
    await callback.answer()


@router.callback_query(F.data.startswith("mkmeet_edit_date:"))
async def on_edit_date(callback: types.CallbackQuery) -> None:
    try:
        _, tok = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return
    ctx_prev = _EDIT_CONTEXT.get(callback.from_user.id or 0)
    if ctx_prev and ctx_prev.get("prompt_chat_id") and ctx_prev.get("prompt_message_id"):
        try:
            await callback.bot.delete_message(chat_id=ctx_prev["prompt_chat_id"], message_id=ctx_prev["prompt_message_id"])  # type: ignore[index]
        except Exception:
            pass
    sent = await callback.message.answer("Введите дату в формате YYYY-MM-DD (МСК)")
    _EDIT_CONTEXT[callback.from_user.id] = {"tok": tok, "field": "date", "prompt_chat_id": sent.chat.id, "prompt_message_id": sent.message_id}  # type: ignore[index]
    await callback.answer()


@router.callback_query(F.data.startswith("mkmeet_edit_time:"))
async def on_edit_time(callback: types.CallbackQuery) -> None:
    try:
        _, tok = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return
    ctx_prev = _EDIT_CONTEXT.get(callback.from_user.id or 0)
    if ctx_prev and ctx_prev.get("prompt_chat_id") and ctx_prev.get("prompt_message_id"):
        try:
            await callback.bot.delete_message(chat_id=ctx_prev["prompt_chat_id"], message_id=ctx_prev["prompt_message_id"])  # type: ignore[index]
        except Exception:
            pass
    sent = await callback.message.answer("Введите время в формате HH:MM (МСК)")
    _EDIT_CONTEXT[callback.from_user.id] = {"tok": tok, "field": "time", "prompt_chat_id": sent.chat.id, "prompt_message_id": sent.message_id}  # type: ignore[index]
    await callback.answer()


@router.callback_query(F.data.startswith("mkmeet_edit_dur:"))
async def on_edit_dur(callback: types.CallbackQuery) -> None:
    try:
        _, tok = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return
    ctx_prev = _EDIT_CONTEXT.get(callback.from_user.id or 0)
    if ctx_prev and ctx_prev.get("prompt_chat_id") and ctx_prev.get("prompt_message_id"):
        try:
            await callback.bot.delete_message(chat_id=ctx_prev["prompt_chat_id"], message_id=ctx_prev["prompt_message_id"])  # type: ignore[index]
        except Exception:
            pass
    sent = await callback.message.answer("Введите длительность в минутах (15/30/45/60)")
    _EDIT_CONTEXT[callback.from_user.id] = {"tok": tok, "field": "dur", "prompt_chat_id": sent.chat.id, "prompt_message_id": sent.message_id}  # type: ignore[index]
    await callback.answer()


@router.message(F.text)
async def on_edit_text(message: types.Message) -> None:
    if not message.from_user:
        return
    ctx = _EDIT_CONTEXT.get(message.from_user.id)
    if not ctx:
        return
    tok = ctx.get("tok")
    field = ctx.get("field")
    payload = _MEETING_PROPOSALS.get(tok)
    if not tok or not payload:
        _EDIT_CONTEXT.pop(message.from_user.id, None)
        return

    val = (message.text or "").strip()
    from datetime import datetime, timezone, timedelta
    import zoneinfo
    tz = zoneinfo.ZoneInfo(payload.get("tz") or "Europe/Moscow")

    try:
        if field == "title":
            if not val:
                raise ValueError
            payload["title"] = val
        elif field == "date":
            # keep existing time
            tpart = payload.get("start_local", "00:00").split(" ")[-1]
            dt_local = datetime.strptime(val + " " + tpart, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            payload["start_local"] = val + " " + tpart
            payload["start_utc"] = dt_local.astimezone(timezone.utc)
            payload["end_utc"] = (dt_local + timedelta(minutes=int(payload.get("duration_min") or 30))).astimezone(timezone.utc)
        elif field == "time":
            dpart = payload.get("start_local", "1970-01-01 00:00").split(" ")[0]
            dt_local = datetime.strptime(dpart + " " + val, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            payload["start_local"] = dpart + " " + val
            payload["start_utc"] = dt_local.astimezone(timezone.utc)
            payload["end_utc"] = (dt_local + timedelta(minutes=int(payload.get("duration_min") or 30))).astimezone(timezone.utc)
        elif field == "dur":
            mins = int(val)
            if mins not in (15, 30, 45, 60):
                raise ValueError
            payload["duration_min"] = mins
            # recalc end
            dpart = payload.get("start_local", "1970-01-01 00:00").split(" ")[0]
            tpart = payload.get("start_local", "1970-01-01 00:00").split(" ")[-1]
            dt_local = datetime.strptime(dpart + " " + tpart, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            payload["end_utc"] = (dt_local + timedelta(minutes=mins)).astimezone(timezone.utc)
        else:
            return
    except Exception:
        if field == "title":
            await message.answer("Введите непустое название")
        elif field == "date":
            await message.answer("Неверный формат даты. Пример: 2025-08-31")
        elif field == "time":
            await message.answer("Неверный формат времени. Пример: 14:30")
        elif field == "dur":
            await message.answer("Длительность только 15/30/45/60")
        return

    # удаляем сообщение пользователя
    # удаляем подсказку-промпт и пользовательское сообщение
    ctx_prompt = ctx.get("prompt_chat_id"), ctx.get("prompt_message_id")
    if ctx_prompt[0] and ctx_prompt[1]:
        try:
            await message.bot.delete_message(chat_id=ctx_prompt[0], message_id=ctx_prompt[1])
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass
    _EDIT_CONTEXT.pop(message.from_user.id, None)

    # перерисовываем старую карточку
    chat_id = payload.get("origin_chat_id")
    msg_id = payload.get("origin_message_id")
    if chat_id and msg_id:
        # собрать весь список, отсортировав по order
        items = [v for k, v in _MEETING_PROPOSALS.items() if v.get("origin_message_id") == msg_id]
        items.sort(key=lambda x: x.get("order", 0))
        lines = ["Найдены встречи:"]
        kb_rows: list[list[types.InlineKeyboardButton]] = []
        for it in items:
            idx = it.get("order")
            lines.append(f"{idx}) {it['title']} — {it['start_local']} МСК ({it['duration_min']} мин)")
            tok2 = None
            # найдём токен по объекту
            for k, v in _MEETING_PROPOSALS.items():
                if v is it:
                    tok2 = k
                    break
            if not tok2:
                continue
            kb_rows.append([
                types.InlineKeyboardButton(text=f"✏️ Название {idx}", callback_data=f"mkmeet_edit_title:{tok2}"),
                types.InlineKeyboardButton(text=f"📅 Дата {idx}", callback_data=f"mkmeet_edit_date:{tok2}"),
            ])
            kb_rows.append([
                types.InlineKeyboardButton(text=f"⏰ Время {idx}", callback_data=f"mkmeet_edit_time:{tok2}"),
                types.InlineKeyboardButton(text=f"⏳ Длит. {idx}", callback_data=f"mkmeet_edit_dur:{tok2}"),
            ])
            kb_rows.append([types.InlineKeyboardButton(text=f"✅ Подтвердить {idx}", callback_data=f"mkmeet_confirm:{tok2}")])
        if kb_rows:
            kb_rows.append([types.InlineKeyboardButton(text="Отмена", callback_data="mkmeet_cancel:all")])
            kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
            try:
                await message.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="\n".join(lines), reply_markup=kb)
            except Exception:
                pass


@router.callback_query(F.data.startswith("ack:"))
async def on_ack(callback: types.CallbackQuery) -> None:
    # формат: ack:<notif_id>
    try:
        _, notif_id = callback.data.split(":", 1)  # type: ignore[union-attr]
    except Exception:
        await callback.answer("Ошибка параметров", show_alert=True)
        return

    async with session_factory() as session:
        from sqlalchemy import select
        from app.db.models import Notification
        res = await session.execute(select(Notification).where(Notification.id == int(notif_id)))
        notif = res.scalar_one_or_none()
        if not notif:
            await callback.answer("Уведомление не найдено", show_alert=True)
            return
        notif.status = "ack"
        notif.sent_at = notif.sent_at or datetime.now(timezone.utc)
        await session.commit()

    await callback.answer("Ок", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)


