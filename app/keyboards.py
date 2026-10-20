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
EXPORT_PREFIX = "xls:"

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


def export_kb(tracking_id: int) -> InlineKeyboardMarkup:
    """Кнопка под сообщением о добавлении магазина/поиска."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📊 Выгрузить в Excel", callback_data=f"{EXPORT_PREFIX}{tracking_id}")]])


def _rows(buttons: list[InlineKeyboardButton], per_row: int = 4) -> list[list]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def list_kb(trackings: list[Tracking]) -> InlineKeyboardMarkup | None:
    """Кнопки под списком: «📊 2» — выгрузить магазин/поиск №2 в Excel,
    «🗑 1» — удалить №1. По 4 в ряд.
    В callback_data — id отслеживания в базе, а не номер в списке:
    номер меняется после удаления, а id — нет."""
    if not trackings:
        return None
    export = [InlineKeyboardButton(text=f"📊 {i}", callback_data=f"{EXPORT_PREFIX}{t.id}")
              for i, t in enumerate(trackings, 1) if t.kind in ("seller", "search")]
    delete = [InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"{DELETE_PREFIX}{t.id}")
              for i, t in enumerate(trackings, 1)]
    return InlineKeyboardMarkup(inline_keyboard=_rows(export) + _rows(delete))
