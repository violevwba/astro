import os
import re
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrovilki")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
tf = TimezoneFinder()

DATE, TIME, CITY = range(3)

DISCLAIMER = (
    "\n\n⚠️ Это эзотерическая интерпретация для саморефлексии, "
    "а не научный метод. Не используй её как единственное основание "
    "для медицинских, юридических или финансовых решений."
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

SIGN_TEXT = {
    "Овен": "инициативность, прямота, самостоятельность и стремление действовать первой",
    "Телец": "потребность в устойчивости, практичность, чувственность и умение доводить начатое",
    "Близнецы": "любознательность, гибкость мышления, интерес к информации и общению",
    "Рак": "эмоциональная глубина, привязанность к близким, интуитивность и потребность в безопасности",
    "Лев": "яркость, творческое самовыражение, уверенность и желание проявлять индивидуальность",
    "Дева": "аналитичность, наблюдательность, практичность и внимание к деталям",
    "Весы": "дипломатичность, чувство баланса, интерес к партнёрству и красоте",
    "Скорпион": "интенсивность, проницательность, сильная воля и стремление докапываться до сути",
    "Стрелец": "свобода, масштаб мышления, интерес к знаниям, путешествиям и новым горизонтам",
    "Козерог": "целеустремлённость, дисциплина, ответственность и ориентация на результат",
    "Водолей": "независимость, оригинальность, интерес к новым идеям и свободе выбора",
    "Рыбы": "чувствительность, воображение, эмпатия и сильная интуитивная восприимчивость",
}

HOUSE_TEXT = {
    1: "личность, внешний образ, самостоятельность и способ проявляться в мире",
    2: "деньги, личные ресурсы, ценности и ощущение собственной опоры",
    3: "общение, обучение, информация, документы, поездки на небольшие расстояния",
    4: "дом, семья, корни, личное пространство и внутреннее чувство безопасности",
    5: "романтика, творчество, удовольствие, хобби и самовыражение",
    6: "повседневная работа, режим, обязанности, навыки и организация жизни",
    7: "партнёрство, отношения, брак и взаимодействие один-на-один",
    8: "общие финансы, глубокие перемены, доверие и психологическая трансформация",
    9: "высшее образование, мировоззрение, дальние поездки и расширение кругозора",
    10: "карьера, статус, цели, общественная реализация и профессиональное направление",
    11: "друзья, сообщества, планы, аудитория и долгосрочные мечты",
    12: "уединение, внутренний мир, завершение циклов и скрытые процессы",
}

PLANET_TEXT = {
    "Солнце": "ядро личности, воля, чувство индивидуальности и направление самореализации",
    "Луна": "эмоциональные реакции, привычки, потребность в безопасности и внутренний комфорт",
    "Меркурий": "мышление, речь, обучение, анализ информации и стиль общения",
    "Венера": "симпатии, отношения, вкус, удовольствие и отношение к ценностям",
    "Марс": "действие, энергия, напор, инициативность и способ добиваться своего",
    "Юпитер": "рост, убеждения, образование, возможности и стремление расширять горизонты",
    "Сатурн": "дисциплина, границы, ответственность, ограничения и долгосрочное взросление",
    "Уран": "перемены, свобода, нестандартность и желание нарушить привычный сценарий",
    "Нептун": "мечты, воображение, идеалы, эмпатия и чувствительность к атмосфере",
    "Плутон": "глубокие изменения, сила, контроль, кризисы и способность к перерождению",
    "Северный узел": "направление развития и качества, которые символически предлагается осваивать",
}

ASPECT_TEXT = {
    "соединение": "усиливает и объединяет темы двух планет",
    "секстиль": "может указывать на удобный потенциал, который раскрывается через действие",
    "квадрат": "создаёт напряжение и задачу научиться согласовывать разные потребности",
    "тригон": "указывает на естественную совместимость и относительно лёгкое проявление качеств",
    "оппозиция": "подчёркивает необходимость баланса между двумя противоположными полюсами",
}

ASPECTS = [
    ("соединение", 0, 8),
    ("секстиль", 60, 5),
    ("квадрат", 90, 7),
    ("тригон", 120, 7),
    ("оппозиция", 180, 8),
]


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌌 Натальная карта", callback_data="open_natal")],
        [InlineKeyboardButton("☀️ Соляр", callback_data="open_solar")],
        [InlineKeyboardButton("🔢 Матрица судьбы", callback_data="open_matrix")],
        [InlineKeyboardButton("✨ Полный разбор", callback_data="full")],
    ])


def natal_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌞 Личность", callback_data="natal_personality"),
         InlineKeyboardButton("❤️ Отношения", callback_data="natal_relationships")],
        [InlineKeyboardButton("💰 Деньги", callback_data="natal_money"),
         InlineKeyboardButton("🎓 Учёба", callback_data="natal_study")],
        [InlineKeyboardButton("💼 Карьера", callback_data="natal_career"),
         InlineKeyboardButton("🏠 Дом/семья", callback_data="natal_home")],
        [InlineKeyboardButton("🧠 Психология", callback_data="natal_psychology"),
         InlineKeyboardButton("👥 Друзья", callback_data="natal_friends")],
        [InlineKeyboardButton("🪐 Планеты", callback_data="natal_planets"),
         InlineKeyboardButton("🏠 Все дома", callback_data="natal_houses")],
        [InlineKeyboardButton("🔗 Аспекты", callback_data="natal_aspects"),
         InlineKeyboardButton("⭐ Итог", callback_data="natal_summary")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back_main")],
    ])


