"""Прогон П1 «Ловец тёплых лидов» над свежими raw_messages (шаг 1, автовыгрузка).

Текст промпта П1 и его правила — дословно из `materials/Дополнительные материалы к уроку 3.md`
(источник истины), с ICP-заполнением под og1 вместо примера "автозапчасти" из раздатки.

Полнота поиска (диагностика 2026-09-17). Один проход терял лидов, которые модель находит
стабильно: повторный прогон тех же сообщений давал находки, которых не было в боевом
результате. Поэтому здесь:
  * сообщения нумеруются, и модель возвращает номер — сопоставление по цитате осталось
    только запасным путём (раньше слегка перефразированная цитата молча выбрасывалась);
  * каждая пачка прогоняется несколькими проходами, находки объединяются;
  * соседние пачки идут внахлёст, чтобы разрезанный границей диалог попал целиком хотя бы в одну;
  * ответ в ветке подаётся вместе с исходным сообщением, иначе «да, у нас та же беда» — мусор;
  * дешёвая модель отсеивает явный офтоп до дорогого разбора;
  * сообщения помечаются разобранными ТОЛЬКО если модель реально отработала — иначе сбой
    LLM навсегда хоронит пачку (так уже терялись лиды до 2026-09-10).
"""
from __future__ import annotations

import difflib
import json
import logging

import catcher_db
import llm
from config import CONFIG

logger = logging.getLogger(__name__)

# ICP под og1 (og1/PLAN.md п.2) - продукт/боль/маркеры совпадают с prompts.BUSINESS_CONTEXT_OG1
# и config.GROUP_SIGNAL_WORDS, чтобы не расходиться с остальным конвейером.
ICP_OG1 = """\
Продукт: Онлайн Гимназия №1 (og1.ru) - лицензированная дистанционная школа, 3-11 класс, гос.
аккредитация и аттестат, вся Россия/зарубежье (дистант).
Клиент: родитель ребёнка 8-18 лет.
Боль: травля/буллинг в очной школе, неудобный график (спорт/переезд), желание перейти на
семейное обучение или сдать аттестацию экстерном, эмиграция/переезд семьи.
Слова-маркеры: "перевести ребёнка", "ищу дистанционную школу", "как оформить семейное обучение",
"аттестация экстерном", "травят в школе", "уезжаем, что со школой".
Стоп-слова: "продам", "реклама", "пишите в лс" (от продавцов курсов/репетиторов), офтоп не про
образование детей.
"""

PREFILTER_SYSTEM_PROMPT = """\
Ты - грубый предфильтр перед дорогим разбором. Твоя задача - отбросить только заведомо
постороннее, НЕ принимая решения о том, лид это или нет.

Тема, ради которой всё читается:
{icp}

ОСТАВЬ номер сообщения, если в нём есть ХОТЬ ЧТО-ТО про: школу, учёбу, образование детей,
перевод/выбор школы, семейное обучение, аттестацию, экзамены, репетиторов, проблемы ребёнка
в школе, переезд семьи с детьми, жизнь за границей с детьми школьного возраста.

ОТБРОСЬ только очевидно постороннее: приветствия, благодарности, смайлики, бытовая болтовня,
темы без всякой связи с детьми и учёбой.

ВАЖНО: сомневаешься - ОСТАВЛЯЙ. Пропустить лид дороже, чем лишний раз перепроверить.
Рекламу и продавцов НЕ отбрасывай - с ними разберётся следующий шаг.

Ответь СТРОГО JSON-массивом номеров, без пояснений: [1, 4, 7]
Если не подходит ни одно - верни []
"""

P1_SYSTEM_PROMPT = """\
Ты - аналитик, который читает открытый чат и ищет ВХОДЯЩИЙ интерес к продукту.

ПРОДУКТ, который я продаю, и КОГО ИЩЕМ (ICP):
{icp}

ЗАДАЧА:
Найди сообщения, где человек ИЩЕТ или НУЖДАЕТСЯ в решении: просит совет, ищет
исполнителя/поставщика, жалуется на боль, которую закрывает мой продукт.

ЖЁСТКО ИСКЛЮЧИ:
- рекламу и объявления;
- тех, кто САМ продаёт (продавцы, оптовики, конкуренты);
- оффтоп и всё, что попадает под стоп-слова;
- сообщения не по моему продукту (даже если тема смежная).

ПРАВИЛА:
- Опирайся ТОЛЬКО на то, что реально есть в тексте. Ничего не додумывай.
- Контакты и ссылки НЕ выдумывай. Если ссылки на сообщение в тексте нет - оставь поле пустым.
- Не формируй базу людей: не выводи имена, телефоны, отдельные списки контактов.
- Проверь КАЖДОЕ сообщение по списку, не останавливайся на первом подходящем.

ФОРМАТ ВХОДА:
Каждое сообщение пронумеровано как [N]. Строка «в ответ на [M]: ...» показывает, на что
человек отвечает - учитывай этот контекст, короткий ответ в ветке может быть сильным сигналом.

УВЕРЕННОСТЬ (поле confidence):
- "high" - человек прямо ищет, спрашивает или описывает свою боль по теме продукта;
- "maybe" - похоже на нашего клиента, но по тексту нельзя утверждать уверенно.
Лучше пометить "maybe", чем промолчать.

Ответь СТРОГО в формате JSON-массива (без пояснений вокруг), один элемент = один подходящий
человек, если подходящих нет - верни пустой массив []:
[{{"n": <номер сообщения>, "quote": "<цитата>", "reason": "<повод>", "confidence": "high|maybe", "contact_url": "<ссылка или null>", "opener_text": "<заготовка входа>"}}]
"""


