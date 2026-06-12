import os
import json
import threading
import time
from datetime import datetime
import requests
import math

import telebot
from telebot import types

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
from sqlalchemy import create_engine, Column, Integer, JSON
from sqlalchemy.orm import declarative_base, sessionmaker

# ============================================================
# КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================
TOKEN = os.getenv("TAXI_BOT_TOKEN", "")
YANDEX_SUGGEST_KEY = os.getenv("YANDEX_SUGGEST_KEY", "")
YANDEX_GEOCODER_KEY = os.getenv("YANDEX_GEOCODER_KEY", "")
MINI_APP_BASE_URL = os.getenv("MINI_APP_BASE_URL", "https://your-domain.com")
DB_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/usertaxi")

# Для SQLAlchemy строка подключения должна быть postgresql:// (на случай если сервер передает postgres://)
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

if not TOKEN:
    print("⚠️ TAXI_BOT_TOKEN не задан в переменных окружения")

# ============================================================
# БАЗА ДАННЫХ (PostgreSQL)
# ============================================================
engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Сохраняем состояние бота в JSONB колонку для 100% совместимости с твоей логикой
class BotState(Base):
    __tablename__ = "bot_state"
    id = Column(Integer, primary_key=True, index=True)
    state_data = Column(JSON, nullable=False)

Base.metadata.create_all(bind=engine)

# ============================================================
# FASTAPI ВЕБ-СЕРВЕР (Раздает Mini Apps)
# ============================================================
app = FastAPI(title="UserTaxi")

@app.get("/index.html", response_class=HTMLResponse)
def get_index():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "❌ Файл index.html не найден. Положите его рядом с main.py"

@app.get("/driver_map.html", response_class=HTMLResponse)
def get_driver_map():
    try:
        with open("driver_map.html", "r", encoding="utf-8") as f:
            content = f.read()
            # Динамически меняем захардкоженный токен на серверный из os.getenv
            content = content.replace(
                'const BOT_TOKEN = "8787169638:AAF8Zy4ZOStZIbQ-opE-xpbnz8NdOvIfsz8";',
                f'const BOT_TOKEN = "{TOKEN}";'
            )
            return content
    except Exception:
        return "❌ Файл driver_map.html не найден. Положите его рядом с main.py"

# ============================================================
# ТАРИФЫ
# ============================================================
BASE_PRICE = 2
PRICE_PER_KM = 0.50
WAITING_PRICE_PER_MIN = 0.10
COMMISSION_RATE = 0.10

GEO_BOUNDS = {
    "lat_min": 41.0,
    "lat_max": 43.6,
    "lon_min": 40.0,
    "lon_max": 46.8
}

# ============================================================
# ГЛОБАЛЬНЫЕ ДАННЫЕ И БЛОКИРОВКИ
# ============================================================
user_role = {}
driver_locations = {}
driver_online = {}
driver_busy = {}
driver_info = {}
passenger_data = {}
orders = {}
order_counter = 1
driver_rating = {}
passenger_rating = {}
driver_balance = {}
driver_status = {}
active_order_for_driver = {}
chat_sessions = {}
suggest_sessions = {}
temp_suggestions = {}

order_lock = threading.Lock()
order_counter_lock = threading.Lock()
save_lock = threading.Lock()
driver_dispatch_lock = threading.Lock()

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ============================================================
# СОХРАНЕНИЕ И ЗАГРУЗКА ИЗ БАЗЫ ДАННЫХ
# ============================================================
def save_state():
    state = {
        "user_role": user_role,
        "driver_locations": {str(k): list(v) for k, v in driver_locations.items()},
        "driver_online": driver_online,
        "driver_busy": driver_busy,
        "driver_info": {str(k): v for k, v in driver_info.items()},
        "passenger_data": {
            str(k): {
                "from": list(v["from"]) if v.get("from") else None,
                "to": list(v["to"]) if v.get("to") else None,
                "from_lat": v.get("from_lat"),
                "from_lon": v.get("from_lon"),
                "to_lat": v.get("to_lat"),
                "to_lon": v.get("to_lon"),
                "from_address": v.get("from_address"),
                "to_address": v.get("to_address")
            } for k, v in passenger_data.items()
        },
        "orders": orders,
        "order_counter": order_counter,
        "driver_rating": driver_rating,
        "passenger_rating": passenger_rating,
        "driver_balance": driver_balance,
        "active_order_for_driver": {str(k): v for k, v in active_order_for_driver.items()},
        "driver_status": {str(k): v for k, v in driver_status.items()}
    }
    with save_lock:
        db = SessionLocal()
        try:
            db_state = db.query(BotState).filter(BotState.id == 1).first()
            if not db_state:
                db_state = BotState(id=1, state_data=state)
                db.add(db_state)
            else:
                db_state.state_data = state
            db.commit()
            print("✅ Состояние сохранено в PostgreSQL")
        except Exception as e:
            db.rollback()
            print(f"❌ Ошибка сохранения в PostgreSQL: {e}")
        finally:
            db.close()

