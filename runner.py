import os
import json
import asyncio
import logging
import uuid
import copy
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler, CallbackQueryHandler
)
from keepalive import nudge_keepalive

load_dotenv()

if not firebase_admin._apps:
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    else:
        cred = credentials.Certificate("firebase-credentials.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()
logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────
# STATES
# ──────────────────────────────────────────────
(
    ADMIN_MAIN,
    ADMIN_BTN_LIST,
    ADMIN_BTN_DETAIL,
    ADMIN_MSG_LIST,
    WAITING_WELCOME_MSG,
    WAITING_BTN_LABEL,
    WAITING_EDIT_BTN_LABEL,
    WAITING_INSERT_BTN_LABEL,
    WAITING_DELETE_CONFIRM,
    WAITING_ASSIGN_MESSAGES,
    WAITING_EDIT_MESSAGE,
    WAITING_INSERT_MESSAGE,
    WAITING_BROADCAST,
    WAITING_BROADCAST_CONFIRM,
    ADMIN_PREVIEW,
    WAITING_RESET_CONFIRM,
    ADMIN_BTN_REORDER,
    WAITING_BTN_TYPE,
    WAITING_BTN_URL,
    WAITING_INSERT_BTN_TYPE,
    WAITING_INSERT_BTN_URL,
) = range(21)

# ──────────────────────────────────────────────
# DB HELPERS
# ──────────────────────────────────────────────
def get_user_data(uid):
    doc = db.collection("users").document(str(uid)).get()
    return doc.to_dict() if doc.exists else {}

def save_menus(uid, menus):
    db.collection("users").document(str(uid)).update({"menus": menus})

def save_welcome(uid, text):
    db.collection("users").document(str(uid)).update({"welcome_message": text})

def get_menus(uid):
    return get_user_data(uid).get("menus", {})

def get_welcome(uid):
    return get_user_data(uid).get("welcome_message", "Welcome!")

def get_all_admins():
    docs = db.collection("users").stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("bot_token"):
            result.append(data | {"uid": doc.id})
    return result

def track_button_click(uid, menu_id, btn_label):
    """Track button usage analytics."""
    try:
        key = f"{menu_id}::{btn_label}"
        ref = db.collection("analytics").document(str(uid))
        doc = ref.get()
        data = doc.to_dict() if doc.exists else {}
        clicks = data.get("clicks", {})
        clicks[key] = clicks.get(key, 0) + 1
        ref.set({"clicks": clicks}, merge=True)
    except Exception:
        pass

def get_analytics(uid):
    try:
        doc = db.collection("analytics").document(str(uid)).get()
        return doc.to_dict() if doc.exists else {}
    except Exception:
        return {}

def track_user(uid, user_id):
    """Track unique users."""
    try:
        ref = db.collection("bot_users").document(str(uid))
        ref.set({"users": firestore.ArrayUnion([str(user_id)])}, merge=True)
    except Exception:
        pass

def get_bot_users(uid):
    try:
        doc = db.collection("bot_users").document(str(uid)).get()
        data = doc.to_dict() if doc.exists else {}
        return data.get("users", [])
    except Exception:
        return []

# ──────────────────────────────────────────────
# MESSAGE SERIALIZATION
# ──────────────────────────────────────────────
def serialize_message(msg):
    """Turn a Telegram Message into a storable dict."""
    if msg.text:
        return {"type": "text", "content": msg.text, "parse_mode": "HTML"}
    elif msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption or ""}
    elif msg.video:
        return {"type": "video", "file_id": msg.video.file_id, "caption": msg.caption or ""}
    elif msg.document:
        return {"type": "document", "file_id": msg.document.file_id, "caption": msg.caption or ""}
    elif msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id, "caption": msg.caption or ""}
    elif msg.voice:
        return {"type": "voice", "file_id": msg.voice.file_id}
    elif msg.sticker:
        return {"type": "sticker", "file_id": msg.sticker.file_id}
    elif msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id, "caption": msg.caption or ""}
    elif msg.video_note:
        return {"type": "video_note", "file_id": msg.video_note.file_id}
    elif msg.contact:
        return {"type": "contact", "phone": msg.contact.phone_number, "name": msg.contact.first_name}
    elif msg.location:
        return {"type": "location", "lat": msg.location.latitude, "lon": msg.location.longitude}
    return None

async def send_stored_message(bot, chat_id, msg_data, reply_markup=None):
    """Send a stored message dict to a chat."""
    t = msg_data.get("type")
    caption = msg_data.get("caption", "")
    file_id = msg_data.get("file_id")
    mk = reply_markup

    if t == "text":
        await bot.send_message(chat_id, msg_data["content"], reply_markup=mk)
    elif t == "photo":
        await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=mk)
    elif t == "video":
        await bot.send_video(chat_id, file_id, caption=caption, reply_markup=mk)
    elif t == "document":
        await bot.send_document(chat_id, file_id, caption=caption, reply_markup=mk)
    elif t == "audio":
        await bot.send_audio(chat_id, file_id, caption=caption, reply_markup=mk)
    elif t == "voice":
        await bot.send_voice(chat_id, file_id, reply_markup=mk)
    elif t == "sticker":
        await bot.send_sticker(chat_id, file_id, reply_markup=mk)
    elif t == "animation":
        await bot.send_animation(chat_id, file_id, caption=caption, reply_markup=mk)
    elif t == "video_note":
        await bot.send_video_note(chat_id, file_id, reply_markup=mk)
    elif t == "contact":
        await bot.send_contact(chat_id, phone_number=msg_data["phone"], first_name=msg_data["name"], reply_markup=mk)
    elif t == "location":
        await bot.send_location(chat_id, latitude=msg_data["lat"], longitude=msg_data["lon"], reply_markup=mk)

def msg_preview_text(msg_data, idx):
    """Short admin preview of a stored message."""
    t = msg_data.get("type", "?")
    icons = {
        "text": "📝", "photo": "🖼️", "video": "🎬", "document": "📄",
        "audio": "🎵", "voice": "🎤", "sticker": "🎭", "animation": "🎞️",
        "video_note": "📹", "contact": "👤", "location": "📍"
    }
    icon = icons.get(t, "📦")
    if t == "text":
        preview = msg_data.get("content", "")[:30]
        if len(msg_data.get("content", "")) > 30:
            preview += "…"
    elif t == "contact":
        preview = msg_data.get("name", "Contact")
    elif t == "location":
        preview = f"{msg_data.get('lat', 0):.4f}, {msg_data.get('lon', 0):.4f}"
    else:
        preview = msg_data.get("caption", "") or t
        if len(preview) > 30:
            preview = preview[:30] + "…"
    return f"{icon} #{idx+1}: {preview}"

