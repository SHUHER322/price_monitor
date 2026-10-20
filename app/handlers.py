"""Обработчики: что бот отвечает на каждое действие пользователя.

Порядок важен: aiogram проверяет обработчики сверху вниз и вызывает первый
подходящий. Поэтому кнопки меню и «Отменить» стоят первыми — они работают
на любом шаге диалога.
"""
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .config import Config
from .db import Database
from .excel import build_workbook, export_filename
from .formatting import (esc, parse_rubles, product_card, products_preview,
                         trackings_text)
from .keyboards import (BTN_CANCEL, BTN_LIST, BTN_PRODUCT, BTN_SEARCH, BTN_SELLER,
                        BTN_SKIP, DELETE_PREFIX, EXPORT_PREFIX, cancel_kb, export_kb,
                        list_kb, main_kb, price_kb)
from .sources import wb
from .sources.base import BlockedError, ProductInfo, SourceError, format_price
from .sources.wb import WBSource
from .states import AddProduct, AddSearch, AddSeller

log = logging.getLogger(__name__)
router = Router()

WAIT_TEXT = "⏳ Проверяю на Wildberries… Иногда это занимает до минуты."
MIN_QUERY, MAX_QUERY = 2, 100


def error_text(error: SourceError) -> str:
    if isinstance(error, BlockedError):
        return "😔 Wildberries сейчас не пускает. Попробуйте через несколько минут."
    return f"😔 Не получилось: {esc(str(error))}"


async def limit_reached(message: Message, db: Database, config: Config) -> bool:
    """Не больше max_trackings отслеживаний на человека — бережём IP от бана WB."""
    if await db.count_trackings(message.from_user.id) >= config.max_trackings:
        await message.answer(f"Достигнут лимит: {config.max_trackings} отслеживаний. "
                             "Удалите что-нибудь в «Мои отслеживания».", reply_markup=main_kb)
        return True
    return False


# --- общие команды: работают на любом шаге ---

@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я слежу за ценами на Wildberries и сообщаю, когда они меняются.\n\n"
        f"{BTN_PRODUCT} — следить за одним товаром (можно задать желаемую цену)\n"
        f"{BTN_SELLER} — следить за всеми товарами продавца\n"
        f"{BTN_SEARCH} — следить за топом поисковой выдачи\n\n"
        "Выберите в меню 👇", reply_markup=main_kb)


@router.message(F.text == BTN_CANCEL)
@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb)


