"""Кнопки бота: меню под полем ввода (reply) и кнопки под сообщением (inline)."""
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

from .db import Tracking
from .formatting import button_text, ordered

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
VIEW_PREFIX = "view:"       # открыть карточку (товара, магазина или поиска)
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


def group_kb(tracking_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой магазина/поиска."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Excel", callback_data=f"{EXPORT_PREFIX}{tracking_id}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{DELETE_PREFIX}{tracking_id}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data=LIST_CALLBACK)],
    ])


def list_kb(trackings: list[Tracking]) -> InlineKeyboardMarkup | None:
    """Под списком — по кнопке на отслеживание, в том же порядке, что и текст.
    Нажатие открывает карточку: у товара — цена, цель, график; у магазина и
    поиска — сводка и Excel. Удаление — внутри карточки.
    В callback_data — id отслеживания в базе: он не меняется после удаления других."""
    if not trackings:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text(t), callback_data=f"{VIEW_PREFIX}{t.id}")]
        for t in ordered(trackings)])