# ──────────────────────────────────────────────
# USER-FACING KEYBOARD
# ──────────────────────────────────────────────
def build_reply_keyboard(buttons, add_back=False):
    if not buttons and not add_back:
        return ReplyKeyboardRemove()
    rows = {}
    for btn in buttons:
        r = btn.get("row", 0)
        rows.setdefault(r, []).append(KeyboardButton(btn["label"]))
    keyboard = [rows[k] for k in sorted(rows)]
    if add_back:
        keyboard.append([KeyboardButton("🔙 Back")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_main_menu(menus):
    return menus.get("main") or next(
        (m for m in menus.values() if m.get("is_main")), None
    )

def find_button_in_menu(menu, text):
    for btn in menu.get("buttons", []):
        if btn["label"] == text:
            return btn
    return None

# ──────────────────────────────────────────────
# NESTED BUTTON PATH HELPERS
# A "path" is a list of button indices descending from the main
# menu's top-level buttons list, e.g. [] = top level, [2, 0] = the
# first sub-button of the button at top-level index 2.
# ──────────────────────────────────────────────
def get_buttons_at_path(menus, path):
    """Return the list of buttons living at `path` (creates nothing)."""
    main = get_main_menu(menus) or {}
    buttons = main.get("buttons", [])
    for i in path:
        if i < 0 or i >= len(buttons):
            return []
        buttons = buttons[i].setdefault("buttons", [])
    return buttons

def get_button_at_path(menus, path):
    """Return the button living at `path` (path must be non-empty), or None."""
    if not path:
        return None
    parent = get_buttons_at_path(menus, path[:-1])
    idx = path[-1]
    if 0 <= idx < len(parent):
        return parent[idx]
    return None

def path_valid(menus, path):
    if not path:
        return True
    parent_buttons = get_buttons_at_path(menus, path[:-1])
    return 0 <= path[-1] < len(parent_buttons)

# ──────────────────────────────────────────────
# ADMIN KEYBOARDS
# ──────────────────────────────────────────────
def admin_home_kb():
    return ReplyKeyboardMarkup([
        ["Manage Buttons", "💬 Welcome Message"],
        ["📢 Broadcast", "📊 Analytics"],
        ["👁️ Preview Bot"],
        ["🗑️ Reset All Buttons"],
    ], resize_keyboard=True)

def btn_list_kb(buttons, show_back=True, has_clipboard=False, show_up=False):
    rows_map = {}
    for btn in buttons:
        r = btn.get("row", 0)
        rows_map.setdefault(r, []).append(KeyboardButton(f"{btn['label']}"))
    rows = [rows_map[k] for k in sorted(rows_map)]
    rows.append(["➕ Add Button"])
    if has_clipboard:
        rows.append(["📋 Paste"])
    bottom = []
    if show_up:
        bottom.append("⬆️ Up a Level")
    if show_back:
        bottom.append("🔙 Back")
    if bottom:
        rows.append(bottom)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _two_per_row(items):
    rows = []
    for i in range(0, len(items), 2):
        rows.append(items[i:i+2])
    return rows

def btn_detail_kb(idx, total, btn_type, sub_buttons=None, has_clipboard=False):
    sub_buttons = sub_buttons or []
    rows = []

    # Sub-buttons of this button, 2 per row — tapping one opens its own
    # combined screen (sub-buttons + edit actions for that button).
    sub_labels = [f"{b['label']}" for b in sub_buttons]
    rows += _two_per_row(sub_labels)

    add_row = ["➕ Add Button"]
    if has_clipboard:
        add_row.append("📋 Paste")
    rows.append(add_row)

    # Edit actions for *this* button, 2 per row.
    actions = ["↕️ Reorder", "✏️ Edit Label", "📨 Messages", "📄 Copy", "✂️ Cut", "🗑️ Delete Button"]
    rows += _two_per_row(actions)

    rows.append(["🔙 Back"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def msg_list_kb(messages):
    rows = [
        [KeyboardButton("➕ Add Messages")],
        [KeyboardButton("🔙 Back")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def msg_inline_action_kb(idx, total):
    """Inline keyboard attached to each message in Preview All."""
    rows = []
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("⬆️ Move Up", callback_data=f"pmv_up:{idx}"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("⬇️ Move Down", callback_data=f"pmv_dn:{idx}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("✏️ Edit", callback_data=f"pedit:{idx}"),
        InlineKeyboardButton("🗑️ Delete", callback_data=f"pdel:{idx}"),
    ])
    return InlineKeyboardMarkup(rows)

def _normalize_rows(buttons):
    """Compact row numbers to 0, 1, 2, ... preserving sorted order."""
    sorted_unique = sorted(set(b.get("row", 0) for b in buttons))
    mapping = {r: i for i, r in enumerate(sorted_unique)}
    for btn in buttons:
        btn["row"] = mapping[btn.get("row", 0)]

def _btn_row_peers(buttons, idx):
    """Return (row_value, [indices of buttons on the same row])."""
    r = buttons[idx].get("row", 0)
    peers = [i for i, b in enumerate(buttons) if b.get("row", 0) == r]
    return r, peers

def reorder_kb(idx, buttons):
    """Real keyboard with ▶️ on selected button + nav controls."""
    rows_map = {}
    for i, btn in enumerate(buttons):
        r = btn.get("row", 0)
        label = f"▶️ {btn['label']}" if i == idx else btn["label"]
        rows_map.setdefault(r, []).append(KeyboardButton(label))

    keyboard = [rows_map[k] for k in sorted(rows_map)]

    cur_row, peers = _btn_row_peers(buttons, idx)
    sorted_rows = sorted(set(b.get("row", 0) for b in buttons))
    cur_row_pos = sorted_rows.index(cur_row)

    nav = []
    if cur_row_pos > 0:
        nav.append("⬆️ Row Up")
    if cur_row_pos < len(sorted_rows) - 1:
        nav.append("⬇️ Row Down")
    if nav:
        keyboard.append(nav)

    lr = []
    pos_in_row = peers.index(idx)
    if pos_in_row > 0:
        lr.append("⬅️ Move Left")
    if pos_in_row < len(peers) - 1:
        lr.append("➡️ Move Right")
    if lr:
        keyboard.append(lr)

    split = []
    if cur_row_pos > 0 or len(peers) > 1:
        split.append("⬆️ New Row")
    if cur_row_pos < len(sorted_rows) - 1 or len(peers) > 1:
        split.append("⬇️ New Row")
    if split:
        keyboard.append(split)

    keyboard.append(["✅ Done Reordering"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def broadcast_confirm_kb():
    return ReplyKeyboardMarkup([["✅ Send to all users", "❌ Cancel"]], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True)

def done_kb():
    return ReplyKeyboardMarkup([["✅ Done adding messages", "🔙 Cancel"]], resize_keyboard=True)

def preview_kb(buttons, add_back=False):
    """Exact user-facing keyboard + a small Exit Preview button at the bottom."""
    rows = {}
    for btn in buttons:
        r = btn.get("row", 0)
        rows.setdefault(r, []).append(KeyboardButton(btn["label"]))
    keyboard = [rows[k] for k in sorted(rows)]
    if add_back:
        keyboard.append([KeyboardButton("🔙 Back")])
    keyboard.append([KeyboardButton("🚪 Exit Preview")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def btn_label_from_display(display):
    return display.strip()

def msg_idx_from_display(display, messages):
    try:
        part = display.split("#")[1].split(":")[0].strip()
        idx = int(part) - 1
        if 0 <= idx < len(messages):
            return idx
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# FACTORY
# ──────────────────────────────────────────────
def make_handlers(owner_uid):

    # ── /start ──
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        data = get_user_data(owner_uid)
        admin_id = data.get("admin_id")
        menus = data.get("menus", {})

        if str(user_id) == str(admin_id):
            context.user_data.clear()
            await update.message.reply_text(
                "⚙️ *Admin Panel* — Choose an action:",
                parse_mode="Markdown",
                reply_markup=admin_home_kb()
            )
            return ADMIN_MAIN

        # Regular user — track and send welcome
        track_user(owner_uid, user_id)
        welcome = data.get("welcome_message", "Welcome!")
        main_menu = get_main_menu(menus)
        if main_menu and main_menu.get("buttons"):
            keyboard = build_reply_keyboard(main_menu["buttons"])
        else:
            keyboard = ReplyKeyboardRemove()
        await update.message.reply_text(welcome, reply_markup=keyboard)
        context.user_data["menu_stack"] = ["main"]
        return ConversationHandler.END

    # ── /help (admin only) ──
    async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        data = get_user_data(owner_uid)
        if str(user_id) != str(data.get("admin_id")):
            return
        help_text = (
            "📖 *Admin Help*\n\n"
            "🗂️ *Menus* — Create unlimited menus. One is the Main Menu shown on /start.\n"
            "*Buttons* — Add buttons to each menu. Each button can:\n"
            "  • 💬 Send multiple messages (text, photos, video, files, stickers...)\n"
            "  • 📂 Open a submenu (nested menus!)\n"
            "  • 🌐 Show a URL link\n"
            "📨 *Messages* — Per button, you can:\n"
            "  • Add many messages at once (forward directly!)\n"
            "  • Preview all of them\n"
            "  • Edit/delete/insert individual messages by position\n"
            "  • Reorder messages\n"
            "📢 *Broadcast* — Send a message to ALL your bot users at once\n"
            "📊 *Analytics* — See which buttons are most clicked\n"
            "👁️ *Preview* — See exactly what users see\n\n"
            "Tip: Forward any messages directly to me when assigning button messages!"
        )
        await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=admin_home_kb())
        return ADMIN_MAIN

    # ════════════════════════════════════════════
    # ADMIN HANDLERS
    # ════════════════════════════════════════════

    async def admin_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid

        if text == "Manage Buttons":
            menus = get_menus(uid)
            main_menu = get_main_menu(menus)
            if not main_menu:
                # Auto-create main menu if missing
                menus["main"] = {"name": "Main", "message": "Choose an option:", "is_main": True, "buttons": []}
                save_menus(uid, menus)
                main_menu = menus["main"]
            context.user_data["current_path"] = []
            buttons = main_menu.get("buttons", [])
            header = "*Your Buttons* — Choose one to edit:" if buttons else "*No buttons yet.* Tap ➕ Add Button to create your first."
            await update.message.reply_text(header, parse_mode="Markdown", reply_markup=btn_list_kb(buttons))
            return ADMIN_BTN_LIST

        elif text == "💬 Welcome Message":
            current = get_welcome(uid)
            await update.message.reply_text(
                f"💬 *Current:*\n_{current}_\n\nSend the new welcome message:",
                parse_mode="Markdown",
                reply_markup=cancel_kb()
            )
            return WAITING_WELCOME_MSG

        elif text == "📢 Broadcast":
            users = get_bot_users(uid)
            count = len(users)
            await update.message.reply_text(
                f"📢 *Broadcast Message*\n\n"
                f"You have *{count}* user(s).\n\n"
                f"Send the message you want to broadcast (text, photo, video, etc.):",
                parse_mode="Markdown",
                reply_markup=cancel_kb()
            )
            return WAITING_BROADCAST

        elif text == "📊 Analytics":
            analytics = get_analytics(uid)
            clicks = analytics.get("clicks", {})
            users = get_bot_users(uid)
            if not clicks:
                msg = f"📊 *Analytics*\n\n👥 Total users: {len(users)}\n\nNo button clicks recorded yet."
            else:
                sorted_clicks = sorted(clicks.items(), key=lambda x: x[1], reverse=True)
                lines = [f"📊 *Analytics*\n\n👥 Total users: {len(users)}\n\n*Top buttons:*"]
                for key, count in sorted_clicks[:15]:
                    parts = key.split("::")
                    btn_name = parts[1] if len(parts) > 1 else key
                    lines.append(f"• {btn_name}: *{count}* click(s)")
                msg = "\n".join(lines)
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_home_kb())
            return ADMIN_MAIN

        elif text == "👁️ Preview Bot":
            menus = get_menus(uid)
            main_menu = get_main_menu(menus)
            if not main_menu:
                await update.message.reply_text(
                    "⚠️ No buttons created yet. Add a button first via *Manage Buttons*.",
                    parse_mode="Markdown",
                    reply_markup=admin_home_kb()
                )
                return ADMIN_MAIN
            btns = main_menu.get("buttons", [])
            welcome = get_welcome(uid)
            context.user_data["preview_stack"] = ["main"]
            await update.message.reply_text(
                "👁️ *Preview Mode* — you're now seeing the bot as a user.\nTap *🚪 Exit Preview* to return to admin.",
                parse_mode="Markdown",
            )
            await update.message.reply_text(welcome, reply_markup=preview_kb(btns))
            return ADMIN_PREVIEW

        elif text == "🗑️ Reset All Buttons":
            menus = get_menus(uid)
            main_menu = get_main_menu(menus)
            count = len(main_menu.get("buttons", [])) if main_menu else 0
            if count == 0:
                await update.message.reply_text("ℹ️ No buttons to reset.", reply_markup=admin_home_kb())
                return ADMIN_MAIN
            await update.message.reply_text(
                f"⚠️ *Are you sure you want to delete ALL {count} button(s)?*\n\n"
                f"This will permanently remove every button and its messages.\n\n"
                f"Type *RESET* to confirm, or tap Cancel.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
            )
            return WAITING_RESET_CONFIRM

        await update.message.reply_text("Choose an option:", reply_markup=admin_home_kb())
        return ADMIN_MAIN

    # ── BROADCAST ──
    async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        uid = owner_uid

        if msg.text == "🔙 Cancel":
            await msg.reply_text("❌ Cancelled.", reply_markup=admin_home_kb())
            return ADMIN_MAIN

        serialized = serialize_message(msg)
        if not serialized:
            await msg.reply_text("⚠️ Unsupported message type.", reply_markup=cancel_kb())
            return WAITING_BROADCAST

        context.user_data["broadcast_msg"] = serialized
        users = get_bot_users(uid)
        await msg.reply_text(
            f"📢 Ready to send to *{len(users)}* user(s). Confirm?",
            parse_mode="Markdown",
            reply_markup=broadcast_confirm_kb()
        )
        return WAITING_BROADCAST_CONFIRM

    async def receive_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid

        if text != "✅ Send to all users":
            await update.message.reply_text("❌ Cancelled.", reply_markup=admin_home_kb())
            return ADMIN_MAIN

        broadcast_msg = context.user_data.get("broadcast_msg")
        users = get_bot_users(uid)
        bot = context.bot
        sent = 0
        failed = 0

        await update.message.reply_text(f"📤 Sending to {len(users)} users...", reply_markup=admin_home_kb())

        for user_id in users:
            try:
                await send_stored_message(bot, int(user_id), broadcast_msg)
                sent += 1
                await asyncio.sleep(0.05)  # Rate limit protection
            except Exception:
                failed += 1

        await update.message.reply_text(
            f"✅ Broadcast done!\n✅ Sent: {sent}\n❌ Failed: {failed}",
            reply_markup=admin_home_kb()
        )
        return ADMIN_MAIN

    # ── RESET CONFIRM ──
    async def receive_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        uid = owner_uid
        if text == "❌ Cancel":
            await update.message.reply_text("❌ Reset cancelled.", reply_markup=admin_home_kb())
            return ADMIN_MAIN
        if text != "RESET":
            await update.message.reply_text(
                "❌ Invalid. Type *RESET* exactly to confirm, or tap Cancel.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
            )
            return WAITING_RESET_CONFIRM
        save_menus(uid, {})
        await update.message.reply_text(
            "✅ *All menus have been reset.*\n\nUse *📋 Manage Menus* to start fresh.",
            parse_mode="Markdown",
            reply_markup=admin_home_kb()
        )
        return ADMIN_MAIN

    # ── BUTTON LIST (top level only — "Manage Buttons") ──
    async def admin_btn_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, [])
        clipboard = context.user_data.get("clipboard")

        if text == "🔙 Back":
            context.user_data["current_path"] = []
            await update.message.reply_text(
                "⚙️ *Admin Panel*",
                parse_mode="Markdown",
                reply_markup=admin_home_kb()
            )
            return ADMIN_MAIN

        elif text == "➕ Add Button":
            context.user_data["current_path"] = []
            context.user_data["insert_idx"] = None
            context.user_data["add_btn_return"] = "list"
            await update.message.reply_text("Send the *label* for the new button:", parse_mode="Markdown", reply_markup=cancel_kb())
            return WAITING_BTN_LABEL

        elif text == "📋 Paste" and clipboard:
            pasted = copy.deepcopy(clipboard["button"])
            buttons.append(pasted)
            if clipboard["mode"] == "cut":
                src_path = list(clipboard["source_path"])
                src_idx = clipboard["source_idx"]
                src_buttons = get_buttons_at_path(menus, src_path)
                if 0 <= src_idx < len(src_buttons):
                    src_buttons.pop(src_idx)
                context.user_data.pop("clipboard", None)
            save_menus(uid, menus)
            await update.message.reply_text(
                "✅ Pasted!",
                reply_markup=btn_list_kb(buttons, show_up=False, has_clipboard=bool(context.user_data.get("clipboard")))
            )
            return ADMIN_BTN_LIST

        else:
            label = btn_label_from_display(text)
            idx = next((i for i, b in enumerate(buttons) if b["label"] == label), None)
            if idx is None:
                await update.message.reply_text("❌ Not found.", reply_markup=btn_list_kb(buttons, show_up=False, has_clipboard=bool(clipboard)))
                return ADMIN_BTN_LIST
            btn = buttons[idx]

            # Tapping a top-level button opens its combined screen:
            # sub-buttons (if any) plus this button's edit actions.
            context.user_data["current_path"] = []
            context.user_data["current_btn_idx"] = idx
            msg_count = len(btn.get("messages", []))
            sub_count = len(btn.get("buttons", []))
            extra = f"\n📨 {msg_count} message(s) assigned"
            if sub_count:
                extra += f"\n📂 {sub_count} sub-button(s)"
            await update.message.reply_text(
                f"*{btn['label']}* ({idx+1} of {len(buttons)})\nRow: {btn.get('row', 0)}{extra}",
                parse_mode="Markdown",
                reply_markup=btn_detail_kb(idx, len(buttons), btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(clipboard))
            )
            return ADMIN_BTN_DETAIL

    # ── BUTTON DETAIL ──
    async def admin_btn_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        idx = context.user_data.get("current_btn_idx", 0)
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        clipboard = context.user_data.get("clipboard")

        if not buttons or idx >= len(buttons):
            await update.message.reply_text("❌ Button not found.", reply_markup=admin_home_kb())
            return ADMIN_MAIN

        btn = buttons[idx]

        def detail_kb():
            return btn_detail_kb(idx, len(buttons), btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))

        async def show_detail(button, button_idx, parent_buttons, header_extra=""):
            msg_count = len(button.get("messages", []))
            sub_count = len(button.get("buttons", []))
            extra = f"\n📨 {msg_count} message(s) assigned"
            if sub_count:
                extra += f"\n📂 {sub_count} sub-button(s)"
            await update.message.reply_text(
                f"*{button['label']}* ({button_idx+1} of {len(parent_buttons)})\nRow: {button.get('row', 0)}{extra}{header_extra}",
                parse_mode="Markdown",
                reply_markup=btn_detail_kb(button_idx, len(parent_buttons), button.get("type", "message"), sub_buttons=button.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))
            )

        if text == "🔙 Back":
            if not path:
                await update.message.reply_text(
                    "⚙️ *Admin Panel*",
                    parse_mode="Markdown",
                    reply_markup=admin_home_kb()
                )
                return ADMIN_MAIN
            # Return to the *combined* screen of the parent button.
            parent_path = path[:-1]
            parent_idx = path[-1]
            context.user_data["current_path"] = parent_path
            context.user_data["current_btn_idx"] = parent_idx
            parent_buttons = get_buttons_at_path(menus, parent_path)
            if not parent_buttons or parent_idx >= len(parent_buttons):
                await update.message.reply_text("❌ Button not found.", reply_markup=admin_home_kb())
                return ADMIN_MAIN
            await show_detail(parent_buttons[parent_idx], parent_idx, parent_buttons)
            return ADMIN_BTN_DETAIL

        elif text == "↕️ Reorder":
            total = len(buttons)
            if total < 2:
                await update.message.reply_text("ℹ️ Only one button at this level — nothing to reorder.", reply_markup=detail_kb())
                return ADMIN_BTN_DETAIL
            view_text = (
                f"↕️ *Reorder mode*\n\n"
                f"Selected: *{btn['label']}* ({idx+1} of {total})\n\n"
                f"Row Up/Down to move rows · ⬅️➡️ within row · ⬆️⬇️ New Row to split."
            )
            await update.message.reply_text(view_text, parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "✏️ Edit Label":
            await update.message.reply_text(
                f"✏️ Current: *{btn['label']}*\n\nSend the new label:",
                parse_mode="Markdown",
                reply_markup=cancel_kb()
            )
            return WAITING_EDIT_BTN_LABEL

        elif text == "📨 Messages":
            messages = btn.get("messages", [])
            if not messages:
                await update.message.reply_text(
                    "📨 No messages assigned yet.\n\nSend messages one by one (you can forward them!), then tap ✅ Done:",
                    reply_markup=done_kb()
                )
                context.user_data["assigning_to"] = (tuple(path), idx)
                context.user_data["temp_messages"] = []
                return WAITING_ASSIGN_MESSAGES
            for i, msg_data in enumerate(messages):
                await send_stored_message(
                    context.bot, update.effective_chat.id, msg_data,
                    reply_markup=msg_inline_action_kb(i, len(messages))
                )
            await update.message.reply_text(
                "👆 Tap buttons under any message to edit it.",
                reply_markup=msg_list_kb(messages)
            )
            return ADMIN_MSG_LIST

        elif text == "➕ Add Button":
            context.user_data["insert_idx"] = None
            context.user_data["add_btn_return"] = "detail"
            context.user_data["add_btn_parent_path"] = list(path)
            context.user_data["add_btn_parent_idx"] = idx
            context.user_data["current_path"] = path + [idx]
            await update.message.reply_text("Send the *label* for the new sub-button:", parse_mode="Markdown", reply_markup=cancel_kb())
            return WAITING_BTN_LABEL

        elif text == "📄 Copy":
            context.user_data["clipboard"] = {
                "button": copy.deepcopy(btn),
                "mode": "copy",
                "source_path": list(path),
                "source_idx": idx,
            }
            await update.message.reply_text(f"📄 Copied *{btn['label']}*. Paste it anywhere.", parse_mode="Markdown", reply_markup=detail_kb())
            return ADMIN_BTN_DETAIL

        elif text == "✂️ Cut":
            context.user_data["clipboard"] = {
                "button": copy.deepcopy(btn),
                "mode": "cut",
                "source_path": list(path),
                "source_idx": idx,
            }
            await update.message.reply_text(f"✂️ Cut *{btn['label']}*. It will be removed when you paste it elsewhere.", parse_mode="Markdown", reply_markup=detail_kb())
            return ADMIN_BTN_DETAIL

        elif text == "📋 Paste" and clipboard:
            pasted = copy.deepcopy(clipboard["button"])
            target_buttons = btn.setdefault("buttons", [])
            target_buttons.append(pasted)
            if clipboard["mode"] == "cut":
                src_path = list(clipboard["source_path"])
                src_idx = clipboard["source_idx"]
                src_buttons = get_buttons_at_path(menus, src_path)
                # Adjust for the case we're pasting into a descendant of the
                # cut button's own list at a higher index — not applicable
                # here since we're pasting *inside* btn, a different list
                # than src_buttons in all valid cases (can't paste a button
                # inside itself via this action). Just remove the original.
                if 0 <= src_idx < len(src_buttons):
                    src_buttons.pop(src_idx)
                context.user_data.pop("clipboard", None)
            save_menus(uid, menus)
            await update.message.reply_text(
                f"✅ Pasted into *{btn['label']}*'s sub-buttons!", parse_mode="Markdown",
                reply_markup=detail_kb()
            )
            return ADMIN_BTN_DETAIL

        elif text == "➕ Insert Before":
            context.user_data["insert_idx"] = idx
            context.user_data["insert_before"] = True
            await update.message.reply_text("Send the label:", reply_markup=cancel_kb())
            return WAITING_INSERT_BTN_LABEL

        elif text == "➕ Insert After":
            context.user_data["insert_idx"] = idx
            context.user_data["insert_before"] = False
            await update.message.reply_text("Send the label:", reply_markup=cancel_kb())
            return WAITING_INSERT_BTN_LABEL

        elif text == "🗑️ Delete Button":
            context.user_data["delete_type"] = "button"
            await update.message.reply_text(
                f"⚠️ Delete *{btn['label']}*?",
                parse_mode="Markdown",
                reply_markup=confirm_kb()
            )
            return WAITING_DELETE_CONFIRM

        # Otherwise, check if a sub-button label was tapped — open its
        # own combined screen (sub-buttons + edit actions for it).
        sub_buttons = btn.get("buttons", [])
        label = btn_label_from_display(text)
        sub_idx = next((i for i, b in enumerate(sub_buttons) if b["label"] == label), None)
        if sub_idx is not None:
            sub_path = path + [idx]
            context.user_data["current_path"] = sub_path
            context.user_data["current_btn_idx"] = sub_idx
            await show_detail(sub_buttons[sub_idx], sub_idx, sub_buttons)
            return ADMIN_BTN_DETAIL

        await update.message.reply_text("Choose an option:", reply_markup=detail_kb())
        return ADMIN_BTN_DETAIL

    # ── BUTTON REORDER ──
    async def admin_btn_reorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        idx = context.user_data.get("current_btn_idx", 0)
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        clipboard = context.user_data.get("clipboard")

        if not buttons or idx >= len(buttons):
            await update.message.reply_text("❌ Button not found.", reply_markup=admin_home_kb())
            return ADMIN_MAIN

        total = len(buttons)

        def reorder_view(cur_idx):
            btn_label = buttons[cur_idx]['label']
            return (
                f"↕️ *Reorder mode*\n\n"
                f"Selected: *{btn_label}* ({cur_idx+1} of {total})\n\n"
                f"Row Up/Down to move rows · ⬅️➡️ within row · ⬆️⬇️ New Row to split."
            )

        if text == "⬆️ Row Up":
            cur_row, peers = _btn_row_peers(buttons, idx)
            sorted_rows = sorted(set(b.get("row", 0) for b in buttons))
            cur_pos = sorted_rows.index(cur_row)
            if cur_pos > 0:
                # Merge into the row above
                above_row = sorted_rows[cur_pos - 1]
                buttons[idx]["row"] = above_row
            else:
                # Already at top — split into a new row above (only if not already alone)
                if len(peers) > 1:
                    buttons[idx]["row"] = cur_row - 0.5
                # If already alone on the top row, do nothing
            _normalize_rows(buttons)
            save_menus(uid, menus)
            await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "⬇️ Row Down":
            cur_row, peers = _btn_row_peers(buttons, idx)
            sorted_rows = sorted(set(b.get("row", 0) for b in buttons))
            cur_pos = sorted_rows.index(cur_row)
            if cur_pos < len(sorted_rows) - 1:
                # Merge into the row below
                below_row = sorted_rows[cur_pos + 1]
                buttons[idx]["row"] = below_row
            else:
                # Already at bottom — split into a new row below (only if not already alone)
                if len(peers) > 1:
                    buttons[idx]["row"] = cur_row + 0.5
                # If already alone on the bottom row, do nothing
            _normalize_rows(buttons)
            save_menus(uid, menus)
            await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "⬅️ Move Left":
            cur_row, peers = _btn_row_peers(buttons, idx)
            pos_in_row = peers.index(idx)
            if pos_in_row > 0:
                left_idx = peers[pos_in_row - 1]
                buttons[idx], buttons[left_idx] = buttons[left_idx], buttons[idx]
                idx = left_idx
                context.user_data["current_btn_idx"] = idx
                save_menus(uid, menus)
            await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "➡️ Move Right":
            cur_row, peers = _btn_row_peers(buttons, idx)
            pos_in_row = peers.index(idx)
            if pos_in_row < len(peers) - 1:
                right_idx = peers[pos_in_row + 1]
                buttons[idx], buttons[right_idx] = buttons[right_idx], buttons[idx]
                idx = right_idx
                context.user_data["current_btn_idx"] = idx
                save_menus(uid, menus)
            await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "⬆️ New Row":
            cur_row, peers = _btn_row_peers(buttons, idx)
            if len(peers) > 1:
                buttons[idx]["row"] = cur_row - 0.5
                _normalize_rows(buttons)
                save_menus(uid, menus)
            await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "⬇️ New Row":
            cur_row, peers = _btn_row_peers(buttons, idx)
            if len(peers) > 1:
                buttons[idx]["row"] = cur_row + 0.5
                _normalize_rows(buttons)
                save_menus(uid, menus)
            await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
            return ADMIN_BTN_REORDER

        elif text == "✅ Done Reordering":
            btn = buttons[idx]
            msg_count = len(btn.get("messages", []))
            sub_count = len(btn.get("buttons", []))
            extra = f"\n📨 {msg_count} message(s) assigned"
            if sub_count:
                extra += f"\n📂 {sub_count} sub-button(s)"
            await update.message.reply_text(
                f"✅ Order saved!\n\n*{btn['label']}* ({idx+1} of {total})\nRow: {btn.get('row', 0)}{extra}",
                parse_mode="Markdown",
                reply_markup=btn_detail_kb(idx, total, btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(clipboard))
            )
            return ADMIN_BTN_DETAIL

        # Any other input — re-show the view
        await update.message.reply_text(reorder_view(idx), parse_mode="Markdown", reply_markup=reorder_kb(idx, buttons))
        return ADMIN_BTN_REORDER

    # ── MESSAGE LIST ──
    async def admin_msg_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        idx = context.user_data.get("current_btn_idx", 0)
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        btn = buttons[idx] if idx < len(buttons) else {}
        messages = btn.get("messages", [])

        if text == "🔙 Back":
            await update.message.reply_text(
                f"*{btn.get('label', 'Button')}*",
                parse_mode="Markdown",
                reply_markup=btn_detail_kb(idx, len(buttons), btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))
            )
            return ADMIN_BTN_DETAIL

        elif text == "➕ Add Messages":
            context.user_data["assigning_to"] = (tuple(path), idx)
            context.user_data["temp_messages"] = []
            context.user_data["append_to_existing"] = True
            await update.message.reply_text(
                "📨 Send messages one by one (you can forward them!). Tap ✅ Done when finished:",
                reply_markup=done_kb()
            )
            return WAITING_ASSIGN_MESSAGES

        else:
            await update.message.reply_text("Choose an option:", reply_markup=msg_list_kb(messages))
            return ADMIN_MSG_LIST

    # ══════════════════════════════════════════
    # ══════════════════════════════════════════
    # WAITING STATE HANDLERS
    # ══════════════════════════════════════════

    async def receive_welcome_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message.text == "🔙 Cancel":
            await update.message.reply_text("❌ Cancelled.", reply_markup=admin_home_kb())
            return ADMIN_MAIN
        save_welcome(owner_uid, update.message.text.strip())
        await update.message.reply_text("✅ Welcome message updated!", reply_markup=admin_home_kb())
        return ADMIN_MAIN

    async def receive_btn_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        return_to = context.user_data.get("add_btn_return", "list")

        if update.message.text == "🔙 Cancel":
            menus = get_menus(uid)
            if return_to == "detail":
                parent_path = context.user_data.get("add_btn_parent_path", [])
                parent_idx = context.user_data.get("add_btn_parent_idx", 0)
                context.user_data["current_path"] = parent_path
                context.user_data["current_btn_idx"] = parent_idx
                parent_buttons = get_buttons_at_path(menus, parent_path)
                if not parent_buttons or parent_idx >= len(parent_buttons):
                    await update.message.reply_text("❌ Button not found.", reply_markup=admin_home_kb())
                    return ADMIN_MAIN
                parent_btn = parent_buttons[parent_idx]
                await update.message.reply_text(
                    "❌ Cancelled.",
                    reply_markup=btn_detail_kb(parent_idx, len(parent_buttons), parent_btn.get("type", "message"), sub_buttons=parent_btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))
                )
                return ADMIN_BTN_DETAIL
            buttons = get_buttons_at_path(menus, path)
            await update.message.reply_text("❌ Cancelled.", reply_markup=btn_list_kb(buttons, show_up=bool(path), has_clipboard=bool(context.user_data.get("clipboard"))))
            return ADMIN_BTN_LIST

        label = update.message.text.strip()
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        insert_idx = context.user_data.get("insert_idx")
        insert_before = context.user_data.get("insert_before", False)
        # New button gets its own row (max existing row + 1)
        max_row = max((b.get("row", 0) for b in buttons), default=-1)
        new_btn = {"label": label, "type": "message", "value": "", "row": max_row + 1, "messages": [], "buttons": []}
        if insert_idx is None:
            buttons.append(new_btn)
            new_idx = len(buttons) - 1
        else:
            pos = insert_idx if insert_before else insert_idx + 1
            buttons.insert(pos, new_btn)
            new_idx = pos
        _normalize_rows(buttons)
        save_menus(uid, menus)
        context.user_data["current_btn_idx"] = new_idx
        context.user_data["assigning_to"] = (tuple(path), new_idx)
        context.user_data["temp_messages"] = []
        context.user_data.pop("add_btn_return", None)
        context.user_data.pop("add_btn_parent_path", None)
        context.user_data.pop("add_btn_parent_idx", None)
        await update.message.reply_text(
            f"✅ Button *{label}* created!\n\n📨 Now send the messages for this button (you can forward them!).\nTap ✅ Done when finished:",
            parse_mode="Markdown",
            reply_markup=done_kb()
        )
        return WAITING_ASSIGN_MESSAGES

    async def receive_edit_btn_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        idx = context.user_data.get("current_btn_idx", 0)
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        if update.message.text == "🔙 Cancel":
            btn = buttons[idx]
            await update.message.reply_text("❌ Cancelled.", reply_markup=btn_detail_kb(idx, len(buttons), btn["type"], sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard"))))
            return ADMIN_BTN_DETAIL
        buttons[idx]["label"] = update.message.text.strip()
        save_menus(uid, menus)
        btn = buttons[idx]
        await update.message.reply_text("✅ Label updated!", reply_markup=btn_detail_kb(idx, len(buttons), btn["type"], sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard"))))
        return ADMIN_BTN_DETAIL

    async def receive_insert_btn_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        if update.message.text == "🔙 Cancel":
            buttons = get_buttons_at_path(get_menus(uid), path)
            await update.message.reply_text("❌ Cancelled.", reply_markup=btn_list_kb(buttons, show_up=bool(path), has_clipboard=bool(context.user_data.get("clipboard"))))
            return ADMIN_BTN_LIST
        label = update.message.text.strip()
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        insert_idx = context.user_data.get("insert_idx")
        insert_before = context.user_data.get("insert_before", False)
        max_row = max((b.get("row", 0) for b in buttons), default=-1)
        new_btn = {"label": label, "type": "message", "value": "", "row": max_row + 1, "messages": [], "buttons": []}
        pos = insert_idx if insert_before else insert_idx + 1
        buttons.insert(pos, new_btn)
        _normalize_rows(buttons)
        save_menus(uid, menus)
        new_idx = pos
        context.user_data["current_btn_idx"] = new_idx
        context.user_data["assigning_to"] = (tuple(path), new_idx)
        context.user_data["temp_messages"] = []
        await update.message.reply_text(
            f"✅ Button *{label}* inserted!\n\n📨 Send the messages for this button. Tap ✅ Done when finished:",
            parse_mode="Markdown",
            reply_markup=done_kb()
        )
        return WAITING_ASSIGN_MESSAGES

    # ── ASSIGN MESSAGES ──
    async def receive_assign_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        uid = owner_uid

        if msg.text and msg.text in ["✅ Done adding messages", "🔙 Cancel"]:
            path, btn_idx = context.user_data.get("assigning_to", ((), None))
            path = list(path) if path else []
            temp = context.user_data.get("temp_messages", [])
            cancelled = msg.text == "🔙 Cancel"

            context.user_data["current_path"] = path
            context.user_data["current_btn_idx"] = btn_idx

            if not cancelled and temp:
                menus = get_menus(uid)
                buttons = get_buttons_at_path(menus, path)
                append = context.user_data.get("append_to_existing", False)
                if append:
                    existing = buttons[btn_idx].get("messages", [])
                    buttons[btn_idx]["messages"] = existing + temp
                else:
                    buttons[btn_idx]["messages"] = temp
                save_menus(uid, menus)
                btn = buttons[btn_idx]
                total = len(btn["messages"])
                await update.message.reply_text(
                    f"✅ {len(temp)} message(s) saved! ({total} total)",
                    reply_markup=btn_detail_kb(btn_idx, len(buttons), btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))
                )
            elif cancelled:
                await update.message.reply_text("❌ Cancelled.", reply_markup=admin_home_kb())
                context.user_data.pop("temp_messages", None)
                context.user_data.pop("append_to_existing", None)
                return ADMIN_MAIN
            else:
                await update.message.reply_text("⚠️ No messages received.", reply_markup=admin_home_kb())
                context.user_data.pop("temp_messages", None)
                context.user_data.pop("append_to_existing", None)
                return ADMIN_MAIN

            context.user_data.pop("temp_messages", None)
            context.user_data.pop("append_to_existing", None)
            return ADMIN_BTN_DETAIL

        # Support forwarded messages too
        serialized = serialize_message(msg)
        if serialized:
            context.user_data.setdefault("temp_messages", []).append(serialized)
            count = len(context.user_data["temp_messages"])
            await msg.reply_text(f"✅ Message #{count} added. Send more or tap ✅ Done:", reply_markup=done_kb())
        else:
            await msg.reply_text("⚠️ Unsupported message type. Try text, photo, video, or document.", reply_markup=done_kb())

        return WAITING_ASSIGN_MESSAGES

    # ── EDIT A SINGLE MESSAGE ──
    async def receive_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        btn_idx = context.user_data.get("current_btn_idx", 0)
        msg_idx = context.user_data.get("current_msg_idx", 0)
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        messages = buttons[btn_idx].get("messages", [])

        if update.message.text == "🔙 Cancel":
            await update.message.reply_text("❌ Cancelled.", reply_markup=msg_list_kb(messages))
            return ADMIN_MSG_LIST

        serialized = serialize_message(update.message)
        if not serialized:
            await update.message.reply_text("⚠️ Unsupported type. Try text, photo, video, or document.", reply_markup=ReplyKeyboardRemove())
            return WAITING_EDIT_MESSAGE

        messages[msg_idx] = serialized
        save_menus(uid, menus)

        if context.user_data.pop("preview_return", False):
            messages = buttons[btn_idx].get("messages", [])
            await update.message.reply_text("✅ Message updated! Re-previewing:")
            for i, msg_data in enumerate(messages):
                await send_stored_message(context.bot, update.effective_chat.id, msg_data,
                    reply_markup=msg_inline_action_kb(i, len(messages)))
            await update.message.reply_text("👆 Tap buttons under any message to edit it.", reply_markup=msg_list_kb(messages))
            return ADMIN_MSG_LIST

        await update.message.reply_text("✅ Message updated!", reply_markup=msg_list_kb(messages))
        return ADMIN_MSG_LIST

    # ── INSERT MULTIPLE MESSAGES ──
    async def receive_insert_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        btn_idx = context.user_data.get("current_btn_idx", 0)
        msg_idx = context.user_data.get("current_msg_idx", 0)
        before = context.user_data.get("insert_msg_before", False)
        menus = get_menus(uid)
        buttons = get_buttons_at_path(menus, path)
        messages = buttons[btn_idx].get("messages", [])

        if update.message.text in ["✅ Done adding messages", "🔙 Cancel"]:
            temp = context.user_data.pop("temp_insert_messages", [])
            cancelled = update.message.text == "🔙 Cancel"

            if not cancelled and temp:
                pos = msg_idx if before else msg_idx + 1
                for i, m in enumerate(temp):
                    messages.insert(pos + i, m)
                save_menus(uid, menus)
                context.user_data["current_msg_idx"] = pos

                if context.user_data.pop("preview_return", False):
                    await update.message.reply_text(f"✅ {len(temp)} message(s) inserted! Re-previewing:")
                    for i, msg_data in enumerate(messages):
                        await send_stored_message(context.bot, update.effective_chat.id, msg_data,
                            reply_markup=msg_inline_action_kb(i, len(messages)))
                    await update.message.reply_text("👆 Tap buttons under any message to edit it.", reply_markup=msg_list_kb(messages))
                    return ADMIN_MSG_LIST

                await update.message.reply_text(
                    f"✅ {len(temp)} message(s) inserted at position #{pos+1}!",
                    reply_markup=msg_list_kb(messages)
                )
            elif cancelled:
                context.user_data.pop("preview_return", None)
                await update.message.reply_text("❌ Cancelled.", reply_markup=msg_list_kb(messages))
            else:
                await update.message.reply_text("⚠️ No messages received.", reply_markup=msg_list_kb(messages))

            return ADMIN_MSG_LIST

        serialized = serialize_message(update.message)
        if not serialized:
            await update.message.reply_text("⚠️ Unsupported type.", reply_markup=done_kb())
            return WAITING_INSERT_MESSAGE

        context.user_data.setdefault("temp_insert_messages", []).append(serialized)
        count = len(context.user_data["temp_insert_messages"])
        await update.message.reply_text(f"✅ Message #{count} added. Send more or tap ✅ Done:", reply_markup=done_kb())
        return WAITING_INSERT_MESSAGE

    # ── DELETE CONFIRM ──
    async def waiting_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid
        path = context.user_data.get("current_path", [])
        menus = get_menus(uid)

        if text == "✅ Yes, delete":
            dtype = context.user_data.get("delete_type")

            if dtype == "button":
                idx = context.user_data.get("current_btn_idx", 0)
                buttons = get_buttons_at_path(menus, path)
                if idx < len(buttons):
                    removed = buttons.pop(idx)
                    save_menus(uid, menus)

                    if path:
                        # Deleted a sub-button: return to the parent's
                        # combined detail screen.
                        parent_path = path[:-1]
                        parent_idx = path[-1]
                        context.user_data["current_path"] = parent_path
                        context.user_data["current_btn_idx"] = parent_idx
                        parent_buttons = get_buttons_at_path(menus, parent_path)
                        if not parent_buttons or parent_idx >= len(parent_buttons):
                            await update.message.reply_text(f"🗑️ *{removed['label']}* deleted.", parse_mode="Markdown", reply_markup=admin_home_kb())
                            return ADMIN_MAIN
                        parent_btn = parent_buttons[parent_idx]
                        msg_count = len(parent_btn.get("messages", []))
                        sub_count = len(parent_btn.get("buttons", []))
                        extra = f"\n📨 {msg_count} message(s) assigned"
                        if sub_count:
                            extra += f"\n📂 {sub_count} sub-button(s)"
                        await update.message.reply_text(
                            f"🗑️ *{removed['label']}* deleted.\n\n*{parent_btn['label']}* ({parent_idx+1} of {len(parent_buttons)})\nRow: {parent_btn.get('row', 0)}{extra}",
                            parse_mode="Markdown",
                            reply_markup=btn_detail_kb(parent_idx, len(parent_buttons), parent_btn.get("type", "message"), sub_buttons=parent_btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))
                        )
                        return ADMIN_BTN_DETAIL

                    # Deleted a top-level button: return to the plain list.
                    await update.message.reply_text(
                        f"🗑️ *{removed['label']}* deleted.",
                        parse_mode="Markdown",
                        reply_markup=btn_list_kb(buttons, show_up=False, has_clipboard=bool(context.user_data.get("clipboard"))) if buttons else admin_home_kb()
                    )
                    return ADMIN_BTN_LIST if buttons else ADMIN_MAIN
                return ADMIN_BTN_LIST if (buttons or path) else ADMIN_MAIN

            elif dtype == "message":
                btn_idx = context.user_data.get("current_btn_idx", 0)
                msg_idx = context.user_data.get("current_msg_idx", 0)
                buttons = get_buttons_at_path(menus, path)
                messages = buttons[btn_idx].get("messages", [])
                if msg_idx < len(messages):
                    messages.pop(msg_idx)
                    save_menus(uid, menus)

                if context.user_data.pop("preview_return", False) and messages:
                    await update.message.reply_text(f"🗑️ Message #{msg_idx+1} deleted. Re-previewing:")
                    for i, msg_data in enumerate(messages):
                        await send_stored_message(context.bot, update.effective_chat.id, msg_data,
                            reply_markup=msg_inline_action_kb(i, len(messages)))
                    await update.message.reply_text("👆 Tap buttons under any message to edit it.", reply_markup=msg_list_kb(messages))
                    return ADMIN_MSG_LIST

                btn = buttons[btn_idx]
                await update.message.reply_text(
                    f"🗑️ Message #{msg_idx+1} deleted.",
                    reply_markup=msg_list_kb(messages) if messages else btn_detail_kb(btn_idx, len(buttons), btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard")))
                )
                return ADMIN_MSG_LIST if messages else ADMIN_BTN_DETAIL

        else:
            dtype = context.user_data.get("delete_type")
            buttons = get_buttons_at_path(menus, path)
            if dtype == "button":
                idx = context.user_data.get("current_btn_idx", 0)
                btn = buttons[idx] if idx < len(buttons) else {}
                await update.message.reply_text("❌ Cancelled.", reply_markup=btn_detail_kb(idx, len(buttons), btn.get("type", "message"), sub_buttons=btn.get("buttons", []), has_clipboard=bool(context.user_data.get("clipboard"))))
                return ADMIN_BTN_DETAIL
            elif dtype == "message":
                btn_idx = context.user_data.get("current_btn_idx", 0)
                messages = buttons[btn_idx].get("messages", []) if btn_idx < len(buttons) else []
                context.user_data.pop("preview_return", None)
                await update.message.reply_text("❌ Cancelled.", reply_markup=msg_list_kb(messages))
                return ADMIN_MSG_LIST

        return ADMIN_MAIN

    # ════════════════════════════════════════════
    # PREVIEW MODE HANDLER
    # ════════════════════════════════════════════
    async def admin_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        uid = owner_uid
        data = get_user_data(uid)
        menus = data.get("menus", {})

        if text == "🚪 Exit Preview":
            context.user_data.pop("preview_stack", None)
            await update.message.reply_text("✅ Exited preview. Back to admin panel.", reply_markup=admin_home_kb())
            return ADMIN_MAIN

        stack = context.user_data.setdefault("preview_stack", ["main"])
        current_mid = stack[-1] if stack else "main"
        current_menu = menus.get(current_mid, get_main_menu(menus))

        if text == "🔙 Back":
            if len(stack) > 1:
                stack.pop()
                current_mid = stack[-1]
                current_menu = menus.get(current_mid, get_main_menu(menus))
            else:
                context.user_data["preview_stack"] = ["main"]
                current_mid = "main"
                current_menu = get_main_menu(menus)
            btns = current_menu.get("buttons", []) if current_menu else []
            await update.message.reply_text(
                current_menu.get("message", "Choose an option:") if current_menu else "Choose an option:",
                reply_markup=preview_kb(btns, add_back=len(stack) > 1)
            )
            return ADMIN_PREVIEW

        btn = find_button_in_menu(current_menu, text) if current_menu else None

        if not btn:
            main_menu = get_main_menu(menus)
            btns = main_menu.get("buttons", []) if main_menu else []
            await update.message.reply_text("Please choose an option from the menu:", reply_markup=preview_kb(btns))
            context.user_data["preview_stack"] = ["main"]
            return ADMIN_PREVIEW

        if btn["type"] == "message":
            messages = btn.get("messages", [])
            if not messages:
                await update.message.reply_text(
                    "⚠️ No content set for this button yet.",
                    reply_markup=preview_kb(current_menu.get("buttons", []), add_back=len(stack) > 1)
                )
                return ADMIN_PREVIEW
            keyboard = preview_kb(current_menu.get("buttons", []), add_back=len(stack) > 1)
            for i, msg_data in enumerate(messages):
                mk = keyboard if i == len(messages) - 1 else None
                await send_stored_message(context.bot, update.effective_chat.id, msg_data, reply_markup=mk)

        elif btn["type"] == "submenu":
            target_mid = btn.get("value")
            target = menus.get(target_mid)
            if not target:
                await update.message.reply_text(
                    "❌ This menu no longer exists.",
                    reply_markup=preview_kb(current_menu.get("buttons", []), add_back=len(stack) > 1)
                )
                return ADMIN_PREVIEW
            stack.append(target_mid)
            await update.message.reply_text(
                target.get("message", "Choose an option:"),
                reply_markup=preview_kb(target.get("buttons", []), add_back=True)
            )

        elif btn["type"] == "url":
            await update.message.reply_text(
                f"🔗 {btn['label']}:\n{btn.get('value', '')}",
                reply_markup=preview_kb(current_menu.get("buttons", []), add_back=len(stack) > 1)
            )

        return ADMIN_PREVIEW

    # ════════════════════════════════════════════
    # REGULAR USER HANDLER
    # ════════════════════════════════════════════
    async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        nudge_keepalive()
        user_id = update.effective_user.id
        data = get_user_data(owner_uid)
        if str(user_id) == str(data.get("admin_id")):
            return

        text = update.message.text
        menus = data.get("menus", {})

        if "menu_stack" not in context.user_data:
            context.user_data["menu_stack"] = ["main"]

        stack = context.user_data["menu_stack"]
        current_mid = stack[-1] if stack else "main"
        current_menu = menus.get(current_mid, get_main_menu(menus))

        if text == "🔙 Back":
            if len(stack) > 1:
                stack.pop()
                current_mid = stack[-1]
                current_menu = menus.get(current_mid, get_main_menu(menus))
            else:
                current_menu = get_main_menu(menus)
                context.user_data["menu_stack"] = ["main"]
                current_mid = "main"
            keyboard = build_reply_keyboard(
                current_menu.get("buttons", []) if current_menu else [],
                add_back=len(stack) > 1
            )
            await update.message.reply_text(
                current_menu.get("message", "Choose an option:") if current_menu else "Choose an option:",
                reply_markup=keyboard
            )
            return

        btn = find_button_in_menu(current_menu, text) if current_menu else None

        if not btn:
            main_menu = get_main_menu(menus)
            keyboard = build_reply_keyboard(main_menu.get("buttons", []) if main_menu else [])
            await update.message.reply_text("Please choose an option from the menu:", reply_markup=keyboard)
            context.user_data["menu_stack"] = ["main"]
            return

        # Track click
        track_button_click(owner_uid, current_mid, btn["label"])

        if btn["type"] == "message":
            messages = btn.get("messages", [])
            if not messages:
                await update.message.reply_text("⚠️ No content set for this button yet.")
                return
            keyboard = build_reply_keyboard(current_menu.get("buttons", []), add_back=len(stack) > 1)
            for i, msg_data in enumerate(messages):
                mk = keyboard if i == len(messages) - 1 else None
                await send_stored_message(context.bot, update.effective_chat.id, msg_data, reply_markup=mk)

        elif btn["type"] == "submenu":
            target_mid = btn.get("value")
            target = menus.get(target_mid)
            if not target:
                await update.message.reply_text("❌ This menu no longer exists.")
                return
            stack.append(target_mid)
            keyboard = build_reply_keyboard(target.get("buttons", []), add_back=True)
            await update.message.reply_text(target.get("message", "Choose an option:"), reply_markup=keyboard)

        elif btn["type"] == "url":
            keyboard = build_reply_keyboard(current_menu.get("buttons", []), add_back=len(stack) > 1)
            await update.message.reply_text(f"🔗 {btn['label']}:\n{btn.get('value', '')}", reply_markup=keyboard)

    async def handle_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        uid = owner_uid
        mid = context.user_data.get("current_mid")
        btn_idx = context.user_data.get("current_btn_idx", 0)
        menus = get_menus(uid)
        buttons = menus.get(mid, {}).get("buttons", [])
        btn = buttons[btn_idx] if btn_idx < len(buttons) else {}
        messages = btn.get("messages", [])
        total = len(messages)

        if data.startswith("pmv_up:"):
            idx = int(data.split(":")[1])
            if idx > 0:
                messages[idx], messages[idx-1] = messages[idx-1], messages[idx]
                save_menus(uid, menus)
                # Refresh inline buttons on all messages by re-sending them
                await query.message.reply_text("✅ Moved up! Re-sending preview...")
                for i, msg_data in enumerate(messages):
                    await send_stored_message(context.bot, query.message.chat_id, msg_data,
                        reply_markup=msg_inline_action_kb(i, total))
            return

        if data.startswith("pmv_dn:"):
            idx = int(data.split(":")[1])
            if idx < total - 1:
                messages[idx], messages[idx+1] = messages[idx+1], messages[idx]
                save_menus(uid, menus)
                await query.message.reply_text("✅ Moved down! Re-sending preview...")
                for i, msg_data in enumerate(messages):
                    await send_stored_message(context.bot, query.message.chat_id, msg_data,
                        reply_markup=msg_inline_action_kb(i, total))
            return

        if data.startswith("pedit:"):
            idx = int(data.split(":")[1])
            context.user_data["current_msg_idx"] = idx
            context.user_data["preview_return"] = True
            await query.message.reply_text(
                f"✏️ Send the replacement for message #{idx+1}:",
                reply_markup=cancel_kb()
            )
            return WAITING_EDIT_MESSAGE

        if data.startswith("pdel:"):
            idx = int(data.split(":")[1])
            context.user_data["current_msg_idx"] = idx
            context.user_data["delete_type"] = "message"
            context.user_data["preview_return"] = True
            await query.message.reply_text(
                f"⚠️ Delete message #{idx+1}?",
                reply_markup=confirm_kb()
            )
            return WAITING_DELETE_CONFIRM

        if data.startswith("pins_b:"):
            idx = int(data.split(":")[1])
            context.user_data["current_msg_idx"] = idx
            context.user_data["insert_msg_before"] = True
            context.user_data["preview_return"] = True
            context.user_data["temp_insert_messages"] = []
            await query.message.reply_text(
                f"➕ Insert before message #{idx+1}. Send messages one by one, then tap ✅ Done:",
                reply_markup=done_kb()
            )
            return WAITING_INSERT_MESSAGE

        if data.startswith("pins_a:"):
            idx = int(data.split(":")[1])
            context.user_data["current_msg_idx"] = idx
            context.user_data["insert_msg_before"] = False
            context.user_data["preview_return"] = True
            context.user_data["temp_insert_messages"] = []
            await query.message.reply_text(
                f"➕ Insert after message #{idx+1}. Send messages one by one, then tap ✅ Done:",
                reply_markup=done_kb()
            )
            return WAITING_INSERT_MESSAGE


    # ── Build ConversationHandler ──
    txt = filters.TEXT & ~filters.COMMAND
    all_msg = filters.ALL & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", admin_help),
        ],
        states={
            ADMIN_MAIN:               [MessageHandler(txt, admin_main)],
            ADMIN_BTN_LIST:           [MessageHandler(txt, admin_btn_list)],
            ADMIN_BTN_DETAIL:         [MessageHandler(txt, admin_btn_detail)],
            ADMIN_MSG_LIST:           [MessageHandler(txt, admin_msg_list), CallbackQueryHandler(handle_preview_callback, pattern=r"^(pmv_up|pmv_dn|pedit|pdel|pins_b|pins_a):")],
            WAITING_WELCOME_MSG:      [MessageHandler(txt, receive_welcome_msg)],
            WAITING_BTN_LABEL:        [MessageHandler(txt, receive_btn_label)],
            WAITING_EDIT_BTN_LABEL:   [MessageHandler(txt, receive_edit_btn_label)],
            WAITING_INSERT_BTN_LABEL: [MessageHandler(txt, receive_insert_btn_label)],
            WAITING_DELETE_CONFIRM:   [MessageHandler(txt, waiting_delete_confirm), CallbackQueryHandler(handle_preview_callback, pattern=r"^(pmv_up|pmv_dn|pedit|pdel|pins_b|pins_a):")],
            WAITING_ASSIGN_MESSAGES:  [MessageHandler(all_msg, receive_assign_messages)],
            WAITING_EDIT_MESSAGE:     [MessageHandler(all_msg, receive_edit_message), CallbackQueryHandler(handle_preview_callback, pattern=r"^(pmv_up|pmv_dn|pedit|pdel|pins_b|pins_a):")],
            WAITING_INSERT_MESSAGE:   [MessageHandler(all_msg, receive_insert_message), CallbackQueryHandler(handle_preview_callback, pattern=r"^(pmv_up|pmv_dn|pedit|pdel|pins_b|pins_a):")],
            WAITING_BROADCAST:        [MessageHandler(all_msg, receive_broadcast)],
            WAITING_BROADCAST_CONFIRM:[MessageHandler(txt, receive_broadcast_confirm)],
            ADMIN_PREVIEW:            [MessageHandler(txt, admin_preview)],
            WAITING_RESET_CONFIRM:    [MessageHandler(txt, receive_reset_confirm)],
            ADMIN_BTN_REORDER:        [MessageHandler(txt, admin_btn_reorder)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("help", admin_help),
            CallbackQueryHandler(handle_preview_callback, pattern=r"^(pmv_up|pmv_dn|pedit|pdel|pins_b|pins_a):"),
        ],
        per_message=False,
    )

    return conv, handle_user_message, handle_preview_callback


# ──────────────────────────────────────────────
# RUN ALL BOTS
# ──────────────────────────────────────────────
async def start_bot(admin):
    token = admin.get("bot_token")
    uid = admin.get("uid")
    if not token:
        return None
    try:
        app = Application.builder().token(token).build()
        conv, user_handler, cb_handler = make_handlers(uid)
        app.add_handler(conv, group=0)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user_handler), group=1)
        app.add_handler(CallbackQueryHandler(cb_handler, pattern=r"^(pmv_up|pmv_dn|pedit|pdel|pins_b|pins_a):"), group=0)
        await app.initialize()
        await app.bot.set_my_commands([BotCommand("start", "Start the bot")])
        await app.start()
        await app.updater.start_polling()
        print(f"  ✅ @{admin.get('bot_username', uid)} started.")
        return app
    except Exception as e:
        print(f"  ❌ Failed for admin {uid}: {e}")
        return None


async def run_all_bots():
    running = {}
    print("🤖 Runner started — watching for new bots every 10s...\n")
    try:
        while True:
            admins = get_all_admins()
            for admin in admins:
                uid = admin.get("uid")
                token = admin.get("bot_token")
                if not uid or not token:
                    continue
                if uid not in running:
                    print(f"🆕 New bot detected: @{admin.get('bot_username', uid)}")
                    app = await start_bot(admin)
                    if app:
                        running[uid] = app
            if not running:
                print("⚠️  No bots running yet. Waiting for a token to be submitted...")
            await asyncio.sleep(10)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n🛑 Shutting down all bots...")
    finally:
        for app in running.values():
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass
        print("👋 All bots stopped.")


def main():
    print("🤖 Runner starting...")
    asyncio.run(run_all_bots())


if __name__ == "__main__":
    main()