def solar_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌞 Главная тема", callback_data="solar_main"),
         InlineKeyboardButton("❤️ Отношения", callback_data="solar_relationships")],
        [InlineKeyboardButton("💰 Деньги", callback_data="solar_money"),
         InlineKeyboardButton("🎓 Учёба", callback_data="solar_study")],
        [InlineKeyboardButton("💼 Работа", callback_data="solar_career"),
         InlineKeyboardButton("🏠 Переезд/дом", callback_data="solar_home")],
        [InlineKeyboardButton("✈️ Поездки", callback_data="solar_travel"),
         InlineKeyboardButton("👥 Друзья", callback_data="solar_friends")],
        [InlineKeyboardButton("🧠 Психология", callback_data="solar_psychology"),
         InlineKeyboardButton("💘 Партнёр", callback_data="solar_partner")],
        [InlineKeyboardButton("⚠️ Риски", callback_data="solar_risks"),
         InlineKeyboardButton("📅 Периоды года", callback_data="solar_periods")],
        [InlineKeyboardButton("⭐ Итог", callback_data="solar_summary"),
         InlineKeyboardButton("⬅️ Главное меню", callback_data="back_main")],
    ])


def matrix_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌟 Главная энергия", callback_data="matrix_core"),
         InlineKeyboardButton("❤️ Отношения", callback_data="matrix_relationships")],
        [InlineKeyboardButton("💰 Деньги", callback_data="matrix_money"),
         InlineKeyboardButton("💼 Реализация", callback_data="matrix_career")],
        [InlineKeyboardButton("🧠 Характер", callback_data="matrix_character"),
         InlineKeyboardButton("⚠️ Тени", callback_data="matrix_shadows")],
        [InlineKeyboardButton("🎁 Ресурсы", callback_data="matrix_resources"),
         InlineKeyboardButton("⭐ Итог", callback_data="matrix_summary")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back_main")],
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
        "Теперь всё работает без Geocult и без OpenAI API. "
        "Натальная карта и соляр рассчитываются локально, "
        "а расшифровка формируется внутри бота.\n\n"
        "Напиши дату рождения в формате ДД.ММ.ГГГГ."
    )
    return DATE


async def get_date(update, context):
    d = valid_date(update.message.text)
    if not d:
        await update.message.reply_text("Не смогла понять дату. Пример: 14.02.2001")
        return DATE
    context.user_data["date"] = d.strftime("%d.%m.%Y")
    await update.message.reply_text("Теперь напиши точное местное время рождения, например 07:35.")
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
        "Теперь выбери расчёт:",
        reply_markup=main_menu(),
    )
    return ConversationHandler.END


async def geocode_city(city):
    headers = {"User-Agent": "AstroVilkiBot/3.0"}
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
        raise ValueError("Не удалось определить часовой пояс")

    return lat, lon, display, tz_name


def normalize_angle(x):
    return x % 360.0


def sign_position(longitude):
    longitude = normalize_angle(longitude)
    index = int(longitude // 30)
    degree = longitude % 30
    return {
        "longitude": round(longitude, 4),
        "sign": SIGNS[index],
        "degree": round(degree, 2),
    }


def angle_distance(a, b):
    diff = abs((a - b) % 360)
    return min(diff, 360 - diff)


def get_julian_day(date_str, time_str, tz_name):
    local_dt = datetime.strptime(
        f"{date_str} {time_str}", "%d.%m.%Y %H:%M"
    ).replace(tzinfo=ZoneInfo(tz_name))

    utc_dt = local_dt.astimezone(timezone.utc)
    hour = utc_dt.hour + utc_dt.minute / 60 + utc_dt.second / 3600

    jd = swe.julday(
        utc_dt.year,
        utc_dt.month,
        utc_dt.day,
        hour,
        swe.GREG_CAL,
    )
    return jd, local_dt, utc_dt


async def calculate_chart(date_str, time_str, city):
    lat, lon, display, tz_name = await geocode_city(city)
    jd, local_dt, utc_dt = get_julian_day(date_str, time_str, tz_name)

    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    planets = {}

    for name, planet_id in PLANETS:
        result, _ = swe.calc_ut(jd, planet_id, flags)
        planets[name] = {
            **sign_position(result[0]),
            "retrograde": result[3] < 0,
        }

    cusps, ascmc = swe.houses_ex(jd, lat, lon, b"P", 0)
    houses = [round(float(x), 4) for x in cusps[:12]]

    angles = {
        "Асцендент": float(ascmc[0]),
        "MC": float(ascmc[1]),
        "Десцендент": float(ascmc[0]) + 180,
        "IC": float(ascmc[1]) + 180,
    }

    aspects = []
    names = list(planets.keys())

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            distance = angle_distance(
                planets[names[i]]["longitude"],
                planets[names[j]]["longitude"],
            )

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

    for body, value in planets.items():
        lon_body = value["longitude"]
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

    return {
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
            k: sign_position(v) for k, v in angles.items()
        },
        "houses": [
            {"house": i + 1, **sign_position(houses[i])}
            for i in range(12)
        ],
        "planets": planets,
        "aspects": aspects,
    }


def matrix_22(date_str):
    d = datetime.strptime(date_str, "%d.%m.%Y")

    def reduce22(n):
        while n > 22:
            n = sum(int(x) for x in str(n))
        return 22 if n == 0 else n

    day = reduce22(d.day)
    month = reduce22(d.month)
    year = reduce22(sum(int(x) for x in str(d.year)))
    core = reduce22(day + month + year)

    return {
        "day": day,
        "month": month,
        "year": year,
        "core": core,
    }


def planet_sentence(name, value):
    retro = " В ретроградном движении эта тема может больше проживаться через пересмотр и внутреннюю работу." if value["retrograde"] else ""
    return (
        f"• {name} — {value['sign']} ({value['degree']}°), "
        f"{value['house']} дом. Здесь акцентированы "
        f"{PLANET_TEXT[name]}. Знак добавляет "
        f"{SIGN_TEXT[value['sign']]}. "
        f"Сфера дома связана с {HOUSE_TEXT[value['house']]}.{retro}"
    )


