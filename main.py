import os
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import swisseph as swe
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder
from dotenv import load_dotenv
from openai import OpenAI

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/")

FULL_REPORT_PRICE = os.getenv("FULL_REPORT_PRICE_RUB", "990.00")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

# Payment is optional at startup. If YooKassa credentials are absent,
# the bot can still be run in free/demo mode.
YOOKASSA_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

DATE, TIME, PLACE = range(3)

MENU = ReplyKeyboardMarkup(
    [
        ["🌌 Полный разбор", "🔭 Натальная карта"],
        ["🔢 Матрица судьбы", "☀️ Соляр"],
        ["💳 Оплатить полный разбор", "👤 Мои данные"],
    ],
    resize_keyboard=True,
)

PAYMENT_BUTTONS = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton(
            f"💳 Оплатить {FULL_REPORT_PRICE} ₽ через ЮKassa",
            callback_data="create_payment"
        )],
        [InlineKeyboardButton(
            "✅ Я оплатил — проверить",
            callback_data="check_payment"
        )],
    ]
)

# ============================================================
# ASTROLOGY
# ============================================================

PLANETS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mercury": swe.MERCURY,
    "Venus": swe.VENUS,
    "Mars": swe.MARS,
    "Jupiter": swe.JUPITER,
    "Saturn": swe.SATURN,
    "Uranus": swe.URANUS,
    "Neptune": swe.NEPTUNE,
    "Pluto": swe.PLUTO,
    "True Node": swe.TRUE_NODE,
}

SIGNS = [
    "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
]


