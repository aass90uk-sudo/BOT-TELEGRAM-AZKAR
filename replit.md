# Islamic Da'wah Telegram Bot

A Python Telegram bot that sends daily Islamic reminders to all subscribers: night prayer alerts, hadith, stories of the Salaf, and white-day fasting reminders.

## Stack
- **Language:** Python 3.12
- **Library:** python-telegram-bot 20.7 (with APScheduler job queue)
- **Entry point:** `telegram_bot/main.py`

## How to run

```bash
cd telegram_bot && python3 main.py
```

## Required environment variables / secrets

| Key | Description |
|-----|-------------|
| `TELEGRAM_TOKEN` | Bot token from @BotFather (required) |
| `ADMIN_ID` | Comma-separated Telegram user IDs for admin access (optional) |
| `ADMIN_PASSWORD` | Password for the in-bot admin panel (optional) |

## Scheduled jobs (Riyadh timezone)
- **02:00** — Night prayer reminder to all subscribers
- **08:00** — White days fasting reminder (12th of each Hijri month)
- **20:45** — Stories announcement
- **21:00** — Story 1 (from the Salaf)
- **21:05** — Story 2 (contemporary)

## Data storage
Subscriber data is stored locally in `telegram_bot/users_data.json`.

## User preferences