def natal_report(chart):
    p = chart["planets"]
    a = chart["angles"]

    lines = [
        "🌌 НАТАЛЬНАЯ КАРТА",
        "",
        "Расчёт выполнен локально через Swiss Ephemeris.",
        f"Дата: {chart['birth']['date']}",
        f"Время: {chart['birth']['local_time']} ({chart['birth']['timezone']})",
        f"Место: {chart['location']['display_name']}",
        "",
        "✨ ОСНОВНОЙ ПОРТРЕТ",
        f"Солнце в {p['Солнце']['sign']} символически связывают с {SIGN_TEXT[p['Солнце']['sign']]}.",
        f"Луна в {p['Луна']['sign']} подчёркивает {SIGN_TEXT[p['Луна']['sign']]}.",
        f"Асцендент в {a['Асцендент']['sign']} описывается как способ проявляться через {SIGN_TEXT[a['Асцендент']['sign']]}.",
        "",
        "🪐 ПЛАНЕТЫ",
    ]

    for name, value in p.items():
        lines.append(planet_sentence(name, value))

    lines.extend(["", "🔗 ОСНОВНЫЕ АСПЕКТЫ"])

    if chart["aspects"]:
        for x in chart["aspects"]:
            lines.append(
                f"• {x['body1']} — {x['aspect']} — {x['body2']} "
                f"(орб {x['orb']}°): {ASPECT_TEXT[x['aspect']]}. "
            )
    else:
        lines.append("• Значимых аспектов по установленным орбам не найдено.")

    lines.extend([
        "",
        "🏠 ДОМА",
    ])

    for h in chart["houses"]:
        lines.append(
            f"• {h['house']} дом в {h['sign']} — "
            f"сфера: {HOUSE_TEXT[h['house']]}; стиль проявления — "
            f"{SIGN_TEXT[h['sign']]}."
        )

    lines.extend([
        "",
        "❤️ ОТНОШЕНИЯ",
        f"Главный показатель отношений — Венера в {p['Венера']['sign']} и "
        f"{p['Венера']['house']} доме. Символически это подчёркивает "
        f"{SIGN_TEXT[p['Венера']['sign']]} в любви и ценностях. "
        f"Дополнительно смотри 7 дом: он начинается в {chart['houses'][6]['sign']}.",
        "",
        "💰 ДЕНЬГИ",
        f"Для финансовой темы особенно важен 2 дом: он начинается в "
        f"{chart['houses'][1]['sign']}. В карте также стоит учитывать планеты "
        f"во 2, 6, 8 и 10 домах.",
        "",
        "🎓 УЧЁБА И РЕАЛИЗАЦИЯ",
        f"Для обучения и расширения горизонтов важен 9 дом "
        f"({chart['houses'][8]['sign']}), а для карьеры — 10 дом "
        f"({chart['houses'][9]['sign']}). Меркурий находится в "
        f"{p['Меркурий']['sign']}, что символически описывает стиль мышления "
        f"через {SIGN_TEXT[p['Меркурий']['sign']]}."
    ])

    return "\n".join(lines) + DISCLAIMER


def solar_return_jd(jd_birth):
    natal_sun = swe.calc_ut(jd_birth, swe.SUN, swe.FLG_SWIEPH)[0][0]

    def diff(jd):
        current = swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH)[0][0]
        return ((current - natal_sun + 180) % 360) - 180

    lo = jd_birth + 350
    hi = jd_birth + 380
    step = 0.25
    prev_jd = lo
    prev = diff(prev_jd)

    x = lo + step
    while x <= hi:
        cur = diff(x)
        if (prev <= 0 < cur) or (prev >= 0 > cur):
            left, right = prev_jd, x
            for _ in range(50):
                mid = (left + right) / 2
                md = diff(mid)
                if (prev <= 0 < md) or (prev >= 0 > md):
                    right = mid
                else:
                    left = mid
                    prev = md
            return (left + right) / 2
        prev_jd, prev, x = x, cur, x + step

    raise ValueError("Не удалось найти соляр")


async def calculate_solar(chart):
    jd_birth, _, _ = get_julian_day(
        chart["birth"]["date"],
        chart["birth"]["local_time"],
        chart["birth"]["timezone"],
    )

    jd = solar_return_jd(jd_birth)
    lat = chart["location"]["latitude"]
    lon = chart["location"]["longitude"]

    cusps, ascmc = swe.houses_ex(jd, lat, lon, b"P", 0)

    planets = {}
    for name, planet_id in PLANETS:
        result, _ = swe.calc_ut(
            jd, planet_id, swe.FLG_SWIEPH | swe.FLG_SPEED
        )
        planets[name] = {
            **sign_position(result[0]),
            "retrograde": result[3] < 0,
        }

    year, month, day, hour = swe.revjul(jd, swe.GREG_CAL)
    hour_i = int(hour)
    minute = int((hour - hour_i) * 60)

    return {
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "time_utc": f"{hour_i:02d}:{minute:02d}",
        "asc": sign_position(ascmc[0]),
        "mc": sign_position(ascmc[1]),
        "houses": [
            {"house": i + 1, **sign_position(cusps[i])}
            for i in range(12)
        ],
        "planets": planets,
    }


