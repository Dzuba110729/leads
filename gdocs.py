"""Отчёт по найденным лидам в Google Docs.

Документ создаётся от имени владельца (OAuth-токен из google_login.py) и лежит на его Диске;
при желании открывается ещё людям из GOOGLE_SHARE_EMAILS и кладётся в папку GOOGLE_DRIVE_FOLDER_ID.
Все вызовы Google синхронные — снаружи оборачивать в asyncio.to_thread.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import catcher_db
from config import CONFIG

logger = logging.getLogger(__name__)

# drive.file — только файлы, созданные этим приложением: Google не требует проверки приложения
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


def available() -> bool:
    return bool(
        (CONFIG.google_oauth_client_file and Path(CONFIG.google_oauth_token_file).exists())
        or CONFIG.google_service_account_file
    )


def load_credentials():
    if CONFIG.google_oauth_client_file and Path(CONFIG.google_oauth_token_file).exists():
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(CONFIG.google_oauth_token_file, SCOPES)
        if creds.expired and creds.refresh_token:
            from google.auth.exceptions import RefreshError

            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError("вход в Google истёк или отозван — запустите google_login.py заново") from exc
            Path(CONFIG.google_oauth_token_file).write_text(creds.to_json(), encoding="utf-8")
        return creds
    if CONFIG.google_service_account_file:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(CONFIG.google_service_account_file, scopes=SCOPES)
    raise RuntimeError("Google не настроен: запустите google_login.py или укажите GOOGLE_SERVICE_ACCOUNT_FILE")


class DocBuilder:
    """Собирает текст и запросы форматирования. Индексы Docs API — в UTF-16, не в символах."""

    def __init__(self) -> None:
        self.text = ""
        self.requests: list[dict] = []
        self.index = 1

    @staticmethod
    def _length(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    def line(self, text: str = "", heading: str | None = None, bold_prefix: str | None = None, link: str | None = None) -> None:
        start = self.index
        full = (bold_prefix or "") + text + "\n"
        self.text += full
        end = start + self._length(full)
        self.index = end
        if heading:
            self.requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": heading},
                    "fields": "namedStyleType",
                }
            })
        if bold_prefix:
            self.requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": start + self._length(bold_prefix)},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        if link and text:
            text_start = start + self._length(bold_prefix or "")
            self.requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": text_start, "endIndex": text_start + self._length(text)},
                    "textStyle": {"link": {"url": link}},
                    "fields": "link",
                }
            })

    def batch(self) -> list[dict]:
        return [{"insertText": {"location": {"index": 1}, "text": self.text}}, *self.requests]


def _lead_link(c) -> str | None:
    return catcher_db.author_link(c)


def _fmt_date(value: str | None) -> str:
    if not value:
        return "дата неизвестна"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def build_leads_document(candidates: list, title_suffix: str = "") -> tuple[str, DocBuilder]:
    now = datetime.now(timezone.utc).astimezone()
    title = f"Лиды og1 — {now.strftime('%d.%m.%Y %H:%M')}{title_suffix}"
    b = DocBuilder()
    b.line(title, heading="HEADING_1")
    b.line(f"Всего кандидатов: {len(candidates)}. По каждому — цитата, повод, готовый ответ и две ссылки: "
           "в чат (ответить комментарием) и в личку.")
    b.line()

    current_source = None
    n = 0
    for c in candidates:
        if c["source_url"] != current_source:
            current_source = c["source_url"]
            b.line(f"Чат: {catcher_db.source_label(c)}", heading="HEADING_2", link=current_source)
        n += 1
        author = catcher_db.author_label(c)
        maybe = " (под вопросом)" if c["confidence"] == "maybe" else ""
        b.line(f"{n}. {author} · {_fmt_date(c['posted_at'])}{maybe}", heading="HEADING_3")
        b.line(f"«{c['quote']}»", bold_prefix="Что написал: ")
        b.line(c["reason"], bold_prefix="Почему это лид: ")
        b.line(c["opener_text"], bold_prefix="Вариант ответа: ")
        if c["message_url"]:
            b.line(c["message_url"], bold_prefix="Ответить в чате: ", link=c["message_url"])
        lead_link = _lead_link(c)
        if lead_link:
            b.line(lead_link, bold_prefix="Написать в личку: ", link=lead_link if lead_link.startswith("http") else None)
        else:
            b.line("нет контакта — только через чат", bold_prefix="Написать в личку: ")
        b.line()
    return title, b


def create_leads_document(candidates: list, title_suffix: str = "") -> str:
    """Создаёт документ, выдаёт доступ, возвращает ссылку."""
    from googleapiclient.discovery import build

    creds = load_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)

    title, builder = build_leads_document(candidates, title_suffix)
    meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
    if CONFIG.google_drive_folder_id:
        meta["parents"] = [CONFIG.google_drive_folder_id]
    file = drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    doc_id = file["id"]

    docs.documents().batchUpdate(documentId=doc_id, body={"requests": builder.batch()}).execute()

    for email in CONFIG.google_share_emails:
        try:
            drive.permissions().create(
                fileId=doc_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=False,
                supportsAllDrives=True,
            ).execute()
        except Exception:
            logger.exception("failed to share doc %s with %s", doc_id, email)

    return f"https://docs.google.com/document/d/{doc_id}/edit"
