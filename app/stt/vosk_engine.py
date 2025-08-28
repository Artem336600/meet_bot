from __future__ import annotations

import io
import json
import os
import threading
from typing import Optional
from pathlib import Path
import shutil

import ffmpeg  # type: ignore
from vosk import Model, KaldiRecognizer, SetLogLevel  # type: ignore


_model_lock = threading.Lock()
_model: Optional[Model] = None


def _get_model_path() -> str:
    # Можно задать через переменную окружения VOSK_MODEL_PATH
    return os.getenv("VOSK_MODEL_PATH", os.path.join("models", "vosk-model-small-ru-0.22"))


def _ensure_model_loaded() -> Model:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            # Уменьшаем уровень логирования Vosk
            try:
                SetLogLevel(-1)
            except Exception:
                pass
            path = _get_model_path()
            if not os.path.isdir(path):
                raise RuntimeError(
                    "Модель Vosk не найдена. Укажите путь в VOSK_MODEL_PATH и распакуйте модель, "
                    "например vosk-model-small-ru-0.22"
                )
            _model = Model(path)
    return _model  # type: ignore[return-value]


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _resolve_env_ffmpeg(env_value: str) -> Optional[str]:
    p = Path(env_value)
    # Если это файл и исполняемый — используем
    if _is_executable_file(p):
        return str(p)
    # Если это каталог — попробуем типичные варианты внутри
    if p.is_dir():
        candidates = [
            p / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
            p / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
        ]
        for c in candidates:
            if _is_executable_file(c):
                return str(c)
    # Если значение — путь типа .../ffmpeg без расширения и это каталог — уже выше обработали
    return None


def _get_ffmpeg_cmd() -> str:
    # 1) Явный путь/каталог в переменной окружения
    env_bin = os.getenv("FFMPEG_BINARY")
    if env_bin:
        # Если мы внутри Linux-контейнера, а значение похоже на Windows-путь, игнорируем
        if os.name != "nt" and (":\\" in env_bin or env_bin.startswith("C:/") or env_bin.startswith("c:/")):
            resolved = None
        else:
            resolved = _resolve_env_ffmpeg(env_bin)
        if resolved:
            return resolved

    # 2) В PATH системы
    which = shutil.which("ffmpeg")
    if which and _is_executable_file(Path(which)):
        return which

    # 3) Известные пути Linux/containers
    for known in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        if _is_executable_file(Path(known)):
            return known

    # 4) Локальная портативная установка в проекте: tools/**/bin/ffmpeg(.exe) или tools/**/ffmpeg(.exe)
    project_root = Path(__file__).resolve().parents[2]
    patterns = [
        "tools/**/bin/ffmpeg.exe",
        "tools/**/ffmpeg.exe",
        "tools/**/bin/ffmpeg",
        "tools/**/ffmpeg",
    ]
    for pattern in patterns:
        for candidate in project_root.glob(pattern):
            pc = Path(candidate)
            if _is_executable_file(pc):
                return str(pc)

    raise FileNotFoundError("ffmpeg executable not found")


def _convert_to_pcm16_mono16000(audio_bytes: bytes) -> bytes:
    try:
        ffmpeg_cmd = _get_ffmpeg_cmd()
        out, err = (
            ffmpeg.input("pipe:")
            .output(
                "pipe:",
                format="s16le",
                acodec="pcm_s16le",
                ac=1,
                ar=16000,
            )
            .run(capture_stdout=True, capture_stderr=True, input=audio_bytes, cmd=ffmpeg_cmd)
        )
    except FileNotFoundError as exc:  # ffmpeg не найден
        raise RuntimeError(
            "Не найден исполняемый файл ffmpeg. Установите ffmpeg, либо укажите путь в FFMPEG_BINARY, "
            "либо поместите портативную версию в каталог tools/"
        ) from exc
    except ffmpeg.Error as exc:  # type: ignore[attr-defined]
        stderr = getattr(exc, "stderr", b"")
        msg = stderr.decode(errors="ignore") if isinstance(stderr, (bytes, bytearray)) else str(exc)
        raise RuntimeError(f"Ошибка конвертации аудио через ffmpeg: {msg}")
    return out


def recognize_speech_ru(audio_bytes: bytes) -> str:
    """
    Распознаёт речь на русском языке из произвольного аудио (OGG/OPUS/MP3/MP4/WEBM/WAV ...).
    Возвращает распознанный текст (может быть пустой строкой).
    Выполняется синхронно; выносите в поток через asyncio.to_thread.
    """
    model = _ensure_model_loaded()

    # Конвертируем в требуемый PCM 16kHz mono
    pcm = _convert_to_pcm16_mono16000(audio_bytes)

    recognizer = KaldiRecognizer(model, 16000)

    # Кормим по кускам, чтобы избегать больших буферов
    chunk_size = 4000
    offset = 0
    partials: list[str] = []
    while offset < len(pcm):
        chunk = pcm[offset : offset + chunk_size]
        offset += chunk_size
        if recognizer.AcceptWaveform(chunk):
            try:
                res = json.loads(recognizer.Result())
                if isinstance(res, dict) and res.get("text"):
                    partials.append(res["text"])  # type: ignore[index]
            except Exception:
                pass

    try:
        final = json.loads(recognizer.FinalResult())
        final_text = final.get("text") if isinstance(final, dict) else ""
    except Exception:
        final_text = ""

    parts = [p for p in partials if p]
    if final_text:
        parts.append(final_text)
    return " ".join(parts).strip()