def solar_report(chart, solar):
    p = solar["planets"]

    lines = [
        "☀️ СОЛЯР",
        "",
        "Соляр рассчитывается локально по возвращению Солнца к его "
        "натальному положению.",
        f"Момент соляра примерно: {solar['date']} {solar['time_utc']} UTC",
        "",
        f"🌅 Асцендент соляра: {solar['asc']['sign']} {solar['asc']['degree']}°.",
        f"🏔 MC соляра: {solar['mc']['sign']} {solar['mc']['degree']}°.",
        "",
        "🪐 ПОЛОЖЕНИЯ ПЛАНЕТ",
    ]

    for name, value in p.items():
        lines.append(
            f"• {name}: {value['sign']} {value['degree']}° — "
            f"{SIGN_TEXT[value['sign']]}."
        )

    lines.extend([
        "",
        "🎯 ТЕМЫ ГОДА",
        f"• Личное проявление: Асцендент в {solar['asc']['sign']} — "
        f"акцент на {SIGN_TEXT[solar['asc']['sign']]}",
        f"• Карьерный вектор: MC в {solar['mc']['sign']} — "
        f"через {SIGN_TEXT[solar['mc']['sign']]}",
        f"• Солнце года находится в {p['Солнце']['sign']} — "
        f"главная тема самореализации может символически разворачиваться через "
        f"{SIGN_TEXT[p['Солнце']['sign']]}."
    ])

    lines.extend([
        "",
        "❤️ ОТНОШЕНИЯ",
        f"Венера в {p['Венера']['sign']} подчёркивает "
        f"{SIGN_TEXT[p['Венера']['sign']]} в романтической сфере.",
        "",
        "💰 ДЕНЬГИ И РАБОТА",
        f"Юпитер в {p['Юпитер']['sign']} символически усиливает "
        f"{SIGN_TEXT[p['Юпитер']['sign']]}. Сатурн в "
        f"{p['Сатурн']['sign']} напоминает о дисциплине и долгосрочном подходе.",
        "",
        "📚 УЧЁБА И ПЕРЕМЕНЫ",
        f"Меркурий в {p['Меркурий']['sign']} подчёркивает "
        f"{SIGN_TEXT[p['Меркурий']['sign']]}; Уран в "
        f"{p['Уран']['sign']} связан с темой перемен и свободы.",
    ])

    return "\n".join(lines) + DISCLAIMER


def matrix_report(date_str):
    m = matrix_22(date_str)

    descriptions = {
        1: "инициативы и самостоятельного старта",
        2: "интуиции, чувствительности и наблюдательности",
        3: "творчества, коммуникации и созидания",
        4: "структуры, порядка и ответственности",
        5: "знаний, наставничества и ценностей",
        6: "выбора, отношений и умения договариваться",
        7: "движения, цели и победы через дисциплину",
        8: "баланса, справедливости и ответственности",
        9: "самостоятельного поиска, анализа и мудрости",
        10: "циклов, изменений и умения пользоваться возможностями",
        11: "силы характера и управления энергией",
        12: "переосмысления, терпения и смены взгляда",
        13: "трансформации и завершения старого",
        14: "баланса, умеренности и постепенного развития",
        15: "желаний, материальной мотивации и контроля привязанностей",
        16: "перестройки старых конструкций и освобождения от лишнего",
        17: "надежды, вдохновения и долгосрочного видения",
        18: "интуиции, эмоций и работы со страхами",
        19: "самовыражения, радости и открытости",
        20: "переоценки прошлого и нового этапа",
        21: "завершения циклов и расширения масштаба",
        22: "свободы, нового опыта и нестандартного пути",
    }

    lines = [
        "🔢 МАТРИЦА СУДЬБЫ",
        "",
        "Расчёт выполнен по упрощённой 22-арканной схеме. "
        "Разные школы используют разные формулы, поэтому это не объективная "
        "характеристика личности.",
        "",
        f"День: {m['day']} — тема {descriptions[m['day']]}",
        f"Месяц: {m['month']} — тема {descriptions[m['month']]}",
        f"Год: {m['year']} — тема {descriptions[m['year']]}",
        f"Центр: {m['core']} — тема {descriptions[m['core']]}",
        "",
        "✨ ИНТЕРПРЕТАЦИЯ",
        f"Повторяющиеся темы этой комбинации можно использовать как вопросы "
        f"для саморефлексии: где проявляется {descriptions[m['core']]}; "
        f"как ты используешь {descriptions[m['day']]}; и чему можешь научиться "
        f"через тему {descriptions[m['month']]}."
    ]

    return "\n".join(lines) + DISCLAIMER


def full_report(chart, solar, matrix):
    return (
        natal_report(chart)
        + "\n\n\n"
        + solar_report(chart, solar)
        + "\n\n\n"
        + matrix_report(chart["birth"]["date"])
    )


def section_title(title):
    return f"━━━━━━━━━━━━━━━━\n{title}\n━━━━━━━━━━━━━━━━\n"


