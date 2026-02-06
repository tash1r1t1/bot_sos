import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")
TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_RADIUS_KM = 3
EXPANDED_RADIUS_KM = 10
WALKING_SPEED_KMH = 5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Location:
    lat: float
    lon: float


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_ready INTEGER DEFAULT 0,
                ready_until TEXT,
                lat REAL,
                lon REAL,
                ready_note TEXT,
                is_banned INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sos_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                lat REAL,
                lon REAL,
                location_text TEXT,
                summary TEXT,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sos_accepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                helper_id INTEGER NOT NULL,
                accepted_at TEXT NOT NULL,
                arrived_at TEXT,
                feedback TEXT,
                media_file_id TEXT
            );
            """
        )


def update_user(user) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
            """,
            (user.id, user.username, user.first_name, user.last_name),
        )


def set_ready(user_id: int, location: Location, ready_until: datetime) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE users
            SET is_ready=1, ready_until=?, lat=?, lon=?
            WHERE user_id=?
            """,
            (ready_until.isoformat(), location.lat, location.lon, user_id),
        )


def clear_ready(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE users
            SET is_ready=0, ready_until=NULL
            WHERE user_id=?
            """,
            (user_id,),
        )


def get_ready_helpers(location: Location | None, radius_km: float) -> list[tuple[int, float]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT user_id, lat, lon, ready_until
            FROM users
            WHERE is_ready=1 AND is_banned=0
            """
        ).fetchall()
    helpers: list[tuple[int, float]] = []
    cutoff = now_utc()
    for user_id, lat, lon, ready_until in rows:
        if ready_until:
            if datetime.fromisoformat(ready_until) < cutoff:
                clear_ready(user_id)
                continue
        if location and lat is not None and lon is not None:
            distance = haversine_km(location, Location(lat, lon))
            if distance <= radius_km:
                helpers.append((user_id, distance))
        else:
            helpers.append((user_id, 0.0))
    return helpers


def create_sos_case(requester_id: int, location: Location | None, text: str | None) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO sos_cases (requester_id, created_at, status, lat, lon, location_text)
            VALUES (?, ?, 'open', ?, ?, ?)
            """,
            (
                requester_id,
                now_utc().isoformat(),
                location.lat if location else None,
                location.lon if location else None,
                text,
            ),
        )
        return cursor.lastrowid


def close_sos_case(case_id: int, summary: str | None = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE sos_cases
            SET status='closed', closed_at=?, summary=?
            WHERE id=?
            """,
            (now_utc().isoformat(), summary, case_id),
        )


def latest_open_case(requester_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id FROM sos_cases
            WHERE requester_id=? AND status='open'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (requester_id,),
        ).fetchone()
    return row[0] if row else None


def accept_case(case_id: int, helper_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO sos_accepts (case_id, helper_id, accepted_at)
            VALUES (?, ?, ?)
            """,
            (case_id, helper_id, now_utc().isoformat()),
        )


def haversine_km(a: Location, b: Location) -> float:
    r = 6371
    dlat = radians(b.lat - a.lat)
    dlon = radians(b.lon - a.lon)
    lat1 = radians(a.lat)
    lat2 = radians(b.lat)
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(h))


def eta_minutes(distance_km: float) -> int:
    if distance_km <= 0:
        return 0
    hours = distance_km / WALKING_SPEED_KMH
    return max(1, int(hours * 60))


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🆘 SOS", "🤝 ГОТОВИЙ"],
            ["ℹ️ Мій статус", "✅ Завершити SOS"],
            ["ℹ️ Правила", "⚠️ Скарга"],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    update_user(user)
    await update.message.reply_text(
        "Вітаю! Це SOS Bot. Якщо є реальна загроза життю — телефонуйте 102/103/112.\n"
        "Обирайте дію нижче:",
        reply_markup=main_keyboard(),
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Правила допомоги:\n"
        "1) Бот не заміняє екстрені служби.\n"
        "2) Спершу діалог і безпечні дії згідно із законом.\n"
        "3) Допустимі засоби самозахисту лише в межах законодавства.\n"
        "4) Після кейсу потрібен фідбек та, за можливості, медіа.\n",
        reply_markup=main_keyboard(),
    )


async def sos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = "sos"
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Надіслати геолокацію", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Надішліть геолокацію або напишіть адресу/орієнтир одним повідомленням.",
        reply_markup=keyboard,
    )