@router.message(F.text == BTN_LIST)
@router.message(Command("list"))
async def show_list(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    trackings = await db.list_trackings(message.from_user.id)
    products = [t.target for t in trackings if t.kind == "product"]
    last = await db.last_prices("wb", products)
    await message.answer(trackings_text(trackings, last), reply_markup=list_kb(trackings))


@router.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_tracking(callback: CallbackQuery, db: Database) -> None:
    tracking_id = int(callback.data.removeprefix(DELETE_PREFIX))
    removed = await db.remove_tracking(callback.from_user.id, tracking_id)
    await callback.answer("Удалено" if removed else "Уже удалено")
    # Обновляем список в том же сообщении
    trackings = await db.list_trackings(callback.from_user.id)
    products = [t.target for t in trackings if t.kind == "product"]
    last = await db.last_prices("wb", products)
    try:
        await callback.message.edit_text(trackings_text(trackings, last),
                                         reply_markup=list_kb(trackings))
    except TelegramBadRequest:
        pass  # текст не изменился (нажали дважды) — Telegram ругается, это не ошибка


@router.callback_query(F.data.startswith(EXPORT_PREFIX))
async def export_tracking(callback: CallbackQuery, db: Database, config: Config) -> None:
    """Выгрузка магазина/поиска в Excel. Данные — из нашей базы, к WB не ходим."""
    tracking_id = int(callback.data.removeprefix(EXPORT_PREFIX))
    tracking = await db.get_tracking(callback.from_user.id, tracking_id)
    if tracking is None:
        await callback.answer("Это отслеживание уже удалено", show_alert=True)
        return
    rows = await db.tracking_rows(tracking.id)
    if not rows:
        await callback.answer("Пока нет данных для выгрузки", show_alert=True)
        return
    await callback.answer("Готовлю файл…")
    now = datetime.now(config.timezone)
    content = build_workbook(tracking, rows, config.timezone, now)
    await callback.message.answer_document(
        BufferedInputFile(content, filename=export_filename(tracking, now)),
        caption=f"📊 {esc(tracking.title)}: {len(rows)} товаров")


# --- кнопки меню: начинают диалог (даже если другой не закончен) ---

@router.message(F.text == BTN_PRODUCT)
async def ask_product(message: Message, state: FSMContext) -> None:
    await state.set_state(AddProduct.link)
    await message.answer("Пришлите ссылку на товар Wildberries.\n"
                         "Например: https://www.wildberries.ru/catalog/683146715/detail.aspx",
                         reply_markup=cancel_kb)


@router.message(F.text == BTN_SELLER)
async def ask_seller(message: Message, state: FSMContext) -> None:
    await state.set_state(AddSeller.link)
    await message.answer("Пришлите ссылку на магазин Wildberries.\n"
                         "Например: https://www.wildberries.ru/seller/250079242\n\n"
                         "Где её взять: на странице товара нажмите на название продавца.",
                         reply_markup=cancel_kb)


@router.message(F.text == BTN_SEARCH)
async def ask_search(message: Message, state: FSMContext) -> None:
    await state.set_state(AddSearch.query)
    await message.answer("Что ищем? Напишите запрос, как в поиске Wildberries.\n"
                         "Например: стеклянный коврик для мыши", reply_markup=cancel_kb)


# --- товар ---

@router.message(AddProduct.link, F.text)
async def get_product_link(message: Message, state: FSMContext, db: Database,
                           config: Config, wb_source: WBSource) -> None:
    text = message.text.strip()
    if not wb.matches(text):
        hint = ("Это ссылка на магазин — для неё нажмите «🏪 Магазин»."
                if wb.SELLER_RE.search(text) else
                "Это не похоже на ссылку на товар Wildberries.")
        await message.answer(f"{hint}\nПришлите ссылку на товар или нажмите «Отменить».")
        return
    if await limit_reached(message, db, config):
        await state.clear()
        return

    await message.answer(WAIT_TEXT)
    try:
        info = await wb_source.fetch(text)
    except SourceError as e:
        log.warning("Товар %s: %s", text, e)
        await message.answer(error_text(e) + "\nПришлите другую ссылку или нажмите «Отменить».")
        return

    # Первая точка истории цен — сразу, чтобы было с чем сравнивать
    await db.save_snapshot([info])
    await state.update_data(article=info.article, title=info.name, price=info.price)
    await state.set_state(AddProduct.price)
    await message.answer(
        f"{product_card(info)}\n\n"
        "Желаемая цена? Напишите сумму в рублях, например <b>2500</b> — "
        "сообщу, когда цена опустится до неё.\n"
        "Или нажмите «Пропустить» — тогда сообщу о любом заметном снижении.",
        reply_markup=price_kb)


@router.message(AddProduct.price, F.text)
async def get_product_price(message: Message, state: FSMContext, db: Database) -> None:
    if message.text == BTN_SKIP:
        target_price = None
    else:
        target_price = parse_rubles(message.text)
        if target_price is None:
            await message.answer("Не понял сумму. Напишите число в рублях, например 2500, "
                                 "или нажмите «Пропустить».")
            return

    data = await state.get_data()
    await state.clear()
    _, created = await db.add_tracking(message.from_user.id, "product", "wb",
                                       data["article"], data["title"], target_price)
    if not created:
        await message.answer("Этот товар уже отслеживается.", reply_markup=main_kb)
        return
    goal = f"\nЖелаемая цена: {format_price(target_price)}" if target_price else ""
    await message.answer(f"✅ Слежу за товаром «{esc(data['title'][:60])}»{goal}",
                         reply_markup=main_kb)


# --- магазин ---

@router.message(AddSeller.link, F.text)
async def get_seller_link(message: Message, state: FSMContext, db: Database,
                          config: Config, wb_source: WBSource) -> None:
    text = message.text.strip()
    if not wb.SELLER_RE.search(text):
        hint = ("Это ссылка на товар — для неё нажмите «🛍 Товар»."
                if wb.matches(text) else "Это не похоже на ссылку на магазин Wildberries.")
        await message.answer(f"{hint}\nПришлите ссылку на магазин или нажмите «Отменить».")
        return
    if await limit_reached(message, db, config):
        await state.clear()
        return

    await message.answer(WAIT_TEXT)
    try:
        products = await wb_source.seller(text, limit=config.seller_limit)
    except SourceError as e:
        log.warning("Магазин %s: %s", text, e)
        await message.answer(error_text(e) + "\nПришлите другую ссылку или нажмите «Отменить».")
        return
    if not products:
        await message.answer("У этого продавца не нашлось товаров. Проверьте ссылку "
                             "или нажмите «Отменить».")
        return

    await state.clear()
    seller_id = wb.seller_id_from_url(text)
    title = products[0].seller or f"Продавец {seller_id}"
    tracking_id, created = await db.add_tracking(message.from_user.id, "seller", "wb",
                                                 seller_id, title)
    await save_baseline(db, tracking_id, products)
    if not created:
        await message.answer("Этот магазин уже отслеживается. Данные обновил.",
                             reply_markup=main_kb)
    else:
        await message.answer(f"✅ Слежу за магазином «{esc(title)}». "
                             "Сообщу о снижении цен и о новых товарах.", reply_markup=main_kb)
    await message.answer(f"Товаров: {len(products)}\n\n{products_preview(products)}",
                         reply_markup=export_kb(tracking_id))


# --- поиск ---

@router.message(AddSearch.query, F.text)
async def get_search_query(message: Message, state: FSMContext, db: Database,
                           config: Config, wb_source: WBSource) -> None:
    query = " ".join(message.text.split())  # лишние пробелы убираем
    if not MIN_QUERY <= len(query) <= MAX_QUERY:
        await message.answer(f"Запрос должен быть от {MIN_QUERY} до {MAX_QUERY} символов.")
        return
    if await limit_reached(message, db, config):
        await state.clear()
        return

    await message.answer(WAIT_TEXT)
    try:
        products = await wb_source.search(query, limit=config.search_limit)
    except SourceError as e:
        log.warning("Поиск %r: %s", query, e)
        await message.answer(error_text(e) + "\nПопробуйте другой запрос или нажмите «Отменить».")
        return
    if not products:
        await message.answer("По этому запросу ничего не нашлось. Попробуйте другой.")
        return

    await state.clear()
    tracking_id, created = await db.add_tracking(message.from_user.id, "search", "wb",
                                                 query.lower(), query)
    await save_baseline(db, tracking_id, products)
    if not created:
        await message.answer("Этот запрос уже отслеживается. Данные обновил.",
                             reply_markup=main_kb)
    else:
        await message.answer(f"✅ Слежу за поиском «{esc(query)}». "
                             "Сообщу о снижении цен и о новых товарах в топе.",
                             reply_markup=main_kb)
    await message.answer(f"Топ-{len(products)}:\n\n{products_preview(products)}",
                         reply_markup=export_kb(tracking_id))


async def save_baseline(db: Database, tracking_id: int, products: list[ProductInfo]) -> None:
    """Первая точка отсчёта: цены и состав магазина/выдачи на сейчас.
    С ней будут сравниваться следующие проверки."""
    await db.save_snapshot(products)
    await db.update_tracking_items(tracking_id, "wb", [p.article for p in products])


# --- всё остальное ---

@router.message(StateFilter(AddProduct, AddSeller, AddSearch))
async def wrong_input(message: Message) -> None:
    """На шаге диалога прислали не текст (фото, стикер…)."""
    await message.answer("Пришлите, пожалуйста, текстом — или нажмите «Отменить».")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие в меню 👇", reply_markup=main_kb)
