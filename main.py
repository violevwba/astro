import os
import re
import logging
import sqlite3
import uuid
import asyncio
import json
import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import swisseph as swe
from timezonefinder import TimezoneFinder

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, PreCheckoutQueryHandler, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrovilki")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ========================= ASTROVILKI PRO =========================
ASTROVILKI_PRO_PAYMENT = "astrovilki_pro_67_rub_yookassa"
PRO_PRICE_RUB = "67.00"
PRO_DB = os.environ.get("ASTROVILKI_PRO_DB", "astrovilki_pro.sqlite3")

# ЮKassa. Добавь эти переменные в Railway:
# YOOKASSA_SHOP_ID
# YOOKASSA_SECRET_KEY
# YOOKASSA_RETURN_URL (необязательно; по умолчанию ведёт в Telegram)
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.environ.get("YOOKASSA_RETURN_URL", "https://t.me/").strip()

# Старый вариант СБП оставлен для совместимости, но основной PRO теперь через ЮKassa.
SBP_PAYMENT_URL = os.environ.get("SBP_PAYMENT_URL", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
SBP_AUTO_APPROVE = os.environ.get("SBP_AUTO_APPROVE", "false").lower() == "true"

# ========================= ОБЯЗАТЕЛЬНАЯ ПОДПИСКА НА КАНАЛ =========================
# Юзернейм канала (с собакой) для проверки через Bot API.
# ВАЖНО: бот должен быть добавлен в канал как АДМИНИСТРАТОР, иначе
# Telegram не даст боту узнать, подписан ли пользователь.
REQUIRED_CHANNEL_USERNAME = os.environ.get("REQUIRED_CHANNEL_USERNAME", "@astrovilkaa").strip()
REQUIRED_CHANNEL_URL = os.environ.get("REQUIRED_CHANNEL_URL", "https://t.me/astrovilkaa").strip()
# Если проверка отключена (REQUIRE_SUBSCRIPTION=false), гейт не показывается вообще.
REQUIRE_SUBSCRIPTION = os.environ.get("REQUIRE_SUBSCRIPTION", "true").lower() == "true"



def pro_db_init():
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pro_users "
            "(user_id INTEGER PRIMARY KEY, paid_at TEXT NOT NULL, charge_id TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def pro_is_paid(user_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        row = conn.execute(
            "SELECT 1 FROM pro_users WHERE user_id=?",
            (int(user_id),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def pro_grant(user_id, charge_id=""):
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pro_users(user_id, paid_at, charge_id) "
            "VALUES (?, ?, ?)",
            (int(user_id), datetime.now(timezone.utc).isoformat(), charge_id)
        )
        conn.commit()
    finally:
        conn.close()


def yookassa_pending_init():
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS yookassa_pending "
            "(user_id INTEGER PRIMARY KEY, payment_id TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def yookassa_pending_set(user_id, payment_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO yookassa_pending(user_id, payment_id, created_at) VALUES (?, ?, ?)",
            (int(user_id), str(payment_id), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def yookassa_pending_get(user_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        row = conn.execute(
            "SELECT payment_id FROM yookassa_pending WHERE user_id=?",
            (int(user_id),)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def yookassa_pending_delete(user_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute("DELETE FROM yookassa_pending WHERE user_id=?", (int(user_id),))
        conn.commit()
    finally:
        conn.close()


def yookassa_pending_all():
    conn = sqlite3.connect(PRO_DB)
    try:
        return conn.execute("SELECT user_id, payment_id FROM yookassa_pending").fetchall()
    finally:
        conn.close()


async def yookassa_create_payment(user_id):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError(
            "Не настроена ЮKassa. Добавь YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в Railway Variables."
        )

    payload = {
        "amount": {"value": PRO_PRICE_RUB, "currency": "RUB"},
        "capture": True,
        "description": "AstroVilki PRO — глубокий астрологический разбор",
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL
        },
        "metadata": {
            "astrovilki_user_id": str(user_id)
        }
    }

    headers = {
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.yookassa.ru/v3/payments",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers=headers,
            json=payload
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"ЮKassa вернула HTTP {response.status_code}: {response.text[:500]}"
        )

    data = response.json()
    payment_id = data.get("id")
    confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")

    if not payment_id or not confirmation_url:
        raise RuntimeError("ЮKassa не вернула ссылку на оплату.")

    yookassa_pending_set(user_id, payment_id)
    return payment_id, confirmation_url


async def yookassa_get_payment(payment_id):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError(
            "Не настроена ЮKassa. Добавь YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в Railway Variables."
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"https://api.yookassa.ru/v3/payments/{payment_id}",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"ЮKassa вернула HTTP {response.status_code}: {response.text[:500]}"
        )

    return response.json()


async def yookassa_check_and_activate(user_id):
    payment_id = yookassa_pending_get(user_id)
    if not payment_id:
        return False, "У тебя пока нет созданного платежа. Нажми «Оплатить 67 ₽»."

    data = await yookassa_get_payment(payment_id)
    status = data.get("status")

    if status == "succeeded":
        amount = ((data.get("amount") or {}).get("value") or "")
        if str(amount) != PRO_PRICE_RUB:
            return False, "⚠️ Получен платёж с другой суммой. PRO не активирован."
        pro_grant(user_id, f"YOOKASSA:{payment_id}")
        yookassa_pending_delete(user_id)
        return True, "🎉 Оплата подтверждена!\n\n💎 PRO разблокирован."
    if status == "canceled":
        yookassa_pending_delete(user_id)
        return False, "❌ Платёж отменён. Можно создать новый платёж."
    return False, "⏳ Платёж пока не подтверждён. Если ты уже оплатил(а), подожди немного и нажми «Проверить оплату» ещё раз."


async def yookassa_auto_activation(context):
    """Проверяет ожидающие платежи и автоматически выдаёт PRO после успешной оплаты."""
    for user_id, payment_id in yookassa_pending_all():
        try:
            if pro_is_paid(user_id):
                yookassa_pending_delete(user_id)
                continue

            data = await yookassa_get_payment(payment_id)
            status = data.get("status")
            amount = ((data.get("amount") or {}).get("value") or "")

            if status == "succeeded" and str(amount) == PRO_PRICE_RUB:
                pro_grant(user_id, f"YOOKASSA:{payment_id}")
                yookassa_pending_delete(user_id)

                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🎉 Оплата успешно получена!\n\n"
                        "💎 PRO активирован автоматически.\n"
                        "Теперь доступны все глубокие разборы и ответы на вопросы.\n\n"
                        "Выбирай раздел:"
                    ),
                    reply_markup=pro_menu(),
                )

            elif status == "canceled":
                yookassa_pending_delete(user_id)

        except Exception:
            log.exception("Automatic YooKassa check failed for user %s", user_id)


async def yookassa_auto_loop(app):
    """Фоновая проверка ЮKassa каждые 5 секунд."""
    while True:
        await yookassa_auto_activation(type("Ctx", (), {"bot": app.bot})())
        await asyncio.sleep(5)


def sbp_pending_init():
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sbp_pending "
            "(user_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def sbp_pending_set(user_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sbp_pending(user_id, created_at) VALUES (?, ?)",
            (int(user_id), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def sbp_pending_delete(user_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        conn.execute("DELETE FROM sbp_pending WHERE user_id=?", (int(user_id),))
        conn.commit()
    finally:
        conn.close()


def sbp_pending_has(user_id):
    conn = sqlite3.connect(PRO_DB)
    try:
        return conn.execute(
            "SELECT 1 FROM sbp_pending WHERE user_id=?", (int(user_id),)
        ).fetchone() is not None
    finally:
        conn.close()


def sbp_payment_menu():
    if SBP_PAYMENT_URL:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Открыть оплату СБП — 67 ₽", url=SBP_PAYMENT_URL)],
            [InlineKeyboardButton("✅ Я оплатил(а)", callback_data="pro_paid")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="pro_open")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 СБП пока не настроена", callback_data="pro_sbp_info")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="pro_open")]
    ])


def pro_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Отношения", callback_data="pro_rel"),
         InlineKeyboardButton("💘 Партнёр", callback_data="pro_partner")],
        [InlineKeyboardButton("📍 Где встречу?", callback_data="pro_where"),
         InlineKeyboardButton("💍 Брак", callback_data="pro_marriage")],
        [InlineKeyboardButton("💰 Деньги", callback_data="pro_money"),
         InlineKeyboardButton("💼 Карьера", callback_data="pro_career")],
        [InlineKeyboardButton("🎓 Учёба", callback_data="pro_study"),
         InlineKeyboardButton("🏠 Переезд", callback_data="pro_move")],
        [InlineKeyboardButton("✈️ Поездки", callback_data="pro_travel"),
         InlineKeyboardButton("👥 Друзья", callback_data="pro_friends")],
        [InlineKeyboardButton("🧠 Психология", callback_data="pro_psych"),
         InlineKeyboardButton("☀️ Соляр PRO", callback_data="pro_solar")],
        [InlineKeyboardButton("🔢 Матрица PRO", callback_data="pro_matrix"),
         InlineKeyboardButton("🪐 Планеты", callback_data="pro_planets")],
        [InlineKeyboardButton("🏠 Дома", callback_data="pro_houses"),
         InlineKeyboardButton("🔗 Аспекты", callback_data="pro_aspects")],
        [InlineKeyboardButton("📅 Периоды", callback_data="pro_periods"),
         InlineKeyboardButton("⭐ Итог", callback_data="pro_summary")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="back_main")],
    ])


def pro_sales_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить PRO — 67 ₽", callback_data="pro_buy")],
        [InlineKeyboardButton("🔄 Проверить оплату", callback_data="pro_check_payment")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
    ])


def pro_sales_text():
    return (
        "💎 <b>ASTROVILKI PRO — 67 ₽</b>\n\n"
        "<b>Не короткий гороскоп, а подробное исследование твоих данных.</b>\n"
        "После оплаты открывается расширенное меню, где каждый раздел разбирается отдельно.\n\n"
        "━━━━━━━━━━━━━━\n"
        "❤️ <b>ОТНОШЕНИЯ</b>\n"
        "• 5, 7 и 8 дома\n• Венера, Марс, Луна и управители\n• сценарии притяжения и конфликтов\n• сильные стороны пары и возможные слабые места\n• что тебе важно в близости и какие границы сохранять\n\n"
        "💘 <b>ПАРТНЁР</b> — подробный символический портрет подходящего типа человека и динамики отношений.\n\n"
        "📍 <b>ГДЕ ВСТРЕТИШЬ ПАРТНЁРА?</b> — не выдуманный адрес, а анализ 5/7/11 домов, их управителей и повторяющихся жизненных сред: учёба, работа, друзья, интернет, поездки и т. д.\n\n"
        "💍 <b>БРАК</b> — показатели долгосрочного партнёрства, зрелости отношений и того, что помогает союзу держаться.\n\n"
        "━━━━━━━━━━━━━━\n"
        "💰 <b>ДЕНЬГИ</b> — 2/6/8/10 дома, Юпитер, Сатурн, Плутон: личный доход, работа, общие ресурсы, финансовые привычки и точки роста.\n\n"
        "💼 <b>КАРЬЕРА</b> — сильные профессиональные направления, рабочий стиль, долгосрочная реализация и потенциальные ограничения.\n\n"
        "🎓 <b>УЧЁБА</b> — 3 и 9 дома, Меркурий и Юпитер: как легче учиться, какие знания полезнее развивать и где раскрывается интерес.\n\n"
        "🏠 <b>ПЕРЕЕЗД</b> — 4 и 9 дома, Луна, Уран и другие показатели перемен.\n"
        "✈️ <b>ПОЕЗДКИ</b> — дальние поездки, иностранная среда и расширение горизонтов.\n"
        "👥 <b>ДРУЗЬЯ</b> — 11 дом, окружение, сообщества и социальные связи.\n"
        "🧠 <b>ПСИХОЛОГИЯ</b> — эмоциональные реакции, внутренние противоречия, границы и повторяющиеся паттерны.\n\n"
        "━━━━━━━━━━━━━━\n"
        "☀️ <b>СОЛЯР PRO</b> — карта конкретного года: ASC, MC, планеты по домам, главные темы, отношения, деньги, учёба, дом, карьера и последовательность периодов.\n\n"
        "🔢 <b>МАТРИЦА PRO</b> — предназначение, точка сердца, таланты, любовный и денежный каналы, кармический хвост, родовые линии, ресурсы и зоны напряжения.\n\n"
        "🪐 <b>ПЛАНЕТЫ</b> • 🏠 <b>12 ДОМОВ</b> • 🔗 <b>АСПЕКТЫ</b> • 📅 <b>ПЕРИОДЫ</b> • ⭐ <b>ИТОГ</b>\n\n"
        "━━━━━━━━━━━━━━\n"
        "💳 <b>Стоимость: 67 ₽</b>\n"
        "🔐 Оплата проходит через ЮKassa. После успешной оплаты PRO активируется автоматически.\n\n"
        "<i>Астрология и матрица судьбы — эзотерические системы для саморефлексии, а не научные методы прогнозирования.</i>"
    )

tf = TimezoneFinder()

DATE, TIME, CITY = range(3)

DISCLAIMER = (
    "\n\n⚠️ Это эзотерическая интерпретация для саморефлексии, "
    "а не научный метод. Не используй её как единственное основание "
    "для медицинских, юридических или финансовых решений."
)

_DIVIDER_CHARS = set("⸻━ ")


