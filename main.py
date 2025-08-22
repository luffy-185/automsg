import asyncio import time import os import json import logging import threading from collections import defaultdict from typing import Dict, Set

from telethon import TelegramClient, events from telethon.sessions import StringSession from flask import Flask

Setup logging

logging.basicConfig(level=logging.INFO) logger = logging.getLogger(name)

Flask app for health checks

app = Flask(name)

@app.route("/") def health_check(): return "Bot is running!", 200

@app.route("/ping") def ping(): return "pong", 200

class TelegramBot: def init(self): # Environment vars self.api_id = int(os.getenv("API_ID")) self.api_hash = os.getenv("API_HASH") self.session_string = os.getenv("SESSION_STRING") self.owner_id = int(os.getenv("OWNER_ID"))

# Telethon client
    self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

    # Bot state
    self.reply_settings: Dict[int, str] = {}
    self.user_message_count: Dict[int, int] = defaultdict(int)
    self.user_last_reply: Dict[int, float] = {}
    self.afk_group_active = False
    self.afk_dm_active = False
    self.afk_message = "Currently offline"
    self.spam_tasks: Set[asyncio.Task] = set()
    self.bot_user_id = None
    self.start_time = time.time()
    self.spam_active = False

    # Load settings
    self.load_settings()

def save_settings(self):
    settings = {
        "reply_settings": self.reply_settings,
        "afk_group_active": self.afk_group_active,
        "afk_dm_active": self.afk_dm_active,
        "afk_message": self.afk_message,
        "spam_active": self.spam_active,
    }
    try:
        with open("bot_settings.json", "w") as f:
            json.dump(settings, f)
    except Exception as e:
        logger.error(f"Error saving settings: {e}")

def load_settings(self):
    try:
        if os.path.exists("bot_settings.json"):
            with open("bot_settings.json", "r") as f:
                settings = json.load(f)
                self.reply_settings = {int(k): v for k, v in settings.get("reply_settings", {}).items()}
                self.afk_group_active = settings.get("afk_group_active", False)
                self.afk_dm_active = settings.get("afk_dm_active", False)
                self.afk_message = settings.get("afk_message", "Currently offline")
                self.spam_active = settings.get("spam_active", False)
    except Exception as e:
        logger.error(f"Error loading settings: {e}")

def get_uptime(self):
    uptime_seconds = time.time() - self.start_time
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    seconds = int(uptime_seconds % 60)

    if days > 0:
        return f"{days}d {hours}h {minutes}m {seconds}s"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

async def start(self):
    try:
        await self.client.start()
        self.bot_user_id = (await self.client.get_me()).id
        logger.info(f"Bot started! Session User ID: {self.bot_user_id}")
        logger.info(f"Owner ID set to: {self.owner_id}")

        if self.bot_user_id != self.owner_id:
            logger.warning(f"Owner ID ({self.owner_id}) doesn't match session user ({self.bot_user_id})")

        # Register handlers
        self.client.add_event_handler(self.handle_message, events.NewMessage)
        self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))

        logger.info("Bot is running...")
        await self.client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        await asyncio.sleep(10)
        await self.start()

async def handle_outgoing(self, event):
    if event.sender_id == self.owner_id and event.message.text != self.afk_message:
        if self.afk_group_active or self.afk_dm_active:
            self.afk_group_active = False
            self.afk_dm_active = False
            self.save_settings()
            logger.info("AFK disabled - owner sent a message")

async def handle_message(self, event):
    try:
        if event.is_private:
            await self.handle_dm(event)
        else:
            await self.handle_group_message(event)
    except Exception as e:
        logger.error(f"Error handling message: {e}")

async def handle_dm(self, event):
    user_id = event.sender_id

    if user_id == self.owner_id:
        await self.handle_command(event)
        return

    if event.message.text and event.message.text.startswith("/"):
        return  # ignore non-owner commands

    if self.afk_dm_active:
        current_time = time.time()
        if user_id not in self.user_last_reply or (current_time - self.user_last_reply[user_id]) >= 1800:
            await event.reply(self.afk_message)
            self.user_last_reply[user_id] = current_time

    if user_id in self.reply_settings:
        self.user_message_count[user_id] += 1
        if self.user_message_count[user_id] == 1:
            await event.reply(self.reply_settings[user_id])
            asyncio.create_task(self.reset_user_count(user_id))

async def reset_user_count(self, user_id):
    await asyncio.sleep(1800)
    self.user_message_count[user_id] = 0

async def handle_group_message(self, event):
    chat_id = event.chat_id
    is_mentioned = event.message.mentioned

    if event.is_reply:
        replied_msg = await event.get_reply_message()
        if replied_msg and replied_msg.sender_id == self.bot_user_id:
            is_mentioned = True

    if not is_mentioned:
        return

    if self.afk_group_active:
        await event.reply(self.afk_message)

    if chat_id in self.reply_settings:
        await event.reply(self.reply_settings[chat_id])

