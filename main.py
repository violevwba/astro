
import os
import re
import asyncio
import logging
from datetime import datetime
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
from openai import AsyncOpenAI
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrovilki")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
GEOCULT_URL = os.getenv("GEOCULT_URL", "https://geocult.ru/natalnaya-karta-onlayn-raschet")

llm = AsyncOpenAI(api_key=OPENAI_API_KEY)

DATE, TIME, CITY = range(3)

DISCLAIMER = (
    "\n\n⚠️ Астрология и «матрица судьбы» — развлекательная/эзотерическая интерпретация, "
    "а не научный метод. Не используй прогнозы как единственное основание для медицинских, "
    "финансовых или юридических решений."
)

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
        "Я могу получить расчёт натальной карты через Geocult, а затем передать "
        "структурированные данные ИИ для большого понятного разбора.\n\n"
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
        "Теперь выбери, что построить:",
        reply_markup=menu()
    )
    return ConversationHandler.END

async def geocode_city(city):
    # Nominatim is used only to obtain coordinates. Geocult itself remains the source
    # of the astrological calculation.
    import httpx
    headers = {"User-Agent": "AstroVilkiBot/1.0"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city, "format": "jsonv2", "limit": 1}
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            raise ValueError("Город не найден")
        return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name", city)

async def scrape_geocult(date_str, time_str, city):
    """
    Opens the public Geocult calculator in a real browser and fills its form.
    Because Geocult does not expose a documented public API, this adapter intentionally
    uses the website UI instead of pretending there is an official API.
    """
    lat, lon, display = await geocode_city(city)
    d = datetime.strptime(date_str, "%d.%m.%Y")
    hour, minute = map(int, time_str.split(":"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="ru-RU")
        try:
            await page.goto(GEOCULT_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)

            # Fill likely fields by labels/names. Geocult may change markup, so selectors
            # are deliberately tolerant and we also inspect inputs by nearby text.
            inputs = page.locator("input")
            selects = page.locator("select")

            # Set text/date/time through select elements where possible.
            # First input is usually name; location search is often a text input.
            # We avoid relying on fixed IDs.
            texts = []
            for i in range(min(await inputs.count(), 30)):
                el = inputs.nth(i)
                try:
                    typ = await el.get_attribute("type")
                    name = await el.get_attribute("name")
                    value = await el.get_attribute("value")
                    ph = await el.get_attribute("placeholder")
                    texts.append((i, typ, name, value, ph))
                except Exception:
                    pass

            # Use page text to find date/time controls, then select options by value.
            # The site currently renders day/month/year and hour/minute as <select>.
            sel_count = await selects.count()
            if sel_count < 5:
                raise RuntimeError("Geocult form structure changed: not enough select fields")

            # The first selects on the calculator are date/time/location/GMT/house system.
            # We select matching values by inspecting option text/value.
            async def choose_select_containing(index, candidates):
                sel = selects.nth(index)
                opts = await sel.locator("option").all()
                for opt in opts:
                    txt = (await opt.inner_text()).strip()
                    val = await opt.get_attribute("value")
                    if any(c == txt or c == val or c in txt for c in candidates):
                        await sel.select_option(val)
                        return True
                return False

            # Find date selects by their option sets rather than hard-coded positions.
            for i in range(sel_count):
                sel = selects.nth(i)
                option_texts = [(await sel.locator("option").nth(j).inner_text()).strip()
                                for j in range(min(await sel.locator("option").count(), 40))]
                joined = " ".join(option_texts)
                if str(d.year) in joined and len(option_texts) > 20:
                    await choose_select_containing(i, [str(d.year)])
                elif any(str(d.month) == x or f"{d.month:02d}" == x for x in option_texts):
                    await choose_select_containing(i, [str(d.month), f"{d.month:02d}"])
                elif any(str(d.day) == x or f"{d.day:02d}" == x for x in option_texts):
                    await choose_select_containing(i, [str(d.day), f"{d.day:02d}"])
                elif any(f"{hour:02d}" == x or str(hour) == x for x in option_texts):
                    await choose_select_containing(i, [f"{hour:02d}", str(hour)])
                elif any(f"{minute:02d}" == x or str(minute) == x for x in option_texts):
                    await choose_select_containing(i, [f"{minute:02d}", str(minute)])

            # Try to find the city input by placeholder/name.
            city_input = None
            for i, typ, name, value, ph in texts:
                blob = " ".join(str(x or "").lower() for x in (name, ph))
                if "город" in blob or "city" in blob or "town" in blob:
                    city_input = inputs.nth(i)
                    break
            if city_input is None:
                # fallback: first text input with a nearby location-related placeholder
                for i, typ, name, value, ph in texts:
                    if typ in (None, "text") and (ph or ""):
                        city_input = inputs.nth(i)
                        break

            if city_input:
                await city_input.fill(city)
                await page.wait_for_timeout(1200)
                # Choose first visible autocomplete item.
                candidates = page.locator(
                    "li:visible, .ui-menu-item:visible, .autocomplete-suggestion:visible, "
                    ".suggestion:visible, [role='option']:visible"
                )
                if await candidates.count():
                    await candidates.first.click()
                else:
                    await city_input.press("Enter")

            # If coordinates are present as inputs, fill them. This makes the fallback
            # resilient when autocomplete is unavailable.
            for i, typ, name, value, ph in texts:
                blob = " ".join(str(x or "").lower() for x in (name, ph))
                try:
                    if any(k in blob for k in ["широта", "latitude", "lat"]):
                        await inputs.nth(i).fill(f"{lat:.6f}")
                    elif any(k in blob for k in ["долгота", "longitude", "lon"]):
                        await inputs.nth(i).fill(f"{lon:.6f}")
                except Exception:
                    pass

            # Click calculator button.
            btn = page.get_by_role("button", name=re.compile("Рассчитать натальную карту", re.I))
            if await btn.count() == 0:
                btn = page.locator("input[type=submit], button").filter(has_text=re.compile("Рассчитать", re.I))
            if await btn.count() == 0:
                raise RuntimeError("Не найдена кнопка расчёта Geocult")
            await btn.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1800)

            body = await page.locator("body").inner_text()
            if len(body) < 1000:
                raise RuntimeError("Geocult вернул слишком мало данных")

            # Keep a bounded snapshot for the LLM to avoid huge prompts.
            return {
                "source": "Geocult",
                "city_display": display,
                "latitude": lat,
                "longitude": lon,
                "birth_date": date_str,
                "birth_time": time_str,
                "page_url": page.url,
                "raw_text": body[:50000],
            }
        finally:
            await browser.close()