def render_report_html(text):
    """Превращает обычный текст отчёта в красивый HTML для Telegram:
    заголовки разделов — жирным, разделители и предупреждения — курсивом,
    остальное — как есть. Каждая подсветка целиком лежит в одной строке,
    поэтому текст можно потом безопасно резать по строкам на части."""
    out_lines = []
    for line in text.split("\n"):
        stripped = line.strip()

        if not stripped:
            out_lines.append(line)
            continue

        # Строки-разделители вида "⸻" или "━━━━━━━━━━━━━━━━"
        if set(stripped) <= _DIVIDER_CHARS:
            out_lines.append(f"<i>{html.escape(stripped)}</i>")
            continue

        escaped = html.escape(line)

        # Предупреждения — всегда курсивом, независимо от регистра
        if stripped.startswith("⚠️"):
            out_lines.append(f"<i>{escaped}</i>")
            continue

        # Заголовки разделов: строка почти целиком заглавными буквами
        # (например "🌌 НАТАЛЬНАЯ КАРТА" или "❤️ ОТНОШЕНИЯ — ПОЛНЫЙ РАЗБОР")
        letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", stripped)
        if len(letters) >= 3 and letters == letters.upper() and len(stripped) <= 80:
            out_lines.append(f"<b>{escaped}</b>")
            continue

        out_lines.append(escaped)

    return "\n".join(out_lines)


async def send_html_chunks(message, text, limit=3500):
    """Форматирует текст в HTML и отправляет его частями, не разрезая
    строки (а значит и HTML-теги) посередине."""
    formatted = render_report_html(text)
    buf = []
    length = 0
    for line in formatted.split("\n"):
        add_len = len(line) + 1
        if buf and length + add_len > limit:
            await message.reply_text("\n".join(buf), parse_mode="HTML")
            buf = [line]
            length = add_len
        else:
            buf.append(line)
            length += add_len
    if buf:
        await message.reply_text("\n".join(buf), parse_mode="HTML")

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
    "Овен": "живую инициативность и прямоту — тут человек скорее начнёт действовать, чем будет долго готовиться, идёт туда, где другие ещё сомневаются, ценит скорость, честность и результат больше долгих разговоров, а нетерпеливость и вспыльчивость идут рядом как обратная сторона этой же энергии",
    "Телец": "спокойную устойчивость и практичность — опора здесь важнее суеты, начатое доводится до конца, ценятся комфорт, вкус и качество вещей вокруг, а упрямство и тяжёлая реакция на перемены — цена этой же стабильности",
    "Близнецы": "живой ум и гибкость мышления — интересно сразу многое, легко находится общий язык с разными людьми, новая информация схватывается на лету, а разбросанность и поверхностность в отдельных темах — обратная сторона этой лёгкости",
    "Рак": "эмоциональную глубину и заботу о близких — тонко считывается настроение вокруг, дом и семья становятся точкой опоры, помнятся мелочи, важные для других, а ранимость и склонность закрываться при малейшей угрозе — защита этой же чувствительности",
    "Лев": "яркую уверенность и творческое самовыражение — важно, чтобы тебя видели и ценили именно такой(-им), есть дар вдохновлять и вести за собой, щедрость на тепло, а острая потребность в признании и обида на невнимание — обратная сторона той же щедрости",
    "Дева": "аналитический ум и внимание к деталям — замечается то, что другие пропускают, работа делается на совесть, ценится порядок и польза, а самокритичность и тревога из-за мелочей — цена этой добросовестности",
    "Весы": "чувство баланса и тягу к партнёрству — важна гармония вокруг, видятся обе стороны любой ситуации, ценится красота и справедливость, а нерешительность и желание всем угодить — обратная сторона этого стремления к равновесию",
    "Скорпион": "внутреннюю интенсивность и проницательность — видна суть за фасадом, не терпится поверхностность, есть способность к сильной привязанности и глубокой личной трансформации, а недоверчивость и тяга к контролю — защита этой же уязвимой глубины",
    "Стрелец": "тягу к свободе и широкий взгляд на вещи — важно расти, узнавать новое и видеть горизонт дальше привычного, есть природный оптимизм и прямолинейная честность, а нетерпение к деталям и рутине — цена этой масштабности",
    "Козерог": "целеустремлённость и умение нести ответственность — есть готовность работать на далёкий результат, не ожидая быстрых наград, ценится структура и репутация, а строгость к себе и сложность в проявлении уязвимости — обратная сторона этой дисциплины",
    "Водолей": "независимость и оригинальность мышления — важно оставаться собой и не подстраиваться под чужие рамки, нестандартные решения находятся раньше других, а внутренняя отстранённость и сложность в близости — цена этой же свободы",
    "Рыбы": "тонкую чувствительность и воображение — считывается то, что не проговорено словами, легко возникает сопереживание другим, есть тяга к творчеству и мечте, а склонность растворяться в чужих переживаниях и уходить от реальности — обратная сторона этой же чуткости",
}

HOUSE_TEXT = {
    1: "то, как ты выглядишь в глазах других с первых секунд знакомства — твою самостоятельность, темп жизни и естественную манеру начинать что-то новое; это первое впечатление, которое складывается ещё до слов",
    2: "личные деньги и то, как они зарабатываются и тратятся, вещи и ресурсы, дающие опору, а ещё — самоценность: сколько, по внутреннему ощущению, человек стоит и заслуживает",
    3: "повседневное общение, учёбу, короткие поездки, переписку и разговоры, а ещё то, как устроено мышление — насколько быстро приходят мысли и легко ли объяснить их другим",
    4: "дом, семью, корни и то, что происходит за закрытой дверью, где никто не видит: детские сценарии, отношения с родителями и внутреннее чувство «у меня есть тыл»",
    5: "романтику, влюблённость, детей, творчество и всё, что делается ради удовольствия, а не по необходимости — зону, где разрешается быть спонтанным(-ой) и ярким(-ой)",
    6: "рутину, работу руками и головой каждый день, здоровье, привычки и распорядок — то, насколько устойчиво выстроена повседневная жизнь",
    7: "партнёрство один на один: брак, серьёзные отношения, деловые союзы, а иногда и открытых соперников — зеркало, в котором человек видит себя через другого",
    8: "общие деньги, долги, наследство, интимную близость и глубокие кризисы, которые меняют изнутри — всё, что нельзя полностью контролировать",
    9: "высшее образование, дальние путешествия, философию и мировоззрение — поиск смысла и того, во что верится, когда привычная картина мира перестаёт устраивать",
    10: "карьеру, статус, репутацию и то дело, за которое узнают в обществе — вершину публичной реализации",
    11: "друзей, сообщества, единомышленников и долгосрочные мечты — людей, с которыми строится будущее, а не просто проводится время",
    12: "уединение, скрытые процессы, завершение циклов и то, что человек прячет даже от самого себя — изнанку личности, которая проявляется в снах, интуиции и моментах одиночества",
}

PLANET_TEXT = {
    "Солнце": "ядро личности и волю — то, ради чего вообще стоит проявляться, куда направлена жизненная сила и в какой роли человек чувствует себя настоящим, а не играющим чужую роль",
    "Луна": "эмоциональные реакции на автомате, то, что успокаивает в стрессе, и глубинную потребность в безопасности, сформированную ещё в детстве",
    "Меркурий": "стиль мышления, речи и обучения новому — скорость соображения, манеру говорить и то, как информация извне обрабатывается и усваивается",
    "Венера": "то, кого и что человек выбирает в отношениях и в жизни вообще, его вкус, ощущение красоты и то, через что приходит удовольствие",
    "Марс": "энергию действия — как человек добивается своего, проявляет злость, отстаивает границы и находит мотивацию двигаться вперёд, когда трудно",
    "Юпитер": "зону везения и роста, то, во что верится и где есть готовность рисковать ради расширения возможностей",
    "Сатурн": "страхи, ограничения и зону, где приходится взрослеть через дисциплину — но именно там со временем появляется настоящая, не показная уверенность",
    "Уран": "потребность в переменах и внутренний бунт против рамок — зону, где человек неожидан(-нна) даже для самого себя",
    "Нептун": "мечты, идеализацию и ту часть жизни, где реальность немного размывается — вдохновение и иллюзии здесь идут рука об руку",
    "Плутон": "самые глубокие трансформации — то, что должно умереть внутри, чтобы родилось новое, и откуда приходит настоящая внутренняя сила",
    "Северный узел": "направление, куда стоит расти в этой жизни, даже если поначалу это неудобно и непривычно — зону развития, а не зону комфорта",
}

ASPECT_TEXT = {
    "соединение": "две планеты работают почти как одна сила — их темы настолько сливаются, что сложно отделить одно от другого",
    "секстиль": "лёгкая возможность, которая раскрывается только через собственное действие — сама по себе она не реализуется",
    "квадрат": "внутреннее трение и задача научиться примирять две разные потребности — самый энергозатратный, но и самый развивающий тип аспекта",
    "тригон": "природный талант и лёгкость — здесь многое получается почти без усилий, и потому этим ресурсом легко пользоваться не задумываясь",
    "оппозиция": "два полюса, между которыми приходится искать баланс, часто через отношения с другими людьми — маятник, качающийся, пока не найдена середина",
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
        [InlineKeyboardButton("💎 PRO — глубокий разбор 67 ⭐", callback_data="pro_open")],
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
         InlineKeyboardButton("❤️ Любовный канал", callback_data="matrix_relationships")],
        [InlineKeyboardButton("💰 Денежный канал", callback_data="matrix_money"),
         InlineKeyboardButton("💼 Реализация", callback_data="matrix_career")],
        [InlineKeyboardButton("🎁 Таланты", callback_data="matrix_talents"),
         InlineKeyboardButton("🩶 Кармический хвост", callback_data="matrix_karma")],
        [InlineKeyboardButton("🌳 Родовые линии", callback_data="matrix_ancestry"),
         InlineKeyboardButton("🧠 Характер", callback_data="matrix_character")],
        [InlineKeyboardButton("⚠️ Зоны напряжения", callback_data="matrix_shadows"),
         InlineKeyboardButton("🎁 Ресурсы", callback_data="matrix_resources")],
        [InlineKeyboardButton("⭐ Итог", callback_data="matrix_summary"),
         InlineKeyboardButton("⬅️ Главное меню", callback_data="back_main")],
    ])



async def is_subscribed(bot, user_id: int) -> bool:
    """Проверяет подписку пользователя на обязательный канал.

    Возвращает True, если:
    - проверка отключена (REQUIRE_SUBSCRIPTION=false), либо
    - пользователь состоит в канале (не 'left' и не 'kicked'), либо
    - проверку не удалось выполнить технически (например, бот ещё не
      добавлен в канал администратором) — чтобы ошибка настройки не
      заблокировала всех пользователей бота.
    """
    if not REQUIRE_SUBSCRIPTION:
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_USERNAME, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        log.warning(f"Не удалось проверить подписку на {REQUIRED_CHANNEL_USERNAME}: {e}")
        return True


def subscribe_gate_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Перейти в канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался, проверить", callback_data="check_sub")],
    ])


SUBSCRIBE_GATE_TEXT = (
    "🌙 <b>Добро пожаловать в AstroVilki!</b>\n\n"
    "Чтобы пользоваться ботом, подпишись на наш канал 👇\n\n"
    "1️⃣ Нажми «Перейти в канал» и подпишись\n"
    "2️⃣ Вернись сюда и нажми «Я подписался, проверить»\n\n"
    "После этого доступ откроется автоматически ✨"
)


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