def natal_expanded(chart, section):
    p = chart["planets"]
    h = chart["houses"]
    a = chart["angles"]

    if section == "personality":
        return section_title("🌞 ЛИЧНОСТЬ — ПОЛНЫЙ РАЗБОР") + (
            f"☀️ Солнце: {p['Солнце']['sign']}, {p['Солнце']['house']} дом.\n"
            f"Символически это сочетает {SIGN_TEXT[p['Солнце']['sign']]} "
            f"с темами {HOUSE_TEXT[p['Солнце']['house']]}\n\n"
            f"☽ Луна: {p['Луна']['sign']}, {p['Луна']['house']} дом.\n"
            f"Эмоциональная реакция связана с {SIGN_TEXT[p['Луна']['sign']]} "
            f"и сферой {HOUSE_TEXT[p['Луна']['house']]}\n\n"
            f"🌅 ASC: {a['Асцендент']['sign']}.\n"
            f"Внешний стиль: {SIGN_TEXT[a['Асцендент']['sign']]}\n\n"
            "Главная связка личности — то, что ты хочешь реализовать (Солнце), "
            "то, что тебе эмоционально необходимо (Луна), и то, как ты входишь "
            "в мир (ASC). Эти три уровня лучше рассматривать вместе."
        )

    if section == "relationships":
        v, m = p["Венера"], p["Марс"]
        return section_title("❤️ ОТНОШЕНИЯ — ПОЛНЫЙ РАЗБОР") + (
            f"7 дом: {h[6]['sign']} — тема партнёрства окрашена через "
            f"{SIGN_TEXT[h[6]['sign']]}\n\n"
            f"♀ Венера: {v['sign']}, {v['house']} дом.\n"
            f"В любви важны {SIGN_TEXT[v['sign']]}; проявление идёт через "
            f"{HOUSE_TEXT[v['house']]}\n\n"
            f"♂ Марс: {m['sign']}, {m['house']} дом.\n"
            f"Способ действовать и добиваться близости: {SIGN_TEXT[m['sign']]}\n\n"
            "На что смотреть в отношениях:\n"
            "• насколько совпадают ценности;\n"
            "• есть ли уважение к границам;\n"
            "• можно ли открыто говорить о деньгах и планах;\n"
            "• не становится ли партнёр центром всей жизни;\n"
            "• сохраняется ли личная самостоятельность.\n\n"
            "Карта описывает символические предпочтения, а не гарантирует знакомство "
            "или конкретный исход отношений."
        )

    if section == "money":
        return section_title("💰 ДЕНЬГИ — ПОЛНЫЙ РАЗБОР") + (
            f"2 дом: {h[1]['sign']} — личный доход и собственные ресурсы.\n"
            f"Стиль: {SIGN_TEXT[h[1]['sign']]}\n\n"
            f"8 дом: {h[7]['sign']} — общие деньги, поддержка, вложения и "
            f"финансовые связи. Стиль: {SIGN_TEXT[h[7]['sign']]}\n\n"
            f"♃ Юпитер: {p['Юпитер']['sign']}, {p['Юпитер']['house']} дом — "
            f"расширение через {SIGN_TEXT[p['Юпитер']['sign']]}\n\n"
            f"♇ Плутон: {p['Плутон']['sign']}, {p['Плутон']['house']} дом — "
            "глубокая перестройка отношения к ресурсам и контролю.\n\n"
            "Практический смысл: развивать собственный доход, считать расходы, "
            "не смешивать чувства и финансовые обещания и постепенно повышать "
            "ценность своих навыков."
        )

    if section == "study":
        return section_title("🎓 УЧЁБА — ПОЛНЫЙ РАЗБОР") + (
            f"3 дом: {h[2]['sign']} — повседневное обучение и информация.\n"
            f"9 дом: {h[8]['sign']} — высшее образование и расширение горизонтов.\n\n"
            f"☿ Меркурий: {p['Меркурий']['sign']}, {p['Меркурий']['house']} дом.\n"
            f"Мышление и обучение: {SIGN_TEXT[p['Меркурий']['sign']]}\n\n"
            "Сильнее всего могут раскрывать потенциал задачи, где нужно "
            "анализировать, объяснять, писать, общаться, работать с информацией "
            "или осваивать цифровые инструменты."
        )

    if section == "career":
        return section_title("💼 КАРЬЕРА — ПОЛНЫЙ РАЗБОР") + (
            f"6 дом: {h[5]['sign']} — ежедневная работа и навыки.\n"
            f"10 дом: {h[9]['sign']} — статус и профессиональная реализация.\n\n"
            f"MC: {a['MC']['sign']} — публичное направление через "
            f"{SIGN_TEXT[a['MC']['sign']]}\n\n"
            f"♄ Сатурн: {p['Сатурн']['sign']}, {p['Сатурн']['house']} дом — "
            "тема дисциплины, ответственности и долгосрочного результата.\n\n"
            "Лучший сценарий — не распыляться, а превращать интерес в измеримый "
            "навык, портфолио и реальный опыт."
        )

    if section == "home":
        return section_title("🏠 ДОМ, СЕМЬЯ И ПЕРЕЕЗД") + (
            f"4 дом: {h[3]['sign']} — {SIGN_TEXT[h[3]['sign']]}\n\n"
            "4 дом показывает тему дома, семьи и личной базы. Для переезда "
            "важно дополнительно смотреть 9 и 10 дома, Луну и управителей.\n\n"
            "Если переезд планируется, разделяй символику карты и реальное решение: "
            "жильё, бюджет, документы, учёба/работа и безопасность должны проверяться отдельно."
        )

    if section == "psychology":
        return section_title("🧠 ПСИХОЛОГИЯ — ПОЛНЫЙ РАЗБОР") + (
            f"☽ Луна в {p['Луна']['sign']}: {SIGN_TEXT[p['Луна']['sign']]}\n\n"
            f"♇ Плутон в {p['Плутон']['sign']}, {p['Плутон']['house']} дом: "
            f"тема {HOUSE_TEXT[p['Плутон']['house']]}\n\n"
            f"♄ Сатурн в {p['Сатурн']['sign']}: развитие через "
            f"{SIGN_TEXT[p['Сатурн']['sign']]}\n\n"
            "Полезно наблюдать, где решения идут из собственного желания, "
            "а где — из страха, давления или ожиданий окружающих."
        )

    if section == "friends":
        return section_title("👥 ДРУЗЬЯ И ОКРУЖЕНИЕ") + (
            f"11 дом: {h[10]['sign']} — {SIGN_TEXT[h[10]['sign']]}\n\n"
            "Друзья, сообщества и аудитория могут быть источником новых идей "
            "и возможностей. Важно оценивать не количество контактов, "
            "а качество окружения и его влияние на твои цели."
        )

    if section == "planets":
        return section_title("🪐 ВСЕ ПЛАНЕТЫ") + "\n\n".join(
            planet_sentence(name, value) for name, value in p.items()
        )

    if section == "houses":
        return section_title("🏠 ВСЕ 12 ДОМОВ") + "\n\n".join(
            f"🏠 {x['house']} дом — {x['sign']} {x['degree']}°\n"
            f"{HOUSE_TEXT[x['house']]}\n"
            f"Проявление: {SIGN_TEXT[x['sign']]}"
            for x in h
        )

    if section == "aspects":
        lines = [section_title("🔗 АСПЕКТЫ")]
        for x in chart["aspects"]:
            lines.append(
                f"• {x['body1']} {x['aspect']} {x['body2']} — "
                f"орб {x['orb']}°. {ASPECT_TEXT[x['aspect']]}"
            )
        return "\n".join(lines)

    return natal_report(chart)


