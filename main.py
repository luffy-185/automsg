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
from keep_alive import keep_alive  # your existing keep_alive file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask for health check
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

@app.route('/ping')
def ping():
    return "pong", 200

class TelegramBot:
    def __init__(self):
        self.api_id = int(os.getenv("API_ID"))
        self.api_hash = os.getenv("API_HASH")
        self.session_string = os.getenv("SESSION_STRING")
        self.owner_id = int(os.getenv("OWNER_ID"))
        
        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
        
        # Bot state
        self.reply_settings: Dict[int, str] = {}  # user_id/group_id -> message
        self.user_message_count: Dict[int, int] = defaultdict(int)
        self.user_last_reply: Dict[int, float] = {}  # per DM cooldown
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.afk_dm_cooldown = 300  # 5 min
        self.spam_tasks: Dict[int, asyncio.Task] = {}  # chat_id -> task
        self.bot_user_id = None
        self.start_time = time.time()
        
        self.load_settings()
    
    # ----------------- SETTINGS -----------------
    def save_settings(self):
        settings = {
            'reply_settings': self.reply_settings,
            'afk_group_active': self.afk_group_active,
            'afk_dm_active': self.afk_dm_active,
            'afk_message': self.afk_message,
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
    
    # ----------------- BOT START -----------------
    async def start(self):
        await self.client.start()
        self.bot_user_id = (await self.client.get_me()).id
        logger.info(f"Bot started! Session User ID: {self.bot_user_id}")
        logger.info(f"Owner ID set to: {self.owner_id}")
        
        # Event handlers
        self.client.add_event_handler(self.handle_message, events.NewMessage)
        self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))
        
        await self.client.run_until_disconnected()
    
    # ----------------- OUTGOING -----------------
    async def handle_outgoing(self, event):
        try:
            if event.sender_id == self.owner_id and event.message.text != self.afk_message:
                if self.afk_group_active or self.afk_dm_active:
                    self.afk_group_active = False
                    self.afk_dm_active = False
                    self.save_settings()
                    logger.info("AFK disabled - owner sent a message")
        except Exception as e:
            logger.error(f"Error handling outgoing message: {e}")
    
    # ----------------- INCOMING -----------------
    async def handle_message(self, event):
        try:
            if event.is_private:
                await self.handle_dm(event)
            else:
                await self.handle_group_message(event)
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    # ----------------- DM HANDLER -----------------
    async def handle_dm(self, event):
        try:
            user_id = event.sender_id
            if user_id == self.owner_id:
                await self.handle_command(event)
                return
            if event.message.text and event.message.text.startswith('/'):
                return  # ignore commands from others
            
            # AFK DM (5 min cooldown)
            if self.afk_dm_active:
                last = self.user_last_reply.get(user_id, 0)
                if time.time() - last >= self.afk_dm_cooldown:
                    await event.reply(self.afk_message)
                    self.user_last_reply[user_id] = time.time()
            
            # setReplyFor DM (30 min cooldown)
            if user_id in self.reply_settings:
                last = self.user_last_reply.get(user_id, 0)
                if time.time() - last >= 1800:
                    await event.reply(self.reply_settings[user_id])
                    self.user_last_reply[user_id] = time.time()
        except Exception as e:
            logger.error(f"Error handling DM: {e}")
    
    # ----------------- GROUP HANDLER -----------------
    async def handle_group_message(self, event):
        try:
            chat_id = event.chat_id
            is_mentioned = False
            if event.message.mentioned:
                is_mentioned = True
            if event.message.reply_to:
                replied_msg = await event.get_reply_message()
                if replied_msg and replied_msg.sender_id == self.bot_user_id:
                    is_mentioned = True
            if not is_mentioned:
                return
            if self.afk_group_active:
                await event.reply(self.afk_message)
            if chat_id in self.reply_settings:
                await event.reply(self.reply_settings[chat_id])
        except Exception as e:
            logger.error(f"Error handling group message: {e}")
    
    # ----------------- COMMANDS -----------------
    async def handle_command(self, event):
        try:
            text = event.message.text.strip()
            if event.sender_id != self.owner_id:
                return  # only owner commands
            if text.startswith('/spam '):
                await self.handle_spam_command(event, text)
            elif text.startswith('/stop_spam '):
                await self.handle_stop_spam_command(event, text)
            elif text.startswith('/setReplyFor '):
                await self.handle_set_reply_command(event, text)
            elif text.startswith('/resetreplyfor '):
                await self.handle_reset_reply_command(event, text)
            elif text == '/clear_reply':
                await self.handle_clear_reply_command(event)
            elif text == '/listreply':
                await self.handle_list_reply_command(event)
            elif text.startswith('/afk_group '):
                await self.handle_afk_group_command(event, text)
            elif text == '/afk_group_off':
                await self.handle_afk_group_off_command(event)
            elif text.startswith('/afk_dm '):
                await self.handle_afk_dm_command(event, text)
            elif text == '/afk_dm_off':
                await self.handle_afk_dm_off_command(event)
            elif text.startswith('/afk '):
                await self.handle_afk_command(event, text)
            elif text == '/afk_off':
                await self.handle_afk_off_command(event)
            elif text == '/help':
                await self.handle_help_command(event)
            elif text == '/status':
                await self.handle_status_command(event)
            elif text == '/debug':
                await self.handle_debug_command(event)
        except Exception as e:
            logger.error(f"Error handling command: {e}")
            await event.reply(f"❌ Command error: {e}")
    
    # ----------------- SPAM -----------------
    async def handle_spam_command(self, event, text):
        try:
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /spam <chat_id> <message> <delay>")
                return
            chat_id = int(parts[1])
            message_delay = parts[2].split(' ', 1)
            if len(message_delay) != 2:
                await event.reply("Usage: /spam <chat_id> <message> <delay>")
                return
            message = message_delay[0]
            delay = int(message_delay[1])
            
            # Stop existing spam for this chat
            if chat_id in self.spam_tasks:
                self.spam_tasks[chat_id].cancel()
            
            async def spam_loop(chat, msg, d):
                while True:
                    await self.client.send_message(chat, msg)
                    await asyncio.sleep(d)
            
            task = asyncio.create_task(spam_loop(chat_id, message, delay))
            self.spam_tasks[chat_id] = task
            await event.reply(f"✅ Started spam in chat {chat_id}")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_stop_spam_command(self, event, text):
        try:
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await event.reply("Usage: /stop_spam <chat_id>")
                return
            chat_id = int(parts[1])
            if chat_id in self.spam_tasks:
                self.spam_tasks[chat_id].cancel()
                del self.spam_tasks[chat_id]
                await event.reply(f"✅ Stopped spam in chat {chat_id}")
            else:
                await event.reply("❌ No active spam in this chat")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    # ----------------- REPLY -----------------
    async def handle_set_reply_command(self, event, text):
        try:
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /setReplyFor <id> <message>")
                return
            target_id = int(parts[1])
            message = parts[2]
            self.reply_settings[target_id] = message
            self.save_settings()
            await event.reply(f"✅ Reply set for ID {target_id}: {message}")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_reset_reply_command(self, event, text):
        try:
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await event.reply("Usage: /resetreplyfor <id>")
                return
            target_id = int(parts[1])
            if target_id in self.reply_settings:
                del self.reply_settings[target_id]
                self.save_settings()
                await event.reply(f"✅ Reply removed for ID {target_id}")
            else:
                await event.reply(f"❌ No reply found for ID {target_id}")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_clear_reply_command(self, event):
        self.reply_settings.clear()
        self.save_settings()
        await event.reply("✅ All replies cleared")
    
    async def handle_list_reply_command(self, event):
        if not self.reply_settings:
            await event.reply("❌ No active replies")
            return
        lines = []
        for k, v in self.reply_settings.items():
            lines.append(f"{k}: {v}")
        await event.reply("📋 Active replies:\n" + "\n".join(lines))
    
    # ----------------- AFK -----------------
    async def handle_afk_group_command(self, event, text):
        parts = text.split(' ', 1)
        if len(parts) < 2:
            await event.reply("Usage: /afk_group <message>")
            return
        self.afk_message = parts[1]
        self.afk_group_active = True
        self.save_settings()
        await event.reply(f"✅ AFK group activated: {self.afk_message}")
    
    async def handle_afk_group_off_command(self, event):
        self.afk_group_active = False
        self.save_settings()
        await event.reply("✅ AFK group deactivated")
    
    async def handle_afk_dm_command(self, event, text):
        parts = text.split(' ', 1)
        if len(parts) < 2:
            await event.reply("Usage: /afk_dm <message>")
            return
        self.afk_message = parts[1]
        self.afk_dm_active = True
        self.save_settings()
        await event.reply(f"✅ AFK DM activated: {self.afk_message}")
    
    async def handle_afk_dm_off_command(self, event):
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ AFK DM deactivated")
    
    async def handle_afk_command(self, event, text):
        parts = text.split(' ', 1)
        if len(parts) >= 2:
            self.afk_message = parts[1]
        else:
            self.afk_message = "Currently offline"
        self.afk_group_active = True
        self.afk_dm_active = True
        self.save_settings()
        await event.reply(f"✅ AFK activated for groups and DMs: {self.afk_message}")
    
    async def handle_afk_off_command(self, event):
        self.afk_group_active = False
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ All AFK modes deactivated")
    
    # ----------------- INFO -----------------
    async def handle_help_command(self, event):
        help_text = """🤖 **Bot Commands:**
/spam <chat_id> <msg> <delay> - Start spam in a chat
/stop_spam <chat_id> - Stop spam in chat
/setReplyFor <id> <msg> - Set auto reply
/resetreplyfor <id> - Remove reply
/clear_reply - Remove all replies
/listreply - List replies
/afk_group <msg> - AFK group
/afk_group_off - Disable AFK group
/afk_dm <msg> - AFK DM
/afk_dm_off - Disable AFK DM
/afk <msg> - AFK both
/afk_off - Disable AFK
/help - Show this
/status - Bot status
/debug - Debug info"""
        await event.reply(help_text)
    
    async def handle_status_command(self, event):
        uptime = self.get_uptime()
        reply_count = len(self.reply_settings)
        spam_count = len(self.spam_tasks)
        status_text = f"""📊 Bot Status:
⏱ Uptime: {uptime}
🟢 Spam active in {spam_count} chats
😴 AFK Group: {'ON' if self.afk_group_active else 'OFF'}
😴 AFK DM: {'ON' if self.afk_dm_active else 'OFF'}
💬 Replies active: {reply_count}"""
        await event.reply(status_text)
    
    async def handle_debug_command(self, event):
        debug_info = f"Owner ID: {self.owner_id}\nBot ID: {self.bot_user_id}\nSender: {event.sender_id}\nPrivate: {event.is_private}"
        await event.reply(debug_info)

# ----------------- RUN -----------------
async def main():
    keep_alive()
    bot = TelegramBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
