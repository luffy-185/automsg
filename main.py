import asyncio
import time
import os
import json
import logging
from collections import defaultdict
from typing import Dict, Set
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from flask import Flask
import threading
from keep_alive import keep_alive  # make sure keep_alive.py is in repo

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
        self.api_id = int(os.getenv('API_ID'))
        self.api_hash = os.getenv('API_HASH')
        self.session_string = os.getenv('SESSION_STRING')
        self.owner_id = int(os.getenv('OWNER_ID'))

        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

        self.reply_settings: Dict[int, str] = {}   # group or user -> message
        self.user_last_reply: Dict[int, float] = {} # DM cooldowns
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.spam_tasks: Dict[int, asyncio.Task] = {} # chat_id -> task
        
        # Track AFK disable per chat
        self.afk_disabled_chats: Set[int] = set()  # chats where AFK is disabled

        self.bot_user_id = None
        self.start_time = time.time()

        self.load_settings()

    def save_settings(self):
        settings = {
            'reply_settings': self.reply_settings,
            'afk_group_active': self.afk_group_active,
            'afk_dm_active': self.afk_dm_active,
            'afk_message': self.afk_message,
            'afk_disabled_chats': list(self.afk_disabled_chats)
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
                    self.afk_disabled_chats = set(settings.get('afk_disabled_chats', []))
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
        self.bot_user_id = (await self.client.get_me()).id
        logger.info(f"Bot started! ID: {self.bot_user_id}")

        self.client.add_event_handler(self.handle_message, events.NewMessage)
        self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))

        await self.client.run_until_disconnected()

    async def handle_outgoing(self, event):
        if event.sender_id == self.owner_id:
            message_text = event.message.text or ""
            chat_id = event.chat_id if hasattr(event, 'chat_id') else event.peer_id.user_id
            
            # If owner sends any message that's not the AFK message
            if message_text != self.afk_message:
                # Remove setReplyFor for this specific chat/user
                if chat_id in self.reply_settings:
                    del self.reply_settings[chat_id]
                    self.save_settings()
                    logger.info(f"Removed setReplyFor for chat {chat_id} - owner sent message")
                
                # Disable AFK for this specific chat only
                if event.is_private:
                    if self.afk_dm_active:
                        self.afk_disabled_chats.add(chat_id)
                        self.save_settings()
                        logger.info(f"AFK disabled for DM {chat_id} - owner sent message")
                else:
                    if self.afk_group_active:
                        self.afk_disabled_chats.add(chat_id)
                        self.save_settings()
                        logger.info(f"AFK disabled for group {chat_id} - owner sent message")

    async def handle_message(self, event):
        try:
            # Commands - Only respond if owner, otherwise ignore completely
            if event.message.text and event.message.text.startswith('/'):
                if event.sender_id == self.owner_id:
                    await self.handle_command(event)
                return  # Don't process further if it's a command

            if event.is_private:
                await self.handle_dm(event)
            else:
                await self.handle_group(event)
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def handle_dm(self, event):
        user_id = event.sender_id
        now = time.time()

        # Skip if it's the owner
        if user_id == self.owner_id:
            return

        # AFK DM - Reply to ALL DMs when active (unless disabled for this chat)
        if self.afk_dm_active and user_id not in self.afk_disabled_chats:
            if user_id not in self.user_last_reply or now - self.user_last_reply[user_id] >= 300:
                await event.reply(self.afk_message)
                self.user_last_reply[user_id] = now
                return  # Don't process setReplyFor if AFK replied

        # setReplyFor DM cooldown 30 min (only if AFK didn't reply)
        if user_id in self.reply_settings:
            if user_id not in self.user_last_reply or now - self.user_last_reply[user_id] >= 1800:
                await event.reply(self.reply_settings[user_id])
                self.user_last_reply[user_id] = now

    async def handle_group(self, event):
        chat_id = event.chat_id
        
        # Check if mentioned
        is_mentioned = False
        if event.message.mentioned:
            is_mentioned = True
        elif event.message.reply_to_msg_id:
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.sender_id == self.bot_user_id:
                    is_mentioned = True
            except:
                pass

        if not is_mentioned:
            return

        # AFK group - Reply to ALL mentions when active (unless disabled for this chat)
        if self.afk_group_active and chat_id not in self.afk_disabled_chats:
            await event.reply(self.afk_message)
            return  # Don't process setReplyFor if AFK replied

        # setReplyFor group (only if AFK didn't reply)
        if chat_id in self.reply_settings:
            await event.reply(self.reply_settings[chat_id])

    async def handle_command(self, event):
        text = event.message.text.strip()
        chat_id = event.chat_id if hasattr(event, 'chat_id') else event.peer_id.user_id

        if text.startswith('/spam '):
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /spam <message> <delay>")
                return
            try:
                msg = parts[1]
                delay = int(parts[2])
            except:
                await event.reply("❌ Invalid parameters")
                return
            await self.start_spam(chat_id, msg, delay)
            await event.reply(f"✅ Started spam in this chat every {delay}s")

        elif text.startswith('/stop_spam'):
            if chat_id in self.spam_tasks:
                self.spam_tasks[chat_id].cancel()
                del self.spam_tasks[chat_id]
                await event.reply("✅ Stopped spam in this chat")
            else:
                await event.reply("❌ No spam running in this chat")

        elif text == '/stop_all_spam':
            stopped = await self.stop_all_spam()
            await event.reply(f"✅ Stopped {stopped} spam tasks")

        elif text.startswith('/setReplyFor '):
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /setReplyFor <id> <message>")
                return
            try:
                target_id = int(parts[1])
            except:
                await event.reply("❌ Invalid ID")
                return
            self.reply_settings[target_id] = parts[2]
            self.save_settings()
            await event.reply(f"✅ Reply set for ID {target_id}")

        elif text.startswith('/resetreplyfor '):
            parts = text.split(' ')
            if len(parts) < 2:
                await event.reply("Usage: /resetreplyfor <id>")
                return
            try:
                target_id = int(parts[1])
            except:
                await event.reply("❌ Invalid ID")
                return
            if target_id in self.reply_settings:
                del self.reply_settings[target_id]
                self.save_settings()
                await event.reply(f"✅ Reply removed for ID {target_id}")
            else:
                await event.reply(f"❌ No reply found for ID {target_id}")

        elif text == '/clear_reply':
            self.reply_settings.clear()
            self.save_settings()
            await event.reply("✅ All replies cleared")

        elif text == '/listreply':
            if not self.reply_settings:
                await event.reply("❌ No active replies")
                return
            msg = "\n".join([f"{k}: {v}" for k, v in self.reply_settings.items()])
            await event.reply("📋 Active Replies:\n" + msg)

        elif text.startswith('/afk_group '):
            self.afk_message = text[len('/afk_group '):].strip()
            self.afk_group_active = True
            self.afk_disabled_chats.clear()  # Reset disabled chats
            self.save_settings()
            await event.reply(f"✅ AFK group activated: {self.afk_message}")

        elif text == '/afk_group_off':
            self.afk_group_active = False
            self.afk_disabled_chats.clear()
            self.save_settings()
            await event.reply("✅ AFK group deactivated")

        elif text.startswith('/afk_dm '):
            self.afk_message = text[len('/afk_dm '):].strip()
            self.afk_dm_active = True
            self.afk_disabled_chats.clear()  # Reset disabled chats
            self.save_settings()
            await event.reply(f"✅ AFK DM activated: {self.afk_message}")

        elif text == '/afk_dm_off':
            self.afk_dm_active = False
            self.afk_disabled_chats.clear()
            self.save_settings()
            await event.reply("✅ AFK DM deactivated")

        elif text.startswith('/afk '):
            self.afk_message = text[len('/afk '):].strip()
            self.afk_group_active = True
            self.afk_dm_active = True
            self.afk_disabled_chats.clear()  # Reset disabled chats
            self.save_settings()
            await event.reply(f"✅ AFK activated for both groups and DMs: {self.afk_message}")

        elif text == '/afk_off':
            self.afk_group_active = False
            self.afk_dm_active = False
            self.afk_disabled_chats.clear()
            self.save_settings()
            await event.reply("✅ AFK all deactivated")

        elif text == '/status':
            uptime = self.get_uptime()
            spam_count = len(self.spam_tasks)
            disabled_count = len(self.afk_disabled_chats)
            await event.reply(f"⏱ Uptime: {uptime}\n🤖 Bot ID: {self.bot_user_id}\n📊 AFK Group: {self.afk_group_active}\n📊 AFK DM: {self.afk_dm_active}\n📋 Active Replies: {len(self.reply_settings)}\n🚀 Spam Tasks: {spam_count}\n🚫 AFK Disabled Chats: {disabled_count}")

        elif text == '/help':
            help_text = """🤖 Bot Commands (Owner Only):

**Spam**
• /spam <msg> <delay> - spam in current chat
• /stop_spam - stop spam in current chat
• /stop_all_spam - stop all spam tasks

**Replies**
• /setReplyFor <id> <msg> - set auto reply
• /resetreplyfor <id> - remove auto reply
• /clear_reply - clear all replies
• /listreply - list active replies

**AFK**
• /afk_group <msg> - enable group AFK (all mentions)
• /afk_group_off - disable group AFK
• /afk_dm <msg> - enable DM AFK (all DMs, 5min cooldown)
• /afk_dm_off - disable DM AFK
• /afk <msg> - enable both AFK
• /afk_off - disable all AFK

**Info**
• /status - show bot status
• /help - show this help

**Note**: AFK auto-disables per chat when you send messages there. setReplyFor also resets when you message that chat."""
            await event.reply(help_text)

    async def start_spam(self, chat_id: int, msg: str, delay: int):
        if chat_id in self.spam_tasks:
            self.spam_tasks[chat_id].cancel()
        async def spam_loop():
            try:
                while True:
                    await self.client.send_message(chat_id, msg)
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                pass
        task = asyncio.create_task(spam_loop())
        self.spam_tasks[chat_id] = task

    async def stop_all_spam(self):
        stopped = len(self.spam_tasks)
        for task in self.spam_tasks.values():
            task.cancel()
        self.spam_tasks.clear()
        return stopped

# Flask server in thread
def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    bot = TelegramBot()
    while True:
        try:
            await bot.start()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
