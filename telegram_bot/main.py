"""
البوت الإسلامي الدعوي الشامل
==============================
يُرسل تنبيهات يومية (قيام الليل، قصص السلف، صيام الأيام البيض) لجميع المشتركين.
لوحة تحكم WebApp مدمجة للمشرفين عبر زر ⊞ بجانب حقل الكتابة.
"""

import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytz
from hijri_converter import Gregorian
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = (
    os.environ.get("TELEGRAM_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TOKEN")
)

_raw_admins = os.environ.get("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]

TZ_RIYADH = pytz.timezone("Asia/Riyadh")


def _detect_webapp_url() -> str:
    """يكتشف رابط WebApp تلقائياً من المتغيرات المتاحة."""
    for key in ("WEBAPP_URL", "RAILWAY_PUBLIC_DOMAIN", "REPLIT_DEV_DOMAIN"):
        val = os.environ.get(key, "").strip().rstrip("/")
        if not val:
            continue
        url = val if val.startswith("https://") else f"https://{val}"
        return url
    return ""


WEBAPP_URL = _detect_webapp_url()

# ─── حالات المحادثة ────────────────────────────────────────────────────────────

WAITING_BROADCAST = 1
WAITING_IMPORT = 2

# ─── قاعدة البيانات SQLite ────────────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", _BASE_DIR)
DB_FILE = os.path.join(DATA_DIR, "bot.db")

BOT_START_TIME = datetime.utcnow()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_flags (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
    _migrate_json()


def _migrate_json():
    """ترحيل تلقائي من users_data.json القديم إلى SQLite (يعمل مرة واحدة فقط)."""
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
                    "INSERT OR IGNORE INTO users (id, first_name, last_name, username, joined)"
                    " VALUES (?,?,?,?,?)",
                    (u.get("id"), u.get("first_name", "—"), u.get("last_name", ""),
                     u.get("username"), u.get("joined", "—")),
                )
            for uid in data.get("banned", []):
                conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
            conn.commit()
        os.rename(json_file, json_file + ".migrated")
        logger.info("✅ تم ترحيل البيانات من JSON إلى SQLite")
    except Exception as e:
        logger.error(f"⚠️ خطأ في ترحيل JSON: {e}")


# ── دوال قاعدة البيانات ────────────────────────────────────────────────────────

def has_flag(key: str) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM bot_flags WHERE key=?", (key,)).fetchone() is not None


def set_flag(key: str, value: str = "1"):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO bot_flags (key, value) VALUES (?,?)", (key, value))
        conn.commit()


def add_user(entry: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, first_name, last_name, username, joined)"
            " VALUES (?,?,?,?,?)",
            (entry["id"], entry.get("first_name", "—"), entry.get("last_name", ""),
             entry.get("username"), entry.get("joined", "—")),
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


def find_user(user_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def load_data() -> dict:
    with get_db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, first_name, last_name, username, joined FROM users"
        ).fetchall()]
        banned = [r[0] for r in conn.execute("SELECT user_id FROM banned").fetchall()]
    return {"users": users, "banned": banned}


def count_users() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_banned() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM banned").fetchone()[0]


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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_banned(user_id: int) -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM banned WHERE user_id=?", (user_id,)).fetchone() is not None


def register_admins():
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


# ─── HTML لوحة التحكم (WebApp) ────────────────────────────────────────────────

