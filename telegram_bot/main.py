import os
import json
import logging
from datetime import datetime, time as dt_time
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")

DATA_FILE = os.path.join(os.path.dirname(__file__), "users_data.json")

def load_users():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("users", [])
    return []

def save_users(users):
    with open(DATA_FILE, "w") as f:
        json.dump({"users": users}, f)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id

    users = load_users()
    if user_id not in users:
        users.append(user_id)
        save_users(users)
        logger.info(f"عضو جديد انضم للبوت: {user_id}")

    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🕌 مواقيت الصلاة", callback_data="prayer_times"), InlineKeyboardButton("📅 التاريخ الهجري", callback_data="hijri_date")],
        [InlineKeyboardButton("📖 حديث اليوم", callback_data="hadith_today"), InlineKeyboardButton("💡 عن البوت", callback_data="about_bot")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "مرحباً بك في البوت الإسلامي الدعوي الشامل ☝🏻\nكل التنبيهات والفوائد تصلك هنا مجاناً وتلقائياً دون أي اشتراكات!"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total_users = len(load_users())
    await update.message.reply_text(f"📊 **إحصائيات البوت الحالية:**\n\n👥 عدد المشتركين الكلي: {total_users} عضو.")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "prayer_times":
        await query.message.reply_text(
            "📌 **تنبيهات الصلوات الخمس بالتوقيت المحلي:**\n\n"
            "🇩🇿 **بتوقيت الجزائر العاصمة وما جاورها**\n"
            "🇸🇦 **بتوقيت مكة المكرمة شرفها الله**\n\n"
            "📢 التنبيهات تصلكم تلقائياً وبانتظام يومياً في مواعيد الأذان."
        )
    elif query.data == "hijri_date":
        await query.message.reply_text(
            "📅 **تذكير التاريخ الهجري والحدث الإسلامي:**\n\n"
            "🕌 **حدث من زمن الرسول ﷺ والصحابة:**\n"
            "في مثل هذه الأيام تجلت تضحيات الصحابة في مواقف الثبات ونصرة الدين بالمال والنفس لرفع راية التوحيد.\n\n"
            "☝🏻 **حدث من واقعنا المعاصر:**\n"
            "ثبات الموحدين المرابطين على أرض الثغور متمسكين بدينهم."
        )
    elif query.data == "hadith_today":
        await query.message.reply_text(
            "📖 **حديث صحيح وتفسيره:**\n\n"
            "عن عمر بن الخطاب رضي الله عنه قال: سمعت رسول الله ﷺ يقول: «إنما الأعمال بالنيات...» [رواه البخاري]\n\n"
            "📝 **التفسير:** صلاح العمل بصلاح النية، فمن جعل وجهته لله نال القبول والرفعة."
        )
    elif query.data == "about_bot":
        await query.message.reply_text("🤖 بوت إسلامي دعوي شامل يقوم بإرسال الأذكار، التنبيهات، وقصص السلف تلقائياً.")

async def send_night_prayer_reminder(context: ContextTypes.DEFAULT_TYPE):
    text = "🌌 **صلاة قيام الليل يا مؤمن**\n\nالله يبارك فيكم، الليل راه في ثلثه الأخير، قوموا ركعتين واغتنموا هذه النفحات المباركة والدعوات المستجابة ☝🏻"
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue

async def send_daily_stories(context: ContextTypes.DEFAULT_TYPE):
    text = "📚 **موعد القصص اليومية المباركة (9 مساءً)**\n\n🎭 **القصة الأولى (طرائف السلف):** ذكاء القاضي إياس بن معاوية في إثبات حكمة تحريم الخمر.\n\n☝🏻 **القصة الثانية:** ثبات زوجات الموحدين المرابطين على أرض الثغور."
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue

async def send_white_days_reminder(context: ContextTypes.DEFAULT_TYPE):
    text = "📅 **تذكير مبارك: صيام الأيام البيض يا غاليين!**\n\nغدوة إن شاء الله وتبدأ الأيام البيض (13، 14، و 15). صيامهم كصيام الدهر كله! ☝🏻"
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue

def main() -> None:
    if not BOT_TOKEN:
        logger.error("خطأ: لم يتم العثور على TELEGRAM_TOKEN في المتغيرات البيئية!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CallbackQueryHandler(button_click))

    try:
        jq = app.job_queue
        tz = pytz.timezone("Asia/Riyadh")

        jq.run_daily(send_night_prayer_reminder, time=dt_time(2, 0, 0, tzinfo=tz))
        jq.run_daily(send_daily_stories, time=dt_time(21, 0, 0, tzinfo=tz))
        jq.run_daily(send_white_days_reminder, time=dt_time(8, 0, 0, tzinfo=tz))
        logger.info("تم ضبط نظام الجدولة بنجاح.")
    except Exception as e:
        logger.warning(f"تحذير أثناء تشغيل الجدولة: {e}")

    logger.info("البوت يبدأ العمل الآن...")
    app.run_polling()

if __name__ == "__main__":
    main()
