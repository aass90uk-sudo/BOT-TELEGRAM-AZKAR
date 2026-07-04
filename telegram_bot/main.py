import io
import os
import json
import logging
import threading
import time
from datetime import datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz
from hijri_converter import Gregorian
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, ConversationHandler, MessageHandler, filters
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Health Check Server ───────────────────────────────────────────

BOT_START_TIME = datetime.utcnow()

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        uptime = (datetime.utcnow() - BOT_START_TIME).seconds
        body   = (
            f"status: ok\n"
            f"uptime: {uptime}s\n"
            f"bot: running\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # إخفاء logs الطلبات العادية


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health check server على المنفذ {port}")
    server.serve_forever()


BOT_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_raw_admins = os.environ.get("ADMIN_ID", "")
ADMIN_IDS   = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]

DATA_FILE = os.path.join(os.path.dirname(__file__), "users_data.json")
TZ_RIYADH = pytz.timezone("Asia/Riyadh")

WAITING_PASSWORD  = 1
WAITING_BROADCAST = 2
WAITING_IMPORT    = 3

# ─── قاعدة بيانات ─────────────────────────────────────────────────

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"users": [], "banned": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # ترحيل من الصيغة القديمة (قائمة IDs) إلى الجديدة
    if data.get("users") and isinstance(data["users"][0], int):
        data["users"] = [
            {"id": uid, "first_name": "—", "username": None, "joined": "—"}
            for uid in data["users"]
        ]
        save_data(data)
    # إضافة قائمة الحظر إن لم تكن موجودة
    if "banned" not in data:
        data["banned"] = []
        save_data(data)
    return data


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_ids() -> list:
    data = load_data()
    banned = set(data.get("banned", []))
    return [u["id"] for u in data["users"] if u["id"] not in banned]


def is_banned(user_id: int) -> bool:
    return user_id in load_data().get("banned", [])


def find_user(user_id: int) -> dict | None:
    for u in load_data()["users"]:
        if u["id"] == user_id:
            return u
    return None


# ─── مساعدات ──────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_authenticated(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("admin_auth") is True


