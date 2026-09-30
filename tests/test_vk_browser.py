import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import catcher_db
import catcher_pipeline
import vk_browser
from config import CONFIG

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))
CUTOFF = NOW - timedelta(days=180)


# --- даты -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2 часа назад", NOW - timedelta(hours=2)),
    ("19 ч назад", NOW - timedelta(hours=19)),
    ("час назад", NOW - timedelta(hours=1)),
    ("три часа назад", NOW - timedelta(hours=3)),
    ("5 минут назад", NOW - timedelta(minutes=5)),
    ("минуту назад", NOW - timedelta(minutes=1)),
    ("7 д назад", NOW - timedelta(days=7)),
    ("2 дня назад", NOW - timedelta(days=2)),
    ("только что", NOW),
    ("сегодня в 9:10", NOW.replace(hour=9, minute=10)),
    ("вчера в 12:30", (NOW - timedelta(days=1)).replace(hour=12, minute=30)),
    ("29 авг", NOW.replace(month=8, day=29, hour=0, minute=0)),
    ("29 авг в 16:45", NOW.replace(month=8, day=29, hour=16, minute=45)),
    ("30 окт 2014", NOW.replace(year=2014, month=10, day=30, hour=0, minute=0)),
    ("7 дек 2025", NOW.replace(year=2025, month=12, day=7, hour=0, minute=0)),
    ("3 мая в 10:00", NOW.replace(month=5, day=3, hour=10, minute=0)),
    ("29\xa0авг\xa0в\xa016:45", NOW.replace(month=8, day=29, hour=16, minute=45)),
])
def test_parse_vk_date(text, expected):
    assert vk_browser.parse_vk_date(text, NOW) == expected


def test_parse_vk_date_without_year_is_most_recent_past():
    # 7 декабря ещё не наступило в сентябре 2026 → это декабрь 2025
    assert vk_browser.parse_vk_date("7 дек", NOW) == NOW.replace(year=2025, month=12, day=7, hour=0, minute=0)


@pytest.mark.parametrize("text", ["", "Реклама", "32 фев", "вчерa в 25:99", "много лет назад"])
def test_parse_vk_date_garbage(text):
    assert vk_browser.parse_vk_date(text, NOW) is None


# --- стена ------------------------------------------------------------------------------------

def test_owner_and_cutoff_logic():
    assert vk_browser.owner_from_url("https://vk.com/club76168813") == "-76168813"
    assert vk_browser.owner_from_url("https://vk.com/ourhomeedu") is None
    keys = ["-1_10", "-1_9", "-777_5", "-1_8"]
    assert vk_browser.pick_owner(keys) == "-1"
    assert vk_browser.pick_owner(keys, "-5") == "-5"
    old = NOW - timedelta(days=400)
    fresh = NOW - timedelta(days=1)
    # закреплённый старый пост первым — не повод останавливаться
    assert not vk_browser.wall_reached_cutoff([old, fresh, fresh, None], CUTOFF)
    assert vk_browser.wall_reached_cutoff([old, fresh, old, None, old, old], CUTOFF)


def _posts():
    return [
        {"id": "-1_100", "date": "30 окт 2014", "text": "Закреплённый пост", "comments": 5},  # старый закреп
        {"id": "-1_205", "date": "2 часа назад", "text": "  Новый пост  ", "comments": 3},
        {"id": "-777_9", "date": "вчера в 10:00", "text": "Реклама курсов", "comments": 0},  # чужой владелец
        {"id": "-1_204", "date": "вчера в 12:30", "text": "", "comments": 0},  # без текста
    ]


def test_post_rows_keep_only_own_fresh_posts_with_text():
    rows = vk_browser.post_rows(_posts(), "-1", "Наш дом", NOW, CUTOFF)
    assert rows == [{
        "external_id": "205",
        "author": "Наш дом",
        "text": "Новый пост",
        "url": "https://vk.com/wall-1_205",
        "posted_at": (NOW - timedelta(hours=2)).isoformat(),
    }]


