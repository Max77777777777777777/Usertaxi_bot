import os
import json
import math
import asyncio
import datetime
import httpx
from telebot.async_telebot import AsyncTeleBot
from telebot import types

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Float, Integer, DateTime, Boolean, ForeignKey, Text, select, update

# ============================================================
# КОНФИГУРАЦИЯ И ОКРУЖЕНИЕ
# ============================================================
TOKEN = os.getenv("TAXI_BOT_TOKEN", "")
YANDEX_SUGGEST_KEY = os.getenv("YANDEX_SUGGEST_KEY", "")
YANDEX_GEOCODER_KEY = os.getenv("YANDEX_GEOCODER_KEY", "")
MINI_APP_BASE_URL = os.getenv("MINI_APP_BASE_URL", "https://your-domain.com")

# URL для PostgreSQL (замените на свои учетные данные при необходимости)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/usertaxi")

if not TOKEN:
    raise RuntimeError("❌ TAXI_BOT_TOKEN не задан в переменных окружения!")

bot = AsyncTeleBot(TOKEN)

# Тарифная сетка
BASE_PRICE = 2.0
PRICE_PER_KM = 1.0

# ============================================================
# СХЕМА БАЗЫ ДАННЫХ (SQLAlchemy 2.0)
# ============================================================
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=15, max_overflow=25)
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(AsyncAttrs, DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=True)  # 'passenger' или 'driver'
    
    # Профиль водителя
    name: Mapped[str] = mapped_column(String(100), nullable=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=True)
    car_info: Mapped[str] = mapped_column(String(200), nullable=True) # Марка, цвет, номер
    
    # Состояния водителя
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    is_busy: Mapped[bool] = mapped_column(Boolean, default=False)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Последние координаты
    lat: Mapped[float] = mapped_column(Float, nullable=True)
    lon: Mapped[float] = mapped_column(Float, nullable=True)

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    passenger_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)
    
    from_address: Mapped[str] = mapped_column(Text, nullable=True)
    to_address: Mapped[str] = mapped_column(Text, nullable=True)
    
    from_lat: Mapped[float] = mapped_column(Float)
    from_lon: Mapped[float] = mapped_column(Float)
    to_lat: Mapped[float] = mapped_column(Float)
    to_lon: Mapped[float] = mapped_column(Float)
    
    distance: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    
    # Статусы: searching, accepted, arrived, trip, completed, cancelled
    status: Mapped[str] = mapped_column(String(20), default="searching")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def calc_dist(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

async def get_user(telegram_id: int, session):
    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.commit()
    return user

# ============================================================
# ОБРАБОТЧИКИ КОМАНД БОТА
# ============================================================
@bot.message_handler(commands=['start'])
async def cmd_start(message):
    async with async_session() as session:
        user = await get_user(message.from_user.id, session)
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_pass = types.InlineKeyboardButton("🚕 Пассажир", callback_data="set_role_passenger")
        btn_driver = types.InlineKeyboardButton("🚘 Водитель", callback_data="set_role_driver")
        markup.add(btn_pass, btn_driver)
        
        await bot.send_message(
            message.chat.id, 
            "👋 Добро пожаловать в **UserTaxi**!\nВыберите вашу роль для настройки интерфейса:", 
            reply_markup=markup,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_role_"))
async def handle_role_selection(call):
    role = call.data.replace("set_role_", "")
    async with async_session() as session:
        await session.execute(
            update(User).where(User.telegram_id == call.from_user.id).values(role=role)
        )
        await session.commit()
        
    await bot.answer_callback_query(call.id, "Роль успешно сохранена!")
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    
    if role == "passenger":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton("🚀 Вызвать такси", web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/index.html")))
        await bot.send_message(call.message.chat.id, "📱 Меню пассажира активировано. Нажмите кнопку ниже для заказа:", reply_markup=markup)
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton("🧭 Панель водителя", web_app=types.WebAppInfo(url=f"{MINI_APP_BASE_URL}/driver_map.html")))
        markup.add("🟢 Выйти на линию", "🔴 Уйти с линии")
        await bot.send_message(call.message.chat.id, "🚖 Меню водителя активировано. Настройте статус на линии:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text in ["🟢 Выйти на линию", "🔴 Уйти с линии"])
async def handle_driver_status(message):
    online = message.text == "🟢 Выйти на линию"
    async with async_session() as session:
        await session.execute(
            update(User).where(User.telegram_id == message.from_user.id).values(is_online=online)
        )
        await session.commit()
    status_text = "🟢 Вы теперь НА ЛИНИИ и получаете заказы." if online else "🔴 Вы ушли с линии."
    await bot.send_message(message.chat.id, status_text)

# ============================================================
# ПРИЕМ ДАННЫХ ИЗ MINI APPS (WEB APP DATA)
# ============================================================
@bot.message_handler(content_types=['web_app_data'])
async def handle_web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        
        async with async_session() as session:
            if action == "select_points":
                # Создание заказа пассажиром
                new_order = Order(
                    passenger_id=message.from_user.id,
                    from_address=data.get("from_address"),
                    to_address=data.get("to_address"),
                    from_lat=float(data.get("from_lat")),
                    from_lon=float(data.get("from_lon")),
                    to_lat=float(data.get("to_lat")),
                    to_lon=float(data.get("to_lon")),
                    distance=float(data.get("distance")),
                    price=float(data.get("price")),
                    status="searching"
                )
                session.add(new_order)
                await session.flush() # Получаем ID заказа
                order_id = new_order.id
                await session.commit()
                
                await bot.send_message(message.chat.id, f"🔍 Поиск водителя для заказа **№{order_id}**...\n📍 Откуда: {new_order.from_address}\n🏁 Куда: {new_order.to_address}\n💵 Стоимость: {new_order.price} GEL", parse_mode="Markdown")
                
                # Рассылка свободным водителям в радиусе
                drivers_res = await session.execute(select(User).where(User.role == "driver", User.is_online == True, User.is_busy == False))
                drivers = drivers_res.scalars().all()
                
                for driver in drivers:
                    if driver.lat and driver.lon:
                        d = calc_dist(new_order.from_lat, new_order.from_lon, driver.lat, driver.lon)
                        if d <= 5.0: # В радиусе 5 км
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("✅ Принять заказ", callback_data=f"accept_{order_id}"))
                            await bot.send_message(
                                driver.telegram_id,
                                f"🔔 **Новый заказ №{order_id}!** ({round(d, 2)} км от вас)\n📍 Из: {new_order.from_address}\n🏁 В: {new_order.to_address}\n💰 Доход: {new_order.price} GEL",
                                reply_markup=markup,
                                parse_mode="Markdown"
                            )
                            
            elif action == "driver_location":
                # Обновление координат водителя из driver_map.html
                await session.execute(
                    update(User).where(User.telegram_id == message.from_user.id).values(
                        lat=float(data.get("lat")),
                        lon=float(data.get("lng"))
                    )
                )
                await session.commit()
                
    except Exception as e:
        print(f"❌ Ошибка обработки WebAppData: {e}")

# ============================================================
# ЖИЗНЕННЫЙ ЦИКЛ ЗАКАЗА (CALLBACK QUERIES)
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
async def handle_accept_order(call):
    order_id = int(call.data.replace("accept_", ""))
    driver_id = call.from_user.id
    
    async with async_session() as session:
        # Проверяем, свободен ли еще заказ и сам водитель
        order_res = await session.execute(select(Order).where(Order.id == order_id))
        order = order_res.scalar_one_or_none()
        
        driver_res = await session.execute(select(User).where(User.telegram_id == driver_id))
        driver = driver_res.scalar_one_or_none()
        
        if not order or order.status != "searching":
            await bot.answer_callback_query(call.id, "❌ Заказ уже взят другим водителем или отменен.")
            await bot.delete_message(call.message.chat.id, call.message.message_id)
            return
            
        if driver.is_busy:
            await bot.answer_callback_query(call.id, "❌ У вас уже есть активный заказ.")
            return
            
        # Обновляем статусы
        order.status = "accepted"
        order.driver_id = driver_id
        driver.is_busy = True
        await session.commit()
        
        await bot.answer_callback_query(call.id, "Вы приняли заказ!")
        await bot.delete_message(call.message.chat.id, call.message.message_id)
        
        # Управленческая панель для выполнения поездки
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏁 Завершить поездку", callback_data=f"complete_{order_id}"))
        await bot.send_message(driver_id, f"🚖 Вы выполняете заказ №{order_id}.\nМаршрут: {order.from_address} -> {order.to_address}", reply_markup=markup)
        
        # Уведомляем пассажира
        await bot.send_message(order.passenger_id, f"🚘 Водитель принял ваш заказ №{order_id}!\n📱 Машина скоро будет на месте.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("complete_"))
async def handle_complete_order(call):
    order_id = int(call.data.replace("complete_", ""))
    
    async with async_session() as session:
        order_res = await session.execute(select(Order).where(Order.id == order_id))
        order = order_res.scalar_one_or_none()
        
        if order and order.status == "accepted":
            order.status = "completed"
            
            # Начисляем баланс водителю
            await session.execute(
                update(User).where(User.telegram_id == order.driver_id).values(
                    is_busy=False,
                    balance=User.balance + order.price
                )
            )
            await session.commit()
            
            await bot.answer_callback_query(call.id, "Поездка успешно завершена!")
            await bot.delete_message(call.message.chat.id, call.message.message_id)
            
            await bot.send_message(order.driver_id, f"💵 Заказ №{order_id} закрыт. На ваш баланс зачислено {order.price} GEL.")
            await bot.send_message(order.passenger_id, f"✨ Спасибо за поездку! Заказ №{order_id} успешно завершен.")

# ============================================================
# АСИНХРОННЫЙ ЗАПУСК БОТА
# ============================================================
async def main():
    print("🚀 Инициализация базы данных PostgreSQL...")
    await init_db()
    print("✅ База данных готова к работе!")
    print(f"📡 Запуск AsyncTeleBot на сервере bothost.ru... MiniApp URL: {MINI_APP_BASE_URL}")
    
    # Запускаем поллинг без блокировки основного потока
    await bot.infinity_polling(timeout=20, long_polling_timeout=10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен администратором.")
