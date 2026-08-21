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
        reply_markup=menu(),
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
        reply_markup=menu(),
    )


async def callback(update, context):
    kind = update.callback_query.data

    if kind == "reset":
        await update.callback_query.answer()
        context.user_data.clear()
        await update.callback_query.message.reply_text(
            "Начинаем заново. Напиши дату рождения: ДД.ММ.ГГГГ"
        )
        return DATE

    await run_report(update, context, kind)


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
