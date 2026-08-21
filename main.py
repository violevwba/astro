import os
import re
import math
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import swisseph as swe
from timezonefinder import TimezoneFinder

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrovilki")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

llm = AsyncOpenAI(api_key=OPENAI_API_KEY)
tf = TimezoneFinder()

DATE, TIME, CITY = range(3)

DISCLAIMER = (
    "\n\n⚠️ Астрология и «матрица судьбы» — развлекательная/эзотерическая "
    "интерпретация, а не научный метод. Не используй прогнозы как единственное "
    "основание для медицинских, финансовых или юридических решений."
)

PLANETS = [
    ("Солнце", swe.SUN),
    ("Луна", swe.MOON),
    ("Меркурий", swe.MERCURY),
    ("Венера", swe.VENUS),
    ("Марс", swe.MARS),
    ("Юпитер", swe.JUPITER),
    ("Сатурн", swe.SATURN),
    ("Уран", swe.URANUS),
    ("Нептун", swe.NEPTUNE),
    ("Плутон", swe.PLUTO),
    ("Северный узел", swe.TRUE_NODE),
]

SIGNS = [
    "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
]

ASPECTS = [
    ("соединение", 0, 8),
    ("секстиль", 60, 5),
    ("квадрат", 90, 7),
    ("тригон", 120, 7),
    ("оппозиция", 180, 8),
]


def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌌 Натальная карта", callback_data="natal")],
        [InlineKeyboardButton("☀️ Соляр", callback_data="solar")],
        [InlineKeyboardButton("🔢 Матрица судьбы", callback_data="matrix")],
        [InlineKeyboardButton("✨ Полный разбор", callback_data="full")],
        [InlineKeyboardButton("♻️ Изменить данные", callback_data="reset")],
    ])


def valid_date(s):
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def valid_time(s):
    if not re.fullmatch(r"\d{1,2}:\d{2}", s.strip()):
        return None
    try:
        return datetime.strptime(s.strip(), "%H:%M").time()
    except ValueError:
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Привет! 🌙 Я AstroVilki.\n\n"
        "Теперь расчёт натальной карты выполняется локально через Swiss Ephemeris — "
        "без Geocult и без копирования его расшифровок.\n\n"
        "Напиши дату рождения в формате ДД.ММ.ГГГГ."
    )
    return DATE


async def get_date(update, context):
    d = valid_date(update.message.text)
    if not d:
        await update.message.reply_text("Не смогла понять дату. Пример: 14.02.2001")
        return DATE
    context.user_data["date"] = d.strftime("%d.%m.%Y")
    await update.message.reply_text(
        "Теперь напиши точное местное время рождения, например 07:35."
    )
    return TIME


async def get_time(update, context):
    t = valid_time(update.message.text)
    if not t:
        await update.message.reply_text("Нужен формат ЧЧ:ММ. Например: 07:35")
        return TIME
    context.user_data["time"] = t.strftime("%H:%M")
    await update.message.reply_text("Теперь напиши город рождения. Например: Москва")
    return CITY


async def get_city(update, context):
    city = update.message.text.strip()
    if len(city) < 2:
        await update.message.reply_text("Напиши название города.")
        return CITY

    context.user_data["city"] = city
    await update.message.reply_text(
        "Данные сохранены 🌌\n\n"
        f"Дата: {context.user_data['date']}\n"
        f"Время: {context.user_data['time']}\n"
        f"Город: {city}\n\n"
        "Теперь выбери, что построить:",
        reply_markup=menu(),
    )
    return ConversationHandler.END


async def geocode_city(city):
    headers = {
        "User-Agent": "AstroVilkiBot/2.0 (astrology calculator)"
    }
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": city,
                "format": "jsonv2",
                "limit": 1,
                "accept-language": "ru",
            },
        )
        r.raise_for_status()
        data = r.json()

    if not data:
        raise ValueError("Город не найден")

    lat = float(data[0]["lat"])
    lon = float(data[0]["lon"])
    display = data[0].get("display_name", city)

    tz_name = tf.timezone_at(lat=lat, lng=lon)
    if not tz_name:
        raise ValueError("Не удалось определить часовой пояс города")

    return lat, lon, display, tz_name


def normalize_angle(x):
    return x % 360.0


