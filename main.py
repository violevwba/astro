import os
import re
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import swisseph as swe
from timezonefinder import TimezoneFinder
from geopy.geocoders import Nominatim
from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
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
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrovilki")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/")
FULL_REPORT_PRICE = os.getenv("FULL_REPORT_PRICE_RUB", "67.00")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

YOOKASSA_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

DATE, TIME, PLACE = range(3)

DISCLAIMER = (
    "\n\n⚠️ Астрология и «Матрица судьбы» — эзотерическая/развлекательная "
    "интерпретация, а не научный метод. Не используй её как единственное "
    "основание для медицинских, юридических или финансовых решений."
)

# ============================================================
# TELEGRAM MENUS
# ============================================================
MENU = ReplyKeyboardMarkup(
    [
        ["🌌 Полный разбор — 67 ₽", "🔭 Натальная карта"],
        ["🔢 Матрица судьбы", "☀️ Соляр"],
        ["💳 Оплата / полный разбор", "👤 Мои данные"],
    ],
    resize_keyboard=True,
)

PAYMENT_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("💳 Оплатить 67 ₽ через ЮKassa", callback_data="create_payment")],
        [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back_menu")],
    ]
)

# ============================================================
# ASTRO DATA
# ============================================================
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
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы",
]

SIGN_TEXT = {
    "Овен": "инициативность, прямота, самостоятельность и стремление действовать",
    "Телец": "устойчивость, практичность, чувственность и потребность в надёжной опоре",
    "Близнецы": "любознательность, коммуникацию, гибкость мышления и интерес к информации",
    "Рак": "эмоциональную глубину, интуицию, привязанность и потребность в безопасности",
    "Лев": "яркость, творчество, самовыражение и желание быть заметной",
    "Дева": "аналитичность, наблюдательность, практичность и внимание к деталям",
    "Весы": "дипломатию, партнёрство, красоту и стремление к балансу",
    "Скорпион": "интенсивность, проницательность, волю и стремление докапываться до сути",
    "Стрелец": "свободу, масштаб мышления, обучение и расширение горизонтов",
    "Козерог": "дисциплину, ответственность, стратегию и ориентацию на результат",
    "Водолей": "независимость, оригинальность, свободу и интерес к новым идеям",
    "Рыбы": "чувствительность, воображение, эмпатию и сильную интуитивную восприимчивость",
}

HOUSE_TEXT = {
    1: "личность, внешность, самостоятельность и способ проявляться",
    2: "личные деньги, ресурсы, ценности и самооценку",
    3: "общение, обучение, документы, информацию и короткие поездки",
    4: "дом, семью, корни, жильё и внутреннее чувство безопасности",
    5: "романтику, творчество, удовольствие, хобби и самовыражение",
    6: "работу, повседневность, режим, обязанности и навыки",
    7: "партнёрство, отношения, брак и взаимодействие один-на-один",
    8: "глубокую близость, общие финансы, кризисы и трансформацию",
    9: "высшее образование, путешествия, другую культуру и мировоззрение",
    10: "карьеру, статус, цели и профессиональную реализацию",
    11: "друзей, аудиторию, сообщества и долгосрочные планы",
    12: "уединение, внутренний мир, завершение циклов и скрытые процессы",
}

PLANET_TEXT = {
    "Солнце": "личность, воля и самореализация",
    "Луна": "эмоции, привычки и внутреннее чувство безопасности",
    "Меркурий": "мышление, речь, обучение и коммуникация",
    "Венера": "любовь, симпатии, вкус и ценности",
    "Марс": "энергия, действия, напор и способ добиваться своего",
    "Юпитер": "рост, знания, возможности и расширение горизонтов",
    "Сатурн": "дисциплина, границы, ответственность и взросление",
    "Уран": "перемены, свобода и нестандартность",
    "Нептун": "мечты, идеалы, воображение и чувствительность",
    "Плутон": "глубокие изменения, сила и психологическая трансформация",
    "Северный узел": "направление развития и символические жизненные задачи",
}

