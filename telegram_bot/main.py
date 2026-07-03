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

BOT_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_raw_admins = os.environ.get("ADMIN_ID", "")
ADMIN_IDS   = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]

DATA_FILE = os.path.join(os.path.dirname(__file__), "users_data.json")

# حالات المحادثة
WAITING_PASSWORD  = 1
WAITING_BROADCAST = 2


# ─── قاعدة بيانات المستخدمين ──────────────────────────────────────

def load_users():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("users", [])
    return []


def save_users(users):
    with open(DATA_FILE, "w") as f:
        json.dump({"users": users}, f)


# ─── مساعدات الإدارة ──────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_authenticated(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يتحقق إذا كان المشرف قد أدخل كلمة المرور في هذه الجلسة."""
    return context.user_data.get("admin_auth") is True


async def show_admin_panel(target, context):
    """يعرض لوحة التحكم — يقبل message أو callback_query."""
    total = len(load_users())
    keyboard = [
        [InlineKeyboardButton(f"👥 المشتركون: {total} عضو", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 نشر رسالة للمشتركين",     callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔒 تسجيل الخروج",            callback_data="admin_logout")]
    ]
    text = "🛠️ لوحة تحكم المشرف\nاختر ما تريد:"
    markup = InlineKeyboardMarkup(keyboard)

    if hasattr(target, "reply_text"):
        await target.reply_text(text, reply_markup=markup)
    else:
        await target.message.reply_text(text, reply_markup=markup)


# ─── /start ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    users = load_users()
    if user.id not in users:
        users.append(user.id)
        save_users(users)
        logger.info(f"عضو جديد: {user.id}")

    await update.message.reply_text(
        f"مرحباً {user.first_name} ☝🏻\n\n"
        "أهلاً بك في البوت الإسلامي الدعوي.\n"
        "ستصلك يومياً:\n"
        "🌌 تنبيه قيام الليل — الساعة 2:00 فجراً\n"
        "📚 قصتان إسلاميتان — الساعة 9:00 مساءً\n"
        "📅 تذكير صيام الأيام البيض — الساعة 8:00 صباحاً\n\n"
        "جزاكم الله خيراً ونفع بكم ☝🏻"
    )


# ─── /admin — نقطة البداية ────────────────────────────────────────

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔️ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    # إذا كان مصادقاً مسبقاً في هذه الجلسة
    if is_authenticated(context):
        await show_admin_panel(update.message, context)
        return WAITING_BROADCAST

    await update.message.reply_text(
        "🔐 لوحة تحكم المشرف\n\n"
        "أدخل كلمة المرور السرية للمتابعة:\n"
        "(أرسل /cancel للإلغاء)"
    )
    return WAITING_PASSWORD


async def check_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    entered = update.message.text.strip()

    if not ADMIN_PASSWORD:
        await update.message.reply_text("⚠️ لم يتم تعيين ADMIN_PASSWORD في المتغيرات البيئية.")
        return ConversationHandler.END

    if entered == ADMIN_PASSWORD:
        context.user_data["admin_auth"] = True
        logger.info(f"مشرف سجّل دخولاً: {update.effective_user.id}")
        await update.message.reply_text("✅ كلمة المرور صحيحة!")
        await show_admin_panel(update.message, context)
        return WAITING_BROADCAST
    else:
        await update.message.reply_text(
            "❌ كلمة المرور خاطئة.\n"
            "أعد المحاولة أو أرسل /cancel للإلغاء."
        )
        return WAITING_PASSWORD


# ─── أزرار لوحة التحكم ────────────────────────────────────────────

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id) or not is_authenticated(context):
        await query.message.reply_text("⛔️ غير مصرح. أرسل /admin وأدخل كلمة المرور.")
        return ConversationHandler.END

    if query.data == "admin_stats":
        users = load_users()
        await query.message.reply_text(
            f"📊 إحصائيات البوت:\n\n"
            f"👥 إجمالي المشتركين: {len(users)} عضو"
        )
        return WAITING_BROADCAST

    elif query.data == "admin_broadcast":
        await query.message.reply_text(
            "📢 أرسل الرسالة التي تريد نشرها لجميع المشتركين:\n"
            "(أرسل /cancel للإلغاء)"
        )
        return WAITING_BROADCAST

    elif query.data == "admin_logout":
        context.user_data["admin_auth"] = False
        await query.message.reply_text("🔒 تم تسجيل الخروج بنجاح.")
        return ConversationHandler.END

    return WAITING_BROADCAST


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id) or not is_authenticated(context):
        return ConversationHandler.END

    message_text = update.message.text
    users = load_users()
    success, failed = 0, 0

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


# ─── المهام المجدولة ──────────────────────────────────────────────

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


# ─── التشغيل ──────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("خطأ: TELEGRAM_TOKEN غير موجود!")
        return
    if not ADMIN_IDS:
        logger.warning("تحذير: ADMIN_ID غير محدد.")
    if not ADMIN_PASSWORD:
        logger.warning("تحذير: ADMIN_PASSWORD غير محدد — لوحة الإدارة بدون حماية!")
    else:
        logger.info(f"✅ المشرفون: {ADMIN_IDS} | كلمة المرور: مضبوطة")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)
            ],
            WAITING_BROADCAST: [
                CallbackQueryHandler(admin_button, pattern="^admin_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(admin_conv)

    try:
        jq  = app.job_queue
        tz  = pytz.timezone("Asia/Riyadh")
        jq.run_daily(send_night_prayer_reminder, time=dt_time(2,  0, 0, tzinfo=tz))
        jq.run_daily(send_white_days_reminder,   time=dt_time(8,  0, 0, tzinfo=tz))
        jq.run_daily(send_stories_announcement,  time=dt_time(20, 45, 0, tzinfo=tz))
        jq.run_daily(send_story_one,             time=dt_time(21, 0, 0, tzinfo=tz))
        jq.run_daily(send_story_two,             time=dt_time(21, 5, 0, tzinfo=tz))
        logger.info("✅ تم ضبط الجدولة.")
    except Exception as e:
        logger.warning(f"تحذير الجدولة: {e}")

    logger.info("البوت يعمل الآن...")
    app.run_polling()


if __name__ == "__main__":
    main()
