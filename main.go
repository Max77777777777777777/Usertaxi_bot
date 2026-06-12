package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"strconv"
	"strings"
	"time"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	db         *pgxpool.Pool
	miniAppURL string
)

// WebAppData — структура для парсинга данных из Mini App
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

// calcDist вычисляет расстояние (Формула гаверсинуса)
func calcDist(lat1, lon1, lat2, lon2 float64) float64 {
	rad := math.Pi / 180
	dlat := (lat2 - lat1) * rad
	dlon := (lon2 - lon1) * rad
	a := math.Sin(dlat/2)*math.Sin(dlat/2) + math.Cos(lat1*rad)*math.Cos(lat2*rad)*math.Sin(dlon/2)*math.Sin(dlon/2)
	return 6371.0 * 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
}

// initDB создает пулинг и таблицы в базе
func initDB(ctx context.Context, dsn string) {
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		log.Fatalf("Ошибка DSN: %v", err)
	}
	config.MaxConns = 10

	db, err = pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		log.Fatalf("Ошибка подключения к БД: %v", err)
	}

	queries := []string{
		`CREATE TABLE IF NOT EXISTS users(
			telegram_id BIGINT PRIMARY KEY,
			role TEXT,
			is_online BOOL DEFAULT FALSE,
			is_busy BOOL DEFAULT FALSE,
			balance REAL DEFAULT 0.0,
			lat REAL,
			lon REAL
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
			status TEXT DEFAULT 'searching'
		)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(ctx, q); err != nil {
			log.Fatalf("Ошибка создания таблиц: %v", err)
		}
	}
	log.Println("✅ База данных готова")
}

func main() {
	// === Fail-Fast проверка переменных ===
	token := os.Getenv("TAXI_BOT_TOKEN")
	dsn := os.Getenv("PG_DSN")
	miniAppURL = os.Getenv("MINI_APP_BASE_URL")
	yandexGeoKey := os.Getenv("YANDEX_GEOCODER_KEY")

	if token == "" || dsn == "" || miniAppURL == "" || yandexGeoKey == "" {
		log.Fatal("❌ Ключи TAXI_BOT_TOKEN, PG_DSN, MINI_APP_BASE_URL или YANDEX_GEOCODER_KEY не заданы в панели!")
	}

	ctx := context.Background()
	initDB(ctx, dsn)
	defer db.Close()

	bot, err := tgbotapi.NewBotAPI(token)
	if err != nil {
		log.Fatalf("Ошибка запуска бота: %v", err)
	}

	log.Printf("📡 Бот запущен под аккаунтом %s", bot.Self.UserName)

	u := tgbotapi.NewUpdate(0)
	u.Timeout = 60
	updates := bot.GetUpdatesChan(u)

	// === Главный цикл обработки ===
	for update := range updates {
		
		// 1. ОБРАБОТКА ИНЛАЙН КНОПОК (CallbackQuery)
		if update.CallbackQuery != nil {
			cb := update.CallbackQuery
			uid := cb.From.ID
			data := cb.Data

			// Выбор роли
			if strings.HasPrefix(data, "set_role_") {
				role := strings.Replace(data, "set_role_", "", 1)
				_, _ = db.Exec(ctx, "UPDATE users SET role=$1 WHERE telegram_id=$2", role, uid)

				bot.Send(tgbotapi.NewCallback(cb.ID, "✅ Роль сохранена!"))
				bot.Send(tgbotapi.NewDeleteMessage(cb.Message.Chat.ID, cb.Message.MessageID))

				if role == "passenger" {
					msg := tgbotapi.NewMessage(uid, "📱 Меню пассажира. Нажмите кнопку ниже:")
					msg.ReplyMarkup = tgbotapi.NewReplyKeyboard(
						tgbotapi.NewKeyboardButtonRow(
							tgbotapi.KeyboardButton{Text: "🗺 Заказать такси", WebApp: &tgbotapi.WebAppInfo{URL: miniAppURL + "/index.html"}},
						),
					)
					bot.Send(msg)
				} else {
					msg := tgbotapi.NewMessage(uid, "🚖 Меню водителя. Выберите ваш статус:")
					msg.ReplyMarkup = tgbotapi.NewReplyKeyboard(
						tgbotapi.NewKeyboardButtonRow(
							tgbotapi.KeyboardButton{Text: "🧭 Карта водителя", WebApp: &tgbotapi.WebAppInfo{URL: miniAppURL + "/driver_map.html"}},
						),
						tgbotapi.NewKeyboardButtonRow(
							tgbotapi.NewKeyboardButton("🟢 На линии"),
							tgbotapi.NewKeyboardButton("🔴 Оффлайн"),
						),
					)
					bot.Send(msg)
				}
			}

			// Водитель принял заказ
			if strings.HasPrefix(data, "accept_") {
				orderIDStr := strings.Replace(data, "accept_", "", 1)
				orderID, _ := strconv.Atoi(orderIDStr)

				tx, err := db.Begin(ctx)
				if err == nil {
					var status, fromAddr, toAddr string
					var passID int64
					err = tx.QueryRow(ctx, "SELECT status, from_address, to_address, passenger_id FROM orders WHERE id=$1 FOR UPDATE", orderID).Scan(&status, &fromAddr, &toAddr, &passID)
					
					if err == nil && status == "searching" {
						var isBusy bool
						_ = tx.QueryRow(ctx, "SELECT is_busy FROM users WHERE telegram_id=$1 FOR UPDATE", uid).Scan(&isBusy)
						
						if !isBusy {
							_, _ = tx.Exec(ctx, "UPDATE orders SET status='accepted', driver_id=$1 WHERE id=$2", uid, orderID)
							_, _ = tx.Exec(ctx, "UPDATE users SET is_busy=TRUE WHERE telegram_id=$1", uid)
							_ = tx.Commit(ctx)

							bot.Send(tgbotapi.NewCallback(cb.ID, "✅ Вы приняли заказ!"))
							bot.Send(tgbotapi.NewDeleteMessage(cb.Message.Chat.ID, cb.Message.MessageID))

							// Кнопка финиша
							finishBtn := tgbotapi.NewInlineKeyboardMarkup(
								tgbotapi.NewInlineKeyboardRow(
									tgbotapi.NewInlineKeyboardButtonData("🏁 Завершить поездку", "complete_"+orderIDStr),
								),
							)
							driveMsg := tgbotapi.NewMessage(uid, fmt.Sprintf("🚖 <b>Заказ #%d</b> принят.\n📍 Маршрут: %s → %s", orderID, fromAddr, toAddr))
							driveMsg.ParseMode = "HTML"
							driveMsg.ReplyMarkup = finishBtn
							bot.Send(driveMsg)

							passMsg := tgbotapi.NewMessage(passID, fmt.Sprintf("✅ Водитель найден! Ваш заказ <b>#%d</b> выполняется.", orderID))
							passMsg.ParseMode = "HTML"
							bot.Send(passMsg)
							continue
						}
					}
					tx.Rollback(ctx)
				}
				bot.Send(tgbotapi.NewCallback(cb.ID, "❌ Заказ недоступен."))
			}

			// Завершение заказа
			if strings.HasPrefix(data, "complete_") {
				orderIDStr := strings.Replace(data, "complete_", "", 1)
				orderID, _ := strconv.Atoi(orderIDStr)

				var price float64
				var passID int64
				err := db.QueryRow(ctx, "UPDATE orders SET status='completed' WHERE id=$1 AND driver_id=$2 AND status='accepted' RETURNING price, passenger_id", orderID, uid).Scan(&price, &passID)
				
				if err == nil {
					_, _ = db.Exec(ctx, "UPDATE users SET is_busy=FALSE, balance=balance+$1 WHERE telegram_id=$2", price, uid)
					bot.Send(tgbotapi.NewCallback(cb.ID, "✅ Поездка завершена!"))
					bot.Send(tgbotapi.NewDeleteMessage(cb.Message.Chat.ID, cb.Message.MessageID))

					bot.Send(tgbotapi.NewMessage(uid, fmt.Sprintf("💵 Заказ #%d закрыт. Начислено: %.2f GEL", orderID, price)))
					bot.Send(tgbotapi.NewMessage(passID, fmt.Sprintf("✨ Спасибо за поездку! Заказ #%d завершен.", orderID)))
				}
			}
			continue
		}

		// 2. ОБРАБОТКА ОБЫЧНЫХ ТЕКСТОВЫХ СООБЩЕНИЙ
		if update.Message != nil {
			msg := update.Message
			uid := msg.From.ID

			// Команда /start
			if msg.IsCommand() && msg.Command() == "start" {
				_, _ = db.Exec(ctx, "INSERT INTO users(telegram_id) VALUES($1) ON CONFLICT DO NOTHING", uid)

				startBuf := tgbotapi.NewMessage(msg.Chat.ID, "👋 Добро пожаловать в <b>UserTaxi</b>!\nВыберите роль:")
				startBuf.ParseMode = "HTML"
				startBuf.ReplyMarkup = tgbotapi.NewInlineKeyboardMarkup(
					tgbotapi.NewInlineKeyboardRow(
						tgbotapi.NewInlineKeyboardButtonData("🚕 Пассажир", "set_role_passenger"),
						tgbotapi.NewInlineKeyboardButtonData("🚘 Водитель", "set_role_driver"),
					),
				)
				bot.Send(startBuf)
				continue
			}

			// Статусы водителя на линии/оффлайн
			if msg.Text == "🟢 На линии" || msg.Text == "🔴 Оффлайн" {
				isOnline := msg.Text == "🟢 На линии"
				_, _ = db.Exec(ctx, "UPDATE users SET is_online=$1 WHERE telegram_id=$2", isOnline, uid)

				resText := "🔴 Вы ушли в оффлайн."
				if isOnline {
					resText = "🟢 Вы <b>НА ЛИНИИ</b> и принимаете заказы."
				}
				resMsg := tgbotapi.NewMessage(msg.Chat.ID, resText)
				resMsg.ParseMode = "HTML"
				bot.Send(resMsg)
				continue
			}

			// Данные из Web App (Mini Apps)
			if msg.WebAppData != nil {
				var payload WebAppData
				if err := json.Unmarshal([]byte(msg.WebAppData.Data), &payload); err == nil {
					
					if payload.Action == "select_points" {
						fromLat, _ := strconv.ParseFloat(payload.FromLat, 64)
						fromLon, _ := strconv.ParseFloat(payload.FromLon, 64)
						distance, _ := strconv.ParseFloat(payload.Distance, 64)
						price, _ := strconv.ParseFloat(payload.Price, 64)

						var orderID int
						err := db.QueryRow(ctx, `
							INSERT INTO orders(passenger_id, from_address, to_address, from_lat, from_lon, distance, price)
							VALUES($1, $2, $3, $4, $5, $6, $7) RETURNING id
						`, uid, payload.FromAddress, payload.ToAddress, fromLat, fromLon, distance, price).Scan(&orderID)

						if err == nil {
							bot.Send(tgbotapi.NewMessage(uid, fmt.Sprintf("🔍 Ищем водителя для заказа #%d...\n💰 Стоимость: %.2f GEL", orderID, price)))

							// Рассылка водителям в радиусе 7км
							rows, _ := db.Query(ctx, "SELECT telegram_id, lat, lon FROM users WHERE is_online=TRUE AND is_busy=FALSE")
							for rows.Next() {
								var dID int64
								var dLat, dLon float64
								if err := rows.Scan(&dID, &dLat, &dLon); err == nil && dLat != 0 {
									if calcDist(fromLat, fromLon, dLat, dLon) <= 7.0 {
										drvMsg := tgbotapi.NewMessage(dID, fmt.Sprintf("🔔 <b>НОВЫЙ ЗАКАЗ #%d</b>\n\n📍 Откуда: %s\n🏁 Куда: %s\n💰 Стоимость: %.2f GEL", orderID, payload.FromAddress, payload.ToAddress, price))
										drvMsg.ParseMode = "HTML"
										drvMsg.ReplyMarkup = tgbotapi.NewInlineKeyboardMarkup(
											tgbotapi.NewInlineKeyboardRow(
												tgbotapi.NewInlineKeyboardButtonData("✅ Принять", fmt.Sprintf("accept_%d", orderID)),
											),
										)
										bot.Send(drvMsg)
									}
								}
							}
							rows.Close()
						}
					} else if payload.Action == "driver_location" {
						lat, _ := strconv.ParseFloat(payload.Lat, 64)
						lng, _ := strconv.ParseFloat(payload.Lng, 64)
						_, _ = db.Exec(ctx, "UPDATE users SET lat=$1, lon=$2 WHERE telegram_id=$3", lat, lng, uid)
					}
				}
				continue
			}
		}
	}
}