ASPECTS = [
    ("соединение", 0, 8),
    ("секстиль", 60, 5),
    ("квадрат", 90, 7),
    ("тригон", 120, 7),
    ("оппозиция", 180, 8),
]

ASPECT_TEXT = {
    "соединение": "усиливает и объединяет темы двух планет",
    "секстиль": "даёт потенциал, который раскрывается через действие",
    "квадрат": "создаёт внутреннее напряжение и задачу научиться согласовывать разные потребности",
    "тригон": "даёт естественную совместимость и относительно лёгкое проявление качеств",
    "оппозиция": "подчёркивает необходимость баланса между противоположными полюсами",
}

# ============================================================
# VALIDATION / GEOCODING
# ============================================================
def valid_date(value: str):
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def valid_time(value: str):
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        return None


def geocode(place: str):
    geolocator = Nominatim(user_agent="AstroVilki/4.0", timeout=20)
    loc = geolocator.geocode(place, language="ru", exactly_one=True, timeout=20)
    if not loc:
        raise ValueError("Город не найден. Попробуй написать город и страну, например: Чебоксары, Россия")

    lat = float(loc.latitude)
    lon = float(loc.longitude)
    tz_name = TimezoneFinder().timezone_at(lat=lat, lng=lon)
    if not tz_name:
        raise ValueError("Не удалось определить часовой пояс города")

    return {
        "query": place,
        "resolved": loc.address,
        "lat": lat,
        "lon": lon,
        "timezone": tz_name,
    }


