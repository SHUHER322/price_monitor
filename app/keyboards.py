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
BTN_MARKET_WB = "🟣 Wildberries"
BTN_MARKET_OZON = "🔵 Ozon"
MARKET_BUTTONS = {BTN_MARKET_WB: "wb", BTN_MARKET_OZON: "ozon"}

DELETE_PREFIX = "del:"
EXPORT_PREFIX = "xls:"
VIEW_PREFIX = "view:"       # открыть карточку товара
TARGET_PREFIX = "target:"   # изменить желаемую цену
RECHECK_PREFIX = "recheck:" # обновить данные товара с WB
NO_TARGET_PREFIX = "notarget:"  # убрать желаемую цену
CHART_PREFIX = "chart:"     # график истории цены
LIST_CALLBACK = "list"      # вернуться к списку отслеживаний

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

market_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_MARKET_WB), KeyboardButton(text=BTN_MARKET_OZON)],
              [KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)

price_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SKIP)], [KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)

def product_kb(tracking_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой товара. Всё меняется в том же сообщении."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Изменить цель", callback_data=f"{TARGET_PREFIX}{tracking_id}"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data=f"{RECHECK_PREFIX}{tracking_id}")],
        [InlineKeyboardButton(text="📈 График", callback_data=f"{CHART_PREFIX}{tracking_id}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{DELETE_PREFIX}{tracking_id}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data=LIST_CALLBACK)],
    ])


def target_inline_kb(tracking_id: int, has_target: bool) -> InlineKeyboardMarkup:
    """Кнопки, пока ждём новую цель: «Без цели» — только если цель есть."""
    row = [InlineKeyboardButton(text="◀️ Назад", callback_data=f"{VIEW_PREFIX}{tracking_id}")]
    if has_target:
        row.insert(0, InlineKeyboardButton(text="🚫 Без цели",
                                           callback_data=f"{NO_TARGET_PREFIX}{tracking_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def export_kb(tracking_id: int) -> InlineKeyboardMarkup:
    """Кнопка под сообщением о добавлении магазина/поиска."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📊 Выгрузить в Excel", callback_data=f"{EXPORT_PREFIX}{tracking_id}")]])


def _rows(buttons: list[InlineKeyboardButton], per_row: int = 4) -> list[list]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def list_kb(trackings: list[Tracking]) -> InlineKeyboardMarkup | None:
    """Кнопки под списком: «🔎 1» — карточка товара №1, «📊 2» — выгрузить
    магазин/поиск №2 в Excel, «🗑 1» — удалить №1. По 4 в ряд.
    В callback_data — id отслеживания в базе, а не номер в списке:
    номер меняется после удаления, а id — нет."""
    if not trackings:
        return None
    # Для товара — карточка, для магазина и поиска — Excel; в порядке списка
    open_buttons = [
        InlineKeyboardButton(text=f"🔎 {i}", callback_data=f"{VIEW_PREFIX}{t.id}")
        if t.kind == "product" else
        InlineKeyboardButton(text=f"📊 {i}", callback_data=f"{EXPORT_PREFIX}{t.id}")
        for i, t in enumerate(trackings, 1)]
    delete = [InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"{DELETE_PREFIX}{t.id}")
              for i, t in enumerate(trackings, 1)]
    return InlineKeyboardMarkup(inline_keyboard=_rows(open_buttons) + _rows(delete))
