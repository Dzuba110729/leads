"""Конфигурация из переменных окружения, безопасные дефолты."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).with_name(".env")

load_dotenv(ENV_PATH)


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
    # Бот-кассир выключен (2026-09-28): горячего лида доводит до звонка специалиста сам продавец,
    # ссылку на бота лиду не шлём, статусы заявки ему не рассылаем. Токен оставлен — включить: 1.
    cashier_bot_enabled: bool = os.getenv("CASHIER_BOT_ENABLED", "0") == "1"

    @property
    def cashier_bot_active(self) -> bool:
        return self.cashier_bot_enabled and bool(self.bot_token)

    # LLM
    llm_api_key: str = os.getenv("ANTHROPIC_API_KEY", os.getenv("LLM_API_KEY", ""))
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-5")
    # Дешёвая модель для предфильтра сборщика: отсеивает явный мусор до дорогого разбора
    llm_model_cheap: str = os.getenv("LLM_MODEL_CHEAP", "claude-haiku-4-5-20251001")
    # Чем ловец разбирает чаты: claude_code — Claude Code CLI по подписке владельца (claude_code.py),
    # api — через ANTHROPIC_API_KEY, как продающий бот.
    catcher_llm_backend: str = os.getenv("CATCHER_LLM_BACKEND", "claude_code")
    claude_bin: str = os.getenv("CLAUDE_BIN", "")
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
    # Полный список из 20 категорий брифа (БРИФ_OG1_ЗАПОЛНЕННЫЙ.md п.1.5), один плоский список,
    # без разной логики реакции по категории — решение заказчика 2026-08-12, MVP.
    group_signal_words: list[str] = field(
        default_factory=lambda: _list(
            "GROUP_SIGNAL_WORDS",
            "онлайн-школа,онлайн школа,дистанционная школа,школа онлайн,учиться онлайн,"
            "обучение онлайн,дистанционное обучение,дистант,удалённое обучение,школа удалённо,"
            "семейное обучение,семейное образование,семейная форма,перейти на семейное,"
            "уйти на семейное,со,хоумскулинг,homeschooling,учимся дома,обучение дома,"
            "сменить школу,поменять школу,ищем другую школу,какую школу выбрать,посоветуйте школу,"
            "хорошая школа,куда перевести ребёнка,перевод в другую школу,забрать из школы,"
            "уйти из школы,перевести ребёнка,перевести ребенка,"
            "не нравится школа,плохая школа,проблемы в школе,не устраивает школа,"
            "учителя не объясняют,плохой учитель,конфликт с учителем,конфликт с классным руководителем,"
            "школа не подходит,"
            "буллинг,буллят,травля,травят в школе,издеваются,обижают в школе,"
            "конфликт с одноклассниками,боится идти в школу,не хочет ходить в школу,"
            "проблемы с одноклассниками,"
            "не хочет учиться,ненавидит школу,стресс из-за школы,устал от школы,перегруз в школе,"
            "не справляется со школой,много домашки,школа отнимает весь день,потерял мотивацию,"
            "переезжаем,переезд,часто переезжаем,переехали в другой город,переезд в другую страну,"
            "живём за границей,уезжаем за границу,релокация,эмиграция,экспаты,"
            "российская школа за границей,русская школа за границей,российский аттестат за границей,"
            "учиться по российской программе,ребёнок живёт за границей,русское образование за границей,"
            "профессиональный спорт,спортивные сборы,соревнования,тренировки каждый день,"
            "школа мешает тренировкам,юный спортсмен,спортивная карьера,музыкальная школа,"
            "гастроли,съёмки,"
            "гибкий график обучения,свободный график,индивидуальный график,учиться в своём темпе,"
            "совмещать школу,пропускает уроки,записи уроков,"
            "часто пропускает школу,много пропусков,не может посещать школу,пропускает занятия,"
            "отстал из-за пропусков,как догнать программу,"
            "аттестация,промежуточная аттестация,пройти аттестацию,прикрепиться к школе,"
            "школа для аттестации,аттестация дистанционно,аттестация на семейном,"
            "зачисление в онлайн-школу,официальная онлайн-школа,аккредитованная онлайн-школа,"
            "лицензия школы,государственная аккредитация,личное дело,перевод в онлайн-школу,"
            "аттестат онлайн,государственный аттестат,аттестат государственного образца,"
            "получить аттестат дистанционно,аттестат после онлайн-школы,"
            "подготовка к огэ,огэ онлайн,как сдать огэ на семейном,где сдавать огэ,"
            "подготовиться к огэ,9 класс онлайн,"
            "подготовка к егэ,егэ онлайн,как сдать егэ на семейном,где сдавать егэ,"
            "подготовиться к егэ,10 класс онлайн,11 класс онлайн,"
            "сильные учителя,хорошее образование онлайн,качественная онлайн-школа,"
            "индивидуальный подход,маленькие классы,мало учеников в классе,хорошие преподаватели,"
            "как контролировать учёбу,ребёнок ничего не делает,нужен контроль,"
            "контроль успеваемости,кто следит за учёбой,нужен куратор,самостоятельность ребёнка,"
            "социализация на семейном,социализация в онлайн-школе,как общаться со сверстниками,"
            "нет друзей в школе,общение в онлайн-школе,кружки онлайн,"
            "сколько стоит онлайн-школа,стоимость онлайн-школы,цена обучения,тариф онлайн-школы,"
            "недорогая онлайн-школа,онлайн-школа отзывы,какую онлайн-школу выбрать,"
            "лучшая онлайн-школа,рейтинг онлайн-школ,отзывы онлайн-школа,кто учится в онлайн-школе,"
            "посоветуйте онлайн-школу,онлайн-школа отзывы родителей,сравнение онлайн-школ,"
            "фоксфорд,интернетурок,учи.дома,дом знаний,бит,алгоритм",
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
    # Без токена VK читается браузером (vk_browser.py) на профиле, куда оператор один раз
    # вошёл через vk_browser_login.py. Относительный путь — от папки проекта.
    vk_browser_profile_dir: str = os.getenv("VK_BROWSER_PROFILE_DIR", "secrets/vk_browser_profile")
    # Не анализируем сообщения старше этого срока - иначе первая выгрузка старого чата
    # начинает читать историю с самого начала (могут оказаться сообщения многолетней давности)
    catcher_max_message_age_days: int = int(os.getenv("CATCHER_MAX_MESSAGE_AGE_DAYS", "180"))

    # Полнота поиска лидов (см. диагностику 2026-09-17: один проход терял стабильно
    # находимые лиды, поэтому проходов несколько, а пачки идут внахлёст).
    p1_batch_size: int = int(os.getenv("P1_BATCH_SIZE", "30"))
    # Нахлёст соседних пачек: диалог, разрезанный границей, целиком попадает хотя бы в одну
    p1_batch_overlap: int = int(os.getenv("P1_BATCH_OVERLAP", "5"))
    # Сколько раз прогонять каждую пачку: ответ модели не дословно повторяем,
    # объединение находок за несколько проходов поднимает полноту
    p1_passes: int = int(os.getenv("P1_PASSES", "2"))
    # Предфильтр дешёвой моделью перед дорогим разбором
    p1_prefilter_enabled: bool = _bool("P1_PREFILTER_ENABLED", "1")
    p1_prefilter_batch_size: int = int(os.getenv("P1_PREFILTER_BATCH_SIZE", "60"))

    # ТГ-агент оператора: отдельный бот, слушает только перечисленные Telegram-ID
    agent_bot_token: str = os.getenv("AGENT_BOT_TOKEN", "")
    agent_allowed_ids: list[int] = field(
        default_factory=lambda: [int(x) for x in _list("AGENT_ALLOWED_IDS", "") if x.isdigit()]
    )
    # Megabitra (AlterCPA): готовый лид с телефоном отправляется в поток (megabitra.py).
    megabitra_api_key: str = os.getenv("MEGABITRA_API_KEY", "")
    megabitra_offer: str = os.getenv("MEGABITRA_OFFER", "")
    megabitra_flow: str = os.getenv("MEGABITRA_FLOW", "")
    megabitra_lead_ip: str = os.getenv("MEGABITRA_LEAD_IP", "")
    megabitra_country: str = os.getenv("MEGABITRA_COUNTRY", "")
    # Лид без телефона (выбрал тг/почту): API требует phone — ставим заглушку, способ связи в комментарии.
    megabitra_no_phone: str = os.getenv("MEGABITRA_NO_PHONE", "")
    # Кому бот @BOT_USERNAME шлёт карточки лидов, готовых к связи. По умолчанию — операторы ТГ-агента.
    ready_leads_chat_ids: list[int] = field(
        default_factory=lambda: [int(x) for x in _list("READY_LEADS_CHAT_IDS", os.getenv("AGENT_ALLOWED_IDS", "")) if x.isdigit()]
    )
    agent_model: str = os.getenv("AGENT_MODEL", "claude-sonnet-5")
    # Модель faster-whisper для расшифровки голосовых лидов (локально, аудио никуда не уходит)
    whisper_model: str = os.getenv("WHISPER_MODEL", "small")

    # Google Docs для отчётов агента. Основной путь — вход от имени владельца (OAuth-клиент
    # типа Desktop + токен из google_login.py): документы лежат на его Диске. Сервисный
    # аккаунт — запасной вариант только для Google Workspace (у обычного Gmail у робота
    # нулевая квота Диска, создание файла падает с storageQuotaExceeded).
    google_oauth_client_file: str = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "")
    google_oauth_token_file: str = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "secrets/google-token.json")
    google_service_account_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    google_share_emails: list[str] = field(default_factory=lambda: _list("GOOGLE_SHARE_EMAILS", ""))
    google_drive_folder_id: str = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")


CONFIG = Config()


def save_env_value(name: str, value: str, env_path: Path | None = None) -> None:
    """Записывает NAME=value в .env (заменяет строку или дописывает), чтобы переключатели
    из ТГ-агента переживали перезапуск. Остальные строки файла не трогает."""
    path = env_path or ENV_PATH
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{name}={value}"
    pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda _: line, text, count=1)
    else:
        text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    path.write_text(text, encoding="utf-8")
