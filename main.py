import os
import asyncio
import logging
import aiosqlite
import secrets
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.deep_linking import create_start_link

# --- 1. НАСТРОЙКИ ---
def check_env():
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("BOT_TOKEN=ВАШ_ТОКЕН_ЗДЕСЬ\n")
        print(f"⚠️ Файл {env_path} создан.")
        exit()

check_env()
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

class ListStates(StatesGroup):
    waiting_for_product = State()

# --- 2. БАЗА ДАННЫХ ---
DB_NAME = "shopping_list.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Список покупок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                product_name TEXT
            )
        """)
        # История с счетчиком (добавлена колонка count)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                user_id INTEGER,
                product_name TEXT,
                count INTEGER DEFAULT 1,
                UNIQUE(user_id, product_name)
            )
        """)
        # Таблица токенов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS share_tokens (
                user_id INTEGER PRIMARY KEY,
                token TEXT UNIQUE
            )
        """)
        
        # Миграция: если колонка count не существует (для старых баз данных)
        try:
            await db.execute("ALTER TABLE history ADD COLUMN count INTEGER DEFAULT 1")
        except:
            pass # Колонка уже есть
            
        await db.commit()

# --- 3. ФУНКЦИИ БЕЗОПАСНОСТИ ---

async def get_or_create_token(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT token FROM share_tokens WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            new_token = secrets.token_urlsafe(12)
            await db.execute("INSERT INTO share_tokens (user_id, token) VALUES (?, ?)", (user_id, new_token))
            await db.commit()
            return new_token

async def get_user_by_token(token: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM share_tokens WHERE token = ?", (token,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

# --- 4. КЛАВИАТУРЫ ---

def get_main_inline_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📋 Мой список", callback_data="view_list"))
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data="add_item"))
    builder.row(InlineKeyboardButton(text="🔗 Поделиться", callback_data="share_link"))
    builder.row(InlineKeyboardButton(text="🗑 Очистить мой список", callback_data="clear_list"))
    return builder.as_markup()

async def get_products_inline_kb(owner_id, viewer_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, product_name FROM items WHERE user_id = ? ORDER BY id ASC", (owner_id,)) as cursor:
            rows = await cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    for index, (item_id, name) in enumerate(rows, start=1):
        builder.row(InlineKeyboardButton(text=f"{index}. {name} ❌", callback_data=f"del_{item_id}_{owner_id}"))
    
    if owner_id == viewer_id:
        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="main_menu"))
    return builder.as_markup()

async def get_history_suggestions_kb(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        # Сортируем по убыванию count, исключая то, что уже в списке
        async with db.execute("""
            SELECT product_name FROM history 
            WHERE user_id = ? 
            AND product_name NOT IN (SELECT product_name FROM items WHERE user_id = ?)
            ORDER BY count DESC 
            LIMIT 10
        """, (user_id, user_id)) as cursor:
            rows = await cursor.fetchall()
    
    builder = InlineKeyboardBuilder()
    for (name,) in rows:
        builder.row(InlineKeyboardButton(text=f"💡 {name}", callback_data=f"hist_add_{name}"))
    
    # Если список пуст (все популярные товары уже в корзине), можно добавить уведомление или просто кнопку Назад
    builder.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="main_menu"))
    return builder.as_markup()

# --- 5. ХЕНДЛЕРЫ ---

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if command.args and command.args.startswith("share_"):
        token = command.args.replace("share_", "")
        owner_id = await get_user_by_token(token)
        if owner_id:
            try:
                owner_chat = await bot.get_chat(owner_id)
                owner_info = f"{owner_chat.first_name} {owner_chat.last_name or ''} (@{owner_chat.username or 'no_user'})"
            except:
                owner_info = "Владелец списка"
            kb = await get_products_inline_kb(owner_id, message.from_user.id)
            await message.answer(f"👤 Список пользователя:\n<b>{owner_info}</b>\n\nНажмите на продукт, чтобы удалить его:", 
                                 reply_markup=kb, parse_mode="HTML")
            return
        else:
            await message.answer("⚠️ Ссылка недействительна или устарела.")
    await message.answer(f"Привет, {message.from_user.first_name}! Твой список покупок:", reply_markup=get_main_inline_kb())

@dp.callback_query(F.data == "share_link")
async def share_link(callback: types.CallbackQuery):
    token = await get_or_create_token(callback.from_user.id)
    link = await create_start_link(bot, f"share_{token}", encode=False)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔄 Обновить ссылку", callback_data="refresh_token"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu"))
    await callback.message.edit_text(
        f"🔗 Твоя секретная ссылка:\n\n<code>{link}</code>\n\n",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "refresh_token")
async def refresh_token(callback: types.CallbackQuery):
    new_token = secrets.token_urlsafe(12)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE share_tokens SET token = ? WHERE user_id = ?", (new_token, callback.from_user.id))
        await db.commit()
    await callback.answer("Ссылка обновлена.")
    await share_link(callback)

@dp.callback_query(F.data == "main_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛒 Главное меню:", reply_markup=get_main_inline_kb())

@dp.callback_query(F.data == "view_list")
async def view_list(callback: types.CallbackQuery):
    kb = await get_products_inline_kb(callback.from_user.id, callback.from_user.id)
    await callback.message.edit_text("Твой список:", reply_markup=kb)

@dp.callback_query(F.data.startswith("del_"))
async def delete_item(callback: types.CallbackQuery):
    _, item_id, owner_id = callback.data.split("_")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        await db.commit()
    kb = await get_products_inline_kb(int(owner_id), callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Удалено")

@dp.callback_query(F.data == "clear_list")
async def clear_list(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM items WHERE user_id = ?", (callback.from_user.id,))
        await db.commit()
    await callback.message.edit_text("Список очищен.", reply_markup=get_main_inline_kb())

@dp.callback_query(F.data == "add_item")
@dp.callback_query(F.data == "add_more_yes")
async def start_add(callback: types.CallbackQuery, state: FSMContext):
    kb = await get_history_suggestions_kb(callback.from_user.id)
    await callback.message.edit_text("✍️ Что добавить?", reply_markup=kb)
    await state.set_state(ListStates.waiting_for_product)

@dp.callback_query(F.data.startswith("hist_add_"))
async def add_from_history(callback: types.CallbackQuery):
    product = callback.data.replace("hist_add_", "")
    await save_to_db(callback.from_user.id, product)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Да", callback_data="add_more_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="main_menu"))
    await callback.message.answer(f"Добавлено: {product}. Еще?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.message(ListStates.waiting_for_product)
async def process_text(message: types.Message):
    # Очищаем текст от лишних пробелов и приводим к нижнему регистру для точности счета
    product = message.text.strip().capitalize()
    await save_to_db(message.from_user.id, product)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Да", callback_data="add_more_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="main_menu"))
    await message.answer(f"Добавлено: {product}. Еще?", reply_markup=builder.as_markup())

async def save_to_db(uid, prod):
    async with aiosqlite.connect(DB_NAME) as db:
        # Добавляем в текущий список
        await db.execute("INSERT INTO items (user_id, product_name) VALUES (?, ?)", (uid, prod))
        
        # Обновляем историю: если товар уже есть, увеличиваем count на 1
        await db.execute("""
            INSERT INTO history (user_id, product_name, count) 
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, product_name) 
            DO UPDATE SET count = count + 1
        """, (uid, prod))
        
        await db.commit()

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())