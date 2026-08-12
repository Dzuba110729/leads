"""Прогон П1 «Ловец тёплых лидов» над свежими raw_messages (шаг 1, автовыгрузка).

Текст промпта П1 и его правила — дословно из `materials/Дополнительные материалы к уроку 3.md`
(источник истины), с ICP-заполнением под og1 вместо примера "автозапчасти" из раздатки.
"""
from __future__ import annotations

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

Ответь СТРОГО в формате JSON-массива (без пояснений вокруг), один элемент = один подходящий
человек, если подходящих нет - верни пустой массив []:
[{{"quote": "<цитата>", "reason": "<повод>", "contact_url": "<ссылка или null>", "opener_text": "<заготовка входа>"}}]
"""


def _format_batch(messages: list) -> str:
    return "\n".join(f"{m['author'] or 'аноним'}: {m['text']}" for m in messages)


def run_p1_on_batch(messages: list) -> list[dict]:
    """Прогоняет пачку raw_messages через П1. Возвращает список кандидатов (сырые dict)."""
    if not messages:
        return []
    system_prompt = P1_SYSTEM_PROMPT.format(icp=ICP_OG1)
    batch_text = _format_batch(messages)
    result = llm.call_text(system_prompt, batch_text, max_tokens=2000)
    if result:
        try:
            candidates = json.loads(result)
            if isinstance(candidates, list):
                return candidates
        except json.JSONDecodeError:
            logger.warning("П1: не удалось распарсить ответ LLM как JSON, фолбэк на эвристику")

    # Деградированный режим без LLM: грубый предфильтр по словам-сигналам (без домысливания
    # цитаты/повода/захода - на выходе только сырой факт совпадения, требует ручной проверки).
    candidates = []
    for m in messages:
        lowered = m["text"].lower()
        if any(word in lowered for word in CONFIG.group_signal_words):
            candidates.append(
                {
                    "quote": m["text"],
                    "reason": "совпадение по слову-сигналу (эвристика, LLM недоступен)",
                    "contact_url": m["url"],
                    "opener_text": "Проверьте вручную - LLM-заготовка недоступна.",
                }
            )
    return candidates


def process_source(conn, source_chat_id: int) -> int:
    """Прогоняет П1 над необработанными сообщениями источника, создаёт кандидатов.
    Возвращает число найденных кандидатов."""
    messages = catcher_db.unprocessed_messages_for_source(conn, source_chat_id)
    candidates = run_p1_on_batch(list(messages))
    by_text = {m["text"]: m["id"] for m in messages}
    created = 0
    for c in candidates:
        raw_message_id = by_text.get(c.get("quote", ""))
        if raw_message_id is None:
            continue  # П1 не должен выдумывать цитаты вне входного текста - пропускаем несовпавшее
        catcher_db.add_candidate(
            conn,
            raw_message_id=raw_message_id,
            quote=c.get("quote", ""),
            reason=c.get("reason", ""),
            contact_url=c.get("contact_url"),
            opener_text=c.get("opener_text", ""),
        )
        created += 1
    return created