async def send_start_flow(message, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветствие и начинает сбор данных рождения (шаг 1 из 3)."""
    context.user_data.clear()
    await message.reply_text(
        "🌙 <b>Добро пожаловать в AstroVilki!</b>\n\n"
        "Здесь ты можешь получить персональный разбор по <b>дате, точному времени и месту рождения</b>.\n\n"
        "🔭 <b>Натальная карта</b> — характер, отношения, деньги, учёба, карьера, дом, друзья, планеты, дома и аспекты.\n"
        "☀️ <b>Соляр</b> — символическая карта твоего личного года и его главных тем.\n"
        "🔢 <b>Матрица судьбы</b> — эзотерический разбор числовых энергий даты рождения.\n"
        "💎 <b>PRO за 67 ₽</b> — те же темы, но в разы глубже: каждая планета, дом и аспект разобраны отдельно.\n\n"
        "📌 Сначала введи данные рождения. Это нужно, чтобы расчёт был именно твоим.\n\n"
        "<b>Шаг 1 из 3</b>\n📅 Напиши дату рождения в формате <b>ДД.ММ.ГГГГ</b>.\n"
        "Например: <i>21.06.2008</i>",
        parse_mode="HTML"
    )
    return DATE


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            SUBSCRIBE_GATE_TEXT,
            reply_markup=subscribe_gate_menu(),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    return await send_start_flow(update.message, context)


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not await is_subscribed(context.bot, user_id):
        await query.answer("❌ Пока не вижу подписку. Подпишись и попробуй снова.", show_alert=True)
        return ConversationHandler.END

    await query.answer("✅ Подписка подтверждена!")
    try:
        await query.edit_message_text("✅ Подписка подтверждена! Открываю доступ...")
    except Exception:
        pass
    return await send_start_flow(query.message, context)


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


def reduce_arcanum(n):
    """Сводит любое число к диапазону 1–22 (22 остаётся без изменений, 0 → 22)."""
    while n > 22:
        n = sum(int(x) for x in str(n))
    return 22 if n == 0 else n


# Полная информация по 22 арканам матрицы судьбы: имя, ключевое слово
# и короткая формулировка сути энергии (свет / тень).
ARCANA_INFO = {
    1:  {"name": "Маг",           "key": "воля и старт",        "light": "умение брать инициативу и запускать новое своими руками", "shadow": "манипуляции и игра «на публику» вместо реального действия"},
    2:  {"name": "Жрица",         "key": "интуиция",            "light": "тонкое чутьё, наблюдательность, связь с внутренним знанием", "shadow": "уход в иллюзии и нежелание смотреть на факты"},
    3:  {"name": "Императрица",   "key": "творчество и изобилие","light": "созидание, забота, умение создавать красоту и уют",        "shadow": "избыточный контроль или, наоборот, разбросанность"},
    4:  {"name": "Император",     "key": "структура и власть",  "light": "порядок, ответственность, умение выстраивать систему",     "shadow": "жёсткость, желание продавить всё по-своему"},
    5:  {"name": "Иерофант",      "key": "традиции и наставничество","light": "передача знаний, опора на опыт и проверенные правила", "shadow": "слепое следование чужим авторитетам"},
    6:  {"name": "Влюблённые",    "key": "выбор и отношения",   "light": "искренний выбор из ценностей, а не из страха одиночества", "shadow": "нерешительность, метания между вариантами"},
    7:  {"name": "Колесница",     "key": "движение к цели",     "light": "дисциплина, скорость, умение держать курс несмотря ни на что","shadow": "гонка ради гонки, выгорание"},
    8:  {"name": "Справедливость","key": "баланс и честность",  "light": "трезвая оценка, умение отвечать за свои решения",          "shadow": "излишняя самокритика или, наоборот, желание всех судить"},
    9:  {"name": "Отшельник",     "key": "поиск и мудрость",    "light": "способность искать ответы внутри себя, не в толпе",        "shadow": "самоизоляция и избегание людей"},
    10: {"name": "Колесо Фортуны","key": "циклы и удача",       "light": "умение ловить момент и использовать благоприятные повороты","shadow": "пассивное ожидание, что всё случится само"},
    11: {"name": "Сила",          "key": "внутренняя сила",     "light": "мягкая, но устойчивая власть над собой и обстоятельствами", "shadow": "подавление себя или окружающих"},
    12: {"name": "Повешенный",    "key": "смена угла зрения",   "light": "готовность посмотреть на ситуацию иначе, терпение",        "shadow": "затянутая жертвенность, застревание в ожидании"},
    13: {"name": "Смерть",        "key": "трансформация",       "light": "смелость завершать то, что изжило себя, и идти дальше",    "shadow": "страх перемен, цепляние за прошлое"},
    14: {"name": "Умеренность",   "key": "гармония меры",       "light": "умение соединять разное без перекосов, постепенность",    "shadow": "крайности: то всё, то ничего"},
    15: {"name": "Дьявол",        "key": "желания и притяжение","light": "яркая харизма и умение управлять материальными желаниями", "shadow": "зависимости и попадание в чужой контроль"},
    16: {"name": "Башня",         "key": "резкое обновление",   "light": "смелость разрушить то, что построено на гнилом фундаменте","shadow": "хаос и разрушение ради самого разрушения"},
    17: {"name": "Звезда",        "key": "вдохновение и надежда","light": "вера в будущее, умение вдохновлять себя и других",        "shadow": "витание в облаках без опоры на реальность"},
    18: {"name": "Луна",          "key": "эмоции и страхи",     "light": "глубокая чувствительность и работа с подсознанием",        "shadow": "тревожность и бег по замкнутым сценариям"},
    19: {"name": "Солнце",        "key": "радость и свет",      "light": "открытость, ясность, лёгкая самореализация",               "shadow": "эгоцентризм и обесценивание чужих заслуг"},
    20: {"name": "Суд",           "key": "переоценка",          "light": "честный итог пройденного этапа и осознанный новый старт", "shadow": "самобичевание за прошлые решения"},
    21: {"name": "Мир",           "key": "завершение цикла",    "light": "чувство целостности, умение довести дело до конца",       "shadow": "страх ставить точку, бесконечное затягивание"},
    22: {"name": "Шут",           "key": "свобода и новый путь","light": "лёгкость, готовность к нестандартным решениям",            "shadow": "безответственность и бегство от обязательств"},
}



# ================= STRUCTURED INTERPRETATION ENGINE =================
# Единый формат: смысл → плюс → минус → проработка → сюжеты → маркеры.
# Все формулировки вероятностные: эзотерическая символика не гарантирует события.

PLANET_PLUS={
'Солнце':'самостоятельность, уверенное проявление и ответственность за собственные цели','Луна':'эмоциональная устойчивость, забота о себе и умение прямо просить о поддержке','Меркурий':'ясное мышление, коммуникация, обучение и способность договариваться','Венера':'взаимность, самоценность, вкус и умение строить отношения без погони','Марс':'решительность, здоровые границы, действие и способность доводить начатое','Юпитер':'рост через знания, связи, расширение возможностей и разумное масштабирование','Сатурн':'дисциплина, система, выдержка и создание долгосрочной опоры','Уран':'новаторство, свобода, технологии и способность обновлять устаревшее','Нептун':'воображение, эмпатия, творчество и тонкое восприятие','Плутон':'глубокая трансформация, управление ресурсами и внутренняя сила','Северный узел':'готовность развивать непривычные качества и выходить за пределы автоматизма'}
PLANET_MINUS={
'Солнце':'зависимость от признания, жизнь ради чужой оценки или отказ от собственных целей','Луна':'эмоциональные качели, закрытость, зависимость от реакции других или контроль','Меркурий':'накручивание, поспешные выводы, конфликтность или избегание важных разговоров','Венера':'страх отвержения, идеализация, зависимость от внимания или выбор недоступных людей','Марс':'импульсивность, подавленная злость, борьба вместо диалога или перегрузка','Юпитер':'переоценка возможностей, обещания без расчёта и ожидание удачи вместо действий','Сатурн':'страх ошибки, чрезмерная жёсткость к себе, откладывание жизни и перегрузка','Уран':'резкие решения без плана и разрушение стабильности ради самого изменения','Нептун':'идеализация, самообман, размытые границы и уход от фактов','Плутон':'борьба за контроль, крайности и разрушительные попытки изменить всё сразу','Северный узел':'возврат к привычному комфорту и страх нового выбора'}
PLANET_ACTION={
'Солнце':'сформулировать одну личную цель на 30 дней и каждую неделю делать для неё измеримый шаг','Луна':'ежедневно называть свою эмоцию и потребность; отдельно планировать сон и восстановление','Меркурий':'перед важным разговором выписывать факты, позицию и вопрос вместо догадок','Венера':'проверять взаимность: кто вкладывается, кто договаривается и кто уважает границы','Марс':'выбирать один приоритет в день и направлять энергию в действие, а конфликт сначала формулировать словами','Юпитер':'выбрать один навык роста и составить измеримый план обучения или масштабирования','Сатурн':'ввести одну систему — бюджет, расписание, чек-лист или дедлайн — минимум на 4 недели','Уран':'тестировать новое маленькими шагами, а крупные перемены делать только с запасным планом','Нептун':'отделять желание от факта и фиксировать важные обещания и договорённости письменно','Плутон':'определить одну область гиперконтроля и заменить контроль ясными границами и зоной ответственности','Северный узел':'сделать одно непривычное, но полезное действие, соответствующее долгосрочной цели'}
PLANET_EVENTS={
'Солнце':'новая роль, самостоятельное решение, заметный проект или усиление личной ответственности','Луна':'изменение бытовой опоры, семейный сюжет, новый режим или переоценка потребностей','Меркурий':'обучение, переписка, документы, переговоры или информационный проект','Венера':'знакомство, укрепление союза, творческий проект или пересмотр ценностей','Марс':'старт проекта, конкуренция, защита границ, спорт или конфликт, требующий решения','Юпитер':'обучение, поездка, новый круг людей, расширение проекта или международная/онлайн-возможность','Сатурн':'новая ответственность, долгий проект, оформление обязательств или проверка устойчивости','Уран':'неожиданное предложение, смена формата работы/учёбы или новая технология','Нептун':'творческий период, вдохновение, идеализация человека/проекта и необходимость проверить ожидания','Плутон':'завершение старого этапа, перераспределение ресурсов или глубокая перестройка','Северный узел':'новая среда, непривычная роль или задача, которая заставляет расти'}
PLANET_MARKERS={k:v for k,v in {
'Солнце':'хочется больше самостоятельности, признания и права решать за себя','Луна':'повторяются темы дома, заботы, эмоциональной безопасности и привычных реакций','Меркурий':'становится больше разговоров, сообщений, обучения, документов и решений','Венера':'усиливается внимание к любви, внешности, ценностям, деньгам и взаимности','Марс':'возрастает темп, инициативность, раздражительность или необходимость защищать границы','Юпитер':'появляются новые люди, знания, предложения и желание расширить рамки','Сатурн':'жизнь требует дисциплины, сроков, ответственности и проверки реальностью','Уран':'старый формат начинает раздражать, а новые варианты появляются неожиданно','Нептун':'чувства и фантазии становятся сильнее, поэтому особенно важны факты','Плутон':'возникает ощущение, что прежний способ жить или работать больше не подходит','Северный узел':'появляется шанс сделать непривычный выбор, расширяющий опыт'}.items()}

HOUSE_PLUS={1:'самостоятельность и смелость начинать',2:'устойчивый личный доход и здоровая самоценность',3:'гибкость мышления и сильная коммуникация',4:'чувство тыла и зрелые семейные границы',5:'творчество, романтика и живое самовыражение',6:'дисциплина, полезные привычки и качество работы',7:'партнёрство на равных и умение договариваться',8:'глубокое доверие и финансовая прозрачность',9:'образование, путешествия и широкий взгляд',10:'профессиональный рост и сильная репутация',11:'сильное окружение, аудитория и коллективные проекты',12:'восстановление, завершение старого и внутреннее освобождение'}
HOUSE_MINUS={1:'зависимость от впечатления окружающих или импульсивность',2:'финансовые качели, обесценивание навыков или импульсивные траты',3:'информационный шум, споры и поверхностность',4:'семейный контроль или жизнь по чужому сценарию',5:'драматизация, потребность во внимании или страх проявляться',6:'перфекционизм, перегрузка или хаос в режиме',7:'созависимость, борьба за власть или страх одиночества',8:'ревность, контроль, непрозрачные общие деньги или крайности',9:'догматизм, бегство в идеи или обучение без применения',10:'зависимость от статуса, выгорание или страх ошибки',11:'давление группы, сравнение себя с другими и размытые цели',12:'изоляция, избегание проблем и накопление непрожитого напряжения'}
HOUSE_ACTION={1:'показывать инициативу и строить образ вокруг реальных целей',2:'вести учёт денег и развивать навыки монетизации',3:'учиться, писать, задавать вопросы и проверять информацию',4:'создавать устойчивую базу и отделять свои решения от семейных ожиданий',5:'давать место творчеству, свиданиям и удовольствию',6:'выстраивать режим, процессы и рабочие привычки',7:'проверять взаимность и уважать границы обеих сторон',8:'прозрачно обсуждать общие деньги, доверие и обязательства',9:'расширять образование и проверять убеждения практикой',10:'строить репутацию через качество, ответственность и результат',11:'развивать полезные связи и долгосрочные проекты',12:'оставлять время на восстановление и завершение дел'}
HOUSE_EVENTS={1:'новый образ, самостоятельный старт или смена роли',2:'новый источник дохода, изменение бюджета или крупная покупка',3:'курс, переписка, документы или короткая поездка',4:'переезд, ремонт, семейный разговор или создание своего пространства',5:'роман, хобби, праздник или творческий проект',6:'новая работа, клиент, режим или изменение процессов',7:'знакомство, договор, партнёрство или изменение статуса отношений',8:'общие деньги, обязательство, разговор о доверии или кризис с последующей перестройкой',9:'университет, поездка, иностранная среда или международный проект',10:'новая должность, клиент, публичность или изменение профессионального направления',11:'новые друзья, интернет-комьюнити или коллективный проект',12:'завершение цикла, пауза, отпуск или закрытие старого проекта'}
HOUSE_MARKERS={1:'чаще думаешь о себе, внешнем образе и новых стартах',2:'на первый план выходят деньги, цена труда и чувство опоры',3:'становится больше общения, обучения и переписки',4:'сильнее ощущаются дом, семья и личное пространство',5:'возрастают романтика, творчество и желание проявляться',6:'жизнь требует режима и порядка',7:'становятся заметнее отношения один на один',8:'поднимаются темы доверия, близости и общих ресурсов',9:'появляется тяга к знаниям и поездкам',10:'возрастает внимание к карьере и статусу',11:'расширяется круг общения и совместные планы',12:'возникает потребность остановиться и завершить незакрытое'}

ASPECT_PLUS={'соединение':'две темы могут работать как единый двигатель','секстиль':'доступный ресурс, который раскрывается через действие','квадрат':'высокая мотивация к развитию через преодоление противоречий','тригон':'природная лёгкость и талант','оппозиция':'способность видеть обе стороны и искать баланс'}
ASPECT_MINUS={'соединение':'слияние тем без дистанции','секстиль':'пассивность и упущенные возможности','квадрат':'раздражение и повторяющийся внутренний конфликт','тригон':'недооценка лёгкого таланта или отсутствие развития','оппозиция':'качели, проекции и зависимость от чужой реакции'}
ASPECT_ACTION={'соединение':'объединить две темы в одну конструктивную цель','секстиль':'создать регулярное действие, которое раскрывает возможность','квадрат':'найти правило, где обе потребности получают место','тригон':'превратить природную лёгкость в конкретный навык','оппозиция':'замечать маятник и учиться договариваться вместо выбора крайности'}
ASPECT_EVENTS={'соединение':'одновременное усиление двух жизненных тем','секстиль':'предложение или шанс, реализующийся после действия','квадрат':'конфликт, дедлайн или препятствие, требующее нового подхода','тригон':'лёгкое продвижение или признание навыка','оппозиция':'ситуация-зеркало через другого человека или обстоятельство'}
ASPECT_MARKERS={'соединение':'две темы постоянно ощущаются вместе','секстиль':'возможности появляются, но их легко пропустить','квадрат':'один и тот же конфликт возвращается','тригон':'что-то получается легко и поэтому недооценивается','оппозиция':'ситуация заставляет искать середину между полюсами'}

def structured_block(title,meaning,plus,minus,actions,events,markers,extra=''):
    return (f'{title}\n🔎 1. ЧТО ОЗНАЧАЕТ\n{meaning}\n\n✨ 2. РЕСУРС — В ПЛЮСЕ\n{plus}\n\n⚠️ 3. ТЕНЕВАЯ СТОРОНА — В МИНУСЕ\n{minus}\n\n🛠 4. КАК ПРОРАБАТЫВАТЬ\n• {actions}\n• Вопрос: «Что в моей жизни сейчас требует именно этой проработки?»\n• Практика: повторяй выбранное действие 7–30 дней.\n\n📖 5. КАНОНИЧНЫЕ ЖИЗНЕННЫЕ СЮЖЕТЫ\n• {events}\n\n👁 6. КАК ПОНЯТЬ, ЧТО ПОКАЗАТЕЛЬ ВКЛЮЧИЛСЯ\n• {markers}' + (f'\n\n{extra}' if extra else ''))

def structured_planet(chart,name,value=None,title_prefix='🪐'):
    p=value or chart['planets'][name]; house=p.get('house')
    meaning=f'{name} отвечает за {PLANET_TEXT[name]}. В {p["sign"]} это проявляется через {SIGN_TEXT[p["sign"]]}, а в {house or "указанной сфере"} доме — прежде всего в теме {HOUSE_TEXT.get(house,"этого показателя")}. '
    extra='↩️ Ретроградность: полезнее давать теме время на пересмотр и внутреннюю работу, прежде чем принимать окончательное решение.' if p.get('retrograde') else ''
    return structured_block(f'{title_prefix} {name} — {p["sign"]} {p["degree"]}°' + (f' • {house} дом' if house else ''),meaning,PLANET_PLUS[name],PLANET_MINUS[name],PLANET_ACTION[name],PLANET_EVENTS[name],PLANET_MARKERS[name],extra)

def structured_house(chart,house_num):
    h=chart['houses'][house_num-1]
    meaning=f'{house_num} дом отвечает за {HOUSE_TEXT[house_num]}. Знак {h["sign"]} задаёт стиль проявления: {SIGN_TEXT[h["sign"]]}.'
    return structured_block(f'🏠 {house_num} ДОМ — {h["sign"]} {h["degree"]}°',meaning,HOUSE_PLUS[house_num],HOUSE_MINUS[house_num],HOUSE_ACTION[house_num],HOUSE_EVENTS[house_num],HOUSE_MARKERS[house_num])

def structured_aspect(a):
    b1,b2,asp=a['body1'],a['body2'],a['aspect']
    meaning=f'{ASPECT_TEXT[asp]}. Здесь встречаются темы {b1} ({PLANET_TEXT.get(b1,b1)}) и {b2} ({PLANET_TEXT.get(b2,b2)}).'
    return structured_block(f'🔗 {b1} — {asp.upper()} — {b2} • орб {a["orb"]}°',meaning,ASPECT_PLUS[asp],ASPECT_MINUS[asp],ASPECT_ACTION[asp],ASPECT_EVENTS[asp],ASPECT_MARKERS[asp])

def structured_arcanum(number,title='🔢 ЭНЕРГИЯ'):
    i=ARCANA_INFO[number]
    actions={1:'вести один проект от идеи до результата',2:'проверять интуицию фактами',3:'создать конкретный творческий продукт',4:'ввести правила и систему ответственности',5:'учиться у наставника, сохраняя критическое мышление',6:'при выборе выписывать ценности и последствия',7:'выбрать измеримую цель и держать курс без выгорания',8:'фиксировать договорённости, деньги и границы',9:'оставлять место одиночеству, не превращая его в изоляцию',10:'замечать возможности, но проверять их цифрами',11:'развивать мягкую силу и устойчивые границы',12:'сменить угол зрения и определить срок для действия',13:'закрывать изжившее себя по одному пункту',14:'ввести умеренный темп и избегать крайностей',15:'составить правила денег, желаний и границ',16:'отделять необходимое обновление от разрушения',17:'перевести мечту в план, портфолио и дедлайны',18:'называть страхи и проверять предположения',19:'делиться результатом и признавать вклад других',20:'подвести итог этапа без самобичевания',21:'доводить начатое до точки',22:'пробовать новое маленькими шагами с учётом ответственности'}[number]
    events={1:'новый проект или лидерская роль',2:'скрытая информация или важный внутренний выбор',3:'творческий/материальный проект',4:'новая ответственность или управление',5:'обучение, наставник или оформление правил',6:'важное знакомство или выбор в отношениях',7:'поездка, соревнование или ускорение проекта',8:'договор, финансовая тема или восстановление баланса',9:'уединение, исследование или самостоятельная работа',10:'поворот обстоятельств или шанс',11:'проверка характера и границ',12:'пауза и смена точки зрения',13:'завершение этапа или сильная перестройка',14:'восстановление баланса и постепенный рост',15:'сильное притяжение, деньги или искушение быстрым результатом',16:'резкая смена планов или обновление фундамента',17:'публичность, творчество или проект мечты',18:'неясная ситуация и необходимость отличить страх от факта',19:'признание, радостное событие или публичный результат',20:'возвращение к незавершённому вопросу и переоценка',21:'завершение большого цикла или выход на новый уровень',22:'новый путь, поездка или необычный проект'}[number]
    marker=f'в жизни повторяется тема «{i["key"]}», влияющая на решения и выбор.'
    return structured_block(f'{title} • {number} — {i["name"]} ({i["key"]})',f'Эта энергия символически описывает тему «{i["key"]}». Её значение зависит от точки матрицы: личность, любовь, деньги, талант, карма или родовая линия.',i['light'],i['shadow'],actions,events,marker)

def solar_structured_planet(solar,name):
    p=solar['planets'][name]; h=p.get('house')
    meaning=f'{name} в соляре показывает, как тема «{PLANET_TEXT[name]}» может быть акцентирована от дня рождения до следующего дня рождения. Знак {p["sign"]} задаёт стиль: {SIGN_TEXT[p["sign"]]}. ' + (f'Дом {h}: {HOUSE_TEXT[h]}.' if h else 'Дом в этом расчёте не определён.')
    return structured_block(f'☀️ {name} — {p["sign"]} {p["degree"]}°' + (f' • {h} дом' if h else ''),meaning,PLANET_PLUS[name],PLANET_MINUS[name],PLANET_ACTION[name],PLANET_EVENTS[name],PLANET_MARKERS[name],'📌 Это символическая тема года, а не гарантированное событие.')


def arcanum_line(number, prefix=""):
    return structured_arcanum(number, prefix.rstrip(" —"))

def matrix_22(date_str):
    """Расширенный расчёт матрицы судьбы: личный квадрат, точка сердца,
    таланты, любовный и денежный каналы, кармический хвост и родовые линии."""
    d = datetime.strptime(date_str, "%d.%m.%Y")
    r = reduce_arcanum

    # --- личный (диагональный) квадрат ---
    A = r(d.day)                                   # день — портрет, как видят другие
    B = r(d.month)                                  # месяц — духовный талант
    C = r(sum(int(x) for x in str(d.year)))          # год — материальный талант/потенциал
    D = r(A + B + C)                                 # предназначение / нижняя вершина
    E = r(A + B + C + D)                             # точка сердца — зона внутреннего комфорта

    # --- средние точки сторон личного квадрата = углы родового квадрата ---
    F = r(A + B)   # верх — духовный талант
    G = r(B + C)   # право — материальный талант / вход денег
    H = r(C + D)   # низ
    I = r(A + D)   # лево — вход в отношения

    # --- кармический хвост (3 энергии) ---
    k2 = r(D + E)
    k3 = r(k2 + D)
    karma = [D, k2, k3]

    # --- любовный канал (вход в отношения → сценарий → карма) ---
    l2 = r(I + E)
    l3 = r(l2 + I)
    love = [I, l2, l3]

    # --- денежный канал (вход денег → сценарий → карма) ---
    m2 = r(G + E)
    m3 = r(m2 + G)
    money = [G, m2, m3]

    # --- таланты ---
    talents = [F, G, r(F + G)]

    # --- родовые линии (вертикаль = отцовская, горизонталь = материнская) ---
    paternal = r(F + H)
    maternal = r(G + I)

    # --- зоны напряжения / задачи ("проблемные" точки — диагонали квадрата) ---
    tension = [r(A + C), r(B + D)]

    return {
        # старые ключи — оставлены для совместимости
        "day": A, "month": B, "year": C, "core": E,
        # новые расширенные точки
        "purpose": D,
        "heart": E,
        "talent_spirit": F,
        "talent_material": G,
        "edge_bottom": H,
        "edge_left": I,
        "karma": karma,
        "love": love,
        "money": money,
        "talents": talents,
        "paternal": paternal,
        "maternal": maternal,
        "tension": tension,
    }


def planet_sentence(name, value):
    return structured_planet({"planets": {name: value}}, name, value=value)

def natal_report(chart):
    p = chart["planets"]
    a = chart["angles"]
    div = "⸻"

    lines = [
        "🌌 НАТАЛЬНАЯ КАРТА",
        "",
        "Расчёт выполнен локально через Swiss Ephemeris — без воды, чётко по делу.",
        f"📅 Дата: {chart['birth']['date']}",
        f"🕐 Время: {chart['birth']['local_time']} ({chart['birth']['timezone']})",
        f"📍 Место: {chart['location']['display_name']}",
        "",
        div,
        "✨ ОСНОВНОЙ ПОРТРЕТ",
        "",
        f"☀️ Солнце в {p['Солнце']['sign']}",
        f"Это твоё ядро — {SIGN_TEXT[p['Солнце']['sign']]}",
        "",
        f"☽ Луна в {p['Луна']['sign']}",
        f"Эмоционально тебе важно: {SIGN_TEXT[p['Луна']['sign']]}",
        "",
        f"🌅 Асцендент в {a['Асцендент']['sign']}",
        f"Первое впечатление о тебе строится через {SIGN_TEXT[a['Асцендент']['sign']]}",
        "",
        "Это три главных слоя личности: то, что ты хочешь реализовать (Солнце), "
        "то, что тебе эмоционально необходимо (Луна), и то, как ты входишь в "
        "мир (Асцендент). Их важно рассматривать вместе, а не по отдельности.",
        "",
        div,
        "🪐 ПЛАНЕТЫ",
        "",
    ]

    lines.append("\n\n".join(planet_sentence(name, value) for name, value in p.items()))

    lines.extend(["", div, "🔗 ОСНОВНЫЕ АСПЕКТЫ", ""])

    if chart["aspects"]:
        aspect_blocks = []
        for x in chart["aspects"]:
            aspect_blocks.append(
                f"• {x['body1']} — {x['aspect']} — {x['body2']} (орб {x['orb']}°)\n"
                f"   {ASPECT_TEXT[x['aspect']]}"
            )
        lines.append("\n\n".join(aspect_blocks))
    else:
        lines.append("Значимых аспектов по установленным орбам не найдено.")

    lines.extend(["", div, "🏠 ДОМА", ""])

    house_blocks = []
    for h in chart["houses"]:
        house_blocks.append(
            f"• {h['house']} дом в {h['sign']}\n"
            f"   Сфера: {HOUSE_TEXT[h['house']]}\n"
            f"   Стиль проявления: {SIGN_TEXT[h['sign']]}"
        )
    lines.append("\n\n".join(house_blocks))

    lines.extend([
        "",
        div,
        "❤️ ОТНОШЕНИЯ",
        "",
        f"Главный показатель здесь — Венера в {p['Венера']['sign']} и "
        f"{p['Венера']['house']} доме: {SIGN_TEXT[p['Венера']['sign']]}",
        "",
        f"Дополнительно смотри 7 дом — он начинается в {chart['houses'][6]['sign']}, "
        f"а значит тема партнёрства окрашена через {SIGN_TEXT[chart['houses'][6]['sign']]}",
        "",
        div,
        "💰 ДЕНЬГИ",
        "",
        f"Для финансовой темы особенно важен 2 дом — он начинается в "
        f"{chart['houses'][1]['sign']}: {SIGN_TEXT[chart['houses'][1]['sign']]}",
        "",
        "Дополнительно стоит учитывать планеты во 2, 6, 8 и 10 домах — они "
        "показывают, откуда приходят деньги и насколько устойчиво они держатся.",
        "",
        div,
        "🎓 УЧЁБА И РЕАЛИЗАЦИЯ",
        "",
        f"Для обучения и расширения горизонтов важен 9 дом "
        f"({chart['houses'][8]['sign']}), а для карьеры — 10 дом "
        f"({chart['houses'][9]['sign']}).",
        "",
        f"Меркурий в {p['Меркурий']['sign']} описывает стиль мышления через "
        f"{SIGN_TEXT[p['Меркурий']['sign']]}",
        "",
        div,
        "⭐ ИТОГ",
        "",
        f"Тебе важно опираться на связку Солнце ({p['Солнце']['sign']}) — "
        f"Луна ({p['Луна']['sign']}) — Асцендент ({a['Асцендент']['sign']}): "
        "это и есть твой устойчивый центр, к которому стоит возвращаться "
        "в любой спорной ситуации.",
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
    div = "⸻"

    lines = [
        "☀️ СОЛЯР",
        "",
        "Соляр рассчитывается локально — по возвращению Солнца к его "
        "натальному положению. Это карта конкретного года твоей жизни, "
        "а не карта характера.",
        f"🕐 Момент соляра примерно: {solar['date']} {solar['time_utc']} UTC",
        "",
        div,
        "🌅 ГЛАВНЫЕ ТОЧКИ ГОДА",
        "",
        f"Асцендент соляра — {solar['asc']['sign']} {solar['asc']['degree']}°",
        f"Стиль года через ASC: {SIGN_TEXT[solar['asc']['sign']]}",
        "",
        f"MC соляра — {solar['mc']['sign']} {solar['mc']['degree']}°",
        f"Карьерный вектор через MC: {SIGN_TEXT[solar['mc']['sign']]}",
        "",
        div,
        "🪐 ПОЛОЖЕНИЯ ПЛАНЕТ ГОДА",
        "",
    ]

    planet_blocks = [
        f"• {name} — {value['sign']} {value['degree']}°\n   {SIGN_TEXT[value['sign']]}"
        for name, value in p.items()
    ]
    lines.append("\n\n".join(planet_blocks))

    lines.extend([
        "",
        div,
        "🎯 ГЛАВНАЯ ТЕМА ГОДА",
        "",
        f"Солнце года находится в {p['Солнце']['sign']} — самореализация в "
        f"этом году может разворачиваться через {SIGN_TEXT[p['Солнце']['sign']]}",
        "",
        "Это очень важно: главную тему года лучше определять не по одной "
        "планете, а по тому, что повторяется сразу в нескольких точках карты.",
        "",
        div,
        "❤️ ОТНОШЕНИЯ",
        "",
        f"Венера в {p['Венера']['sign']} подчёркивает в романтической сфере: "
        f"{SIGN_TEXT[p['Венера']['sign']]}",
        "",
        div,
        "💰 ДЕНЬГИ И РАБОТА",
        "",
        f"Юпитер в {p['Юпитер']['sign']} символически усиливает "
        f"{SIGN_TEXT[p['Юпитер']['sign']]}",
        "",
        f"Сатурн в {p['Сатурн']['sign']} напоминает о дисциплине и "
        "долгосрочном подходе — год лучше строить не на рывках, а на системе.",
        "",
        div,
        "📚 УЧЁБА И ПЕРЕМЕНЫ",
        "",
        f"Меркурий в {p['Меркурий']['sign']} подчёркивает "
        f"{SIGN_TEXT[p['Меркурий']['sign']]}",
        "",
        f"Уран в {p['Уран']['sign']} связан с темой перемен и свободы — "
        "именно здесь в течение года может появляться желание что-то резко поменять.",
        "",
        div,
        "⭐ ИТОГ",
        "",
        f"Тебе важно сохранять связку ASC ({solar['asc']['sign']}) — "
        f"MC ({solar['mc']['sign']}) — Солнце года ({p['Солнце']['sign']}): "
        "это и есть главный вектор, вокруг которого разворачивается год.",
    ])

    return "\n".join(lines) + DISCLAIMER


def matrix_report(date_str):
    m = matrix_22(date_str)
    A, B, C = ARCANA_INFO[m["day"]], ARCANA_INFO[m["month"]], ARCANA_INFO[m["year"]]
    D, E = ARCANA_INFO[m["purpose"]], ARCANA_INFO[m["heart"]]
    div = "⸻"

    lines = [
        "🔢 МАТРИЦА СУДЬБЫ",
        "",
        "Всё по делу и без воды — ниже полный расклад по дате рождения: "
        "личный квадрат, точка сердца, таланты, любовный и денежный каналы, "
        "кармический хвост и родовые линии.",
        "",
        div,
        "🧩 ЛИЧНЫЙ КВАДРАТ",
        "",
        arcanum_line(m["day"], "📅 День — "),
        "",
        arcanum_line(m["month"], "🌙 Месяц — "),
        "",
        arcanum_line(m["year"], "📆 Год — "),
        "",
        arcanum_line(m["purpose"], "🎯 Предназначение (день+месяц+год) — "),
        "",
        div,
        "💗 ТОЧКА ПОД СЕРДЦЕМ",
        "",
        "Это центр всей матрицы — то, что ощущается как «настоящий я», "
        "и зона внутреннего комфорта, к которой хочется возвращаться.",
        "",
        arcanum_line(m["heart"]),
        "",
        div,
        "🎁 ТАЛАНТЫ",
        "",
        arcanum_line(m["talents"][0], "🌤 Духовный талант — "),
        "",
        arcanum_line(m["talents"][1], "💼 Материальный талант — "),
        "",
        arcanum_line(m["talents"][2], "🔗 Как соединить оба таланта — "),
        "",
        div,
        "❤️ ЛЮБОВНЫЙ КАНАЛ",
        "",
        arcanum_line(m["love"][0], "🚪 Вход в отношения — "),
        "",
        arcanum_line(m["love"][1], "🎬 Сценарий, который повторяется — "),
        "",
        arcanum_line(m["love"][2], "🔁 Любовная карма — "),
        "",
        div,
        "💰 ДЕНЕЖНЫЙ КАНАЛ",
        "",
        arcanum_line(m["money"][0], "🚪 Как деньги входят в жизнь — "),
        "",
        arcanum_line(m["money"][1], "🎬 Финансовый сценарий — "),
        "",
        arcanum_line(m["money"][2], "🔁 Денежная карма — "),
        "",
        div,
        "🩶 КАРМИЧЕСКИЙ ХВОСТ",
        "",
        "Три нижние энергии — незакрытые задачи, из-за которых чаще всего "
        "повторяются одни и те же ситуации.",
        "",
        arcanum_line(m["karma"][0], "1️⃣ "),
        "",
        arcanum_line(m["karma"][1], "2️⃣ "),
        "",
        arcanum_line(m["karma"][2], "3️⃣ "),
        "",
        div,
        "🌳 РОДОВЫЕ ЛИНИИ",
        "",
        arcanum_line(m["paternal"], "👨 По папиной линии — "),
        "",
        arcanum_line(m["maternal"], "👩 По маминой линии — "),
        "",
        div,
        "⚡ ЗОНЫ НАПРЯЖЕНИЯ",
        "",
        "Диагонали квадрата — точки, где чаще всего возникает внутренний конфликт "
        "«хочу одно, а получаю другое».",
        "",
        arcanum_line(m["tension"][0]),
        "",
        arcanum_line(m["tension"][1]),
        "",
        div,
        "✨ ИТОГ",
        "",
        f"Центр карты — {E['name']} ({E['key']}). Именно к этому состоянию "
        f"стоит возвращаться в спорных ситуациях. День рождения даёт "
        f"{A['name']} ({A['key']}) как основу характера, а предназначение "
        f"через {D['name']} ({D['key']}) показывает, куда в итоге ведёт весь путь.",
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
    div = "⸻"

    if section == "personality":
        return section_title("🌞 ЛИЧНОСТЬ — ПОЛНЫЙ РАЗБОР") + (
            f"☀️ Солнце — {p['Солнце']['sign']}, {p['Солнце']['house']} дом\n"
            f"Это сочетает {SIGN_TEXT[p['Солнце']['sign']]}\n"
            f"с темами: {HOUSE_TEXT[p['Солнце']['house']]}\n\n"
            f"{div}\n\n"
            f"☽ Луна — {p['Луна']['sign']}, {p['Луна']['house']} дом\n"
            f"Эмоциональная реакция связана с {SIGN_TEXT[p['Луна']['sign']]}\n"
            f"и сферой: {HOUSE_TEXT[p['Луна']['house']]}\n\n"
            f"{div}\n\n"
            f"🌅 Асцендент — {a['Асцендент']['sign']}\n"
            f"Внешний стиль: {SIGN_TEXT[a['Асцендент']['sign']]}\n\n"
            f"{div}\n\n"
            "✨ Главное\n\n"
            "Три уровня личности лучше рассматривать вместе: то, что ты хочешь "
            "реализовать (Солнце), то, что тебе эмоционально необходимо (Луна), "
            "и то, как ты входишь в мир (Асцендент). Именно эта связка и есть "
            "твой устойчивый центр."
        )

    if section == "relationships":
        v, m = p["Венера"], p["Марс"]
        return section_title("❤️ ОТНОШЕНИЯ — ПОЛНЫЙ РАЗБОР") + (
            f"7 дом — {h[6]['sign']}\n"
            f"Тема партнёрства окрашена через {SIGN_TEXT[h[6]['sign']]}\n\n"
            f"{div}\n\n"
            f"♀ Венера — {v['sign']}, {v['house']} дом\n"
            f"В любви важны: {SIGN_TEXT[v['sign']]}\n"
            f"Проявление идёт через: {HOUSE_TEXT[v['house']]}\n\n"
            f"{div}\n\n"
            f"♂ Марс — {m['sign']}, {m['house']} дом\n"
            f"Способ действовать и добиваться близости: {SIGN_TEXT[m['sign']]}\n\n"
            f"{div}\n\n"
            "🔎 На что смотреть в отношениях\n\n"
            "• насколько совпадают ценности;\n"
            "• есть ли уважение к границам;\n"
            "• можно ли открыто говорить о деньгах и планах;\n"
            "• не становится ли партнёр центром всей жизни;\n"
            "• сохраняется ли личная самостоятельность.\n\n"
            "Это очень важно: карта описывает символические предпочтения, "
            "а не гарантирует знакомство или конкретный исход отношений."
        )

    if section == "money":
        return section_title("💰 ДЕНЬГИ — ПОЛНЫЙ РАЗБОР") + (
            f"2 дом — {h[1]['sign']}\n"
            f"Личный доход и собственные ресурсы. Стиль: {SIGN_TEXT[h[1]['sign']]}\n\n"
            f"{div}\n\n"
            f"8 дом — {h[7]['sign']}\n"
            "Общие деньги, поддержка, вложения и финансовые связи. "
            f"Стиль: {SIGN_TEXT[h[7]['sign']]}\n\n"
            f"{div}\n\n"
            f"♃ Юпитер — {p['Юпитер']['sign']}, {p['Юпитер']['house']} дом\n"
            f"Расширение через {SIGN_TEXT[p['Юпитер']['sign']]}\n\n"
            f"{div}\n\n"
            f"♇ Плутон — {p['Плутон']['sign']}, {p['Плутон']['house']} дом\n"
            "Глубокая перестройка отношения к ресурсам и контролю.\n\n"
            f"{div}\n\n"
            "🔑 Практический смысл\n\n"
            "Развивать собственный доход, считать расходы, не смешивать чувства "
            "и финансовые обещания, постепенно повышать ценность своих навыков."
        )

    if section == "study":
        return section_title("🎓 УЧЁБА — ПОЛНЫЙ РАЗБОР") + (
            f"3 дом — {h[2]['sign']}\n"
            "Повседневное обучение и информация.\n\n"
            f"9 дом — {h[8]['sign']}\n"
            "Высшее образование и расширение горизонтов.\n\n"
            f"{div}\n\n"
            f"☿ Меркурий — {p['Меркурий']['sign']}, {p['Меркурий']['house']} дом\n"
            f"Мышление и обучение: {SIGN_TEXT[p['Меркурий']['sign']]}\n\n"
            f"{div}\n\n"
            "Сильнее всего может раскрывать потенциал то, где нужно "
            "анализировать, объяснять, писать, общаться, работать с информацией "
            "или осваивать цифровые инструменты."
        )

    if section == "career":
        return section_title("💼 КАРЬЕРА — ПОЛНЫЙ РАЗБОР") + (
            f"6 дом — {h[5]['sign']}\n"
            "Ежедневная работа и навыки.\n\n"
            f"10 дом — {h[9]['sign']}\n"
            "Статус и профессиональная реализация.\n\n"
            f"{div}\n\n"
            f"MC — {a['MC']['sign']}\n"
            f"Публичное направление через {SIGN_TEXT[a['MC']['sign']]}\n\n"
            f"{div}\n\n"
            f"♄ Сатурн — {p['Сатурн']['sign']}, {p['Сатурн']['house']} дом\n"
            "Тема дисциплины, ответственности и долгосрочного результата.\n\n"
            f"{div}\n\n"
            "Лучший сценарий — не распыляться, а превращать интерес в измеримый "
            "навык, портфолио и реальный опыт."
        )

    if section == "home":
        return section_title("🏠 ДОМ, СЕМЬЯ И ПЕРЕЕЗД") + (
            f"4 дом — {h[3]['sign']}\n"
            f"{SIGN_TEXT[h[3]['sign']]}\n\n"
            f"{div}\n\n"
            "4 дом показывает тему дома, семьи и личной базы. Для переезда "
            "важно дополнительно смотреть 9 и 10 дома, Луну и управителей.\n\n"
            "Это очень важно: если переезд планируется, разделяй символику "
            "карты и реальное решение — жильё, бюджет, документы, учёба/работа "
            "и безопасность должны проверяться отдельно."
        )

    if section == "psychology":
        return section_title("🧠 ПСИХОЛОГИЯ — ПОЛНЫЙ РАЗБОР") + (
            f"☽ Луна — {p['Луна']['sign']}\n"
            f"{SIGN_TEXT[p['Луна']['sign']]}\n\n"
            f"{div}\n\n"
            f"♇ Плутон — {p['Плутон']['sign']}, {p['Плутон']['house']} дом\n"
            f"Тема: {HOUSE_TEXT[p['Плутон']['house']]}\n\n"
            f"{div}\n\n"
            f"♄ Сатурн — {p['Сатурн']['sign']}\n"
            f"Развитие через {SIGN_TEXT[p['Сатурн']['sign']]}\n\n"
            f"{div}\n\n"
            "Тебе важно наблюдать, где решения идут из собственного желания, "
            "а где — из страха, давления или ожиданий окружающих."
        )

    if section == "friends":
        return section_title("👥 ДРУЗЬЯ И ОКРУЖЕНИЕ") + (
            f"11 дом — {h[10]['sign']}\n"
            f"{SIGN_TEXT[h[10]['sign']]}\n\n"
            f"{div}\n\n"
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
            f"   Сфера: {HOUSE_TEXT[x['house']]}\n"
            f"   Проявление: {SIGN_TEXT[x['sign']]}"
            for x in h
        )

    if section == "aspects":
        blocks = [section_title("🔗 АСПЕКТЫ")]
        for x in chart["aspects"]:
            blocks.append(
                f"• {x['body1']} {x['aspect']} {x['body2']} — орб {x['orb']}°\n"
                f"   {ASPECT_TEXT[x['aspect']]}"
            )
        return "\n\n".join(blocks)

    return natal_report(chart)


def solar_expanded(chart, solar, section):
    solar=pro_solar_house_assign(solar); p,h=solar["planets"],solar["houses"]
    if section=="main":
        blocks=[section_title("🌞 СОЛЯР — ПОЛНЫЙ РАЗБОР ГОДА"),"📌 Соляр описывает темы периода от дня рождения до следующего дня рождения. Это не гарантия событий.",f"🌅 ASC: {solar['asc']['sign']} {solar['asc']['degree']}° — {SIGN_TEXT[solar['asc']['sign']]}",f"🎯 MC: {solar['mc']['sign']} {solar['mc']['degree']}° — {SIGN_TEXT[solar['mc']['sign']]}","🪐 ПЛАНЕТЫ ГОДА"]
        blocks += [solar_structured_planet(solar,n) for n in p]
        blocks.append("⭐ Главная тема года определяется повторениями домов, планет, ASC и MC. Периоды повышенного внимания — это окна для активности, а не точные даты.")
        return "\n\n".join(blocks)
    if section=="relationships": return section_title("❤️ ОТНОШЕНИЯ — СОЛЯР")+solar_structured_planet(solar,"Венера")+"\n\n"+solar_structured_planet(solar,"Марс")+"\n\n"+structured_house({"houses":h},7)+"\n\n💚 Ориентир: взаимность, последовательность и уважение границ."
    if section=="money": return section_title("💰 ДЕНЬГИ — СОЛЯР")+structured_house({"houses":h},2)+"\n\n"+structured_house({"houses":h},8)+"\n\n"+solar_structured_planet(solar,"Юпитер")+"\n\n"+solar_structured_planet(solar,"Сатурн")+"\n\nПрактический фокус: доход, расходы, резерв и устойчивость решений."
    if section=="study": return section_title("🎓 УЧЁБА — СОЛЯР")+structured_house({"houses":h},3)+"\n\n"+structured_house({"houses":h},9)+"\n\n"+solar_structured_planet(solar,"Меркурий")+"\n\n"+solar_structured_planet(solar,"Юпитер")
    if section=="career": return section_title("💼 РАБОТА И КАРЬЕРА — СОЛЯР")+structured_house({"houses":h},6)+"\n\n"+structured_house({"houses":h},10)+"\n\n"+solar_structured_planet(solar,"Сатурн")
    if section=="home": return section_title("🏠 ПЕРЕЕЗД И ДОМ — СОЛЯР")+structured_house({"houses":h},4)+"\n\n"+structured_house({"houses":h},9)+"\n\n"+solar_structured_planet(solar,"Луна")+"\n\n"+solar_structured_planet(solar,"Уран")+"\n\n📌 Реальный переезд проверяй по бюджету, жилью, документам и работе/учёбе."
    if section=="travel": return section_title("✈️ ПОЕЗДКИ — СОЛЯР")+structured_house({"houses":h},9)+"\n\n"+solar_structured_planet(solar,"Юпитер")+"\n\n"+solar_structured_planet(solar,"Уран")
    if section=="friends": return section_title("👥 ДРУЗЬЯ — СОЛЯР")+structured_house({"houses":h},11)+"\n\n"+solar_structured_planet(solar,"Уран")
    if section=="psychology": return section_title("🧠 ПСИХОЛОГИЧЕСКАЯ ТЕМА — СОЛЯР")+solar_structured_planet(solar,"Луна")+"\n\n"+solar_structured_planet(solar,"Плутон")+"\n\n⚠️ Это не диагностика."
    if section=="partner": return section_title("💘 ПАРТНЁР — СИМВОЛИЧЕСКИЙ ОБРАЗ")+structured_house({"houses":h},7)+"\n\n"+solar_structured_planet(solar,"Венера")+"\n\n"+solar_structured_planet(solar,"Марс")+"\n\nТипаж — диапазон качеств, а не точный портрет человека."
    if section=="risks": return section_title("⚠️ РИСКИ — СОЛЯР")+"1. Идеализация → проверять фактами.\n2. Импульсивные деньги → считать бюджет.\n3. Перегрузка → ставить пределы.\n4. Зависимость от оценки → возвращаться к своим критериям.\n5. Резкие перемены → иметь запасной план."
    if section=="periods": return section_title("📅 ПЕРИОДЫ ПОВЫШЕННОГО ВНИМАНИЯ")+"1 — личность\n2 — деньги\n3 — общение/обучение\n4 — дом\n5 — любовь/творчество\n6 — работа\n7 — партнёрство\n8 — близость/общие ресурсы\n9 — учёба/поездки\n10 — карьера\n11 — друзья/интернет\n12 — завершение\n\n📌 Это символическая последовательность, а не календарь гарантированных событий."
    return section_title("⭐ ИТОГ СОЛЯРА")+"Главная задача: выбрать 1–2 повторяющиеся темы года и перевести их в реальные цели.\n\nЧто уже работает в плюс: использовать сильные качества показателей осознанно.\n\nЧто может уводить в минус: автоматические реакции, идеализация и импульсивность.\n\nПроработка на 30 дней: одна привычка + одна граница + один измеримый результат.\n\n⚠️ Соляр — символическая модель личного года."


def matrix_expanded(date_str, section):
    m=matrix_22(date_str)
    if section=="core":
        return section_title("🌟 ГЛАВНАЯ ЭНЕРГИЯ — ПОЛНЫЙ РАЗБОР") + "\n\n".join([structured_arcanum(m["day"],"📅 ДЕНЬ"),structured_arcanum(m["month"],"🌙 МЕСЯЦ"),structured_arcanum(m["year"],"📆 ГОД"),structured_arcanum(m["purpose"],"🎯 ПРЕДНАЗНАЧЕНИЕ"),structured_arcanum(m["heart"],"💗 ТОЧКА СЕРДЦА")])
    groups={"relationships":("love","❤️ ЛЮБОВНЫЙ КАНАЛ",["🚪 ВХОД","🎬 СЦЕНАРИЙ","🔁 ЗАДАЧА"]),"money":("money","💰 ДЕНЕЖНЫЙ КАНАЛ",["🚪 ВХОД","🎬 СЦЕНАРИЙ","🔁 ЗАДАЧА"]),"talents":("talents","🎁 ТАЛАНТЫ",["🌤 ДУХОВНЫЙ","💼 МАТЕРИАЛЬНЫЙ","🔗 СВЯЗКА"]),"karma":("karma","🩶 КАРМИЧЕСКИЙ ХВОСТ",["1️⃣","2️⃣","3️⃣"]),"tension":("tension","⚠️ ЗОНЫ НАПРЯЖЕНИЯ",["1️⃣","2️⃣"])}
    if section in groups:
        key,title,labels=groups[section]; vals=m[key]
        return section_title(title)+"\n\n".join(structured_arcanum(n,labels[i]) for i,n in enumerate(vals))
    if section=="career":
        return section_title("💼 РЕАЛИЗАЦИЯ — ПОЛНЫЙ РАЗБОР")+structured_arcanum(m["talents"][1],"💼 МАТЕРИАЛЬНЫЙ ТАЛАНТ")+"\n\n"+structured_arcanum(m["purpose"],"🎯 ПРЕДНАЗНАЧЕНИЕ")+"\n\n"+structured_arcanum(m["year"],"📆 МАТЕРИАЛЬНЫЙ ПОТЕНЦИАЛ")
    if section=="character":
        return section_title("🧠 ХАРАКТЕР — ПОЛНЫЙ РАЗБОР")+structured_arcanum(m["day"],"📅 БАЗОВАЯ ЭНЕРГИЯ")+"\n\n"+structured_arcanum(m["heart"],"💗 ВНУТРЕННИЙ КОМФОРТ")
    if section=="ancestry":
        return section_title("🌳 РОДОВЫЕ ЛИНИИ — ПОЛНЫЙ РАЗБОР")+structured_arcanum(m["paternal"],"👨 ПО ПАПИНОЙ ЛИНИИ")+"\n\n"+structured_arcanum(m["maternal"],"👩 ПО МАМИНОЙ ЛИНИИ")
    if section=="resources":
        return section_title("✨ РЕСУРСЫ — ПОЛНЫЙ РАЗБОР")+structured_arcanum(m["talents"][0],"🌤 ДУХОВНЫЙ ТАЛАНТ")+"\n\n"+structured_arcanum(m["talents"][1],"💼 МАТЕРИАЛЬНЫЙ ТАЛАНТ")+"\n\n"+structured_arcanum(m["heart"],"💗 ТОЧКА СЕРДЦА")
    if section=="summary":
        vals=[m["day"],m["month"],m["year"],m["purpose"],m["heart"],*m["talents"],*m["love"],*m["money"],*m["karma"],m["paternal"],m["maternal"],*m["tension"]]; counts={n:vals.count(n) for n in set(vals)}; rep=sorted(counts.items(),key=lambda x:-x[1])[:3]
        rt="\n".join(f"• {ARCANA_INFO[n]['name']} ({n}) — повторяется {c} раз(а)" for n,c in rep if c>1) or "• Явных повторений нет."
        return (section_title("⭐ ИТОГ МАТРИЦЫ") +
                f"Главная задача: увидеть повторяющиеся темы, а не искать один фатальный показатель.\n\n"
                f"Что уже работает в плюс: {ARCANA_INFO[m['heart']]['light']}.\n\n"
                f"Что может уводить в минус: {ARCANA_INFO[m['heart']]['shadow']}.\n\n"
                f"Повторяющиеся энергии:\n{rt}\n\n"
                "Проработка на ближайшие 30 дней: выбрать одну тень и заменить её действием из соответствующего блока.\n\n"
                "⚠️ Матрица — символическая модель для саморефлексии.")
    return matrix_report(date_str)


def pro_house_line(chart, house_num):
    return structured_house(chart, house_num)

def pro_planet_line(chart, name):
    return structured_planet(chart, name)

def pro_aspect_block(chart):
    if not chart["aspects"]:
        return "По заданным орбам значимых аспектов не найдено. Это не минус карты."
    return "\n\n".join(structured_aspect(a) for a in chart["aspects"])


def pro_solar_house_assign(solar):
    houses = solar["houses"]
    for name, p in solar["planets"].items():
        x = p["longitude"]
        house = 12
        for i in range(12):
            start = houses[i]["longitude"]
            end = houses[(i + 1) % 12]["longitude"]
            if end < start:
                end += 360
            xx = x
            if xx < start:
                xx += 360
            if start <= xx < end:
                house = i + 1
                break
        p["house"] = house
    return solar


def pro_deep_report(chart, question=""):
    p = chart["planets"]
    h = chart["houses"]
    a = chart["angles"]
    q = (question or "").lower()

    # The engine intentionally combines several indicators rather than making
    # a conclusion from one placement.
    sections = []

    sections.append(
        "💎 ASTROVILKI PRO — ГЛУБОКИЙ АНАЛИЗ\n\n"
        f"Дата: {chart['birth']['date']}\n"
        f"Время: {chart['birth']['local_time']} ({chart['birth']['timezone']})\n"
        f"Место: {chart['location']['display_name']}\n\n"
        "Метод чтения: каждый показатель разбирается по одной схеме — смысл → плюс → минус → проработка → жизненные сюжеты → признаки включения. Повторяющиеся темы считаются более заметными внутри этой эзотерической модели.\n\n"
        "⚠️ Это символическая интерпретация для саморефлексии, а не гарантия событий."
    )

    # Main identity
    sections.append(
        "🌞 ЛИЧНОСТЬ И ВНУТРЕННЯЯ КОНСТРУКЦИЯ\n\n"
        + pro_planet_line(chart, "Солнце") + "\n\n"
        + pro_planet_line(chart, "Луна") + "\n\n"
        + (
            f"ASC: {a['Асцендент']['sign']} {a['Асцендент']['degree']}°. "
            f"Это символический способ входить в ситуацию: "
            f"{SIGN_TEXT[a['Асцендент']['sign']]}.\n"
        )
        + (
            f"MC: {a['MC']['sign']} {a['MC']['degree']}°. "
            f"Карьерный образ раскрывается через "
            f"{SIGN_TEXT[a['MC']['sign']]}."
        )
    )

    # Love
    sections.append(
        "❤️ ОТНОШЕНИЯ — ГЛУБОКИЙ УРОВЕНЬ\n\n"
        + pro_house_line(chart, 5) + "\n"
        + pro_house_line(chart, 7) + "\n"
        + pro_house_line(chart, 8) + "\n\n"
        + pro_planet_line(chart, "Венера") + "\n\n"
        + pro_planet_line(chart, "Марс") + "\n\n"
        + pro_planet_line(chart, "Луна") + "\n\n"
        "В PRO-подходе отношения не сводятся к Венере. "
        "5 дом описывает романтическое включение, 7 — модель значимого "
        "партнёрства, 8 — глубину, доверие и общие ресурсы. "
        "Если несколько показателей указывают на одну и ту же сферу, "
        "её символический вес становится выше.\n\n"
        "Отдельный вопрос для самопроверки: где заканчивается здоровая "
        "близость и начинается отказ от собственных границ?"
    )

    # Partner / where
    sections.append(
        "💘 ПАРТНЁР И СЦЕНАРИЙ ЗНАКОМСТВА\n\n"
        f"7 дом начинается в {h[6]['sign']}: {SIGN_TEXT[h[6]['sign']]}.\n"
        f"5 дом начинается в {h[4]['sign']}; 11 дом — в {h[10]['sign']}.\n\n"
        + pro_planet_line(chart, "Венера") + "\n\n"
        + pro_planet_line(chart, "Марс") + "\n\n"
        "Символически знакомство ищут через повторяющиеся темы 5/7/11 "
        "домов и их управителей. Поэтому возможны такие среды, как "
        "работа, учёба, друзья, интернет, поездка, мероприятие или "
        "новая социальная группа — конкретный адрес карта не определяет.\n\n"
        "Если человек спрашивает «где именно?», правильнее давать "
        "несколько наиболее повторяющихся символических каналов, а не "
        "придумывать конкретное место."
    )

    # Money
    sections.append(
        "💰 ДЕНЬГИ И РЕСУРСЫ\n\n"
        + pro_house_line(chart, 2) + "\n"
        + pro_house_line(chart, 6) + "\n"
        + pro_house_line(chart, 8) + "\n"
        + pro_house_line(chart, 10) + "\n\n"
        + pro_planet_line(chart, "Меркурий") + "\n\n"
        + pro_planet_line(chart, "Юпитер") + "\n\n"
        + pro_planet_line(chart, "Сатурн") + "\n\n"
        "Для финансовой интерпретации важно различать личный доход, "
        "ежедневную работу, совместные ресурсы и карьерный статус. "
        "Карта не обещает конкретную сумму денег; практический результат "
        "зависит от навыков, рынка, решений и обстоятельств."
    )

    # Career/study
    sections.append(
        "💼 КАРЬЕРА + 🎓 УЧЁБА\n\n"
        + pro_house_line(chart, 3) + "\n"
        + pro_house_line(chart, 6) + "\n"
        + pro_house_line(chart, 9) + "\n"
        + pro_house_line(chart, 10) + "\n\n"
        + pro_planet_line(chart, "Меркурий") + "\n\n"
        + pro_planet_line(chart, "Юпитер") + "\n\n"
        "3 дом показывает обучение и коммуникацию, 9 — расширение "
        "горизонтов и высшее образование, 6 — практическую работу, "
        "10 — карьерное направление. Сильнее всего читается не один "
        "дом, а связка этих четырёх уровней."
    )

    # Move
    sections.append(
        "🏠 ПЕРЕЕЗД, ДОМ И ДРУГАЯ СТРАНА\n\n"
        + pro_house_line(chart, 4) + "\n"
        + pro_house_line(chart, 9) + "\n\n"
        + pro_planet_line(chart, "Луна") + "\n\n"
        + pro_planet_line(chart, "Сатурн") + "\n\n"
        + pro_planet_line(chart, "Уран") + "\n\n"
        "Для конкретного города желательно строить релокационную карту. "
        "Натальная карта может использоваться как символический фон, "
        "но сама по себе не доказывает, что конкретный город будет лучше."
    )

    # Friends/psychology
    sections.append(
        "👥 ДРУЗЬЯ, ОКРУЖЕНИЕ И СОЦИАЛЬНЫЕ СВЯЗИ\n\n"
        + pro_house_line(chart, 11) + "\n"
        + pro_planet_line(chart, "Уран") + "\n\n"
        "11 дом связывают с друзьями, сообществами, аудиторией и "
        "долгосрочными планами. Здесь особенно полезно смотреть, "
        "какие люди дают чувство роста, а какие заставляют постоянно "
        "подстраиваться.\n\n"
        "🧠 ПСИХОЛОГИЧЕСКИЙ УРОВЕНЬ\n\n"
        + pro_planet_line(chart, "Луна") + "\n\n"
        + pro_planet_line(chart, "Плутон") + "\n\n"
        + pro_planet_line(chart, "Нептун") + "\n\n"
        "Эта часть не является диагностикой. Она используется как "
        "эзотерический язык для разговора о привычках, эмоциях, "
        "идеализации, границах и повторяющихся реакциях."
    )

    # Planets + houses + aspects
    sections.append(
        "🪐 ВСЕ ПЛАНЕТЫ\n\n" +
        "\n\n".join(pro_planet_line(chart, name) for name in p.keys())
    )
    sections.append(
        "🏠 ВСЕ 12 ДОМОВ\n\n" +
        "\n".join(pro_house_line(chart, i) for i in range(1, 13))
    )
    sections.append(
        "🔗 АСПЕКТЫ\n\n" + pro_aspect_block(chart)
    )

    # Personalized question routing
    if q:
        if any(k in q for k in ("где", "встреч", "знаком", "партнер", "партнёр", "муж", "жена")):
            sections.append(
                "💬 ОТВЕТ НА ТВОЙ ВОПРОС\n\n"
                "Твой вопрос относится прежде всего к 5/7/11 домам. "
                f"5 дом: {h[4]['sign']}; 7 дом: {h[6]['sign']}; "
                f"11 дом: {h[10]['sign']}.\n\n"
                "Символически наиболее перспективной считается среда, "
                "которая повторяется через эти дома и связанные с ними планеты. "
                "Карту нельзя использовать, чтобы достоверно назвать конкретный "
                "адрес, дату или личность будущего партнёра."
            )
        elif any(k in q for k in ("переезд", "переехать", "город", "москв", "страна", "жить")):
            sections.append(
                "💬 ОТВЕТ НА ТВОЙ ВОПРОС\n\n"
                f"Тема дома: 4 дом в {h[3]['sign']}.\n"
                f"Тема дальнего перемещения: 9 дом в {h[8]['sign']}.\n"
                f"MC: {a['MC']['sign']}.\n\n"
                "Для сравнения конкретных городов следующий уровень — "
                "релокационная карта каждого города и сопоставление ASC/MC "
                "и домов. Без этого нельзя честно заявлять, что один город "
                "«судьбоносный», а другой нет."
            )
        elif any(k in q for k in ("деньг", "заработ", "доход", "карьер", "работ", "професс")):
            sections.append(
                "💬 ОТВЕТ НА ТВОЙ ВОПРОС\n\n"
                f"Финансовая ось: 2 дом в {h[1]['sign']} → "
                f"{SIGN_TEXT[h[1]['sign']]}.\n"
                f"Рабочая ось: 6 дом в {h[5]['sign']}.\n"
                f"Карьерная ось: 10 дом в {h[9]['sign']}.\n\n"
                "Сильная стратегия — переводить повторяющиеся темы карты "
                "в конкретные навыки, услуги, проекты и финансовую дисциплину."
            )
        else:
            sections.append(
                "💬 ОТВЕТ НА ТВОЙ ВОПРОС\n\n"
                f"Я бы начала с ASC {a['Асцендент']['sign']}, "
                f"Солнца в {p['Солнце']['sign']} и Луны в {p['Луна']['sign']}, "
                "а затем проверила, какие дома и аспекты повторяют ту же тему. "
                "Если вопрос связан с любовью, деньгами, карьерой или переездом, "
                "его можно уточнить одним следующим сообщением."
            )

    sections.append(
        "⚠️ Важное ограничение\n\n"
        "Астрология и матрица судьбы — эзотерические/развлекательные системы, "
        "а не научные методы прогнозирования. Этот бот не должен использоваться "
        "как единственное основание для медицинских, юридических или финансовых решений."
    )
    sections.append(
        "🧩 ИТОГОВАЯ КАРТА НА 30 ДНЕЙ\n\n"
        "Главная задача: выбрать одну повторяющуюся тему и перевести её в измеримое действие.\n\n"
        "Что уже работает в плюс: использовать сильные качества показателей как ресурсы, а не как обещание судьбы.\n\n"
        "Что может уводить в минус: отслеживать избегание, контроль, зависимость от одобрения, импульсивность, идеализацию и финансовый хаос.\n\n"
        "Проработка: одна привычка + одна граница + один измеримый результат на ближайшие 30 дней.\n\n"
        "Что отслеживать: повторяющиеся обстоятельства, эмоции, людей вокруг и собственные решения.\n\n"
        "Вопрос недели: «Что в моей жизни сейчас требует именно этой проработки?»"
    )

    return "\n\n━━━━━━━━━━━━━━━━\n\n".join(sections)


def pro_short_section(chart, solar, matrix, section):
    p = chart["planets"]
    h = chart["houses"]
    a = chart["angles"]

    mapping = {
        "rel": "❤️ ОТНОШЕНИЯ\n\n"
               + pro_house_line(chart, 5) + "\n"
               + pro_house_line(chart, 7) + "\n"
               + pro_house_line(chart, 8) + "\n\n"
               + pro_planet_line(chart, "Венера") + "\n\n"
               + pro_planet_line(chart, "Марс") + "\n\n"
               + pro_planet_line(chart, "Луна"),
        "partner": "💘 ПАРТНЁР\n\n"
                  + pro_house_line(chart, 7) + "\n\n"
                  + pro_planet_line(chart, "Венера") + "\n\n"
                  + pro_planet_line(chart, "Марс") + "\n\n"
                  + "Символический образ партнёра складывается из 7 дома, его управителя, Венеры/Марса и повторяющихся аспектов.",
        "where": "📍 ГДЕ МОЖЕТ ПРОИЗОЙТИ ЗНАКОМСТВО\n\n"
                 + pro_house_line(chart, 5) + "\n"
                 + pro_house_line(chart, 7) + "\n"
                 + pro_house_line(chart, 11) + "\n\n"
                 "Наиболее логично искать повторяющийся канал: друзья/сообщества, учёба, работа, интернет, поездки или творческая среда. Это символическая интерпретация, а не точный адрес.",
        "marriage": "💍 БРАК\n\n"
                    + pro_house_line(chart, 7) + "\n"
                    + pro_house_line(chart, 8) + "\n\n"
                    + pro_planet_line(chart, "Венера") + "\n\n"
                    + pro_planet_line(chart, "Сатурн"),
        "money": "💰 ДЕНЬГИ\n\n"
                 + pro_house_line(chart, 2) + "\n"
                 + pro_house_line(chart, 6) + "\n"
                 + pro_house_line(chart, 8) + "\n"
                 + pro_house_line(chart, 10) + "\n\n"
                 + pro_planet_line(chart, "Юпитер") + "\n\n"
                 + pro_planet_line(chart, "Сатурн"),
        "career": "💼 КАРЬЕРА\n\n"
                  + pro_house_line(chart, 6) + "\n"
                  + pro_house_line(chart, 10) + "\n\n"
                  + pro_planet_line(chart, "Меркурий") + "\n\n"
                  + pro_planet_line(chart, "Юпитер") + "\n\n"
                  + pro_planet_line(chart, "Сатурн"),
        "study": "🎓 УЧЁБА\n\n"
                 + pro_house_line(chart, 3) + "\n"
                 + pro_house_line(chart, 9) + "\n\n"
                 + pro_planet_line(chart, "Меркурий") + "\n\n"
                 + pro_planet_line(chart, "Юпитер"),
        "move": "🏠 ПЕРЕЕЗД\n\n"
                + pro_house_line(chart, 4) + "\n"
                + pro_house_line(chart, 9) + "\n\n"
                + pro_planet_line(chart, "Луна") + "\n\n"
                + pro_planet_line(chart, "Уран"),
        "travel": "✈️ ПОЕЗДКИ\n\n"
                  + pro_house_line(chart, 3) + "\n"
                  + pro_house_line(chart, 9) + "\n\n"
                  + pro_planet_line(chart, "Меркурий") + "\n\n"
                  + pro_planet_line(chart, "Юпитер"),
        "friends": "👥 ДРУЗЬЯ\n\n"
                   + pro_house_line(chart, 11) + "\n\n"
                   + pro_planet_line(chart, "Уран") + "\n\n"
                   + pro_planet_line(chart, "Сатурн"),
        "psych": "🧠 ПСИХОЛОГИЯ\n\n"
                 + pro_planet_line(chart, "Луна") + "\n\n"
                 + pro_planet_line(chart, "Нептун") + "\n\n"
                 + pro_planet_line(chart, "Плутон"),
        "planets": "🪐 ПЛАНЕТЫ\n\n" +
                   "\n\n".join(pro_planet_line(chart, name) for name in p),
        "houses": "🏠 12 ДОМОВ\n\n" +
                  "\n".join(pro_house_line(chart, i) for i in range(1,13)),
        "aspects": "🔗 АСПЕКТЫ\n\n" + pro_aspect_block(chart),
        "summary": pro_deep_report(chart),
        "solar": (
            "☀️ СОЛЯР PRO\n\n"
            f"ASC соляра: {solar['asc']['sign']} {solar['asc']['degree']}°.\n"
            f"MC соляра: {solar['mc']['sign']} {solar['mc']['degree']}°.\n\n"
            "Планеты года:\n" +
            "\n".join(
                f"• {name}: {v['sign']} {v['degree']}°"
                for name, v in solar["planets"].items()
            ) +
            "\n\n"
            "Для PRO-уровня особенно важны дома, в которые попадают Солнце, Луна, Венера, Марс, Юпитер и Сатурн соляра, а также повторения с наталом."
        ),
        "matrix": (
            "🔢 МАТРИЦА PRO — полный расклад\n\n"
            + arcanum_line(matrix["day"], "📅 День — ") + "\n\n"
            + arcanum_line(matrix["month"], "🌙 Месяц — ") + "\n\n"
            + arcanum_line(matrix["year"], "📆 Год — ") + "\n\n"
            + arcanum_line(matrix["purpose"], "🎯 Предназначение — ") + "\n\n"
            + arcanum_line(matrix["heart"], "💗 Точка сердца (центр) — ") + "\n\n"
            + arcanum_line(matrix["talents"][0], "🌤 Духовный талант — ") + "\n\n"
            + arcanum_line(matrix["talents"][1], "💼 Материальный талант — ") + "\n\n"
            + arcanum_line(matrix["love"][0], "🚪 Вход в отношения — ") + "\n\n"
            + arcanum_line(matrix["love"][2], "🔁 Любовная карма — ") + "\n\n"
            + arcanum_line(matrix["money"][0], "🚪 Вход денег — ") + "\n\n"
            + arcanum_line(matrix["money"][2], "🔁 Денежная карма — ") + "\n\n"
            + arcanum_line(matrix["karma"][0], "🩶 Кармический хвост, 1 — ") + "\n\n"
            + arcanum_line(matrix["karma"][1], "🩶 Кармический хвост, 2 — ") + "\n\n"
            + arcanum_line(matrix["karma"][2], "🩶 Кармический хвост, 3 — ") + "\n\n"
            + arcanum_line(matrix["paternal"], "👨 По папиной линии — ") + "\n\n"
            + arcanum_line(matrix["maternal"], "👩 По маминой линии — ") + "\n\n"
            "PRO-интерпретация: каждую энергию лучше читать не отдельно, а через "
            "повторяющиеся темы — если один и тот же аркан встречается в нескольких "
            "точках, это ключевая тема года."
        ),
        "periods": (
            "📅 ПЕРИОДЫ ГОДА\n\n"
            "1 — личность и внешний образ\n"
            "2 — деньги и самоценность\n"
            "3 — общение, документы, обучение\n"
            "4 — дом и семья\n"
            "5 — любовь и творчество\n"
            "6 — работа и режим\n"
            "7 — партнёрство\n"
            "8 — близость и общие ресурсы\n"
            "9 — учёба и дальние поездки\n"
            "10 — карьера и статус\n"
            "11 — друзья и планы\n"
            "12 — завершение цикла\n\n"
            "Это символическая последовательность домов, а не календарные гарантии событий."
        )
    }
    return mapping.get(section, pro_deep_report(chart))


async def send_result(query, text_value, markup=None):
    await send_html_chunks(query.message, text_value)
    if markup:
        await query.message.reply_text("Выбери следующий раздел:", reply_markup=markup)


async def pro_send(query, text_value, markup=None):
    await send_html_chunks(query.message, text_value)
    if markup:
        await query.message.reply_text("💎 PRO — выбери следующий раздел:", reply_markup=markup)


async def pro_buy(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if pro_is_paid(user_id):
        await query.message.reply_text(
            "✅ PRO уже активирован.",
            reply_markup=pro_menu()
        )
        return

    try:
        _, confirmation_url = await yookassa_create_payment(user_id)
        await query.message.reply_text(
            "💳 Оплата AstroVilki PRO\n\n"
            "Стоимость: 67 ₽\n"
            "Нажми кнопку ниже, чтобы перейти на защищённую страницу ЮKassa.\n\n"
            "После оплаты вернись в бот и нажми «🔄 Проверить оплату».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Перейти к оплате — 67 ₽", url=confirmation_url)],
                [InlineKeyboardButton("🔄 Проверить оплату", callback_data="pro_check_payment")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="pro_open")]
            ])
        )
    except Exception as e:
        log.exception("YooKassa create payment error")
        await query.message.reply_text(
            "❌ Не удалось создать платёж через ЮKassa.\n\n"
            f"Техническая ошибка: {type(e).__name__}: {e}",
            reply_markup=pro_sales_menu()
        )


async def pro_precheckout(update, context):
    query = update.pre_checkout_query
    if query.invoice_payload != ASTROVILKI_PRO_PAYMENT:
        await query.answer(ok=False, error_message="Неизвестный платёж.")
        return
    await query.answer(ok=True)


async def pro_successful_payment(update, context):
    payment = update.message.successful_payment
    if payment.invoice_payload != ASTROVILKI_PRO_PAYMENT:
        return
    pro_grant(update.effective_user.id, payment.telegram_payment_charge_id)
    await update.message.reply_text(
        "🎉 Оплата прошла успешно!\n\n"
        "💎 PRO разблокирован.\n"
        "Теперь выбирай разделы или просто напиши вопрос обычным сообщением.",
        reply_markup=pro_menu()
    )



async def sbp_receipt_handler(update, context):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    if not sbp_pending_has(uid) or pro_is_paid(uid):
        return

    if not ADMIN_ID:
        await update.message.reply_text(
            "⚠️ Оплата получена, но администратор ещё не настроен. "
            "Сообщение сохранено. Настрой ADMIN_ID в Railway."
        )
        return

    caption = (
        f"💳 Новая заявка на PRO по СБП\n"
        f"User ID: {uid}\n"
        f"Username: @{update.effective_user.username or 'нет'}\n"
        f"Имя: {update.effective_user.full_name}\n\n"
        "Проверь поступление 67 ₽ и нажми кнопку."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить и открыть PRO", callback_data=f"sbp_ok:{uid}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"sbp_no:{uid}")]
    ])
    if update.message.photo:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=update.message.photo[-1].file_id,
            caption=caption,
            reply_markup=kb
        )
    elif update.message.document:
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=update.message.document.file_id,
            caption=caption,
            reply_markup=kb
        )
    else:
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption, reply_markup=kb)

    await update.message.reply_text(
        "📩 Чек отправлен на проверку.\n\nПосле подтверждения PRO откроется автоматически."
    )


async def sbp_admin_callback(update, context):
    query = update.callback_query
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return True
    await query.answer()
    data = query.data
    if data.startswith("sbp_ok:"):
        uid = int(data.split(":", 1)[1])
        pro_grant(uid, "SBP-VERIFIED")
        sbp_pending_delete(uid)
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="🎉 Оплата подтверждена!\n\n💎 PRO разблокирован.",
                reply_markup=pro_menu()
            )
        except Exception:
            pass
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\n✅ ПОДТВЕРЖДЕНО"
        )
        return True
    if data.startswith("sbp_no:"):
        uid = int(data.split(":", 1)[1])
        sbp_pending_delete(uid)
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="❌ Оплата пока не подтверждена. Если платёж уже прошёл, отправь чек ещё раз."
            )
        except Exception:
            pass
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\n❌ ОТКЛОНЕНО"
        )
        return True
    return False



async def pro_callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pro_open":
        if pro_is_paid(query.from_user.id):
            await query.message.reply_text(
                "💎 PRO уже активирован.\n\nВыбирай раздел:",
                reply_markup=pro_menu()
            )
        else:
            await query.message.reply_text(
                pro_sales_text(),
                reply_markup=pro_sales_menu(),
                parse_mode="HTML"
            )
        return True

    if data == "pro_buy":
        await pro_buy(update, context)
        return True

    if data == "pro_check_payment":
        user_id = query.from_user.id
        if pro_is_paid(user_id):
            await query.message.reply_text(
                "✅ PRO уже активирован.",
                reply_markup=pro_menu()
            )
            return True

        try:
            ok, message = await yookassa_check_and_activate(user_id)
            if ok:
                await query.message.reply_text(message, reply_markup=pro_menu())
            else:
                await query.message.reply_text(
                    message,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Оплатить 67 ₽", callback_data="pro_buy")],
                        [InlineKeyboardButton("🔄 Проверить ещё раз", callback_data="pro_check_payment")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="pro_open")]
                    ])
                )
        except Exception as e:
            log.exception("YooKassa check payment error")
            await query.message.reply_text(
                f"❌ Не удалось проверить оплату.\n\nТехническая ошибка: {type(e).__name__}: {e}",
                reply_markup=pro_sales_menu()
            )
        return True

    if data == "pro_sbp_info":
        if SBP_PAYMENT_URL:
            await query.message.reply_text(
                "💳 ОПЛАТА PRO ЧЕРЕЗ СБП\n\n"
                "Нажми кнопку ниже — откроется защищённая платёжная страница "
                "твоего банка/платёжного провайдера.\n\n"
                "Сумма: 67 ₽.\n\n"
                "После успешной оплаты вернись в бот и нажми «Я оплатил(а)».",
                reply_markup=sbp_payment_menu()
            )
        else:
            await query.message.reply_text(
                "💳 Оплата СБП ещё не настроена в переменных Railway.\n\n"
                "Нужно указать SBP_PAYMENT_URL — готовую платёжную ссылку "
                "банка/эквайера. Обычный перевод по номеру телефона здесь "
                "не используется, чтобы не раскрывать номер получателя.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data="pro_open")]
                ])
            )
        return True

    if data == "pro_paid":
        user_id = query.from_user.id
        if pro_is_paid(user_id):
            await query.message.reply_text("✅ PRO уже активирован.", reply_markup=pro_menu())
            return True

        if SBP_AUTO_APPROVE:
            pro_grant(user_id, "SBP-MANUAL-AUTO")
            await query.message.reply_text(
                "🎉 PRO активирован!\n\n"
                "Теперь можешь пользоваться глубокими разборами.",
                reply_markup=pro_menu()
            )
            return True

        sbp_pending_set(user_id)
        await query.message.reply_text(
            "📩 Заявка на проверку оплаты создана.\n\n"
            "Пришли сюда скриншот/чек оплаты СБП одним сообщением. "
            "После проверки доступ будет открыт.\n\n"
            "Если чек уже отправлялся — просто подожди подтверждения.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="pro_open")]
            ])
        )
        return True

    if data in {
        "pro_rel","pro_partner","pro_where","pro_marriage","pro_money",
        "pro_career","pro_study","pro_move","pro_travel","pro_friends",
        "pro_psych","pro_solar","pro_matrix","pro_planets","pro_houses",
        "pro_aspects","pro_periods","pro_summary"
    }:
        # Reuse the existing callback implementation by letting it handle
        # the detailed section. Return False so the generic callback processes it.
        return False

    return False

async def callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pro_open":
        if pro_is_paid(query.from_user.id):
            await query.message.reply_text("💎 PRO уже активирован.", reply_markup=pro_menu())
        else:
            await query.message.reply_text(pro_sales_text(), reply_markup=pro_sales_menu(), parse_mode="HTML")
        return

    if data == "pro_buy":
        await pro_buy(update, context)
        return

    pro_sections = {
        "pro_rel": "rel",
        "pro_partner": "partner",
        "pro_where": "where",
        "pro_marriage": "marriage",
        "pro_money": "money",
        "pro_career": "career",
        "pro_study": "study",
        "pro_move": "move",
        "pro_travel": "travel",
        "pro_friends": "friends",
        "pro_psych": "psych",
        "pro_solar": "solar",
        "pro_matrix": "matrix",
        "pro_planets": "planets",
        "pro_houses": "houses",
        "pro_aspects": "aspects",
        "pro_periods": "periods",
        "pro_summary": "summary",
    }

    if data in pro_sections:
        if not pro_is_paid(query.from_user.id):
            await query.message.reply_text(
                "🔒 Этот раздел доступен после оплаты PRO — 67 ⭐.",
                reply_markup=pro_sales_menu()
            )
            return

        if not all(k in context.user_data for k in ("date", "time", "city")):
            await query.message.reply_text("Сначала введи данные рождения через /start.")
            return

        try:
            chart = await calculate_chart(
                context.user_data["date"],
                context.user_data["time"],
                context.user_data["city"]
            )
            solar = await calculate_solar(chart)
            solar = pro_solar_house_assign(solar)
            matrix = matrix_22(context.user_data["date"])
            result = pro_short_section(chart, solar, matrix, pro_sections[data])
            await pro_send(query, result, pro_menu())
        except Exception as e:
            log.exception("PRO section error")
            await query.message.reply_text(
                f"❌ Не получилось построить PRO-раздел.\n\n"
                f"Техническая ошибка: {type(e).__name__}: {e}",
                reply_markup=pro_menu()
            )
        return

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
        "Geocult для работы бота не нужен. Оплата PRO — через ЮKassa."
    )


async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    pro_db_init()
    yookassa_pending_init()
    sbp_pending_init()

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub$"),
        ],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_city)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(sbp_admin_callback, pattern=r"^sbp_(ok|no):"))
    app.add_handler(CallbackQueryHandler(pro_callback_handler, pattern=r"^pro_(open|buy|sbp_info|paid|check_payment)$"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, sbp_receipt_handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_error_handler(error_handler)

    async def _post_init(application):
        application.bot_data["yookassa_auto_task"] = asyncio.create_task(
            yookassa_auto_loop(application)
        )
        log.info("YooKassa automatic activation loop started")

    async def _post_shutdown(application):
        task = application.bot_data.get("yookassa_auto_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    log.info("AstroVilki started: LOCAL MODE, no Geocult, no OpenAI")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
