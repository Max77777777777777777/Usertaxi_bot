import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
import asyncpg

# Включаем логирование, чтобы видеть ошибки в панели
logging.basicConfig(level=logging.INFO)

# Читаем переменные из панели Bothost
TOKEN = os.getenv("TAXI_BOT_TOKEN")
PG_DSN = os.getenv("PG_DSN")
MINI_APP_URL = os.getenv("MINI_APP_BASE_URL")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher()

# Инициализация базы данных и таблиц
async def init_db():
    conn = await asyncpg.connect(PG_DSN)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users(
            telegram_id BIGINT PRIMARY KEY,
            role TEXT,
            is_online BOOLEAN DEFAULT FALSE,
            is_busy BOOLEAN DEFAULT FALSE,
            balance REAL DEFAULT 0.0,
            lat REAL,
            lon REAL
        );
        CREATE TABLE IF NOT EXISTS orders(
            id SERIAL PRIMARY KEY,
            passenger_id BIGINT NOT NULL,
            driver_id BIGINT,
            from_address TEXT,
            to_address TEXT,
            price REAL,
            status TEXT DEFAULT 'searching'
        );
    ''')
    await conn.close()
    return await asyncpg.create_pool(PG_DSN, min_size=1, max_size=10)

# --- МИНИМАЛИСТИЧНЫЕ КЛАВИАТУРЫ ---

def get_role_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚕", callback_data="role_passenger"),
         InlineKeyboardButton(text="🚘", callback_data="role_driver")]
    ])

def get_passenger_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🗺 🚕", web_app=WebAppInfo(url=f"{MINI_APP_URL}/index.html"))]
    ], resize_keyboard=True)

def get_driver_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🧭 🚘", web_app=WebAppInfo(url=f"{MINI_APP_URL}/driver_map.html"))],
        [KeyboardButton(text="🟢"), KeyboardButton(text="🔴")]
    ], resize_keyboard=True)

# --- ХЭНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users(telegram_id) VALUES($1) ON CONFLICT DO NOTHING", message.from_user.id)
    
    await message.answer("👋 <b>UserTaxi</b>\n🚕 / 🚘 ?", reply_markup=get_role_kb())

@dp.callback_query(F.data.startswith("role_"))
async def set_role(callback: types.CallbackQuery, db_pool):
    role = callback.data.split("_")[1]
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET role=$1 WHERE telegram_id=$2", role, callback.from_user.id)
    
    await callback.message.delete()
    if role == "passenger":
        await callback.message.answer("🚕", reply_markup=get_passenger_kb())
    else:
        await callback.message.answer("🚘", reply_markup=get_driver_kb())

@dp.message(F.text.in_(["🟢", "🔴"]))
async def change_status(message: types.Message, db_pool):
    is_online = message.text == "🟢"
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_online=$1 WHERE telegram_id=$2", is_online, message.from_user.id)
    await message.answer(message.text)

# Middleware для удобной передачи пула БД в каждый хэндлер
class DbMiddleware:
    def __init__(self, pool):
        self.pool = pool

    async def __call__(self, handler, event, data):
        data['db_pool'] = self.pool
        return await handler(event, data)

# --- ЗАПУСК ---
async def main():
    if not all([TOKEN, PG_DSN, MINI_APP_URL]):
        logging.error("❌ Ключи окружения не найдены! Проверь настройки панели Bothost.")
        return
    
    pool = await init_db()
    dp.update.middleware(DbMiddleware(pool))
    
    logging.info("🚀 UserTaxi запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