async def show_admin_panel(target, context):
    data    = load_data()
    total   = len(data["users"])
    banned  = len(data.get("banned", []))
    keyboard = [
        [InlineKeyboardButton(f"👥 قائمة المشتركين ({total})", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 نشر رسالة للمشتركين",         callback_data="admin_broadcast")],
        [InlineKeyboardButton(f"🚫 المحظورون ({banned})",        callback_data="admin_banned")],
        [InlineKeyboardButton("📦 نسخة احتياطية للمشتركين",     callback_data="admin_backup")],
        [InlineKeyboardButton("📥 استيراد مشتركين (JSON)",      callback_data="admin_import")],
        [InlineKeyboardButton("🔒 تسجيل الخروج",                callback_data="admin_logout")]
    ]
    text   = "🛠️ لوحة تحكم المشرف\nاختر ما تريد:"
    markup = InlineKeyboardMarkup(keyboard)
    if hasattr(target, "reply_text"):
        await target.reply_text(text, reply_markup=markup)
    else:
        await target.message.reply_text(text, reply_markup=markup)


# ─── /start ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("عذراً، لا يمكنك استخدام هذا البوت.")
        return

    data = load_data()
    ids  = [u["id"] for u in data["users"]]

    if user.id not in ids:
        now   = datetime.now(TZ_RIYADH).strftime("%Y-%m-%d %H:%M")
        entry = {
            "id":         user.id,
            "first_name": user.first_name or "—",
            "last_name":  user.last_name  or "",
            "username":   user.username   or None,
            "joined":     now,
        }
        data["users"].append(entry)
        save_data(data)
        logger.info(f"عضو جديد: {user.id} | {user.first_name}")

        # إشعار المشرفين
        uname = f"@{user.username}" if user.username else "لا يوجد"
        notif = (
            "🔔 مشترك جديد انضم للبوت!\n\n"
            f"👤 الاسم: {user.first_name} {user.last_name or ''}\n"
            f"🆔 المعرّف: {user.id}\n"
            f"📛 اليوزر: {uname}\n"
            f"🕐 التاريخ: {now}\n"
            f"👥 إجمالي المشتركين: {len(data['users'])}"
        )
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=notif)
            except Exception:
                pass

    await update.message.reply_text(
        f"السلام عليكم ورحمة الله وبركاته 🌿\n"
        f"أهلاً وسهلاً بك يا {user.first_name} في البوت الإسلامي الدعوي.\n\n"
        "هذا البوت يُرسل إليك يومياً:\n\n"
        "🌄 قصة من أرض الجزائر الشامخة — الساعة 9:00 صباحاً\n"
        "   (قصة من صالحي زماننا مع عبرتها وسؤالين للتأمل)\n\n"
        "🌌 تنبيه قيام الليل — الساعة 2:00 فجراً\n\n"
        "📚 قصتان إسلاميتان من السلف — الساعة 9:00 مساءً\n\n"
        "📅 تذكير صيام الأيام البيض — أيام 13 و14 و15 من كل شهر هجري\n\n"
        "نسأل الله أن ينفع بهذا البوت وأن يجعله في ميزان حسناتكم ☝🏻"
    )


# ─── /admin ───────────────────────────────────────────────────────

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    if is_authenticated(context):
        await show_admin_panel(update.message, context)
        return WAITING_BROADCAST

    await update.message.reply_text(
        "🔐 لوحة تحكم المشرف\n\n"
        "أدخل كلمة المرور السرية:\n"
        "(أرسل /cancel للإلغاء)"
    )
    return WAITING_PASSWORD


async def check_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not ADMIN_PASSWORD:
        await update.message.reply_text("⚠️ لم يتم تعيين ADMIN_PASSWORD.")
        return ConversationHandler.END

    if update.message.text.strip() == ADMIN_PASSWORD:
        context.user_data["admin_auth"] = True
        logger.info(f"مشرف سجّل دخولاً: {update.effective_user.id}")
        await update.message.reply_text("✅ كلمة المرور صحيحة!")
        await show_admin_panel(update.message, context)
        return WAITING_BROADCAST

    await update.message.reply_text("❌ كلمة المرور خاطئة.\nأعد المحاولة أو /cancel للإلغاء.")
    return WAITING_PASSWORD


# ─── أزرار لوحة التحكم ────────────────────────────────────────────

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id) or not is_authenticated(context):
        await query.message.reply_text("⛔️ غير مصرح. أرسل /admin وأدخل كلمة المرور.")
        return ConversationHandler.END

    data = query.data

    # ── قائمة المشتركين ──
    if data == "admin_stats":
        users = load_data()["users"]
        if not users:
            await query.message.reply_text("لا يوجد مشتركون بعد.")
            return WAITING_BROADCAST

        await query.message.reply_text(
            f"📊 إجمالي المشتركين: {len(users)} عضو\n"
            "اضغط على أي اسم لرؤية تفاصيله:"
        )

        # إرسال قائمة بأزرار (10 في كل رسالة)
        chunk_size = 10
        for i in range(0, len(users), chunk_size):
            chunk = users[i:i + chunk_size]
            keyboard = []
            for u in chunk:
                name  = u.get("first_name", "—")
                lname = u.get("last_name", "")
                label = f"👤 {name} {lname}".strip() + f"  |  🆔 {u['id']}"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"user_{u['id']}")])
            await query.message.reply_text(
                f"المشتركون {i+1} — {min(i+chunk_size, len(users))}:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return WAITING_BROADCAST

    # ── تفاصيل مشترك واحد ──
    if data.startswith("user_"):
        uid  = int(data.split("_")[1])
        user = find_user(uid)
        if not user:
            await query.message.reply_text("لم يتم العثور على المشترك.")
            return WAITING_BROADCAST

        uname    = f"@{user['username']}" if user.get("username") else "لا يوجد"
        lname    = user.get("last_name", "")
        fullname = f"{user.get('first_name','—')} {lname}".strip()
        banned   = is_banned(uid)

        # رابط فتح حسابه مباشرة في تليجرام
        if user.get("username"):
            link_btn = InlineKeyboardButton("🔗 فتح الحساب", url=f"https://t.me/{user['username']}")
        else:
            link_btn = InlineKeyboardButton("🔗 فتح الحساب", url=f"tg://user?id={uid}")

        ban_btn = (
            InlineKeyboardButton("✅ إلغاء الحظر", callback_data=f"unban_{uid}")
            if banned else
            InlineKeyboardButton("🚫 حظر المشترك", callback_data=f"ban_{uid}")
        )

        keyboard = [
            [link_btn],
            [ban_btn],
            [InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="admin_stats")]
        ]

        status = "🔴 محظور" if banned else "🟢 نشط"
        await query.message.reply_text(
            f"👤 بيانات المشترك\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"📝 الاسم الكامل: {fullname}\n"
            f"🆔 المعرّف (ID): {uid}\n"
            f"📛 اليوزر: {uname}\n"
            f"📅 تاريخ الانضمام: {user.get('joined','—')}\n"
            f"📌 الحالة: {status}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_BROADCAST

    # ── حظر مشترك ──
    if data.startswith("ban_"):
        uid  = int(data.split("_")[1])
        db   = load_data()
        if uid not in db["banned"]:
            db["banned"].append(uid)
            save_data(db)
        user = find_user(uid)
        name = user.get("first_name", str(uid)) if user else str(uid)
        await query.message.reply_text(f"🚫 تم حظر {name} ({uid}) بنجاح.\nلن يتلقى أي رسائل بعد الآن.")
        return WAITING_BROADCAST

    # ── إلغاء حظر مشترك ──
    if data.startswith("unban_"):
        uid  = int(data.split("_")[1])
        db   = load_data()
        if uid in db["banned"]:
            db["banned"].remove(uid)
            save_data(db)
        user = find_user(uid)
        name = user.get("first_name", str(uid)) if user else str(uid)
        await query.message.reply_text(f"✅ تم إلغاء حظر {name} ({uid}).\nسيتلقى الرسائل مجدداً.")
        return WAITING_BROADCAST

    # ── قائمة المحظورين ──
    if data == "admin_banned":
        db      = load_data()
        banned  = db.get("banned", [])
        if not banned:
            await query.message.reply_text("✅ لا يوجد أي مشترك محظور حالياً.")
            return WAITING_BROADCAST
        keyboard = []
        for uid in banned:
            user = find_user(uid)
            name = f"{user.get('first_name','—')}" if user else "—"
            keyboard.append([InlineKeyboardButton(
                f"🔴 {name}  |  🆔 {uid}",
                callback_data=f"user_{uid}"
            )])
        keyboard.append([InlineKeyboardButton("◀️ رجوع", callback_data="admin_stats")])
        await query.message.reply_text(
            f"🚫 المحظورون ({len(banned)}):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_BROADCAST

    # ── نسخة احتياطية ──
    if data == "admin_backup":
        db       = load_data()
        raw      = json.dumps(db, ensure_ascii=False, indent=2).encode("utf-8")
        bio      = io.BytesIO(raw)
        bio.name = "users_data.json"
        total    = len(db["users"])
        banned   = len(db.get("banned", []))
        await query.message.reply_document(
            document=bio,
            filename="users_data.json",
            caption=(
                f"📦 نسخة احتياطية\n"
                f"👥 المشتركون: {total}\n"
                f"🚫 المحظورون: {banned}\n"
                f"📅 {datetime.now(TZ_RIYADH).strftime('%Y-%m-%d %H:%M')}"
            )
        )
        return WAITING_BROADCAST

    # ── استيراد مشتركين ──
    if data == "admin_import":
        await query.message.reply_text(
            "📥 أرسل ملف JSON الخاص بالمشتركين لاستيراده.\n"
            "يجب أن يكون بنفس صيغة ملف النسخ الاحتياطي.\n\n"
            "(أرسل /cancel للإلغاء)"
        )
        return WAITING_IMPORT

    # ── نشر رسالة ──
    if data == "admin_broadcast":
        await query.message.reply_text(
            "📢 أرسل الرسالة التي تريد نشرها لجميع المشتركين:\n"
            "(أرسل /cancel للإلغاء)"
        )
        return WAITING_BROADCAST

    # ── تسجيل الخروج ──
    if data == "admin_logout":
        context.user_data["admin_auth"] = False
        await query.message.reply_text("🔒 تم تسجيل الخروج.")
        return ConversationHandler.END

    return WAITING_BROADCAST


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id) or not is_authenticated(context):
        return ConversationHandler.END

    message_text   = update.message.text
    user_ids       = get_user_ids()
    success, failed = 0, 0

    await update.message.reply_text(f"⏳ جاري الإرسال لـ {len(user_ids)} مشترك...")

    for uid in user_ids:
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


async def receive_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id) or not is_authenticated(context):
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ يرجى إرسال ملف JSON فقط.")
        return WAITING_IMPORT

    try:
        tg_file = await doc.get_file()
        raw     = await tg_file.download_as_bytearray()
        imported = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await update.message.reply_text(f"❌ فشل قراءة الملف: {e}")
        return WAITING_IMPORT

    if "users" not in imported:
        await update.message.reply_text("❌ الملف لا يحتوي على مفتاح 'users'. تأكد أنه ملف نسخ احتياطي صحيح.")
        return WAITING_IMPORT

    current     = load_data()
    existing_ids = {u["id"] for u in current["users"]}
    added       = 0

    for user in imported.get("users", []):
        if isinstance(user, int):
            # صيغة قديمة — تحويل
            user = {"id": user, "first_name": "—", "username": None, "joined": "—"}
        if user.get("id") not in existing_ids:
            current["users"].append(user)
            existing_ids.add(user["id"])
            added += 1

    # دمج قائمة الحظر
    imported_banned = imported.get("banned", [])
    for uid in imported_banned:
        if uid not in current["banned"]:
            current["banned"].append(uid)

    save_data(current)
    await update.message.reply_text(
        f"✅ تم الاستيراد بنجاح!\n\n"
        f"➕ مشتركون جدد مضافون: {added}\n"
        f"🚫 محظورون مستوردون: {len(imported_banned)}\n"
        f"👥 إجمالي المشتركين الآن: {len(current['users'])}"
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
    for uid in get_user_ids():
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
    for uid in get_user_ids():
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
    for uid in get_user_ids():
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
    for uid in get_user_ids():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


async def send_white_days_reminder(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ_RIYADH).date()
    hijri_day = Gregorian.fromdate(today).to_hijri().day
    if hijri_day not in (13, 14, 15):
        return

    day_labels = {13: "اليوم الأول", 14: "اليوم الثاني", 15: "اليوم الثالث والأخير"}
    label = day_labels[hijri_day]

    text = (
        f"📅 تذكير مبارك — صيام الأيام البيض ☝🏻\n"
        f"اليوم هو {label} ({hijri_day} من الشهر الهجري)\n\n"
        "قال ﷺ: «صِيَامُ ثَلاَثَةِ أَيَّامٍ مِنْ كُلِّ شَهْرٍ صِيَامُ الدَّهْرِ كُلِّهِ» [متفق عليه]\n\n"
        "فمن لم يصم بعد فلينوِ الآن ويُفطر على نية الصيام غداً إن بقي يوم 🤍\n"
        "ومن صام فله البشرى بأجر عظيم إن شاء الله ☝🏻"
    )
    for uid in get_user_ids():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


# ─── قصص جزائرية صباحية ───────────────────────────────────────────

ALGERIA_STORIES = [
    {
        "title": "الشيخ الذي لم تُطفئه العشرية",
        "body": (
            "في قرية صغيرة بالأوراس، كان الشيخ عبد الحميد يُعلّم أبناء القرية القرآن الكريم "
            "في غرفة بيته المتواضعة، وذلك في أحلك سنوات العشرية السوداء في التسعينيات.\n\n"
            "حين جاءه من يُحذّره ويقول له: «يا شيخ، إنهم يقتلون العلماء، أغلق المسجد وابقَ في بيتك»، "
            "رفع رأسه وقال بهدوء: «أنا لا أُعلّمهم لأجل الدنيا، وإن كان ربي يريد أن أموت وأنا أعلّم كلامه، "
            "فتلك خاتمة أتمناها».\n\n"
            "واصل الشيخ دروسه سبع سنوات دون انقطاع، وأخرج في تلك الفترة أكثر من أربعين حافظاً للقرآن. "
            "واليوم يُفتخر بهم في مساجد الجزائر وأوروبا. "
            "أما الشيخ فقد مات على فراشه وهو يُردّد سورة يس، بعد أن أكمل تعليم آخر تلاميذه."
        ),
        "moral": (
            "العبرة: الثبات على الحق في أوقات الفتن هو أعظم أنواع الجهاد. "
            "ومن أخلص النية لله في عمله، حفظه الله وبارك في أثره حتى بعد مماته."
        ),
        "q1": "ما الذي جعل الشيخ يستمر في التعليم رغم الخطر؟ وكيف تُطبّق هذا الثبات في حياتك؟",
        "q2": "كيف يكون المعلم الصادق سبباً في نجاة أمة بأكملها؟ اذكر شاهداً من التاريخ الإسلامي.",
    },
    {
        "title": "الشاب الذي عاد من باريس",
        "body": (
            "غادر كريم الجزائر وهو ابن عشرين عاماً باحثاً عن العمل في باريس. "
            "وجد الدنيا مفتوحةً أمامه، لكنه وجد قلبه مُغلقاً.\n\n"
            "بعد أربع سنوات من الضياع، دخل ذات ليلة مسجداً صغيراً في ضاحية باريسية هرباً من المطر. "
            "سمع الإمام يتلو: ﴿أَلَا بِذِكْرِ اللَّهِ تَطْمَئِنُّ الْقُلُوبُ﴾، فبكى بكاءً لم يعرفه من قبل.\n\n"
            "قرّر أن يعود إلى الجزائر. اشترى مصحفاً وبدأ الحفظ في الباخرة أثناء العودة. "
            "بعد ثلاث سنوات من الجهد، أصبح حافظاً للقرآن وإماماً في مسجد حيّه. "
            "ويقول اليوم لشباب حيّه: «الغربة علّمتني أن كل شيء يُوجد في الدنيا إلا الطمأنينة، "
            "وهي عند الله وحده»."
        ),
        "moral": (
            "العبرة: القلب الذي خُلق لمعرفة الله لا يرتاح إلا بالرجوع إليه، "
            "مهما طال طريق الضياع. والتوبة الصادقة تمحو الماضي وتفتح آفاقاً لم يكن العبد يحلم بها."
        ),
        "q1": "ما الذي أوقف كريم عند سماع الآية؟ وهل مررت بلحظة غيّرت مسار حياتك؟",
        "q2": "كيف يكون القرآن الكريم طريقاً للعودة إلى الله؟ اذكر آية تُعبّر عن ذلك.",
    },
    {
        "title": "أم المجاهدين من تلمسان",
        "body": (
            "في مدينة تلمسان العتيقة، ربّت الحاجة فاطمة سبعةً من أبنائها وحدها بعد أن اعتُقل زوجها "
            "لسنوات بسبب دعوته إلى الله.\n\n"
            "لم تُعرف الحاجة فاطمة بالشكوى ولا بالبكاء أمام أطفالها، بل كانت تقول لهم كل صباح: "
            "«أبوكم في سبيل الله، ونحن في سبيل الله، فلا خوف ولا حزن».\n\n"
            "كانت تعمل بيدها وتُحيي الليل بالقرآن والدعاء. وحين سُئلت: كيف تصبرين؟ "
            "قالت: «أنا لا أصبر، أنا أثق».\n\n"
            "لما أُطلق سراح زوجها بعد خمس سنوات، وجد أبناءه السبعة يحفظون القرآن الكريم. "
            "فسجد شكراً لله وقال: «يا رب، ما ظننت أن السجن سيكون خيراً، لكنك كنت ترعاهم "
            "أحسن مني»."
        ),
        "moral": (
            "العبرة: الثقة بالله لا تعني غياب الألم، بل تعني اليقين بأن الله لا يضيع أجر من أحسن عملاً. "
            "والأم الصالحة حصنٌ للأمة قبل أن تكون حصناً للبيت."
        ),
        "q1": "ما الفرق بين الصبر والثقة كما فهمتَه من كلام الحاجة فاطمة؟",
        "q2": "كيف يؤثّر ثبات الأم في ظروف الشدة على تربية أبنائها؟ اذكر مثالاً من واقعنا.",
    },
    {
        "title": "الطبيب الجزائري الذي أبى الرشوة",
        "body": (
            "كان الدكتور يوسف طبيباً في مستشفى حكومي بالجزائر العاصمة. "
            "عُرضت عليه مناصب ومبالغ مالية كبيرة مقابل أن يُوقّع على وثائق فيها ظلم للمرضى الفقراء "
            "وتحويل ميزانيات لصالح المسؤولين.\n\n"
            "رفض الدكتور يوسف بهدوء، وقال: «أنا أمام الله قبل أن أكون أمام المسؤولين». "
            "نُقل من منصبه إلى قسم نائٍ، وضُيّق عليه في الراتب والترقيات.\n\n"
            "لم يُغادر يوسف المستشفى ولم يستسلم. بدأ يُعالج فقراء الحي مجاناً في وقت فراغه. "
            "انتشر خبره حتى كان المرضى يأتون من ولايات بعيدة. "
            "وحين سألوه: ألا تتمنى أن تكون في مستشفى خاص وتجني المال؟ "
            "قال: «أنا أبحث عن رضا الله، والمريض الفقير أقرب طريق إليه»."
        ),
        "moral": (
            "العبرة: الاستقامة في العمل في زمن الفساد هي جهاد حقيقي. "
            "ومن ترك شيئاً لله عوّضه الله خيراً منه، وجعل له مكانةً في قلوب الناس لا تُشترى بمال."
        ),
        "q1": "ما الثمن الذي دفعه الدكتور يوسف بسبب استقامته؟ وهل ترى أن اختياره كان صحيحاً؟",
        "q2": "كيف يكون إتقان العمل وصدق الأمانة عبادةً يُؤجر عليها الإنسان؟",
    },
    {
        "title": "الشابة التي حافظت على حجابها في الجامعة",
        "body": (
            "التحقت إيمان بالجامعة في مدينة وهران، وكانت الوحيدة المحجبة في قسمها. "
            "لم تسلم من السخرية والضغوط — من الأستاذة التي تقول لها «هذا تخلّف» "
            "إلى الزميلات اللواتي يبتعدن عنها.\n\n"
            "كانت إيمان تبكي في الليل وتدعو: «يا الله، اجعل الحجاب راحةً لي لا عبئاً». "
            "واستمرت في دراستها متفوقةً، ولم تتخلَّ عن شيء مما تعتقده.\n\n"
            "بعد خمس سنوات، تخرّجت بامتياز وأصبحت مهندسةً في شركة محترمة. "
            "وبدأت زميلاتها اللواتي كنّ يسخرن منها يستشرنها ويقلن لها: "
            "«أنتِ كنتِ دائماً الأقوى منّا، كنّا نحن الخائفات لا أنتِ»."
        ),
        "moral": (
            "العبرة: الهوية الإسلامية الراسخة لا تكون عبئاً بل درعاً. "
            "ومن صبر على أذى الناس في طاعة الله، جعل الله له عاقبةً حميدة وجعل أعداءه شهوداً على ثباته."
        ),
        "q1": "ما الذي أعان إيمان على الثبات وسط الضغوط؟ وكيف تستفيد من تجربتها؟",
        "q2": "قال النبي ﷺ: «عجباً لأمر المؤمن، إن أمره كله خير» — كيف تجلّى هذا في قصة إيمان؟",
    },
]


async def send_algeria_story(context: ContextTypes.DEFAULT_TYPE):
    from datetime import date
    idx   = date.today().timetuple().tm_yday % len(ALGERIA_STORIES)
    story = ALGERIA_STORIES[idx]

    text = (
        f"🌄 قصة الصباح — من أرض الجزائر الشامخة\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"☝🏻 {story['title']}\n\n"
        f"{story['body']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 {story['moral']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤔 تأمّل وأجب في نفسك:\n\n"
        f"❓ السؤال الأول:\n{story['q1']}\n\n"
        f"❓ السؤال الثاني:\n{story['q2']}"
    )
    for uid in get_user_ids():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


# ─── بناء التطبيق ─────────────────────────────────────────────────

def build_app():
    """ينشئ تطبيقاً جديداً في كل مرة — ضروري لإعادة المحاولة الصحيحة."""
    token = (
        os.environ.get("TELEGRAM_TOKEN") or
        os.environ.get("BOT_TOKEN")       or
        os.environ.get("TOKEN")
    )
    if not token:
        raise RuntimeError(
            "لم يُعثر على توكن البوت! "
            "أضف أحد هذه المتغيرات: TELEGRAM_TOKEN أو BOT_TOKEN أو TOKEN"
        )

    app = ApplicationBuilder().token(token).build()

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)
            ],
            WAITING_BROADCAST: [
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast)
            ],
            WAITING_IMPORT: [
                MessageHandler(filters.Document.ALL, receive_import),
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_)"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(admin_conv)

    jq = app.job_queue
    tz = TZ_RIYADH
    jq.run_daily(send_algeria_story,         time=dt_time(9,  0, 0, tzinfo=tz))
    jq.run_daily(send_night_prayer_reminder, time=dt_time(2,  0, 0, tzinfo=tz))
    jq.run_daily(send_white_days_reminder,   time=dt_time(8,  0, 0, tzinfo=tz))
    jq.run_daily(send_stories_announcement,  time=dt_time(20, 45, 0, tzinfo=tz))
    jq.run_daily(send_story_one,             time=dt_time(21, 0, 0, tzinfo=tz))
    jq.run_daily(send_story_two,             time=dt_time(21, 5, 0, tzinfo=tz))

    return app


