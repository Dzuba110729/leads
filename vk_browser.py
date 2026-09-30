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
"""
from __future__ import annotations

import logging
import random
import re
import time
from collections import Counter
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
    Playwright: catcher_service зовёт VK-выгрузку через asyncio.to_thread, в потоке без event loop."""
    if not session_ready():
        raise RuntimeError(NOT_LOGGED_IN)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    now = datetime.now(ZoneInfo(VK_TIMEZONE))
    cutoff = now - timedelta(days=CONFIG.catcher_max_message_age_days)
    slug = group_short_name(source["url"])
    fetched = [0]
    try:
        with sync_playwright() as p:
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