def sign_degree(lon: float):
    idx = int(lon // 30) % 12
    deg = lon % 30
    return SIGNS[idx], round(deg, 2)


def geocode(place: str):
    geolocator = Nominatim(
        user_agent="astro-telegram-bot/2.0",
        timeout=15,
    )
    loc = geolocator.geocode(
        place,
        language="ru",
        exactly_one=True,
        timeout=15,
    )
    if not loc:
        raise ValueError("Место рождения не найдено")

    lat = float(loc.latitude)
    lon = float(loc.longitude)

    tz_name = TimezoneFinder().timezone_at(lat=lat, lng=lon)
    if not tz_name:
        raise ValueError("Не удалось определить часовой пояс")

    return {
        "query": place,
        "resolved": loc.address,
        "lat": lat,
        "lon": lon,
        "timezone": tz_name,
    }


def julian_day(dt_utc: datetime):
    return swe.julday(
        dt_utc.year,
        dt_utc.month,
        dt_utc.day,
        dt_utc.hour
        + dt_utc.minute / 60
        + dt_utc.second / 3600,
    )


def aspect_angle(a, b):
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def natal_chart(dt_local: datetime, location: dict):
    dt_utc = dt_local.astimezone(timezone.utc)
    jd = julian_day(dt_utc)

    planets = {}
    for name, code in PLANETS.items():
        xx, _ = swe.calc_ut(jd, code)
        lon_deg = float(xx[0])
        sign, degree = sign_degree(lon_deg)

        planets[name] = {
            "longitude": round(lon_deg, 4),
            "sign": sign,
            "degree": degree,
            "retrograde": bool(xx[3] < 0),
        }

    cusps, ascmc = swe.houses_ex(
        jd,
        location["lat"],
        location["lon"],
        b"P",
    )

    houses = [round(float(x), 4) for x in cusps]
    asc = float(ascmc[0])
    mc = float(ascmc[1])

    aspects = []
    major = [
        (0, "соединение", 8),
        (60, "секстиль", 5),
        (90, "квадрат", 6),
        (120, "тригон", 6),
        (180, "оппозиция", 8),
    ]

    names = list(planets)

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = planets[names[i]]["longitude"]
            b = planets[names[j]]["longitude"]
            diff = aspect_angle(a, b)

            for target, label, orb in major:
                if abs(diff - target) <= orb:
                    aspects.append({
                        "a": names[i],
                        "b": names[j],
                        "aspect": label,
                        "orb": round(abs(diff - target), 2),
                    })
                    break

    asc_sign, asc_deg = sign_degree(asc)
    mc_sign, mc_deg = sign_degree(mc)

    return {
        "planets": planets,
        "houses_cusps": houses,
        "ascendant": {
            "sign": asc_sign,
            "degree": asc_deg,
            "longitude": round(asc, 4),
        },
        "mc": {
            "sign": mc_sign,
            "degree": mc_deg,
            "longitude": round(mc, 4),
        },
        "aspects": aspects,
    }


def reduce_22(n: int) -> int:
    n = abs(int(n))
    while n > 22:
        n = sum(int(x) for x in str(n))
    return 22 if n == 0 else n


def calculate_matrix(date_str: str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    day, month, year = d.day, d.month, d.year

    digits = [int(x) for x in f"{day:02d}{month:02d}{year:04d}"]

    a = reduce_22(day)
    b = reduce_22(month)
    c = reduce_22(sum(int(x) for x in str(year)))
    d4 = reduce_22(a + b + c)
    e = reduce_22(a + b)
    f = reduce_22(b + c)
    g = reduce_22(c + a)

    return {
        "method": "22-арканная базовая схема",
        "positions": {
            "день": a,
            "месяц": b,
            "год": c,
            "итог": d4,
            "день+месяц": e,
            "месяц+год": f,
            "год+день": g,
        },
        "raw_digits": digits,
        "note": (
            "Формулы школ Матрицы судьбы отличаются; "
            "при наличии конкретной методики замените этот расчёт."
        ),
    }


def solar_return(dt_local: datetime, natal_sun_lon: float):
    year = dt_local.year

    approx = datetime(
        year,
        dt_local.month,
        dt_local.day,
        12,
        tzinfo=dt_local.tzinfo,
    )

    start = approx - timedelta(days=2)
    best = None
    best_err = 999

    for minutes in range(0, 5 * 24 * 60, 10):
        cand = start + timedelta(minutes=minutes)
        jd = julian_day(cand.astimezone(timezone.utc))
        sun_lon = float(swe.calc_ut(jd, swe.SUN)[0][0])
        err = aspect_angle(sun_lon, natal_sun_lon)

        if err < best_err:
            best_err = err
            best = cand

    return {
        "approx_local": best.isoformat(),
        "sun_longitude_target": round(natal_sun_lon, 4),
        "residual_error_degrees": round(best_err, 4),
        "note": (
            "Для максимально точного продакшен-соляра стоит "
            "заменить 10-минутный поиск численной оптимизацией."
        ),
    }


def build_report(date_str: str, time_str: str, place: str):
    location = geocode(place)

    naive = datetime.strptime(
        f"{date_str} {time_str}",
        "%Y-%m-%d %H:%M",
    )

    local_tz = ZoneInfo(location["timezone"])
    local_dt = naive.replace(tzinfo=local_tz)

    natal = natal_chart(local_dt, location)
    matrix = calculate_matrix(date_str)
    solar = solar_return(
        local_dt,
        natal["planets"]["Sun"]["longitude"],
    )

    return {
        "birth": {
            "date": date_str,
            "time": time_str,
            "place": place,
            "resolved_place": location["resolved"],
            "timezone": location["timezone"],
            "latitude": location["lat"],
            "longitude": location["lon"],
        },
        "natal_chart": natal,
        "destiny_matrix": matrix,
        "solar_return": solar,
    }


# ============================================================
# AI REPORT
# ============================================================

def generate_report(astrology_data: dict, task: str) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    system = """
Ты — редактор персональных астрологических разборов.
Используй только расчётные данные, переданные пользователем.
Не выдумывай положения планет.

Пиши на русском языке, живо, тепло и конкретно.
Разделяй фактическую часть и интерпретацию.
Не выдавай астрологию за научно доказанный метод.
Не давай медицинских, юридических или финансовых диагнозов.

Структура:
1. Краткое резюме.
2. Ключевые положения.
3. Подробные тематические разделы.
4. Сильные стороны.
5. Зоны внимания.
6. Практические вопросы для саморефлексии.

Если время рождения неточное — предупреди,
что дома и асцендент могут измениться.
"""

    user = (
        task
        + "\n\nРАСЧЁТНЫЕ ДАННЫЕ:\n"
        + json.dumps(
            astrology_data,
            ensure_ascii=False,
            indent=2,
        )
    )

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=system,
        input=user,
    )

    return response.output_text


# ============================================================
# YOOKASSA
# ============================================================

def yookassa_headers():
    return {
        "Content-Type": "application/json",
        "Idempotence-Key": str(uuid.uuid4()),
    }


def create_yookassa_payment(user_id: int):
    if not YOOKASSA_ENABLED:
        raise RuntimeError(
            "ЮKassa не настроена. Укажите "
            "YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY."
        )

    payload = {
        "amount": {
            "value": FULL_REPORT_PRICE,
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": "Полный персональный астрологический разбор",
        "metadata": {
            "telegram_user_id": str(user_id),
            "product": "full_report",
        },
    }

    response = requests.post(
        "https://api.yookassa.ru/v3/payments",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        headers=yookassa_headers(),
        json=payload,
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"ЮKassa HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    data = response.json()

    confirmation = data.get("confirmation", {})
    confirmation_url = confirmation.get("confirmation_url")

    if not confirmation_url:
        raise RuntimeError(
            "ЮKassa не вернула confirmation_url"
        )

    return {
        "id": data["id"],
        "status": data.get("status"),
        "paid": data.get("paid", False),
        "confirmation_url": confirmation_url,
    }


def get_yookassa_payment(payment_id: str):
    if not YOOKASSA_ENABLED:
        raise RuntimeError("ЮKassa не настроена")

    response = requests.get(
        f"https://api.yookassa.ru/v3/payments/{payment_id}",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"ЮKassa HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    data = response.json()

    return {
        "id": data["id"],
        "status": data.get("status"),
        "paid": data.get("paid", False),
    }


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    payment_state = (
        "Оплата через ЮKassa подключена."
        if YOOKASSA_ENABLED
        else "Режим оплаты пока не настроен."
    )

    await update.message.reply_text(
        "✨ Добро пожаловать в AstroGuide.\n\n"
        "Я могу собрать натальную карту, Матрицу судьбы "
        "и соляр, а затем сделать большой персональный разбор.\n\n"
        f"💳 {payment_state}\n\n"
        "Нажми «🌌 Полный разбор», чтобы начать.",
        reply_markup=MENU,
    )

    return ConversationHandler.END


async def begin_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "full"

    await update.message.reply_text(
        "Напиши дату рождения в формате ДД.ММ.ГГГГ"
    )

    return DATE


async def begin_natal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "natal"

    await update.message.reply_text(
        "Напиши дату рождения в формате ДД.ММ.ГГГГ"
    )

    return DATE


async def begin_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "matrix"

    await update.message.reply_text(
        "Напиши дату рождения в формате ДД.ММ.ГГГГ"
    )

    return DATE


async def begin_solar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "solar"

    await update.message.reply_text(
        "Напиши дату рождения в формате ДД.ММ.ГГГГ"
    )

    return DATE


async def date_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        dt = datetime.strptime(
            update.message.text.strip(),
            "%d.%m.%Y",
        )

        context.user_data["birth_date"] = dt.strftime(
            "%Y-%m-%d"
        )

    except ValueError:
        await update.message.reply_text(
            "Не понял дату. Пример: 24.07.1998"
        )
        return DATE

    await update.message.reply_text(
        "Теперь время рождения, например 14:35"
    )

    return TIME


async def time_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        datetime.strptime(
            update.message.text.strip(),
            "%H:%M",
        )

        context.user_data["birth_time"] = (
            update.message.text.strip()
        )

    except ValueError:
        await update.message.reply_text(
            "Не понял время. Пример: 14:35"
        )
        return TIME

    await update.message.reply_text(
        "И последнее — место рождения.\n"
        "Напиши город и страну, например: Москва, Россия"
    )

    return PLACE


async def place_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data["birth_place"] = (
        update.message.text.strip()
    )

    mode = context.user_data.get("mode", "full")

    try:
        data = await asyncio.to_thread(
            build_report,
            context.user_data["birth_date"],
            context.user_data["birth_time"],
            context.user_data["birth_place"],
        )

        context.user_data["pending_report"] = data

    except Exception as e:
        await update.message.reply_text(
            "Не получилось построить расчёт.\n"
            "Проверь дату, время и город.\n\n"
            f"Техническая причина: {type(e).__name__}",
            reply_markup=MENU,
        )
        return ConversationHandler.END

    if mode == "full":
        if not YOOKASSA_ENABLED:
            await update.message.reply_text(
                "Расчёт готов, но оплата через ЮKassa "
                "ещё не настроена.\n\n"
                "Добавь YOOKASSA_SHOP_ID и "
                "YOOKASSA_SECRET_KEY в .env.",
                reply_markup=MENU,
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "🔮 Расчёт готов.\n\n"
            f"Полный разбор стоит {FULL_REPORT_PRICE} ₽.\n"
            "После успешной оплаты я автоматически смогу "
            "сгенерировать полный текст.",
            reply_markup=PAYMENT_BUTTONS,
        )

        return ConversationHandler.END

    # Бесплатные/отдельные режимы
    await generate_and_send_report(
        update,
        context,
        data,
        mode,
    )

    return ConversationHandler.END


async def generate_and_send_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict,
    mode: str,
):
    prompts = {
        "full": (
            "Сделай максимально полный, структурированный "
            "персональный разбор: натальная карта + "
            "матрица судьбы + соляр. "
            "Пиши понятно, тепло и конкретно."
        ),
        "natal": (
            "Сделай подробный разбор именно натальной карты: "
            "личность, эмоции, отношения, карьера, деньги, "
            "сильные стороны, сложности, дома и аспекты."
        ),
        "matrix": (
            "Сделай подробный разбор именно Матрицы судьбы: "
            "ключевые арканы, таланты, отношения, деньги, "
            "уроки и практические рекомендации."
        ),
        "solar": (
            "Сделай подробный разбор соляра: "
            "главные темы года, отношения, работа, деньги, "
            "дом/семья, ресурсы и важные периоды."
        ),
    }

    await update.effective_message.reply_text(
        "✨ Готовлю персональный разбор…"
    )

    try:
        text = await asyncio.to_thread(
            generate_report,
            data,
            prompts[mode],
        )

        for i in range(0, len(text), 3800):
            await update.effective_message.reply_text(
                text[i:i + 3800],
                reply_markup=MENU if i + 3800 >= len(text) else None,
            )

        context.user_data["last_report"] = data

    except Exception as e:
        await update.effective_message.reply_text(
            "Не удалось сгенерировать текст разбора.\n"
            f"Техническая причина: {type(e).__name__}",
            reply_markup=MENU,
        )


async def create_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    data = context.user_data.get("pending_report")

    if not data:
        await query.message.reply_text(
            "Сначала введи дату, время и место рождения.",
            reply_markup=MENU,
        )
        return

    try:
        payment = await asyncio.to_thread(
            create_yookassa_payment,
            update.effective_user.id,
        )

        context.user_data["payment_id"] = payment["id"]

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 Перейти к оплате",
                        url=payment["confirmation_url"],
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✅ Проверить оплату",
                        callback_data="check_payment",
                    )
                ],
            ]
        )

        await query.message.reply_text(
            "💳 Счёт создан.\n\n"
            f"Сумма: {FULL_REPORT_PRICE} ₽\n"
            "Нажми кнопку ниже, оплати на странице ЮKassa, "
            "а затем нажми «Проверить оплату».",
            reply_markup=keyboard,
        )

    except Exception as e:
        await query.message.reply_text(
            "Не удалось создать платёж через ЮKassa.\n"
            f"Техническая причина: {type(e).__name__}",
            reply_markup=MENU,
        )