def matrix_22(date_str):
    # Popular 22-arcana "matrix of destiny" calculation.
    # It is intentionally labelled as an esoteric method, not a scientific calculation.
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
        "day": day, "month": month, "year": year, "core": core,
        "note": "Расчёт выполнен по распространённой 22-арканной схеме; варианты школ отличаются."
    }

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
Пользователь дал данные рождения: {context}.
Ниже находится сырой текст страницы расчёта Geocult. НЕ придумывай положения планет,
аспекты, дома или градусы, которых нет в исходных данных. Если какой-то показатель
не найден, честно напиши «не найден в данных».

Сделай большой, структурированный разбор на русском:
1. Краткий портрет.
2. Солнце, Луна, Асцендент.
3. Все планеты по знакам.
4. Все планеты по домам, если дома есть.
5. Основные аспекты с объяснением.
6. Сильные/напряжённые темы карты.
7. Отношения и любовь.
8. Деньги и ресурсы.
9. Реализация, профессия и таланты.
10. Эмоциональный стиль и коммуникация.
11. Кармические/узловые темы, если данные есть.
12. Итог: 10 самых важных наблюдений.
Пиши понятно, без фатализма и без медицинских/юридических/финансовых обещаний.

GEOCULT DATA:
{data["raw_text"]}
"""
    elif kind == "matrix":
        prompt = f"""
Сделай красивый подробный эзотерический разбор «матрицы судьбы» по 22 арканам.
Данные: {data}. Объясни, что это эзотерическая система, а не научный метод.
Разбери: характер, сильные стороны, отношения, деньги, реализацию, зоны роста,
ресурс и практические вопросы для саморефлексии. Не выдавай трактовки за факты.
"""
    else:
        prompt = f"""
Сделай аккуратный астрологический текст для пользователя AstroVilki.
Укажи, что соляр — эзотерическая интерпретация. Используй только данные ниже и
не выдумывай точные положения, которых нет.
Данные рождения: {context}
Дополнительные данные: {data}
Если полноценного расчёта соляра нет, честно объясни ограничение и не подменяй его
натальной картой.
"""
    resp = await llm.responses.create(model=MODEL, input=prompt)
    return resp.output_text

async def run_report(update, context, kind):
    query = update.callback_query
    await query.answer()
    data = context.user_data
    if not all(k in data for k in ("date", "time", "city")):
        await query.message.reply_text("Сначала введи данные рождения через /start.")
        return

    ctx = f"{data['date']}, {data['time']}, {data['city']}"
    await query.message.chat.send_action(ChatAction.TYPING)

    try:
        if kind in ("natal", "full", "solar"):
            geocult = await scrape_geocult(data["date"], data["time"], data["city"])
        else:
            geocult = None

        if kind == "natal":
            result = await ai_analysis("natal", geocult, ctx)
        elif kind == "matrix":
            result = await ai_analysis("matrix", matrix_22(data["date"]), ctx)
        elif kind == "solar":
            # We intentionally don't invent a solar-return chart until a dedicated
            # Geocult solar adapter is configured.
            result = await ai_analysis("solar", {
                "geocult_natal_source": geocult["page_url"],
                "message": "Полный автоматический расчёт соляра требует отдельного адаптера страницы Geocult Solar."
            }, ctx)
        else:
            natal = await ai_analysis("natal", geocult, ctx)
            matrix = await ai_analysis("matrix", matrix_22(data["date"]), ctx)
            result = "🌌 НАТАЛЬНАЯ КАРТА\n\n" + natal + "\n\n🔢 МАТРИЦА СУДЬБЫ\n\n" + matrix
    except Exception as e:
        log.exception("report error")
        result = (
            "Не получилось автоматически получить расчёт с Geocult.\n\n"
            "Возможная причина — сайт изменил форму, временно недоступен или "
            "не удалось однозначно найти город.\n\n"
            f"Техническая ошибка: {type(e).__name__}"
        )

    result += DISCLAIMER
    for chunk in split_text(result):
        await query.message.reply_text(chunk)

    await query.message.reply_text("Готово ✨ Что сделать дальше?", reply_markup=menu())

async def callback(update, context):
    kind = update.callback_query.data
    if kind == "reset":
        await update.callback_query.answer()
        context.user_data.clear()
        await update.callback_query.message.reply_text("Давай заново. Напиши дату рождения: ДД.ММ.ГГГГ")
        return DATE
    await run_report(update, context, kind)

async def help_cmd(update, context):
    await update.message.reply_text(
        "Команды:\n/start — ввести данные рождения заново\n/help — помощь\n\n"
        "После ввода даты, времени и города можно выбрать натальную карту, матрицу, "
        "соляр или полный разбор."
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
    log.info("AstroVilki started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
