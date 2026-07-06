"""نشر محتوى المجلة صباحاً ومساءً — صفحة واحدة (صورة + نص) لكل المشتركين يومياً.

يعتمد هذا الملف على image.py لمعرفة مسار صورة كل صفحة، وعلى main.py
(عبر استيراد مؤجَّل لتفادي الاستيراد الدائري) للوصول إلى قاعدة البيانات
ومعرفة المشتركين.
"""

import json
import logging
import os

from telegram.ext import ContextTypes

from image import MAGAZINE_PAGES_JSON, get_page_image_path

logger = logging.getLogger(__name__)


def load_pages() -> list:
    try:
        with open(MAGAZINE_PAGES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


async def send_page(context: ContextTypes.DEFAULT_TYPE, slot: str):
    """يُرسل الصفحة التالية من المجلة (صورة + نص) لجميع المشتركين."""
    from main import get_and_increment_magazine_page, get_user_ids

    pages = load_pages()
    if not pages:
        return

    idx = get_and_increment_magazine_page(len(pages))
    page = pages[idx]
    num = page["page"]
    text = page.get("text", "").strip()
    img = get_page_image_path(num)

    caption = (
        f"📖 مجلة حفيدات الخنساء\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 الصفحة {num} من {len(pages)}  |  {slot}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{text[:900] if text else '— صفحة مصوّرة —'}"
    )

    for uid in get_user_ids():
        try:
            if os.path.exists(img):
                with open(img, "rb") as photo:
                    await context.bot.send_photo(chat_id=uid, photo=photo, caption=caption)
            else:
                await context.bot.send_message(chat_id=uid, text=caption)
        except Exception:
            continue


async def send_morning(context: ContextTypes.DEFAULT_TYPE):
    await send_page(context, "🌅 نشرة الصباح")


async def send_evening(context: ContextTypes.DEFAULT_TYPE):
    await send_page(context, "🌙 نشرة المساء")