# ─── التشغيل ──────────────────────────────────────────────────────

def main() -> None:
    # تشخيص — يُظهر المفاتيح الموجودة فقط (بدون قيم)
    watched = ("TELEGRAM_TOKEN", "BOT_TOKEN", "TOKEN", "ADMIN_ID", "ADMIN_PASSWORD", "PORT")
    env_keys = [k for k in os.environ if k in watched]
    logger.info(f"🔑 متغيرات البيئة الموجودة: {env_keys}")

    token = (
        os.environ.get("TELEGRAM_TOKEN") or
        os.environ.get("BOT_TOKEN")       or
        os.environ.get("TOKEN")
    )
    if not token:
        logger.error(
            "❌ لم يُعثر على توكن البوت! "
            "أضف أحد هذه المتغيرات في Railway: TELEGRAM_TOKEN أو BOT_TOKEN أو TOKEN"
        )
        raise SystemExit(1)

    if not ADMIN_IDS:
        logger.warning("⚠️  ADMIN_ID غير محدد.")
    if not ADMIN_PASSWORD:
        logger.warning("⚠️  ADMIN_PASSWORD غير محدد.")
    else:
        logger.info(f"✅ المشرفون: {ADMIN_IDS}")

    # تشغيل health check server في خيط منفصل (مرة واحدة فقط)
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    logger.info("🚀 البوت يعمل الآن...")

    retry_delay = 5
    while True:
        try:
            logger.info("🔄 جاري بناء التطبيق وبدء الـ polling...")
            app = build_app()          # تطبيق جديد في كل محاولة
            app.run_polling(drop_pending_updates=True)
            logger.info("ℹ️  توقف polling بشكل طبيعي.")
            break
        except RuntimeError as e:
            logger.error(f"❌ خطأ فادح: {e}")
            raise SystemExit(1)        # خطأ في الإعداد — لا فائدة من إعادة المحاولة
        except Exception as e:
            logger.error(f"⚠️  توقف البوت: {e}")
            logger.info(f"⏳ إعادة المحاولة خلال {retry_delay} ثانية...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    main()