def load_state():
    global user_role, driver_locations, driver_online, driver_busy
    global driver_info, passenger_data, orders, order_counter
    global driver_rating, passenger_rating, driver_balance
    global active_order_for_driver, driver_status

    state = None
    db = SessionLocal()
    try:
        db_state = db.query(BotState).filter(BotState.id == 1).first()
        if db_state and db_state.state_data:
            state = db_state.state_data
            print("✅ Данные загружены из PostgreSQL")
    except Exception as e:
        print(f"❌ Ошибка загрузки из PostgreSQL: {e}")
    finally:
        db.close()

    if state is None:
        print("ℹ️ Данных в БД нет, начинаем с чистого листа")
        suggest_sessions.clear()
        temp_suggestions.clear()
        chat_sessions.clear()
        return

    user_role.update(state.get("user_role", {}))
    driver_online.update(state.get("driver_online", {}))
    driver_busy.update(state.get("driver_busy", {}))
    driver_info.update({int(k): v for k, v in state.get("driver_info", {}).items()})
    driver_rating.update(state.get("driver_rating", {}))
    passenger_rating.update(state.get("passenger_rating", {}))
    orders.update({int(k): v for k, v in state.get("orders", {}).items()})
    global order_counter
    order_counter = state.get("order_counter", 1)
    driver_balance.update(state.get("driver_balance", {}))
    driver_status.update({int(k): v for k, v in state.get("driver_status", {}).items()})
    active_order_for_driver.update({int(k): v for k, v in state.get("active_order_for_driver", {}).items()})

    driver_locs = state.get("driver_locations", {})
    driver_locations.update({int(k): tuple(v) for k, v in driver_locs.items()})

    pass_data = state.get("passenger_data", {})
    for k, v in pass_data.items():
        passenger_data[int(k)] = {
            "from": tuple(v["from"]) if v.get("from") else None,
            "to": tuple(v["to"]) if v.get("to") else None,
            "from_lat": v.get("from_lat"),
            "from_lon": v.get("from_lon"),
            "to_lat": v.get("to_lat"),
            "to_lon": v.get("to_lon"),
            "from_address": v.get("from_address"),
            "to_address": v.get("to_address")
        }

    suggest_sessions.clear()
    temp_suggestions.clear()
    chat_sessions.clear()

# ============================================================
# ОРИГИНАЛЬНАЯ ЛОГИКА БОТА
# ============================================================
def get_next_order_id():
    global order_counter
    with order_counter_lock:
        oid = order_counter
        order_counter += 1
    return oid

def is_valid_location(lat, lon):
    return (GEO_BOUNDS["lat_min"] <= lat <= GEO_BOUNDS["lat_max"] and
            GEO_BOUNDS["lon_min"] <= lon <= GEO_BOUNDS["lon_max"])

def get_address_suggestions_sync(text):
    if not YANDEX_SUGGEST_KEY:
        return []
    try:
        url = "https://suggest-maps.yandex.ru/v1/suggest"
        params = {"apikey": YANDEX_SUGGEST_KEY, "text": text, "lang": "ru", "results": 5, "print_address": 1}
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        suggestions = []
        if "results" in data:
            for item in data["results"]:
                addr_data = item.get("address", {})
                display_name = addr_data.get("formatted_address", item.get("title", {}).get("text", ""))
                lat, lon = None, None
                if "geometry" in item and "coordinates" in item["geometry"]:
                    coords = item["geometry"]["coordinates"]
                    if len(coords) >= 2:
                        lat, lon = coords[1], coords[0]
                if display_name:
                    suggestions.append({"display": display_name, "lat": lat, "lon": lon})
        return suggestions[:5]
    except Exception as e:
        print(f"⚠️ Ошибка саджеста: {e}")
        return []

def geocode_address_sync(address):
    if not YANDEX_GEOCODER_KEY:
        return None, None, None
    try:
        url = "https://geocode-maps.yandex.ru/1.x/"
        params = {"apikey": YANDEX_GEOCODER_KEY, "geocode": address, "format": "json", "lang": "ru_RU", "results": 1}
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        found = data["response"]["GeoObjectCollection"]["featureMember"]
        if found:
            geo = found[0]["GeoObject"]
            pos = geo["Point"]["pos"]
            lon, lat = map(float, pos.split())
            display_name = geo["metaDataProperty"]["GeocoderMetaData"]["text"]
            return lat, lon, display_name
        return None, None, None
    except Exception as e:
        print(f"⚠️ Ошибка геокодера: {e}")
        return None, None, None

def get_address_suggestions_async(text, chat_id, callback):
    def _worker():
        result = get_address_suggestions_sync(text)
        callback(chat_id, result)
    threading.Thread(target=_worker, daemon=True).start()

def geocode_address_async(address, chat_id, callback):
    def _worker():
        lat, lon, display = geocode_address_sync(address)
        callback(chat_id, lat, lon, display)
    threading.Thread(target=_worker, daemon=True).start()

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def calculate_price(distance_km, waiting_minutes=0):
    return round(BASE_PRICE + (distance_km * PRICE_PER_KM) + (waiting_minutes * WAITING_PRICE_PER_MIN), 2)

def calculate_driver_earnings(price):
    commission = round(price * COMMISSION_RATE, 2)
    earnings = round(price - commission, 2)
    return earnings, commission

def format_location(lat, lon):
    return f"<a href='https://maps.google.com/?q={lat},{lon}'>📍 Открыть на карте</a>"

def expire_order(order_id):
    for _ in range(60):
        time.sleep(1)
        order = orders.get(order_id)
        if not order or order["status"] != "searching":
            return
    order = orders.get(order_id)
    if order and order["status"] == "searching":
        order["status"] = "expired"
        try:
            bot.send_message(order["passenger_id"], f"❌ Заказ #{order_id} отменён: нет свободных водителей. Попробуйте позже.")
        except Exception:
            pass
        save_state()

def dispatch_order(order_id):
    order = orders.get(order_id)
    if not order:
        return

    from_lat, from_lon = order["from"]

    with driver_dispatch_lock:
        candidates = []
        for driver_id, loc in driver_locations.items():
            if (driver_online.get(driver_id) and
                    not driver_busy.get(driver_id, False) and
                    driver_id in driver_info and
                    driver_info[driver_id]):
                dist = calculate_distance(from_lat, from_lon, loc[0], loc[1])
                candidates.append((driver_id, dist))

        candidates.sort(key=lambda x: x[1])
        nearest = candidates[:3]

        if not nearest:
            try:
                bot.send_message(order["passenger_id"], "❌ Нет доступных водителей поблизости. Попробуйте позже.")
            except Exception:
                pass
            return

        to_lat, to_lon = order["to"]
        total_distance = calculate_distance(from_lat, from_lon, to_lat, to_lon)
        price = calculate_price(total_distance)
        earnings, commission = calculate_driver_earnings(price)

        order["price"] = price
        order["driver_earnings"] = earnings
        order["commission"] = commission
        order["distance"] = round(total_distance, 2)
        order["notified_drivers"] = [d[0] for d in nearest]

        save_state()

        for driver_id, dist_to_passenger in nearest:
            send_order_to_driver(driver_id, order_id, dist_to_passenger)