async def handle_command(self, event):
    text = event.message.text.strip()

    if text.startswith("/spam"):
        await self.handle_spam_command(event, text)
    elif text == "/stop_spam":
        await self.handle_stop_spam_command(event)
    elif text.startswith("/setReplyFor"):
        await self.handle_set_reply_command(event, text)
    elif text.startswith("/resetreplyfor"):
        await self.handle_reset_reply_command(event, text)
    elif text == "/clear_reply":
        await self.handle_clear_reply_command(event)
    elif text == "/listreply":
        await self.handle_list_reply_command(event)
    elif text.startswith("/afk_group"):
        await self.handle_afk_group_command(event, text)
    elif text == "/afk_group_off":
        await self.handle_afk_group_off_command(event)
    elif text.startswith("/afk_dm"):
        await self.handle_afk_dm_command(event, text)
    elif text == "/afk_dm_off":
        await self.handle_afk_dm_off_command(event)
    elif text.startswith("/afk"):
        await self.handle_afk_command(event, text)
    elif text == "/afk_off":
        await self.handle_afk_off_command(event)
    elif text == "/help":
        await self.handle_help_command(event)
    elif text == "/status":
        await self.handle_status_command(event)
    elif text == "/debug":
        await self.handle_debug_command(event)

async def handle_debug_command(self, event):
    debug_info = (
        f"🔍 Debug Info:\n"
        f"Your User ID: {event.sender_id}\n"
        f"Set Owner ID: {self.owner_id}\n"
        f"Bot User ID: {self.bot_user_id}\n"
        f"Match: {'✅ YES' if event.sender_id == self.owner_id else '❌ NO'}\n"
        f"Message Type: {'DM' if event.is_private else 'Group'}\n"
        f"Command: {event.message.text}"
    )
    await event.reply(debug_info)

async def handle_help_command(self, event):
    help_text = (
        "🤖 **Bot Commands:**\n\n"
        "**Spam:**\n"
        "/spam <count> <delay> <message> - Spam in current chat\n"
        "/stop_spam - Stop spam\n\n"
        "**Replies:**\n"
        "/setReplyFor <id> <msg>\n"
        "/resetreplyfor <id>\n"
        "/clear_reply\n"
        "/listreply\n\n"
        "**AFK:**\n"
        "/afk_group <msg> | /afk_group_off\n"
        "/afk_dm <msg> | /afk_dm_off\n"
        "/afk <msg> | /afk_off\n\n"
        "**Info:**\n"
        "/help | /status | /debug"
    )
    await event.reply(help_text)

async def handle_status_command(self, event):
    uptime = self.get_uptime()
    active_spam_count = len([t for t in self.spam_tasks if not t.done()])
    reply_count = len(self.reply_settings)

    status_text = (
        f"📊 Bot Status:\n\n"
        f"⏱️ Uptime: {uptime}\n"
        f"🔄 Spam: {'🟢 Active' if self.spam_active and active_spam_count else '🔴 Inactive'} ({active_spam_count} tasks)\n"
        f"😴 AFK: Group: {'🟢 On' if self.afk_group_active else '🔴 Off'}, DM: {'🟢 On' if self.afk_dm_active else '🔴 Off'}\n"
        f"💬 Replies: {reply_count}\n"
        f"🤖 Bot ID: {self.bot_user_id}"
    )
    await event.reply(status_text)

async def handle_spam_command(self, event, text):
    try:
        parts = text.split(" ", 3)
        if len(parts) < 4:
            await event.reply("Usage: /spam <count> <delay> <message>")
            return

        count = int(parts[1])
        delay = int(parts[2])
        message = parts[3]

        async def spam_loop():
            for _ in range(count):
                await event.respond(message)
                await asyncio.sleep(delay)

        task = asyncio.create_task(spam_loop())
        self.spam_tasks.add(task)
        self.spam_active = True
        self.save_settings()

        await event.reply(f"✅ Spamming '{message}' {count} times with {delay}s delay in this chat")
    except Exception as e:
        await event.reply(f"❌ Error: {e}")

async def handle_stop_spam_command(self, event):
    stopped = await self.stop_all_spam_tasks()
    self.spam_active = False
    self.save_settings()
    await event.reply(f"✅ Stopped {stopped} spam tasks")

async def stop_all_spam_tasks(self):
    stopped_count = 0
    for task in self.spam_tasks:
        if not task.done():
            task.cancel()
            stopped_count += 1
    self.spam_tasks.clear()
    return stopped_count

# Reply + AFK command handlers (same as before, cleaned)...

def run_flask(): port = int(os.environ.get("PORT", 5000)) app.run(host="0.0.0.0", port=port, debug=False)

async def main(): flask_thread = threading.Thread(target=run_flask, daemon=True) flask_thread.start()

bot = TelegramBot()
while True:
    try:
        await bot.start()
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        logger.info("Restarting in 10 seconds...")
        await asyncio.sleep(10)

if name == "main": asyncio.run(main())


