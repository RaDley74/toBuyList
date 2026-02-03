import os
import asyncio
import logging
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.deep_linking import create_start_link

# --- 1. ПРОВЕРКА И СОЗДАНИЕ .ENV ---
def check_env():
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("BOT_TOKEN=ВАШ_ТОКЕН_ЗДЕСЬ\n")
            f.write("ALLOWED_USERS=12345678,87654321\n")
        print(f"⚠️ Файл {env_path} создан. Заполните данные и перезапустите.")
        exit()

check_env()
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
raw_users = os.getenv("ALLOWED_USERS", "").split(",")
ALLOWED_USERS = [int(uid.strip()) for uid in raw_users if uid.strip().isdigit()]

logging.basicConfig(level=logging.INFO)

# --- 2. СОСТОЯНИЯ FSM ---
class ListStates(StatesGroup):
    waiting_for_product = State()

# --- 3. БАЗА ДАННЫХ ---
DB_NAME = "shopping_list.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                product_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                user_id INTEGER,
                product_name TEXT,
                UNIQUE(user_id, product_name)
            )
        """)
        await db.commit()

# --- 4. КЛАВИАТУРЫ ---

def get_main_inline_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 Мой список", callback_data="view_list"))
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data="add_item"))
    builder.row(InlineKeyboardButton(text="🔗 Поделиться списком", callback_data="share_link"))
    builder.row(InlineKeyboardButton(text="🗑 Очистить мой список", callback_data="clear_list"))
    return builder.as_markup()

async def get_products_inline_kb(owner_id, viewer_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, product_name FROM items WHERE user_id = ? ORDER BY id ASC", 
            (owner_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    for index, (item_id, name) in enumerate(rows, start=1):
        builder.row(InlineKeyboardButton(
            text=f"{index}. {name} ❌", 
            callback_data=f"del_{item_id}_{owner_id}"
        ))
    
    if owner_id == viewer_id:
        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="main_menu"))
    return builder.as_markup()

async def get_history_suggestions_kb(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT product_name FROM history WHERE user_id = ? ORDER BY product_name ASC LIMIT 10", 
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    for (name,) in rows:
        builder.row(InlineKeyboardButton(text=f"💡 {name}", callback_data=f"hist_add_{name}"))
    builder.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="main_menu"))
    return builder.as_markup()

def get_confirm_add_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data="add_more_yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="main_menu")
    )
    return builder.as_markup()

# --- 5. ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def access_middleware(handler, event, data):
    if event.from_user.id not in ALLOWED_USERS:
        return
    return await handler(event, data)

# --- 6. ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    
    if command.args and command.args.startswith("share_"):
        try:
            owner_id = int(command.args.split("_")[1])
            
            # Пытаемся получить информацию о владельце списка
            try:
                owner_chat = await bot.get_chat(owner_id)
                first_name = owner_chat.first_name or ""
                last_name = owner_chat.last_name or ""
                username = f"(@{owner_chat.username})" if owner_chat.username else ""
                owner_info = f"{first_name} {last_name} {username}".strip()
            except Exception:
                owner_info = f"ID: {owner_id}"

            kb = await get_products_inline_kb(owner_id, message.from_user.id)
            
            await message.answer(
                f"👤 Вы открыли список пользователя:\n<b>{owner_info}</b>\n(<code>{owner_id}</code>)\n\n"
                f"Нажмите на продукт, чтобы отметить его как купленный (удалить).", 
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        except (IndexError, ValueError):
            await message.answer("Некорректная ссылка.")

    await message.answer(f"Привет, {message.from_user.first_name}! Твой список:", 
                         reply_markup=get_main_inline_kb())

@dp.callback_query(F.data == "main_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛒 Главное меню:", reply_markup=get_main_inline_kb())

@dp.callback_query(F.data == "view_list")
async def view_list(callback: types.CallbackQuery):
    kb = await get_products_inline_kb(callback.from_user.id, callback.from_user.id)
    text = "Твой список:" if len(kb.inline_keyboard) > 1 else "Твой список пуст."
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "share_link")
async def share_link(callback: types.CallbackQuery):
    link = await create_start_link(bot, f"share_{callback.from_user.id}", encode=False)
    await callback.message.answer(f"Отправь эту ссылку, чтобы поделиться списком:\n\n<code>{link}</code>", 
                                  parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("del_"))
async def delete_item(callback: types.CallbackQuery):
    data = callback.data.split("_")
    item_id = int(data[1])
    owner_id = int(data[2])
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        await db.commit()
    
    kb = await get_products_inline_kb(owner_id, callback.from_user.id)
    
    # Если это чужой список (нет кнопки "Назад"), проверяем наличие кнопок вообще
    # Если свой (есть кнопка "Назад"), проверяем количество кнопок > 1
    has_items = len(kb.inline_keyboard) > (1 if owner_id == callback.from_user.id else 0)
    text = "Список покупок:" if has_items else "Список пуст."
    
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Удалено")

@dp.callback_query(F.data == "clear_list")
async def clear_list(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM items WHERE user_id = ?", (callback.from_user.id,))
        await db.commit()
    await callback.message.edit_text("Твой список очищен.", reply_markup=get_main_inline_kb())

# --- ДОБАВЛЕНИЕ ---

@dp.callback_query(F.data == "add_item")
@dp.callback_query(F.data == "add_more_yes")
async def start_add(callback: types.CallbackQuery, state: FSMContext):
    kb = await get_history_suggestions_kb(callback.from_user.id)
    await callback.message.edit_text("✍️ Напиши продукт или выбери из истории:", reply_markup=kb)
    await state.set_state(ListStates.waiting_for_product)

@dp.callback_query(F.data.startswith("hist_add_"))
async def add_from_history(callback: types.CallbackQuery, state: FSMContext):
    product = callback.data.replace("hist_add_", "")
    await save_to_db(callback.from_user.id, product)
    await callback.message.answer(f"✅ Добавлено: <b>{product}</b>\nЕщё?", 
                                 reply_markup=get_confirm_add_kb(), parse_mode="HTML")

@dp.message(ListStates.waiting_for_product)
async def process_add_text(message: types.Message, state: FSMContext):
    product = message.text.strip()
    if product:
        await save_to_db(message.from_user.id, product)
        await message.answer(f"✅ Добавлено: <b>{product}</b>\nЕщё?", 
                             reply_markup=get_confirm_add_kb(), parse_mode="HTML")

async def save_to_db(user_id: int, product_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO items (user_id, product_name) VALUES (?, ?)", (user_id, product_name))
        await db.execute("INSERT OR IGNORE INTO history (user_id, product_name) VALUES (?, ?)", (user_id, product_name))
        await db.commit()

async def main():
    await init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass