"""Выгрузка постов и комментариев открытого VK-сообщества через настоящий браузер, без токена.

Токен VK API получить не вышло (VK ID отдаёт «Security Error», бизнес-аккаунт заводить не хотим),
поэтому ловец листает сообщество как человек: Chromium (Playwright) на профиле из
`secrets/vk_browser_profile`, в который оператор один раз вручную входит через
`vk_browser_login.py`. Пароль код не видит и не хранит — только куки профиля браузера.

Как устроено (проверено руками в живом браузере, 2026-09):
- стена — виртуализированный бесконечный список: посты, ушедшие из экрана, удаляются из DOM,
  а подгрузка срабатывает только на настоящее колесо мыши (window.scrollBy не грузит). Поэтому
  крутим `page.mouse.wheel` и после каждого шага собираем посты JS-сборщиком в накопитель;
- комментарии лежат на странице поста (vk.com/wall-1_2), ветки раскрываются кликами по
  «Показать следующие комментарии», «3 ответа» и т.п.;
- новые комментарии падают и под старые посты, поэтому курсор last_message_id больше не
  отсекает: каждый обход заново проходит посты в пределах CATCHER_MAX_MESSAGE_AGE_DAYS,
  от дублей защищает UNIQUE(source_chat_id, external_id).

Темп человеческий: паузы со случайным разбросом, одна вкладка, не больше MAX_POSTS_OPENED
открытых постов за обход — чтобы VK не пометил аккаунт как бота.

Беседы VK (источник вида https://vk.com/im/convo/2000000001 — чат, в который аккаунт ловца уже
вступил), проверено в живом веб-мессенджере 2026-09-30:
- история — тоже виртуализированный список: вне экрана от сообщения остаётся пустая заглушка
  `div.VirtualScrollItem[data-itemkey=<conversation_message_id>]`, более старые сообщения
  подгружаются пачками по ~30, когда колесом мыши крутим вверх над `.ConvoHistory__scrollbar`;
- сообщения одного автора подряд лежат в `section.ConvoStack`, шапка с автором — только у первого;
- день — в разделителе `.DateSeparator[aria-label]` («сегодня», «вчера», «28 сентября») группы
  `.ConvoHistory__dateStack`, у самого сообщения только время «07:49»;
- служебные сообщения (вступил, закрепил) — `article.ServiceMessage`, их пропускаем;
- ответ на сообщение — блок `[data-testid=vkme_replied_message]` с автором и обрезанным текстом
  цитаты; id цитируемого сообщения в разметке нет, находим его по автору и началу текста.
Беседа только читается: ни кликов, ни ввода. Новые сообщения в беседе только дописываются,
поэтому курсор last_message_id (максимальный conversation_message_id) здесь работает.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import catcher_db
from config import CONFIG

logger = logging.getLogger(__name__)

# Файл-метка внутри профиля: пишется только после проверенного входа. Заодно хранит
# user-agent окна, в котором входили, — headless-режим иначе представляется «HeadlessChrome».
LOGIN_MARKER = "og1_user_agent.txt"
NOT_LOGGED_IN = (
    "VK: браузер ловца не залогинен. На компьютере с ботом выполните "
    "`.venv/bin/python vk_browser_login.py`, войдите в VK в открывшемся окне и нажмите Enter"
)

MAX_POSTS_OPENED = 40  # постов с комментариями, открываемых за один обход
MAX_WALL_STEPS = 150  # шагов колеса по стене — предохранитель от бесконечной ленты
IDLE_WALL_STEPS = 8  # столько шагов подряд без новых постов — стена кончилась
OLD_POSTS_IN_ROW = 3  # столько постов подряд старше отсечки — дальше листать незачем
MAX_EXPAND_ROUNDS = 15
COMMENTS_WAIT_MS = 8000
COMMIT_EVERY = 100
# Пояс браузера: в нём VK пишет «сегодня в 9:10», в нём же считаем «сейчас» при разборе дат
VK_TIMEZONE = "Europe/Moscow"

# Беседы
CHAT_PEER_BASE = 2000000000  # peer_id беседы = 2e9 + номер чата у аккаунта
MAX_CHAT_MESSAGES = 1500  # сообщений беседы за один обход
MAX_CHAT_STEPS = 250  # шагов колеса вверх по истории — предохранитель
IDLE_CHAT_STEPS = 8  # столько шагов подряд без новых сообщений — дошли до начала истории
MAX_CHAT_DOWN_STEPS = 60  # вниз до самого свежего (VK может открыть беседу на первом непрочитанном)
KNOWN_REPLY_TARGETS = 3000  # сколько уже сохранённых сообщений беседы брать для поиска цитат

EXPANDER_RE = (
    r"^(Показать следующие комментарии|Показать предыдущие комментарии"
    r"|Показать ещ[её] \d+ (ответ|коммент).*|\d+ ответ(а|ов)?|Показать ответы)$"
)

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
NUMBER_WORDS = {
    "одну": 1, "один": 1, "одна": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
UNITS = (
    (("сек",), "seconds"),
    (("мин",), "minutes"),
    (("ч", "час"), "hours"),
    (("д", "дн", "день", "дня", "дней"), "days"),
    (("нед",), "weeks"),
)


def profile_dir() -> Path:
    path = Path(CONFIG.vk_browser_profile_dir)
    # Относительный путь — от папки проекта: launchd запускает бота не из неё
    return path if path.is_absolute() else Path(__file__).parent / path


def session_ready() -> bool:
    """Оператор уже входил через vk_browser_login.py (живость сессии проверяется при обходе)."""
    return (profile_dir() / LOGIN_MARKER).exists()



# --- один владелец профиля на процесс ------------------------------------------------------------
# Chromium не открывает один профиль дважды, а профиль общий у ловца (обход сообществ/бесед в
# потоке catcher_service) и у диалогов продаж в личке (vk_messenger). Поэтому браузер открывается
# только под этим замком. Диалоги с лидами важнее обхода: их поток ждёт «с приоритетом» — обход
# открывает браузер на каждый источник отдельно, и между источниками замок уходит диалогам.

class ProfileBusy(RuntimeError):
    pass


class _ProfileLock:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held_by: str | None = None
        self._priority_waiting = 0

    @property
    def holder(self) -> str | None:
        return self._held_by

    def acquire(self, owner: str, priority: bool = False, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if priority:
                self._priority_waiting += 1
            try:
                # обычный захват уступает ждущим с приоритетом
                while self._held_by is not None or (not priority and self._priority_waiting):
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._cond.wait(left)
                self._held_by = owner
                return True
            finally:
                if priority:
                    self._priority_waiting -= 1

    def release(self) -> None:
        with self._cond:
            self._held_by = None
            self._cond.notify_all()


PROFILE_LOCK = _ProfileLock()


@contextmanager
def profile_lock(owner: str, priority: bool = False, timeout: float | None = None):
    """Держит профиль браузера на время блока. Не дождались за timeout — ProfileBusy."""
    if not PROFILE_LOCK.acquire(owner, priority=priority, timeout=timeout):
        raise ProfileBusy(f"VK: браузер занят ({PROFILE_LOCK.holder})")
    try:
        yield
    finally:
        PROFILE_LOCK.release()


# --- даты -----------------------------------------------------------------------------------

def _unit(word: str) -> str | None:
    for prefixes, unit in UNITS:
        # «ч»/«д» — только точным словом, иначе «час» совпал бы с «д…» и наоборот
        if word in prefixes or any(len(p) > 2 and word.startswith(p) for p in prefixes):
            return unit
    return None


def parse_vk_date(text: str, now: datetime) -> datetime | None:
    """Дата из шапки поста/комментария VK → datetime в том же поясе, что и `now`.
    Понимает «2 часа назад», «19 ч назад», «7 д назад», «вчера в 12:30», «сегодня в 9:10»,
    «29 авг», «29 авг в 16:45», «30 окт 2014». Дата без года — ближайшая прошедшая."""
    s = " ".join((text or "").replace("\xa0", " ").lower().split())
    if not s:
        return None
    if s in ("только что", "сейчас"):
        return now

    m = re.fullmatch(r"(?:(\d+|[а-яё]+) )?([а-яё]+)\.? назад", s)
    if m:
        amount_raw, unit_word = m.groups()
        if amount_raw is None:
            amount = 1  # «час назад», «минуту назад»
        elif amount_raw.isdigit():
            amount = int(amount_raw)
        else:
            amount = NUMBER_WORDS.get(amount_raw)
        unit = _unit(unit_word)
        if amount is None or unit is None:
            return None
        return now - timedelta(**{unit: amount})

    m = re.fullmatch(r"(сегодня|вчера|позавчера)(?: в (\d{1,2}):(\d{2}))?", s)
    if m:
        day_word, hh, mm = m.groups()
        shift = {"сегодня": 0, "вчера": 1, "позавчера": 2}[day_word]
        day = now - timedelta(days=shift)
        if hh is None:
            return day
        return day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    m = re.fullmatch(r"(\d{1,2}) ([а-яё]+)\.?(?: (\d{4}))?(?: в (\d{1,2}):(\d{2}))?", s)
    if m:
        day_raw, month_word, year_raw, hh, mm = m.groups()
        month = MONTHS.get(month_word[:3])
        if month is None:
            return None
        hour, minute = (int(hh), int(mm)) if hh is not None else (0, 0)
        year = int(year_raw) if year_raw else now.year
        try:
            result = now.replace(year=year, month=month, day=int(day_raw), hour=hour, minute=minute,
                                 second=0, microsecond=0)
        except ValueError:
            return None
        if not year_raw and result > now + timedelta(days=1):
            try:
                result = result.replace(year=year - 1)
            except ValueError:  # 29 февраля
                return None
        return result
    return None


# --- DOM → строки raw_messages (чистые функции, тестируются без браузера) ---------------------

def split_post_key(key: str) -> tuple[str, str]:
    """'-116958555_123914' → ('-116958555', '123914')."""
    owner, post_id = key.rsplit("_", 1)
    return owner, post_id


def group_short_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].lstrip("@").split("?")[0]


def owner_from_url(url: str) -> str | None:
    """vk.com/club123 или vk.com/public123 → '-123'; для коротких имён владельца не угадать."""
    m = re.fullmatch(r"(?:club|public|event)(\d+)", group_short_name(url))
    return f"-{m.group(1)}" if m else None


def pick_owner(post_keys: list[str], known_owner: str | None = None) -> str | None:
    """Владелец стены: из ссылки, иначе самый частый среди постов (реклама — посты чужих владельцев)."""
    if known_owner:
        return known_owner
    owners = Counter(split_post_key(k)[0] for k in post_keys)
    return owners.most_common(1)[0][0] if owners else None


def wall_reached_cutoff(dates: list[datetime | None], cutoff: datetime, in_row: int = OLD_POSTS_IN_ROW) -> bool:
    """Последние `in_row` постов стены с датой — все старше отсечки. Закреплённый пост бывает
    многолетней давности и стоит первым, поэтому один старый пост ещё не повод остановиться."""
    known = [d for d in dates if d is not None]
    return len(known) >= in_row and all(d < cutoff for d in known[-in_row:])


def _profile_path(href: str) -> str:
    """'/id13750322', 'https://vk.com/lenkamin4anka?from=x' → 'id13750322', 'lenkamin4anka'."""
    path = urlparse(href or "").path if "://" in (href or "") else (href or "")
    return path.split("?")[0].split("#")[0].strip("/")


def _is_group_author(author: str, author_path: str, group_name: str | None, group_slug: str) -> bool:
    if re.match(r"^(club|public|event)\d+$", author_path):
        return True
    if author_path and author_path.lower() == group_slug.lower():
        return True
    return bool(group_name) and author.strip().lower() == group_name.strip().lower()


def post_rows(posts: list[dict], owner: str, group_name: str | None, now: datetime, cutoff: datetime) -> list[dict]:
    """Собранные со стены посты → аргументы insert_raw_message. Только посты владельца стены
    (без рекламы), не старше отсечки и с текстом."""
    rows = []
    for p in posts:
        post_owner, post_id = split_post_key(p["id"])
        if post_owner != owner:
            continue
        posted = parse_vk_date(p.get("date", ""), now)
        if posted is not None and posted < cutoff:
            continue
        text = (p.get("text") or "").strip()
        if not text:
            continue
        rows.append({
            "external_id": post_id,
            "author": group_name or None,
            "text": text,
            "url": f"https://vk.com/wall{owner}_{post_id}",
            "posted_at": posted.isoformat() if posted else None,
        })
    return rows


def comment_rows(post_key: str, comments: list[dict], group_name: str | None, group_slug: str,
                 now: datetime, cutoff: datetime) -> list[dict]:
    """Комментарии со страницы поста → аргументы insert_raw_message. Ответ в ветке ссылается на
    корневой комментарий ветки, корневой — на пост: так П1 видит, на что человек отвечает."""
    owner, post_id = split_post_key(post_key)
    rows, seen = [], set()
    for c in comments:
        query = parse_qs(urlparse(c.get("href") or "").query)
        comment_id = (query.get("reply") or [None])[0]
        thread = (query.get("thread") or [None])[0]
        if not comment_id or comment_id in seen:
            continue
        seen.add(comment_id)
        text = (c.get("text") or "").strip()
        author = (c.get("author") or "").strip()
        author_path = _profile_path(c.get("author_href", ""))
        if not text or _is_group_author(author, author_path, group_name, group_slug):
            continue
        posted = parse_vk_date(c.get("date", ""), now)
        if posted is not None and posted < cutoff:
            continue
        url = f"https://vk.com/wall{owner}_{post_id}?reply={comment_id}"
        if thread:
            url += f"&thread={thread}"
        rows.append({
            "external_id": f"{post_id}_{comment_id}",
            "author": author or None,
            "author_username": author_path or None,
            "text": text,
            "url": url,
            "posted_at": posted.isoformat() if posted else None,
            "reply_to_external_id": f"{post_id}_{thread}" if thread and thread != comment_id else post_id,
        })
    return rows


# --- беседы: DOM → строки raw_messages -------------------------------------------------------

def is_chat_url(url: str) -> bool:
    return "/im/convo/" in (url or "")


def chat_peer_id(url: str) -> int | None:
    m = re.search(r"/im/convo/(\d+)", url or "")
    return int(m.group(1)) if m else None


def chat_url(peer_id: int) -> str:
    return f"https://vk.com/im/convo/{peer_id}"


def chat_message_url(peer_id: int, message_id: int | str) -> str:
    # Публичной ссылки на сообщение беседы у VK нет — ссылка просто открывает беседу оператору
    return f"{chat_url(peer_id)}?msgid={message_id}"


def chat_day(label: str, now: datetime) -> datetime | None:
    """Разделитель дней истории («сегодня», «вчера», «28 сентября», «5 октября 2025») → полночь."""
    day = parse_vk_date(label, now)
    return day.replace(hour=0, minute=0, second=0, microsecond=0) if day else None


def chat_message_time(day_label: str, hhmm: str, now: datetime) -> datetime | None:
    day = chat_day(day_label, now)
    if day is None:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (hhmm or "").strip())
    if not m:
        return day
    return day.replace(hour=int(m.group(1)), minute=int(m.group(2)))


def _norm(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split()).lower()


def resolve_reply(reply_author: str, reply_text: str, before_id: int, pool: list[dict]) -> str | None:
    """id сообщения, на которое ответили: в разметке цитаты есть только автор и обрезанный текст,
    поэтому ищем самое позднее более раннее сообщение того же автора, текст которого так начинается.
    pool — [{external_id, author, text}] из этого обхода и уже сохранённых."""
    author = _norm(reply_author)
    preview = _norm(reply_text).rstrip("…").rstrip(".").strip()
    if not author or not preview:
        return None
    best = None
    for m in pool:
        mid = int(m["external_id"])
        if mid >= before_id or _norm(m.get("author") or "") != author:
            continue
        text = _norm(m.get("text") or "")
        if not text or not (text.startswith(preview) or preview.startswith(text)):
            continue
        if best is None or mid > best:
            best = mid
    return str(best) if best is not None else None


def fill_stack_authors(items: list[dict]) -> list[dict]:
    """Шапка с автором есть только у первого сообщения стопки — раздаём её остальным.
    Первое сообщение может быть уже заглушкой, поэтому смотрим на любое сообщение стопки с автором."""
    by_stack: dict[str, tuple[str, str]] = {}
    for it in sorted(items, key=lambda x: int(x["key"])):
        if it.get("author") and it.get("stack"):
            by_stack.setdefault(it["stack"], (it["author"], it.get("author_href") or ""))
    out = []
    for it in items:
        if not it.get("author") and it.get("stack") in by_stack:
            author, href = by_stack[it["stack"]]
            it = {**it, "author": author, "author_href": it.get("author_href") or href}
        out.append(it)
    return out


def chat_rows(items: list[dict], peer_id: int, now: datetime, cutoff: datetime,
              after_id: int = 0, known: list[dict] | None = None) -> list[dict]:
    """Собранные из истории беседы сообщения → аргументы insert_raw_message.
    Пропускает служебные, пустые (стикер, голосовое без текста), уже виденные (id <= after_id)
    и старше отсечки. items: {key, stack, day, time, author, author_href, text, reply_author,
    reply_text, service}."""
    items = fill_stack_authors([it for it in items if str(it.get("key", "")).isdigit()])
    unique: dict[int, dict] = {}
    for it in items:
        unique.setdefault(int(it["key"]), it)
    ordered = [unique[k] for k in sorted(unique)]
    pool = [{"external_id": str(k), "author": it.get("author"), "text": it.get("text")}
            for k, it in unique.items() if not it.get("service") and (it.get("text") or "").strip()]
    pool += list(known or [])

    rows = []
    for it in ordered:
        key = int(it["key"])
        text = (it.get("text") or "").strip()
        if it.get("service") or key <= after_id or not text:
            continue
        posted = chat_message_time(it.get("day", ""), it.get("time", ""), now)
        if posted is not None and posted < cutoff:
            continue
        author_path = _profile_path(it.get("author_href", ""))
        reply_to = None
        if it.get("reply_author") or it.get("reply_text"):
            reply_to = resolve_reply(it.get("reply_author", ""), it.get("reply_text", ""), key, pool)
        rows.append({
            "external_id": str(key),
            "author": (it.get("author") or "").strip() or None,
            "author_username": author_path or None,
            "text": text,
            "url": chat_message_url(peer_id, key),
            "posted_at": posted.isoformat() if posted else None,
            "reply_to_external_id": reply_to,
        })
    return rows


def chat_reached_stop(items: list[dict], now: datetime, cutoff: datetime, after_id: int) -> bool:
    """Листать выше незачем: дошли до уже виденного (курсор) или до сообщений старше отсечки."""
    keyed = [it for it in items if str(it.get("key", "")).isdigit()]
    if not keyed:
        return False
    oldest = min(keyed, key=lambda it: int(it["key"]))
    if after_id and int(oldest["key"]) <= after_id:
        return True
    day = chat_day(oldest.get("day", ""), now)
    return day is not None and day + timedelta(days=1) <= cutoff


# --- браузер ---------------------------------------------------------------------------------

# Посты стены в накопитель window.__og1Posts: стена виртуализирована, ушедшие из экрана посты
# пропадают из DOM, поэтому собираем после каждого шага колеса и ничего не теряем.
COLLECT_POSTS_JS = r"""
() => {
  const store = window.__og1Posts || (window.__og1Posts = {order: [], byId: {}});
  for (const el of document.querySelectorAll('[data-post-id]')) {
    const id = el.getAttribute('data-post-id') || '';
    if (!/^-?\d+_\d+$/.test(id)) continue;
    if (el.parentElement && el.parentElement.closest('[data-post-id]')) continue;  // репост внутри поста
    const links = [...el.querySelectorAll('a[href*="/wall"]')].filter(a => !(a.getAttribute('href') || '').includes('reply='));
    const dateLink = links.find(a => (a.getAttribute('href') || '').includes('/wall' + id)) || links.find(a => (a.getAttribute('href') || '').includes('/wall-'));
    const textEl = [...el.querySelectorAll('[data-testid=showmoretext-in]')].find(t => !t.closest('[data-testid=comment-text]'));
    let comments = 0;
    for (const a of el.querySelectorAll('[aria-label]')) {
      const m = (a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').match(/(\d+) комментари/);
      if (m) { comments = parseInt(m[1], 10); break; }
    }
    const post = {
      id: id,
      date: dateLink ? dateLink.textContent.replace(/\s+/g, ' ').trim() : '',
      text: textEl ? textEl.textContent.trim() : '',
      comments: comments,
      pinned: /Запись закреплена/.test(el.textContent || ''),
    };
    const prev = store.byId[id];
    if (!prev) { store.order.push(id); store.byId[id] = post; continue; }
    if (post.text.length > prev.text.length) prev.text = post.text;
    if (post.date && !prev.date) prev.date = post.date;
    prev.comments = Math.max(prev.comments, post.comments);
  }
  return store.order.map(id => store.byId[id]);
}
"""

GROUP_NAME_JS = r"""
() => {
  const h1 = document.querySelector('h1');
  const name = h1 ? h1.textContent.replace(/\s+/g, ' ').trim() : '';
  return name || (document.title || '').split('|')[0].trim();
}
"""

# Комментарии страницы поста. Поля берём «свои»: у корневого комментария внутри лежат ответы
# ветки со своими датой/автором/текстом, их пропускаем — они соберутся отдельными элементами.
COLLECT_COMMENTS_JS = r"""
() => {
  const IN_THREAD = '[data-testid=wall_comments_comment_in_thread]';
  const out = [];
  for (const el of document.querySelectorAll('[data-testid=wall_comments_comment_root], ' + IN_THREAD)) {
    const own = sel => [...el.querySelectorAll(sel)].find(x => {
      const t = x.closest(IN_THREAD);
      return !t || t === el || !el.contains(t);
    });
    const date = own('[data-testid=wall_comment_date]');
    const owner = own('[data-testid=comment-owner]');
    const text = own('[data-testid=comment-text]');
    out.push({
      href: date ? (date.getAttribute('href') || '') : '',
      date: date ? date.textContent.replace(/\s+/g, ' ').trim() : '',
      author: owner ? owner.textContent.replace(/\s+/g, ' ').trim() : '',
      author_href: owner ? (owner.getAttribute('href') || '') : '',
      text: text ? text.textContent.trim() : '',
    });
  }
  return out;
}
"""

# Помечает видимые раскрывашки веток атрибутом data-og1-exp, чтобы кликнуть их через Playwright.
MARK_EXPANDERS_JS = r"""
(pattern) => {
  const rx = new RegExp(pattern);
  const norm = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-og1-exp]').forEach(e => e.removeAttribute('data-og1-exp'));
  let n = 0;
  for (const el of document.querySelectorAll('span, a, button, div')) {
    if (!rx.test(norm(el))) continue;
    if ([...el.children].some(ch => rx.test(norm(ch)))) continue;  // кликаем самый внутренний
    if (!el.offsetParent) continue;
    el.setAttribute('data-og1-exp', String(n++));
  }
  return n;
}
"""


# Сообщения истории беседы в накопитель window.__og1Chat (история виртуализирована, как стена).
# Берём только числовые data-itemkey внутри основной области: в списке чатов слева ключи вида
# «convo_2000000001». Только чтение DOM.
COLLECT_CHAT_JS = r"""
() => {
  const main = document.querySelector('[data-testid=me_main_content]');
  const store = window.__og1Chat || (window.__og1Chat = {});
  if (!main) return {items: Object.values(store), title: '', ready: false};
  const NOT_OWN = '[data-testid=vkme_replied_message], [data-testid=vkme_pinned_message_banner], [class*=AttachWall], .Attachments';
  const textOf = el => {
    if (!el) return '';
    let out = '';
    const walk = n => {
      for (const ch of n.childNodes) {
        if (ch.nodeType === 3) out += ch.textContent;
        else if (ch.nodeType !== 1) continue;
        else if (ch.tagName === 'IMG') out += ch.getAttribute('alt') || '';  // эмодзи — картинки с alt
        else if (ch.tagName === 'BR') out += '\n';
        else if (ch.classList.contains('MessagePreview__attach')) continue;  // «2 фотографии» в цитате
        else walk(ch);
      }
    };
    walk(el);
    return out.replace(/ /g, ' ').replace(/[ \t]+/g, ' ').replace(/ *\n */g, '\n').trim();
  };
  for (const item of main.querySelectorAll('.VirtualScrollItem[data-itemkey]')) {
    const key = item.getAttribute('data-itemkey') || '';
    if (!/^\d+$/.test(key)) continue;
    const art = item.querySelector('article');
    if (!art) continue;  // заглушка виртуального списка
    const own = sel => [...item.querySelectorAll(sel)].find(e => !e.closest(NOT_OWN));
    const stack = item.closest('section.ConvoStack');
    const first = stack && stack.querySelector('.VirtualScrollItem[data-itemkey]');
    const dayBox = item.closest('.ConvoHistory__dateStack');
    const sep = dayBox && dayBox.querySelector('.DateSeparator');
    const header = own('a.ConvoMessageHeader__authorLink');
    const avatar = own('a[class*="__avatar"]');
    const title = header && header.querySelector('.PeerTitle__title');
    const reply = item.querySelector('[data-testid=vkme_replied_message]');
    const dateEl = own('[class*="MessageInfo"] [class*="__date"]');
    const msg = {
      key: key,
      stack: first ? first.getAttribute('data-itemkey') : '',
      day: sep ? (sep.getAttribute('aria-label') || sep.textContent || '').trim() : '',
      time: dateEl ? dateEl.textContent.trim() : '',
      author: title ? title.textContent.trim() : (header ? header.textContent.trim() : ''),
      author_href: header ? (header.getAttribute('href') || '') : (avatar ? (avatar.getAttribute('href') || '') : ''),
      text: textOf(own('[class*="__text"] .MessageText') || own('.MessageText')),
      reply_author: reply ? textOf(reply.querySelector('[data-testid=vkme_replied_message_author]')) : '',
      reply_text: reply ? textOf(reply.querySelector('[data-testid=vkme_replied_message_content]')) : '',
      service: art.classList.contains('ServiceMessage'),
      // для диалогов продаж (vk_messenger): вложение без текста и классы — направление сообщения
      media: !!item.querySelector('.Attachments, [class*="Sticker"], [class*="AudioMsg"], [class*="AudioMessage"], [class*="Attach"]:not([class*="AttachWall"])'),
      cls: (art.className || '') + ' ' + (stack ? (stack.className || '') : ''),
    };
    const prev = store[key];
    if (!prev) { store[key] = msg; continue; }
    for (const f of ['stack', 'day', 'time', 'author', 'author_href', 'reply_author', 'reply_text', 'cls'])
      if (msg[f] && !prev[f]) prev[f] = msg[f];
    if (msg.text.length > prev.text.length) prev.text = msg.text;
    if (msg.media) prev.media = true;
  }
  const h = main.querySelector('.ConvoTitle__author');
  const sc = main.querySelector('.ConvoHistory__scrollbar');
  return {
    items: Object.values(store),
    title: h ? (h.getAttribute('title') || h.textContent || '').trim() : '',
    ready: !!main.querySelector('.ConvoHistory__flow'),
    atBottom: sc ? sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 5 : true,
  };
}
"""

# Беседы аккаунта из списка чатов слева (тоже виртуализирован — собираем по шагам колеса).
LIST_CHATS_JS = r"""
() => {
  const store = window.__og1Chats || (window.__og1Chats = {});
  for (const b of document.querySelectorAll('[data-testid=vkme_convo_list_item][data-peer-id]')) {
    const peer = b.getAttribute('data-peer-id');
    const h = b.querySelector('.ConvoTitle__author');
    if (peer && h) store[peer] = (h.getAttribute('title') || h.textContent || '').trim();
  }
  return store;
}
"""


def _pause(base: float = 1.5, spread: float = 0.8) -> None:
    time.sleep(base + random.uniform(0, spread))


def open_context(playwright, headless: bool):
    """Chromium на профиле ловца. Один и тот же профиль для ручного входа и для обхода."""
    path = profile_dir()
    marker = path / LOGIN_MARKER
    user_agent = marker.read_text(encoding="utf-8").strip() if headless and marker.exists() else None
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(path),
        # «chromium» вместо headless shell: тот же полноценный браузер, что при ручном входе
        channel="chromium",
        headless=headless,
        locale="ru-RU",
        timezone_id=VK_TIMEZONE,
        viewport={"width": 1280, "height": 900},
        user_agent=user_agent or None,
        args=["--disable-blink-features=AutomationControlled"],
    )


def is_logged_in(page) -> bool:
    """Незалогиненного VK с /feed уводит на страницу входа — по адресу это и видно."""
    page.goto("https://vk.com/feed", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)  # редирект на вход бывает уже после загрузки, из JS
    parsed = urlparse(page.url)
    return parsed.hostname in ("vk.com", "vk.ru", "m.vk.com", "m.vk.ru") and parsed.path.startswith("/feed")


def _wait_selector(page, selector: str, timeout_ms: int) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
        return True
    except PlaywrightTimeout:
        return False


def _scroll_wall(page, cutoff: datetime, now: datetime, known_owner: str | None) -> list[dict]:
    """Листает стену колесом, пока не упрётся в посты старше отсечки или в конец стены."""
    box = page.locator("[data-post-id]").first.bounding_box()
    viewport = page.viewport_size or {"width": 1280, "height": 900}
    x = box["x"] + box["width"] / 2 if box else viewport["width"] / 2
    page.mouse.move(x, viewport["height"] / 2)

    posts: list[dict] = []
    idle = 0
    for _ in range(MAX_WALL_STEPS):
        collected = page.evaluate(COLLECT_POSTS_JS)
        idle = idle + 1 if len(collected) == len(posts) else 0
        posts = collected
        owner = pick_owner([p["id"] for p in posts], known_owner)
        dates = [parse_vk_date(p["date"], now) for p in posts
                 if split_post_key(p["id"])[0] == owner and not p.get("pinned")]
        if wall_reached_cutoff(dates, cutoff) or idle >= IDLE_WALL_STEPS:
            break
        page.mouse.wheel(0, random.randint(1300, 1700))
        _pause()
    return posts


def _collect_comments(page) -> list[dict]:
    """Раскрывает все ветки поста и возвращает комментарии (накопленные по раундам)."""
    found: dict[str, dict] = {}

    def gather() -> None:
        for c in page.evaluate(COLLECT_COMMENTS_JS):
            if c["href"]:
                found.setdefault(c["href"], c)

    gather()
    for _ in range(MAX_EXPAND_ROUNDS):
        before = len(found)
        marked = page.evaluate(MARK_EXPANDERS_JS, EXPANDER_RE)
        for i in range(marked):
            target = page.locator(f'[data-og1-exp="{i}"]')
            try:
                target.click(timeout=3000)
            except Exception:
                try:
                    target.evaluate("e => e.click()")
                except Exception:
                    logger.debug("VK: раскрывашка %s пропала до клика", i)
            time.sleep(random.uniform(0.3, 0.8))
        _pause()
        gather()
        if not marked and len(found) == before:
            break
    return list(found.values())


def _insert(conn, source_id: int, rows: list[dict], counter: list[int]) -> None:
    for row in rows:
        if catcher_db.insert_raw_message(conn, source_chat_id=source_id, **row) is not None:
            counter[0] += 1
            # Обход идёт минутами: без промежуточных коммитов база занята и продающий бот ловит «database is locked»
            if counter[0] % COMMIT_EVERY == 0:
                conn.commit()


def fetch_new_messages(conn, source) -> int:
    """Посты сообщества за CATCHER_MAX_MESSAGE_AGE_DAYS и комментарии под ними. Синхронный
    Playwright: catcher_service зовёт VK-выгрузку через asyncio.to_thread, в потоке без event loop.
    Беседы (vk.com/im/convo/…) читаются отдельно — fetch_chat_messages."""
    if is_chat_url(source["url"]):
        return fetch_chat_messages(conn, source)
    if not session_ready():
        raise RuntimeError(NOT_LOGGED_IN)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    now = datetime.now(ZoneInfo(VK_TIMEZONE))
    cutoff = now - timedelta(days=CONFIG.catcher_max_message_age_days)
    slug = group_short_name(source["url"])
    fetched = [0]
    try:
        with profile_lock("ловец: сообщество"), sync_playwright() as p:
            context = open_context(p, headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if not is_logged_in(page):
                    raise RuntimeError(NOT_LOGGED_IN)
                _pause(1.0, 1.5)
                page.goto(f"https://vk.com/{slug}", wait_until="domcontentloaded", timeout=30000)
                if not _wait_selector(page, "[data-post-id]", 15000):
                    raise RuntimeError(f"VK: на странице {source['url']} не нашлось постов — сообщество закрыто или ссылка неверная")
                group_name = page.evaluate(GROUP_NAME_JS) or None

                posts = _scroll_wall(page, cutoff, now, owner_from_url(source["url"]))
                owner = pick_owner([p_["id"] for p_ in posts], owner_from_url(source["url"]))
                own_posts = [p_ for p_ in posts if split_post_key(p_["id"])[0] == owner]
                _insert(conn, source["id"], post_rows(own_posts, owner, group_name, now, cutoff), fetched)
                conn.commit()

                fresh = [p_ for p_ in own_posts if p_.get("comments")
                         and (d := parse_vk_date(p_["date"], now)) is not None and d >= cutoff]
                for post in fresh[:MAX_POSTS_OPENED]:
                    _pause(2.0, 2.0)
                    page.goto(f"https://vk.com/wall{post['id']}", wait_until="domcontentloaded", timeout=30000)
                    if not _wait_selector(page, "[data-testid=wall_comments_comment_root]", COMMENTS_WAIT_MS):
                        continue
                    comments = _collect_comments(page)
                    _insert(conn, source["id"], comment_rows(post["id"], comments, group_name, slug, now, cutoff), fetched)
                    conn.commit()

                if own_posts:
                    max_post_id = max(int(split_post_key(p_["id"])[1]) for p_ in own_posts)
                    previous = int(source["last_message_id"]) if (source["last_message_id"] or "").isdigit() else 0
                    # Курсор только для справки: комментарии падают и под старые посты, отсекает дедуп
                    catcher_db.update_cursor(conn, source["id"], str(max(max_post_id, previous)))
            finally:
                context.close()
    except PlaywrightError as exc:
        raise RuntimeError(f"VK: браузер не справился ({str(exc).splitlines()[0]})") from exc
    return fetched[0]


# --- беседы: браузер ---------------------------------------------------------------------------

def _max_key(items: list[dict]) -> int:
    return max((int(it["key"]) for it in items if str(it.get("key", "")).isdigit()), default=0)


def _collect_chat(page, now: datetime, cutoff: datetime, after_id: int,
                  max_messages: int = MAX_CHAT_MESSAGES) -> tuple[list[dict], str]:
    """Листает историю открытой беседы колесом: сначала вниз до самого свежего сообщения, потом
    вверх, пока не упрёмся в курсор, отсечку по дате, лимит или начало истории. Только чтение."""
    state = page.evaluate(COLLECT_CHAT_JS)
    box = page.locator("[data-testid=me_main_content] .ConvoHistory__scrollbar").first.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    # VK может открыть беседу на первом непрочитанном — тогда свежие сообщения ниже экрана
    stable = 0
    for _ in range(MAX_CHAT_DOWN_STEPS):
        before = _max_key(state["items"])
        if state.get("atBottom"):
            stable += 1
            if stable >= 2:
                break
        page.mouse.wheel(0, random.randint(1300, 1700))
        _pause(1.0, 0.8)
        state = page.evaluate(COLLECT_CHAT_JS)
        if _max_key(state["items"]) != before:
            stable = 0

    idle = 0
    count = len(state["items"])
    reason = "лимит шагов"
    for _ in range(MAX_CHAT_STEPS):
        items = state["items"]
        messages = sum(1 for it in items if not it.get("service"))
        if chat_reached_stop(items, now, cutoff, after_id):
            reason = "дошли до прошлого обхода или отсечки по дате"
            break
        if messages >= max_messages:
            reason = f"лимит {max_messages} сообщений"
            break
        if idle >= IDLE_CHAT_STEPS:
            reason = "история не подгружается (начало беседы?)"
            break
        # Шаг меньше видимого окна (~25 сообщений), чтобы соседние снимки перекрывались
        page.mouse.wheel(0, -random.randint(1100, 1500))
        _pause(1.2, 1.0)
        state = page.evaluate(COLLECT_CHAT_JS)
        idle = idle + 1 if len(state["items"]) == count else 0
        count = len(state["items"])
    logger.info("VK: беседа %s — собрано %s, остановились: %s", state.get("title"), len(state["items"]), reason)
    return state["items"], state.get("title") or ""


def _open_chat(page, peer_id: int) -> None:
    page.goto(chat_url(peer_id), wait_until="domcontentloaded", timeout=30000)
    if not _wait_selector(page, "[data-testid=me_main_content] .ConvoHistory__flow", 20000) \
            or f"/im/convo/{peer_id}" not in page.url:
        raise RuntimeError(
            f"VK: беседа {chat_url(peer_id)} не открылась — аккаунт ловца в ней не состоит "
            "или ссылка неверная (вступите в беседу в браузере ловца и пришлите ссылку заново)"
        )
    page.wait_for_timeout(2000 + random.randint(0, 1500))


def crawl_chat(peer_id: int, after_id: int = 0, max_messages: int = MAX_CHAT_MESSAGES) -> tuple[list[dict], str, datetime]:
    """Сырые сообщения беседы (без записи в базу) + её название + «сейчас» для разбора дат."""
    if not session_ready():
        raise RuntimeError(NOT_LOGGED_IN)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    now = datetime.now(ZoneInfo(VK_TIMEZONE))
    cutoff = now - timedelta(days=CONFIG.catcher_max_message_age_days)
    try:
        with profile_lock("ловец: беседа"), sync_playwright() as p:
            context = open_context(p, headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if not is_logged_in(page):
                    raise RuntimeError(NOT_LOGGED_IN)
                _pause(1.0, 1.5)
                _open_chat(page, peer_id)
                items, title = _collect_chat(page, now, cutoff, after_id, max_messages)
            finally:
                context.close()
    except PlaywrightError as exc:
        raise RuntimeError(f"VK: браузер не справился ({str(exc).splitlines()[0]})") from exc
    return items, title, now


def fetch_chat_messages(conn, source) -> int:
    """Новые сообщения беседы VK с прошлого обхода (курсор — максимальный id сообщения беседы),
    при первом обходе — за CATCHER_MAX_MESSAGE_AGE_DAYS, но не больше MAX_CHAT_MESSAGES."""
    peer_id = chat_peer_id(source["url"])
    if peer_id is None:
        raise RuntimeError(f"VK: в ссылке {source['url']} нет номера беседы")
    after_id = int(source["last_message_id"]) if (source["last_message_id"] or "").isdigit() else 0
    items, title, now = crawl_chat(peer_id, after_id)
    cutoff = now - timedelta(days=CONFIG.catcher_max_message_age_days)

    known = [dict(r) for r in catcher_db.recent_messages(conn, source["id"], KNOWN_REPLY_TARGETS)]
    fetched = [0]
    _insert(conn, source["id"], chat_rows(items, peer_id, now, cutoff, after_id, known), fetched)
    if title:
        catcher_db.set_source_title(conn, source["id"], title)
    newest = _max_key(items)
    if newest > after_id:
        catcher_db.update_cursor(conn, source["id"], str(newest))
    conn.commit()
    return fetched[0]


def list_joined_chats(max_steps: int = 15) -> list[tuple[int, str]]:
    """Беседы, в которых состоит аккаунт ловца: [(peer_id, название)] — чтобы узнать ссылку
    https://vk.com/im/convo/<peer_id> для добавления в ловец. Только чтение списка чатов."""
    if not session_ready():
        raise RuntimeError(NOT_LOGGED_IN)
    from playwright.sync_api import sync_playwright

    with profile_lock("список бесед"), sync_playwright() as p:
        context = open_context(p, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if not is_logged_in(page):
                raise RuntimeError(NOT_LOGGED_IN)
            page.goto("https://vk.com/im", wait_until="domcontentloaded", timeout=30000)
            if not _wait_selector(page, "[data-testid=vkme_convo_list_item]", 20000):
                return []
            page.wait_for_timeout(1500)
            box = page.locator("[data-testid=vkme_convo_list_item]").first.bounding_box()
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
            found: dict = {}
            idle = 0
            for _ in range(max_steps):
                current = page.evaluate(LIST_CHATS_JS)
                idle = idle + 1 if len(current) == len(found) else 0
                found = current
                if idle >= 3:
                    break
                page.mouse.wheel(0, random.randint(700, 900))
                _pause(0.8, 0.6)
        finally:
            context.close()
    chats = [(int(peer), title) for peer, title in found.items() if peer.isdigit() and int(peer) > CHAT_PEER_BASE]
    return sorted(chats)


if __name__ == "__main__":
    # .venv/bin/python vk_browser.py chats — беседы аккаунта ловца со ссылками для «➕ Добавить чат».
    # Не запускать, пока идёт обход: профиль браузера нельзя открыть дважды.
    import sys

    if sys.argv[1:] != ["chats"]:
        raise SystemExit("использование: .venv/bin/python vk_browser.py chats")
    for peer, title in list_joined_chats():
        print(f"{chat_url(peer)}  {title}")
