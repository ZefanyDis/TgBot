"""
Telegram-бот для работы с заметками.

Стек: aiogram 3.x (>=3.7) + синхронный SQLAlchemy 2.x.

Установка:
    pip install "aiogram>=3.7" "SQLAlchemy>=2.0"

Запуск:
    export BOT_TOKEN="123456:ABC..."
    # необязательно (по умолчанию SQLite-файл notes.db рядом со скриптом):
    # export DATABASE_URL="postgresql+psycopg2://user:pass@host/dbname"
    python notes_bot.py
"""
import asyncio
import html
import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import BigInteger, DateTime, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///notes.db")

PAGE_SIZE = 8          # заметок на одной странице списка
MAX_TITLE_LEN = 100    # максимальная длина заголовка
MAX_TEXT_LEN = 3500    # максимальная длина текста заметки

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("notes_bot")

# --------------------------------------------------------------------------- #
# База данных (синхронный SQLAlchemy)
# --------------------------------------------------------------------------- #


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(MAX_TITLE_LEN))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def db_create_note(user_id: int, title: str, text: str) -> Note:
    with SessionLocal() as session:
        note = Note(user_id=user_id, title=title, text=text)
        session.add(note)
        session.commit()
        session.refresh(note)
        return note


def db_get_note(user_id: int, note_id: int) -> Optional[Note]:
    with SessionLocal() as session:
        note = session.get(Note, note_id)
        if note is None or note.user_id != user_id:
            return None
        return note