def solar_expanded(chart, solar, section):
    p, h = solar["planets"], solar["houses"]

    if section == "main":
        bullets = "\n".join(
            f"• {n} в {v['sign']} — {SIGN_TEXT[v['sign']]}"
            for n, v in p.items()
        )
        return section_title("🌞 ГЛАВНАЯ ТЕМА ГОДА") + (
            f"ASC соляра — {solar['asc']['sign']} {solar['asc']['degree']}°\n"
            f"MC — {solar['mc']['sign']} {solar['mc']['degree']}°\n\n"
            "Положения планет:\n" + bullets + "\n\n"
            f"ASC задаёт стиль года через {SIGN_TEXT[solar['asc']['sign']]}. "
            f"MC переносит внимание на {SIGN_TEXT[solar['mc']['sign']]}. "
            "Главную тему лучше определять по повторяющимся акцентам, а не по одной планете."
        )

    if section == "relationships":
        return section_title("❤️ ОТНОШЕНИЯ — ГЛУБОКИЙ РАЗБОР") + (
            f"7 дом: {h[6]['sign']} — {SIGN_TEXT[h[6]['sign']]}\n\n"
            f"☀️ Солнце в {p['Солнце']['sign']} делает тему самореализации связанной с "
            f"{SIGN_TEXT[p['Солнце']['sign']]}\n\n"
            f"♀ Венера в {p['Венера']['sign']} — {SIGN_TEXT[p['Венера']['sign']]}\n\n"
            f"♅ Уран в {p['Уран']['sign']} добавляет неожиданность и потребность в свободе.\n\n"
            "Возможные символические сценарии: новое знакомство, изменение статуса "
            "отношений, разговор о будущем, отношения через интернет/новое окружение "
            "или необходимость перестроить личные границы."
        )

    if section == "money":
        return section_title("💰 ДЕНЬГИ — ГЛУБОКИЙ РАЗБОР") + (
            f"2 дом: {h[1]['sign']} — {SIGN_TEXT[h[1]['sign']]}\n"
            f"8 дом: {h[7]['sign']} — {SIGN_TEXT[h[7]['sign']]}\n\n"
            f"♇ Плутон в {p['Плутон']['sign']} усиливает тему глубокой перестройки.\n"
            f"♃ Юпитер в {p['Юпитер']['sign']} символически связан с ростом через "
            f"{SIGN_TEXT[p['Юпитер']['sign']]}\n\n"
            "Главный практический принцип: собственный доход + финансовая подушка + "
            "ясные правила общих денег."
        )

    if section == "study":
        return section_title("🎓 УЧЁБА") + (
            f"9 дом: {h[8]['sign']} — {SIGN_TEXT[h[8]['sign']]}\n"
            f"☽ Луна в {p['Луна']['sign']} — эмоциональная вовлечённость через "
            f"{SIGN_TEXT[p['Луна']['sign']]}\n"
            f"☿ Меркурий в {p['Меркурий']['sign']} — обучение через "
            f"{SIGN_TEXT[p['Меркурий']['sign']]}\n\n"
            "Год может быть продуктивным для расширения квалификации, языков, "
            "университета, новых цифровых навыков и поездок, связанных с обучением."
        )

    if section == "career":
        return section_title("💼 РАБОТА") + (
            f"MC: {solar['mc']['sign']} — {SIGN_TEXT[solar['mc']['sign']]}\n"
            f"6 дом: {h[5]['sign']} — {SIGN_TEXT[h[5]['sign']]}\n"
            f"♄ Сатурн: {p['Сатурн']['sign']} — дисциплина и долгосрочная стратегия.\n\n"
            "Возможны новые обязанности, подработка, новый проект или необходимость "
            "выбрать более конкретное профессиональное направление."
        )

    if section == "home":
        return section_title("🏠 ПЕРЕЕЗД И ДОМ") + (
            f"4 дом: {h[3]['sign']} — {SIGN_TEXT[h[3]['sign']]}\n"
            f"♄ Сатурн в {p['Сатурн']['sign']} усиливает ответственность за базу и жильё.\n"
            f"♆ Нептун в {p['Нептун']['sign']} может добавлять неопределённость.\n\n"
            "Если переезд уже рассматривается, год может ощущаться как перестройка "
            "домашней основы. Конкретное решение обязательно проверяй по бюджету, "
            "жилью, документам и учёбе/работе."
        )

    if section == "travel":
        return section_title("✈️ ПОЕЗДКИ И ДРУГАЯ СТРАНА") + (
            f"9 дом: {h[8]['sign']} — {SIGN_TEXT[h[8]['sign']]}\n"
            f"☽ Луна в {p['Луна']['sign']} может сделать тему дальних поездок "
            f"эмоционально значимой через {SIGN_TEXT[p['Луна']['sign']]}\n\n"
            "Поездка может стать не просто событием, а причиной пересмотра "
            "планов на учёбу, окружение и место проживания."
        )

    if section == "friends":
        return section_title("👥 ДРУЗЬЯ") + (
            f"11 дом: {h[10]['sign']} — {SIGN_TEXT[h[10]['sign']]}\n\n"
            "Новые люди могут приходить через учёбу, работу, интернет, сообщества "
            "и общие проекты. Особенно полезны связи, которые открывают новые навыки."
        )

    if section == "psychology":
        return section_title("🧠 ПСИХОЛОГИЯ") + (
            f"ASC {solar['asc']['sign']} — годовой стиль через {SIGN_TEXT[solar['asc']['sign']]}\n"
            f"♇ Плутон в {p['Плутон']['sign']} — тема глубокой перестройки.\n\n"
            "Может усилиться самостоятельность, избирательность и желание избавиться "
            "от сценариев, которые больше не подходят."
        )

    if section == "partner":
        return section_title("💘 КАКИМ МОЖЕТ БЫТЬ ПАРТНЁР") + (
            f"7 дом в {h[6]['sign']} — качества {SIGN_TEXT[h[6]['sign']]}\n"
            f"♀ Венера в {p['Венера']['sign']} — привлекательность через "
            f"{SIGN_TEXT[p['Венера']['sign']]}\n"
            f"♅ Уран в {p['Уран']['sign']} — необычность, свобода, неожиданность.\n\n"
            "Знакомство символически может происходить через новую среду, "
            "интернет, друзей, учёбу или неожиданную ситуацию."
        )

    if section == "risks":
        return section_title("⚠️ СЛОЖНЫЕ СТОРОНЫ") + (
            "1. Эмоциональная зависимость от отношений.\n"
            "2. Финансовая зависимость от других людей.\n"
            "3. Импульсивные решения на пике эмоций.\n"
            "4. Идеализация людей или будущего.\n"
            "5. Перегрузка работой и обязанностями.\n\n"
            "Лучший способ снизить риски — давать важным решениям время и проверять их фактами."
        )

    if section == "periods":
        return section_title("📅 ПЕРИОДЫ ГОДА") + (
            "1 — 🌅 личность и внешний образ\n"
            "2 — 💰 деньги и самоценность\n"
            "3 — 📱 общение, документы, интернет\n"
            "4 — 🏠 дом и семья\n"
            "5 — ❤️ романтика и творчество\n"
            "6 — 💼 работа и режим\n"
            "7 — 💕 партнёрство\n"
            "8 — 🔥 близость и общие ресурсы\n"
            "9 — ✈️ учёба и дальние поездки\n"
            "10 — 👩‍💼 карьера\n"
            "11 — 👥 друзья и планы\n"
            "12 — 🌙 завершение цикла\n\n"
            "Это символическая последовательность домов, а не точные даты событий."
        )

    return section_title("⭐ ИТОГ СОЛЯРА") + (
        "❤️ отношения — заметная тема;\n"
        "💰 деньги — перестройка финансовой самостоятельности;\n"
        "🎓 учёба — расширение горизонтов;\n"
        "🏠 дом — изменение основы;\n"
        "✈️ поездки — новые перспективы;\n"
        "💼 работа — ответственность и практические задачи;\n"
        "👥 окружение — новые люди;\n"
        "🧠 психология — пересмотр старых сценариев."
    )