def _col(row, name, default=None):
    """Достаёт поле и из sqlite3.Row, и из обычного dict (в тестах строки — dict без части колонок)."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _format_batch(messages: list, reply_map: dict | None = None) -> str:
    reply_map = reply_map or {}
    lines = []
    for idx, m in enumerate(messages, start=1):
        author = _col(m, "author", "аноним")
        parent = reply_map.get(_col(m, "reply_to_external_id"))
        if parent is not None:
            parent_text = " ".join(str(_col(parent, "text", "")).split())[:200]
            lines.append(f"[{idx}] {author} (в ответ на «{parent_text}»): {m['text']}")
        else:
            lines.append(f"[{idx}] {author}: {m['text']}")
    return "\n".join(lines)


def _extract_json(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    # Модель иногда добавляет текст до/после массива вопреки инструкции - вырезаем
    # содержимое между первой '[' и последней ']', а не требуем идеально чистый JSON.
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    return cleaned


def prefilter(messages: list, reply_map: dict | None = None) -> tuple[list, bool]:
    """Отсев явного офтопа дешёвой моделью. Возвращает (что осталось, отработала ли модель).
    При любом сбое пропускает всё дальше — предфильтр не имеет права терять лидов."""
    if not CONFIG.p1_prefilter_enabled or not messages:
        return messages, False

    system_prompt = PREFILTER_SYSTEM_PROMPT.format(icp=ICP_OG1)
    raw = llm.call_text(
        system_prompt, _format_batch(messages, reply_map), max_tokens=2048, model=CONFIG.llm_model_cheap
    )
    if not raw:
        return messages, False
    try:
        keep = json.loads(_extract_json(raw))
    except json.JSONDecodeError:
        logger.warning("предфильтр: ответ не разобрался как JSON, пропускаю пачку целиком дальше")
        return messages, False
    if not isinstance(keep, list):
        return messages, False

    indexes = {n for n in keep if isinstance(n, int) and 1 <= n <= len(messages)}
    kept = [m for idx, m in enumerate(messages, start=1) if idx in indexes]
    logger.info("предфильтр: %s из %s сообщений идут на разбор", len(kept), len(messages))
    return kept, True


def run_p1_on_batch(messages: list, reply_map: dict | None = None) -> tuple[list[dict], bool]:
    """Прогоняет пачку raw_messages через П1.
    Возвращает (кандидаты, отработала ли модель). Второе False означает, что разбора
    по сути не было — такие сообщения нельзя помечать разобранными."""
    if not messages:
        return [], True

    system_prompt = P1_SYSTEM_PROMPT.format(icp=ICP_OG1)
    result = llm.call_text(system_prompt, _format_batch(messages, reply_map), max_tokens=8192)
    if result:
        try:
            candidates = json.loads(_extract_json(result))
            if isinstance(candidates, list):
                return candidates, True
        except json.JSONDecodeError:
            logger.warning("П1: не удалось распарсить ответ LLM как JSON, фолбэк на эвристику")

    # Деградированный режим без LLM: грубый предфильтр по словам-сигналам (без домысливания
    # цитаты/повода/захода - на выходе только сырой факт совпадения, требует ручной проверки).
    candidates = []
    for idx, m in enumerate(messages, start=1):
        lowered = m["text"].lower()
        if any(word in lowered for word in CONFIG.group_signal_words):
            candidates.append(
                {
                    "n": idx,
                    "quote": m["text"],
                    "reason": "совпадение по слову-сигналу (эвристика, LLM недоступен)",
                    "confidence": "maybe",
                    "contact_url": _col(m, "url"),
                    "opener_text": "Проверьте вручную - LLM-заготовка недоступна.",
                }
            )
    return candidates, False


def _find_matching_message_id(quote: str, messages: list) -> int | None:
    """Запасной путь, когда модель не вернула номер сообщения. LLM иногда слегка
    перефразирует/склеивает соседние сообщения, поэтому точное равенство слишком хрупкое -
    сначала пробуем подстроку в обе стороны, потом нечёткое сравнение (защита от полностью
    выдуманных цитат остаётся - порог 0.6 достаточно строгий)."""
    quote_norm = " ".join(quote.split())
    if not quote_norm:
        return None
    best_id, best_ratio = None, 0.0
    for m in messages:
        text_norm = " ".join(m["text"].split())
        if quote_norm in text_norm or text_norm in quote_norm:
            return m["id"]
        ratio = difflib.SequenceMatcher(None, quote_norm, text_norm).ratio()
        if ratio > best_ratio:
            best_id, best_ratio = m["id"], ratio
    return best_id if best_ratio >= 0.6 else None


def resolve_message_id(candidate: dict, chunk: list) -> int | None:
    """Номер из ответа модели - основной путь, цитата - запасной."""
    n = candidate.get("n")
    if isinstance(n, int) and 1 <= n <= len(chunk):
        return chunk[n - 1]["id"]
    return _find_matching_message_id(candidate.get("quote", ""), chunk)


def _chunks(messages: list, size: int, overlap: int) -> list[list]:
    """Режет список на пачки внахлёст: последние `overlap` сообщений пачки открывают следующую."""
    if size <= 0 or len(messages) <= size:
        return [messages] if messages else []
    step = max(1, size - max(0, overlap))
    out = []
    for start in range(0, len(messages), step):
        chunk = messages[start:start + size]
        if chunk:
            out.append(chunk)
        if start + size >= len(messages):
            break
    return out


def process_source(conn, source_chat_id: int) -> int:
    """Прогоняет П1 над неразобранными сообщениями источника, создаёт кандидатов.
    Возвращает число новых кандидатов."""
    messages = list(catcher_db.unprocessed_messages_for_source(conn, source_chat_id))
    if not messages:
        return 0

    reply_ids = [rid for rid in (_col(m, "reply_to_external_id") for m in messages) if rid]
    reply_map = catcher_db.reply_targets(conn, source_chat_id, reply_ids)

    created = 0
    analyzed: set[int] = set()

    for chunk in _chunks(messages, CONFIG.p1_batch_size, CONFIG.p1_batch_overlap):
        chunk_ids = {m["id"] for m in chunk}
        shortlist, prefilter_ok = prefilter(chunk, reply_map)
        if prefilter_ok:
            # Отброшенное дешёвой моделью считается разобранным: решение приняла работающая модель.
            analyzed |= chunk_ids - {m["id"] for m in shortlist}
        if not shortlist:
            continue

        shortlist_ids = {m["id"] for m in shortlist}
        found: dict[int, dict] = {}
        any_pass_ok = False
        for _ in range(max(1, CONFIG.p1_passes)):
            candidates, ok = run_p1_on_batch(shortlist, reply_map)
            any_pass_ok = any_pass_ok or ok
            for c in candidates:
                message_id = resolve_message_id(c, shortlist)
                if message_id is None:
                    logger.warning(
                        "П1: находка отброшена, сообщение не опознано (источник %s): %.80s",
                        source_chat_id, c.get("quote", ""),
                    )
                    continue
                previous = found.get(message_id)
                # Несколько проходов над одной пачкой: уверенную находку не затираем сомнительной.
                if previous is None or (previous.get("confidence") != "high" and c.get("confidence") == "high"):
                    found[message_id] = c

        for message_id, c in found.items():
            confidence = "high" if c.get("confidence") == "high" else "maybe"
            if catcher_db.add_candidate(
                conn,
                raw_message_id=message_id,
                quote=c.get("quote", ""),
                reason=c.get("reason", ""),
                contact_url=c.get("contact_url"),
                opener_text=c.get("opener_text", ""),
                confidence=confidence,
            ):
                created += 1

        if any_pass_ok:
            analyzed |= shortlist_ids

    catcher_db.mark_messages_processed(conn, sorted(analyzed))
    skipped = len(messages) - len(analyzed)
    if skipped:
        logger.warning(
            "источник %s: %s сообщений остались неразобранными (LLM не отработал) — повторите прогон",
            source_chat_id, skipped,
        )
    return created
