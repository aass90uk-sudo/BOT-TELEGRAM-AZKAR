import os
import json
import logging
from datetime import time as dt_time
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, ConversationHandler, MessageHandler, filters
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# يدعم مشرفاً واحداً أو عدة مشرفين مفصولين بفاصلة: 123,456,789
_raw_admins = os.environ.get("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]

DATA_FILE = os.path.join(os.path.dirname(__file__), "users_data.json")

WAITING_BROADCAST = 1


def load_users():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("users", [])
    return []


def save_users(users):
    with open(DATA_FILE, "w") as f:
        json.dump({"users": users}, f)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id

    users = load_users()
    if user_id not in users:
        users.append(user_id)
        save_users(users)
        logger.info(f"عضو جديد: {user_id}")

    await update.message.reply_text(
        f"مرحباً {user.first_name} ☝🏻\n\n"
        "أهلاً بك في البوت الإسلامي الدعوي.\n"
        "ستصلك يومياً:\n"
        "🌌 تنبيه قيام الليل — الساعة 2:00 فجراً\n"
        "📚 قصتان إسلاميتان — الساعة 9:00 مساءً\n"
        "📅 تذكير صيام الأيام البيض — الساعة 8:00 صباحاً\n\n"
        "جزاكم الله خيراً ونفع بكم ☝🏻"
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ هذا الأمر للمشرف فقط.")
        return

    total = len(load_users())
    keyboard = [
        [InlineKeyboardButton(f"👥 المشتركون: {total} عضو", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 نشر رسالة للمشتركين", callback_data="admin_broadcast")]
    ]
    await update.message.reply_text(
        "🛠️ لوحة تحكم المشرف",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("⛔️ غير مصرح.")
        return ConversationHandler.END

    if query.data == "admin_stats":
        users = load_users()
        await query.message.reply_text(
            f"📊 إحصائيات البوت:\n\n"
            f"👥 إجمالي المشتركين: {len(users)} عضو"
        )
        return ConversationHandler.END

    elif query.data == "admin_broadcast":
        await query.message.reply_text(
            "📢 أرسل الرسالة التي تريد نشرها لجميع المشتركين:\n"
            "(أرسل /cancel للإلغاء)"
        )
        return WAITING_BROADCAST

    return ConversationHandler.END


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    message_text = update.message.text
    users = load_users()
    success = 0
    failed = 0

    await update.message.reply_text(f"⏳ جاري الإرسال لـ {len(users)} مشترك...")

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=message_text)
            success += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ تم الإرسال!\n\n"
        f"✔️ نجح: {success}\n"
        f"❌ فشل: {failed}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


# ─── المهام المجدولة ───────────────────────────────────────────────

async def send_stories_announcement(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 تنبيه مبارك ☝🏻\n\n"
        "بعد قليل — الساعة 9:00 مساءً — ستصلكم قصتان إسلاميتان منتقاتان:\n"
        "🎭 قصة من طرائف السلف الصالح\n"
        "☝🏻 قصة من واقع المرابطين اليوم\n\n"
        "استعدوا واجعلوا قلوبكم حاضرة 💙"
    )
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


async def send_story_one(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 القصة الأولى — طرائف السلف الصالح\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎭 ذكاء القاضي إياس بن معاوية رحمه الله\n\n"
        "كان إياس بن معاوية من أذكى قضاة المسلمين في عصره، وكان الناس يُضرب بذكائه المثل.\n\n"
        "جاءه يوماً رجل يشكو جاره، فقال: يا قاضي، جاري يشرب الخمر في بيته!\n\n"
        "فقال إياس: كيف علمتَ أنه يشرب الخمر؟\n\n"
        "قال الرجل: رأيته يشتري العنب ويعصره في إناء ويتركه حتى يختمر، ثم يشربه!\n\n"
        "فقال إياس بهدوء: وأنت كيف تعرف أن ما يتركه يختمر؟ هل ذقتَه أنت؟\n\n"
        "فارتبك الرجل وسكت، ففهم الحاضرون أن الشاهد نفسه كان يشرب.\n\n"
        "فأسقط إياس شهادته وقال: لا تقبل شهادة من يشهد على نفسه بالمعصية.\n\n"
        "📝 الفائدة: العدل والذكاء في القضاء من أعظم صفات القاضي المسلم، "
        "وكان السلف يحرصون على أن يكون الحكم بالحق لا بالهوى أو التسرع."
    )
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


async def send_story_two(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌙 القصة الثانية — من واقع المرابطين اليوم\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "☝🏻 ثبات المرأة المؤمنة في زمن الفتن\n\n"
        "في إحدى مناطق الثغور، كانت امرأة مؤمنة زوجها مرابط بعيد عنها أشهراً طويلة. "
        "كانت تربي أطفالها على الصلاة والقرآن، وتُعلمهم أن أباهم ذهب ليحمي دينهم وأرضهم.\n\n"
        "ذات يوم سألها ابنها الصغير: أمي، لماذا أبي لا يرجع مثل آباء أصدقائي؟\n\n"
        "فأجابته بدموع ممزوجة بالإيمان: بُنيّ، أبوك اختار أن يكون مع الله، "
        "ونحن اخترنا أن نصبر معه. وكل ليلة تصبر فيها هي درجة في الجنة لنا جميعاً.\n\n"
        "ثم أخذت تعلم ابنها سورة آل عمران حتى حفظها في تلك الليلة.\n\n"
        "لما عاد الزوج بعد أشهر وجد أطفاله يحفظون القرآن، وزوجته أقوى إيماناً مما تركها.\n\n"
        "فبكى وقال: والله ما الثغر الذي أنا فيه بأعظم من الثغر الذي صمدتِ فيه أنتِ.\n\n"
        "📝 الفائدة: الصبر الجميل في البيت عبادة عظيمة، "
        "وتربية الأبناء على الإيمان في غياب الأب جهاد حقيقي لا يقل شأناً عن غيره."
    )
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


async def send_night_prayer_reminder(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌌 قيام الليل يا أهل الخير\n\n"
        "الساعة الآن 2:00 فجراً — والليل في ثلثه الأخير.\n\n"
        "قال ﷺ: «يَنْزِلُ رَبُّنَا تَبَارَكَ وَتَعَالَى كُلَّ لَيْلَةٍ إِلَى السَّمَاءِ الدُّنْيَا "
        "حِينَ يَبْقَى ثُلُثُ اللَّيْلِ الآخِرُ فَيَقُولُ: مَنْ يَدْعُونِي فَأَسْتَجِيبَ لَهُ؟ "
        "مَنْ يَسْأَلُنِي فَأُعْطِيَهُ؟ مَنْ يَسْتَغْفِرُنِي فَأَغْفِرَ لَهُ؟» [متفق عليه]\n\n"
        "قوموا ركعتين وادعوا الله — فالدعاء في هذا الوقت مستجاب ☝🏻"
    )
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


async def send_white_days_reminder(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📅 تذكير مبارك — صيام الأيام البيض ☝🏻\n\n"
        "غداً تبدأ الأيام البيض: 13 و 14 و 15 من الشهر الهجري.\n\n"
        "قال ﷺ: «صِيَامُ ثَلاَثَةِ أَيَّامٍ مِنْ كُلِّ شَهْرٍ صِيَامُ الدَّهْرِ كُلِّهِ» [متفق عليه]\n\n"
        "فمن استطاع فليصم ولينوِ النية الليلة 🤍"
    )
    for uid in load_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


def main() -> None:
    if not BOT_TOKEN:
        logger.error("خطأ: TELEGRAM_TOKEN غير موجود!")
        return

    if not ADMIN_IDS:
        logger.warning("تحذير: ADMIN_ID غير محدد — لوحة الإدارة لن تعمل.")
    else:
        logger.info(f"✅ المشرفون: {ADMIN_IDS}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_button, pattern="^admin_")],
        states={
            WAITING_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(broadcast_conv)

    try:
        jq = app.job_queue
        tz = pytz.timezone("Asia/Riyadh")

        jq.run_daily(send_night_prayer_reminder,   time=dt_time(2, 0, 0, tzinfo=tz))
        jq.run_daily(send_white_days_reminder,     time=dt_time(8, 0, 0, tzinfo=tz))
        jq.run_daily(send_stories_announcement,    time=dt_time(20, 45, 0, tzinfo=tz))
        jq.run_daily(send_story_one,               time=dt_time(21, 0, 0, tzinfo=tz))
        jq.run_daily(send_story_two,               time=dt_time(21, 5, 0, tzinfo=tz))

        logger.info("✅ تم ضبط نظام الجدولة بنجاح.")
    except Exception as e:
        logger.warning(f"تحذير الجدولة: {e}")

    logger.info("البوت يعمل الآن...")
    app.run_polling()


if __name__ == "__main__":
    main()