def send_order_to_driver(driver_id, order_id, dist_to_passenger):
    order = orders.get(order_id)
    if not order:
        return

    from_text = order.get("from_address") or format_location(order['from'][0], order['from'][1])
    to_text = order.get("to_address") or format_location(order['to'][0], order['to'][1])

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept_{order_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{order_id}"))

    text = (
        f"🚕 <b>НОВЫЙ ЗАКАЗ #{order_id}</b>\n\n"
        f"📍 Откуда: {from_text}\n"
        f"🏁 Куда: {to_text}\n\n"
        f"📏 До пассажира: <b>{dist_to_passenger:.2f} км</b>\n"
        f"🛣 Маршрут: <b>{order['distance']} км</b>\n\n"
        f"💰 <b>Стоимость для пассажира:</b> {order['price']} GEL\n"
        f"💵 <b>Ваш заработок (минус {int(COMMISSION_RATE * 100)}%):</b> {order['driver_earnings']} GEL"
    )
    try:
        bot.send_message(driver_id, text, reply_markup=markup)
    except Exception as e:
        print(f"⚠️ Ошибка отправки водителю {driver_id}: {e}")

def notify_other_drivers(order_id, accepted_driver_id):
    order = orders.get(order_id)
    if not order:
        return
    notified = order.get("notified_drivers", [])
    for did in notified:
        if did != accepted_driver_id:
            try:
                bot.send_message(did, f"ℹ️ Заказ #{order_id} уже принят другим водителем.")
            except Exception:
                pass

def send_driver_info_to_passenger(passenger_id, driver_id, order_id):
    driver = driver_info.get(driver_id, {})
    order = orders.get(order_id, {})

    rating_list = driver_rating.get(driver_id, [])
    rating = f"{sum(rating_list) / len(rating_list):.1f}⭐" if rating_list else "Новый водитель"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💬 Чат", callback_data=f"chat_{order_id}"),
        types.InlineKeyboardButton("📞 Позвонить", callback_data=f"call_{order_id}")
    )
    markup.add(
        types.InlineKeyboardButton("📍 Где водитель?", callback_data=f"track_{order_id}"),
        types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_order_{order_id}")
    )

    text = (
        f"✅ <b>ВОДИТЕЛЬ НАЗНАЧЕН!</b>\n\n"
        f"🚗 <b>Информация о водителе:</b>\n"
        f"👤 Имя: {driver.get('name', 'Не указано')}\n"
        f"⭐ Рейтинг: {rating}\n"
        f"🚘 Автомобиль: {driver.get('car_color', '')} {driver.get('car_model', '')}\n"
        f"🔢 Номер: {driver.get('car_number', 'Не указан')}\n"
        f"📱 Телефон: {driver.get('phone', 'Не указан')}\n\n"
        f"📍 Водитель едет к вам!\n"
        f"💰 Стоимость поездки: <b>{order.get('price', 0)} GEL</b>\n\n"
        f"<i>Используйте кнопки ниже для связи с водителем</i>"
    )
    try:
        bot.send_message(passenger_id, text, reply_markup=markup)
    except Exception as e:
        print(f"⚠️ Ошибка отправки пассажиру: {e}")

def complete_order_internal(order_id, driver_id):
    order = orders.get(order_id)
    if not order:
        return

    order["status"] = "completed"
    order["completed_at"] = datetime.now().isoformat()
    driver_busy[driver_id] = False
    driver_status.pop(driver_id, None)
    if driver_id in active_order_for_driver:
        del active_order_for_driver[driver_id]

    earnings = order.get("driver_earnings", 0)
    driver_balance[driver_id] = round(driver_balance.get(driver_id, 0) + earnings, 2)

    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⭐ Оценить поездку", callback_data=f"rate_{order_id}"))
        bot.send_message(
            order["passenger_id"],
            f"✅ <b>Поездка завершена!</b>\n\n"
            f"💰 Стоимость: <b>{order.get('price', 0)} GEL</b>\n"
            f"Спасибо, что выбрали нас! 🚕",
            reply_markup=markup
        )
    except Exception as e:
        print(f"⚠️ Ошибка уведомления пассажира: {e}")

    try:
        bot.send_message(
            driver_id,
            f"✅ <b>Заказ #{order_id} завершён!</b>\n\n"
            f"💰 Заработано: <b>{earnings} GEL</b>\n"
            f"💵 Общий баланс: <b>{driver_balance[driver_id]} GEL</b>"
        )
    except Exception as e:
        print(f"⚠️ Ошибка уведомления водителя: {e}")

    save_state()

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id
    for d in [passenger_data, suggest_sessions, temp_suggestions, chat_sessions]:
        if user_id in d:
            del d[user_id]
    if user_id in user_role:
        del user_role[user_id]
    if user_id in driver_online:
        driver_online[user_id] = False
    if user_id in driver_busy:
        driver_busy[user_id] = False

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🚕 Заказать", "🚗 Водитель")

    bot.send_message(
        user_id,
        "🚕 <b>UserTaxi</b>\n\n"
        "Выберите режим:\n\n"
        "📋 <b>Тарифы:</b>\n"
        f"• Подача: {BASE_PRICE} GEL\n"
        f"• Км: {PRICE_PER_KM} GEL\n"
        f"• Ожидание: {WAITING_PRICE_PER_MIN} GEL/мин",
        reply_markup=markup
    )

@bot.message_handler(commands=['help'])
def help_command(message):
    text = (
        "🚕 <b>UserTaxi - Помощь</b>\n\n"
        f"📋 <b>Тарифы:</b>\n"
        f"• Подача: {BASE_PRICE} GEL\n"
        f"• Км: {PRICE_PER_KM} GEL\n"
        f"• Ожидание: {WAITING_PRICE_PER_MIN} GEL/мин\n\n"
        "<b>Команды для водителя:</b>\n"
        "/profile — заполнить профиль\n"
        "/status — статус поездки\n"
        "/balance — баланс\n\n"
        "<b>Команды для пассажира:</b>\n"
        "/status — статус поездки\n"
        "/cancel — отменить заказ"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text in ["🚕 Заказать", "🚗 Водитель"])
