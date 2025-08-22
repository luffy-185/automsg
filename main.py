import asyncio
import os
import json
import logging
import time
import threading
from collections import defaultdict
from typing import Dict, Set
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from flask import Flask
from keep_alive import keep_alive  # your keep_alive.py

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask for health checks (optional in render)
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot running!", 200

@app.route('/ping')
def ping():
    return "pong", 200

# Bot Class
class TelegramBot:
    def __init__(self):
        self.api_id = int(os.getenv("API_ID"))
        self.api_hash = os.getenv("API_HASH")
        self.session_string = os.getenv("SESSION_STRING")
        self.owner_id = int(os.getenv("OWNER_ID"))

        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

        # Bot state
        self.reply_settings: Dict[int, str] = {}
        self.user_message_count: Dict[int, int] = defaultdict(int)
        self.user_last_reply: Dict[int, float] = {}
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.spam_tasks: Dict[int, asyncio.Task] = {}  # key = chat_id
        self.spam_active_chats: Set[int] = set()
        self.start_time = time.time()
        self.bot_user_id = None

        # Load previous settings
        self.load_settings()

    # Save and load settings
    def save_settings(self):
        settings = {
            'reply_settings': self.reply_settings,
            'afk_group_active': self.afk_group_active,
            'afk_dm_active': self.afk_dm_active,
            'afk_message': self.afk_message
        }
        with open('bot_settings.json', 'w') as f:
            json.dump(settings, f)

    def load_settings(self):
        if os.path.exists('bot_settings.json'):
            with open('bot_settings.json', 'r') as f:
                settings = json.load(f)
                self.reply_settings = {int(k): v for k, v in settings.get('reply_settings', {}).items()}
                self.afk_group_active = settings.get('afk_group_active', False)
                self.afk_dm_active = settings.get('afk_dm_active', False)
                self.afk_message = settings.get('afk_message', "Currently offline")

    # Bot uptime
    def get_uptime(self):
        uptime_seconds = time.time() - self.start_time
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)
        if days > 0: return f"{days}d {hours}h {minutes}m {seconds}s"
        if hours > 0: return f"{hours}h {minutes}m {seconds}s"
        if minutes > 0: return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    # Start bot
    async def start(self):
        await self.client.start()
        self.bot_user_id = (await self.client.get_me()).id
        logger.info(f"Bot started! User ID: {self.bot_user_id}")
        self.client.add_event_handler(self.handle_message, events.NewMessage)
        self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))
        await self.client.run_until_disconnected()

    # Disable AFK when owner sends any message
    async def handle_outgoing(self, event):
        if event.sender_id == self.owner_id:
            if event.message.text != self.afk_message:
                if self.afk_group_active or self.afk_dm_active:
                    self.afk_group_active = False
                    self.afk_dm_active = False
                    self.save_settings()
                    logger.info("AFK disabled - owner sent a message")

    # Handle incoming messages
    async def handle_message(self, event):
        try:
            if event.is_private:
                await self.handle_dm(event)
            else:
                await self.handle_group(event)
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    # Direct Messages
    async def handle_dm(self, event):
        user_id = event.sender_id

        # Owner commands
        if user_id == self.owner_id and event.message.text.startswith('/'):
            await self.handle_command(event)
            return

        # Unauthorized commands ignored
        if event.message.text and event.message.text.startswith('/'):
            return

        # AFK DM
        if self.afk_dm_active:
            current_time = time.time()
            if user_id not in self.user_last_reply or (current_time - self.user_last_reply[user_id]) >= 1800:
                await event.reply(self.afk_message)
                self.user_last_reply[user_id] = current_time

        # setReplyFor in DM
        if user_id in self.reply_settings:
            if self.user_message_count[user_id] == 0:
                await event.reply(self.reply_settings[user_id])
                asyncio.create_task(self.reset_user_count(user_id))

    async def reset_user_count(self, user_id):
        await asyncio.sleep(1800)  # 30 min cooldown
        self.user_message_count[user_id] = 0

    # Group messages
    async def handle_group(self, event):
        chat_id = event.chat_id
        is_mentioned = False

        if event.message.mentioned:
            is_mentioned = True
        elif event.message.reply_to:
            try:
                replied_msg = await event.get_reply_message()
                if replied_msg and replied_msg.sender_id == self.bot_user_id:
                    is_mentioned = True
            except: pass

        if not is_mentioned:
            return

        if self.afk_group_active:
            await event.reply(self.afk_message)

        if chat_id in self.reply_settings:
            await event.reply(self.reply_settings[chat_id])

    # Command handler
    async def handle_command(self, event):
        text = event.message.text.strip()
        if text.startswith('/spam '):
            await self.handle_spam(event, text)
        elif text.startswith('/setReplyFor '):
            await self.handle_set_reply(event, text)
        elif text.startswith('/resetreplyfor '):
            await self.handle_reset_reply(event, text)
        elif text == '/clear_reply':
            await self.handle_clear_reply(event)
        elif text == '/listreply':
            await self.handle_list_reply(event)
        elif text.startswith('/afk_group '):
            await self.handle_afk_group(event, text)
        elif text == '/afk_group_off':
            await self.handle_afk_group_off(event)
        elif text.startswith('/afk_dm '):
            await self.handle_afk_dm(event, text)
        elif text == '/afk_dm_off':
            await self.handle_afk_dm_off(event)
        elif text.startswith('/afk '):
            await self.handle_afk(event, text)
        elif text == '/afk_off':
            await self.handle_afk_off(event)
        elif text == '/help':
            await self.handle_help(event)
        elif text == '/status':
            await self.handle_status(event)
        elif text == '/debug':
            await self.handle_debug(event)
        elif text == '/stop_spam':
            await self.handle_stop_spam(event)

    # ----------------- Commands Implementation -----------------
    async def handle_help(self, event):
        help_text = """
🤖 **Bot Commands:**

**Spam Commands:** 
• `/spam <msg> <delay>` - Spam only in this chat
• `/stop_spam` - Stop spam in this chat

**Reply Commands:** 
• `/setReplyFor <id> <msg>` - Set auto-reply for chat/user
• `/resetreplyfor <id>` - Remove reply for specific ID
• `/clear_reply` - Remove all replies
• `/listreply` - List all active replies

**AFK Commands:** 
• `/afk_group <msg>` - Enable AFK for groups (mentions only)
• `/afk_group_off` - Disable group AFK
• `/afk_dm <msg>` - Enable AFK for DMs
• `/afk_dm_off` - Disable DM AFK
• `/afk <msg>` - Enable both group & DM AFK
• `/afk_off` - Disable all AFK

**Info Commands:** 
• `/help` - Show this help
• `/status` - Show bot status and uptime
• `/debug` - Show debug information
"""
        await event.reply(help_text)

    async def handle_status(self, event):
        uptime = self.get_uptime()
        spam_count = 1 if event.chat_id in self.spam_tasks else 0
        reply_count = len(self.reply_settings)
        status_text = f"""📊 **Bot Status:**
⏱️ Uptime: {uptime}
🔄 Spam: {'🟢 Active' if spam_count>0 else '🔴 Inactive'}
💬 Auto-Reply: {reply_count} active
😴 AFK: Group: {'🟢 On' if self.afk_group_active else '🔴 Off'}, DM: {'🟢 On' if self.afk_dm_active else '🔴 Off'}
🤖 Bot ID: {self.bot_user_id}"""
        await event.reply(status_text)

    async def handle_debug(self, event):
        debug_text = f"""
🔍 Debug Info:
Your ID: {event.sender_id}
Owner ID: {self.owner_id}
Match Owner: {'✅ YES' if event.sender_id==self.owner_id else '❌ NO'}
Bot ID: {self.bot_user_id}
Chat Type: {'DM' if event.is_private else 'Group'}
Command: {event.message.text}
"""
        await event.reply(debug_text)

    # ----------------- Spam -----------------
    async def handle_spam(self, event, text):
        try:
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /spam <message> <delay>")
                return
            msg = parts[1]
            delay = int(parts[2])
            chat_id = event.chat_id

            # Stop existing spam in this chat
            await self.stop_spam(chat_id)

            async def spam_loop():
                while True:
                    await self.client.send_message(chat_id, msg)
                    await asyncio.sleep(delay)

            task = asyncio.create_task(spam_loop())
            self.spam_tasks[chat_id] = task
            self.spam_active_chats.add(chat_id)
            await event.reply(f"✅ Started spam in this chat: '{msg}' every {delay}s")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")

    async def handle_stop_spam(self, event):
        chat_id = event.chat_id
        count = await self.stop_spam(chat_id)
        await event.reply(f"✅ Stopped spam in this chat")

    async def stop_spam(self, chat_id):
        if chat_id in self.spam_tasks:
            task = self.spam_tasks[chat_id]
            task.cancel()
            del self.spam_tasks[chat_id]
            self.spam_active_chats.discard(chat_id)
            return 1
        return 0

    # ----------------- Replies -----------------
    async def handle_set_reply(self, event, text):
        parts = text.split(' ', 2)
        if len(parts)<3:
            await event.reply("Usage: /setReplyFor <id> <message>")
            return
        target_id = int(parts[1])
        msg = parts[2]
        self.reply_settings[target_id] = msg
        self.save_settings()
        await event.reply(f"✅ Reply set for ID {target_id}: {msg}")

    async def handle_reset_reply(self, event, text):
        parts = text.split(' ')
        if len(parts)<2:
            await event.reply("Usage: /resetreplyfor <id>")
            return
        target_id = int(parts[1])
        if target_id in self.reply_settings:
            del self.reply_settings[target_id]
            self.save_settings()
            await event.reply(f"✅ Reply removed for ID {target_id}")
        else:
            await event.reply("❌ No reply set for this ID")

    async def handle_clear_reply(self, event):
        self.reply_settings.clear()
        self.save_settings()
        await event.reply("✅ All replies cleared")

    async def handle_list_reply(self, event):
        if not self.reply_settings:
            await event.reply("❌ No active replies")
            return
        lines = []
        for k,v in self.reply_settings.items():
            lines.append(f"{k}: {v}")
        await event.reply("📋 Active Replies:\n" + "\n".join(lines))

    # ----------------- AFK -----------------
    async def handle_afk_group(self, event, text):
        self.afk_group_active = True
        parts = text.split(' ', 1)
        if len(parts)>1: self.afk_message = parts[1]
        self.save_settings()
        await event.reply(f"✅ AFK group enabled: {self.afk_message}")

    async def handle_afk_group_off(self, event):
        self.afk_group_active = False
        self.save_settings()
        await event.reply("✅ AFK group disabled")

    async def handle_afk_dm(self, event, text):
        self.afk_dm_active = True
        parts = text.split(' ', 1)
        if len(parts)>1: self.afk_message = parts[1]
        self.save_settings()
        await event.reply(f"✅ AFK DM enabled: {self.afk_message}")

    async def handle_afk_dm_off(self, event):
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ AFK DM disabled")

    async def handle_afk(self, event, text):
        self.afk_group_active = True
        self.afk_dm_active = True
        parts = text.split(' ', 1)
        if len(parts)>1: self.afk_message = parts[1]
        else: self.afk_message = "Currently offline"
        self.save_settings()
        await event.reply(f"✅ AFK enabled for both: {self.afk_message}")

    async def handle_afk_off(self, event):
        self.afk_group_active = False
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ All AFK disabled")


# Run Flask in thread
def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

# Main async
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    bot = TelegramBot()
    await bot.start()

# Entry point
if __name__ == "__main__":
    keep_alive()  # optional, your keep_alive.py
    asyncio.run(main())