def matrix_expanded(date_str, section):
    m = matrix_22(date_str)
    names = {
        1:"Маг",2:"Жрица",3:"Императрица",4:"Император",5:"Иерофант",
        6:"Влюблённые",7:"Колесница",8:"Справедливость",9:"Отшельник",
        10:"Колесо Фортуны",11:"Сила",12:"Повешенный",13:"Трансформация",
        14:"Умеренность",15:"Дьявол",16:"Башня",17:"Звезда",18:"Луна",
        19:"Солнце",20:"Суд",21:"Мир",22:"Шут"
    }
    themes = {
        1:"инициатива",2:"интуиция",3:"творчество",4:"структура",5:"знания",
        6:"выбор и отношения",7:"движение",8:"баланс",9:"самостоятельный поиск",
        10:"изменения",11:"сила характера",12:"переосмысление",13:"трансформация",
        14:"гармония",15:"желания",16:"перестройка",17:"вдохновение",
        18:"эмоции",19:"самовыражение",20:"переоценка",21:"завершение",22:"свобода"
    }
    if section == "core":
        return section_title("🌟 ГЛАВНАЯ ЭНЕРГИЯ") + (
            f"Центр: {m['core']} — {names[m['core']]}\n"
            f"Ключ: {themes[m['core']]}\n\n"
            f"День: {m['day']} — {names[m['day']]} ({themes[m['day']]})\n"
            f"Месяц: {m['month']} — {names[m['month']]} ({themes[m['month']]})\n"
            f"Год: {m['year']} — {names[m['year']]} ({themes[m['year']]})"
        )
    if section == "relationships":
        return section_title("❤️ ОТНОШЕНИЯ") + (
            f"День {m['day']} ({names[m['day']]}) — {themes[m['day']]}\n"
            f"Месяц {m['month']} ({names[m['month']]}) — {themes[m['month']]}\n\n"
            "Используй эти темы как вопросы: умеешь ли ты говорить о желаниях, "
            "сохраняешь ли границы и выбираешь ли отношения из реальной совместимости."
        )
    if section == "money":
        return section_title("💰 ДЕНЬГИ") + (
            f"Центр {m['core']} ({names[m['core']]}) — {themes[m['core']]}\n\n"
            "Матрица не показывает конкретную сумму дохода. Она полезнее как "
            "символическая модель финансовых привычек: самостоятельность, дисциплина, "
            "отношение к риску и умение ценить собственный труд."
        )
    if section == "career":
        return section_title("💼 РЕАЛИЗАЦИЯ") + (
            f"Год {m['year']} ({names[m['year']]}) — {themes[m['year']]}\n"
            f"Центр {m['core']} ({names[m['core']]}) — {themes[m['core']]}\n\n"
            "Переводи сильные темы в конкретные навыки, проекты и портфолио."
        )
    if section == "character":
        return section_title("🧠 ХАРАКТЕР") + (
            f"День — {names[m['day']]}: {themes[m['day']]}\n"
            f"Месяц — {names[m['month']]}: {themes[m['month']]}\n"
            f"Год — {names[m['year']]}: {themes[m['year']]}\n"
            f"Центр — {names[m['core']]}: {themes[m['core']]}"
        )
    if section == "shadows":
        return section_title("⚠️ ТЕНИ") + (
            "Любая энергия может проявляться конструктивно или чрезмерно. "
            "Задача — не бояться «плохих арканов», а замечать повторяющиеся сценарии "
            "и менять поведение там, где оно мешает."
        )
    if section == "resources":
        return section_title("🎁 РЕСУРСЫ") + (
            f"День: {themes[m['day']]}\n"
            f"Месяц: {themes[m['month']]}\n"
            f"Год: {themes[m['year']]}\n"
            f"Центр: {themes[m['core']]}\n\n"
            "Ресурс становится сильнее, когда переводится в конкретное действие."
        )
    return section_title("⭐ ИТОГ МАТРИЦЫ") + (
        f"Главная энергия: {names[m['core']]} ({m['core']}).\n"
        f"День: {names[m['day']]}.\n"
        f"Месяц: {names[m['month']]}.\n"
        f"Год: {names[m['year']]}.\n\n"
        "Это эзотерическая система саморефлексии; разные школы используют разные формулы."
    )



