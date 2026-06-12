package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/Pauloo27/gotgbot/v2"
	"github.com/Pauloo27/gotgbot/v2/ext"
	"github.com/Pauloo27/gotgbot/v2/ext/handlers"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	db           *pgxpool.Pool
	miniAppURL   string
	yandexGeoKey string
)

// WebAppData — структура для парсинга данных из Leaflet-карты в Mini App
type WebAppData struct {
	Action      string `json:"action"`
	FromAddress string `json:"from_address"`
	ToAddress   string `json:"to_address"`
	FromLat     string `json:"from_lat"`
	FromLon     string `json:"from_lon"`
	ToLat       string `json:"to_lat"`
	ToLon       string `json:"to_lon"`
	Distance    string `json:"distance"`
	Price       string `json:"price"`
	Lat         string `json:"lat"`
	Lng         string `json:"lng"`
}

// calcDist вычисляет расстояние между координатами (Формула гаверсинуса)
func calcDist(lat1, lon1, lat2, lon2 float64) float64 {
	rad := math.Pi / 180
	dlat := (lat2 - lat1) * rad
	dlon := (lon2 - lon1) * rad
	a := math.Sin(dlat/2)*math.Sin(dlat/2) + math.Cos(lat1*rad)*math.Cos(lat2*rad)*math.Sin(dlon/2)*math.Sin(dlon/2)
	return 6371.0 * 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
}

