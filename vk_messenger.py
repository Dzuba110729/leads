"""Диалоги продаж в личке VK: тот же продавец, что в Telegram, но транспорт — настоящий браузер.

Оператор сам пишет первым лиду, найденному ловцом в VK (кнопка «✅ Написал» в карточке кандидата).
Когда человек отвечает в личку, этот поллер раз в VK_SALES_POLL_SECONDS открывает веб-мессенджер
(Playwright, headless, профиль `secrets/vk_browser_profile` — ОСНОВНОЙ личный аккаунт оператора),
находит диалоги с непрочитанным от лидов из белого списка, читает новые сообщения и передаёт их
в то же ядро, что и Telegram (main.process_lead_text): guard, скоринг, касание в стиле менеджера,
сбор контакта, карточка в ленту готовых лидов, manual_mode.

Безопасность (это личный аккаунт):
- VK_SALES_ENABLED=0 по умолчанию; первым бот не пишет никогда; беседы (peer ≥ 2e9) не трогает;
- открывает только диалоги лидов из белого списка: кандидат VK, которому оператор написал
  («✅ Написал» → catch_candidates.status='contacted'), или VK-лид, уже заведённый в leads.
  Из списка диалогов слева берутся только строки белого списка (peer id, имя, превью);
- лимиты: VK_SALES_MAX_PER_HOUR на аккаунт и VK_SALES_MAX_PER_DIALOG_HOUR на диалог, пауза
  VK_SALES_DELAY_MIN..MAX секунд перед ответом, ввод текста посимвольно с задержкой;
- капча, «подозрительная активность», выход из аккаунта → отправка в VK останавливается до
  перезапуска или кнопки «Возобновить» в ⚙️ Система, операторам уходит предупреждение.

Как читается диалог (проверено в живом веб-мессенджере 2026-09-30):
- список диалогов: `[data-testid=vkme_convo_list_item][data-peer-id]`, непрочитанное —
  `[data-testid=vkme_convo_list_item_unread_counter]`, превью последнего сообщения — атрибут
  title у `.ConvoListItem__message` (у своих сообщений начинается с «Вы: »);
- история: `div.VirtualScrollItem[data-itemkey=<cmid>]` внутри `[data-testid=me_main_content]`,
  собирается тем же vk_browser.COLLECT_CHAT_JS, что и у бесед ловца; автор — по ссылке шапки
  стопки (`a.ConvoMessageHeader__authorLink` / аватар): профиль собеседника → входящее, иначе своё;
- ввод: `[data-testid=vkme_composer_input]` (contenteditable), отправка — Enter.
Сообщение, которое ушло с аккаунта не от бота (его cmid нет в vk_sent), — это менеджер написал
руками: как в Telegram (main.handle_outgoing), оно пишется в историю репликой «менеджер».

Sync Playwright привязан к потоку, поэтому весь тик браузер живёт в одном отдельном потоке
(однопоточный executor), а asyncio-часть (LLM, база, ТГ-уведомления) ждёт его вызовы.
Профиль общий с ловцом — открывается только под vk_browser.profile_lock, с приоритетом.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import catcher_db
import db
import vk_browser
from config import CONFIG

logger = logging.getLogger("vk_messenger")

IM_URL = "https://vk.com/im"
MAX_RESOLVE_PER_TICK = 3  # профилей «короткое имя → id» за тик: каждый — открытие чужой страницы
FIRST_READ_MESSAGES = 12  # при первом открытии диалога лида — сколько последних сообщений взять в историю
OUTBOX_MAX_AGE = timedelta(hours=6)  # прогрев/напоминание, не ушедшее за это время, выбрасываем
SEND_VERIFY_SECONDS = 12

BLOCK_MARKERS = (
    "подозрительная активность", "подозрительную активность", "введите код с картинки",
    "подтвердите, что вы не робот", "вы не робот", "страница заблокирована", "доступ ограничен",
    "аккаунт заблокирован", "временно заблокирован", "слишком много запросов",
)


# --- состояние для ⚙️ Система -------------------------------------------------------------------

@dataclass
class Status:
    last_poll_at: str | None = None
    last_ok_at: str | None = None
    last_error: str | None = None
    halted: str | None = None  # причина остановки отправки (капча и т.п.) — до перезапуска/«Возобновить»
    dialogs: int = 0  # диалогов в белом списке с известным peer id
    skipped_busy: int = 0  # тиков пропущено: браузер держал ловец


STATUS = Status()


def resume() -> None:
    """Ручное «Возобновить» из ⚙️ Система после того, как оператор разобрался с капчей."""
    STATUS.halted = None
    STATUS.last_error = None


# --- чистые функции (тестируются без браузера) -------------------------------------------------

def is_group_peer(peer_id: int) -> bool:
    return int(peer_id) >= vk_browser.CHAT_PEER_BASE


def local_profile_id(path: str | None) -> int | None:
    """'id13750322' → 13750322; короткое имя ('elenell') без браузера не разрешить → None."""
    m = re.fullmatch(r"id(\d+)", (path or "").strip().strip("/"))
    return int(m.group(1)) if m else None


def parse_profile_id(html: str, path: str) -> int | None:
    """Числовой id со страницы профиля vk.com/<path>: VK кладёт в страницу предзагруженный ответ
    users.get с полями id и domain/screen_name. Берём id записи, у которой domain совпадает."""
    local = local_profile_id(path)
    if local:
        return local
    slug = re.escape(path.strip().strip("/"))
    for m in re.finditer(r'\{"id":(\d+),[^{}]*?"(?:domain|screen_name)":"' + slug + r'"', html or "", re.IGNORECASE):
        return int(m.group(1))
    m = re.search(r'"method":"users\.get","request":\{"user_ids":"?' + slug + r'"?[^}]*\}[^\[]*?"response":\[\{"id":(\d+)',
                  html or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def build_whitelist(contacted_paths: list[str], cache: dict[str, int], vk_leads: list[dict],
                    self_id: int | None = None) -> tuple[dict[int, dict], list[str]]:
    """Белый список диалогов: {peer_id: {path, lead_id}} + короткие имена, которые ещё надо
    разрешить в id. Беседы и собственный аккаунт в список не попадают никогда."""
    peers: dict[int, dict] = {}
    unresolved: list[str] = []

    def add(peer: int | None, path: str | None, lead_id: int | None) -> None:
        if not peer or peer <= 0 or is_group_peer(peer) or (self_id and peer == self_id):
            return
        entry = peers.setdefault(peer, {"path": path, "lead_id": lead_id})
        entry["path"] = entry["path"] or path
        entry["lead_id"] = entry["lead_id"] or lead_id

    for lead in vk_leads:
        add(lead.get("vk_id"), lead.get("vk_path"), lead.get("id"))
    for raw in contacted_paths:
        path = (raw or "").strip().strip("/")
        if not path or re.fullmatch(r"(club|public|event)\d+", path):
            continue
        peer = local_profile_id(path) or cache.get(path.lower())
        if peer:
            add(peer, path, None)
        elif path.lower() not in (p.lower() for p in unresolved):
            unresolved.append(path)
    return peers, unresolved


def author_path(href: str | None) -> str:
    return vk_browser._profile_path(href or "").lower()


def message_direction(item: dict, peer_id: int, peer_path: str | None, self_id: int | None) -> str | None:
    """'in' — от лида, 'out' — с нашего аккаунта, None — не понять (такое сообщение не трогаем).
    Главный признак — ссылка на автора стопки: в личном диалоге авторов двое, поэтому всё, что
    не наш профиль, — собеседник. Запасной признак — классы разметки с in/out."""
    href = author_path(item.get("author_href"))
    if href:
        if self_id and href == f"id{self_id}":
            return "out"
        if href in (f"id{peer_id}", (peer_path or "").lower()):
            return "in"
        return "in" if self_id else None
    cls = (item.get("cls") or "").lower()
    if re.search(r"(--|_)out\b|outgoing", cls):
        return "out"
    if re.search(r"(--|_)in\b|incoming", cls):
        return "in"
    return None


@dataclass
class DialogDiff:
    context: list[tuple[str, str]]  # (роль, текст) по порядку — дописать в историю лида сейчас
    pending_text: str | None  # на что отвечать: входящие после последнего исходящего, склеенные
    pending_media: bool  # среди ожидающих ответа есть вложение без текста
    newest_cmid: int  # до какого cmid разобрали
    newest_in_cmid: int  # последнее входящее (для claim_incoming_message)
    manual: int  # сколько ручных сообщений менеджера нашли
    stale: bool = False  # ожидающее ответа входящее слишком старое — только в историю, без ответа


def _norm(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def diff_dialog(items: list[dict], last_cmid: int, peer_id: int, peer_path: str | None, self_id: int | None,
                bot_cmids: set[int], bot_texts: set[str], now: datetime | None = None,
                max_age: timedelta | None = None) -> DialogDiff:
    """Новые сообщения диалога (cmid > last_cmid) → что записать в историю и на что отвечать.
    Своё исходящее, которого нет среди отправленных ботом, — ручное сообщение менеджера (как
    main.handle_outgoing в Telegram). Отвечаем только на входящие ПОСЛЕ последнего исходящего:
    если менеджер уже ответил руками, бот молчит. max_age — входящее старше (по дате из истории)
    считаем устаревшим: пишем в историю, но не отвечаем."""
    items = vk_browser.fill_stack_authors([it for it in items if str(it.get("key", "")).isdigit()])
    unique: dict[int, dict] = {}
    for it in items:
        unique.setdefault(int(it["key"]), it)
    context: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = []  # ('text'|'media', текст)
    newest = last_cmid
    newest_in = 0
    newest_in_item: dict | None = None
    manual = 0
    norm_bot = {_norm(t) for t in bot_texts}
    for key in sorted(unique):
        if key <= last_cmid:
            continue
        it = unique[key]
        if it.get("service"):
            newest = max(newest, key)
            continue
        direction = message_direction(it, peer_id, peer_path, self_id)
        if direction is None:
            break  # непонятно, чьё — дальше не разбираем, посмотрим на следующем тике
        newest = key
        text = (it.get("text") or "").strip()
        if direction == "out":
            # на всё до этого исходящего уже ответили — это просто история
            context.extend(("лид", t if kind == "text" else "[вложение]") for kind, t in pending)
            pending = []
            if key in bot_cmids or (_norm(text) and _norm(text) in norm_bot):
                continue
            if text:
                context.append(("менеджер", text))
                manual += 1
            continue
        newest_in, newest_in_item = key, it
        if text:
            pending.append(("text", text))
        elif it.get("media"):
            pending.append(("media", ""))
    stale = False
    if pending and newest_in_item is not None and now is not None and max_age is not None:
        posted = vk_browser.chat_message_time(newest_in_item.get("day", ""), newest_in_item.get("time", ""), now)
        stale = posted is not None and now - posted > max_age
    texts = [t for kind, t in pending if kind == "text"]
    media = any(kind == "media" for kind, _ in pending)
    if stale:
        context.extend(("лид", t if kind == "text" else "[вложение]") for kind, t in pending)
        return DialogDiff(context, None, False, newest, newest_in, manual, stale=True)
    if texts and media:
        context.append(("лид", "[вложение]"))
    return DialogDiff(context, "\n".join(texts) if texts else None, media and not texts, newest, newest_in, manual)


def rate_allows(global_times: list[datetime], dialog_times: list[datetime], now: datetime,
                max_per_hour: int, max_per_dialog_hour: int) -> str | None:
    """None — можно отправлять; иначе причина, почему нельзя."""
    hour_ago = now - timedelta(hours=1)
    if sum(1 for t in global_times if t > hour_ago) >= max_per_hour:
        return f"лимит {max_per_hour} сообщений в час на аккаунт"
    if sum(1 for t in dialog_times if t > hour_ago) >= max_per_dialog_hour:
        return f"лимит {max_per_dialog_hour} сообщений в час в одном диалоге"
    return None


def detect_block(url: str, body_text: str, self_id: int | None) -> str | None:
    """Признаки, при которых отправку надо остановить: выход из аккаунта, капча, блокировка."""
    parsed = (url or "").lower()
    if any(m in parsed for m in ("/login", "id.vk.com", "/challenge", "act=blocked", "/blocked")):
        return f"VK открыл служебную страницу ({url}) — похоже, аккаунт разлогинен или проверка"
    if not self_id:
        return "VK: аккаунт не залогинен в браузере"
    lowered = (body_text or "").lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            return f"VK показал «{marker}»"
    if "captcha" in lowered or "капч" in lowered:
        return "VK показал капчу"
    return None


# --- база ---------------------------------------------------------------------------------------

def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_whitelist(conn, self_id: int | None) -> tuple[dict[int, dict], list[str]]:
    catcher_db.migrate(conn)
    paths = [r[0] for r in conn.execute(
        """SELECT DISTINCT raw_messages.author_username FROM catch_candidates
           JOIN raw_messages ON raw_messages.id = catch_candidates.raw_message_id
           JOIN source_chats ON source_chats.id = raw_messages.source_chat_id
           WHERE source_chats.platform = 'vk' AND catch_candidates.status = 'contacted'
             AND raw_messages.author_username IS NOT NULL"""
    )]
    cache = {r["path"].lower(): r["user_id"] for r in conn.execute("SELECT path, user_id FROM vk_peers")}
    leads = [dict(r) for r in conn.execute("SELECT id, vk_id, vk_path FROM leads WHERE platform = 'vk' AND vk_id IS NOT NULL")]
    return build_whitelist(paths, cache, leads, self_id)


def save_peer(conn, path: str, user_id: int) -> None:
    conn.execute(
        "INSERT INTO vk_peers (path, user_id, resolved_at) VALUES (?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET user_id = excluded.user_id, resolved_at = excluded.resolved_at",
        (path.lower(), int(user_id), db.now()),
    )


def get_dialog(conn, peer_id: int):
    return conn.execute("SELECT * FROM vk_dialogs WHERE peer_id = ?", (peer_id,)).fetchone()


def save_dialog(conn, peer_id: int, lead_id: int | None, last_cmid: int, preview: str | None) -> None:
    conn.execute(
        """INSERT INTO vk_dialogs (peer_id, lead_id, last_cmid, preview, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(peer_id) DO UPDATE SET lead_id = COALESCE(excluded.lead_id, vk_dialogs.lead_id),
               last_cmid = MAX(vk_dialogs.last_cmid, excluded.last_cmid),
               preview = COALESCE(excluded.preview, vk_dialogs.preview), updated_at = excluded.updated_at""",
        (peer_id, lead_id, last_cmid, preview, db.now()),
    )


def record_sent(conn, peer_id: int, cmid: int | None, text: str) -> None:
    conn.execute("INSERT INTO vk_sent (peer_id, cmid, text, sent_at) VALUES (?, ?, ?, ?)",
                 (peer_id, cmid, text, db.now()))


def sent_times(conn, peer_id: int | None = None) -> list[datetime]:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if peer_id is None:
        rows = conn.execute("SELECT sent_at FROM vk_sent WHERE sent_at > ?", (since,))
    else:
        rows = conn.execute("SELECT sent_at FROM vk_sent WHERE sent_at > ? AND peer_id = ?", (since, peer_id))
    return [_parse_ts(r[0]) for r in rows]


def bot_sent(conn, peer_id: int) -> tuple[set[int], set[str]]:
    rows = conn.execute("SELECT cmid, text FROM vk_sent WHERE peer_id = ? ORDER BY id DESC LIMIT 200", (peer_id,)).fetchall()
    return {r["cmid"] for r in rows if r["cmid"]}, {r["text"] for r in rows if not r["cmid"]}


def check_rate(conn, peer_id: int) -> str | None:
    return rate_allows(sent_times(conn), sent_times(conn, peer_id), datetime.now(timezone.utc),
                       CONFIG.vk_sales_max_per_hour, CONFIG.vk_sales_max_per_dialog_hour)


def enqueue_outbox(conn, lead_id: int, text: str) -> None:
    conn.execute("INSERT INTO vk_outbox (lead_id, text, created_at) VALUES (?, ?, ?)", (lead_id, text, db.now()))


def pending_outbox(conn) -> list:
    return conn.execute(
        """SELECT vk_outbox.*, leads.vk_id AS vk_id, leads.manual_mode AS manual_mode, leads.blocked AS blocked
           FROM vk_outbox JOIN leads ON leads.id = vk_outbox.lead_id
           WHERE vk_outbox.sent_at IS NULL AND vk_outbox.error IS NULL ORDER BY vk_outbox.id"""
    ).fetchall()


def finish_outbox(conn, item_id: int, error: str | None = None) -> None:
    if error:
        conn.execute("UPDATE vk_outbox SET error = ? WHERE id = ?", (error, item_id))
    else:
        conn.execute("UPDATE vk_outbox SET sent_at = ? WHERE id = ?", (db.now(), item_id))


def queue_for_lead(tg_id: int, text: str) -> None:
    """Прогрев/напоминание VK-лиду (runtime.send_to_lead с отрицательным tg_id): кладём в очередь,
    поллер отправит браузером с теми же лимитами. Выключено или диалога ещё нет — ошибка, вызывающий
    её залогирует и не отметит касание."""
    if not CONFIG.vk_sales_enabled:
        raise RuntimeError("диалоги VK выключены (VK_SALES_ENABLED=0)")
    with db.session() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE tg_id = ?", (tg_id,)).fetchone()
        if lead is None or not lead["vk_id"] or get_dialog(conn, lead["vk_id"]) is None:
            raise RuntimeError(f"нет диалога VK с лидом {tg_id}")
        enqueue_outbox(conn, lead["id"], text)


# --- браузер (всё — в одном потоке сессии) ---------------------------------------------------------

LIST_JS = r"""
(allowed) => {
  const set = new Set(allowed.map(String));
  const out = [];
  for (const b of document.querySelectorAll('[data-testid=vkme_convo_list_item][data-peer-id]')) {
    const peer = b.getAttribute('data-peer-id') || '';
    if (!set.has(peer)) continue;  // чужие диалоги не читаем
    const h = b.querySelector('.ConvoTitle__author');
    const msg = b.querySelector('.ConvoListItem__message');
    const cnt = b.querySelector('[data-testid=vkme_convo_list_item_unread_counter]');
    out.push({
      peer: peer,
      name: h ? (h.getAttribute('title') || h.textContent || '').trim() : '',
      preview: msg ? (msg.getAttribute('title') || '') : '',
      unread: cnt ? (parseInt(cnt.textContent, 10) || 1) : 0,
    });
  }
  return out;
}
"""

STATE_JS = r"""
() => ({
  self: (window.vk && window.vk.id) || 0,
  text: document.body ? document.body.innerText.slice(0, 4000) : '',
  captcha: !!document.querySelector('[class*="captcha" i], iframe[src*="captcha"], [data-testid*="captcha"]'),
})
"""


class VkBlocked(RuntimeError):
    """Капча / проверка / выход из аккаунта — останавливаем отправку в VK."""


class BrowserSession:
    """Браузер на тик поллера. Все вызовы Playwright — через call(), в одном потоке."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vk-sales")
        self._pw = None
        self._ctx = None
        self.page = None
        self.self_id: int | None = None
        self._locked = False

    async def call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(self, *args))

    # -- в потоке сессии --
    def _open(self, wait: float) -> None:
        if not vk_browser.PROFILE_LOCK.acquire("диалоги VK", priority=True, timeout=wait):
            raise vk_browser.ProfileBusy(f"браузер занят ({vk_browser.PROFILE_LOCK.holder})")
        self._locked = True
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = vk_browser.open_context(self._pw, headless=True)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)
        vk_browser._wait_selector(self.page, "[data-testid=vkme_convo_list_item]", 20000)
        self.page.wait_for_timeout(1500 + random.randint(0, 1500))
        self.check_block()

    def _close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            try:
                if self._pw is not None:
                    self._pw.stop()
            finally:
                self._ctx = self._pw = self.page = None
                if self._locked:
                    self._locked = False
                    vk_browser.PROFILE_LOCK.release()

    def check_block(self) -> None:
        state = self.page.evaluate(STATE_JS)
        self.self_id = int(state.get("self") or 0) or None
        reason = detect_block(self.page.url, state.get("text", ""), self.self_id)
        if reason is None and state.get("captcha"):
            reason = "VK показал капчу"
        if reason:
            raise VkBlocked(reason)

    def read_list(self, peers: list[int]) -> list[dict]:
        if "/im" not in self.page.url or "/im/convo/" in self.page.url:
            self.page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)
            vk_browser._wait_selector(self.page, "[data-testid=vkme_convo_list_item]", 20000)
            self.page.wait_for_timeout(1200 + random.randint(0, 800))
        return self.page.evaluate(LIST_JS, [str(p) for p in peers])

    def resolve_profile(self, path: str) -> int | None:
        vk_browser._pause(2.0, 2.0)
        self.page.goto(f"https://vk.com/{path}", wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(1500)
        self.check_block()
        return parse_profile_id(self.page.content(), path)

    def _open_dialog(self, peer_id: int) -> None:
        if f"/im/convo/{peer_id}" in self.page.url:
            return
        vk_browser._pause(1.0, 1.5)
        self.page.goto(vk_browser.chat_url(peer_id), wait_until="domcontentloaded", timeout=30000)
        if f"/im/convo/{peer_id}" not in self.page.url:
            self.check_block()
            raise RuntimeError(f"диалог {peer_id} не открылся")
        vk_browser._wait_selector(self.page, "[data-testid=me_main_content] .ConvoHistory__flow", 15000)
        self.page.wait_for_timeout(1500 + random.randint(0, 1000))
        self.check_block()

    def read_dialog(self, peer_id: int, after_cmid: int) -> list[dict]:
        self._open_dialog(peer_id)
        now = datetime.now(ZoneInfo(vk_browser.VK_TIMEZONE))
        items, _ = vk_browser._collect_chat(self.page, now, now - timedelta(days=30), after_cmid,
                                            max_messages=FIRST_READ_MESSAGES if not after_cmid else 60)
        return items

    def send(self, peer_id: int, text: str) -> int | None:
        """Печатает и отправляет текст в открытый диалог; возвращает cmid появившегося сообщения
        (None — отправилось, но в истории не нашли)."""
        self._open_dialog(peer_id)
        before = vk_browser._max_key(self.page.evaluate(vk_browser.COLLECT_CHAT_JS)["items"])
        box = self.page.locator("[data-testid=vkme_composer_input]").first
        box.click()
        self.page.wait_for_timeout(400 + random.randint(0, 600))
        for i, line in enumerate(text.split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")
            for chunk in re.findall(r"\S+\s*|\s+", line):
                self.page.keyboard.type(chunk, delay=random.randint(45, 140))
                if random.random() < 0.15:
                    self.page.wait_for_timeout(random.randint(200, 900))
        self.page.wait_for_timeout(500 + random.randint(0, 1200))
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1500)
        composer = self.page.locator("[data-testid=vkme_composer_input]").first
        if composer.inner_text().strip():
            # Enter не отправил (в настройках VK может стоять Ctrl+Enter) — жмём кнопку
            self.page.locator("[data-testid=vkme_composer_send]").first.click()
        want = _norm(text)
        deadline = time.monotonic() + SEND_VERIFY_SECONDS
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(700)
            items = self.page.evaluate(vk_browser.COLLECT_CHAT_JS)["items"]
            for it in items:
                if str(it.get("key", "")).isdigit() and int(it["key"]) > before and _norm(it.get("text", "")) == want:
                    self.check_block()
                    return int(it["key"])
        self.check_block()
        leftover = self.page.locator("[data-testid=vkme_composer_input]").first.inner_text().strip()
        if leftover:
            raise RuntimeError("VK: текст остался в поле ввода — сообщение не ушло")
        return None


async def open_session() -> BrowserSession:
    session = BrowserSession()
    try:
        await session.call(lambda s: s._open(CONFIG.vk_sales_lock_wait))
    except BaseException:
        await close_session(session)
        raise
    return session


async def close_session(session: BrowserSession) -> None:
    try:
        await session.call(lambda s: s._close())
    finally:
        session._pool.shutdown(wait=False)


class VkChannel:
    """Канал ответа для main.process_lead_text: тот же интерфейс, что у события Telethon."""
    platform = "vk"

    def __init__(self, session: BrowserSession, peer_id: int) -> None:
        self.session = session
        self.chat_id = peer_id
        self.sent = 0

    async def reply(self, text: str):
        with db.session() as conn:
            limit = check_rate(conn, self.chat_id)
        if STATUS.halted:
            raise VkBlocked(STATUS.halted)
        if limit:
            raise RuntimeError(f"VK: {limit}")
        # Живой человек не отвечает мгновенно
        delay = random.uniform(CONFIG.vk_sales_delay_min, max(CONFIG.vk_sales_delay_min, CONFIG.vk_sales_delay_max))
        await asyncio.sleep(delay)
        cmid = await self.session.call(lambda s: s.send(self.chat_id, text))
        with db.session() as conn:
            record_sent(conn, self.chat_id, cmid, text)
            if cmid:
                save_dialog(conn, self.chat_id, None, cmid, None)
        self.sent += 1
        logger.info("VK SENT peer=%s cmid=%s (%s симв., пауза %.0f с)", self.chat_id, cmid, len(text), delay)
        return SimpleNamespace(id=cmid or 0)


# --- тик поллера ------------------------------------------------------------------------------------

async def _halt(reason: str) -> None:
    import alerts

    first = STATUS.halted is None
    STATUS.halted = reason
    STATUS.last_error = reason
    logger.error("VK SALES HALTED: %s", reason)
    if first:
        await alerts.notify_operators(
            f"⛔ Диалоги в VK остановлены: {reason}.\n\n"
            "Бот больше не отвечает в личке VK. Зайдите в VK с этого аккаунта (vk_browser_login.py), "
            "пройдите проверку, затем нажмите «Возобновить VK» в ⚙️ Система или перезапустите бота."
        )


def _who(entry: dict, name: str | None, peer_id: int) -> str:
    path = entry.get("path") or f"id{peer_id}"
    return f"{name or 'VK'} (vk.com/{path})"


STALE_INCOMING = timedelta(days=2)  # как догонялка Telegram (main.CATCH_UP_MAX): старое не отвечаем


async def _handle_dialog(session: BrowserSession, hooks: "Hooks", peer_id: int, entry: dict, row: dict) -> bool:
    """Разбирает новые сообщения диалога. True — бот что-то отправил."""
    with db.session() as conn:
        dialog = get_dialog(conn, peer_id)
        last_cmid = dialog["last_cmid"] if dialog else 0
        bot_cmids, bot_texts = bot_sent(conn, peer_id)
    items = await session.call(lambda s: s.read_dialog(peer_id, last_cmid))
    now = datetime.now(ZoneInfo(vk_browser.VK_TIMEZONE))
    diff = diff_dialog(items, last_cmid, peer_id, entry.get("path"), session.self_id, bot_cmids, bot_texts,
                       now=now, max_age=STALE_INCOMING)
    name = row.get("name") or None
    who = _who(entry, name, peer_id)
    with db.session() as conn:
        lead = db.get_or_create_vk_lead(conn, peer_id, entry.get("path"), name, touch=diff.newest_in_cmid > 0)
        if lead["blocked"]:
            save_dialog(conn, peer_id, lead["id"], diff.newest_cmid, row.get("preview"))
            return False
        for role, text in diff.context:
            db.append_dialog_context(conn, lead["id"], role, text)
        if diff.manual:
            logger.info("VK MANUAL peer=%s: %s сообщ. менеджера записано в диалог", peer_id, diff.manual)
        if diff.stale:
            logger.info("VK peer=%s: входящее старше %s — записано в историю без ответа", peer_id, STALE_INCOMING)
        claimed = bool(diff.newest_in_cmid) and db.claim_incoming_message(conn, lead["id"], diff.newest_in_cmid)
        lead = db.get_lead(conn, lead["id"])
    channel = VkChannel(session, peer_id)
    try:
        if claimed and diff.pending_text:
            logger.info("VK IN peer=%s cmid=%s: новое от лида (%s симв.)", peer_id, diff.newest_in_cmid,
                        len(diff.pending_text))
            await hooks.process(channel, lead, diff.pending_text, who)
        elif claimed and diff.pending_media:
            await hooks.note_media(lead, "вложение", who, where="VK (личка)")
    finally:
        with db.session() as conn:
            save_dialog(conn, peer_id, lead["id"], diff.newest_cmid, row.get("preview"))
    return channel.sent > 0


@dataclass
class Hooks:
    """Ядро продавца из main.py (main запускается как __main__, импортировать его отсюда нельзя)."""
    process: object  # async (channel, lead, text, who) -> None — main.process_lead_text
    note_media: object  # async (lead, kind, who, where=...) -> None — main.note_unanswered_media


async def _send_outbox(session: BrowserSession) -> list[int]:
    with db.session() as conn:
        items = pending_outbox(conn)
    sent: list[int] = []
    for item in items:
        with db.session() as conn:
            if datetime.now(timezone.utc) - _parse_ts(item["created_at"]) > OUTBOX_MAX_AGE:
                finish_outbox(conn, item["id"], "устарело")
                continue
            if item["manual_mode"] or item["blocked"] or not item["vk_id"]:
                finish_outbox(conn, item["id"], "лид в ручном режиме или заблокирован")
                continue
            if check_rate(conn, item["vk_id"]):
                return sent  # лимит — остальное подождёт следующего тика
        try:
            await VkChannel(session, item["vk_id"]).reply(item["text"])
        except VkBlocked:
            raise
        except Exception as exc:
            logger.exception("VK outbox %s не ушёл", item["id"])
            with db.session() as conn:
                finish_outbox(conn, item["id"], str(exc)[:200])
            continue
        with db.session() as conn:
            finish_outbox(conn, item["id"])
        sent.append(item["vk_id"])
        logger.info("VK OUTBOX lead=%s: прогрев/напоминание отправлено", item["lead_id"])
    return sent


async def poll_once(hooks: Hooks) -> None:
    """Один тик: список диалогов → новые сообщения лидов из белого списка → ядро продавца."""
    STATUS.last_poll_at = db.now()
    if not vk_browser.session_ready():
        STATUS.last_error = "браузер VK не залогинен (vk_browser_login.py)"
        return
    with db.session() as conn:
        rate_limited = rate_allows(sent_times(conn), [], datetime.now(timezone.utc),
                                   CONFIG.vk_sales_max_per_hour, 10 ** 6)
        has_outbox = bool(pending_outbox(conn))
    if rate_limited:
        STATUS.last_error = rate_limited
        logger.info("VK: %s — тик пропущен", rate_limited)
        return
    try:
        session = await open_session()
    except vk_browser.ProfileBusy as exc:
        STATUS.skipped_busy += 1
        logger.info("VK: %s — тик пропущен", exc)
        return
    except VkBlocked as exc:
        await _halt(str(exc))
        return
    try:
        with db.session() as conn:
            peers, unresolved = load_whitelist(conn, session.self_id)
        for path in unresolved[:MAX_RESOLVE_PER_TICK]:
            user_id = await session.call(lambda s, p=path: s.resolve_profile(p))
            if user_id:
                with db.session() as conn:
                    save_peer(conn, path, user_id)
                if not is_group_peer(user_id) and user_id != session.self_id:
                    peers.setdefault(user_id, {"path": path, "lead_id": None})
                logger.info("VK: профиль %s → id%s", path, user_id)
            else:
                logger.warning("VK: не удалось узнать id профиля %s", path)
        STATUS.dialogs = len(peers)
        replied: list[int] = []
        if peers:
            rows = await session.call(lambda s: s.read_list(list(peers)))
            for row in rows:
                peer_id = int(row["peer"])
                if peer_id not in peers or is_group_peer(peer_id):
                    continue
                with db.session() as conn:
                    dialog = get_dialog(conn, peer_id)
                changed = dialog is None or row.get("unread") or (row.get("preview") or "") != (dialog["preview"] or "")
                if not changed:
                    continue
                with db.session() as conn:
                    limit = check_rate(conn, peer_id)
                if limit:
                    logger.info("VK: диалог %s ждёт — %s", peer_id, limit)
                    continue  # входящее не отмечаем прочитанным ботом — разберём на следующем тике
                try:
                    if await _handle_dialog(session, hooks, peer_id, peers[peer_id], row):
                        replied.append(peer_id)
                except VkBlocked:
                    raise
                except Exception as exc:
                    STATUS.last_error = f"диалог {peer_id}: {exc}"
                    logger.exception("VK: диалог %s не обработан", peer_id)
        if has_outbox:
            replied += await _send_outbox(session)
        if replied:
            # превью в списке сменилось на ответ бота — запоминаем, чтобы не открывать диалог зря
            for row in await session.call(lambda s: s.read_list(replied)):
                with db.session() as conn:
                    save_dialog(conn, int(row["peer"]), None, 0, row.get("preview"))
        STATUS.last_ok_at = db.now()
    except VkBlocked as exc:
        await _halt(str(exc))
    finally:
        await close_session(session)


def in_working_hours(now: datetime | None = None, spec: str | None = None) -> bool:
    """«9-22» по Москве → True с 9:00 до 21:59. Пустая или кривая строка — без ограничений.
    Конец меньше начала («22-6») — ночной интервал через полночь."""
    spec = CONFIG.vk_sales_hours if spec is None else spec
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return True
    start, end = int(m.group(1)), int(m.group(2))
    hour = (now or datetime.now(ZoneInfo(vk_browser.VK_TIMEZONE))).hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


async def run_forever(hooks: Hooks) -> None:
    """Фоновая задача main.py. Флаг VK_SALES_ENABLED смотрится на каждом тике."""
    if not CONFIG.vk_sales_enabled:
        logger.info("VK-диалоги выключены (VK_SALES_ENABLED=0)")
    else:
        logger.info("VK-диалоги включены: опрос раз в %s с, лимит %s/ч, часы %s МСК", CONFIG.vk_sales_poll_seconds,
                    CONFIG.vk_sales_max_per_hour, CONFIG.vk_sales_hours)
    await asyncio.sleep(30)  # дать процессу подняться (userbot, боты) до первого открытия браузера
    while True:
        if CONFIG.vk_sales_enabled and not STATUS.halted and in_working_hours():
            try:
                await poll_once(hooks)
            except Exception as exc:
                STATUS.last_error = str(exc)[:300]
                logger.exception("VK: тик поллера упал")
        await asyncio.sleep(CONFIG.vk_sales_poll_seconds + random.uniform(0, 30))
