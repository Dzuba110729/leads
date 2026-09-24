"""Расшифровка голосовых сообщений лидов локально (faster-whisper): аудио не уходит в сторонние сервисы.

Модель грузится при первом голосовом (~0.5 ГБ скачивается один раз в кэш huggingface) и дальше
живёт в памяти процесса. Вызовы синхронные — из asyncio звать через asyncio.to_thread.
"""
from __future__ import annotations

import logging
import threading

from config import CONFIG

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(CONFIG.whisper_model, device="cpu", compute_type="int8")
        return _model


def transcribe(path: str) -> str | None:
    """Текст голосового или None, если расшифровать не вышло (нет библиотеки, тишина, битый файл)."""
    if not available():
        return None
    try:
        segments, _ = _get_model().transcribe(path, language="ru", vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
    except Exception:
        logger.exception("voice: transcription failed for %s", path)
        return None
    return text or None
