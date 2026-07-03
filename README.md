# 🕌 البوت الإسلامي الدعوي الشامل

بوت تليجرام إسلامي يرسل تنبيهات الصلاة، الأذكار، الأحاديث، وقصص السلف تلقائياً لجميع المشتركين.

---

## ✨ المميزات

- 📌 **مواقيت الصلاة** — تنبيهات يومية بتوقيت مكة المكرمة والجزائر
- 📅 **التاريخ الهجري** — تذكير بالأحداث الإسلامية
- 📖 **حديث اليوم** — حديث صحيح مع تفسيره
- 🌌 **قيام الليل** — تنبيه تلقائي الساعة 2:00 فجراً
- 📚 **قصص السلف** — يومياً الساعة 9:00 مساءً
- 📅 **الأيام البيض** — تذكير صيام 13، 14، 15 من كل شهر
- 📊 **إحصائيات** — أمر `/stats` لعرض عدد المشتركين

---

## 🚀 التشغيل على Railway

### 1. شوّك (Fork) أو استنسخ المستودع

```bash
git clone https://github.com/vorkutapilagek-create/Telegram-Bot-Simple.git
cd Telegram-Bot-Simple
```

### 2. أنشئ بوتاً على Telegram

1. افتح [@BotFather](https://t.me/BotFather) على تليجرام
2. أرسل `/newbot` واتبع التعليمات
3. احفظ التوكن الذي ستستلمه

### 3. ارفع على Railway

1. ادخل على [railway.app](https://railway.app) وسجّل دخولاً بـ GitHub
2. اضغط **New Project** ← **Deploy from GitHub repo**
3. اختر مستودع `Telegram-Bot-Simple`
4. في قسم **Variables**، أضف:

```
TELEGRAM_TOKEN = ضع_توكن_البوت_هنا
```

5. Railway سيشغّل البوت تلقائياً ✅

---

## 💻 التشغيل المحلي

```bash
# تثبيت المتطلبات
pip install -r telegram_bot/requirements.txt

# إنشاء ملف .env
echo "TELEGRAM_TOKEN=توكن_البوت" > telegram_bot/.env

# تشغيل البوت
cd telegram_bot && python3 main.py
```

---

## 📁 هيكل المشروع

```
Telegram-Bot-Simple/
├── telegram_bot/
│   ├── main.py          # الكود الرئيسي للبوت
│   ├── requirements.txt # المكتبات المطلوبة
│   ├── .env.example     # مثال على متغيرات البيئة
│   └── .gitignore       # الملفات المستثناة من git
├── railway.toml         # إعداد Railway للنشر
└── README.md            # هذا الملف
```

---

## 🛠️ الأوامر المتاحة

| الأمر | الوصف |
|-------|--------|
| `/start` | بدء البوت وعرض القائمة الرئيسية |
| `/stats` | عرض إحصائيات المشتركين (للمشرف) |

---

## 📦 المتطلبات

- Python 3.11+
- `python-telegram-bot[job-queue]==20.7`
- `pytz`

---

## 📜 الرخصة

مشروع مفتوح المصدر — للاستخدام الدعوي والتعليمي.