// initDB настраивает пул соединений и инициализирует таблицы базы данных
func initDB(ctx context.Context, dsn string) {
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		log.Fatalf("Ошибка конфигурации DSN: %v", err)
	}
	config.MaxConns = 15
	config.MinConns = 2

	db, err = pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		log.Fatalf("Не удалось подключить pgxpool: %v", err)
	}

	queries := []string{
		`CREATE TABLE IF NOT EXISTS users(
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
		)`,
		`CREATE TABLE IF NOT EXISTS orders(
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
		)`,
		`CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(ctx, q); err != nil {
			log.Fatalf("Ошибка при создании таблиц: %v", err)
		}
	}
	log.Println("✅ База данных PostgreSQL успешно инициализирована")
}

func main() {
	// === Строгие настройки из env (Fail-Fast) ===
	token := os.Getenv("TAXI_BOT_TOKEN")
	dsn := os.Getenv("PG_DSN")
	miniAppURL = os.Getenv("MINI_APP_BASE_URL")
	yandexGeoKey = os.Getenv("YANDEX_GEOCODER_KEY")

	if token == "" || dsn == "" || miniAppURL == "" || yandexGeoKey == "" {
		log.Fatal("❌ Ошибка: TAXI_BOT_TOKEN, PG_DSN, MINI_APP_BASE_URL и YANDEX_GEOCODER_KEY должны быть заданы в переменных окружения на сервере!")
	}

	ctx := context.Background()
	initDB(ctx, dsn)
	defer db.Close()

	// Инициализация бота gotgbot
	b, err := gotgbot.NewBot(token, &gotgbot.BotOpts{
		Client:      http.Client{},
		DefaultArgs: &gotgbot.DefaultBotArgs{},
	})
	if err != nil {
		log.Fatalf("Не удалось запустить gotgbot: %v", err)
	}

	updater := ext.NewUpdater(&ext.UpdaterOpts{})
	dispatcher := updater.Dispatcher

	// ============================================================
	// ХЭНДЛЕР: Команда /start
	// ============================================================
	dispatcher.AddHandler(handlers.NewCommand("start", func(b *gotgbot.Bot, ctx *ext.Context) error {
		uid := ctx.EffectiveSender.Id()
		
		_, err := db.Exec(context.Background(), "INSERT INTO users(telegram_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
		if err != nil {
			log.Printf("Ошибка базы на /start: %v", err)
		}

		opts := &gotgbot.SendMessageOpts{
			ParseMode: "HTML",
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{
				InlineKeyboard: [][]gotgbot.InlineKeyboardButton{
					{
						{Text: "🚕 Пассажир", CallbackData: "set_role_passenger"},
						{Text: "🚘 Водитель", CallbackData: "set_role_driver"},
					},
				},
			},
		}
		_, err = b.SendMessage(ctx.EffectiveChat.Id, "👋 Добро пожаловать в <b>UserTaxi</b>!\nВыберите вашу роль:", opts)
		return err
	}))

	// ============================================================
	// ХЭНДЛЕР: Выбор роли (CallbackQuery)
	// ============================================================
	dispatcher.AddHandler(handlers.NewCallback(func(cb *gotgbot.CallbackQuery) bool {
		return strings.HasPrefix(cb.Data, "set_role_")
	}, func(b *gotgbot.Bot, ctx *ext.Context) error {
		cb := ctx.Update.CallbackQuery
		uid := cb.From.Id
		role := strings.Replace(cb.Data, "set_role_", "", 1)

		_, err := db.Exec(context.Background(), "UPDATE users SET role=$1 WHERE telegram_id=$2", role, uid)
		if err != nil {
			return err
		}

		b.AnswerCallbackQuery(cb.Id, &gotgbot.AnswerCallbackQueryOpts{Text: "✅ Роль сохранена!"})
		b.DeleteMessage(ctx.EffectiveChat.Id, cb.Message.GetMessageId(), nil)

		if role == "passenger" {
			opts := &gotgbot.SendMessageOpts{
				ReplyMarkup: gotgbot.ReplyKeyboardMarkup{
					ResizeKeyboard: true,
					Keyboard: [][]gotgbot.KeyboardButton{
						{{Text: "🗺 Заказать такси", WebApp: &gotgbot.WebAppInfo{URL: miniAppURL + "/index.html"}}},
					},
				},
			}
			_, err = b.SendMessage(uid, "📱 Меню пассажира. Нажмите кнопку ниже для вызова машины:", opts)
			return err
		} else {
			opts := &gotgbot.SendMessageOpts{
				ReplyMarkup: gotgbot.ReplyKeyboardMarkup{
					ResizeKeyboard: true,
					Keyboard: [][]gotgbot.KeyboardButton{
						{{Text: "🧭 Карта водителя", WebApp: &gotgbot.WebAppInfo{URL: miniAppURL + "/driver_map.html"}}},
						{{Text: "🟢 На линии"}, {Text: "🔴 Оффлайн"}},
					},
				},
			}
			_, err = b.SendMessage(uid, "🚖 Меню водителя. Выберите статус для получения заказов:", opts)
			return err
		}
	}))

	// ============================================================
	// ХЭНДЛЕР: Изменение статуса водителя (Текст)
	// ============================================================
	dispatcher.AddHandler(handlers.NewMessage(func(msg *gotgbot.Message) bool {
		return msg.Text == "🟢 На линии" || msg.Text == "🔴 Оффлайн"
	}, func(b *gotgbot.Bot, ctx *ext.Context) error {
		uid := ctx.EffectiveSender.Id()
		text := ctx.EffectiveMessage.Text
		isOnline := text == "🟢 На линии"

		_, err := db.Exec(context.Background(), "UPDATE users SET is_online=$1 WHERE telegram_id=$2 AND role='driver'", isOnline, uid)
		if err != nil {
			return err
		}

		statusText := "🔴 Вы ушли в оффлайн."
		if isOnline {
			statusText = "🟢 Вы <b>НА ЛИНИИ</b> и получаете заказы."
		}
		_, err = b.SendMessage(ctx.EffectiveChat.Id, statusText, &gotgbot.SendMessageOpts{ParseMode: "HTML"})
		return err
	}))

	// ============================================================
	// ХЭНДЛЕР: Принятие заказа водителем (CallbackQuery)
	// ============================================================
	dispatcher.AddHandler(handlers.NewCallback(func(cb *gotgbot.CallbackQuery) bool {
		return strings.HasPrefix(cb.Data, "accept_")
	}, func(b *gotgbot.Bot, ctx *ext.Context) error {
		cb := ctx.Update.CallbackQuery
		orderIDStr := strings.Replace(cb.Data, "accept_", "", 1)
		orderID, _ := strconv.Atoi(orderIDStr)
		driverID := cb.From.Id

		// Транзакция для исключения гонки условий (race conditions)
		tx, err := db.Begin(context.Background())
		if err != nil {
			return err
		}
		defer tx.Rollback(context.Background())

		var status, fromAddr, toAddr string
		var passID int64
		err = tx.QueryRow(context.Background(), "SELECT status, from_address, to_address, passenger_id FROM orders WHERE id=$1 FOR UPDATE", orderID).Scan(&status, &fromAddr, &toAddr, &passID)
		if err != nil || status != "searching" {
			b.AnswerCallbackQuery(cb.Id, &gotgbot.AnswerCallbackQueryOpts{Text: "❌ Заказ уже взят или отменен."})
			return nil
		}

		var isBusy bool
		err = tx.QueryRow(context.Background(), "SELECT is_busy FROM users WHERE telegram_id=$1 FOR UPDATE", driverID).Scan(&isBusy)
		if err != nil || isBusy {
			b.AnswerCallbackQuery(cb.Id, &gotgbot.AnswerCallbackQueryOpts{Text: "❌ У вас уже есть активный заказ."})
			return nil
		}

		// Обновляем статусы
		_, _ = tx.Exec(context.Background(), "UPDATE orders SET status='accepted', driver_id=$1, updated_at=NOW() WHERE id=$2", driverID, orderID)
		_, _ = tx.Exec(context.Background(), "UPDATE users SET is_busy=TRUE WHERE telegram_id=$1", driverID)

		if err := tx.Commit(context.Background()); err != nil {
			return err
		}

		b.AnswerCallbackQuery(cb.Id, &gotgbot.AnswerCallbackQueryOpts{Text: "✅ Заказ принят!"})
		b.DeleteMessage(ctx.EffectiveChat.Id, cb.Message.GetMessageId(), nil)

		// Кнопка финиша для водителя
		opts := &gotgbot.SendMessageOpts{
			ParseMode: "HTML",
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{
				InlineKeyboard: [][]gotgbot.InlineKeyboardButton{
					{{Text: "🏁 Завершить поездку", CallbackData: "complete_" + orderIDStr}},
				},
			},
		}
		b.SendMessage(driverID, fmt.Sprintf("🚖 <b>Заказ #%d</b> принят.\n\n📍 Маршрут: %s → %s", orderID, fromAddr, toAddr), opts)
		
		// Сообщение пассажиру
		b.SendMessage(passID, fmt.Sprintf("✅ Водитель найден! Ваш заказ <b>#%d</b> выполняется.", orderID), &gotgbot.SendMessageOpts{ParseMode: "HTML"})
		return nil
	}))

	// ============================================================
	// ХЭНДЛЕР: Завершение поездки (CallbackQuery)
	// ============================================================
	dispatcher.AddHandler(handlers.NewCallback(func(cb *gotgbot.CallbackQuery) bool {
		return strings.HasPrefix(cb.Data, "complete_")
	}, func(b *gotgbot.Bot, ctx *ext.Context) error {
		cb := ctx.Update.CallbackQuery
		orderIDStr := strings.Replace(cb.Data, "complete_", "", 1)
		orderID, _ := strconv.Atoi(orderIDStr)
		driverID := cb.From.Id

		var status string
		var price float64
		var passID int64
		err := db.QueryRow(context.Background(), "SELECT status, price, passenger_id FROM orders WHERE id=$1 AND driver_id=$2", orderID, driverID).Scan(&status, &price, &passID)

		if err == nil && status == "accepted" {
			_, _ = db.Exec(context.Background(), "UPDATE orders SET status='completed', updated_at=NOW() WHERE id=$1", orderID)
			_, _ = db.Exec(context.Background(), "UPDATE users SET is_busy=FALSE, balance=balance+$1 WHERE telegram_id=$2", price, driverID)

			b.AnswerCallbackQuery(cb.Id, &gotgbot.AnswerCallbackQueryOpts{Text: "✅ Поездка завершена!"})
			b.DeleteMessage(ctx.EffectiveChat.Id, cb.Message.GetMessageId(), nil)

			b.SendMessage(driverID, fmt.Sprintf("💵 Заказ <b>#%d</b> закрыт.\nНа баланс зачислено: <b>%.2f GEL</b>", orderID, price), &gotgbot.SendMessageOpts{ParseMode: "HTML"})
			b.SendMessage(passID, fmt.Sprintf("✨ Спасибо за поездку! Заказ <b>#%d</b> успешно завершен.", orderID), &gotgbot.SendMessageOpts{ParseMode: "HTML"})
		}
		return nil
	}))

	// ============================================================
	// ХЭНДЛЕР: Приём данных из Web App (Mini Apps)
	// ============================================================
	dispatcher.AddHandler(handlers.NewWebAppData(func(b *gotgbot.Bot, ctx *ext.Context) error {
		uid := ctx.EffectiveSender.Id()
		rawData := ctx.EffectiveMessage.WebAppData.Data

		var payload WebAppData
		if err := json.Unmarshal([]byte(rawData), &payload); err != nil {
			log.Printf("Ошибка JSON WebApp: %v", err)
			return err
		}

		if payload.Action == "select_points" {
			fromLat, _ := strconv.ParseFloat(payload.FromLat, 64)
			fromLon, _ := strconv.ParseFloat(payload.FromLon, 64)
			toLat, _ := strconv.ParseFloat(payload.ToLat, 64)
			toLon, _ := strconv.ParseFloat(payload.ToLon, 64)
			distance, _ := strconv.ParseFloat(payload.Distance, 64)
			price, _ := strconv.ParseFloat(payload.Price, 64)

			var orderID int
			err := db.QueryRow(context.Background(), `
				INSERT INTO orders(passenger_id, from_address, to_address, from_lat, from_lon, to_lat, to_lon, distance, price)
				VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
			`, uid, payload.FromAddress, payload.ToAddress, fromLat, fromLon, toLat, toLon, distance, price).Scan(&orderID)

			if err != nil {
				log.Printf("Ошибка создания заказа в базе: %v", err)
				return err
			}

			b.SendMessage(uid, fmt.Sprintf("🔍 Ищем водителя для заказа <b>#%d</b>...\n💰 Стоимость: <b>%.2f GEL</b>", orderID, price), &gotgbot.SendMessageOpts{ParseMode: "HTML"})

			// Поиск свободных водителей на линии
			rows, _ := db.Query(context.Background(), "SELECT telegram_id, lat, lon FROM users WHERE role='driver' AND is_online=TRUE AND is_busy=FALSE")
			defer rows.Close()

			for rows.Next() {
				var dID int64
				var dLat, dLon float64
				if err := rows.Scan(&dID, &dLat, &dLon); err == nil {
					if dLat != 0 && dLon != 0 {
						dist := calcDist(fromLat, fromLon, dLat, dLon)
						if dist <= 7.0 { // Радиус рассылки — 7 км
							opts := &gotgbot.SendMessageOpts{
								ParseMode: "HTML",
								ReplyMarkup: gotgbot.InlineKeyboardMarkup{
									InlineKeyboard: [][]gotgbot.InlineKeyboardButton{
										{{Text: "✅ Принять", CallbackData: fmt.Sprintf("accept_%d", orderID)}},
									},
								},
							}
							msg := fmt.Sprintf("🔔 <b>НОВЫЙ ЗАКАЗ #%d</b>\n\n📍 Откуда: %s\n🏁 Куда: %s\n📏 До вас: %.1f км\n💰 Стоимость: <b>%.2f GEL</b>", 
								orderID, payload.FromAddress, payload.ToAddress, dist, price)
							
							b.SendMessage(dID, msg, opts)
						}
					}
				}
			}
		} else if payload.Action == "driver_location" {
			lat, _ := strconv.ParseFloat(payload.Lat, 64)
			lng, _ := strconv.ParseFloat(payload.Lng, 64)
			_, _ = db.Exec(context.Background(), "UPDATE users SET lat=$1, lon=$2 WHERE telegram_id=$3", lat, lng, uid)
		}
		return nil
	}))

	// Поллинг обновлений с очисткой старых (DropPendingUpdates)
	log.Printf("📡 Бот запущен на gotgbot... MiniApp URL: %s", miniAppURL)
	err = updater.StartPolling(b, &ext.PollingOpts{
		DropPendingUpdates: true,
		UnhandledErrFunc: func(err error) {
			log.Printf("Внутренняя ошибка пула: %v", err)
		},
	})
	if err != nil {
		log.Fatalf("Ошибка поллинга: %v", err)
	}

	updater.Idle()
}
