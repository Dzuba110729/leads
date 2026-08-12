"""Конфигурация из переменных окружения, безопасные дефолты."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


@dataclass
class Config:
    # Telegram userbot (продавец)
    tg_api_id: int = int(os.getenv("TG_API_ID", "0") or 0)
    tg_api_hash: str = os.getenv("TG_API_HASH", "")
    tg_string_session: str = os.getenv("TG_STRING_SESSION", "")
    tg_phone: str = os.getenv("TG_PHONE", "")

    # Бот (хэндофф к менеджеру — см. og1/PLAN.md п.4: оплата не разовая, бот доводит до передачи)
    bot_token: str = os.getenv("BOT_TOKEN", "")
    bot_username: str = os.getenv("BOT_USERNAME", "og1_priemnaya_bot")

    # LLM
    llm_api_key: str = os.getenv("ANTHROPIC_API_KEY", os.getenv("LLM_API_KEY", ""))
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-5")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")

    # Хранение
    db_path: str = os.getenv("DB_PATH", "og1_leads.db")

    # Прочее
    operator_chat: str = os.getenv("OPERATOR_CHAT", "")
    crm_secret_key: str = os.getenv("CRM_SECRET_KEY", "dev-secret-change-me")
    crm_host: str = os.getenv("CRM_HOST", "127.0.0.1")
    crm_port: int = int(os.getenv("CRM_PORT", "8080"))

    # Безопасные дефолты — включаются осознанно после ручной обкатки
    dry_run: bool = _bool("DRY_RUN", "1")
    scheduler_enabled: bool = _bool("SCHEDULER_ENABLED", "0")

    # Группы: режим ответа off/reply/dm
    group_reply_mode: str = os.getenv("GROUP_REPLY_MODE", "off")
    group_signal_words: list[str] = field(
        default_factory=lambda: _list(
            "GROUP_SIGNAL_WORDS",
            "аттестация,дистант,дистанционное обучение,семейное обучение,со,перевести ребёнка,"
            "перевести ребенка,травля,буллинг,индивидуальный график,экстернат,подготовка к экзаменам",
        )
    )

    # Минимальный зазор между касаниями прогрева (часы)
    min_touch_gap_hours: int = int(os.getenv("MIN_TOUCH_GAP_HOURS", "20"))

    # Напоминания о незавершённой заявке/брони (минуты от старта)
    reminder_ladder_minutes: tuple[int, ...] = (30, 60 * 24, 60 * 24 * 3)
    reminder_autoclose_minutes: int = 60 * 24 * 3 + 30

    payment_base_url: str = os.getenv("PAYMENT_BASE_URL", "https://example.com/pay")

    # lead-catcher-auto: ОТДЕЛЬНЫЕ аккаунты от боевого userbot'а продаж (см. .scratch/lead-catcher-auto/spec.md)
    catcher_tg_api_id: int = int(os.getenv("CATCHER_TG_API_ID", "0") or 0)
    catcher_tg_api_hash: str = os.getenv("CATCHER_TG_API_HASH", "")
    catcher_tg_string_session: str = os.getenv("CATCHER_TG_STRING_SESSION", "")
    catcher_tg_phone: str = os.getenv("CATCHER_TG_PHONE", "")
    vk_access_token: str = os.getenv("VK_ACCESS_TOKEN", "")


CONFIG = Config()