async def check_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    payment_id = context.user_data.get("payment_id")
    data = context.user_data.get("pending_report")

    if not payment_id or not data:
        await query.message.reply_text(
            "Активный платёж не найден. "
            "Начни с «🌌 Полный разбор».",
            reply_markup=MENU,
        )
        return

    try:
        payment = await asyncio.to_thread(
            get_yookassa_payment,
            payment_id,
        )

        if payment["paid"] or payment["status"] == "succeeded":
            context.user_data["payment_confirmed"] = True

            await query.message.reply_text(
                "✅ Оплата подтверждена!\n\n"
                "Сейчас начинаю готовить полный персональный разбор."
            )

            await generate_and_send_report(
                update,
                context,
                data,
                "full",
            )

            context.user_data.pop("pending_report", None)
            context.user_data.pop("payment_id", None)

        elif payment["status"] == "canceled":
            await query.message.reply_text(
                "❌ Платёж отменён или истёк.\n"
                "Можно создать новый платёж.",
                reply_markup=PAYMENT_BUTTONS,
            )

        else:
            await query.message.reply_text(
                "⏳ Платёж пока не подтверждён.\n"
                "Если ты уже оплатил, подожди несколько секунд "
                "и нажми «Проверить оплату» ещё раз.",
                reply_markup=PAYMENT_BUTTONS,
            )

    except Exception as e:
        await query.message.reply_text(
            "Не удалось проверить платёж.\n"
            f"Техническая причина: {type(e).__name__}",
            reply_markup=MENU,
        )


