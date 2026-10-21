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
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from .chart import build_price_chart, can_draw, checks_word
from .checker import Checker, change_notes, find_changes
from .config import Config
from .db import Database
from .excel import build_workbook, export_filename, to_local
from .formatting import (esc, group_summary_text, parse_rubles, product_card,
                         product_summary_text, products_preview, sizes_prompt, trackings_text)
from .keyboards import (BTN_CANCEL, BTN_LIST, BTN_PRODUCT, BTN_SEARCH, BTN_SELLER, BTN_SKIP,
                        CHART_PREFIX, DELETE_PREFIX, MARKET_BUTTONS, market_kb, EXPORT_PREFIX, LIST_CALLBACK, NO_TARGET_PREFIX,
                        RECHECK_PREFIX, TARGET_PREFIX, VIEW_PREFIX, cancel_kb, export_kb,
                        PICK_DONE, PICK_PREFIX, SIZE_PREFIX, SIZES_PREFIX,
                        group_kb, list_kb, main_kb, price_kb, product_kb, sizes_kb,
                        target_inline_kb)
from .sources import detect, ozon, wb
from .sources.base import BlockedError, ProductInfo, SourceError, format_price
from .states import AddProduct, AddSearch, AddSeller, EditTarget

log = logging.getLogger(__name__)
router = Router()

WAIT_TEXT = "⏳ Проверяю на Wildberries… Иногда это занимает до минуты."
WAIT_TEXT_OZON = ("⏳ Проверяю на Ozon… Это около 15 секунд: Ozon пускает только "
                  "настоящий браузер, поэтому ненадолго откроется окно Chrome.")
MIN_QUERY, MAX_QUERY = 2, 100


def error_text(error: SourceError) -> str:
    if isinstance(error, BlockedError):
        return "😔 Маркетплейс сейчас не пускает. Попробуйте через несколько минут."
    return f"😔 Не получилось: {esc(str(error))}"


async def limit_reached(message: Message, db: Database, config: Config) -> bool:
    """Не больше max_trackings отслеживаний на человека — бережём IP от бана маркетплейсов."""
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
        "Привет! Я слежу за ценами на Wildberries и Ozon и сообщаю, когда они меняются.\n\n"
        f"{BTN_PRODUCT} — следить за товаром WB или Ozon (можно задать желаемую цену)\n"
        f"{BTN_SELLER} — следить за всеми товарами продавца WB или Ozon\n"
        f"{BTN_SEARCH} — следить за топом поисковой выдачи WB или Ozon\n\n"
        "Цены проверяю сам по расписанию, а /check — проверить прямо сейчас.\n\n"
        "Выберите в меню 👇", reply_markup=main_kb)


@router.message(F.text == BTN_CANCEL)
@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    editing = await state.get_state() == EditTarget.price.state
    data = await state.get_data()
    await state.clear()
    if editing:
        # Отменили смену цели — то же сообщение снова становится карточкой товара
        await show_card_in(message.bot, data, message.from_user.id, db, config)
        await safe_delete(message)
        return
    await message.answer("Отменено.", reply_markup=main_kb)


@router.message(Command("check"))
async def check_now(message: Message, state: FSMContext, checker: Checker,
                    db: Database) -> None:
    """Проверить свои отслеживания, не дожидаясь расписания.
    Уведомления об изменениях придут отдельными сообщениями, как обычно."""
    await state.clear()
    if not await db.count_trackings(message.from_user.id):
        await message.answer("Пока нечего проверять: вы ничего не отслеживаете.\n"
                             "Выберите в меню, за чем следить: товар, магазин или поиск.",
                             reply_markup=main_kb)
        return
    if checker.running:
        await message.answer("Проверка уже идёт — уведомления придут, когда она закончится.")
        return
    await message.answer("⏳ Проверяю ваши отслеживания… Это может занять пару минут.")
    sent = await checker.run(message.from_user.id)
    await message.answer(f"Готово. Сообщений об изменениях: {sent}." if sent
                         else "Готово. Заметных изменений нет.", reply_markup=main_kb)