def _comments():
    return [
        {"href": "/wall-1_205?reply=300", "date": "час назад", "author": "Ольга Петрова",
         "author_href": "/id13750322", "text": "Ищем школу на семейное, подскажите"},
        {"href": "/wall-1_205?reply=301&thread=300", "date": "5 минут назад", "author": "Лена",
         "author_href": "/lenkamin4anka", "text": "Тоже ищем"},
        {"href": "/wall-1_205?reply=302&thread=300", "date": "3 минуты назад", "author": "Наш дом",
         "author_href": "/ourhomeedu", "text": "Напишите нам в личку"},  # ответ самого сообщества
        {"href": "/wall-1_205?reply=303", "date": "2 минуты назад", "author": "Какой-то паблик",
         "author_href": "/club999", "text": "Реклама"},  # пишет от имени группы
        {"href": "/wall-1_205?reply=304", "date": "30 окт 2014", "author": "Старый",
         "author_href": "/id1", "text": "очень давно"},  # старше отсечки
        {"href": "/wall-1_205?reply=305", "date": "минуту назад", "author": "Пустой",
         "author_href": "/id2", "text": "  "},
        {"href": "/wall-1_205?reply=300", "date": "час назад", "author": "Ольга Петрова",
         "author_href": "/id13750322", "text": "Ищем школу на семейное, подскажите"},  # повтор
    ]


def test_comment_rows_map_ids_urls_and_replies():
    rows = vk_browser.comment_rows("-1_205", _comments(), "Наш дом", "ourhomeedu", NOW, CUTOFF)
    assert [r["external_id"] for r in rows] == ["205_300", "205_301"]
    top, reply = rows
    assert top["author"] == "Ольга Петрова"
    assert top["author_username"] == "id13750322"
    assert top["url"] == "https://vk.com/wall-1_205?reply=300"
    assert top["reply_to_external_id"] == "205"  # корневой комментарий отвечает на пост
    assert reply["author_username"] == "lenkamin4anka"
    assert reply["url"] == "https://vk.com/wall-1_205?reply=301&thread=300"
    assert reply["reply_to_external_id"] == "205_300"  # ответ в ветке — на корневой комментарий


def test_rows_store_and_show_reply_context():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    catcher_db.migrate(conn)
    src = catcher_db.add_source(conn, "vk", "https://vk.com/ourhomeedu")
    rows = vk_browser.post_rows(_posts(), "-1", "Наш дом", NOW, CUTOFF)
    rows += vk_browser.comment_rows("-1_205", _comments(), "Наш дом", "ourhomeedu", NOW, CUTOFF)
    for row in rows:
        assert catcher_db.insert_raw_message(conn, source_chat_id=src["id"], **row) is not None
    # повторный обход — всё уже видели
    assert all(catcher_db.insert_raw_message(conn, source_chat_id=src["id"], **row) is None for row in rows)

    messages = catcher_db.unprocessed_messages_for_source(conn, src["id"])
    reply_map = catcher_db.reply_targets(conn, src["id"], [m["reply_to_external_id"] for m in messages if m["reply_to_external_id"]])
    batch = catcher_pipeline._format_batch(messages, reply_map)
    assert "Ольга Петрова (в ответ на «Новый пост»)" in batch
    assert "Лена (в ответ на «Ищем школу на семейное, подскажите»)" in batch


def test_no_session_fails_fast_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "vk_browser_profile_dir", str(tmp_path / "нет_профиля"))
    monkeypatch.setattr(CONFIG, "vk_access_token", "")
    import catcher_vk

    with pytest.raises(RuntimeError, match="vk_browser_login.py"):
        catcher_vk.fetch_new_messages(None, {"id": 1, "url": "https://vk.com/ourhomeedu", "last_message_id": None})
