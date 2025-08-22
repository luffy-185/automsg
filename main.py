import asyncio
import os
import json
import time
import logging
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from keep_alive import keep_alive


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TelegramBot:
    def __init__(self):
        self.api_id = int(os.getenv("API_ID"))
        self.api_hash = os.getenv("API_HASH")
        self.session_string = os.getenv("SESSION_STRING")
        self.owner_id = int(os.getenv("OWNER_ID"))

        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
        self.bot_user_id = None

        # Bot state
        self.reply_settings = {}           # chat/user ID -> message
        self.user_message_count = defaultdict(int)
        self.user_last_reply = {}
        self.spam_tasks = {}
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.spam_active = False
        self.start_time = time.time()

        # Load settings
        self.load_settings()

    def save_settings(self):
        try:
            with open("bot_settings.json", "w") as f:
                json.dump({
                    "reply_settings": self.reply_settings,
                    "afk_group_active": self.afk_group_active,
                    "afk_dm_active": self.afk_dm_active,
                    "afk_message": self.afk_message,
                    "spam_active": self.spam_active
                }, f)
        except Exception as e:
            logger.error(f"Error saving settings: {e}")

    def load_settings(self):
        try:
            if os.path.exists("bot_settings.json"):
                with open("bot_settings.json", "r") as f:
                    data = json.load(f)
                    self.reply_settings = {int(k): v for k, v in data.get("reply_settings", {}).items()}
                    self.afk_group_active = data.get("afk_group_active", False)
                    self.afk_dm_active = data.get("afk_dm_active", False)
                    self.afk_message = data.get("afk_message", "Currently offline")
                    self.spam_active = data.get("spam_active", False)
        except Exception as e:
            logger.error(f"Error loading settings: {e}")

    def get_uptime(self):
        uptime = time.time() - self.start_time
        days = int(uptime // 86400)
        hours = int((uptime % 86400) // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)
        return f"{days}d {hours}h {minutes}m {seconds}s" if days else f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    async def start(self):
        await self.client.start()
        self.bot_user_id = (await self.client.get_me()).id
        logger.info(f"Bot started as {self.bot_user_id}")
        self.client.add_event_handler(self.handle_message, events.NewMessage)
        self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))
        await self.client.run_until_disconnected()

    async def handle_outgoing(self, event):
        if event.sender_id == self.owner_id and event.message.text != self.afk_message:
            if self.afk_group_active or self.afk_dm_active:
                self.afk_group_active = False
                self.afk_dm_active = False
                self.save_settings()
                logger.info("AFK disabled - owner sent a message")

    async def handle_message(self, event):
        if event.is_private:
            await self.handle_dm(event)
        else:
            await self.handle_group(event)

    async def handle_dm(self, event):
        uid = event.sender_id
        if uid == self.owner_id:
            await self.handle_command(event)
            return
        if event.message.text and event.message.text.startswith("/"):
            return

        # AFK DM reply
        if self.afk_dm_active:
            now = time.time()
            if uid not in self.user_last_reply or now - self.user_last_reply[uid] >= 1800:
                await event.reply(self.afk_message)
                self.user_last_reply[uid] = now

        # ReplyFor
        if uid in self.reply_settings:
            self.user_message_count[uid] += 1
            if self.user_message_count[uid] == 1:
                await event.reply(self.reply_settings[uid])
                asyncio.create_task(self.reset_user_count(uid))

    async def reset_user_count(self, uid):
        await asyncio.sleep(1800)
        self.user_message_count[uid] = 0

    async def handle_group(self, event):
        chat_id = event.chat_id
        is_mentioned = event.message.mentioned or (event.message.reply_to and (await event.get_reply_message()).sender_id == self.bot_user_id)
        if not is_mentioned:
            return

        if self.afk_group_active:
            await event.reply(self.afk_message)
        if chat_id in self.reply_settings:
            await event.reply(self.reply_settings[chat_id])

    async def handle_command(self, event):
        text = event.message.text.strip()
        if text.startswith("/spam "):
            await self.handle_spam(event, text)
        elif text.startswith("/setReplyFor "):
            await self.handle_set_reply(event, text)
        elif text.startswith("/resetreplyfor "):
            await self.handle_reset_reply(event, text)
        elif text == "/clear_reply":
            await self.handle_clear_reply(event)
        elif text == "/listreply":
            await self.handle_list_reply(event)
        elif text.startswith("/afk_group "):
            await self.handle_afk_group(event, text)
        elif text == "/afk_group_off":
            await self.handle_afk_group_off(event)
        elif text.startswith("/afk_dm "):
            await self.handle_afk_dm(event, text)
        elif text == "/afk_dm_off":
            await self.handle_afk_dm_off(event)
        elif text.startswith("/afk "):
            await self.handle_afk(event, text)
        elif text == "/afk_off":
            await self.handle_afk_off(event)
        elif text == "/help":
            await self.handle_help(event)
        elif text == "/status":
            await self.handle_status(event)
        elif text == "/stop_spam":
            await self.handle_stop_spam(event)

    # -------------------- Commands --------------------

    async def handle_help(self, event):
        help_text = """🤖 **Commands:**
/spam <msg> <delay> - Spam message in groups/channels
/stop_spam - Stop all spam
/setReplyFor <id> <msg> - Auto-reply for chat/user
/resetreplyfor <id> - Remove reply for one chat
/clear_reply - Remove all replies
/listreply - List all active replies
/afk_group <msg> - AFK for groups
/afk_group_off - Disable group AFK
/afk_dm <msg> - AFK for DMs
/afk_dm_off - Disable DM AFK
/afk <msg> - AFK for both
/afk_off - Disable all AFK
/help - Show this
/status - Show bot status"""
        await event.reply(help_text)

    async def handle_status(self, event):
        uptime = self.get_uptime()
        reply_count = len(self.reply_settings)
        active_spam = len([t for t in self.spam_tasks.values() if not t.done()])
        status_text = f"⏱️ Uptime: {uptime}\n💬 Replies: {reply_count}\n🟢 Active Spam Tasks: {active_spam}\n😴 AFK Group: {self.afk_group_active}\n😴 AFK DM: {self.afk_dm_active}"
        await event.reply(status_text)

    async def handle_set_reply(self, event, text):
        try:
            parts = text.split(" ", 2)
            target_id = int(parts[1])
            msg = parts[2]
            self.reply_settings[target_id] = msg
            self.save_settings()
            await event.reply(f"✅ Reply set for {target_id}")
        except:
            await event.reply("❌ Usage: /setReplyFor <id> <msg>")

    async def handle_reset_reply(self, event, text):
        try:
            target_id = int(text.split()[1])
            if target_id in self.reply_settings:
                del self.reply_settings[target_id]
                self.save_settings()
                await event.reply(f"✅ Reply removed for {target_id}")
            else:
                await event.reply("❌ No reply set for this ID")
        except:
            await event.reply("❌ Usage: /resetreplyfor <id>")

    async def handle_clear_reply(self, event):
        self.reply_settings.clear()
        self.save_settings()
        await event.reply("✅ All replies cleared")

    async def handle_list_reply(self, event):
        if not self.reply_settings:
            await event.reply("❌ No active replies")
            return
        lines = []
        for k, v in self.reply_settings.items():
            lines.append(f"{k}: {v}")
        await event.reply("📋 Active Replies:\n" + "\n".join(lines))

    async def handle_afk_group(self, event, text):
        self.afk_group_active = True
        self.afk_message = " ".join(text.split()[1:])
        self.save_settings()
        await event.reply(f"✅ AFK Group activated: {self.afk_message}")

    async def handle_afk_group_off(self, event):
        self.afk_group_active = False
        self.save_settings()
        await event.reply("✅ AFK Group deactivated")

    async def handle_afk_dm(self, event, text):
        self.afk_dm_active = True
        self.afk_message = " ".join(text.split()[1:])
        self.save_settings()
        await event.reply(f"✅ AFK DM activated: {self.afk_message}")

    async def handle_afk_dm_off(self, event):
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ AFK DM deactivated")

    async def handle_afk(self, event, text):
        self.afk_group_active = True
        self.afk_dm_active = True
        self.afk_message = " ".join(text.split()[1:])
        self.save_settings()
        await event.reply(f"✅ AFK activated for both: {self.afk_message}")

    async def handle_afk_off(self, event):
        self.afk_group_active = False
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ AFK deactivated")

    async def handle_spam(self, event, text):
        try:
            parts = text.split(" ", 2)
            msg = parts[1]
            delay = int(parts[2])
            dialogs = await self.client.get_dialogs()
            # stop previous tasks
            await self.handle_stop_spam(event)
            for d in dialogs:
                if d.is_group or d.is_channel:
                    async def spam_task(chat_id=d.id, msg=msg, delay=delay):
                        while True:
                            await self.client.send_message(chat_id, msg)
                            await asyncio.sleep(delay)
                    task = asyncio.create_task(spam_task())
                    self.spam_tasks[d.id] = task
            self.spam_active = True
            self.save_settings()
            await event.reply(f"✅ Started spam '{msg}' with {delay}s delay")
        except:
            await event.reply("❌ Usage: /spam <msg> <delay>")

    async def handle_stop_spam(self, event):
        count = 0
        for t in self.spam_tasks.values():
            if not t.done():
                t.cancel()
                count += 1
        self.spam_tasks.clear()
        self.spam_active = False
        self.save_settings()
        await event.reply(f"✅ Stopped {count} spam tasks")

# -------------------- Main --------------------
async def main():
    bot = TelegramBot()
    keep_alive()
    while True:
        try:
            await bot.start()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