async def list_view(db: Database, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и кнопки списка «Мои отслеживания»."""
    trackings = await db.list_trackings(user_id)
    last = {}
    for source in {t.source for t in trackings if t.kind == "product"}:
        articles = [t.target for t in trackings if t.kind == "product" and t.source == source]
        for article, point in (await db.last_prices(source, articles)).items():
            last[(source, article)] = point
    return trackings_text(trackings, last), list_kb(trackings)


async def edit(message: Message, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    """Заменить текст и кнопки сообщения на месте — без новых сообщений в чате."""
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass  # текст не изменился (нажали дважды) — Telegram ругается, это не ошибка


async def safe_delete(message: Message) -> None:
    """Убрать сообщение пользователя (например, введённую сумму), чтобы не было мусора."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


@router.message(F.text == BTN_LIST)
@router.message(Command("list"))
async def show_list(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    text, keyboard = await list_view(db, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == LIST_CALLBACK)
async def back_to_list(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await callback.answer()
    await edit(callback.message, *await list_view(db, callback.from_user.id))


@router.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_tracking(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    tracking_id = int(callback.data.removeprefix(DELETE_PREFIX))
    removed = await db.remove_tracking(callback.from_user.id, tracking_id)
    await callback.answer("Удалено" if removed else "Уже удалено")
    # То же сообщение (список или карточка) становится обновлённым списком
    await edit(callback.message, *await list_view(db, callback.from_user.id))


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


# --- карточка товара: всё происходит в одном сообщении ---

async def own_product(callback: CallbackQuery, db: Database, prefix: str):
    """Отслеживание товара из нажатой кнопки — если оно есть и принадлежит пользователю."""
    tracking = await db.get_tracking(callback.from_user.id,
                                     int(callback.data.removeprefix(prefix)))
    if tracking is None or tracking.kind != "product":
        await callback.answer("Это отслеживание уже удалено", show_alert=True)
        return None
    return tracking


async def card_view(db: Database, config: Config, tracking,
                    notes: list[str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки карточки. notes — строки сверху: что только что изменилось."""
    summary = await db.product_summary(tracking.source, tracking.target)
    if summary is None:
        text = "По этому товару ещё нет данных. Нажмите «🔄 Обновить»."
    else:
        text = product_summary_text(tracking, summary,
                                    to_local(summary.checked_at, config.timezone))
    if notes:
        text = "\n".join(notes) + "\n\n" + text
    return text, product_kb(tracking.id, has_sizes=bool(size_names(summary)))


def size_names(summary) -> list[str]:
    """Названия размеров товара на последней проверке (пусто — размеров нет)."""
    if summary is None:
        return []
    return [s["name"] for s in summary.sizes if s.get("name")]


async def show_card_in(bot, data: dict, user_id: int, db: Database, config: Config,
                       notes: list[str] | None = None) -> None:
    """Превратить запомненное сообщение (вопрос о цели) обратно в карточку."""
    tracking = await db.get_tracking(user_id, data["tracking_id"])
    if tracking is None:
        text, keyboard = await list_view(db, user_id)
    else:
        text, keyboard = await card_view(db, config, tracking, notes)
    try:
        await bot.edit_message_text(text, chat_id=data["chat_id"],
                                    message_id=data["message_id"], reply_markup=keyboard)
    except TelegramBadRequest:
        pass


async def group_view(db: Database, config: Config, tracking) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки карточки магазина/поиска."""
    rows = await db.tracking_rows(tracking.id)
    checked = max((r.checked_at for r in rows if r.checked_at), default=None)
    text = group_summary_text(tracking, rows, to_local(checked, config.timezone))
    return text, group_kb(tracking.id)


@router.callback_query(F.data.startswith(VIEW_PREFIX))
async def view_tracking(callback: CallbackQuery, state: FSMContext, db: Database,
                        config: Config) -> None:
    """Карточка из списка — из нашей базы, мгновенно и без запросов к маркетплейсу.
    Товар — цена, цель, график; магазин и поиск — сводка и Excel.
    Сюда же ведёт «◀️ Назад» из вопроса о цели."""
    await state.clear()
    tracking = await db.get_tracking(callback.from_user.id,
                                     int(callback.data.removeprefix(VIEW_PREFIX)))
    if tracking is None:
        await callback.answer("Это отслеживание уже удалено", show_alert=True)
        return
    await callback.answer()
    view = card_view if tracking.kind == "product" else group_view
    await edit(callback.message, *await view(db, config, tracking))


@router.callback_query(F.data.startswith(RECHECK_PREFIX))
async def recheck_product(callback: CallbackQuery, db: Database, config: Config,
                          sources: dict) -> None:
    """Свежие данные с маркетплейса по одному товару (у WB — с точными остатками
    по размерам)."""
    tracking = await own_product(callback, db, RECHECK_PREFIX)
    if tracking is None:
        return
    await callback.answer("Обновляю…")
    old = (await db.last_prices(tracking.source, [tracking.target])).get(tracking.target)
    # Ozon запрашивает товар по ссылке — берём её из базы
    urls = await db.product_urls(tracking.source, [tracking.target])
    try:
        info = await sources[tracking.source].fetch(urls.get(tracking.target, tracking.target))
    except SourceError as e:
        log.warning("Обновление товара %s: %s", tracking.target, e)
        await edit(callback.message, *await card_view(db, config, tracking, [error_text(e)]))
        return
    changes = find_changes(old, info, config.price_drop_percent, tracking.target_price,
                           tracking.sizes)
    await db.save_snapshot([info])
    notes = change_notes(tracking, changes) or ["🔄 Обновлено, заметных изменений нет"]
    await edit(callback.message, *await card_view(db, config, tracking, notes))


@router.callback_query(F.data.startswith(CHART_PREFIX))
async def price_chart(callback: CallbackQuery, db: Database, config: Config) -> None:
    """График истории цены. Картинку нельзя вставить в текстовое сообщение,
    поэтому она приходит отдельным сообщением под карточкой."""
    tracking = await own_product(callback, db, CHART_PREFIX)
    if tracking is None:
        return
    history = await db.price_history(tracking.source, tracking.target)
    if not can_draw(history):
        await callback.answer("Пока мало данных: график появится после нескольких проверок",
                              show_alert=True)
        return
    await callback.answer("Рисую график…")
    png = build_price_chart(tracking.title, history, config.timezone, tracking.target_price)
    await callback.message.answer_photo(BufferedInputFile(png, filename="price.png"),
                                        caption=f"📈 {esc(tracking.title[:80])}: "
                                                f"{checks_word(len(history))}")


@router.callback_query(F.data.startswith(TARGET_PREFIX))
async def ask_target(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    tracking = await own_product(callback, db, TARGET_PREFIX)
    if tracking is None:
        return
    await callback.answer()
    # Запоминаем, какое сообщение потом превратить обратно в карточку
    await state.set_state(EditTarget.price)
    await state.update_data(tracking_id=tracking.id, chat_id=callback.message.chat.id,
                            message_id=callback.message.message_id)
    now = (f"Сейчас цель: {format_price(tracking.target_price)}.\n"
           if tracking.target_price else "")
    await edit(callback.message,
               f"🎯 <b>{esc(tracking.title[:60])}</b>\n{now}"
               "Напишите новую желаемую цену в рублях, например <b>2500</b>.",
               target_inline_kb(tracking.id, has_target=bool(tracking.target_price)))


@router.callback_query(F.data.startswith(NO_TARGET_PREFIX))
async def remove_target(callback: CallbackQuery, state: FSMContext, db: Database,
                        config: Config) -> None:
    tracking = await own_product(callback, db, NO_TARGET_PREFIX)
    if tracking is None:
        return
    await state.clear()
    await db.set_target_price(callback.from_user.id, tracking.id, None)
    await callback.answer("Цель убрана")
    tracking = await db.get_tracking(callback.from_user.id, tracking.id)
    await edit(callback.message, *await card_view(
        db, config, tracking, ["🚫 Цель убрана — сообщу о любом заметном снижении цены"]))


# --- размеры отслеживаемого товара (из карточки) ---

@router.callback_query(F.data.startswith(SIZES_PREFIX))
async def open_sizes(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    tracking = await own_product(callback, db, SIZES_PREFIX)
    if tracking is None:
        return
    names = size_names(await db.product_summary(tracking.source, tracking.target))
    if not names:
        await callback.answer("У этого товара нет размеров", show_alert=True)
        return
    await callback.answer()
    await edit(callback.message, sizes_prompt(tracking.title, tracking.sizes),
               sizes_kb(names, tracking.sizes, tracking.id))


@router.callback_query(F.data.startswith(SIZE_PREFIX))
async def toggle_size(callback: CallbackQuery, db: Database) -> None:
    """Отметить или снять размер — сразу сохраняется в базе."""
    tracking_id, _, index = callback.data.removeprefix(SIZE_PREFIX).partition(":")
    tracking = await db.get_tracking(callback.from_user.id, int(tracking_id))
    if tracking is None:
        await callback.answer("Это отслеживание уже удалено", show_alert=True)
        return
    names = size_names(await db.product_summary(tracking.source, tracking.target))
    if not index.isdigit() or int(index) >= len(names):
        await callback.answer("Список размеров изменился — откройте его заново", show_alert=True)
        return
    chosen = toggled(list(tracking.sizes), names[int(index)])
    await db.set_sizes(callback.from_user.id, tracking.id, chosen)
    await callback.answer()
    await edit(callback.message, sizes_prompt(tracking.title, chosen),
               sizes_kb(names, chosen, tracking.id))


def toggled(chosen: list[str], name: str) -> list[str]:
    """Отметить размер или снять отметку."""
    return [n for n in chosen if n != name] if name in chosen else chosen + [name]


# --- кнопки меню: начинают диалог (даже если другой не закончен) ---

@router.message(F.text == BTN_PRODUCT)
async def ask_product(message: Message, state: FSMContext) -> None:
    await state.set_state(AddProduct.link)
    await message.answer("Пришлите ссылку на товар Wildberries или Ozon.\n"
                         "Например: https://www.wildberries.ru/catalog/683146715/detail.aspx",
                         reply_markup=cancel_kb)


@router.message(F.text == BTN_SELLER)
async def ask_seller(message: Message, state: FSMContext) -> None:
    await state.set_state(AddSeller.link)
    await message.answer("Пришлите ссылку на магазин Wildberries или Ozon.\n"
                         "Например: https://www.wildberries.ru/seller/250079242\n"
                         "или https://www.ozon.ru/seller/ridberg/\n\n"
                         "Где её взять: на странице товара нажмите на название продавца.",
                         reply_markup=cancel_kb)


@router.message(F.text == BTN_SEARCH)
async def ask_search(message: Message, state: FSMContext) -> None:
    await state.set_state(AddSearch.market)
    await message.answer("Где ищем?", reply_markup=market_kb)


# --- изменение желаемой цены (после кнопок меню: они важнее) ---

@router.message(EditTarget.price, F.text)
async def get_target(message: Message, state: FSMContext, db: Database,
                     config: Config) -> None:
    data = await state.get_data()
    # Введённую сумму убираем из чата — всё видно в карточке
    await safe_delete(message)
    target_price = parse_rubles(message.text)
    if target_price is None:
        tracking = await db.get_tracking(message.from_user.id, data["tracking_id"])
        if tracking is not None:
            try:
                await message.bot.edit_message_text(
                    f"🎯 <b>{esc(tracking.title[:60])}</b>\n"
                    f"Не понял «{esc(message.text[:30])}». Напишите число в рублях, "
                    "например <b>2500</b>.", chat_id=data["chat_id"],
                    message_id=data["message_id"],
                    reply_markup=target_inline_kb(tracking.id, bool(tracking.target_price)))
            except TelegramBadRequest:
                pass
        return
    await state.clear()
    await db.set_target_price(message.from_user.id, data["tracking_id"], target_price)
    await show_card_in(message.bot, data, message.from_user.id, db, config,
                       [f"🎯 Желаемая цена: {format_price(target_price)}"])


# --- товар ---

@router.message(AddProduct.link, F.text)
async def get_product_link(message: Message, state: FSMContext, db: Database,
                           config: Config, sources: dict) -> None:
    text = message.text.strip()
    source = detect(text)
    if source is None:
        if wb.SELLER_RE.search(text):
            hint = "Это ссылка на магазин — для неё нажмите «🏪 Магазин»."
        elif ozon.SELLER_RE.search(text):
            hint = "Это ссылка на магазин — для неё нажмите «🏪 Магазин»."
        else:
            hint = "Это не похоже на ссылку на товар Wildberries или Ozon."
        await message.answer(f"{hint}\nПришлите ссылку на товар или нажмите «Отменить».")
        return
    if await limit_reached(message, db, config):
        await state.clear()
        return

    await message.answer(WAIT_TEXT_OZON if source == "ozon" else WAIT_TEXT)
    try:
        info = await sources[source].fetch(text)
    except SourceError as e:
        log.warning("Товар %s: %s", text, e)
        await message.answer(error_text(e) + "\nПришлите другую ссылку или нажмите «Отменить».")
        return

    # Первая точка истории цен — сразу, чтобы было с чем сравнивать
    await db.save_snapshot([info])
    await state.update_data(source=info.source, article=info.article, title=info.name,
                            price=info.price, chosen=[])
    if not info.has_sizes:
        await message.answer(product_card(info))
        await ask_price(message, state)
        return

    # Одежда и обувь: сначала выбор размеров. У Ozon ссылка ведёт на конкретный
    # размер (это отдельный товар) — его отмечаем сразу
    names = [s.name for s in info.sizes if s.name]
    chosen = [s.name for s in info.sizes if s.article and s.article == info.article]
    await state.update_data(sizes=names, chosen=chosen)
    await state.set_state(AddProduct.sizes)
    await message.answer(product_card(info))
    await message.answer(sizes_prompt(info.name, chosen), reply_markup=sizes_kb(names, chosen))


async def ask_price(message: Message, state: FSMContext) -> None:
    await state.set_state(AddProduct.price)
    await message.answer(
        "Желаемая цена? Напишите сумму в рублях, например <b>2500</b> — "
        "сообщу, когда цена опустится до неё.\n"
        "Или нажмите «Пропустить» — тогда сообщу о любом заметном снижении.",
        reply_markup=price_kb)


@router.callback_query(AddProduct.sizes, F.data.startswith(PICK_PREFIX))
async def pick_size(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    names, index = data["sizes"], callback.data.removeprefix(PICK_PREFIX)
    if not index.isdigit() or int(index) >= len(names):
        await callback.answer()
        return
    chosen = toggled(data["chosen"], names[int(index)])
    await state.update_data(chosen=chosen)
    await callback.answer()
    await edit(callback.message, sizes_prompt(data["title"], chosen), sizes_kb(names, chosen))


@router.callback_query(AddProduct.sizes, F.data == PICK_DONE)
async def sizes_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    picked = ", ".join(esc(n) for n in data["chosen"]) if data["chosen"] else "любой"
    # Кнопки убираем: выбор сделан, дальше — вопрос о цене
    await edit(callback.message, f"📏 Размеры: <b>{picked}</b>", None)
    await ask_price(callback.message, state)


@router.callback_query(F.data.startswith(PICK_PREFIX) | (F.data == PICK_DONE))
async def pick_outdated(callback: CallbackQuery) -> None:
    """Нажали на кнопки размеров после отмены или уже добавленного товара."""
    await callback.answer("Этот выбор уже неактуален — добавьте товар заново", show_alert=True)


@router.message(AddProduct.sizes)
async def sizes_by_text(message: Message) -> None:
    await message.answer("Отметьте размеры кнопками под сообщением и нажмите «✔️ Готово». "
                         "Ничего не отмечено — слежу за любым размером.")


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
    chosen = data.get("chosen") or []
    _, created = await db.add_tracking(message.from_user.id, "product", data["source"],
                                       data["article"], data["title"], target_price, chosen)
    if not created:
        await message.answer("Этот товар уже отслеживается.", reply_markup=main_kb)
        return
    goal = f"\nЖелаемая цена: {format_price(target_price)}" if target_price else ""
    sizes = (f"\nРазмеры: {esc(', '.join(chosen))} — сообщу, когда появятся или закончатся"
             if chosen else "")
    await message.answer(f"✅ Слежу за товаром «{esc(data['title'][:60])}»{goal}{sizes}",
                         reply_markup=main_kb)


# --- магазин ---

@router.message(AddSeller.link, F.text)
async def get_seller_link(message: Message, state: FSMContext, db: Database,
                          config: Config, sources: dict) -> None:
    text = message.text.strip()
    if wb.SELLER_RE.search(text):
        source, seller_id = "wb", wb.seller_id_from_url(text)
    elif ozon.matches_seller(text):
        source, seller_id = "ozon", ozon.seller_id_from_url(text)
    else:
        hint = ("Это ссылка на товар — для неё нажмите «🛍 Товар»." if detect(text)
                else "Это не похоже на ссылку на магазин Wildberries или Ozon.")
        await message.answer(f"{hint}\nПришлите ссылку на магазин или нажмите «Отменить».")
        return
    if await limit_reached(message, db, config):
        await state.clear()
        return

    if source == "ozon":
        await message.answer(f"{WAIT_TEXT_OZON}\nМагазин Ozon листается медленно, "
                             f"беру первые {ozon.SELLER_LIMIT_CAP} популярных товаров — "
                             "это около минуты.")
    else:
        await message.answer(WAIT_TEXT)
    try:
        products = await sources[source].seller(text, limit=config.seller_limit)
    except SourceError as e:
        log.warning("Магазин %s: %s", text, e)
        await message.answer(error_text(e) + "\nПришлите другую ссылку или нажмите «Отменить».")
        return
    if not products:
        await message.answer("У этого продавца не нашлось товаров. Проверьте ссылку "
                             "или нажмите «Отменить».")
        return

    await state.clear()
    title = products[0].seller or f"Продавец {seller_id}"
    tracking_id, created = await db.add_tracking(message.from_user.id, "seller", source,
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

@router.message(AddSearch.market, F.text)
async def get_search_market(message: Message, state: FSMContext) -> None:
    source = MARKET_BUTTONS.get(message.text)
    if source is None:
        await message.answer("Выберите кнопкой: Wildberries или Ozon.", reply_markup=market_kb)
        return
    await state.update_data(source=source)
    await state.set_state(AddSearch.query)
    market = "Wildberries" if source == "wb" else "Ozon"
    await message.answer(f"Что ищем на {market}? Напишите запрос, как в поиске.\n"
                         "Например: стеклянный коврик для мыши", reply_markup=cancel_kb)


@router.message(AddSearch.query, F.text)
async def get_search_query(message: Message, state: FSMContext, db: Database,
                           config: Config, sources: dict) -> None:
    source = (await state.get_data()).get("source", "wb")
    query = " ".join(message.text.split())  # лишние пробелы убираем
    if not MIN_QUERY <= len(query) <= MAX_QUERY:
        await message.answer(f"Запрос должен быть от {MIN_QUERY} до {MAX_QUERY} символов.")
        return
    if await limit_reached(message, db, config):
        await state.clear()
        return

    await message.answer(WAIT_TEXT_OZON if source == "ozon" else WAIT_TEXT)
    try:
        products = await sources[source].search(query, limit=config.search_limit)
    except SourceError as e:
        log.warning("Поиск %r: %s", query, e)
        await message.answer(error_text(e) + "\nПопробуйте другой запрос или нажмите «Отменить».")
        return
    if not products:
        await message.answer("По этому запросу ничего не нашлось. Попробуйте другой.")
        return

    await state.clear()
    tracking_id, created = await db.add_tracking(message.from_user.id, "search", source,
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
    # Источник товаров берём у самих товаров: у Ozon в списках это OZON_CARD
    await db.update_tracking_items(tracking_id, products[0].source,
                                   [p.article for p in products])


# --- всё остальное ---

@router.message(StateFilter(AddProduct, AddSeller, AddSearch, EditTarget))
async def wrong_input(message: Message) -> None:
    """На шаге диалога прислали не текст (фото, стикер…)."""
    await message.answer("Пришлите, пожалуйста, текстом — или нажмите «Отменить».")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие в меню 👇", reply_markup=main_kb)