def sign_position(longitude: float):
    longitude = longitude % 360.0
    idx = int(longitude // 30)
    degree = longitude % 30
    return {
        "longitude": round(longitude, 4),
        "sign": SIGNS[idx],
        "degree": round(degree, 2),
    }


def angle_distance(a, b):
    diff = abs((a - b) % 360.0)
    return min(diff, 360.0 - diff)


def julian_day(dt_utc: datetime):
    return swe.julday(
        dt_utc.year,
        dt_utc.month,
        dt_utc.day,
        dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600,
        swe.GREG_CAL,
    )

# ============================================================
# NATAL CHART
# ============================================================
def natal_chart(dt_local: datetime, location: dict):
    dt_utc = dt_local.astimezone(timezone.utc)
    jd = julian_day(dt_utc)
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED

    planets = {}
    for name, planet_id in PLANETS:
        result, _ = swe.calc_ut(jd, planet_id, flags)
        pos = sign_position(float(result[0]))
        pos["retrograde"] = bool(result[3] < 0)
        planets[name] = pos

    cusps, ascmc = swe.houses_ex(jd, location["lat"], location["lon"], b"P", 0)
    houses = [float(x) for x in cusps[:12]]

    asc = sign_position(float(ascmc[0]))
    mc = sign_position(float(ascmc[1]))

    aspects = []
    names = list(planets.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            distance = angle_distance(planets[names[i]]["longitude"], planets[names[j]]["longitude"])
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

    for name, value in planets.items():
        lon = value["longitude"]
        house_number = 12
        for i in range(12):
            start = houses[i]
            end = houses[(i + 1) % 12]
            x = lon
            if end < start:
                end += 360
            if x < start:
                x += 360
            if start <= x < end:
                house_number = i + 1
                break
        value["house"] = house_number

    return {
        "planets": planets,
        "houses": [
            {"house": i + 1, **sign_position(houses[i])}
            for i in range(12)
        ],
        "ascendant": asc,
        "mc": mc,
        "aspects": aspects,
    }

# ============================================================
# MATRIX OF DESTINY
# ============================================================
def reduce22(value: int) -> int:
    value = abs(int(value))
    while value > 22:
        value = sum(int(x) for x in str(value))
    return 22 if value == 0 else value


def calculate_matrix(date_str: str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    day = reduce22(d.day)
    month = reduce22(d.month)
    year = reduce22(sum(int(x) for x in str(d.year)))
    core = reduce22(day + month + year)
    return {
        "method": "22-арканная базовая схема",
        "positions": {
            "день": day,
            "месяц": month,
            "год": year,
            "итог": core,
            "день+месяц": reduce22(day + month),
            "месяц+год": reduce22(month + year),
            "год+день": reduce22(year + day),
        },
        "note": "Формулы разных школ Матрицы судьбы отличаются; этот расчёт использует базовую 22-арканную схему.",
    }

# ============================================================
# SOLAR RETURN
# ============================================================
def solar_return(dt_local: datetime, natal_sun_lon: float):
    start = datetime(
        dt_local.year,
        dt_local.month,
        dt_local.day,
        0,
        0,
        tzinfo=dt_local.tzinfo,
    ) - timedelta(days=2)

    # Coarse search, then binary refinement around the closest crossing.
    points = []
    for minutes in range(0, 5 * 24 * 60 + 1, 30):
        cand = start + timedelta(minutes=minutes)
        jd = julian_day(cand.astimezone(timezone.utc))
        sun_lon = float(swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH)[0][0])
        signed = ((sun_lon - natal_sun_lon + 180) % 360) - 180
        points.append((abs(signed), signed, cand))

    _, _, best = min(points, key=lambda x: x[0])
    return {
        "approx_local": best.isoformat(),
        "sun_longitude_target": round(natal_sun_lon, 4),
    }


def build_report(date_str: str, time_str: str, place: str):
    location = geocode(place)
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local_tz = ZoneInfo(location["timezone"])
    local_dt = naive.replace(tzinfo=local_tz)

    natal = natal_chart(local_dt, location)
    matrix = calculate_matrix(date_str)
    solar = solar_return(local_dt, natal["planets"]["Солнце"]["longitude"])

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
# LOCAL BASIC REPORTS
# ============================================================
def basic_natal_text(data):
    chart = data["natal_chart"]
    p = chart["planets"]
    a = chart["ascendant"]
    lines = [
        "🌌 НАТАЛЬНАЯ КАРТА",
        "",
        f"☀️ Солнце: {p['Солнце']['sign']} {p['Солнце']['degree']}°, {p['Солнце']['house']} дом.",
        f"🌙 Луна: {p['Луна']['sign']} {p['Луна']['degree']}°, {p['Луна']['house']} дом.",
        f"⬆️ Асцендент: {a['sign']} {a['degree']}°.",
        "",
        "🪐 ПЛАНЕТЫ",
    ]
    for name, value in p.items():
        retro = " ℞" if value["retrograde"] else ""
        lines.append(
            f"• {name}: {value['sign']} {value['degree']}°, {value['house']} дом{retro}. "
            f"Тема — {PLANET_TEXT[name]}."
        )
    lines += ["", "🔗 АСПЕКТЫ"]
    if chart["aspects"]:
        for x in chart["aspects"]:
            lines.append(
                f"• {x['body1']} — {x['aspect']} — {x['body2']} (орб {x['orb']}°): {ASPECT_TEXT[x['aspect']]}"
            )
    else:
        lines.append("• В заданных орбах значимых аспектов не найдено.")
    return "\n".join(lines) + DISCLAIMER


def basic_matrix_text(data):
    m = data["destiny_matrix"]
    lines = ["🔢 МАТРИЦА СУДЬБЫ", "", "Базовая 22-арканная схема:"]
    for key, value in m["positions"].items():
        lines.append(f"• {key}: {value} аркан")
    lines.append("\n" + m["note"])
    return "\n".join(lines) + DISCLAIMER


def basic_solar_text(data):
    solar = data["solar_return"]
    p = data["natal_chart"]["planets"]
    return (
        "☀️ СОЛЯР\n\n"
        f"Примерный момент солнечного возвращения: {solar['approx_local']}\n"
        f"Натальное Солнце: {p['Солнце']['sign']} {p['Солнце']['degree']}°.\n\n"
        "Солярный период символически рассматривается как годовой цикл, "
        "а подробная интерпретация доступна в платном полном разборе."
        + DISCLAIMER
    )

# ============================================================
# OPENAI DEEP REPORT
# ============================================================
def generate_ai_report(data: dict, mode: str = "full", question: str | None = None) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан")

    client = OpenAI(api_key=OPENAI_API_KEY)

    system = """
Ты — редактор очень глубоких персональных астрологических разборов для Telegram-бота AstroVilki.

Используй ТОЛЬКО расчётные данные из JSON. Никогда не выдумывай положения планет, домов, аспектов или арканов.
Астрологию и Матрицу судьбы описывай как эзотерическую интерпретацию, а не как доказанный научный метод.
Не давай медицинских, юридических или финансовых диагнозов/гарантий.

СТИЛЬ:
- русский язык;
- очень подробный разбор, не поверхностная сводка;
- понятные заголовки и подзаголовки;
- много конкретики именно по данным человека;
- объясняй связь «планета → знак → дом → аспект → жизненная тема»;
- не повторяй одну мысль разными словами ради объёма;
- отделяй факты расчёта от интерпретации словами «символически», «в астрологической традиции», «может проявляться»;
- добавляй сильные стороны, сложности, сценарии проявления и вопросы для саморефлексии.

ДЛЯ ПОЛНОГО РАЗБОРА ОБЯЗАТЕЛЬНО РАСКРОЙ:
1. Главную картину натала.
2. Солнце, Луну, Асцендент.
3. Каждую планету: знак, дом, ретроградность и значение.
4. Все основные аспекты из расчёта.
5. Все 12 домов и их акценты.
6. Отношения: Венера, Марс, 7 дом, управитель 7 дома если его можно определить из данных, 5/8 дома.
7. Деньги: 2/6/8/10 дома и соответствующие планеты.
8. Учёбу, карьеру и профессиональные склонности.
9. Дом, семью и тему самостоятельности.
10. Друзей, окружение, интернет и аудиторию.
11. Психологические паттерны и внутренние противоречия.
12. Матрицу: каждую рассчитанную позицию и её символическую трактовку.
13. Соляр: ASC/MC и все доступные положения соляра; если точные солярные дома не рассчитаны, прямо скажи это и не выдумывай их.
14. Связку натал + матрица + соляр.
15. Практические рекомендации и вопросы для саморефлексии.

Если пользователь задал вопрос о партнёре, ответь на него именно по данным карты, но не обещай конкретное будущее.
"""

    if mode == "natal":
        task = "Сделай максимально глубокий разбор НАТАЛЬНОЙ КАРТЫ: личность, отношения, деньги, учёба, карьера, семья, друзья, психология, планеты, дома, аспекты и итог."
    elif mode == "matrix":
        task = "Сделай максимально глубокий разбор МАТРИЦЫ СУДЬБЫ по всем рассчитанным позициям, отдельно раскрой характер, отношения, деньги, реализацию, ресурсы, тени и уроки."
    elif mode == "solar":
        task = "Сделай максимально глубокий разбор СОЛЯРА: главная тема года, отношения, деньги, работа, учёба, переезд/дом, поездки, друзья, психология, партнёр, риски, периоды и итог. Не выдумывай солярные дома, если их нет в JSON."
    elif question:
        task = f"Ответь на персональный вопрос клиента: «{question}». Сначала коротко дай вывод, затем очень подробно объясни, какие элементы карты поддерживают интерпретацию. Если вопрос про партнёра/знакомство, разберись через 5, 7 и 8 дома, Венеру, Марс, управителя 7 дома и доступные аспекты, не выдумывая отсутствующие данные."
    else:
        task = "Сделай МАКСИМАЛЬНО ГЛУБОКИЙ ПОЛНЫЙ РАЗБОР: натальная карта + Матрица судьбы + соляр. Это платная премиальная версия, поэтому не ограничивайся верхушкой."

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=system,
        input=task + "\n\nРАСЧЁТНЫЕ ДАННЫЕ:\n" + payload,
        reasoning={"effort": "medium"},
    )
    return response.output_text.strip() + DISCLAIMER

# ============================================================
# YOOKASSA
# ============================================================
def create_yookassa_payment(user_id: int):
    if not YOOKASSA_ENABLED:
        raise RuntimeError("ЮKassa не настроена: добавь YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")

    amount = f"{float(FULL_REPORT_PRICE):.2f}"
    payload = {
        "amount": {"value": amount, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "description": "AstroVilki — глубокий полный астрологический разбор",
        "metadata": {"telegram_user_id": str(user_id), "product": "full_report"},
    }
    headers = {
        "Content-Type": "application/json",
        "Idempotence-Key": str(uuid.uuid4()),
    }

    response = requests.post(
        "https://api.yookassa.ru/v3/payments",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"ЮKassa HTTP {response.status_code}: {response.text[:500]}")

    data = response.json()
    confirmation_url = data.get("confirmation", {}).get("confirmation_url")
    if not confirmation_url:
        raise RuntimeError("ЮKassa не вернула ссылку на оплату")

    return {
        "id": data["id"],
        "status": data.get("status"),
        "paid": data.get("paid", False),
        "confirmation_url": confirmation_url,
    }


def get_yookassa_payment(payment_id: str):
    response = requests.get(
        f"https://api.yookassa.ru/v3/payments/{payment_id}",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"ЮKassa HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    return {
        "id": data.get("id"),
        "status": data.get("status"),
        "paid": bool(data.get("paid")),
    }

# ============================================================
# HELPERS
# ============================================================
async def send_long(message, text: str, reply_markup=None):
    text = text or "Пустой ответ."
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
    for i, chunk in enumerate(chunks):
        await message.reply_text(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


async def generate_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, mode: str, question: str | None = None):
    message = update.effective_message
    await message.reply_text("✨ Готовлю подробный персональный разбор…")
    try:
        text = await asyncio.to_thread(generate_ai_report, data, mode, question)
        await send_long(message, text, MENU)
        context.user_data["last_report"] = data
    except Exception as exc:
        log.exception("AI report error")
        await message.reply_text(
            "❌ Не удалось сформировать текст разбора.\n\n"
            f"Техническая причина: {type(exc).__name__}.\n"
            "Проверь OPENAI_API_KEY и доступную квоту API.",
            reply_markup=MENU,
        )

# ============================================================
# START / CONVERSATION
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    payment_status = "🟢 ЮKassa подключена" if YOOKASSA_ENABLED else "🟡 ЮKassa пока не настроена"
    await update.message.reply_text(
        "✨ Привет! Я AstroVilki.\n\n"
        "Я рассчитываю натальную карту, Матрицу судьбы и соляр локально — без Geocult.\n\n"
        f"{payment_status}\n"
        "💎 Глубокий полный разбор — 67 ₽.\n\n"
        "Выбери нужную услугу кнопкой ниже.",
        reply_markup=MENU,
    )


async def begin_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "full"
    await update.message.reply_text("🌌 Напиши дату рождения в формате ДД.ММ.ГГГГ")
    return DATE


async def begin_natal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "natal"
    await update.message.reply_text("🔭 Напиши дату рождения в формате ДД.ММ.ГГГГ")
    return DATE


async def begin_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "matrix"
    await update.message.reply_text("🔢 Напиши дату рождения в формате ДД.ММ.ГГГГ")
    return DATE


async def begin_solar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "solar"
    await update.message.reply_text("☀️ Напиши дату рождения в формате ДД.ММ.ГГГГ")
    return DATE


async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = valid_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Не понял дату. Пример: 21.06.2008")
        return DATE
    context.user_data["birth_date"] = d.strftime("%Y-%m-%d")
    await update.message.reply_text("Теперь напиши точное местное время рождения, например 10:20")
    return TIME


async def time_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = valid_time(update.message.text)
    if not t:
        await update.message.reply_text("❌ Нужен формат ЧЧ:ММ. Например: 10:20")
        return TIME
    context.user_data["birth_time"] = t.strftime("%H:%M")
    await update.message.reply_text("И последнее — город рождения. Например: Чебоксары, Россия")
    return PLACE


async def place_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    place = update.message.text.strip()
    context.user_data["birth_place"] = place
    mode = context.user_data.get("mode", "full")

    await update.message.reply_text("🔄 Рассчитываю координаты, часовой пояс, натальную карту и соляр…")
    try:
        data = await asyncio.to_thread(
            build_report,
            context.user_data["birth_date"],
            context.user_data["birth_time"],
            place,
        )
        context.user_data["pending_report"] = data
    except Exception as exc:
        log.exception("Calculation error")
        await update.message.reply_text(
            "❌ Не получилось автоматически построить расчёт.\n\n"
            f"Техническая причина: {type(exc).__name__}: {exc}",
            reply_markup=MENU,
        )
        return ConversationHandler.END

    if mode == "full":
        if not YOOKASSA_ENABLED:
            await update.message.reply_text(
                "✅ Расчёт готов.\n\n"
                "Но ЮKassa ещё не подключена в переменных окружения Railway.\n"
                "Нужны YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.",
                reply_markup=MENU,
            )
        else:
            await update.message.reply_text(
                "🔮 Расчёт готов!\n\n"
                "💎 Полный глубокий разбор стоит 67 ₽.\n"
                "После подтверждения оплаты я сформирую большой разбор натала + Матрицы + соляра.",
                reply_markup=PAYMENT_MENU,
            )
        return ConversationHandler.END

    if mode == "natal":
        await generate_and_send(update, context, data, "natal")
    elif mode == "matrix":
        await generate_and_send(update, context, data, "matrix")
    elif mode == "solar":
        await generate_and_send(update, context, data, "solar")

    return ConversationHandler.END

# ============================================================
# PAYMENTS
# ============================================================
async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not YOOKASSA_ENABLED:
        await query.message.reply_text(
            "❌ ЮKassa не настроена. Добавь в Railway Variables:\n"
            "YOOKASSA_SHOP_ID\nYOOKASSA_SECRET_KEY\nYOOKASSA_RETURN_URL",
            reply_markup=MENU,
        )
        return

    if not context.user_data.get("pending_report"):
        await query.message.reply_text(
            "Сначала нажми «🌌 Полный разбор — 67 ₽» и введи дату, время и город.",
            reply_markup=MENU,
        )
        return

    try:
        payment = await asyncio.to_thread(create_yookassa_payment, update.effective_user.id)
        context.user_data["payment_id"] = payment["id"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Перейти к оплате 67 ₽", url=payment["confirmation_url"])],
            [InlineKeyboardButton("✅ Я оплатил — проверить", callback_data="check_payment")],
        ])
        await query.message.reply_text(
            "💳 Счёт создан!\n\n"
            "1. Нажми «Перейти к оплате».\n"
            "2. Оплати 67 ₽ на странице ЮKassa.\n"
            "3. Вернись в Telegram и нажми «Я оплатил — проверить».\n\n"
            "После подтверждения бот автоматически начнёт глубокий разбор.",
            reply_markup=keyboard,
        )
    except Exception as exc:
        log.exception("YooKassa create error")
        await query.message.reply_text(
            f"❌ Не удалось создать платёж через ЮKassa.\n\n{type(exc).__name__}: {exc}",
            reply_markup=MENU,
        )


async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    payment_id = context.user_data.get("payment_id")
    data = context.user_data.get("pending_report")
    if not payment_id or not data:
        await query.message.reply_text(
            "❌ Активный платёж не найден. Начни с «🌌 Полный разбор — 67 ₽».",
            reply_markup=MENU,
        )
        return

    try:
        payment = await asyncio.to_thread(get_yookassa_payment, payment_id)
        if payment["paid"] or payment["status"] == "succeeded":
            context.user_data["payment_confirmed"] = True
            await query.message.reply_text("✅ Оплата подтверждена! Начинаю глубокий персональный разбор…")
            await generate_and_send(update, context, data, "full")
            context.user_data.pop("pending_report", None)
            context.user_data.pop("payment_id", None)
        elif payment["status"] == "canceled":
            await query.message.reply_text("❌ Платёж отменён или истёк. Создай новый платёж.", reply_markup=PAYMENT_MENU)
        else:
            await query.message.reply_text(
                "⏳ ЮKassa пока не подтвердила оплату. Подожди несколько секунд и нажми кнопку ещё раз.",
                reply_markup=PAYMENT_MENU,
            )
    except Exception as exc:
        log.exception("YooKassa check error")
        await query.message.reply_text(
            f"❌ Ошибка проверки оплаты: {type(exc).__name__}: {exc}",
            reply_markup=MENU,
        )


async def payment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not YOOKASSA_ENABLED:
        await update.message.reply_text(
            "🟡 ЮKassa пока не настроена.\n\n"
            "В Railway Variables добавь:\n"
            "YOOKASSA_SHOP_ID=твой_shop_id\n"
            "YOOKASSA_SECRET_KEY=твой_secret_key\n"
            "YOOKASSA_RETURN_URL=https://t.me/\n"
            "FULL_REPORT_PRICE_RUB=67.00",
            reply_markup=MENU,
        )
        return

    if not context.user_data.get("pending_report"):
        await update.message.reply_text(
            "💎 Полный глубокий разбор стоит 67 ₽.\n\n"
            "Сначала нажми «🌌 Полный разбор — 67 ₽» и введи данные рождения.",
            reply_markup=MENU,
        )
        return

    await update.message.reply_text("💳 Оплата полного разбора — 67 ₽", reply_markup=PAYMENT_MENU)

# ============================================================
# PROFILE / NAVIGATION
# ============================================================
async def my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("last_report") or context.user_data.get("pending_report")
    if not data:
        await update.message.reply_text("Пока данных нет. Нажми «🌌 Полный разбор — 67 ₽».", reply_markup=MENU)
        return
    birth = data["birth"]
    await update.message.reply_text(
        "👤 Твои данные:\n\n"
        f"Дата: {birth['date']}\n"
        f"Время: {birth['time']}\n"
        f"Город: {birth['place']}\n"
        f"Часовой пояс: {birth['timezone']}\n\n"
        "Можно снова запустить нужный раздел через меню.",
        reply_markup=MENU,
    )


async def back_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Главное меню 🌙", reply_markup=MENU)

# ============================================================
# MAIN
# ============================================================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🌌 Полный разбор — 67 ₽$"), begin_full),
            MessageHandler(filters.Regex(r"^🔭 Натальная карта$"), begin_natal),
            MessageHandler(filters.Regex(r"^🔢 Матрица судьбы$"), begin_matrix),
            MessageHandler(filters.Regex(r"^☀️ Соляр$"), begin_solar),
        ],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_received)],
            PLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, place_received)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(create_payment, pattern=r"^create_payment$"))
    app.add_handler(CallbackQueryHandler(check_payment, pattern=r"^check_payment$"))
    app.add_handler(CallbackQueryHandler(back_menu, pattern=r"^back_menu$"))
    app.add_handler(MessageHandler(filters.Regex(r"^💳 Оплата / полный разбор$"), payment_menu))
    app.add_handler(MessageHandler(filters.Regex(r"^👤 Мои данные$"), my_data))

    log.info("AstroVilki started. YooKassa=%s OpenAI=%s model=%s", YOOKASSA_ENABLED, bool(OPENAI_API_KEY), OPENAI_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
