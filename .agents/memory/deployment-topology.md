---
name: Deployment topology
description: Where this bot actually runs live, and why the Replit workflow is disabled.
---

The Telegram bot's production instance runs on Railway, per user decision (2026-07-06). Replit is used for code editing only.

**Why:** python-telegram-bot's `getUpdates` long-polling only allows one active consumer per bot token. Running the "Start application" workflow in Replit at the same time as the Railway deployment causes intermittent `Conflict: terminated by other getUpdates request` errors.

**How to apply:** Do not start/recreate a running workflow for this bot in Replit unless the user explicitly asks to make Replit the live environment again (in which case confirm they've stopped the Railway instance first). Code changes made here need to be deployed to Railway by the user to take effect in production.
