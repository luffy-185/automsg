import asyncio
import time
import os
import json
import logging
import threading
from collections import defaultdict
from typing import Dict, Set
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from flask import Flask
from keep_alive import keep_alive  # your keep_alive.py in repo

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app for health checks (required for Render)
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

@app.route('/ping')
def ping():
    return "pong", 200

class TelegramBot:
    def __init__(self):
        # Environment variables
        self.api_id = int(os.getenv('API_ID'))
        self.api_hash = os.getenv('API_HASH')
        self.session_string = os.getenv('SESSION_STRING')
        self.owner_id = int(os.getenv('OWNER_ID'))

        # Telegram client
        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

        # Bot state
        self.reply_settings: Dict[int, str] = {}
        self.user_message_count: Dict[int, int] = defaultdict(int)
        self.user_last_reply: Dict[int, float] = {}
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.spam_tasks: Dict[int, asyncio.Task] = {}  # per chat
        self.spam_active: Set[int] = set()
        self.bot_user_id = None
        self.start_time = time.time()

        # Load settings
        self.load_settings()

    def save_settings(self):
        settings = {
            'reply_settings': self.reply_settings,
            'afk_group_active': self.afk_group_active,
            'afk_dm_active': self.afk_dm_active,
            'afk_message': self.afk_message,
            'spam_active': list(self.spam_active)
        }
        try:
            with open('bot_settings.json', 'w') as f:
                json.dump(settings, f)
        except Exception as e:
            logger.error(f"Error saving settings: {e}")

    def load_settings(self):
        try:
            if os.path.exists('bot_settings.json'):
                with open('bot_settings.json', 'r') as f:
                    settings = json.load(f)
                    self.reply_settings = {int(k): v for k, v in settings.get('reply_settings', {}).items()}
                    self.afk_group_active = settings.get('afk_group_active', False)
                    self.afk_dm_active = settings.get('afk_dm_active', False)
                    self.afk_message = settings.get('afk_message', "Currently offline")
                    self.spam_active = set(settings.get('spam_active', []))
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
        await self.client.start()
        me = await self.client.get_me()
        self.bot_user_id = me.id
        logger.info(f"Bot started! Session User ID: {self.bot_user_id}")
        logger.info(f"Owner ID set to: {self.owner_id}")

        self.client.add_event_handler(self.handle_message, events.NewMessage)
        self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))

        await self.client.run_until_disconnected()

    async def handle_outgoing(self, event):
        try:
            if event.sender_id == self.owner_id and event.message.text != self.afk_message:
                if self.afk_group_active or self.afk_dm_active:
                    self.afk_group_active = False
                    self.afk_dm_active = False
                    self.save_settings()
                    logger.info("AFK disabled - owner sent a message")
        except Exception as e:
            logger.error(f"Error handling outgoing: {e}")

    async def handle_message(self, event):
        try:
            if event.is_private:
                await self.handle_dm(event)
            else:
                await self.handle_group(event)
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def handle_dm(self, event):
        user_id = event.sender_id
        text = event.message.text or ""
        try:
            if user_id == self.owner_id:
                await self.handle_command(event)
                return
            # Ignore commands from non-owner
            if text.startswith('/'):
                return
            now = time.time()
            # AFK DM (5 min cooldown per user)
            if self.afk_dm_active:
                if user_id not in self.user_last_reply or now - self.user_last_reply[user_id] > 300:
                    await event.reply(self.afk_message)
                    self.user_last_reply[user_id] = now
            # setReplyFor DM (30 min cooldown)
            if user_id in self.reply_settings:
                if user_id not in self.user_last_reply or now - self.user_last_reply[user_id] > 1800:
                    await event.reply(self.reply_settings[user_id])
                    self.user_last_reply[user_id] = now
        except Exception as e:
            logger.error(f"Error DM: {e}")

    async def handle_group(self, event):
        chat_id = event.chat_id
        text = event.message.text or ""
        try:
            # Only reply if mentioned or replied to bot
            mentioned = event.message.mentioned
            replied = event.message.is_reply and (await event.get_reply_message()).sender_id == self.bot_user_id
            if not (mentioned or replied):
                return
            # AFK group
            if self.afk_group_active:
                await event.reply(self.afk_message)
            # setReplyFor for group
            if chat_id in self.reply_settings:
                await event.reply(self.reply_settings[chat_id])
        except Exception as e:
            logger.error(f"Error group: {e}")

    async def handle_command(self, event):
        text = event.message.text.strip()
        try:
            if text.startswith('/spam '):
                await self.cmd_spam(event, text)
            elif text.startswith('/setReplyFor '):
                await self.cmd_set_reply(event, text)
            elif text.startswith('/resetreplyfor '):
                await self.cmd_reset_reply(event, text)
            elif text == '/clear_reply':
                self.reply_settings.clear()
                self.save_settings()
                await event.reply("✅ All replies cleared")
            elif text == '/listreply':
                await self.cmd_list_reply(event)
            elif text.startswith('/afk_group '):
                self.afk_group_active = True
                self.afk_message = text.split(' ', 1)[1] if ' ' in text else "Currently offline"
                self.save_settings()
                await event.reply(f"✅ AFK group activated: {self.afk_message}")
            elif text == '/afk_group_off':
                self.afk_group_active = False
                self.save_settings()
                await event.reply("✅ AFK group deactivated")
            elif text.startswith('/afk_dm '):
                self.afk_dm_active = True
                self.afk_message = text.split(' ', 1)[1] if ' ' in text else "Currently offline"
                self.save_settings()
                await event.reply(f"✅ AFK DM activated: {self.afk_message}")
            elif text == '/afk_dm_off':
                self.afk_dm_active = False
                self.save_settings()
                await event.reply("✅ AFK DM deactivated")
            elif text.startswith('/afk '):
                self.afk_group_active = True
                self.afk_dm_active = True
                self.afk_message = text.split(' ', 1)[1] if ' ' in text else "Currently offline"
                self.save_settings()
                await event.reply(f"✅ AFK activated for groups & DMs: {self.afk_message}")
            elif text == '/afk_off':
                self.afk_group_active = False
                self.afk_dm_active = False
                self.save_settings()
                await event.reply("✅ All AFK modes deactivated")
            elif text == '/help':
                await event.reply("Commands: /spam /setReplyFor /resetreplyfor /clear_reply /listreply /afk_group /afk_dm /afk /afk_off /afk_group_off /afk_dm_off")
            elif text == '/status':
                uptime = self.get_uptime()
                await event.reply(f"Bot uptime: {uptime}")
        except Exception as e:
            logger.error(f"Command error: {e}")
            await event.reply(f"❌ Error: {e}")

    async def cmd_set_reply(self, event, text):
        parts = text.split(' ', 2)
        if len(parts) < 3:
            await event.reply("Usage: /setReplyFor <id> <msg>")
            return
        target_id = int(parts[1])
        self.reply_settings[target_id] = parts[2]
        self.save_settings()
        await event.reply(f"✅ Reply set for {target_id}")

    async def cmd_reset_reply(self, event, text):
        parts = text.split(' ')
        if len(parts) < 2:
            await event.reply("Usage: /resetreplyfor <id>")
            return
        target_id = int(parts[1])
        if target_id in self.reply_settings:
            del self.reply_settings[target_id]
            self.save_settings()
            await event.reply(f"✅ Reply removed for {target_id}")
        else:
            await event.reply(f"❌ No reply set for {target_id}")

    async def cmd_list_reply(self, event):
        if not self.reply_settings:
            await event.reply("❌ No active replies")
            return
        lines = [f"{k}: {v}" for k, v in self.reply_settings.items()]
        await event.reply("📋 Active replies:\n" + "\n".join(lines))

    async def cmd_spam(self, event, text):
        parts = text.split(' ', 2)
        if len(parts) < 3:
            await event.reply("Usage: /spam <msg> <delay_sec>")
            return
        msg = parts[1]
        delay = int(parts[2])
        chat = event.chat_id
        # Cancel previous spam in this chat
        if chat in self.spam_tasks:
            self.spam_tasks[chat].cancel()
        async def spam_loop():
            while True:
                await self.client.send_message(chat, msg)
                await asyncio.sleep(delay)
        task = asyncio.create_task(spam_loop())
        self.spam_tasks[chat] = task
        self.spam_active.add(chat)
        self.save_settings()
        await event.reply(f"✅ Started spamming in chat {chat} every {delay}s")

async def main():
    keep_alive()  # Start Flask server
    bot = TelegramBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
