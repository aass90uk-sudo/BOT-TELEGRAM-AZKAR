import io
import os
import json
import sqlite3
import logging
import threading
import time
from datetime import datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz
from hijri_converter import Gregorian
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
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
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health check server على المنفذ {port}")
    server.serve_forever()


BOT_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_raw_admins = os.environ.get("ADMIN_ID", "")
ADMIN_IDS   = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]

TZ_RIYADH = pytz.timezone("Asia/Riyadh")

WAITING_BROADCAST = 1
WAITING_IMPORT    = 2

ADMIN_PANEL_BTN = "🛠️ لوحة التحكم"

# ─── قاعدة بيانات SQLite ──────────────────────────────────────────

# مسار قابل للتهيئة عبر DATA_DIR (مفيد لـ Railway Volumes)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.environ.get("DATA_DIR", _BASE_DIR)
DB_FILE   = os.path.join(DATA_DIR, "bot.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """إنشاء الجداول وترحيل البيانات القديمة من JSON إن وجدت."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY,
                first_name TEXT    DEFAULT '—',
                last_name  TEXT    DEFAULT '',
                username   TEXT,
                joined     TEXT    DEFAULT '—'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.commit()
    _migrate_json()


def _migrate_json():
    """ترحيل تلقائي من users_data.json القديم إلى SQLite."""
    json_file = os.path.join(_BASE_DIR, "users_data.json")
    if not os.path.exists(json_file):
        return
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        with get_db() as conn:
            for u in data.get("users", []):
                if isinstance(u, int):
                    u = {"id": u, "first_name": "—", "username": None, "joined": "—"}
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, first_name, last_name, username, joined) "
                    "VALUES (?,?,?,?,?)",
                    (
                        u.get("id"),
                        u.get("first_name", "—"),
                        u.get("last_name", ""),
                        u.get("username"),
                        u.get("joined", "—"),
                    ),
                )
            for uid in data.get("banned", []):
                conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
            conn.commit()
        os.rename(json_file, json_file + ".migrated")
        logger.info("✅ تم ترحيل البيانات من JSON إلى SQLite بنجاح")
    except Exception as e:
        logger.error(f"⚠️ خطأ في ترحيل JSON: {e}")


# ─── دوال قاعدة البيانات ──────────────────────────────────────────

def load_data() -> dict:
    """تحميل كامل البيانات كـ dict — للنسخ الاحتياطية والاستيراد."""
    with get_db() as conn:
        users  = [dict(r) for r in conn.execute(
            "SELECT id, first_name, last_name, username, joined FROM users"
        ).fetchall()]
        banned = [r[0] for r in conn.execute("SELECT user_id FROM banned").fetchall()]
    return {"users": users, "banned": banned}


def save_data(data: dict):
    """استبدال كامل لجميع البيانات — يُستخدم عند الاستيراد فقط."""
    with get_db() as conn:
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM banned")
        for u in data.get("users", []):
            conn.execute(
                "INSERT INTO users (id, first_name, last_name, username, joined) VALUES (?,?,?,?,?)",
                (
                    u.get("id"),
                    u.get("first_name", "—"),
                    u.get("last_name", ""),
                    u.get("username"),
                    u.get("joined", "—"),
                ),
            )
        for uid in data.get("banned", []):
            conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
        conn.commit()


def add_user(entry: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, first_name, last_name, username, joined) "
            "VALUES (?,?,?,?,?)",
            (
                entry["id"],
                entry.get("first_name", "—"),
                entry.get("last_name", ""),
                entry.get("username"),
                entry.get("joined", "—"),
            ),
        )
        conn.commit()


def user_exists(user_id: int) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is not None


def get_user_ids() -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM banned)"
        ).fetchall()
    return [r[0] for r in rows]


def is_banned(user_id: int) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM banned WHERE user_id=?", (user_id,)).fetchone() is not None


def find_user(user_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def ban_user(user_id: int):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (user_id,))
        conn.commit()


def unban_user(user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
        conn.commit()


def delete_user(user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
        conn.commit()


def count_users() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_banned() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM banned").fetchone()[0]


def register_admins():
    """تسجيل المشرفين تلقائياً كمشتركين عند بدء البوت إن لم يكونوا مسجلين."""
    if not ADMIN_IDS:
        return
    now = datetime.now(TZ_RIYADH).strftime("%Y-%m-%d %H:%M")
    with get_db() as conn:
        for aid in ADMIN_IDS:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, first_name, joined) VALUES (?,?,?)",
                (aid, "مشرف", now),
            )
        conn.commit()
    logger.info(f"✅ تم تسجيل المشرفين كمشتركين: {ADMIN_IDS}")


# ─── مساعدات ──────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS



