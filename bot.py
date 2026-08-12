"""Бот-хэндофф (Bot API): deep-link на карточку заявки, кнопка «Передать менеджеру».

Замена генерической «Оплатил ✅» под цикл сделки og1 (подписка + рассрочка + договор —
см. og1/PLAN.md п.4): бот не завершает оплату сам, а доводит лида до передачи менеджеру
приёмной комиссии. Договор/оплата дальше ведёт человек вручную вне бота.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
from config import CONFIG
from prompts import HANDOFF_MESSAGE_TEMPLATE, OPERATOR_CARD_TEMPLATE

logger = logging.getLogger(__name__)

router_dp = Dispatcher()


def _order_card_text(order) -> str:
    price_label = f"{order['price']:,.0f} ₽".replace(",", " ") if order["price"] else "по запросу"
    return (
        f"Заявка #{order['id']}\n"
        f"Тариф: {order['tariff']}\n"
        f"Стоимость: {price_label}\n"
        f"Статус: {order['status']}"
    )


def _order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Передать менеджеру ✅", callback_data=f"handoff:{order_id}")]]
    )


@router_dp.message(CommandStart(deep_link=True))
async def start_with_order(message: Message, command: CommandObject, bot: Bot) -> None:
    payload = command.args or ""
    if not payload.startswith("order_"):
        await message.answer("Добро пожаловать! Ссылка не распознана — напишите нам в личку.")
        return
    order_id = int(payload.removeprefix("order_"))
    with db.session() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            await message.answer("Заявка не найдена. Попробуйте получить ссылку заново.")
            return
        await message.answer(_order_card_text(order), reply_markup=_order_keyboard(order_id))


@router_dp.message(CommandStart())
async def start_plain(message: Message) -> None:
    await message.answer("Добро пожаловать в бота приёмной комиссии og1! Ссылку на вашу заявку пришлёт менеджер.")


@router_dp.callback_query(F.data.startswith("handoff:"))
async def handle_handoff(callback: CallbackQuery, bot: Bot) -> None:
    order_id = int(callback.data.removeprefix("handoff:"))
    with db.session() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        db.mark_handed_off(conn, order_id)
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (order["lead_id"],)).fetchone()
        db.create_handoff(conn, order_id, order["lead_id"], score=None, summary=order["summary"] or "")

    price_label = f"{order['price']:,.0f} ₽".replace(",", " ") if order["price"] else "по запросу"
    text = HANDOFF_MESSAGE_TEMPLATE.format(tariff=order["tariff"], price_label=price_label)
    await callback.message.edit_text(text)
    await callback.answer()

    if CONFIG.operator_chat and not CONFIG.dry_run:
        card = OPERATOR_CARD_TEMPLATE.format(
            username=(lead["username"] or "без username") if lead else "?",
            tg_id=lead["tg_id"] if lead else "?",
            tariff=order["tariff"],
            price_label=price_label,
            department=order["department"],
            score="—",
            summary=order["summary"] or "",
        )
        await bot.send_message(CONFIG.operator_chat, card)


def build_bot() -> Bot:
    return Bot(token=CONFIG.bot_token)


async def notify_status_change(bot: Bot, tg_id: int, text: str) -> None:
    if CONFIG.dry_run:
        logger.info("[DRY_RUN] notify %s: %s", tg_id, text)
        return
    await bot.send_message(tg_id, text)


def deep_link_for_order(order_id: int) -> str:
    return f"https://t.me/{CONFIG.bot_username}?start=order_{order_id}"
