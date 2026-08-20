import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from config import TELEGRAM_BOT_TOKEN
from models import UserProfile
from services.ai_engine import generate_full_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ASK_DATE, ASK_TIME, ASK_CITY = range(3)

user_profiles: dict[int, UserProfile] = {}

MAIN_MENU = [
    ["🌀 Натальная карта", "☀️ Соляр"],
    ["💞 Синастрия", "📄 PDF-разбор"],
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, я AstroVilki_bot — твой личный астролог.\n\n"
        "Нажми «🌀 Натальная карта», чтобы получить полный разбор.",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Натальная карта" in text:
        await update.message.reply_text("Введи дату рождения (ДД.ММ.ГГГГ):")
        return ASK_DATE
    await update.message.reply_text("Этот раздел скоро появится 💫")
    return ConversationHandler.END

async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["birth_date"] = update.message.text.strip()
    await update.message.reply_text("Теперь время рождения (ЧЧ:ММ):")
    return ASK_TIME

async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["birth_time"] = update.message.text.strip()
    await update.message.reply_text("И город рождения (с страной при необходимости):")
    return ASK_CITY

async def process_natal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    birth_city = update.message.text.strip()
    birth_date = context.user_data["birth_date"]
    birth_time = context.user_data["birth_time"]

    user_id = update.effective_user.id
    profile = UserProfile(
        user_id=user_id,
        name=update.effective_user.full_name,
        birth_date=birth_date,
        birth_time=birth_time,
        birth_city=birth_city,
    )
    user_profiles[user_id] = profile

    await update.message.reply_text("Секунду, строю твою карту и готовлю разбор… ✨")

    report = generate_full_report(birth_date, birth_time, birth_city)

    await update.message.reply_text(report)
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_natal = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Натальная карта"), menu_router)],
        states={
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_time)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_city)],
            ASK_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_natal)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_natal)

    logger.info("AstroVilki_bot запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
