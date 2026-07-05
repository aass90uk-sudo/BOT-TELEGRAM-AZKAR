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
    MenuButtonWebApp,
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


# ─── دالة مساعدة: ضبط زر WebApp للمشرف ──────────────────────────────────────

async def _set_admin_webapp_button(bot, admin_id: int):
    """يضبط زر ⊞ لوحة التحكم في محادثة المشرف مع البوت."""
    if not WEBAPP_URL:
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=admin_id,
            menu_button=MenuButtonWebApp(
                text="🛠️ لوحة التحكم",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin"),
            ),
        )
        logger.info(f"✅ MenuButtonWebApp ← المشرف {admin_id}")
    except Exception as e:
        logger.warning(f"⚠️ تعذّر ضبط MenuButtonWebApp للمشرف {admin_id}: {e}")


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("عذراً، لا يمكنك استخدام هذا البوت.")
        return

    # للمشرفين: أعد ضبط زر ⊞ عند كل /start لضمان ظهوره حتى بعد تغيير الرابط
    if is_admin(user.id):
        await _set_admin_webapp_button(context.bot, user.id)

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

    await update.message.reply_text(welcome_text(user.first_name))


# ─── إعداد المشرفين عند بدء البوت (post_init) ────────────────────────────────

async def setup_admins(app) -> None:
    """يُشغَّل مرة واحدة عند انطلاق البوت: يضبط زر ⊞ وأوامر المشرف."""
    admin_commands = [
        BotCommand("start",  "▶️ رسالة الترحيب"),
        BotCommand("admin",  "🛠️ لوحة التحكم"),
        BotCommand("cancel", "❌ إلغاء العملية الحالية"),
    ]

    if WEBAPP_URL:
        logger.info(f"🌐 WEBAPP_URL = {WEBAPP_URL}/admin")
    else:
        logger.warning("⚠️ WEBAPP_URL غير محدد — زر ⊞ لن يظهر.")

    for aid in ADMIN_IDS:
        try:
            await _set_admin_webapp_button(app.bot, aid)
            await app.bot.set_my_commands(
                commands=admin_commands,
                scope=BotCommandScopeChat(chat_id=aid),
            )

            # رسالة الترحيب — مرة واحدة فقط
            flag_key = f"welcome_sent_{aid}"
            if not has_flag(flag_key):
                hint = (
                    "اضغط على زر *🛠️ لوحة التحكم* (⊞ بجانب حقل الكتابة) لفتح لوحة التحكم."
                    if WEBAPP_URL else
                    "أرسل /admin لفتح لوحة التحكم."
                )
                await app.bot.send_message(
                    chat_id=aid,
                    text=welcome_text("مشرف") + f"\n\n🛠️ *ملاحظة للمشرف:*\n{hint}",
                    parse_mode="Markdown",
                )
                set_flag(flag_key)
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
        "بعد قليل — الساعة 9:00 مساءً — ستصلكم قصتان إسلاميتان:\n"
        "🎭 قصة من طرائف السلف الصالح\n"
        "☝🏻 قصة من واقع المرابطين اليوم\n\n"
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


async def send_story_one(context: ContextTypes.DEFAULT_TYPE):
    await _broadcast(context, (
        "📖 القصة الأولى — طرائف السلف الصالح\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎭 ذكاء القاضي إياس بن معاوية رحمه الله\n\n"
        "كان إياس بن معاوية من أذكى قضاة المسلمين في عصره.\n\n"
        "جاءه رجل يشكو جاره فقال: جاري يشرب الخمر في بيته!\n\n"
        "فقال إياس: كيف علمتَ؟\n\n"
        "قال: رأيته يشتري العنب ويعصره ويتركه حتى يختمر ثم يشربه!\n\n"
        "فقال إياس: وأنت كيف تعرف أنه يختمر؟ هل ذقتَه أنت؟\n\n"
        "فارتبك الرجل وسكت — ففهم الحاضرون أن الشاهد نفسه كان يشرب.\n\n"
        "فأسقط إياس شهادته وقال: لا تقبل شهادة من يشهد على نفسه بالمعصية.\n\n"
        "📝 الفائدة: العدل والذكاء في القضاء من أعظم صفات القاضي المسلم."
    ))


async def send_story_two(context: ContextTypes.DEFAULT_TYPE):
    await _broadcast(context, (
        "🌙 القصة الثانية — من واقع المرابطين اليوم\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "☝🏻 ثبات المرأة المؤمنة في زمن الفتن\n\n"
        "في إحدى مناطق الثغور، كانت امرأة مؤمنة زوجها مرابط بعيد أشهراً طويلة. "
        "كانت تُربي أطفالها على الصلاة والقرآن.\n\n"
        "سألها ابنها الصغير: أمي، لماذا أبي لا يرجع؟\n\n"
        "فأجابته: بُنيّ، أبوك اختار أن يكون مع الله، ونحن اخترنا أن نصبر معه. "
        "وكل ليلة صبر هي درجة في الجنة لنا جميعاً.\n\n"
        "لما عاد الزوج وجد أطفاله يحفظون القرآن فبكى وقال: "
        "والله ما الثغر الذي أنا فيه بأعظم من الثغر الذي صمدتِ فيه أنتِ.\n\n"
        "📝 الفائدة: تربية الأبناء على الإيمان في غياب الأب جهاد حقيقي."
    ))


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

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            WAITING_BROADCAST: [
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_|delete_)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast),
            ],
            WAITING_IMPORT: [
                MessageHandler(filters.Document.ALL, receive_import),
                CallbackQueryHandler(admin_button, pattern="^(admin_|user_|ban_|unban_|delete_)"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("admin",  admin_entry),
        ],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(admin_conv)

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
