"""Лента готовых лидов в боте @BOT_USERNAME (бывший бот-кассир, CASHIER_BOT_ENABLED=0).

Когда лид ответил, как и когда с ним связаться (main._take_contact_if_awaited), операторам
из READY_LEADS_CHAT_IDS приходит карточка: кто, что нужно, контакт и переписка. Под ней кнопка
«✅ Передал специалисту» — отмечает заявку и обновляет все копии карточки. Больше в этом боте
ничего не бывает: только готовые лиды.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import runtime
from config import CONFIG

logger = logging.getLogger(__name__)

router = Router()

TG_MESSAGE_LIMIT = 4096
DONE_PREFIX = "ready:done:"


def _e(text) -> str:
    return html.escape(str(text or ""))


def _fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m %H:%M")
    except ValueError:
        return value


def card_text(order, lead) -> str:
    if db.is_vk_lead(lead):  # диалог в личке VK (vk_messenger.py)
        link = db.vk_profile_url(lead)
        nick = "VK " + link.removeprefix("https://")
    else:
        nick = f"@{lead['username']}" if lead["username"] else f"ID {lead['tg_id']}"
        link = f"https://t.me/{lead['username']}" if lead["username"] else f"tg://user?id={lead['tg_id']}"
    who = f"{lead['name']} ({nick})" if lead["name"] else nick
    price = f"{order['price']:,.0f} ₽".replace(",", " ") if order["price"] else "по запросу"
    head = (
        f"🟢 <b>Готов к связи — заявка №{order['id']}</b>\n\n"
        f"👤 <a href=\"{link}\">{_e(who)}</a>\n"
        f"📚 {_e(order['summary'] or order['tariff'])} · {_e(order['tariff'])}, {price}\n"
        f"📞 <b>Как связаться:</b> «{_e(order['contact'])}»\n"
    )
    if lead["last_score"] is not None:
        head += f"⭐ Балл: {lead['last_score']}\n"
    if order["megabitra_result"]:
        mark = "📤" if order["megabitra_id"] else "⚠️"
        head += f"{mark} Megabitra: {_e(order['megabitra_result'])}\n"
    if order["passed_at"]:
        tail = f"\n\n✅ <b>Передан специалисту</b> {_fmt_dt(order['passed_at'])} ({_e(order['passed_by'])})"
    else:
        tail = ""
    # Переписка — сколько влезает в сообщение Telegram, свежие реплики важнее старых.
    room = TG_MESSAGE_LIMIT - len(head) - len(tail) - 120
    dialog = _e(lead["dialog_context"] or "")
    if len(dialog) > room:
        dialog = "…" + dialog[-room:]
    return f"{head}\n💬 <b>Переписка:</b>\n<blockquote expandable>{dialog}</blockquote>{tail}"


def card_keyboard(order) -> InlineKeyboardMarkup | None:
    if order["passed_at"]:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Передал специалисту", callback_data=f"{DONE_PREFIX}{order['id']}")
    ]])


def _load(order_id: int):
    with db.session() as conn:
        order = db.get_order(conn, order_id)
        lead = db.get_lead(conn, order["lead_id"]) if order else None
    return order, lead


async def send_ready_card(order_id: int) -> int:
    """Шлёт карточку всем получателям. Возвращает, скольким дошло (0 — бот не настроен
    или никто из операторов ещё не нажал у него /start)."""
    bot = runtime.ready_bot
    if bot is None or not CONFIG.ready_leads_chat_ids:
        return 0
    order, lead = _load(order_id)
    if order is None or lead is None:
        return 0
    text, kb = card_text(order, lead), card_keyboard(order)
    delivered: list[tuple[int, int]] = []
    for chat_id in CONFIG.ready_leads_chat_ids:
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML",
                                         disable_web_page_preview=True)
            delivered.append((chat_id, msg.message_id))
        except Exception as exc:
            logger.warning("ready card %s не доставлена в %s: %s", order_id, chat_id, exc)
    if delivered:
        with db.session() as conn:
            db.set_ready_msgs(conn, order_id, delivered)
    return len(delivered)


@router.callback_query(F.data.startswith(DONE_PREFIX))
async def on_passed(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in CONFIG.ready_leads_chat_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.removeprefix(DONE_PREFIX))
    by = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    with db.session() as conn:
        first = db.mark_passed_to_specialist(conn, order_id, by)
    order, lead = _load(order_id)
    if order is None or lead is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    text = card_text(order, lead)
    # Обновляем все копии карточки: у второго оператора кнопка тоже должна исчезнуть.
    targets = [tuple(map(int, pair.split(":"))) for pair in (order["ready_msgs"] or "").split(",") if pair]
    if (callback.message.chat.id, callback.message.message_id) not in targets:
        targets.append((callback.message.chat.id, callback.message.message_id))
    for chat_id, msg_id in targets:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
                                        disable_web_page_preview=True, reply_markup=None)
        except Exception as exc:
            logger.info("ready card %s: не обновил копию %s:%s (%s)", order_id, chat_id, msg_id, exc)
    await callback.answer("Отмечено ✅" if first else f"Уже отмечено ({order['passed_by']})")


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    if message.from_user.id not in CONFIG.ready_leads_chat_ids:
        await message.answer("Это служебный бот Онлайн Гимназии №1.")
        return
    with db.session() as conn:
        pending = db.ready_orders_not_passed(conn)
    await message.answer(
        "Сюда приходят карточки лидов, которые готовы к связи и оставили контакт.\n"
        "Передали специалисту — нажмите «✅ Передал специалисту» под карточкой.\n\n"
        + (f"Не переданы специалисту: {len(pending)} — присылаю ниже." if pending else "Сейчас все переданы.")
    )
    for order in pending:
        order, lead = _load(order["id"])
        await message.answer(card_text(order, lead), reply_markup=card_keyboard(order), parse_mode="HTML",
                             disable_web_page_preview=True)