async def pay_full_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not YOOKASSA_ENABLED:
        await update.message.reply_text(
            "ЮKassa пока не настроена.\n\n"
            "Добавь в .env:\n"
            "YOOKASSA_SHOP_ID=...\n"
            "YOOKASSA_SECRET_KEY=...\n"
            "YOOKASSA_RETURN_URL=https://..."
        )
        return

    if not context.user_data.get("pending_report"):
        await update.message.reply_text(
            "Сначала нажми «🌌 Полный разбор» "
            "и введи дату, время и место рождения.",
            reply_markup=MENU,
        )
        return

    await update.message.reply_text(
        f"💳 Полный разбор — {FULL_REPORT_PRICE} ₽",
        reply_markup=PAYMENT_BUTTONS,
    )


async def my_data(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    data = (
        context.user_data.get("last_report")
        or context.user_data.get("pending_report")
    )

    if not data:
        await update.message.reply_text(
            "Пока данных нет. "
            "Нажми «🌌 Полный разбор».",
            reply_markup=MENU,
        )
        return

    birth = data["birth"]

    await update.message.reply_text(
        "👤 Данные профиля:\n\n"
        f"Дата: {birth['date']}\n"
        f"Время: {birth['time']}\n"
        f"Место: {birth['place']}",
        reply_markup=MENU,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^🌌 Полный разбор$"),
                begin_full,
            ),
            MessageHandler(
                filters.Regex("^🔭 Натальная карта$"),
                begin_natal,
            ),
            MessageHandler(
                filters.Regex("^🔢 Матрица судьбы$"),
                begin_matrix,
            ),
            MessageHandler(
                filters.Regex("^☀️ Соляр$"),
                begin_solar,
            ),
        ],
        states={
            DATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    date_received,
                )
            ],
            TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    time_received,
                )
            ],
            PLACE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    place_received,
                )
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    app.add_handler(
        CallbackQueryHandler(
            create_payment,
            pattern="^create_payment$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            check_payment,
            pattern="^check_payment$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex("^💳 Оплатить полный разбор$"),
            pay_full_report,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex("^👤 Мои данные$"),
            my_data,
        )
    )

    print(
        "AstroGuide started. "
        f"YooKassa: {'ON' if YOOKASSA_ENABLED else 'OFF'}"
    )

    app.run_polling()


if __name__ == "__main__":
    main()
