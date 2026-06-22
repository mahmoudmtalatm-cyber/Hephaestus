import os
import json
import logging
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)
from keepalive import start_keepalive_server

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not firebase_admin._apps:
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    else:
        cred = credentials.Certificate("firebase-credentials.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()
logging.basicConfig(level=logging.INFO)

WAITING_FOR_TOKEN = 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *Hephaestus*!\n\n"
        "Build your own Telegram menu bot — completely *free*. "
        "No subscriptions, no paywalls, unlike other menu builders out there.\n\n"
        "Send me your bot token from @BotFather to get started.\n\n"
        "Once activated, go to *your own bot* to configure and manage it!",
        parse_mode="Markdown"
    )
    return WAITING_FOR_TOKEN


async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import telegram
    token = update.message.text.strip()
    uid = update.effective_user.id

    if ":" not in token or len(token) < 30:
        await update.message.reply_text("❌ Invalid token. Please try again.")
        return WAITING_FOR_TOKEN

    try:
        test_bot = telegram.Bot(token=token)
        bot_info = await test_bot.get_me()
    except Exception:
        await update.message.reply_text(
            "❌ Could not connect to that bot. Check your token and try again."
        )
        return WAITING_FOR_TOKEN

    # Check if already registered
    existing = db.collection("users").document(str(uid)).get()
    existing_data = existing.to_dict() if existing.exists else {}

    # Store all bots as a list keyed by token so each gets its own config
    bots = existing_data.get("bots", {})

    # Migrate legacy single-token field if present
    legacy_token = existing_data.get("bot_token")
    if legacy_token and legacy_token not in bots:
        bots[legacy_token] = {
            "bot_token": legacy_token,
            "bot_username": existing_data.get("bot_username", ""),
            "welcome_message": existing_data.get("welcome_message", "Welcome!"),
            "menus": existing_data.get("menus", {}),
        }

    # Add or update this token's entry
    if token not in bots:
        bots[token] = {
            "bot_token": token,
            "bot_username": bot_info.username,
            "admin_id": uid,
            "welcome_message": "Welcome!",
            "menus": {},
        }
    else:
        # Token already registered — just refresh the username
        bots[token]["bot_username"] = bot_info.username
        bots[token]["admin_id"] = uid

    db.collection("users").document(str(uid)).set({
        "admin_id": uid,
        "bots": bots,
        # Keep legacy field pointing at latest token so old runner code doesn't break
        "bot_token": token,
        "bot_username": bot_info.username,
    }, merge=True)

    already = "🔄 Already registered — refreshed!" if token in existing_data.get("bots", {}) else "✅ Activated!"
    await update.message.reply_text(
        f"{already} *@{bot_info.username}* is ready.\n\n"
        f"👉 Open your bot [@{bot_info.username}](https://t.me/{bot_info.username}) "
        f"and send /start to configure it.\n\n"
        f"You are the admin. Regular users won't see the admin panel.",
        parse_mode="Markdown"
    )
    return WAITING_FOR_TOKEN


async def setup_commands(app):
    await app.bot.set_my_commands([BotCommand("start", "Start the bot")])


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(setup_commands).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token)
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(conv)
    start_keepalive_server()
    print("🤖 Builder Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
