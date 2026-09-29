import os
import asyncio
import logging
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
DB_PATH = os.getenv("DB_PATH", "confessions.db")

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_banned INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                post_type TEXT,
                content TEXT,
                status TEXT DEFAULT 'pending',
                channel_msg_id INTEGER
            )
        """)
        await db.commit()

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            return bool(res[0]) if res else False

async def ban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, is_banned) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET is_banned=1",
            (user_id,)
        )
        await db.commit()

async def add_post(user_id: int, post_type: str, content: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO posts (user_id, post_type, content) VALUES (?, ?, ?)",
            (user_id, post_type, content)
        )
        await db.commit()
        return cursor.lastrowid

async def get_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, post_type, content, status FROM posts WHERE id = ?", (post_id,)) as cursor:
            return await cursor.fetchone()

async def update_post_status(post_id: int, status: str, msg_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET status = ?, channel_msg_id = ? WHERE id = ?",
            (status, msg_id, post_id)
        )
        await db.commit()

# --- СОСТОЯНИЯ ФОРМЫ ---
class PostForm(StatesGroup):
    waiting_for_take = State()
    waiting_for_search_who = State()
    waiting_for_search_about = State()
    waiting_for_search_username = State()

def get_main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Отправить тейк")],
            [KeyboardButton(text="🤝 Найти друга / подругу")],
            [KeyboardButton(text="📜 Правила")]
        ],
        resize_keyboard=True
    )

# --- ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ---
router = Router()

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_banned(message.from_user.id):
        await message.answer("Вы заблокированы в системе.")
        return
    await message.answer("ШКОЛЬНЫЙ АРХИВ\n\nВыбери нужный раздел ниже:", reply_markup=get_main_kb())

@router.message(F.text == "📜 Правила")
async def show_rules(message: types.Message):
    await message.answer(
        "ПРАВИЛА ПУБЛИКАЦИИ:\n\n"
        "1. Запрещена травля, доксинг и личные оскорбления.\n"
        "2. В тейках не указываются чужие контакты.\n"
        "3. Все посты проходят предварительную модерацию."
    )

# Обычный Тейк
@router.message(F.text == "📝 Отправить тейк")
async def start_take(message: types.Message, state: FSMContext):
    if await is_banned(message.from_user.id):
        return
    await state.set_state(PostForm.waiting_for_take)
    await message.answer("Напиши свой тейк (мысль, историю или наблюдение):")

@router.message(PostForm.waiting_for_take)
async def process_take(message: types.Message, state: FSMContext, bot: Bot):
    await state.clear()
    post_id = await add_post(message.from_user.id, "take", message.text)
    
    await message.answer(f"Принято! Твой тейк отправлен на модерацию (ID: #{post_id}).", reply_markup=get_main_kb())
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать", callback_data=f"app_{post_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"rej_{post_id}")
        ],
        [InlineKeyboardButton(text="🚫 Забанить автора", callback_data=f"ban_{post_id}")]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📥 НОВЫЙ ТЕЙК #{post_id}\nАвтор: @{message.from_user.username or 'без_юзернейма'}\n\n«{message.text}»",
                reply_markup=admin_kb
            )
        except Exception:
            pass

# Поиск друга/подруги
@router.message(F.text == "🤝 Найти друга / подругу")
async def start_search(message: types.Message, state: FSMContext):
    if await is_banned(message.from_user.id):
        return
    await state.set_state(PostForm.waiting_for_search_who)
    await message.answer("Кого ты ищешь? (например: Подругу, Друга, Компанию в кофейню)")

@router.message(PostForm.waiting_for_search_who)
async def process_search_who(message: types.Message, state: FSMContext):
    await state.update_data(search_who=message.text)
    await state.set_state(PostForm.waiting_for_search_about)
    await message.answer("Расскажи немного о себе и своих интересах:")

@router.message(PostForm.waiting_for_search_about)
async def process_search_about(message: types.Message, state: FSMContext):
    await state.update_data(search_about=message.text)
    await state.set_state(PostForm.waiting_for_search_username)
    
    user_handle = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    await message.answer(
        f"Укажи контакт для связи (по умолчанию: {user_handle}).\n"
        "Отправь свой @username или другой контакт:"
    )

@router.message(PostForm.waiting_for_search_username)
async def process_search_username(message: types.Message, state: FSMContext, bot: Bot):
    user_data = await state.get_data()
    await state.clear()
    
    contact = message.text.strip()
    content_str = f"Кого ищет: {user_data['search_who']}\nО себе: {user_data['search_about']}\nСвязь: {contact}"
    
    post_id = await add_post(message.from_user.id, "search", content_str)
    
    await message.answer(f"Анкета отправлена на модерацию (ID: #{post_id}).", reply_markup=get_main_kb())
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать", callback_data=f"app_{post_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"rej_{post_id}")
        ],
        [InlineKeyboardButton(text="🚫 Забанить автора", callback_data=f"ban_{post_id}")]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📥 НОВЫЙ ПОИСК #{post_id}\n\n{content_str}",
                reply_markup=admin_kb
            )
        except Exception:
            pass

# --- ХЭНДЛЕРЫ МОДЕРАЦИИ ---
@router.callback_query(F.data.startswith("app_"))
async def approve_post(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    post_id = int(callback.data.split("_")[1])
    post_data = await get_post(post_id)
    
    if not post_data or post_data[3] != "pending":
        await callback.answer("Запись уже обработана.")
        return
        
    user_id, post_type, content, _ = post_data
    
    if post_type == "take":
        formatted_text = (
            f"ШКОЛЬНЫЙ АРХИВ\n\n"
            f"#{post_id} · Тейк\n\n"
            f"«{content}»\n\n"
            f"────────────────\n"
            f"— анонимно"
        )
    else:
        formatted_text = (
            f"ШКОЛЬНЫЙ АРХИВ\n\n"
            f"#{post_id} · Поиск\n\n"
            f"{content}"
        )
    
    sent_msg = await bot.send_message(CHANNEL_ID, formatted_text)
    await update_post_status(post_id, "approved", sent_msg.message_id)
    
    await callback.message.edit_text(f"✅ Пост #{post_id} опубликован.")
    try:
        await bot.send_message(user_id, f"Твой пост #{post_id} опубликован в канале!")
    except Exception:
        pass

@router.callback_query(F.data.startswith("rej_"))
async def reject_post(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        return
        
    post_id = int(callback.data.split("_")[1])
    post_data = await get_post(post_id)
    
    if post_data:
        user_id = post_data[0]
        await update_post_status(post_id, "rejected")
        await callback.message.edit_text(f"❌ Пост #{post_id} отклонен.")
        try:
            await bot.send_message(user_id, f"Твой пост #{post_id} не прошёл модерацию.")
        except Exception:
            pass

@router.callback_query(F.data.startswith("ban_"))
async def ban_author(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        return
        
    post_id = int(callback.data.split("_")[1])
    post_data = await get_post(post_id)
    
    if post_data:
        user_id = post_data[0]
        await ban_user(user_id)
        await update_post_status(post_id, "banned")
        await callback.message.edit_text(f"🚫 Автор поста #{post_id} заблокирован.")
        try:
            await bot.send_message(user_id, "Вы были заблокированы за нарушение правил.")
        except Exception:
            pass

# --- ЗАПУСК ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    print("Бот успешно запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())