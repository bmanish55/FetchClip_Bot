"""Run the bot on your own PC (long polling) to test it before deploying to Vercel.

    python run_local.py

Uses the exact same handlers as app.py. Needs only BOT_TOKEN in your .env file.
NOTE: starting this removes the Telegram webhook, so after testing run
      python set_webhook.py https://YOUR-PROJECT.vercel.app
once more to point the bot back at Vercel.
"""
import logging

import app as A
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters


def build_polling_app() -> Application:
    tg = Application.builder().token(A.BOT_TOKEN).job_queue(None).build()
    tg.add_handler(CommandHandler("start", A.cmd_start))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, A.on_text))
    tg.add_handler(CallbackQueryHandler(A.on_button, pattern=r"^dl:"))
    tg.add_error_handler(A.on_error)
    return tg


if __name__ == "__main__":
    if not A.BOT_TOKEN:
        raise SystemExit("Put BOT_TOKEN=... in your .env file first.")
    logging.getLogger("mediabot").info("Local mode (polling). ffmpeg: %s", A.HAVE_FFMPEG)
    build_polling_app().run_polling(drop_pending_updates=True)