async def run_report(update, context, kind):
    query = update.callback_query
    await query.answer()

    if not all(k in context.user_data for k in ("date", "time", "city")):
        await query.message.reply_text("Сначала введи данные через /start.")
        return

    data = context.user_data
    await query.message.chat.send_action(ChatAction.TYPING)

    try:
        chart = await calculate_chart(
            data["date"], data["time"], data["city"]
        )

        if kind == "natal":
            result = natal_report(chart)
        elif kind == "solar":
            solar = await calculate_solar(chart)
            result = solar_report(chart, solar)
        elif kind == "matrix":
            result = matrix_report(data["date"])
        elif kind == "full":
            solar = await calculate_solar(chart)
            result = full_report(
                chart,
                solar,
                matrix_22(data["date"]),
            )
        else:
            result = "Неизвестный тип расчёта."

    except Exception as e:
        log.exception("Calculation error")
        result = (
            "❌ Не получилось построить расчёт.\n\n"
            "Проверь дату, время и название города.\n\n"
            f"Техническая ошибка: {type(e).__name__}: {e}"
        )

    # Telegram message limit is about 4096 characters.
    for i in range(0, len(result), 3900):
        await query.message.reply_text(result[i:i + 3900])

    await query.message.reply_text(
        "Готово ✨ Выбери следующий раздел:",
        reply_markup=main_menu(),
    )


async def send_result(query, result, markup):
    for i in range(0, len(result), 3900):
        await query.message.reply_text(result[i:i+3900])
    await query.message.reply_text("Выбери следующий раздел:", reply_markup=markup)


async def callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_main":
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        return

    if data == "open_natal":
        await query.message.reply_text("🌌 Натальная карта — выбери раздел:", reply_markup=natal_menu())
        return

    if data == "open_solar":
        await query.message.reply_text("☀️ Соляр — выбери раздел:", reply_markup=solar_menu())
        return

    if data == "open_matrix":
        await query.message.reply_text("🔢 Матрица — выбери раздел:", reply_markup=matrix_menu())
        return

    if data == "full":
        try:
            chart = await calculate_chart(context.user_data["date"], context.user_data["time"], context.user_data["city"])
            solar = await calculate_solar(chart)
            result = (
                natal_expanded(chart, "personality") + "\n\n" +
                solar_expanded(chart, solar, "main") + "\n\n" +
                matrix_expanded(context.user_data["date"], "core")
            )
            await send_result(query, result, main_menu())
        except Exception as e:
            log.exception("full report error")
            await query.message.reply_text(f"❌ Ошибка расчёта: {type(e).__name__}: {e}", reply_markup=main_menu())
        return

    if not all(k in context.user_data for k in ("date", "time", "city")):
        await query.message.reply_text("Сначала введи данные через /start.")
        return

    try:
        if data.startswith("natal_"):
            chart = await calculate_chart(context.user_data["date"], context.user_data["time"], context.user_data["city"])
            section = data[len("natal_"):]
            result = natal_expanded(chart, section)
            await send_result(query, result, natal_menu())
            return

        if data.startswith("solar_"):
            chart = await calculate_chart(context.user_data["date"], context.user_data["time"], context.user_data["city"])
            solar = await calculate_solar(chart)
            section = data[len("solar_"):]
            result = solar_expanded(chart, solar, section)
            await send_result(query, result, solar_menu())
            return

        if data.startswith("matrix_"):
            section = data[len("matrix_"):]
            result = matrix_expanded(context.user_data["date"], section)
            await send_result(query, result, matrix_menu())
            return

    except Exception as e:
        log.exception("section error")
        await query.message.reply_text(
            f"❌ Не получилось построить этот раздел.\n\nТехническая ошибка: {type(e).__name__}: {e}",
            reply_markup=main_menu()
        )



async def help_cmd(update, context):
    await update.message.reply_text(
        "Команды:\n"
        "/start — ввести данные рождения\n"
        "/help — помощь\n\n"
        "OpenAI API и Geocult для работы бота НЕ нужны."
    )


async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_city)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_error_handler(error_handler)

    log.info("AstroVilki started: LOCAL MODE, no Geocult, no OpenAI")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
