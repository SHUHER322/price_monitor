"""Кнопки бота: меню под полем ввода (reply) и кнопки под сообщением (inline)."""
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

from .db import Tracking

# Тексты кнопок — константы: по ним же обработчики узнают нажатия
BTN_PRODUCT = "🛍 Товар"
BTN_SELLER = "🏪 Магазин"
BTN_SEARCH = "🔍 Поиск"
BTN_LIST = "📋 Мои отслеживания"
BTN_CANCEL = "❌ Отменить"
BTN_SKIP = "⏭ Пропустить"

DELETE_PREFIX = "del:"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PRODUCT), KeyboardButton(text=BTN_SELLER),
         KeyboardButton(text=BTN_SEARCH)],
        [KeyboardButton(text=BTN_LIST)],
    ],
    resize_keyboard=True,
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)

price_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SKIP)], [KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)


def delete_kb(trackings: list[Tracking]) -> InlineKeyboardMarkup | None:
    """Кнопки «🗑 1», «🗑 2»… под списком, по 4 в ряд.
    В callback_data — id отслеживания в базе, а не номер в списке:
    номер меняется после удаления, а id — нет."""
    if not trackings:
        return None
    buttons = [InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"{DELETE_PREFIX}{t.id}")
               for i, t in enumerate(trackings, 1)]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(inline_keyboard=rows)