async def ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = "ready"
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Надіслати геолокацію", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Щоб стати READY, надішліть геолокацію або напишіть адресу/орієнтир.",
        reply_markup=keyboard,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    update_user(user)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT is_ready, ready_until, lat, lon
            FROM users WHERE user_id=?
            """,
            (user.id,),
        ).fetchone()
    if not row:
        await update.message.reply_text("Статус не знайдено.", reply_markup=main_keyboard())
        return
    is_ready, ready_until, lat, lon = row
    if is_ready and ready_until:
        await update.message.reply_text(
            f"READY увімкнено до {ready_until}.\n"
            f"Локація: {lat:.5f}, {lon:.5f}",
            reply_markup=main_keyboard(),
        )
    else:
        await update.message.reply_text("READY вимкнено.", reply_markup=main_keyboard())


async def finish_sos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    case_id = latest_open_case(user.id)
    if not case_id:
        await update.message.reply_text("У вас немає активних SOS.", reply_markup=main_keyboard())
        return
    close_sos_case(case_id)
    await update.message.reply_text(
        f"SOS #{case_id} завершено. Дякуємо!\n"
        "Будь ласка, опишіть результат та, за можливості, надішліть аудіо/відео.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data["flow"] = "feedback"
    context.user_data["case_id"] = case_id


async def complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = "complaint"
    await update.message.reply_text(
        "Опишіть скаргу одним повідомленням і, за можливості, додайте медіа.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    update_user(user)
    flow = context.user_data.get("flow")
    location = Location(update.message.location.latitude, update.message.location.longitude)
    if flow == "ready":
        context.user_data["ready_location"] = location
        await ask_ready_duration(update, context)
        return
    if flow == "sos":
        await handle_sos_creation(update, context, location, None)
        return
    await update.message.reply_text("Локацію отримано.", reply_markup=main_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    update_user(user)
    flow = context.user_data.get("flow")
    text = update.message.text.strip()
    if flow == "ready":
        context.user_data["ready_text"] = text
        await ask_ready_duration(update, context)
        return
    if flow == "sos":
        await handle_sos_creation(update, context, None, text)
        return
    if flow == "feedback":
        case_id = context.user_data.get("case_id")
        if case_id:
            close_sos_case(case_id, summary=text)
        context.user_data.pop("flow", None)
        context.user_data.pop("case_id", None)
        await update.message.reply_text("Фідбек збережено. Дякуємо!", reply_markup=main_keyboard())
        return
    if flow == "complaint":
        context.user_data.pop("flow", None)
        await update.message.reply_text("Скаргу прийнято. Дякуємо.", reply_markup=main_keyboard())
        return
    await update.message.reply_text("Оберіть дію через меню.", reply_markup=main_keyboard())


async def ask_ready_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("30 хв", callback_data="ready_30")],
            [InlineKeyboardButton("1 год", callback_data="ready_60")],
            [InlineKeyboardButton("2 год", callback_data="ready_120")],
            [InlineKeyboardButton("Скасувати", callback_data="ready_cancel")],
        ]
    )
    await update.message.reply_text("На який час увімкнути готовність?", reply_markup=keyboard)


async def on_ready_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "ready_cancel":
        context.user_data.pop("flow", None)
        await query.edit_message_text("READY скасовано.")
        return
    minutes = int(data.split("_")[1])
    location = context.user_data.get("ready_location")
    if not location:
        await query.edit_message_text("Потрібна геолокація для READY.")
        return
    ready_until = now_utc() + timedelta(minutes=minutes)
    set_ready(query.from_user.id, location, ready_until)
    context.user_data.pop("flow", None)
    await query.edit_message_text(
        f"READY увімкнено до {ready_until.isoformat(timespec='minutes')}."
    )


async def handle_sos_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    location: Location | None,
    text: str | None,
) -> None:
    user = update.effective_user
    case_id = create_sos_case(user.id, location, text)
    helpers = get_ready_helpers(location, DEFAULT_RADIUS_KM)
    radius = DEFAULT_RADIUS_KM
    if not helpers:
        helpers = get_ready_helpers(location, EXPANDED_RADIUS_KM)
        radius = EXPANDED_RADIUS_KM
    if helpers:
        for helper_id, distance in helpers:
            eta = eta_minutes(distance)
            await context.bot.send_message(
                chat_id=helper_id,
                text=(
                    f"SOS поруч! Кейс #{case_id}.\n"
                    f"Відстань: {distance:.1f} км (~{eta} хв).\n"
                    "Натисніть, якщо можете допомогти."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Прийняти", callback_data=f"accept_{case_id}")]]
                ),
            )
    await update.message.reply_text(
        f"Кейс SOS #{case_id} створено. Радіус оповіщення: {radius} км.\n"
        "Якщо є загроза життю — телефонуйте 102/103/112.",
        reply_markup=main_keyboard(),
    )
    context.user_data.pop("flow", None)


async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    case_id = int(query.data.split("_")[1])
    accept_case(case_id, query.from_user.id)
    await query.edit_message_text(f"Ви прийняли SOS #{case_id}. Будьте обережні.")


def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    init_db()
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^🆘 SOS$"), sos))
    application.add_handler(MessageHandler(filters.Regex("^🤝 ГОТОВИЙ$"), ready))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ Мій статус$"), status))
    application.add_handler(MessageHandler(filters.Regex("^✅ Завершити SOS$"), finish_sos))
    application.add_handler(MessageHandler(filters.Regex("^⚠️ Скарга$"), complaint))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ Правила$"), rules))
    application.add_handler(CallbackQueryHandler(on_ready_duration, pattern=r"^ready_"))
    application.add_handler(CallbackQueryHandler(on_accept, pattern=r"^accept_"))
    application.add_handler(MessageHandler(filters.LOCATION, on_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return application


def main() -> None:
    app = build_app()
    app.run_polling()


if __name__ == "__main__":
    main()