def choose_role(message):
    user_id = message.chat.id

    if message.text == "🚕 Заказать":
        user_role[user_id] = "passenger"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📍 Откуда", "🏁 Куда")
        markup.add(types.KeyboardButton(
            "🗺 Выбрать на карте",
            web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
        ))
        markup.add("🚀 Создать заказ", "❌ Отмена")
        markup.add("📊 Статус", "🔙 Меню")
        bot.send_message(message.chat.id, "✅ Режим: <b>Пассажир</b>\n\nМожете указать адрес вручную или выбрать на карте.", reply_markup=markup)

    elif message.text == "🚗 Водитель":
        user_role[user_id] = "driver"
        if user_id not in driver_balance:
            driver_balance[user_id] = 0
        driver_busy[user_id] = False

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🟢 На линии", "🔴 Оффлайн")
        markup.add(types.KeyboardButton(
            "🗺 Карта водителя",
            web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/driver_map.html")
        ))
        markup.add("📍 Локация", "📊 Статистика")
        markup.add("👤 Профиль", "💰 Баланс")
        markup.add("📌 Статус", "🔙 Меню")
        bot.send_message(message.chat.id, "✅ Режим: <b>Водитель</b>\n\nЗаполните профиль и нажмите «На линии» для приёма заказов.", reply_markup=markup)
    save_state()

@bot.message_handler(func=lambda m: m.text == "🔙 Меню")
def back_to_main(message):
    user_id = message.chat.id
    for d in [passenger_data, suggest_sessions, temp_suggestions, chat_sessions]:
        if user_id in d:
            del d[user_id]
    if user_id in user_role:
        del user_role[user_id]

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🚕 Заказать", "🚗 Водитель")
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "🔙 Назад")
def back_button(message):
    user_id = message.chat.id
    role = user_role.get(user_id)
    for d in [suggest_sessions, temp_suggestions]:
        if user_id in d:
            del d[user_id]

    if role == "driver":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🟢 На линии", "🔴 Оффлайн")
        markup.add(types.KeyboardButton(
            "🗺 Карта водителя",
            web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/driver_map.html")
        ))
        markup.add("📍 Локация", "📊 Статистика")
        markup.add("👤 Профиль", "💰 Баланс")
        markup.add("📌 Статус", "🔙 Меню")
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📍 Откуда", "🏁 Куда")
        markup.add(types.KeyboardButton(
            "🗺 Выбрать на карте",
            web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
        ))
        markup.add("🚀 Создать заказ", "❌ Отмена")
        markup.add("📊 Статус", "🔙 Меню")

    bot.send_message(message.chat.id, "◀️ Назад", reply_markup=markup)

@bot.message_handler(commands=['profile'])
def driver_profile_cmd(message):
    driver_profile(message)

@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def driver_profile_btn(message):
    driver_profile(message)

def driver_profile(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "driver":
        bot.send_message(user_id, "❌ Эта команда только для водителей")
        return
    msg = bot.send_message(
        user_id,
        "📝 <b>Заполните профиль водителя</b>\n\n"
        "Введите данные в формате:\n"
        "<code>Имя;Модель авто;Номер;Цвет;Телефон</code>\n\n"
        "<i>Например:</i>\n"
        "<code>Георгий;Toyota Camry;ABC-123;Белый;+995599123456</code>"
    )
    bot.register_next_step_handler(msg, process_profile)

def process_profile(message):
    user_id = message.chat.id
    try:
        parts = message.text.split(";")
        if len(parts) >= 5:
            driver_info[user_id] = {
                "name": parts[0].strip(),
                "car_model": parts[1].strip(),
                "car_number": parts[2].strip(),
                "car_color": parts[3].strip(),
                "phone": parts[4].strip()
            }
            bot.send_message(user_id, "✅ Профиль сохранён!")
            save_state()
        else:
            bot.send_message(user_id, "❌ Неверный формат. Используйте /profile чтобы попробовать снова.")
    except Exception as e:
        bot.send_message(user_id, "❌ Ошибка. Используйте /profile чтобы попробовать снова.")

@bot.message_handler(commands=['status'])
def status_cmd(message):
    show_status(message)

@bot.message_handler(func=lambda m: m.text in ["📊 Статус", "📌 Статус"])
def status_btn(message):
    show_status(message)

def show_status(message):
    user_id = message.chat.id

    if user_role.get(user_id) == "driver":
        order_id = active_order_for_driver.get(user_id)
        if not order_id:
            bot.send_message(user_id, "❌ У вас нет активного заказа")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("🚗 Еду к пассажиру", callback_data=f"dstatus_driving_{order_id}"),
            types.InlineKeyboardButton("📍 На месте", callback_data=f"dstatus_arrived_{order_id}"),
            types.InlineKeyboardButton("🛣 Везу пассажира", callback_data=f"dstatus_trip_{order_id}"),
            types.InlineKeyboardButton("✅ Завершить поездку", callback_data=f"dstatus_complete_{order_id}")
        )
        cur = driver_status.get(user_id, "Не установлен")
        bot.send_message(user_id, f"📊 <b>Текущий статус:</b> {cur}\n\nВыберите новый статус:", reply_markup=markup)

    elif user_role.get(user_id) == "passenger":
        active_order = None
        active_order_id = None
        for oid, order in orders.items():
            if (order.get("passenger_id") == user_id and
                    order["status"] in ["accepted", "driving", "arrived", "trip"]):
                active_order = order
                active_order_id = oid
                break
        if not active_order:
            bot.send_message(user_id, "❌ У вас нет активных заказов")
            return
        driver_id = active_order.get("driver_id")
        status = driver_status.get(driver_id, "Водитель в пути")
        driver = driver_info.get(driver_id, {})
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💬 Чат", callback_data=f"chat_{active_order_id}"))
        markup.add(types.InlineKeyboardButton("📍 Где водитель?", callback_data=f"track_{active_order_id}"))
        text = (
            f"📊 <b>СТАТУС ПОЕЗДКИ #{active_order_id}</b>\n\n"
            f"🚗 Водитель: {driver.get('name', 'Не указано')}\n"
            f"🚘 Авто: {driver.get('car_color', '')} {driver.get('car_model', '')}\n"
            f"🔢 Номер: {driver.get('car_number', 'Не указан')}\n\n"
            f"📌 <b>Статус:</b> {status}"
        )
        bot.send_message(user_id, text, reply_markup=markup)
    else:
        bot.send_message(user_id, "❌ Сначала выберите роль (Заказать / Водитель)")