ADMIN_HTML = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>لوحة التحكم</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  background:var(--tg-theme-bg-color,#fff);
  color:var(--tg-theme-text-color,#222);
  padding:12px 14px 24px;font-size:15px;
}
h1{font-size:17px;text-align:center;margin-bottom:14px;font-weight:700}
.tab-bar{display:flex;gap:6px;margin-bottom:12px}
.tab{
  flex:1;padding:8px 4px;border:1.5px solid var(--tg-theme-hint-color,#bbb);
  border-radius:10px;background:none;color:var(--tg-theme-text-color,#222);
  font-size:13px;cursor:pointer;transition:.15s;
}
.tab.active{
  background:var(--tg-theme-button-color,#2196F3);
  color:var(--tg-theme-button-text-color,#fff);border-color:transparent;
}
.card{
  background:var(--tg-theme-secondary-bg-color,#f4f4f4);
  border-radius:14px;padding:14px 16px;margin-bottom:10px;
}
.row{display:flex;justify-content:space-between;align-items:center;
  padding:8px 0;border-bottom:1px solid var(--tg-theme-hint-color,#ddd)}
.row:last-child{border-bottom:none}
.val{font-weight:700;font-size:16px;color:var(--tg-theme-link-color,#2196F3)}
.badge{font-size:11px;padding:3px 8px;border-radius:8px;font-weight:600}
.badge.active{background:#e8f5e9;color:#388e3c}
.badge.banned{background:#ffebee;color:#c62828}
textarea{
  width:100%;border:1.5px solid var(--tg-theme-hint-color,#bbb);
  border-radius:10px;padding:10px;font-size:14px;
  background:var(--tg-theme-bg-color,#fff);
  color:var(--tg-theme-text-color,#222);
  resize:vertical;min-height:110px;margin-top:10px;outline:none;
}
textarea:focus{border-color:var(--tg-theme-button-color,#2196F3)}
.btn{
  width:100%;padding:13px;border:none;border-radius:12px;font-size:15px;
  font-weight:700;cursor:pointer;margin-top:8px;transition:.2s;
}
.btn-primary{
  background:var(--tg-theme-button-color,#2196F3);
  color:var(--tg-theme-button-text-color,#fff);
}
.btn-primary:disabled{opacity:.45;cursor:default}
.status{text-align:center;padding:8px 0;font-size:13px;
  color:var(--tg-theme-hint-color,#888);min-height:24px}
.hint{font-size:12px;color:var(--tg-theme-hint-color,#999);margin-top:4px}
#panel-stats,#panel-subs,#panel-broadcast{display:none}
#panel-stats.show,#panel-subs.show,#panel-broadcast.show{display:block}
.loading{text-align:center;padding:20px;color:var(--tg-theme-hint-color,#888);font-size:14px}
.user-row{display:flex;justify-content:space-between;align-items:center;
  padding:9px 0;border-bottom:1px solid var(--tg-theme-hint-color,#eee)}
.user-row:last-child{border-bottom:none}
.user-name{font-size:14px}
.user-id{font-size:11px;color:var(--tg-theme-hint-color,#999)}
</style>
</head>
<body>
<h1>🛠️ لوحة التحكم</h1>
<div class="tab-bar">
  <button class="tab active" onclick="showTab('stats')">📊 الإحصاء</button>
  <button class="tab" onclick="showTab('subs')">👥 مشتركون</button>
  <button class="tab" onclick="showTab('broadcast')">📢 بث</button>
</div>

<div id="panel-stats" class="show">
  <div class="card" id="stats-card"><div class="loading">⏳ جاري التحميل...</div></div>
</div>

<div id="panel-subs">
  <div class="card" id="subs-card"><div class="loading">⏳ جاري التحميل...</div></div>
</div>

<div id="panel-broadcast">
  <div class="card">
    <p style="font-weight:600">📢 رسالة للمشتركين النشطين</p>
    <p class="hint">ستُرسل لجميع المشتركين غير المحظورين.</p>
    <textarea id="bcast-text" placeholder="اكتب رسالتك هنا..."></textarea>
    <div id="bcast-status" class="status"></div>
    <button class="btn btn-primary" id="bcast-btn" onclick="sendBroadcast()">إرسال للجميع</button>
  </div>
</div>

<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
const initData = tg.initData;
let statsLoaded = false, subsLoaded = false;

function showTab(name) {
  ['stats','subs','broadcast'].forEach((t, i) => {
    document.getElementById('panel-' + t).classList.remove('show');
    document.querySelectorAll('.tab')[i].classList.remove('active');
  });
  const idx = ['stats','subs','broadcast'].indexOf(name);
  document.getElementById('panel-' + name).classList.add('show');
  document.querySelectorAll('.tab')[idx].classList.add('active');
  if (name === 'stats' && !statsLoaded) loadStats();
  if (name === 'subs'  && !subsLoaded)  loadSubs();
}

async function api(action, extra = {}) {
  try {
    const r = await fetch('/api', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, init_data: initData, ...extra})
    });
    return r.json();
  } catch (e) { return {error: e.message}; }
}

async function loadStats() {
  statsLoaded = true;
  const d = await api('stats');
  if (d.error) {
    document.getElementById('stats-card').innerHTML = '<div class="loading">❌ ' + d.error + '</div>';
    return;
  }
  document.getElementById('stats-card').innerHTML = `
    <div class="row"><span>👥 إجمالي المشتركين</span><span class="val">${d.total}</span></div>
    <div class="row"><span>🟢 النشطون</span><span class="val">${d.active}</span></div>
    <div class="row"><span>🔴 المحظورون</span><span class="val">${d.banned}</span></div>
  `;
}

async function loadSubs() {
  subsLoaded = true;
  const d = await api('subscribers');
  if (d.error) {
    document.getElementById('subs-card').innerHTML = '<div class="loading">❌ ' + d.error + '</div>';
    return;
  }
  if (!d.users || !d.users.length) {
    document.getElementById('subs-card').innerHTML = '<div class="loading">لا يوجد مشتركون بعد.</div>';
    return;
  }
  document.getElementById('subs-card').innerHTML = d.users.map(u => `
    <div class="user-row">
      <div>
        <div class="user-name">${esc(u.first_name + ' ' + (u.last_name || ''))}</div>
        <div class="user-id">${u.id}${u.username ? ' · @' + esc(u.username) : ''}</div>
      </div>
      <span class="badge ${u.banned ? 'banned' : 'active'}">${u.banned ? 'محظور' : 'نشط'}</span>
    </div>
  `).join('');
}

async function sendBroadcast() {
  const text = document.getElementById('bcast-text').value.trim();
  if (!text) { tg.showAlert('الرجاء كتابة رسالة أولاً.'); return; }
  const btn    = document.getElementById('bcast-btn');
  const status = document.getElementById('bcast-status');
  btn.disabled = true;
  status.textContent = '⏳ جاري الإرسال...';
  const d = await api('broadcast', {text});
  btn.disabled = false;
  if (d.error) { status.textContent = '❌ ' + d.error; return; }
  status.textContent = `✅ نجح: ${d.success} | فشل: ${d.failed}`;
  document.getElementById('bcast-text').value = '';
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

loadStats();
</script>
</body>
</html>"""


# ─── HTTP Server (Health + Admin API) ─────────────────────────────────────────

_INIT_DATA_MAX_AGE = 86400  # 24 ساعة — كافٍ لجلسة عمل طويلة


def _validate_init_data(init_data: str) -> dict | None:
    """
    يتحقق من initData الواردة من Telegram WebApp:
    - توقيع HMAC صحيح
    - العمر لا يتجاوز 24 ساعة
    يُعيد بيانات المستخدم أو None.
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        params   = {k: v[0] for k, v in parse_qs(init_data, keep_blank_values=True).items()}
        hash_val = params.pop("hash", None)
        if not hash_val:
            return None

        auth_date = int(params.get("auth_date", 0))
        if auth_date == 0:
            return None
        if int(time.time()) - auth_date > _INIT_DATA_MAX_AGE:
            logger.warning("initData منتهية الصلاحية")
            return None

        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed   = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_val):
            return None

        return json.loads(params.get("user", "{}"))
    except Exception as e:
        logger.warning(f"خطأ في التحقق من initData: {e}")
        return None


def _sync_send(chat_id: int, text: str) -> bool:
    """إرسال رسالة مزامنة عبر HTTP مباشرة — يُستخدم من thread الـ WebApp API."""
    import urllib.request as _ur
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req  = _ur.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        _ur.urlopen(req, timeout=8)
        return True
    except Exception:
        return False


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.rstrip("/") == "/admin":
            self._html(ADMIN_HTML)
        else:
            uptime = (datetime.utcnow() - BOT_START_TIME).seconds
            self._text(f"status: ok\nuptime: {uptime}s\nbot: running\n")

    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            logger.warning(f"HTTP POST error: {e}")
            try:
                self._json({"error": "internal server error"}, 500)
            except Exception:
                pass

    def _handle_post(self):
        if self.path != "/api":
            self._json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            self._json({"error": "bad Content-Length"}, 400)
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            self._json({"error": "invalid json"}, 400)
            return

        user = _validate_init_data(payload.get("init_data", ""))
        if not user or not is_admin(user.get("id", 0)):
            self._json({"error": "unauthorized"}, 403)
            return

        action = payload.get("action", "")

        if action == "stats":
            total  = count_users()
            banned = count_banned()
            self._json({"total": total, "active": total - banned, "banned": banned})

        elif action == "subscribers":
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT u.id, u.first_name, u.last_name, u.username,"
                    " (SELECT 1 FROM banned b WHERE b.user_id=u.id) as banned"
                    " FROM users u ORDER BY u.id DESC LIMIT 200"
                ).fetchall()
            self._json({"users": [
                {"id": r["id"], "first_name": r["first_name"] or "—",
                 "last_name": r["last_name"] or "", "username": r["username"],
                 "banned": bool(r["banned"])}
                for r in rows
            ]})

        elif action == "broadcast":
            text = (payload.get("text") or "").strip()
            if not text:
                self._json({"error": "نص الرسالة فارغ"}, 400)
                return
            success, failed = 0, 0
            for uid in get_user_ids():
                if _sync_send(uid, text):
                    success += 1
                else:
                    failed += 1
            self._json({"success": success, "failed": failed})

        else:
            self._json({"error": "unknown action"}, 400)

    def _html(self, body: str):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _text(self, body: str):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b)

    def _json(self, data: dict, status: int = 200):
        b = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass  # كتم سجلات HTTP العادية


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server على المنفذ {port}")
    server.serve_forever()


# ─── رسائل البوت ──────────────────────────────────────────────────────────────

def welcome_text(name: str) -> str:
    return (
        f"السلام عليكم ورحمة الله وبركاته 🌿\n"
        f"أهلاً وسهلاً بك يا {name} في البوت الإسلامي الدعوي.\n\n"
        "هذا البوت يُرسل إليك يومياً:\n\n"
        "🌄 قصة من أرض الجزائر الشامخة — الساعة 9:00 صباحاً\n"
        "   (قصة من صالحي زماننا مع عبرتها وسؤالين للتأمل)\n\n"
        "🌌 تنبيه قيام الليل — الساعة 2:00 فجراً\n\n"
        "📚 قصتان إسلاميتان من السلف — الساعة 9:00 مساءً\n\n"
        "📅 تذكير صيام الأيام البيض — أيام 13 و14 و15 من كل شهر هجري\n\n"
        "نسأل الله أن ينفع بهذا البوت وأن يجعله في ميزان حسناتكم ☝🏻"
    )


# ─── لوحة مفاتيح المشرف (زر نصي بحت — بدون WebApp) ──────────────────────────

def _admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    زر أيقونة ⚙️ فقط بدون نص — يفتح لوحة التحكم عند الضغط.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("⚙️")]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("عذراً، لا يمكنك استخدام هذا البوت.")
        return

    if not user_exists(user.id):
        now = datetime.now(TZ_RIYADH).strftime("%Y-%m-%d %H:%M")
        add_user({
            "id":         user.id,
            "first_name": user.first_name or "—",
            "last_name":  user.last_name  or "",
            "username":   user.username   or None,
            "joined":     now,
        })
        total = count_users()
        uname = f"@{user.username}" if user.username else "لا يوجد"
        notif = (
            "🔔 مشترك جديد!\n\n"
            f"👤 {user.first_name} {user.last_name or ''}\n"
            f"🆔 {user.id}\n"
            f"📛 {uname}\n"
            f"🕐 {now}\n"
            f"👥 الإجمالي: {total}"
        )
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=notif)
            except Exception:
                pass

    # للمشرفين: أرسل لوحة المفاتيح مع زر لوحة التحكم
    if is_admin(user.id):
        await update.message.reply_text(
            welcome_text(user.first_name),
            reply_markup=_admin_reply_keyboard(),
        )
    else:
        await update.message.reply_text(welcome_text(user.first_name))


# ─── إعداد المشرفين عند بدء البوت (post_init) ────────────────────────────────

async def setup_admins(app) -> None:
    """
    يُشغَّل مرة واحدة عند انطلاق البوت.
    يضبط أوامر المشرف ويُرسل لوحة مفاتيح بزر "🛠️ لوحة التحكم".
    """
    admin_commands = [
        BotCommand("start",  "▶️ رسالة الترحيب"),
        BotCommand("admin",  "🛠️ لوحة التحكم"),
        BotCommand("cancel", "❌ إلغاء العملية الحالية"),
    ]

    for aid in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                commands=admin_commands,
                scope=BotCommandScopeChat(chat_id=aid),
            )
            logger.info(f"✅ أوامر المشرف ضُبطت للمشرف {aid}")
        except Exception as e:
            logger.warning(f"⚠️ تعذّر إعداد المشرف {aid}: {e}")


# ─── /admin ───────────────────────────────────────────────────────────────────

async def show_admin_panel(target, context):
    total  = count_users()
    banned = count_banned()
    active = total - banned
    keyboard = [
        [InlineKeyboardButton(f"👥 المشتركون ({active} نشط / {total} إجمالي)", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 نشر رسالة للمشتركين",    callback_data="admin_broadcast")],
        [InlineKeyboardButton(f"🚫 المحظورون ({banned})",   callback_data="admin_banned")],
        [InlineKeyboardButton("📦 نسخة احتياطية (JSON)",    callback_data="admin_backup")],
        [InlineKeyboardButton("📥 استيراد مشتركين (JSON)", callback_data="admin_import")],
    ]
    text = (
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


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ هذا الأمر للمشرفين فقط.")
        return ConversationHandler.END
    await show_admin_panel(update.message, context)
    return WAITING_BROADCAST


async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("⛔️ غير مصرح.")
        return ConversationHandler.END

    data = query.data

    if data == "admin_home":
        await show_admin_panel(query, context)
        return WAITING_BROADCAST

    if data == "admin_stats":
        users = load_data()["users"]
        if not users:
            await query.message.reply_text("لا يوجد مشتركون بعد.")
            return WAITING_BROADCAST
        await query.message.reply_text(
            f"📊 *إجمالي المشتركين: {len(users)} عضو*\nاضغط على أي اسم لرؤية تفاصيله:",
            parse_mode="Markdown",
        )
        chunk_size = 10
        for i in range(0, len(users), chunk_size):
            chunk = users[i:i + chunk_size]
            keyboard = []
            for u in chunk:
                mark  = "🔴 " if is_banned(u["id"]) else "🟢 "
                label = f"{mark}{u.get('first_name','—')} {u.get('last_name','')}".strip()
                keyboard.append([InlineKeyboardButton(
                    f"{label}  |  {u['id']}", callback_data=f"user_{u['id']}"
                )])
            await query.message.reply_text(
                f"المشتركون {i + 1}–{min(i + chunk_size, len(users))}:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return WAITING_BROADCAST

    if data.startswith("user_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        u = find_user(uid)
        if not u:
            await query.message.reply_text("لم يتم العثور على المشترك.")
            return WAITING_BROADCAST
        uname    = f"@{u['username']}" if u.get("username") else "لا يوجد"
        fullname = f"{u.get('first_name','—')} {u.get('last_name','')}".strip()
        banned   = is_banned(uid)
        link_btn = InlineKeyboardButton(
            "🔗 فتح الحساب",
            url=f"https://t.me/{u['username']}" if u.get("username") else f"tg://user?id={uid}",
        )
        ban_btn = (
            InlineKeyboardButton("✅ إلغاء الحظر", callback_data=f"unban_{uid}")
            if banned else
            InlineKeyboardButton("🚫 حظر المشترك", callback_data=f"ban_{uid}")
        )
        await query.message.reply_text(
            f"👤 *بيانات المشترك*\n━━━━━━━━━━━━━━━━\n\n"
            f"📝 الاسم: {fullname}\n"
            f"🆔 المعرّف: `{uid}`\n"
            f"📛 اليوزر: {uname}\n"
            f"📅 الانضمام: {u.get('joined','—')}\n"
            f"📌 الحالة: {'🔴 محظور' if banned else '🟢 نشط'}",
            reply_markup=InlineKeyboardMarkup([
                [link_btn],
                [ban_btn],
                [InlineKeyboardButton("🗑️ حذف من القاعدة", callback_data=f"delete_{uid}")],
                [InlineKeyboardButton("◀️ رجوع",            callback_data="admin_stats")],
            ]),
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    if data.startswith("ban_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        ban_user(uid)
        u    = find_user(uid)
        name = u.get("first_name", str(uid)) if u else str(uid)
        await query.message.reply_text(
            f"🚫 تم حظر *{name}* (`{uid}`).\nلن يتلقى أي رسائل بعد الآن.",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    if data.startswith("unban_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        unban_user(uid)
        u    = find_user(uid)
        name = u.get("first_name", str(uid)) if u else str(uid)
        await query.message.reply_text(
            f"✅ تم إلغاء حظر *{name}* (`{uid}`).\nسيتلقى الرسائل مجدداً.",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    if data.startswith("delete_"):
        try:
            uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("❌ معرّف غير صالح.")
            return WAITING_BROADCAST
        u    = find_user(uid)
        name = u.get("first_name", str(uid)) if u else str(uid)
        delete_user(uid)
        await query.message.reply_text(
            f"🗑️ تم حذف *{name}* (`{uid}`) نهائياً.",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    if data == "admin_banned":
        with get_db() as conn:
            banned_ids = [r[0] for r in conn.execute("SELECT user_id FROM banned").fetchall()]
        if not banned_ids:
            await query.message.reply_text("✅ لا يوجد أي مشترك محظور حالياً.")
            return WAITING_BROADCAST
        keyboard = []
        for uid in banned_ids:
            u = find_user(uid)
            if u:
                keyboard.append([InlineKeyboardButton(
                    f"🔴 {u.get('first_name','—')}  |  {uid}", callback_data=f"user_{uid}"
                )])
            else:
                keyboard.append([
                    InlineKeyboardButton(f"🔴 {uid}",        callback_data=f"user_{uid}"),
                    InlineKeyboardButton("✅ رفع الحظر",      callback_data=f"unban_{uid}"),
                    InlineKeyboardButton("🗑️ حذف",            callback_data=f"delete_{uid}"),
                ])
        keyboard.append([InlineKeyboardButton("◀️ رجوع", callback_data="admin_home")])
        await query.message.reply_text(
            f"🚫 *المحظورون ({len(banned_ids)}):*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    if data == "admin_backup":
        db  = load_data()
        raw = json.dumps(db, ensure_ascii=False, indent=2).encode("utf-8")
        bio = io.BytesIO(raw)
        bio.name = "users_data.json"
        await query.message.reply_document(
            document=bio,
            filename="users_data.json",
            caption=(
                f"📦 *نسخة احتياطية*\n"
                f"👥 {len(db['users'])} مشترك\n"
                f"🚫 {len(db.get('banned',[]))} محظور\n"
                f"📅 {datetime.now(TZ_RIYADH).strftime('%Y-%m-%d %H:%M')}"
            ),
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    if data == "admin_import":
        await query.message.reply_text(
            "📥 أرسل ملف JSON لاستيراد المشتركين.\n_(أرسل /cancel للإلغاء)_",
            parse_mode="Markdown",
        )
        return WAITING_IMPORT

    if data == "admin_broadcast":
        context.user_data["awaiting_broadcast"] = True
        await query.message.reply_text(
            "📢 أرسل الرسالة للمشتركين النشطين:\n_(أرسل /cancel للإلغاء)_",
            parse_mode="Markdown",
        )
        return WAITING_BROADCAST

    return WAITING_BROADCAST


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    if not context.user_data.get("awaiting_broadcast"):
        await update.message.reply_text(
            "ℹ️ اضغط على *📢 نشر رسالة* من لوحة التحكم أولاً.",
            parse_mode="Markdown",
        )
        await show_admin_panel(update.message, context)
        return WAITING_BROADCAST

    context.user_data.pop("awaiting_broadcast", None)
    user_ids        = get_user_ids()
    success, failed = 0, 0
    await update.message.reply_text(f"⏳ جاري الإرسال لـ {len(user_ids)} مشترك...")

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=update.message.text)
            success += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ *تم الإرسال!*\n\n✔️ نجح: {success}\n❌ فشل: {failed}",
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
        await update.message.reply_text("❌ الملف لا يحتوي على مفتاح 'users'.")
        return WAITING_IMPORT

    added, skipped = 0, 0
    imported_banned = []
    for uid in imported.get("banned", []):
        try:
            imported_banned.append(int(uid))
        except (TypeError, ValueError):
            skipped += 1

    with get_db() as conn:
        for u in imported.get("users", []):
            if isinstance(u, int):
                u = {"id": u, "first_name": "—", "username": None, "joined": "—"}
            try:
                user_id = int(u.get("id"))
            except (TypeError, ValueError):
                skipped += 1
                continue
            result = conn.execute(
                "INSERT OR IGNORE INTO users (id, first_name, last_name, username, joined)"
                " VALUES (?,?,?,?,?)",
                (user_id, str(u.get("first_name") or "—"), str(u.get("last_name") or ""),
                 u.get("username"), str(u.get("joined") or "—")),
            )
            if result.rowcount:
                added += 1
        for uid in imported_banned:
            conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
        conn.commit()

    skip_note = f"\n⚠️ سجلات متجاهلة: {skipped}" if skipped else ""
    await update.message.reply_text(
        f"✅ *تم الاستيراد!*\n\n"
        f"➕ جدد: {added}\n"
        f"🚫 محظورون: {len(imported_banned)}\n"
        f"👥 الإجمالي: {count_users()}"
        f"{skip_note}",
        parse_mode="Markdown",
    )
    await show_admin_panel(update.message, context)
    return WAITING_BROADCAST


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


# ─── المهام المجدولة ──────────────────────────────────────────────────────────

async def _broadcast(context: ContextTypes.DEFAULT_TYPE, text: str):
    """إرسال رسالة لجميع المشتركين النشطين."""
    for uid in get_user_ids():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            continue


async def send_stories_announcement(context: ContextTypes.DEFAULT_TYPE):
    await _broadcast(context, (
        "📚 تنبيه مبارك ☝🏻\n\n"
        "بعد قليل — الساعة 9:00 مساءً — ستصلكم قصتان جديدتان:\n"
        "🎭 طرفة من طرائف السلف الصالح\n"
        "🌙 قصة من واقع المرابطين الصابرين من زمننا\n\n"
        "استعدوا واجعلوا قلوبكم حاضرة 💙"
    ))


async def send_night_prayer_reminder(context: ContextTypes.DEFAULT_TYPE):
    await _broadcast(context, (
        "🌌 قيام الليل يا أهل الخير\n\n"
        "الساعة الآن 2:00 فجراً — والليل في ثلثه الأخير.\n\n"
        "قال ﷺ: «يَنْزِلُ رَبُّنَا تَبَارَكَ وَتَعَالَى كُلَّ لَيْلَةٍ إِلَى السَّمَاءِ الدُّنْيَا "
        "حِينَ يَبْقَى ثُلُثُ اللَّيْلِ الآخِرُ فَيَقُولُ: مَنْ يَدْعُونِي فَأَسْتَجِيبَ لَهُ؟ "
        "مَنْ يَسْأَلُنِي فَأُعْطِيَهُ؟ مَنْ يَسْتَغْفِرُنِي فَأَغْفِرَ لَهُ؟» [متفق عليه]\n\n"
        "قوموا ركعتين وادعوا الله — فالدعاء في هذا الوقت مستجاب ☝🏻"
    ))


async def send_white_days_reminder(context: ContextTypes.DEFAULT_TYPE):
    today     = datetime.now(TZ_RIYADH).date()
    hijri_day = Gregorian.fromdate(today).to_hijri().day
    if hijri_day not in (13, 14, 15):
        return
    label = {13: "اليوم الأول", 14: "اليوم الثاني", 15: "اليوم الثالث والأخير"}[hijri_day]
    await _broadcast(context, (
        f"📅 تذكير مبارك — صيام الأيام البيض ☝🏻\n"
        f"اليوم هو {label} ({hijri_day} من الشهر الهجري)\n\n"
        "قال ﷺ: «صِيَامُ ثَلاَثَةِ أَيَّامٍ مِنْ كُلِّ شَهْرٍ صِيَامُ الدَّهْرِ كُلِّهِ» [متفق عليه]\n\n"
        "فمن لم يصم بعد فلينوِ الآن 🤍 ومن صام فله البشرى بأجر عظيم إن شاء الله ☝🏻"
    ))


# ─── قصص السلف والمرابطين — تتجدد يومياً ────────────────────────────────────

SALAF_STORIES = [
    {
        "title": "ذكاء القاضي إياس بن معاوية 🧠",
        "body": (
            "كان إياس بن معاوية من أذكى قضاة المسلمين في عصره.\n\n"
            "جاءه رجل يشكو جاره: «جاري يشرب الخمر في بيته!»\n\n"
            "فقال إياس: «كيف علمتَ؟»\n\n"
            "قال: «رأيته يشتري العنب ويعصره ويتركه حتى يختمر ثم يشربه!»\n\n"
            "فقال إياس: «وأنت كيف تعرف أنه يختمر؟ هل ذقتَه أنت؟»\n\n"
            "فارتبك الرجل وسكت — ففُهم أن الشاهد نفسه كان يشرب.\n\n"
            "فأسقط إياس شهادته وقال: «لا تقبل شهادة من يشهد على نفسه بالمعصية»."
        ),
        "moral": "العدل والفطنة في القضاء درعٌ يحمي البريء ويفضح المتحيّل.",
        "lesson": "العضة: قبل أن تشكو غيرك، تأكد أن يدك نظيفة.",
        "q1": "لماذا أسقط القاضي شهادة الرجل؟",
        "q1a": "أ) لأنه كذب", "q1b": "ب) لأنه أدان نفسه",
        "q2": "ما الصفة التي يجب أن يتحلى بها القاضي المسلم؟",
        "q2a": "أ) الذكاء والعدل", "q2b": "ب) الشدة والصرامة",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "الإمام أحمد والفقير الذي أذهله 🌿",
        "body": (
            "جلس الإمام أحمد بن حنبل مرةً مع أحد فقراء المسجد بعد العشاء.\n\n"
            "فقال له الفقير: «يا إمام، أنا أنام في المسجد منذ سنين، "
            "وكل ليلة أرى فيها ملائكة تصلي على من ينتظر الصلاة».\n\n"
            "ففكّر الإمام أحمد وقال: «أنا أحمل علم الحديث خمسين سنة "
            "ولم أدرك ما أدركتَه أنت بجلستك هذه».\n\n"
            "ثم أوصى أصحابه: «لا تحتقروا الصالحين حتى وإن لم يعلموا»."
        ),
        "moral": "القرب من الله لا يُقاس بكثرة العلم فقط بل بصدق الإقبال عليه.",
        "lesson": "العضة: العمل الصالح الخالص يرفع صاحبه فوق حاملي الأسفار.",
        "q1": "ما الذي أدهش الإمام أحمد في الفقير؟",
        "q1a": "أ) كثرة صلاته", "q1b": "ب) قربه من الله بعمل بسيط",
        "q2": "ما العبرة من قول الإمام أحمد لأصحابه؟",
        "q2a": "أ) عدم احتقار الصالحين", "q2b": "ب) أهمية طلب العلم",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "الشافعي ومجادل الكلام 🎯",
        "body": (
            "دخل رجل من أهل الجدل على الإمام الشافعي يريد أن يناظره في الكلام.\n\n"
            "فقال الشافعي: «يا هذا، ما الذي تريد؟»\n\n"
            "قال: «أناظرك في مسائل الكلام».\n\n"
            "فقال الشافعي: «حسناً — لكن اشترط علي شرطاً: كل حجة تأتي بها "
            "أسألك: من أين أخذتَها؟ فإن قلتَ: من القرآن قبلتُ. "
            "وإن قلتَ: من عقلك فقط لم أقبل».\n\n"
            "فانصرف الرجل لأنه لم يجد لقوله أصلاً من الوحي.\n\n"
            "فقال الشافعي لأصحابه: «الكلام بلا وحي كالبناء بلا أساس»."
        ),
        "moral": "العقل نعمة لكن الوحي ميزانه — كل فكر لا يُعرض على الكتاب والسنة ضلّ.",
        "lesson": "العضة: لا تُجادل بعقلك المجرد — ابحث عن الأصل من القرآن والسنة.",
        "q1": "لماذا انصرف الرجل المجادل؟",
        "q1a": "أ) لأنه خاف من الشافعي", "q1b": "ب) لأن حججه لا أصل لها في الوحي",
        "q2": "ما معنى قول الشافعي «الكلام بلا وحي كالبناء بلا أساس»؟",
        "q2a": "أ) الفكر بلا دليل شرعي يتهاوى", "q2b": "ب) العقل عاجز عن الفهم",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "الحسن البصري والمتكبر 🪞",
        "body": (
            "مرّ رجل متكبر بالحسن البصري وقال: «يا شيخ، أنا أجلس مع الأمراء "
            "وأُدعى إلى موائدهم — فكيف تقول إن التواضع فضيلة؟»\n\n"
            "فتبسّم الحسن وقال: «وأنا يا بُني أجلس مع الفقراء والمساكين "
            "وأستفيد من كل واحد منهم ما لا تستفيده أنت من أمرائك».\n\n"
            "قال الرجل: «ماذا تستفيد من الفقراء؟»\n\n"
            "قال: «أستفيد الصدق — فالفقير لا يكذب لأجل الطمع، "
            "بخلاف من يجلس مع الأمراء يصدّق كل ما يقال».\n\n"
            "فمشى الرجل وهو يفكر."
        ),
        "moral": "التواضع لا يُنقص صاحبه بل يرفعه — والكبر يُعمي عن الحق.",
        "lesson": "العضة: من تجالس يُشكّل فكرك — اختر جليسك بعين البصيرة.",
        "q1": "ما الذي يستفيده الحسن البصري من مجالسة الفقراء؟",
        "q1a": "أ) المال والعون", "q1b": "ب) الصدق والاستقامة",
        "q2": "لماذا يكذب من يجلس مع أهل السلطة أحياناً؟",
        "q2a": "أ) بسبب الطمع والمصلحة", "q2b": "ب) بسبب الجهل",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "ابن تيمية وتاجر الخمر 🌅",
        "body": (
            "رأى ابن تيمية رحمه الله رجلاً يحمل خمراً في الطريق.\n\n"
            "فدنا منه بهدوء وقال: «يا أخي، أتبيع هذا؟»\n\n"
            "قال الرجل: «نعم».\n\n"
            "فقال ابن تيمية: «بكم تبيعه؟»\n\n"
            "فسمّى الرجل ثمناً. فقال ابن تيمية: «وكم تربح؟»\n\n"
            "قال: «كذا وكذا». فقال ابن تيمية: «أنا أعطيك هذا الربح "
            "على أن تُريقه الآن».\n\n"
            "فتعجّب الرجل وأراق الخمر وأخذ المال — ثم جلس مع ابن تيمية "
            "وكان أول جلسته بداية توبته."
        ),
        "moral": "الحكمة في الدعوة أبلغ من العنف — والمعاملة الحسنة تفتح القلوب.",
        "lesson": "العضة: لا تُغلق باب التوبة أمام أحد — أحسن الظن وابدأ بالرفق.",
        "q1": "ما الأسلوب الذي استخدمه ابن تيمية مع التاجر؟",
        "q1a": "أ) الترهيب والتوبيخ", "q1b": "ب) الحكمة والرفق",
        "q2": "ما الذي دفع الرجل إلى التوبة في هذه القصة؟",
        "q2a": "أ) الخوف من العقوبة", "q2b": "ب) المعاملة الحسنة واللطف",
        "ans1": "ب", "ans2": "ب",
    },
    {
        "title": "سفيان الثوري والسائل الفضولي 😄",
        "body": (
            "جاء رجل إلى سفيان الثوري وسأله: «يا أبا عبد الله، "
            "هل تعرف فلاناً العالِم؟»\n\n"
            "قال: «نعم».\n\n"
            "قال: «ما رأيك فيه؟»\n\n"
            "قال: «لا أُزكّي أحداً على الله».\n\n"
            "قال: «لكن الناس يقولون إنه من خيار العلماء!»\n\n"
            "قال: «والناس يُخطئون كثيراً — الله وحده يعلم السرائر».\n\n"
            "فأصرّ الرجل: «ومتى يكون العالِم حقاً عالِماً؟»\n\n"
            "فقال سفيان: «حين يعمل بما يعلم — وإلا فهو راوية كتب لا عالِم».\n\n"
            "فانصرف الرجل وقد ربح أكثر من جواب!"
        ),
        "moral": "العلم الحقيقي ما قرن بالعمل — وكثير من العلم بلا عمل وبال على صاحبه.",
        "lesson": "العضة: قيّم نفسك بما تعمل لا بما تعلم.",
        "q1": "ما تعريف سفيان للعالم الحقيقي؟",
        "q1a": "أ) من يحفظ الكتب", "q1b": "ب) من يعمل بعلمه",
        "q2": "لماذا امتنع سفيان عن تزكية العلماء أمام الناس؟",
        "q2a": "أ) لأن الله وحده يعلم السرائر", "q2b": "ب) لأنه يكره المدح",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "مالك بن دينار ومزدهرة الدنيا 🌺",
        "body": (
            "مرّ مالك بن دينار بدار فارهة لأحد الأثرياء المسلمين.\n\n"
            "فقيل له: «يا أبا يحيى، أما تتمنى داراً مثلها؟»\n\n"
            "فضحك وقال: «أتعرف من يملك هذه الدار الآن؟»\n\n"
            "قالوا: «فلان التاجر الغني».\n\n"
            "قال: «وقبله؟» قالوا: «فلان». قال: «وقبله؟» قالوا: «فلان».\n\n"
            "فقال: «ثلاثة أجيال دخلوا الدار ثم خرجوا منها إلى القبر — "
            "أتريدون أن أكون الرابع؟\n\n"
            "أما والله، داري التي لا يُنقل عنها أحد هي التي أتمناها»."
        ),
        "moral": "الدنيا فندق والآخرة وطن — من بنى للوطن ربح ومن بنى للفندق خسر.",
        "lesson": "العضة: لا تتعلق قلبك بما ستتركه — واسعَ لما ستجده.",
        "q1": "بم شبّه مالك بن دينار الدار الفارهة؟",
        "q1a": "أ) بمحطة عبور إلى القبر", "q1b": "ب) بمكان الراحة الأبدية",
        "q2": "ما «الدار» التي تمناها مالك بن دينار؟",
        "q2a": "أ) الجنة والدار الآخرة", "q2b": "ب) داراً أكبر في الدنيا",
        "ans1": "أ", "ans2": "أ",
    },
    {
        "title": "شريح القاضي وزوجته الغاضبة 😄",
        "body": (
            "كان شريح القاضي من أعدل القضاة — وكان لزوجته عليه رأي!\n\n"
            "يوماً ما غضبت زوجته ورفعت صوتها عليه أمام الناس.\n\n"
            "فجلس شريح صامتاً لا يرد.\n\n"
            "فسأله أحد أصحابه: «يا أبا أمية، كيف لا تردّ عليها؟»\n\n"
            "فقال: «يا أخي، أنا قاضٍ أحكم بالعدل بين الناس — "
            "فكيف أحكم لنفسي على زوجتي وأنا خصم وحكم في آنٍ واحد؟\n\n"
            "من عدل المرء أن يتحمل في بيته ما يأمر الناس بتحمله».\n\n"
            "فأُسكت صاحبه — وأُسكت زوجته أيضاً من الحياء!"
        ),
        "moral": "العدل الحقيقي يبدأ من البيت — ومن أنصف في الخفاء ظهر عدله في العلن.",
        "lesson": "العضة: طبّق على نفسك ما تطالب به غيرك — فالعدل لا يتجزأ.",
        "q1": "لماذا لم يردّ شريح على زوجته أمام الناس؟",
        "q1a": "أ) لأنه يخشاها", "q1b": "ب) لأنه لا يجوز أن يكون خصماً وحكماً",
        "q2": "ما المعنى العميق في موقف شريح القاضي؟",
        "q2a": "أ) العدل يبدأ من تطبيقه على النفس", "q2b": "ب) الصمت أفضل من الكلام دائماً",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "الإمام مالك وسؤال الخليفة 👑",
        "body": (
            "جاء الخليفة هارون الرشيد إلى الإمام مالك يريد أن يسمع منه الموطأ.\n\n"
            "فقال مالك: «يا أمير المؤمنين، العلم يُؤتى إليه ولا يأتي».\n\n"
            "فتعجّب الخليفة — لكنه جلس مع الناس في حلقة مالك.\n\n"
            "ثم أراد أن يجلس وحده فيخلو بمالك، فقال مالك: «العلم للناس جميعاً — "
            "ليس لأحد أن يحجبه لنفسه».\n\n"
            "فقال الرشيد لوزيره: «هذا الرجل أعزّ من ملوك الأرض — "
            "لأن ملوك الأرض يتعالون بالمال والجيش، وهذا تعالى بالحق»."
        ),
        "moral": "العزة الحقيقية لمن يتمسك بالحق ولا يُهادن السلطان على حساب العلم.",
        "lesson": "العضة: من أعزّ الحق أعزّه الله ولو أمام الملوك.",
        "q1": "لماذا لم يذهب الإمام مالك إلى الخليفة؟",
        "q1a": "أ) لأنه يكره الخلفاء", "q1b": "ب) لأن العلم يُؤتى إليه لا يأتي",
        "q2": "لماذا وصف الرشيد مالكاً بأنه أعزّ من الملوك؟",
        "q2a": "أ) لأنه غني جداً", "q2b": "ب) لأن عزته من الحق لا من الجاه",
        "ans1": "ب", "ans2": "ب",
    },
    {
        "title": "يحيى بن معين والمدّاح 🎭",
        "body": (
            "كان يحيى بن معين إماماً في الجرح والتعديل — لا تأخذه في الله لومة لائم.\n\n"
            "جاءه رجل يُثني عليه ويمدحه أمام الناس بكلام كثير.\n\n"
            "فقال يحيى: «اسكت — لو كنتُ كما تقول لما كان لي وقت أجلس فيه معك».\n\n"
            "فضحك الحاضرون، ثم قال يحيى بجدية: «الرجل الذي يُشغله المدح عن العمل "
            "خسر المدحَ والعملَ معاً».\n\n"
            "ثم قام ودخل داره وأغلق الباب ليواصل الكتابة."
        ),
        "moral": "الإعجاب بمدح الناس أول خطوة في الغرور — والعارف بنفسه لا يركن إلى الثناء.",
        "lesson": "العضة: العمل في الخفاء أصدق من المدح في العلن.",
        "q1": "لماذا ردّ يحيى بن معين على الرجل بهذا الأسلوب؟",
        "q1a": "أ) لأنه يكره المدح ويخشى الغرور", "q1b": "ب) لأنه لا يحب الناس",
        "q2": "من يخسر الأمرين معاً وفق قول يحيى؟",
        "q2a": "أ) من ينشغل بمدح الناس عن العمل", "q2b": "ب) من يعمل بلا تحدث",
        "ans1": "أ", "ans2": "أ",
    },
]

MURABITEEN_STORIES = [
    {
        "title": "ثبات المرأة المؤمنة في غياب زوجها 🌙",
        "body": (
            "في إحدى مناطق الثغور، كانت امرأة مؤمنة زوجها مرابط بعيد أشهراً طويلة.\n\n"
            "سألها ابنها الصغير: «أمي، لماذا أبي لا يرجع؟»\n\n"
            "فأجابته: «بُنيّ، أبوك اختار أن يكون مع الله، ونحن اخترنا أن نصبر معه. "
            "وكل ليلة صبر هي درجة في الجنة لنا جميعاً».\n\n"
            "لما عاد الزوج وجد أطفاله يحفظون القرآن فبكى وقال: "
            "«والله ما الثغر الذي أنا فيه بأعظم من الثغر الذي صمدتِ فيه أنتِ»."
        ),
        "moral": "تربية الأبناء على الإيمان في غياب الأب جهاد حقيقي ورباط لا يُرى.",
        "lesson": "العضة: الصبر على الغياب في سبيل الله له أجر الرباط.",
        "q1": "كيف جعلت الأم ابنها يفهم غياب أبيه؟",
        "q1a": "أ) أخبرته أن أباه مسافر للعمل", "q1b": "ب) علّمته أن الصبر درجات في الجنة",
        "q2": "لماذا بكى الزوج حين عاد؟",
        "q2a": "أ) من الفرح بالعودة", "q2b": "ب) لأن ثغر زوجته في البيت كان أعظم",
        "ans1": "ب", "ans2": "ب",
    },
    {
        "title": "الطالب المسلم في جامعة أوروبية 📚",
        "body": (
            "ذهب عمر إلى ألمانيا لدراسة الهندسة بمنحة دراسية.\n\n"
            "في أول أسبوع قيل له: «ستصعب الصلاة هنا — الجدول لا يتوقف».\n\n"
            "فقال عمر: «سأجد طريقة». وفعلها — كان يُصلي في الممرات والحدائق والمكتبة.\n\n"
            "بعد سنة، جاءه زميله الألماني وقال: «أريد أن أعرف عن دينك — "
            "رأيتك لا تتركه في أي ظرف. ما الذي يجعلك هكذا؟»\n\n"
            "كانت تلك بداية رحلة إسلام زميله على يد عمر."
        ),
        "moral": "الثبات على الشعائر في بلاد الغربة دعوة صامتة أبلغ من ألف خطبة.",
        "lesson": "العضة: استقامتك في الخفاء هي دعوتك الأقوى في العلن.",
        "q1": "ما الذي استفتح قلب الزميل الألماني نحو الإسلام؟",
        "q1a": "أ) حفظ عمر للقرآن", "q1b": "ب) ثبات عمر على الصلاة في كل ظرف",
        "q2": "ما العبرة من قدرة عمر على إيجاد طريقة للصلاة دائماً؟",
        "q2a": "أ) الإرادة تجد الحل والعذر يصنع العقبة", "q2b": "ب) الظروف تحدد إمكانية العبادة",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "الطبيب المسلم في المستشفى الغربي 🏥",
        "body": (
            "كانت الدكتورة سارة تعمل في مستشفى كبير في كندا.\n\n"
            "طُلب منها يوماً توقيع ورقة تعارض مبادئها الإسلامية.\n\n"
            "فرفضت بهدوء وقالت: «مبادئي الطبية والدينية لا تسمح لي بهذا».\n\n"
            "نُقلت من قسمها وتأثر مسارها المهني.\n\n"
            "لكنها واصلت وبعد ثلاث سنوات أصبحت رئيسة قسم في مستشفى آخر "
            "عرّفها مديره: «من أبرز الأطباء في المهنة والمبدأ».\n\n"
            "فقالت لزميلاتها: «من ثبت على الحق في الشدة أرضاه الله في الرخاء»."
        ),
        "moral": "الاستقامة في زمن الفساد طريقٌ يبدو ضيقاً ويتسع لمن سلكه.",
        "lesson": "العضة: لا تُضيّع مبدأك لأجل منصب — فالله يعوّض من يُعزّ دينه.",
        "q1": "ما العاقبة التي جاءت بعد ثبات سارة على مبدأها؟",
        "q1a": "أ) خسارة مهنتها للأبد", "q1b": "ب) ارتقاء أعلى في المسيرة المهنية",
        "q2": "ما معنى قولها «من ثبت على الحق في الشدة أرضاه الله في الرخاء»؟",
        "q2a": "أ) الثواب يأتي دائماً في الدنيا", "q2b": "ب) الله لا يُضيع أجر من ثبت على مبدأه",
        "ans1": "ب", "ans2": "ب",
    },
    {
        "title": "المعلم في قرية منسية ✏️",
        "body": (
            "رفض الأستاذ خالد عرضاً بمدرسة في المدينة براتب أعلى.\n\n"
            "بقي في قريته النائية حيث لا يوجد غيره يُعلّم الأطفال.\n\n"
            "سألوه: «لماذا تضحي بمستقبلك؟»\n\n"
            "فقال: «أنا لا أضحي — أنا أستثمر. كل طفل يتعلم القراءة هنا "
            "ربما يصبح عالِماً أو طبيباً أو أباً صالحاً يُعلّم أولاده».\n\n"
            "بعد خمس عشرة سنة، كان بين تلاميذه عشرون معلماً في مختلف المناطق — "
            "كل منهم يقول: «الأستاذ خالد هو من أشعل فيّ شمعة التعلم»."
        ),
        "moral": "الاستثمار في العقول صدقة جارية — وكل من علّمته يحمل نوراً منك.",
        "lesson": "العضة: لا تحقر عملك في الخفاء — فقد تكون سبب إصلاح أمة.",
        "q1": "لماذا رفض الأستاذ خالد العرض الأفضل في المدينة؟",
        "q1a": "أ) لأنه لا يريد الترقي", "q1b": "ب) لأنه يرى في بقائه استثماراً حقيقياً",
        "q2": "ما الذي يُعبّر عنه قول تلاميذه «أشعل فيّ شمعة»؟",
        "q2a": "أ) أن المعلم مصدر الإلهام الأول في حياتهم", "q2b": "ب) أنه كان يُنير الفصل بالكهرباء",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "الحافظ الذي لم يتوقف في السجن 📖",
        "body": (
            "سُجن الشاب يوسف ظلماً ثلاث سنوات.\n\n"
            "قال زملاؤه في الزنزانة: «كيف تقضي وقتك؟»\n\n"
            "قال: «أحفظ القرآن — لم يبق معي إلا ما في صدري».\n\n"
            "في ثلاث سنوات أتمّ حفظ القرآن كاملاً وراجعه ثلاث مرات.\n\n"
            "حين أُطلق سراحه قال للقاضي الذي ظلمه: «أنت أسجنتَ جسدي "
            "لكنني خرجت من سجنك حاملاً ما لا يأخذه أحد — كلام الله في صدري».\n\n"
            "فبكى القاضي وكانت تلك بداية توبته."
        ),
        "moral": "من ملأ وقته بالله في الضيق، منحه الله في الفرج ما لا يُعدّ.",
        "lesson": "العضة: الظلم لا يضر من ردّه إلى الله — بل ربما كان بداية خير عظيم.",
        "q1": "كيف حوّل يوسف سنوات السجن إلى ربح؟",
        "q1a": "أ) بالغضب والتخطيط للانتقام", "q1b": "ب) بحفظ القرآن وإملاء الوقت بالله",
        "q2": "ما معنى قول يوسف للقاضي: «خرجت حاملاً ما لا يأخذه أحد»؟",
        "q2a": "أ) أنه يقصد المال الذي ادخره", "q2b": "ب) أن العلم والإيمان لا يُسجنان",
        "ans1": "ب", "ans2": "ب",
    },
    {
        "title": "الشاب الذي عاد من الضياع 🌅",
        "body": (
            "غادر كريم الجزائر وهو ابن عشرين باحثاً عن العمل في أوروبا.\n\n"
            "وجد الدنيا مفتوحةً لكنه وجد قلبه مُغلقاً.\n\n"
            "بعد أربع سنوات من الضياع، دخل مسجداً هرباً من المطر.\n\n"
            "سمع الإمام يتلو: ﴿أَلَا بِذِكْرِ اللَّهِ تَطْمَئِنُّ الْقُلُوبُ﴾\n\n"
            "فبكى بكاءً لم يعرفه من قبل.\n\n"
            "قرّر أن يعود. بدأ يحفظ القرآن في طريق العودة. بعد ثلاث سنوات "
            "أصبح حافظاً وإماماً يُعلّم شباباً مثله."
        ),
        "moral": "القلب الذي خُلق لمعرفة الله لا يرتاح إلا بالرجوع إليه.",
        "lesson": "العضة: لا تُغلق باب التوبة أمام نفسك — فالله يقبل من رجع إليه.",
        "q1": "ما الذي أوقف كريم عند سماع الآية؟",
        "q1a": "أ) جمال صوت الإمام", "q1b": "ب) الآية مسّت وجعاً في قلبه",
        "q2": "ما المعنى الذي يحمله ﴿أَلَا بِذِكْرِ اللَّهِ تَطْمَئِنُّ الْقُلُوبُ﴾؟",
        "q2a": "أ) الطمأنينة لا تكون إلا بذكر الله", "q2b": "ب) العمل والمال يُريحان القلب",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "المهندس الذي رفض المشروع المشبوه 🏗️",
        "body": (
            "عُرض على المهندس ياسين مشروع بعقد ضخم — لكنه يعلم أن فيه غشاً للمواطنين.\n\n"
            "رفضه وقال: «العقد الذي لا يُرضي الله لا أوقّع عليه».\n\n"
            "سخر منه بعض زملائه: «أنت مثالي — الدنيا لا تعمل هكذا».\n\n"
            "بعد سنتين انهار المشروع وحوكم كل من وقّع عليه.\n\n"
            "أما ياسين فكان يعمل في مشروع متواضع لكنه نظيف. قال: "
            "«الحلال يبارك فيه حتى وإن قلّ، والحرام يُذهب بركته وصاحبه»."
        ),
        "moral": "البركة في الحلال حتى وإن قلّ — والحرام لا يبقى وإن كثر.",
        "lesson": "العضة: الرزق الحلال كسبٌ مزدوج: في الدنيا بالطمأنينة وفي الآخرة بالأجر.",
        "q1": "لماذا رفض ياسين المشروع رغم عائده الكبير؟",
        "q1a": "أ) لأنه لم يحتج المال", "q1b": "ب) لأنه فيه غش ولا يُرضي الله",
        "q2": "كيف صدق قول ياسين عن البركة بعد سنتين؟",
        "q2a": "أ) بحصوله على مشروع أكبر", "q2b": "ب) بانهيار المشروع الحرام وسلامته",
        "ans1": "ب", "ans2": "ب",
    },
    {
        "title": "الفتاة التي ثبتت على حجابها 🌸",
        "body": (
            "التحقت إيمان بالجامعة وكانت الوحيدة المحجبة في قسمها.\n\n"
            "لم تسلم من السخرية — لكنها كانت تبكي وتدعو: "
            "«يا الله، اجعل الحجاب راحةً لي لا عبئاً».\n\n"
            "تخرّجت بامتياز وأصبحت مهندسة.\n\n"
            "وبدأت زميلاتها اللواتي سخرن منها يقلن: "
            "«أنتِ كنتِ دائماً الأقوى منّا — كنا نحسدك ولا نعترف».\n\n"
            "ردّت إيمان: «لم أكن أقوى — لكنني كنت ممسكةً بما هو أقوى مني»."
        ),
        "moral": "الهوية الإسلامية الراسخة درعٌ في وقت الضعف وعزٌّ في وقت الثبات.",
        "lesson": "العضة: التمسك بالثوابت في الضغط أصعب ما يكون — وأجمل ما يكون.",
        "q1": "ما سر قوة إيمان في رأيها هي؟",
        "q1a": "أ) تفوقها الدراسي", "q1b": "ب) تمسكها بما هو أقوى منها",
        "q2": "ماذا كان رد فعل زميلاتها بعد التخرج؟",
        "q2a": "أ) حسدنها واعترفن بقوتها الحقيقية", "q2b": "ب) تجاهلنها تماماً",
        "ans1": "ب", "ans2": "أ",
    },
    {
        "title": "أم تُعلّم وحدها بعد فقد زوجها 🤲",
        "body": (
            "رحل زوج أم البنات الثلاث وهي في الأربعين.\n\n"
            "قالت لأولادها: «ربنا لم يتركنا — ولن نترك ربنا».\n\n"
            "كانت تعمل نهاراً وتُعلّم أولادها ليلاً.\n\n"
            "حفظت معهم القرآن وهي في الخمسين.\n\n"
            "حين سألوها: «كيف صبرتِ؟» قالت: «أنا لم أصبر على الحُزن — "
            "أنا شغلتُ حُزني بالعمل لله. الفرق كبير».\n\n"
            "نجحت بناتها الثلاث في مجالات مختلفة وكلهن يقلن: «أمنا جامعتنا الأولى»."
        ),
        "moral": "الحزن إذا صار وقوداً للعمل الصالح تحوّل إلى رحمة.",
        "lesson": "العضة: لا تجلس مع حزنك — اجعله يدفعك نحو الله.",
        "q1": "كيف فرّقت الأم بين الصبر وشغل الحزن بالعمل لله؟",
        "q1a": "أ) الصبر سلبي والعمل إيجابي", "q1b": "ب) كلاهما نفس الشيء",
        "q2": "ما الذي جعل بنات الأم يصفنها بـ«جامعتهن الأولى»؟",
        "q2a": "أ) لأنها درّستهن في الجامعة", "q2b": "ب) لأنها كانت مدرستهن في العلم والإيمان",
        "ans1": "أ", "ans2": "ب",
    },
    {
        "title": "الشيخ الذي واصل التعليم في الأزمة 📿",
        "body": (
            "في زمن الأزمة والخوف أغلق كثيرٌ من العلماء أبوابهم.\n\n"
            "أما الشيخ عبد الرحمن فكان يُعلّم كل يوم ولو بثلاثة طلاب.\n\n"
            "قيل له: «ألا تخشى؟» فقال: «أخشى الله أكثر مما أخشى الفتنة. "
            "وإن مات العلم في وقت الخوف، كيف يعيش وقت الأمان؟»\n\n"
            "واصل سبع سنوات وأخرج أكثر من خمسين طالباً. "
            "مات على فراشه وهو يُردّد: «الحمد لله — علّمتُ وما خنتُ»."
        ),
        "moral": "العلم أمانة — والأمانة لا تُترك في وقت الشدة خاصةً.",
        "lesson": "العضة: ثبوتك على عملك في الأزمة هو شهادتك أن ما تعمله يستحق.",
        "q1": "لماذا لم يُغلق الشيخ باب التعليم رغم الخوف؟",
        "q1a": "أ) لأنه لم يعلم بالخطر", "q1b": "ب) لأن خشيته من الله أكبر من خشيته من الفتنة",
        "q2": "ما معنى قوله «علّمتُ وما خنتُ» قبل وفاته؟",
        "q2a": "أ) أنه وفّى الأمانة التي أعطاه الله إياها", "q2b": "ب) أنه حمى نفسه من الأعداء",
        "ans1": "ب", "ans2": "أ",
    },
]


def _story_callback(story_set: str, idx: int, q: int, choice: str) -> str:
    """ينشئ callback_data مضغوطاً — أقصر من 64 بايت دائماً.
    صيغة: sq_{s|m}_{idx}_{q}_{a|b}
    """
    return f"sq_{story_set}_{idx}_{q}_{choice}"


async def _send_story_with_quiz(
    context: ContextTypes.DEFAULT_TYPE,
    header: str,
    story: dict,
    story_set: str,
    story_idx: int,
):
    """إرسال قصة مع عبرة وعضة وسؤالين بخيارين قابلين للتحقق (inline)."""
    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ {story['title']}\n\n"
        f"{story['body']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 {story['moral']}\n\n"
        f"🌿 {story['lesson']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤔 اختبر نفسك:\n\n"
        f"❓ س١: {story['q1']}\n\n"
        f"❓ س٢: {story['q2']}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                story["q1a"],
                callback_data=_story_callback(story_set, story_idx, 1, "a"),
            ),
            InlineKeyboardButton(
                story["q1b"],
                callback_data=_story_callback(story_set, story_idx, 1, "b"),
            ),
        ],
        [
            InlineKeyboardButton(
                story["q2a"],
                callback_data=_story_callback(story_set, story_idx, 2, "a"),
            ),
            InlineKeyboardButton(
                story["q2b"],
                callback_data=_story_callback(story_set, story_idx, 2, "b"),
            ),
        ],
    ])
    for uid in get_user_ids():
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                reply_markup=keyboard,
            )
        except Exception:
            continue


async def send_story_one(context: ContextTypes.DEFAULT_TYPE):
    idx   = date.today().timetuple().tm_yday % len(SALAF_STORIES)
    story = SALAF_STORIES[idx]
    await _send_story_with_quiz(
        context,
        "🎭 القصة الأولى — طرائف السلف الصالح",
        story,
        story_set="s",
        story_idx=idx,
    )


async def send_story_two(context: ContextTypes.DEFAULT_TYPE):
    idx   = date.today().timetuple().tm_yday % len(MURABITEEN_STORIES)
    story = MURABITEEN_STORIES[idx]
    await _send_story_with_quiz(
        context,
        "🌙 القصة الثانية — المرابطون الصابرون من زمننا",
        story,
        story_set="m",
        story_idx=idx,
    )


async def story_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل إجابات أسئلة القصص — يتحقق من الصواب ويرد بتغذية راجعة."""
    query = update.callback_query
    await query.answer()

    # sq_{s|m}_{idx}_{q}_{a|b}
    parts = query.data.split("_")          # ["sq", set, idx, q, choice]
    if len(parts) != 5:
        return

    _, story_set, idx_str, q_str, choice = parts
    try:
        idx = int(idx_str)
        q   = int(q_str)
    except ValueError:
        return

    stories  = SALAF_STORIES if story_set == "s" else MURABITEEN_STORIES
    idx      = idx % len(stories)
    story    = stories[idx]
    ans_key  = f"ans{q}"
    correct  = story.get(ans_key, "")        # "أ" أو "ب"
    chosen   = "أ" if choice == "a" else "ب"
    is_right = chosen == correct

    q_text   = story[f"q{q}"]
    right_opt_key = f"q{q}{'a' if correct == 'أ' else 'b'}"
    right_text    = story.get(right_opt_key, "")

    if is_right:
        reply = (
            f"✅ أحسنتَ! الإجابة الصحيحة هي: {right_text}\n\n"
            f"💡 {story['moral']}\n\n"
            "نسأل الله أن يجعلنا من أهل التدبر والعمل ☝🏻"
        )
    else:
        reply = (
            f"❌ الإجابة الصحيحة كانت: {right_text}\n\n"
            f"🤔 السؤال: {q_text}\n\n"
            f"💡 {story['moral']}\n\n"
            "لا بأس — التأمل في العبرة هو المقصود ☝🏻"
        )

    await query.message.reply_text(reply)


ALGERIA_STORIES = [
    {
        "title": "الشيخ الذي لم تُطفئه العشرية",
        "body": (
            "في قرية صغيرة بالأوراس، كان الشيخ عبد الحميد يُعلّم أبناء القرية القرآن "
            "في أحلك سنوات العشرية السوداء.\n\n"
            "حين جاءه من يُحذّره قال بهدوء: «أنا لا أُعلّمهم لأجل الدنيا، وإن كان ربي يريد "
            "أن أموت وأنا أعلّم كلامه، فتلك خاتمة أتمناها».\n\n"
            "واصل سبع سنوات دون انقطاع وأخرج أكثر من أربعين حافظاً. "
            "مات على فراشه وهو يُردّد سورة يس."
        ),
        "moral": "العبرة: الثبات على الحق في الفتن هو أعظم أنواع الجهاد.",
        "q1": "ما الذي جعل الشيخ يستمر رغم الخطر؟ وكيف تُطبّق هذا في حياتك؟",
        "q2": "كيف يكون المعلم الصادق سبباً في نجاة أمة؟ اذكر شاهداً من التاريخ.",
    },
    {
        "title": "الشاب الذي عاد من باريس",
        "body": (
            "غادر كريم الجزائر وهو ابن عشرين باحثاً عن العمل في باريس. وجد الدنيا مفتوحةً "
            "لكنه وجد قلبه مُغلقاً.\n\n"
            "بعد أربع سنوات من الضياع، دخل مسجداً هرباً من المطر. سمع الإمام يتلو: "
            "﴿أَلَا بِذِكْرِ اللَّهِ تَطْمَئِنُّ الْقُلُوبُ﴾ فبكى بكاءً لم يعرفه من قبل.\n\n"
            "قرّر أن يعود. بدأ يحفظ القرآن في الباخرة. بعد ثلاث سنوات أصبح حافظاً وإماماً."
        ),
        "moral": "العبرة: القلب الذي خُلق لمعرفة الله لا يرتاح إلا بالرجوع إليه.",
        "q1": "ما الذي أوقف كريم عند سماع الآية؟",
        "q2": "كيف يكون القرآن طريقاً للعودة إلى الله؟",
    },
    {
        "title": "أم المجاهدين من تلمسان",
        "body": (
            "في تلمسان العتيقة، ربّت الحاجة فاطمة سبعةً من أبنائها وحدها بعد اعتقال زوجها.\n\n"
            "كانت تقول كل صباح: «أبوكم في سبيل الله، ونحن في سبيل الله».\n\n"
            "حين سُئلت كيف تصبرين؟ قالت: «أنا لا أصبر، أنا أثق».\n\n"
            "لما أُطلق زوجها وجد أبناءه السبعة يحفظون القرآن. "
            "فسجد وقال: «يا رب، ما ظننت أن السجن سيكون خيراً»."
        ),
        "moral": "العبرة: الثقة بالله لا تعني غياب الألم، بل اليقين بأنه لا يضيع الأجر.",
        "q1": "ما الفرق بين الصبر والثقة كما فهمتَه من كلام الحاجة فاطمة؟",
        "q2": "كيف يؤثر ثبات الأم في الشدة على تربية أبنائها؟",
    },
    {
        "title": "الطبيب الجزائري الذي أبى الرشوة",
        "body": (
            "عُرضت على الدكتور يوسف مبالغ مالية مقابل توقيع وثائق فيها ظلم للمرضى الفقراء.\n\n"
            "رفض وقال: «أنا أمام الله قبل أن أكون أمام المسؤولين».\n\n"
            "نُقل من منصبه. لكنه بدأ يُعالج فقراء الحي مجاناً. "
            "حين سألوه: ألا تتمنى المال؟ قال: «أنا أبحث عن رضا الله»."
        ),
        "moral": "العبرة: الاستقامة في زمن الفساد جهاد حقيقي.",
        "q1": "ما الثمن الذي دفعه يوسف بسبب استقامته؟",
        "q2": "كيف يكون إتقان العمل وصدق الأمانة عبادةً يُؤجر عليها الإنسان؟",
    },
    {
        "title": "الشابة التي حافظت على حجابها في الجامعة",
        "body": (
            "التحقت إيمان بالجامعة في وهران، وكانت الوحيدة المحجبة في قسمها. "
            "لم تسلم من السخرية.\n\n"
            "كانت تبكي وتدعو: «يا الله، اجعل الحجاب راحةً لي لا عبئاً».\n\n"
            "تخرّجت بامتياز وأصبحت مهندسة. وبدأت زميلاتها يقلن لها: "
            "«أنتِ كنتِ دائماً الأقوى منّا»."
        ),
        "moral": "العبرة: الهوية الإسلامية الراسخة درعٌ لا عبء.",
        "q1": "ما الذي أعان إيمان على الثبات؟",
        "q2": "قال ﷺ: «عجباً لأمر المؤمن» — كيف تجلّى هذا في قصة إيمان؟",
    },
]


async def send_algeria_story(context: ContextTypes.DEFAULT_TYPE):
    idx   = date.today().timetuple().tm_yday % len(ALGERIA_STORIES)
    story = ALGERIA_STORIES[idx]
    await _broadcast(context, (
        f"🌄 قصة الصباح — من أرض الجزائر الشامخة\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"☝🏻 {story['title']}\n\n"
        f"{story['body']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 {story['moral']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤔 تأمّل وأجب في نفسك:\n\n"
        f"❓ {story['q1']}\n\n"
        f"❓ {story['q2']}"
    ))


# ─── بناء التطبيق ─────────────────────────────────────────────────────────────

def build_app():
    if not BOT_TOKEN:
        raise RuntimeError(
            "لم يُعثر على توكن البوت! أضف TELEGRAM_TOKEN أو BOT_TOKEN أو TOKEN"
        )

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_admins).build()

    _btn_filter = filters.TEXT & filters.Regex(r"^⚙️$")

    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_entry),
            MessageHandler(_btn_filter, admin_entry),   # زر لوحة المفاتيح
        ],
        states={
            WAITING_BROADCAST: [
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_|delete_)"),
                MessageHandler(_btn_filter, admin_entry),          # إعادة فتح اللوحة
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~_btn_filter, receive_broadcast),
            ],
            WAITING_IMPORT: [
                MessageHandler(filters.Document.ALL, receive_import),
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_|delete_)"),
                MessageHandler(_btn_filter, admin_entry),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("admin",  admin_entry),
            MessageHandler(_btn_filter, admin_entry),
        ],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(story_answer_callback, pattern=r"^sq_"))

    jq = app.job_queue
    tz = TZ_RIYADH
    jq.run_daily(send_algeria_story,         time=dt_time(9,  0,  tzinfo=tz))
    jq.run_daily(send_night_prayer_reminder, time=dt_time(2,  0,  tzinfo=tz))
    jq.run_daily(send_white_days_reminder,   time=dt_time(8,  0,  tzinfo=tz))
    jq.run_daily(send_stories_announcement,  time=dt_time(20, 45, tzinfo=tz))
    jq.run_daily(send_story_one,             time=dt_time(21, 0,  tzinfo=tz))
    jq.run_daily(send_story_two,             time=dt_time(21, 5,  tzinfo=tz))

    return app


# ─── نقطة الدخول ──────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("❌ لم يُعثر على توكن البوت!")
        raise SystemExit(1)

    logger.info(f"💾 مسار قاعدة البيانات: {DB_FILE}")
    logger.info(f"🌐 WEBAPP_URL: {WEBAPP_URL or 'غير محدد'}")
    logger.info(f"👮 المشرفون: {ADMIN_IDS}")

    init_db()
    register_admins()
    logger.info(f"✅ قاعدة البيانات جاهزة — {count_users()} مشترك")

    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("🚀 البوت يعمل...")
    retry_delay = 5
    while True:
        try:
            build_app().run_polling(drop_pending_updates=True)
            break
        except RuntimeError as e:
            logger.error(f"❌ خطأ فادح: {e}")
            raise SystemExit(1)
        except Exception as e:
            logger.error(f"⚠️ توقف البوت: {e}")
            logger.info(f"⏳ إعادة المحاولة خلال {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    main()
