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

func calcDist(lat1, lon1, lat2, lon2 float64) float64 {
	rad := math.Pi / 180
	dlat := (lat2 - lat1) * rad
	dlon := (lon2 - lon1) * rad
	a := math.Sin(dlat/2)*math.Sin(dlat/2) + math.Cos(lat1*rad)*math.Cos(lat2*rad)*math.Sin(dlon/2)*math.Sin(dlon/2)
	return 6371.0 * 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
}

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
			status TEXT DEFAULT 'searching',
			created_at TIMESTAMPTZ DEFAULT NOW(),
			updated_at TIMESTAMPTZ DEFAULT NOW()
		)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(ctx, q); err != nil {
			log.Printf("Ошибка при создании таблиц: %v", err)
		}
	}
	log.Println("✅ База данных PostgreSQL инициализирована")
}

func main() {
	token := os.Getenv("TAXI_BOT_TOKEN")
	dsn := os.Getenv("PG_DSN")
	miniAppURL = os.Getenv("MINI_APP_BASE_URL")
	yandexGeoKey = os.Getenv("YANDEX_GEOCODER_KEY")

	if token == "" {
		log.Fatal("❌ TAXI_BOT_TOKEN не задан")
	}

	ctx := context.Background()
	if dsn != "" {
		initDB(ctx, dsn)
		defer db.Close()
	}

	b, err := gotgbot.NewBot(token, nil)
	if err != nil {
		log.Fatalf("Не удалось запустить бота: %v", err)
	}

	updater := ext.NewUpdater(nil)
	dispatcher := updater.Dispatcher

	dispatcher.AddHandler(handlers.NewCommand("start", func(b *gotgbot.Bot, ctx *ext.Context) error {
		uid := ctx.EffectiveUser.Id

		if db != nil {
			_, _ = db.Exec(context.Background(), "INSERT INTO users(telegram_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
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
		_, err := b.SendMessage(ctx.EffectiveChat.Id, "👋 Добро пожаловать в <b>UserTaxi</b>!\nВыберите вашу роль:", opts)
		return err
	}))

	dispatcher.AddHandler(handlers.NewCallbackData("role", func(b *gotgbot.Bot, ctx *ext.Context) error {
		cb := ctx.Update.CallbackQuery
		uid := cb.From.Id
		role := strings.Replace(cb.Data, "set_role_", "", 1)

		if db != nil {
			_, _ = db.Exec(context.Background(), "UPDATE users SET role=$1 WHERE telegram_id=$2", role, uid)
		}

		b.AnswerCallbackQuery(cb.Id, &gotgbot.AnswerCallbackQueryOpts{Text: "✅ Роль сохранена!"})
		b.DeleteMessage(ctx.EffectiveChat.Id, cb.Message.MessageId, nil)

		if role == "passenger" && miniAppURL != "" {
			opts := &gotgbot.SendMessageOpts{
				ReplyMarkup: gotgbot.ReplyKeyboardMarkup{
					ResizeKeyboard: true,
					Keyboard: [][]gotgbot.KeyboardButton{
						{{Text: "🗺 Заказать такси", WebApp: &gotgbot.WebAppInfo{URL: miniAppURL + "/index.html"}}},
					},
				},
			}
			_, err := b.SendMessage(uid, "📱 Меню пассажира:", opts)
			return err
		}
		opts := &gotgbot.SendMessageOpts{
			ReplyMarkup: gotgbot.ReplyKeyboardMarkup{
				ResizeKeyboard: true,
				Keyboard: [][]gotgbot.KeyboardButton{
					{{Text: "🟢 На линии"}, {Text: "🔴 Оффлайн"}},
				},
			},
		}
		_, err := b.SendMessage(uid, "🚖 Меню водителя:", opts)
		return err
	}))

	log.Println("📡 Бот запущен!")
	err = updater.StartPolling(b, &ext.PollingOpts{
		DropPendingUpdates: true,
	})
	if err != nil {
		log.Fatalf("Ошибка поллинга: %v", err)
	}
	updater.Idle()
}