@bot.message_handler(commands=['cancel'])
def cancel_cmd(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "passenger":
        bot.send_message(user_id, "❌ Только пассажир может отменить заказ")
        return
    active = None
    active_id = None
    for oid, o in orders.items():
        if o.get("passenger_id") == user_id and o["status"] in ["accepted", "driving", "arrived", "trip"]:
            active = o
            active_id = oid
            break
    if not active:
        bot.send_message(user_id, "❌ Нет активного заказа для отмены")
        return
    active["status"] = "cancelled"
    driver_id = active.get("driver_id")
    if driver_id:
        driver_busy[driver_id] = False
        driver_status.pop(driver_id, None)
        active_order_for_driver.pop(driver_id, None)
        try:
            bot.send_message(driver_id, "❌ Пассажир отменил заказ.")
        except Exception:
            pass
    bot.send_message(user_id, "❌ Заказ отменён.")
    save_state()

@bot.message_handler(func=lambda m: m.text == "🟢 На линии")
def go_online(message):
    user_id = message.chat.id
    if user_id not in driver_info or not driver_info[user_id]:
        bot.send_message(user_id, "❌ Сначала заполните профиль (кнопка «👤 Профиль» или /profile)")
        return
    driver_online[user_id] = True
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📍 Отправить локацию", request_location=True))
    markup.add(types.KeyboardButton(
        "🗺 Карта водителя",
        web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/driver_map.html")
    ))
    markup.add("🔙 Назад")
    bot.send_message(user_id, "✅ Вы на линии! Отправьте вашу геолокацию или откройте карту.", reply_markup=markup)
    save_state()

@bot.message_handler(func=lambda m: m.text == "🔴 Оффлайн")
def go_offline(message):
    user_id = message.chat.id
    driver_online[user_id] = False
    bot.send_message(user_id, "❌ Вы вышли с линии. Новые заказы приходить не будут.")
    save_state()

@bot.message_handler(func=lambda m: m.text == "💰 Баланс")
def balance_button(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "driver":
        return
    balance = driver_balance.get(user_id, 0)
    completed = sum(1 for o in orders.values()
                    if o.get("driver_id") == user_id and o["status"] == "completed")
    bot.send_message(
        user_id,
        f"💰 <b>Ваш баланс</b>\n\n"
        f"💵 Заработано всего: <b>{balance} GEL</b>\n"
        f"✅ Выполнено заказов: <b>{completed}</b>\n"
        f"📊 Комиссия сервиса: <b>{int(COMMISSION_RATE * 100)}%</b>"
    )

@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def driver_stats(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "driver":
        return
    completed = sum(1 for o in orders.values()
                    if o.get("driver_id") == user_id and o["status"] == "completed")
    total_earned = driver_balance.get(user_id, 0)
    rating_list = driver_rating.get(user_id, [])
    rating = f"{sum(rating_list) / len(rating_list):.1f}⭐" if rating_list else "Нет оценок"
    bot.send_message(
        user_id,
        f"📊 <b>Ваша статистика</b>\n\n"
        f"✅ Выполнено заказов: <b>{completed}</b>\n"
        f"💰 Заработано: <b>{total_earned} GEL</b>\n"
        f"⭐ Рейтинг: <b>{rating}</b>"
    )

@bot.message_handler(func=lambda m: m.text == "📍 Локация")
def ask_driver_location(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "driver":
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📍 Отправить локацию", request_location=True))
    markup.add("🔙 Назад")
    bot.send_message(user_id, "📍 Нажмите кнопку, чтобы отправить вашу геопозицию:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📍 Откуда")
def ask_from(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "passenger":
        return
    if user_id not in passenger_data:
        passenger_data[user_id] = {}
    passenger_data[user_id]["waiting_for"] = "from"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📍 Отправить геопозицию", request_location=True))
    markup.add("📝 Ввести адрес")
    markup.add("🔙 Назад")
    bot.send_message(user_id, "📍 <b>Откуда вас забрать?</b>", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "🏁 Куда")
def ask_to(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "passenger":
        return
    if user_id not in passenger_data or "from" not in passenger_data.get(user_id, {}):
        bot.send_message(user_id, "❌ Сначала укажите точку отправления (📍 Откуда)")
        return
    passenger_data[user_id]["waiting_for"] = "to"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📍 Отправить геопозицию", request_location=True))
    markup.add("📝 Ввести адрес")
    markup.add("🔙 Назад")
    bot.send_message(user_id, "🏁 <b>Куда едем?</b>", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📝 Ввести адрес")
def manual_address(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "passenger":
        return
    if user_id not in passenger_data:
        passenger_data[user_id] = {}
    waiting_for = passenger_data[user_id].get("waiting_for", "from")
    hint = "отправления" if waiting_for == "from" else "назначения"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔙 Отмена")
    bot.send_message(
        user_id,
        f"📝 <b>Введите адрес {hint}:</b>\n\n<i>Например: улица Лермонтова 31</i>",
        reply_markup=markup
    )
    suggest_sessions[user_id] = {"waiting_for": waiting_for, "step": "waiting"}

@bot.message_handler(func=lambda m: m.text == "🔙 Отмена")
def cancel_manual_input(message):
    user_id = message.chat.id
    for d in [suggest_sessions, temp_suggestions]:
        if user_id in d:
            del d[user_id]
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📍 Откуда", "🏁 Куда")
    markup.add(types.KeyboardButton(
        "🗺 Выбрать на карте",
        web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
    ))
    markup.add("🚀 Создать заказ", "❌ Отмена")
    markup.add("📊 Статус", "🔙 Меню")
    bot.send_message(user_id, "◀️ Ввод адреса отменён", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "❌ Отмена")
def cancel_passenger(message):
    user_id = message.chat.id
    if user_id in passenger_data:
        del passenger_data[user_id]
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📍 Откуда", "🏁 Куда")
    markup.add(types.KeyboardButton(
        "🗺 Выбрать на карте",
        web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
    ))
    markup.add("🚀 Создать заказ", "❌ Отмена")
    markup.add("📊 Статус", "🔙 Меню")
    bot.send_message(user_id, "❌ Маршрут очищен", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "🚀 Создать заказ")
def create_order_text(message):
    user_id = message.chat.id
    if user_role.get(user_id) != "passenger":
        return
    data = passenger_data.get(user_id, {})
    if "from" not in data or "to" not in data:
        bot.send_message(user_id, "❌ Укажите обе точки маршрута")
        return
    _create_order(user_id, data["from"], data["to"],
                  data.get("from_address"), data.get("to_address"))

def _create_order(user_id, from_coords, to_coords, from_address=None, to_address=None):
    order_id = get_next_order_id()
    orders[order_id] = {
        "passenger_id": user_id,
        "from": from_coords,
        "to": to_coords,
        "from_address": from_address,
        "to_address": to_address,
        "status": "searching",
        "created_at": datetime.now().isoformat()
    }
    save_state()

    bot.send_message(user_id, f"🔍 <b>Ищем водителя...</b>\nНомер заказа: #{order_id}")
    threading.Thread(target=dispatch_order, args=(order_id,), daemon=True).start()
    threading.Thread(target=expire_order, args=(order_id,), daemon=True).start()

@bot.message_handler(content_types=['location'])
def handle_location(message):
    user_id = message.chat.id
    lat = message.location.latitude
    lon = message.location.longitude
    role = user_role.get(user_id)

    if not is_valid_location(lat, lon):
        bot.send_message(user_id, "❌ Вы находитесь вне зоны обслуживания (Грузия).")
        return

    if role == "driver":
        if not driver_online.get(user_id):
            bot.send_message(user_id, "❌ Сначала нажмите «🟢 На линии»")
            return
        driver_locations[user_id] = (lat, lon)

        order_id = active_order_for_driver.get(user_id)
        if order_id:
            order = orders.get(order_id)
            if order:
                try:
                    bot.send_message(order["passenger_id"],
                                     f"📍 <b>Водитель обновил местоположение!</b>\n{format_location(lat, lon)}")
                except Exception:
                    pass

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🟢 На линии", "🔴 Оффлайн")
        markup.add(types.KeyboardButton(
            "🗺 Карта водителя",
            web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/driver_map.html")
        ))
        markup.add("📍 Локация", "📊 Статистика")
        markup.add("👤 Профиль", "💰 Баланс")
        markup.add("📌 Статус", "🔙 Меню")
        bot.send_message(user_id, f"✅ Локация обновлена!\n{format_location(lat, lon)}", reply_markup=markup)
        save_state()

    elif role == "passenger":
        if user_id not in passenger_data:
            passenger_data[user_id] = {}
        waiting_for = passenger_data[user_id].get("waiting_for", "from")
        if waiting_for == "from":
            passenger_data[user_id].update({
                "from": (lat, lon), "from_lat": lat, "from_lon": lon,
                "from_address": "Текущее местоположение"
            })
        else:
            passenger_data[user_id].update({
                "to": (lat, lon), "to_lat": lat, "to_lon": lon,
                "to_address": "Точка на карте"
            })
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📍 Откуда", "🏁 Куда")
        markup.add(types.KeyboardButton(
            "🗺 Выбрать на карте",
            web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
        ))
        markup.add("🚀 Создать заказ", "❌ Отмена")
        markup.add("📊 Статус", "🔙 Меню")
        bot.send_message(user_id, f"✅ Точка сохранена!\n{format_location(lat, lon)}", reply_markup=markup)
        save_state()

@bot.message_handler(content_types=['web_app_data'])
def handle_webapp_data(message):
    user_id = message.chat.id
    try:
        data = json.loads(message.web_app_data.data)
    except Exception as e:
        bot.send_message(user_id, "❌ Ошибка данных с карты.")
        return

    action = data.get("action")

    if action == "select_points":
        try:
            from_lat = float(data.get("from_lat"))
            from_lon = float(data.get("from_lon"))
            from_addr = data.get("from_address", "Точка на карте")
            to_lat = float(data.get("to_lat"))
            to_lon = float(data.get("to_lon"))
            to_addr = data.get("to_address", "Точка на карте")
            distance = data.get("distance", "?")
            price = data.get("price", "?")
        except Exception:
            bot.send_message(user_id, "❌ Неверные координаты.")
            return

        if not is_valid_location(from_lat, from_lon) or not is_valid_location(to_lat, to_lon):
            bot.send_message(user_id, "❌ Одна из точек вне зоны обслуживания.")
            return

        if user_id not in passenger_data:
            passenger_data[user_id] = {}

        passenger_data[user_id].update({
            "from": (from_lat, from_lon),
            "from_lat": from_lat, "from_lon": from_lon,
            "from_address": from_addr,
            "to": (to_lat, to_lon),
            "to_lat": to_lat, "to_lon": to_lon,
            "to_address": to_addr,
        })

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Подтвердить и заказать", callback_data="confirm_order_from_map"))

        bot.send_message(
            user_id,
            f"✅ <b>Маршрут выбран!</b>\n\n"
            f"📍 <b>Откуда:</b> {from_addr}\n"
            f"🏁 <b>Куда:</b> {to_addr}\n"
            f"📏 <b>Расстояние:</b> {distance} км\n"
            f"💰 <b>Стоимость:</b> {price} GEL\n\n"
            f"Нажмите «Подтвердить», чтобы создать заказ:",
            reply_markup=markup
        )
        save_state()

    elif action == "driver_location":
        try:
            lat = float(data.get("lat"))
            lon = float(data.get("lon"))
        except Exception:
            return

        if not is_valid_location(lat, lon):
            bot.send_message(user_id, "❌ Вы вне зоны обслуживания.")
            return

        if user_role.get(user_id) == "driver" and driver_online.get(user_id):
            driver_locations[user_id] = (lat, lon)
            order_id = active_order_for_driver.get(user_id)
            if order_id:
                order = orders.get(order_id)
                if order:
                    try:
                        bot.send_message(order["passenger_id"],
                                         f"📍 <b>Водитель обновил местоположение!</b>\n{format_location(lat, lon)}")
                    except Exception:
                        pass
            bot.send_message(user_id, f"✅ Локация обновлена!\n{format_location(lat, lon)}")
            save_state()
        else:
            bot.send_message(user_id, "❌ Вы не на линии.")

@bot.callback_query_handler(func=lambda call: call.data == "confirm_order_from_map")
def confirm_order_from_map(call):
    user_id = call.message.chat.id
    data = passenger_data.get(user_id)
    if not data or "from" not in data or "to" not in data:
        bot.answer_callback_query(call.id, "❌ Данные маршрута не найдены")
        return

    bot.edit_message_text(
        f"🔍 <b>Ищем водителя...</b>",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )
    _create_order(user_id, data["from"], data["to"],
                  data.get("from_address"), data.get("to_address"))
    bot.answer_callback_query(call.id, "✅ Заказ создан!")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    user_id = message.chat.id
    text = message.text

    if user_id in chat_sessions:
        target_id = chat_sessions[user_id]
        sender_role = "Водитель" if user_role.get(user_id) == "driver" else "Пассажир"
        try:
            bot.send_message(target_id, f"💬 <b>{sender_role}:</b>\n{text}")
            bot.send_message(user_id, "✅ Сообщение отправлено (для выхода нажмите «Назад»)")
        except Exception:
            bot.send_message(user_id, "❌ Не удалось отправить сообщение.")
        return

    session = suggest_sessions.get(user_id)
    if not session or session.get("step") != "waiting":
        return

    def on_suggestions(chat_id, suggestions):
        if not suggestions:
            bot.send_message(chat_id, "❌ Ничего не найдено. Попробуйте другой адрес.")
            return
        temp_suggestions[chat_id] = suggestions
        markup = types.InlineKeyboardMarkup(row_width=1)
        for i, s in enumerate(suggestions):
            markup.add(types.InlineKeyboardButton(f"📍 {s['display'][:50]}", callback_data=f"suggest_{i}"))
        markup.add(types.InlineKeyboardButton("🔍 Другой адрес", callback_data="suggest_other"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="suggest_cancel"))
        bot.send_message(chat_id, "🔍 <b>Найдены адреса:</b>\n\nВыберите подходящий вариант:", reply_markup=markup)

    get_address_suggestions_async(text, user_id, on_suggestions)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.message.chat.id
    data = call.data

    if data.startswith("accept_"):
        order_id = int(data.split("_")[1])
        with order_lock:
            order = orders.get(order_id)
            if not order or order["status"] != "searching":
                bot.answer_callback_query(call.id, "❌ Заказ уже не актуален")
                return
            if driver_busy.get(user_id, False):
                bot.answer_callback_query(call.id, "❌ У вас уже активный заказ!")
                return
            order["status"] = "accepted"
            order["driver_id"] = user_id
            driver_busy[user_id] = True
            active_order_for_driver[user_id] = order_id
            driver_status[user_id] = "🚗 Еду к пассажиру"
            accepted_driver_id = user_id

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "✅ Заказ принят!")
        notify_other_drivers(order_id, accepted_driver_id)
        send_driver_info_to_passenger(order["passenger_id"], user_id, order_id)

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📍 На месте", callback_data=f"dstatus_arrived_{order_id}"),
            types.InlineKeyboardButton("🛣 Везу пассажира", callback_data=f"dstatus_trip_{order_id}"),
            types.InlineKeyboardButton("✅ Завершить поездку", callback_data=f"dstatus_complete_{order_id}"),
            types.InlineKeyboardButton("💬 Чат с пассажиром", callback_data=f"chat_{order_id}")
        )
        from_text = order.get("from_address") or format_location(order['from'][0], order['from'][1])
        to_text = order.get("to_address") or format_location(order['to'][0], order['to'][1])
        bot.send_message(
            user_id,
            f"✅ Вы приняли заказ!\n\n"
            f"👤 Пассажир ожидает:\n{from_text}\n\n"
            f"🏁 Пункт назначения:\n{to_text}\n\n"
            f"💰 Стоимость: <b>{order.get('price', '?')} GEL</b>",
            reply_markup=markup
        )
        save_state()

    elif data.startswith("decline_"):
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "❌ Заказ отклонён")

    elif data.startswith("dstatus_"):
        parts = data.split("_")
        status_type = parts[1]
        order_id = int(parts[2])
        order = orders.get(order_id)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        status_map = {
            "driving": ("🚗 Еду к пассажиру", "🚗 Водитель едет к вам!"),
            "arrived": ("📍 На месте", "📍 Водитель на месте! Выходите, пожалуйста."),
            "trip": ("🛣 Везу пассажира", "🛣 Поездка началась! Водитель везёт вас."),
        }

        if status_type in status_map:
            new_status, passenger_msg = status_map[status_type]
            driver_status[user_id] = new_status
            bot.answer_callback_query(call.id, "✅ Статус обновлён")
            try:
                bot.send_message(order["passenger_id"], passenger_msg)
            except Exception:
                pass
        elif status_type == "complete":
            complete_order_internal(order_id, user_id)
            bot.answer_callback_query(call.id, "✅ Заказ завершён!")

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        save_state()

    elif data.startswith("chat_"):
        order_id = int(data.split("_")[1])
        order = orders.get(order_id)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return
        if user_role.get(user_id) == "passenger":
            target_id = order.get("driver_id")
            partner = "водителем"
        else:
            target_id = order.get("passenger_id")
            partner = "пассажиром"
        chat_sessions[user_id] = target_id
        bot.send_message(user_id, f"💬 <b>Чат с {partner}</b>\n\nВведите сообщение. Для выхода нажмите «🔙 Назад» в меню.")
        bot.answer_callback_query(call.id, "💬 Чат открыт")

    elif data.startswith("call_"):
        order_id = int(data.split("_")[1])
        order = orders.get(order_id)
        driver_id = order.get("driver_id")
        driver = driver_info.get(driver_id, {})
        phone = driver.get("phone", "Не указан")
        bot.send_message(user_id, f"📞 Телефон водителя: <code>{phone}</code>")
        bot.answer_callback_query(call.id)

    elif data.startswith("track_"):
        order_id = int(data.split("_")[1])
        order = orders.get(order_id)
        driver_id = order.get("driver_id")
        loc = driver_locations.get(driver_id)
        if loc:
            bot.send_message(user_id, f"📍 <b>Местоположение водителя:</b>\n{format_location(loc[0], loc[1])}")
        else:
            bot.send_message(user_id, "❌ Местоположение недоступно.")
        bot.answer_callback_query(call.id)

    elif data.startswith("cancel_order_"):
        order_id = int(data.split("_")[2])
        order = orders.get(order_id)
        if order and order["status"] not in ["completed", "cancelled", "expired"]:
            order["status"] = "cancelled"
            driver_id = order.get("driver_id")
            if driver_id:
                driver_busy[driver_id] = False
                driver_status.pop(driver_id, None)
                active_order_for_driver.pop(driver_id, None)
                try:
                    bot.send_message(driver_id, "❌ Пассажир отменил заказ.")
                except Exception:
                    pass
            bot.send_message(user_id, "❌ Заказ отменён.")
            save_state()
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    elif data.startswith("rate_"):
        order_id = int(data.split("_")[1])
        markup = types.InlineKeyboardMarkup(row_width=5)
        markup.add(*[types.InlineKeyboardButton(f"{'⭐'*i}", callback_data=f"rating_{order_id}_{i}") for i in range(1, 6)])
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(user_id, "⭐ <b>Оцените поездку:</b>", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif data.startswith("rating_"):
        parts = data.split("_")
        order_id, stars = int(parts[1]), int(parts[2])
        order = orders.get(order_id)
        if order:
            driver_id = order.get("driver_id")
            if driver_id:
                if driver_id not in driver_rating:
                    driver_rating[driver_id] = []
                driver_rating[driver_id].append(stars)
                avg = sum(driver_rating[driver_id]) / len(driver_rating[driver_id])
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                bot.send_message(user_id, f"✅ Спасибо за оценку! {'⭐'*stars}")
                try:
                    bot.send_message(driver_id, f"⭐ Вы получили оценку: {'⭐'*stars}\nВаш рейтинг: <b>{avg:.1f}⭐</b>")
                except Exception:
                    pass
                save_state()
        bot.answer_callback_query(call.id)

    elif data.startswith("suggest_"):
        if data == "suggest_other":
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(user_id, "📝 Введите другой адрес:")
            if user_id in suggest_sessions:
                suggest_sessions[user_id]["step"] = "waiting"
            bot.answer_callback_query(call.id)
            return

        if data == "suggest_cancel":
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            for d in [suggest_sessions, temp_suggestions]:
                if user_id in d:
                    del d[user_id]
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("📍 Откуда", "🏁 Куда")
            markup.add(types.KeyboardButton(
                "🗺 Выбрать на карте",
                web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
            ))
            markup.add("🚀 Создать заказ", "❌ Отмена")
            markup.add("📊 Статус", "🔙 Меню")
            bot.send_message(user_id, "❌ Выбор отменён", reply_markup=markup)
            bot.answer_callback_query(call.id)
            return

        try:
            index = int(data.split("_")[1])
        except Exception:
            return

        suggestions = temp_suggestions.get(user_id, [])
        if index >= len(suggestions):
            bot.answer_callback_query(call.id, "❌ Адрес не найден")
            return

        selected = suggestions[index]
        session = suggest_sessions.get(user_id)
        if not session:
            return

        waiting_for = session.get("waiting_for")

        def on_geocode(chat_id, lat, lon, addr):
            if not lat or not lon:
                lat, lon, addr = selected.get('lat'), selected.get('lon'), selected['display']
            if not lat or not lon:
                bot.send_message(chat_id, "❌ Не удалось определить координаты")
                return

            if chat_id not in passenger_data:
                passenger_data[chat_id] = {}

            if waiting_for == "from":
                passenger_data[chat_id].update({"from": (lat, lon), "from_lat": lat, "from_lon": lon, "from_address": addr})
            else:
                passenger_data[chat_id].update({"to": (lat, lon), "to_lat": lat, "to_lon": lon, "to_address": addr})

            for d in [suggest_sessions, temp_suggestions]:
                if chat_id in d:
                    del d[chat_id]

            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("📍 Откуда", "🏁 Куда")
            markup.add(types.KeyboardButton(
                "🗺 Выбрать на карте",
                web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")
            ))
            markup.add("🚀 Создать заказ", "❌ Отмена")
            markup.add("📊 Статус", "🔙 Меню")
            bot.send_message(chat_id, f"✅ Адрес сохранён:\n<b>{addr}</b>\n{format_location(lat, lon)}", reply_markup=markup)
            save_state()

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        geocode_address_async(selected['display'], user_id, on_geocode)
        bot.answer_callback_query(call.id, "⏳ Обрабатываем...")

# ============================================================
# ЗАПУСК БОТА В ОТДЕЛЬНОМ ПОТОКЕ И СТАРТ СЕРВЕРА
# ============================================================
@app.on_event("startup")
def on_startup():
    print("🚕 Инициализация данных (PostgreSQL)...")
    load_state()
    
    print("🤖 Запуск Telegram Бота в фоновом потоке...")
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

def run_bot():
    while True:
        try:
            # Увеличенные таймауты для стабильности
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            print(f"❌ Ошибка в главном цикле бота: {e}")
            time.sleep(5)

if __name__ == "__main__":
    print("=" * 50)
    print("🚕 USERTAXI SERVER (FastAPI + PostgreSQL + TeleBot)")
    print("=" * 50)
    # На bothost.ru порт обычно берется из переменной окружения PORT
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