def db_list_notes(user_id: int, page: int) -> tuple[list[Note], int]:
    """Возвращает (заметки страницы, общее количество заметок)."""
    with SessionLocal() as session:
        total = session.scalar(
            select(func.count()).select_from(Note).where(Note.user_id == user_id)
        ) or 0
        notes = session.scalars(
            select(Note)
            .where(Note.user_id == user_id)
            .order_by(Note.id.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()
        return list(notes), total


def db_update_note(user_id: int, note_id: int, **fields) -> Optional[Note]:
    with SessionLocal() as session:
        note = session.get(Note, note_id)
        if note is None or note.user_id != user_id:
            return None
        for key, value in fields.items():
            setattr(note, key, value)
        session.commit()
        session.refresh(note)
        return note


def db_delete_note(user_id: int, note_id: int) -> bool:
    with SessionLocal() as session:
        note = session.get(Note, note_id)
        if note is None or note.user_id != user_id:
            return False
        session.delete(note)
        session.commit()
        return True


async def run_db(func_, *args, **kwargs):
    """Синхронные вызовы БД выполняем в потоке, чтобы не блокировать event loop."""
    return await asyncio.to_thread(func_, *args, **kwargs)


# --------------------------------------------------------------------------- #
# Callback-данные и состояния FSM
# --------------------------------------------------------------------------- #


class NoteCB(CallbackData, prefix="n"):
    action: str
    note_id: int = 0
    page: int = 0


class CreateNote(StatesGroup):
    title = State()
    text = State()


class EditNote(StatesGroup):
    title = State()
    text = State()


# --------------------------------------------------------------------------- #
# Клавиатуры и экраны
# --------------------------------------------------------------------------- #

esc = html.escape


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать заметку", callback_data=NoteCB(action="new"))
    kb.button(text="📋 Мои заметки", callback_data=NoteCB(action="list", page=0))
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NoteCB(action="cancel"))
    return kb.as_markup()


def list_kb(notes: list[Note], total: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in notes:
        title = n.title if len(n.title) <= 40 else n.title[:37] + "..."
        kb.button(text=f"📝 {title}", callback_data=NoteCB(action="view", note_id=n.id, page=page))
    kb.adjust(1)

    pages = max(1, -(-total // PAGE_SIZE))
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=NoteCB(action="list", page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=NoteCB(action="noop"))
    if page + 1 < pages:
        nav.button(text="▶️", callback_data=NoteCB(action="list", page=page + 1))
    nav.adjust(3)
    kb.attach(nav)

    bottom = InlineKeyboardBuilder()
    bottom.button(text="➕ Создать", callback_data=NoteCB(action="new"))
    bottom.button(text="🏠 Меню", callback_data=NoteCB(action="menu"))
    bottom.adjust(2)
    kb.attach(bottom)
    return kb.as_markup()


def note_kb(note_id: int, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data=NoteCB(action="edit", note_id=note_id, page=page))
    kb.button(text="🗑 Удалить", callback_data=NoteCB(action="del", note_id=note_id, page=page))
    kb.button(text="⬅️ К списку", callback_data=NoteCB(action="list", page=page))
    kb.adjust(2, 1)
    return kb.as_markup()


def edit_choice_kb(note_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔤 Заголовок", callback_data=NoteCB(action="edit_title", note_id=note_id, page=page))
    kb.button(text="📄 Текст", callback_data=NoteCB(action="edit_text", note_id=note_id, page=page))
    kb.button(text="⬅️ Назад", callback_data=NoteCB(action="view", note_id=note_id, page=page))
    kb.adjust(2, 1)
    return kb.as_markup()


def delete_confirm_kb(note_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=NoteCB(action="del_yes", note_id=note_id, page=page))
    kb.button(text="↩️ Нет", callback_data=NoteCB(action="view", note_id=note_id, page=page))
    kb.adjust(2)
    return kb.as_markup()


def render_note(note: Note) -> str:
    created = note.created_at.strftime("%d.%m.%Y %H:%M") if note.created_at else "—"
    updated = note.updated_at.strftime("%d.%m.%Y %H:%M") if note.updated_at else "—"
    text = f"📝 <b>{esc(note.title)}</b>\n\n{esc(note.text)}\n\n<i>Создана: {created}</i>"
    if updated != created:
        text += f"\n<i>Изменена: {updated}</i>"
    return text


MENU_TEXT = "👋 <b>Бот для заметок</b>\n\nВыберите действие:"


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Редактирует сообщение (для callback) или отправляет новое (для message)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    else:
        await target.answer(text, reply_markup=kb)


async def show_list(target: Message | CallbackQuery, user_id: int, page: int) -> None:
    notes, total = await run_db(db_list_notes, user_id, page)
    # если страница опустела (например, после удаления) — возвращаемся назад
    if not notes and total > 0 and page > 0:
        page = max(0, (total - 1) // PAGE_SIZE)
        notes, total = await run_db(db_list_notes, user_id, page)

    if total == 0:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать заметку", callback_data=NoteCB(action="new"))
        kb.button(text="🏠 Меню", callback_data=NoteCB(action="menu"))
        kb.adjust(1)
        await show(target, "📭 У вас пока нет заметок.", kb.as_markup())
        return

    await show(target, f"📋 <b>Ваши заметки</b> (всего: {total}):", list_kb(notes, total, page))


async def show_note(target: Message | CallbackQuery, user_id: int, note_id: int, page: int = 0) -> None:
    note = await run_db(db_get_note, user_id, note_id)
    if note is None:
        await show_list(target, user_id, page)
        return
    await show(target, render_note(note), note_kb(note.id, page))


# --------------------------------------------------------------------------- #
# Хендлеры
# --------------------------------------------------------------------------- #

router = Router()


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(MENU_TEXT, reply_markup=main_menu_kb())


@router.callback_query(NoteCB.filter(F.action == "noop"))
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(NoteCB.filter(F.action.in_({"menu", "cancel"})))
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await show(call, MENU_TEXT, main_menu_kb())


@router.callback_query(NoteCB.filter(F.action == "list"))
async def cb_list(call: CallbackQuery, callback_data: NoteCB, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await show_list(call, call.from_user.id, callback_data.page)


@router.callback_query(NoteCB.filter(F.action == "view"))
async def cb_view(call: CallbackQuery, callback_data: NoteCB, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await show_note(call, call.from_user.id, callback_data.note_id, callback_data.page)


# ---------- Создание ----------


@router.callback_query(NoteCB.filter(F.action == "new"))
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(CreateNote.title)
    await show(call, f"✏️ Введите <b>заголовок</b> заметки (до {MAX_TITLE_LEN} символов):", cancel_kb())


@router.message(CreateNote.title, F.text)
async def create_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title or len(title) > MAX_TITLE_LEN:
        await message.answer(
            f"⚠️ Заголовок должен быть от 1 до {MAX_TITLE_LEN} символов. Попробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return
    await state.update_data(title=title)
    await state.set_state(CreateNote.text)
    await message.answer(
        f"📄 Теперь введите <b>текст</b> заметки (до {MAX_TEXT_LEN} символов):",
        reply_markup=cancel_kb(),
    )


@router.message(CreateNote.text, F.text)
async def create_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text or len(text) > MAX_TEXT_LEN:
        await message.answer(
            f"⚠️ Текст должен быть от 1 до {MAX_TEXT_LEN} символов. Попробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    await state.clear()
    note = await run_db(db_create_note, message.from_user.id, data["title"], text)
    await message.answer("✅ Заметка создана!")
    await show_note(message, message.from_user.id, note.id)





# ---------- Редактирование ----------


@router.callback_query(NoteCB.filter(F.action == "edit"))
async def cb_edit(call: CallbackQuery, callback_data: NoteCB, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await show(call, "✏️ Что хотите изменить?", edit_choice_kb(callback_data.note_id, callback_data.page))


@router.callback_query(NoteCB.filter(F.action == "edit_title"))
async def cb_edit_title(call: CallbackQuery, callback_data: NoteCB, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(EditNote.title)
    await state.update_data(note_id=callback_data.note_id)
    await show(call, f"🔤 Введите <b>новый заголовок</b> (до {MAX_TITLE_LEN} символов):", cancel_kb())


@router.callback_query(NoteCB.filter(F.action == "edit_text"))
async def cb_edit_text(call: CallbackQuery, callback_data: NoteCB, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(EditNote.text)
    await state.update_data(note_id=callback_data.note_id)
    await show(call, f"📄 Введите <b>новый текст</b> (до {MAX_TEXT_LEN} символов):", cancel_kb())


@router.message(EditNote.title, F.text)
async def edit_title_input(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title or len(title) > MAX_TITLE_LEN:
        await message.answer(
            f"⚠️ Заголовок должен быть от 1 до {MAX_TITLE_LEN} символов. Попробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    await state.clear()
    note = await run_db(db_update_note, message.from_user.id, data["note_id"], title=title)
    if note is None:
        await message.answer("⚠️ Заметка не найдена.", reply_markup=main_menu_kb())
        return
    await message.answer("✅ Заголовок обновлён!")
    await show_note(message, message.from_user.id, note.id)


@router.message(EditNote.text, F.text)
async def edit_text_input(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text or len(text) > MAX_TEXT_LEN:
        await message.answer(
            f"⚠️ Текст должен быть от 1 до {MAX_TEXT_LEN} символов. Попробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    await state.clear()
    note = await run_db(db_update_note, message.from_user.id, data["note_id"], text=text)
    if note is None:
        await message.answer("⚠️ Заметка не найдена.", reply_markup=main_menu_kb())
        return
    await message.answer("✅ Текст обновлён!")
    await show_note(message, message.from_user.id, note.id)


# ---------- Удаление ----------


@router.callback_query(NoteCB.filter(F.action == "del"))
async def cb_delete(call: CallbackQuery, callback_data: NoteCB) -> None:
    await call.answer()
    await show(
        call,
        "🗑 Вы уверены, что хотите удалить эту заметку?",
        delete_confirm_kb(callback_data.note_id, callback_data.page),
    )


@router.callback_query(NoteCB.filter(F.action == "del_yes"))
async def cb_delete_yes(call: CallbackQuery, callback_data: NoteCB) -> None:
    ok = await run_db(db_delete_note, call.from_user.id, callback_data.note_id)
    await call.answer("Заметка удалена ✅" if ok else "Заметка не найдена", show_alert=False)
    await show_list(call, call.from_user.id, callback_data.page)


# ---------- Прочие сообщения ----------


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(MENU_TEXT, reply_markup=main_menu_kb())


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Укажите его в переменной окружения.")

    Base.metadata.create_all(engine)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        engine.dispose()

@router.message(CreateNote.title)
@router.message(CreateNote.text)
@router.message(EditNote.title)
@router.message(EditNote.text)
async def only_text_allowed(message: Message) -> None:
    await message.answer("⚠️ Пожалуйста, отправьте обычный текст.", reply_markup=cancel_kb())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")