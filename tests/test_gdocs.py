import gdocs


def _row(**kw):
    base = {
        "source_url": "https://t.me/chat1", "author_username": "mama", "author_name": "Мама",
        "author_tg_id": 5, "posted_at": "2026-09-20T10:00:00+00:00", "confidence": "high",
        "quote": "ищем онлайн-школу", "reason": "ищет школу", "opener_text": "Здравствуйте!",
        "message_url": "https://t.me/chat1/10",
    }
    base.update(kw)
    return base


def test_builder_utf16_indices_for_emoji():
    b = gdocs.DocBuilder()
    b.line("😀 привет", bold_prefix="Х: ")
    # эмодзи занимает 2 единицы UTF-16, кириллица по 1
    assert b.index == 1 + gdocs.DocBuilder._length("Х: 😀 привет\n")
    assert gdocs.DocBuilder._length("😀") == 2


def test_build_document_groups_by_chat_and_links():
    rows = [_row(), _row(source_url="https://t.me/chat2", author_username=None, author_tg_id=None, message_url="https://t.me/chat2/3")]
    title, b = gdocs.build_leads_document(rows)
    assert title.startswith("Лиды og1")
    assert "Чат: https://t.me/chat1" in b.text and "Чат: https://t.me/chat2" in b.text
    assert "Написать в личку: https://t.me/mama" in b.text
    assert "нет контакта" in b.text
    links = [r["updateTextStyle"]["textStyle"]["link"]["url"] for r in b.requests if "updateTextStyle" in r and "link" in r["updateTextStyle"]["textStyle"]]
    assert "https://t.me/chat1/10" in links and "https://t.me/mama" in links
    batch = b.batch()
    assert batch[0]["insertText"]["location"]["index"] == 1
    # все диапазоны форматирования внутри вставленного текста
    total = 1 + gdocs.DocBuilder._length(b.text)
    for r in batch[1:]:
        rng = next(iter(r.values()))["range"]
        assert 1 <= rng["startIndex"] < rng["endIndex"] <= total