async def show_admin_panel(target, context):
    total  = count_users()
    banned = count_banned()
    active = total - banned
    keyboard = [
        [InlineKeyboardButton(f"👥 المشتركون ({active} نشط / {total} إجمالي)", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 نشر رسالة للمشتركين",      callback_data="admin_broadcast")],
        [InlineKeyboardButton(f"🚫 المحظورون ({banned})",     callback_data="admin_banned")],
        [InlineKeyboardButton("📦 نسخة احتياطية (JSON)",      callback_data="admin_backup")],
        [InlineKeyboardButton("📥 استيراد مشتركين (JSON)",   callback_data="admin_import")],
    ]
    text   = (
        "🛠️ *لوحة تحكم المشرف*\n\n"
        f"👥 إجمالي المشتركين: *{total}*\n"
        f"🟢 النشطون: *{active}*\n"
        f"🔴 المحظورون: *{banned}*\n\n"
        "اختر ما تريد:"
    )
    markup = InlineKeyboardMarkup(keyboard)
    if hasattr(target, "reply_text"):
        await target.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


# ─── /start ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("عذراً، لا يمكنك استخدام هذا البوت.")
        return

    if not user_exists(user.id):
        now   = datetime.now(TZ_RIYADH).strftime("%Y-%m-%d %H:%M")
        entry = {
            "id":         user.id,
            "first_name": user.first_name or "—",
            "last_name":  user.last_name  or "",
            "username":   user.username   or None,
            "joined":     now,
        }
        add_user(entry)
        total = count_users()
        logger.info(f"عضو جديد: {user.id} | {user.first_name}")

        uname = f"@{user.username}" if user.username else "لا يوجد"
        notif = (
            "🔔 مشترك جديد انضم للبوت!\n\n"
            f"👤 الاسم: {user.first_name} {user.last_name or ''}\n"
            f"🆔 المعرّف: {user.id}\n"
            f"📛 اليوزر: {uname}\n"
            f"🕐 التاريخ: {now}\n"
            f"👥 إجمالي المشتركين: {total}"
        )
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=notif)
            except Exception:
                pass

    # لوحة مفاتيح دائمة للمشرف
    if is_admin(user.id):
        reply_markup = ReplyKeyboardMarkup(
            [[KeyboardButton(ADMIN_PANEL_BTN)]],
            resize_keyboard=True,
            is_persistent=True,
        )
    else:
        reply_markup = None

    await update.message.reply_text(
        f"السلام عليكم ورحمة الله وبركاته 🌿\n"
        f"أهلاً وسهلاً بك يا {user.first_name} في البوت الإسلامي الدعوي.\n\n"
        "هذا البوت يُرسل إليك يومياً:\n\n"
        "🌄 قصة من أرض الجزائر الشامخة — الساعة 9:00 صباحاً\n"
        "   (قصة من صالحي زماننا مع عبرتها وسؤالين للتأمل)\n\n"
        "🌌 تنبيه قيام الليل — الساعة 2:00 فجراً\n\n"
        "📚 قصتان إسلاميتان من السلف — الساعة 9:00 مساءً\n\n"
        "📅 تذكير صيام الأيام البيض — أيام 13 و14 و15 من كل شهر هجري\n\n"
        "نسأل الله أن ينفع بهذا البوت وأن يجعله في ميزان حسناتكم ☝🏻",
        reply_markup=reply_markup,
    )


# ─── /admin والزر الدائم ──────────────────────────────────────────

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END

    logger.info(f"مشرف فتح لوحة التحكم: {update.effective_user.id}")
    await show_admin_panel(update.message, context)
    return WAITING_BROADCAST


# ─── أزرار لوحة التحكم ────────────────────────────────────────────

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("⛔️ غير مصرح.")
        return ConversationHandler.END

    data = query.data

    # ── رجوع للوحة الرئيسية ──
    if data == "admin_home":
        await show_admin_panel(query, context)
        return WAITING_BROADCAST

    # ── قائمة المشتركين ──
    if data == "admin_stats":
        users = load_data()["users"]
        if not users:
            await query.message.reply_text("لا يوجد مشتركون بعد.")
            return WAITING_BROADCAST

        await query.message.reply_text(
            f"📊 *إجمالي المشتركين: {len(users)} عضو*\n"
            "اضغط على أي اسم لرؤية تفاصيله:",
            parse_mode="Markdown",
        )

        chunk_size = 10
        for i in range(0, len(users), chunk_size):
            chunk = users[i:i + chunk_size]
            keyboard = []
            for u in chunk:
                name  = u.get("first_name", "—")
                lname = u.get("last_name", "")
                banned_mark = "🔴 " if is_banned(u["id"]) else "🟢 "
                label = f"{banned_mark}{name} {lname}".strip() + f"  |  {u['id']}"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"user_{u['id']}")])
            await query.message.reply_text(
                f"المشتركون {i+1} — {min(i+chunk_size, len(users))}:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return WAITING_BROADCAST

    # ── تفاصيل مشترك واحد ──
    if data.startswith("user_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        user = find_user(uid)
        if not user:
            await query.message.reply_text("لم يتم العثور على المشترك.")
            return WAITING_BROADCAST

        uname    = f"@{user['username']}" if user.get("username") else "لا يوجد"
        lname    = user.get("last_name", "")
        fullname = f"{user.get('first_name','—')} {lname}".strip()
        banned   = is_banned(uid)
        status   = "🔴 محظور" if banned else "🟢 نشط"

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
            [InlineKeyboardButton("🗑️ حذف من القاعدة", callback_data=f"delete_{uid}")],
            [InlineKeyboardButton("◀️ رجوع للقائمة",   callback_data="admin_stats")],
        ]

        await query.message.reply_text(
            f"👤 *بيانات المشترك*\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"📝 الاسم الكامل: {fullname}\n"
            f"🆔 المعرّف (ID): `{uid}`\n"
            f"📛 اليوزر: {uname}\n"
            f"📅 تاريخ الانضمام: {user.get('joined','—')}\n"
            f"📌 الحالة: {status}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    # ── حظر مشترك ──
    if data.startswith("ban_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        ban_user(uid)
        user = find_user(uid)
        name = user.get("first_name", str(uid)) if user else str(uid)
        await query.message.reply_text(
            f"🚫 تم حظر *{name}* (`{uid}`) بنجاح.\n"
            "لن يتلقى أي رسائل بعد الآن.",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    # ── إلغاء حظر مشترك ──
    if data.startswith("unban_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        unban_user(uid)
        user = find_user(uid)
        name = user.get("first_name", str(uid)) if user else str(uid)
        await query.message.reply_text(
            f"✅ تم إلغاء حظر *{name}* (`{uid}`).\n"
            "سيتلقى الرسائل مجدداً.",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    # ── حذف مشترك نهائياً ──
    if data.startswith("delete_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        user = find_user(uid)
        name = user.get("first_name", str(uid)) if user else str(uid)
        delete_user(uid)
        await query.message.reply_text(
            f"🗑️ تم حذف *{name}* (`{uid}`) من قاعدة البيانات نهائياً.",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    # ── قائمة المحظورين ──
    if data == "admin_banned":
        with get_db() as conn:
            banned_ids = [r[0] for r in conn.execute("SELECT user_id FROM banned").fetchall()]
        if not banned_ids:
            await query.message.reply_text("✅ لا يوجد أي مشترك محظور حالياً.")
            return WAITING_BROADCAST
        keyboard = []
        for uid in banned_ids:
            user = find_user(uid)
            if user:
                # مشترك موجود في قاعدة البيانات — فتح صفحته
                name = user.get("first_name", "—")
                keyboard.append([InlineKeyboardButton(
                    f"🔴 {name}  |  {uid}",
                    callback_data=f"user_{uid}",
                )])
            else:
                # مشترك محظور غير موجود في جدول users — أزرار مباشرة
                keyboard.append([
                    InlineKeyboardButton(f"🔴 {uid}", callback_data=f"user_{uid}"),
                    InlineKeyboardButton("✅ رفع الحظر", callback_data=f"unban_{uid}"),
                    InlineKeyboardButton("🗑️ حذف",      callback_data=f"delete_{uid}"),
                ])
        keyboard.append([InlineKeyboardButton("◀️ رجوع", callback_data="admin_home")])
        await query.message.reply_text(
            f"🚫 *المحظورون ({len(banned_ids)}):*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
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
                f"📦 *نسخة احتياطية*\n"
                f"👥 المشتركون: {total}\n"
                f"🚫 المحظورون: {banned}\n"
                f"📅 {datetime.now(TZ_RIYADH).strftime('%Y-%m-%d %H:%M')}"
            ),
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    # ── استيراد مشتركين ──
    if data == "admin_import":
        await query.message.reply_text(
            "📥 أرسل ملف JSON الخاص بالمشتركين لاستيراده.\n"
            "يجب أن يكون بنفس صيغة ملف النسخ الاحتياطي.\n\n"
            "_(أرسل /cancel للإلغاء)_",
            parse_mode="Markdown",
        )
        return WAITING_IMPORT

    # ── نشر رسالة ──
    if data == "admin_broadcast":
        await query.message.reply_text(
            "📢 أرسل الرسالة التي تريد نشرها لجميع المشتركين النشطين:\n"
            "_(أرسل /cancel للإلغاء)_",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    return WAITING_BROADCAST


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    message_text    = update.message.text
    user_ids        = get_user_ids()
    success, failed = 0, 0

    await update.message.reply_text(f"⏳ جاري الإرسال لـ {len(user_ids)} مشترك نشط...")

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=message_text)
            success += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ *تم الإرسال!*\n\n"
        f"✔️ نجح: {success}\n"
        f"❌ فشل: {failed}",
        parse_mode="Markdown",
    )
    await show_admin_panel(update.message, context)
    return WAITING_BROADCAST


async def receive_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ يرجى إرسال ملف JSON فقط.")
        return WAITING_IMPORT

    try:
        tg_file  = await doc.get_file()
        raw      = await tg_file.download_as_bytearray()
        imported = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await update.message.reply_text(f"❌ فشل قراءة الملف: {e}")
        return WAITING_IMPORT

    if "users" not in imported:
        await update.message.reply_text(
            "❌ الملف لا يحتوي على مفتاح 'users'. تأكد أنه ملف نسخ احتياطي صحيح."
        )
        return WAITING_IMPORT

    # دمج — لا استبدال — حتى لا نضيع المشتركين الحاليين
    added           = 0
    skipped         = 0
    raw_banned      = imported.get("banned", [])
    imported_banned = []

    # تحقق من صحة قائمة الحظر
    for uid in raw_banned:
        try:
            imported_banned.append(int(uid))
        except (TypeError, ValueError):
            skipped += 1

    with get_db() as conn:
        for u in imported.get("users", []):
            if isinstance(u, int):
                u = {"id": u, "first_name": "—", "username": None, "joined": "—"}
            # تحقق من وجود ID صحيح
            try:
                user_id = int(u.get("id"))
            except (TypeError, ValueError):
                skipped += 1
                continue
            result = conn.execute(
                "INSERT OR IGNORE INTO users (id, first_name, last_name, username, joined) "
                "VALUES (?,?,?,?,?)",
                (
                    user_id,
                    str(u.get("first_name") or "—"),
                    str(u.get("last_name")  or ""),
                    u.get("username"),
                    str(u.get("joined")     or "—"),
                ),
            )
            if result.rowcount:
                added += 1
        for uid in imported_banned:
            conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
        conn.commit()

    total = count_users()
    skip_note = f"\n⚠️ سجلات متجاهلة (بيانات غير صالحة): {skipped}" if skipped else ""
    await update.message.reply_text(
        f"✅ *تم الاستيراد بنجاح!*\n\n"
        f"➕ مشتركون جدد مضافون: {added}\n"
        f"🚫 محظورون مستوردون: {len(imported_banned)}\n"
        f"👥 إجمالي المشتركين الآن: {total}"
        f"{skip_note}",
        parse_mode="Markdown",
    )
    await show_admin_panel(update.message, context)
    return WAITING_BROADCAST


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
    today     = datetime.now(TZ_RIYADH).date()
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
        entry_points=[
            CommandHandler("admin", admin_entry),
            MessageHandler(filters.Regex(f"^{ADMIN_PANEL_BTN}$"), admin_entry),
        ],
        states={
            WAITING_BROADCAST: [
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_|delete_)"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{ADMIN_PANEL_BTN}$"),
                    receive_broadcast,
                ),
            ],
            WAITING_IMPORT: [
                MessageHandler(filters.Document.ALL, receive_import),
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_|delete_)"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(f"^{ADMIN_PANEL_BTN}$"), admin_entry),
        ],
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
    watched  = ("TELEGRAM_TOKEN", "BOT_TOKEN", "TOKEN", "ADMIN_ID", "ADMIN_PASSWORD", "PORT", "DATA_DIR")
    env_keys = [k for k in os.environ if k in watched]
    logger.info(f"🔑 متغيرات البيئة الموجودة: {env_keys}")
    logger.info(f"💾 مسار قاعدة البيانات: {DB_FILE}")

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

    # تهيئة قاعدة البيانات (وترحيل JSON إن وجد)
    init_db()
    # تسجيل المشرفين تلقائياً كمشتركين حتى تصلهم الرسائل
    register_admins()
    logger.info(f"✅ قاعدة البيانات جاهزة — {count_users()} مشترك مسجّل")

    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    logger.info("🚀 البوت يعمل الآن...")

    retry_delay = 5
    while True:
        try:
            logger.info("🔄 جاري بناء التطبيق وبدء الـ polling...")
            app = build_app()
            app.run_polling(drop_pending_updates=True)
            logger.info("ℹ️  توقف polling بشكل طبيعي.")
            break
        except RuntimeError as e:
            logger.error(f"❌ خطأ فادح: {e}")
            raise SystemExit(1)
        except Exception as e:
            logger.error(f"⚠️  توقف البوت: {e}")
            logger.info(f"⏳ إعادة المحاولة خلال {retry_delay} ثانية...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    main()
