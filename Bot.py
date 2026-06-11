import os
import json
import math
import asyncio
import logging
from datetime import datetime

import asyncpg
from telebot.async_telebot import AsyncTeleBot
from telebot import types

# === Строгие настройки из env (Fail-Fast) ===
TOKEN = os.environ["TAXI_BOT_TOKEN"]
PG_DSN = os.environ["PG_DSN"]
MINI_APP_BASE_URL = os.environ["MINI_APP_BASE_URL"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = AsyncTeleBot(TOKEN)
_pg_pool = None

# Тарифная сетка
BASE_PRICE = 2.0
PRICE_PER_KM = 0.50

# === База данных (Raw SQL via asyncpg) ===
async def init_pg():
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=15)
    async with _pg_pool.acquire() as conn:
        # Таблица пользователей (и пассажиры, и водители)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
                telegram_id BIGINT PRIMARY KEY,
                role TEXT,
                name TEXT,
                phone TEXT,
                car_model TEXT,
                car_number TEXT,
                car_color TEXT,
                is_online BOOL DEFAULT FALSE,
                is_busy BOOL DEFAULT FALSE,
                balance REAL DEFAULT 0.0,
                lat REAL,
                lon REAL,
                registered_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Таблица заказов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders(
                id SERIAL PRIMARY KEY,
                passenger_id BIGINT NOT NULL,
                driver_id BIGINT,
                from_address TEXT,
                to_address TEXT,
                from_lat REAL,
                from_lon REAL,
                to_lat REAL,
                to_lon REAL,
                distance REAL,
                price REAL,
                status TEXT DEFAULT 'searching' CHECK(status IN ('searching','accepted','arrived','trip','completed','cancelled')),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        log.info("✅ База данных PostgreSQL инициализирована")

# === Утилиты ===
def calc_dist(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

async def safe_send(user_id, text, reply_markup=None, parse_mode="HTML"):
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        log.error(f"Ошибка отправки пользователю {user_id}: {e}")

# === СТАРТ / ВЫБОР РОЛИ ===
@bot.message_handler(commands=['start'])
async def cmd_start(message):
    uid = message.from_user.id
    async with _pg_pool.acquire() as conn:
        await conn.execute("INSERT INTO users(telegram_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
        
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🚕 Пассажир", callback_data="set_role_passenger"),
        types.InlineKeyboardButton("🚘 Водитель", callback_data="set_role_driver")
    )
    
    await bot.send_message(
        message.chat.id, 
        "👋 Добро пожаловать в <b>UserTaxi</b>!\nВыберите вашу роль:", 
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_role_"))
async def handle_role_selection(call):
    uid = call.from_user.id
    role = call.data.replace("set_role_", "")
    
    async with _pg_pool.acquire() as conn:
        await conn.execute("UPDATE users SET role=$1 WHERE telegram_id=$2", role, uid)
        
    await bot.answer_callback_query(call.id, "✅ Роль сохранена!")
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if role == "passenger":
        markup.add(types.KeyboardButton("🗺 Заказать такси", web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")))
        await bot.send_message(uid, "📱 Меню пассажира. Нажмите кнопку ниже для вызова машины:", reply_markup=markup)
    else:
        markup.add(types.KeyboardButton("🧭 Карта водителя", web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/driver_map.html")))
        markup.add("🟢 На линии", "🔴 Оффлайн")
        await bot.send_message(uid, "🚖 Меню водителя. Выберите статус для получения заказов:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text in ["🟢 На линии", "🔴 Оффлайн"])
async def handle_driver_status(message):
    uid = message.from_user.id
    is_online = message.text == "🟢 На линии"
    
    async with _pg_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_online=$1 WHERE telegram_id=$2 AND role='driver'", is_online, uid)
        
    status_text = "🟢 Вы <b>НА ЛИНИИ</b> и получаете заказы." if is_online else "🔴 Вы ушли в оффлайн."
    await bot.send_message(message.chat.id, status_text, parse_mode="HTML")

# === WEB APP DATA (Данные с Mini Apps) ===
@bot.message_handler(content_types=['web_app_data'])
async def handle_web_app_data(message):
    uid = message.from_user.id
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        
        if action == "select_points":
            # Пассажир создал заказ
            from_lat, from_lon = float(data.get("from_lat")), float(data.get("from_lon"))
            price = float(data.get("price"))
            
            async with _pg_pool.acquire() as conn:
                order_id = await conn.fetchval("""
                    INSERT INTO orders(passenger_id, from_address, to_address, from_lat, from_lon, to_lat, to_lon, distance, price)
                    VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
                """, uid, data.get("from_address"), data.get("to_address"), from_lat, from_lon, 
                   float(data.get("to_lat")), float(data.get("to_lon")), float(data.get("distance")), price)
                
                await bot.send_message(uid, f"🔍 Ищем водителя для заказа <b>#{order_id}</b>...\n💰 Стоимость: <b>{price} GEL</b>", parse_mode="HTML")
                
                # Поиск свободных водителей
                drivers = await conn.fetch("SELECT telegram_id, lat, lon FROM users WHERE role='driver' AND is_online=TRUE AND is_busy=FALSE")
                
            # Рассылка водителям поблизости
            for d in drivers:
                if d['lat'] and d['lon']:
                    dist = calc_dist(from_lat, from_lon, d['lat'], d['lon'])
                    if dist <= 7.0: # Радиус 7 км
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept_{order_id}"))
                        await safe_send(
                            d['telegram_id'],
                            f"🔔 <b>НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n📍 Откуда: {data.get('from_address')}\n🏁 Куда: {data.get('to_address')}\n📏 До вас: {round(dist, 1)} км\n💰 Стоимость: <b>{price} GEL</b>",
                            reply_markup=markup
                        )
                        
        elif action == "driver_location":
            # Водитель обновил геопозицию
            lat, lon = float(data.get("lat")), float(data.get("lng"))
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE users SET lat=$1, lon=$2 WHERE telegram_id=$3", lat, lon, uid)
                
    except Exception as e:
        log.error(f"Ошибка обработки WebAppData: {e}", exc_info=True)

# === ЛОГИКА ЗАКАЗОВ (Инлайн-кнопки) ===
@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
async def handle_accept_order(call):
    order_id = int(call.data.replace("accept_", ""))
    driver_id = call.from_user.id
    
    async with _pg_pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1 FOR UPDATE", order_id)
            driver = await conn.fetchrow("SELECT is_busy FROM users WHERE telegram_id=$1 FOR UPDATE", driver_id)
            
            if not order or order['status'] != "searching":
                return await bot.answer_callback_query(call.id, "❌ Заказ уже взят или отменен.")
            if driver['is_busy']:
                return await bot.answer_callback_query(call.id, "❌ У вас уже есть активный заказ.")
                
            # Забираем заказ
            await conn.execute("UPDATE orders SET status='accepted', driver_id=$1, updated_at=NOW() WHERE id=$2", driver_id, order_id)
            await conn.execute("UPDATE users SET is_busy=TRUE WHERE telegram_id=$1", driver_id)
            
    await bot.answer_callback_query(call.id, "✅ Заказ принят!")
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏁 Завершить поездку", callback_data=f"complete_{order_id}"))
    await bot.send_message(driver_id, f"🚖 <b>Заказ #{order_id}</b> принят.\n\n📍 Маршрут: {order['from_address']} → {order['to_address']}", reply_markup=markup, parse_mode="HTML")
    
    await safe_send(order['passenger_id'], f"✅ Водитель найден! Ваш заказ <b>#{order_id}</b> выполняется.", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("complete_"))
async def handle_complete_order(call):
    order_id = int(call.data.replace("complete_", ""))
    driver_id = call.from_user.id
    
    async with _pg_pool.acquire() as conn:
        order = await conn.fetchrow("SELECT status, price, passenger_id FROM orders WHERE id=$1 AND driver_id=$2", order_id, driver_id)
        if order and order['status'] == "accepted":
            await conn.execute("UPDATE orders SET status='completed', updated_at=NOW() WHERE id=$1", order_id)
            await conn.execute("UPDATE users SET is_busy=FALSE, balance=balance+$1 WHERE telegram_id=$2", order['price'], driver_id)
            
            await bot.answer_callback_query(call.id, "✅ Поездка завершена!")
            await bot.delete_message(call.message.chat.id, call.message.message_id)
            
            await bot.send_message(driver_id, f"💵 Заказ <b>#{order_id}</b> закрыт.\nНа баланс зачислено: <b>{order['price']} GEL</b>", parse_mode="HTML")
            await safe_send(order['passenger_id'], f"✨ Спасибо за поездку! Заказ <b>#{order_id}</b> успешно завершен.", parse_mode="HTML")

# === ЗАПУСК ===
async def main():
    await init_pg()
    log.info(f"📡 Запуск AsyncTeleBot... MiniApp URL: {MINI_APP_BASE_URL}")
    await bot.infinity_polling(timeout=20, long_polling_timeout=10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Бот остановлен.")