def sign_position(longitude):
    longitude = normalize_angle(longitude)
    sign_index = int(longitude // 30)
    degree = longitude % 30
    return {
        "longitude": round(longitude, 4),
        "sign": SIGNS[sign_index],
        "degree": round(degree, 2),
    }


def angle_distance(a, b):
    diff = abs((a - b) % 360)
    return min(diff, 360 - diff)


def format_degree(deg):
    whole = int(deg)
    minutes = int(round((deg - whole) * 60))
    if minutes == 60:
        whole += 1
        minutes = 0
    return f"{whole}°{minutes:02d}'"


def get_julian_day(date_str, time_str, tz_name):
    local_dt = datetime.strptime(
        f"{date_str} {time_str}", "%d.%m.%Y %H:%M"
    ).replace(tzinfo=ZoneInfo(tz_name))
    utc_dt = local_dt.astimezone(timezone.utc)

    hour_decimal = (
        utc_dt.hour
        + utc_dt.minute / 60
        + utc_dt.second / 3600
    )

    jd = swe.julday(
        utc_dt.year,
        utc_dt.month,
        utc_dt.day,
        hour_decimal,
        swe.GREG_CAL,
    )
    return jd, local_dt, utc_dt


def calculate_chart(date_str, time_str, city):
    lat, lon, display, tz_name = asyncio.run(geocode_city(city)) if False else (None, None, None, None)
    raise RuntimeError("Internal async wrapper should call calculate_chart_async")


async def calculate_chart_async(date_str, time_str, city):
    lat, lon, display, tz_name = await geocode_city(city)
    jd, local_dt, utc_dt = get_julian_day(date_str, time_str, tz_name)

    flags = swe.FLG_SWIEPH | swe.FLG_SPEED

    planets = {}
    for name, planet_id in PLANETS:
        result, _ = swe.calc_ut(jd, planet_id, flags)
        planets[name] = {
            **sign_position(result[0]),
            "retrograde": result[3] < 0,
            "speed": round(result[3], 5),
        }

    cusps, ascmc = swe.houses_ex(
        jd,
        lat,
        lon,
        b"P",
        0,
    )

    houses = [round(float(x), 4) for x in cusps[:12]]
    angles = {
        "Асцендент": round(float(ascmc[0]), 4),
        "MC": round(float(ascmc[1]), 4),
        "Десцендент": normalize_angle(float(ascmc[0]) + 180),
        "IC": normalize_angle(float(ascmc[1]) + 180),
    }

    aspects = []
    names = list(planets.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            p1 = planets[names[i]]["longitude"]
            p2 = planets[names[j]]["longitude"]
            distance = angle_distance(p1, p2)
            for aspect_name, exact, orb in ASPECTS:
                delta = abs(distance - exact)
                if delta <= orb:
                    aspects.append({
                        "body1": names[i],
                        "body2": names[j],
                        "aspect": aspect_name,
                        "orb": round(delta, 2),
                    })
                    break

    for body in planets:
        lon_body = planets[body]["longitude"]
        house_number = 12
        for i in range(12):
            start = houses[i]
            end = houses[(i + 1) % 12]
            if end < start:
                end += 360
            x = lon_body
            if x < start:
                x += 360
            if start <= x < end:
                house_number = i + 1
                break
        planets[body]["house"] = house_number

    chart = {
        "source": "Swiss Ephemeris",
        "birth": {
            "date": date_str,
            "local_time": time_str,
            "timezone": tz_name,
            "utc_time": utc_dt.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "location": {
            "city_input": city,
            "display_name": display,
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
        },
        "angles": {
            key: {
                **sign_position(value),
                "formatted": format_degree(sign_position(value)["degree"]),
            }
            for key, value in angles.items()
        },
        "houses": [
            {
                "house": i + 1,
                **sign_position(houses[i]),
            }
            for i in range(12)
        ],
        "planets": planets,
        "aspects": aspects,
    }

    return chart


def matrix_22(date_str):
    d = datetime.strptime(date_str, "%d.%m.%Y")
    digits = [int(x) for x in d.strftime("%d%m%Y")]
    total = sum(digits)

    def arc(n):
        while n > 22:
            n = sum(int(x) for x in str(n))
        return 22 if n == 0 else n

    day = arc(d.day)
    month = arc(d.month)
    year = arc(sum(int(x) for x in str(d.year)))
    core = arc(day + month + year)

    return {
        "day": day,
        "month": month,
        "year": year,
        "core": core,
        "digits_sum": total,
        "note": (
            "Расчёт выполнен по распространённой 22-арканной схеме; "
            "варианты школ отличаются."
        ),
    }


def chart_text(chart):
    lines = [
        "ИСТОЧНИК: Swiss Ephemeris",
        f"Дата: {chart['birth']['date']}",
        f"Местное время: {chart['birth']['local_time']}",
        f"Часовой пояс: {chart['birth']['timezone']}",
        f"UTC: {chart['birth']['utc_time']}",
        f"Место: {chart['location']['display_name']}",
        f"Координаты: {chart['location']['latitude']}, {chart['location']['longitude']}",
        "",
        "УГЛЫ:",
    ]

    for key, value in chart["angles"].items():
        lines.append(
            f"{key}: {value['sign']} {value['degree']}°"
        )

    lines.append("")
    lines.append("ПЛАНЕТЫ:")

    for name, value in chart["planets"].items():
        retro = " R" if value["retrograde"] else ""
        lines.append(
            f"{name}: {value['sign']} {value['degree']}°; "
            f"дом {value['house']}{retro}"
        )

    lines.append("")
    lines.append("КУСПИДЫ ДОМОВ:")

    for h in chart["houses"]:
        lines.append(
            f"Дом {h['house']}: {h['sign']} {h['degree']}°"
        )

    lines.append("")
    lines.append("ОСНОВНЫЕ АСПЕКТЫ:")

    if chart["aspects"]:
        for a in chart["aspects"]:
            lines.append(
                f"{a['body1']} — {a['aspect']} — {a['body2']} "
                f"(орб {a['orb']}°)"
            )
    else:
        lines.append("Подходящих аспектов по заданным орбам не найдено.")

    return "\n".join(lines)


def approximate_solar_year(jd_birth):
    # The exact return time is found by searching for the moment when
    # the transiting Sun returns to the natal Sun longitude.
    natal_sun = swe.calc_ut(jd_birth, swe.SUN, swe.FLG_SWIEPH)[0][0]
    start = jd_birth + 350
    end = jd_birth + 380

    step = 0.25
    prev_jd = start
    prev_diff = ((swe.calc_ut(prev_jd, swe.SUN, swe.FLG_SWIEPH)[0][0] - natal_sun + 180) % 360) - 180

    jd = start + step
    while jd <= end:
        sun = swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH)[0][0]
        diff = ((sun - natal_sun + 180) % 360) - 180

        if prev_diff <= 0 < diff or prev_diff >= 0 > diff:
            lo, hi = prev_jd, jd
            for _ in range(40):
                mid = (lo + hi) / 2
                m_sun = swe.calc_ut(mid, swe.SUN, swe.FLG_SWIEPH)[0][0]
                m_diff = ((m_sun - natal_sun + 180) % 360) - 180
                if (prev_diff <= 0 < m_diff) or (prev_diff >= 0 > m_diff):
                    hi = mid
                else:
                    lo = mid
                    prev_diff = m_diff
            return (lo + hi) / 2

        prev_jd = jd
        prev_diff = diff
        jd += step

    raise RuntimeError("Не удалось найти момент соляра")


async def calculate_solar_async(date_str, time_str, city):
    natal = await calculate_chart_async(date_str, time_str, city)

    jd_birth, _, _ = get_julian_day(
        date_str,
        time_str,
        natal["birth"]["timezone"],
    )

    solar_jd = approximate_solar_year(jd_birth)

    lat = natal["location"]["latitude"]
    lon = natal["location"]["longitude"]

    cusps, ascmc = swe.houses_ex(
        solar_jd,
        lat,
        lon,
        b"P",
        0,
    )

    sun = swe.calc_ut(
        solar_jd,
        swe.SUN,
        swe.FLG_SWIEPH | swe.FLG_SPEED,
    )[0][0]

    planets = {}
    for name, planet_id in PLANETS:
        result, _ = swe.calc_ut(
            solar_jd,
            planet_id,
            swe.FLG_SWIEPH | swe.FLG_SPEED,
        )
        planets[name] = {
            **sign_position(result[0]),
            "retrograde": result[3] < 0,
        }

    solar = {
        "source": "Swiss Ephemeris",
        "type": "Solar Return",
        "location": natal["location"],
        "timezone": natal["birth"]["timezone"],
        "natal_sun": sign_position(
            swe.calc_ut(jd_birth, swe.SUN, swe.FLG_SWIEPH)[0][0]
        ),
        "solar_sun": sign_position(sun),
        "solar_julian_day": solar_jd,
        "solar_houses": [
            {
                "house": i + 1,
                **sign_position(float(cusps[i])),
            }
            for i in range(12)
        ],
        "solar_angles": {
            "Асцендент": sign_position(float(ascmc[0])),
            "MC": sign_position(float(ascmc[1])),
        },
        "planets": planets,
    }

    return solar


def split_text(text, limit=3900):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < 1000:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()

    if text:
        chunks.append(text)

    return chunks


async def ai_analysis(kind, data, context):
    if kind == "natal":
        prompt = f"""
Ты — редактор эзотерического астрологического разбора AstroVilki.

Пользователь:
{context}

Ниже приведён автоматически рассчитанный набор астрологических данных.
Источник расчёта — Swiss Ephemeris. Не придумывай отсутствующие положения.

Сделай большой, понятный и структурированный разбор на русском:

1. Общий портрет.
2. Солнце, Луна и Асцендент.
3. Все планеты по знакам.
4. Все планеты по домам.
5. Основные аспекты.
6. Сильные стороны.
7. Напряжённые темы.
8. Любовь и отношения.
9. Деньги и ресурсы.
10. Учёба, профессия и реализация.
11. Общение и эмоциональный стиль.
12. Северный узел/кармические темы, если он есть.
13. Итог — 10 самых важных наблюдений.

Не выдавай астрологию за научный факт. Не делай медицинских, юридических или финансовых обещаний.

ДАННЫЕ:
{chart_text(data)}
"""

    elif kind == "solar":
        prompt = f"""
Ты — редактор эзотерического соляра AstroVilki.

Данные рождения:
{context}

Ниже находится автоматически рассчитанный Solar Return.
Сделай подробный разбор года на русском:

1. Главная тема года.
2. Асцендент соляра.
3. Положение Солнца.
4. Планеты соляра.
5. Дома и наиболее заметные акценты.
6. Отношения.
7. Деньги.
8. Учёба и работа.
9. Переезд/путешествия, если есть соответствующие показатели.
10. Эмоциональный фон.
11. Возможные точки роста.
12. Самые важные периоды — только если из предоставленных данных это можно обосновать.
13. Итог года.

Не придумывай отсутствующие положения и не выдавай астрологию за научный прогноз.

ДАННЫЕ СОЛЯРА:
{data}
"""

    else:
        prompt = f"""
Сделай красивый подробный эзотерический разбор «матрицы судьбы»
по 22 арканам.

Данные пользователя:
{context}

Расчёт:
{data}

Разбери:
характер, сильные стороны, отношения, деньги, реализацию,
зоны роста, ресурс и практические вопросы для саморефлексии.

Обязательно объясни, что это эзотерическая система, а не научный метод.
Не выдавай трактовки за объективные факты.
"""

    resp = await llm.responses.create(
        model=MODEL,
        input=prompt,
    )
    return resp.output_text


async def run_report(update, context, kind):
    query = update.callback_query
    await query.answer()

    data = context.user_data

    if not all(k in data for k in ("date", "time", "city")):
        await query.message.reply_text(
            "Сначала введи данные рождения через /start."
        )
        return

    ctx = f"{data['date']}, {data['time']}, {data['city']}"

    await query.message.chat.send_action(ChatAction.TYPING)

    try:
        if kind in ("natal", "full"):
            chart = await calculate_chart_async(
                data["date"],
                data["time"],
                data["city"],
            )

            if kind == "natal":
                result = await ai_analysis("natal", chart, ctx)
            else:
                natal = await ai_analysis("natal", chart, ctx)
                matrix = await ai_analysis(
                    "matrix",
                    matrix_22(data["date"]),
                    ctx,
                )
                result = (
                    "🌌 НАТАЛЬНАЯ КАРТА\n\n"
                    + natal
                    + "\n\n🔢 МАТРИЦА СУДЬБЫ\n\n"
                    + matrix
                )

        elif kind == "matrix":
            result = await ai_analysis(
                "matrix",
                matrix_22(data["date"]),
                ctx,
            )

        elif kind == "solar":
            solar = await calculate_solar_async(
                data["date"],
                data["time"],
                data["city"],
            )
            result = await ai_analysis(
                "solar",
                solar,
                ctx,
            )

        else:
            result = "Неизвестный тип расчёта."

    except Exception as e:
        log.exception("report error")
        result = (
            "Не получилось автоматически построить расчёт.\n\n"
            "Проверь дату, время и название города. "
            "Если данные правильные, возможно, временно недоступен "
            "сервис определения координат города.\n\n"
            f"Техническая ошибка: {type(e).__name__}: {e}"
        )

    result += DISCLAIMER

    for chunk in split_text(result):
        await query.message.reply_text(chunk)

    await query.message.reply_text(
        "Готово ✨ Что сделать дальше?",
        reply_markup=menu(),
    )


async def callback(update, context):
    kind = update.callback_query.data

    if kind == "reset":
        await update.callback_query.answer()
        context.user_data.clear()
        await update.callback_query.message.reply_text(
            "Давай заново. Напиши дату рождения: ДД.ММ.ГГГГ"
        )
        return DATE

    await run_report(update, context, kind)


async def help_cmd(update, context):
    await update.message.reply_text(
        "Команды:\n"
        "/start — ввести данные рождения заново\n"
        "/help — помощь\n\n"
        "После ввода даты, времени и города можно выбрать "
        "натальную карту, матрицу, соляр или полный разбор."
    )


async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_date,
                )
            ],
            TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_time,
                )
            ],
            CITY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_city,
                )
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_error_handler(error_handler)

    log.info("AstroVilki started without Geocult")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
