# main.py
import asyncio
import time
import os
import json
import logging
import threading
from collections import defaultdict
from typing import Dict, Set
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from flask import Flask

import sqlite3

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Flask health check ----------
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

@app.route('/ping')
def ping():
    return "pong", 200


# ---------- Simple thread-safe SQLite helper ----------
class BotDatabase:
    def __init__(self, db_path="settings.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._create_tables()

    def _connect(self):
        # Allow use from different threads/tasks
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _create_tables(self):
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    def set(self, key: str, value):
        with self.lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            conn.commit()

    def get(self, key: str, default=None):
        with self.lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = cur.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except:
                    return default
            return default

    def delete(self, key: str):
        with self.lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM settings WHERE key=?", (key,))
            conn.commit()


# ---------- Telegram Bot ----------
class TelegramBot:
    def __init__(self):
        # load required env vars (will raise if missing)
        self.api_id = int(os.getenv('API_ID'))
        self.api_hash = os.getenv('API_HASH')
        self.session_string = os.getenv('SESSION_STRING')
        self.owner_id = int(os.getenv('OWNER_ID'))

        # Telethon client
        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

        # state
        self.reply_settings: Dict[int, str] = {}
        self.user_message_count = defaultdict(int)
        self.user_last_reply = {}
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.spam_tasks: Set[asyncio.Task] = set()
        self.bot_user_id = None
        self.start_time = time.time()
        self.spam_active = False

        # DB
        # On Render, ensure the working directory or configured path is on a persistent disk
        db_path = os.getenv("SETTINGS_DB_PATH", "settings.db")
        self.db = BotDatabase(db_path)

        # load persisted settings
        self.load_settings()

    def load_settings(self):
        try:
            self.reply_settings = {int(k): v for k, v in self.db.get("reply_settings", {}).items()} if self.db.get("reply_settings", None) else {}
            self.afk_group_active = self.db.get("afk_group_active", False)
            self.afk_dm_active = self.db.get("afk_dm_active", False)
            self.afk_message = self.db.get("afk_message", "Currently offline")
            self.spam_active = self.db.get("spam_active", False)
        except Exception as e:
            logger.error(f"Error loading settings from DB: {e}")

    def save_settings(self):
        try:
            self.db.set("reply_settings", self.reply_settings)
            self.db.set("afk_group_active", self.afk_group_active)
            self.db.set("afk_dm_active", self.afk_dm_active)
            self.db.set("afk_message", self.afk_message)
            self.db.set("spam_active", self.spam_active)
        except Exception as e:
            logger.error(f"Error saving settings to DB: {e}")

    def get_uptime(self):
        uptime_seconds = int(time.time() - self.start_time)
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h {minutes}m {seconds}s"
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    async def start(self):
        """Start the Telethon client and register handlers."""
        await self.client.start()
        me = await self.client.get_me()
        self.bot_user_id = me.id
        logger.info(f"Bot session user id: {self.bot_user_id}")
        logger.info(f"Configured owner id: {self.owner_id}")

        # Register handlers
        # Use class-bound coroutine functions (they accept event)
        self.client.add_event_handler(self._on_new_message, events.NewMessage)
        # NO outgoing handler needed for owner detection — owner sends messages to bot which are incoming

        logger.info("Bot started and event handlers registered.")
        await self.client.run_until_disconnected()

    # Unified incoming message handler
    async def _on_new_message(self, event):
        try:
            # Telethon event has .message, .sender_id, .is_private, etc.
            # If event has no message or text, we still handle mentions and replies in group handler
            sender = event.sender_id

            # If message is from owner and is a command (anywhere), handle command
            if sender == self.owner_id and event.message and isinstance(event.message.text, str) and event.message.text.strip().startswith('/'):
                await self.handle_command(event)
                return

            # If owner sent a non-command direct message to bot: disable AFK (owner is interacting)
            if event.is_private and sender == self.owner_id:
                # If owner message equals AFK message, ignore; otherwise disable AFK
                text = getattr(event.message, 'text', '') or ''
                if text.strip() != self.afk_message:
                    if self.afk_group_active or self.afk_dm_active:
                        self.afk_group_active = False
                        self.afk_dm_active = False
                        self.save_settings()
                        logger.info("AFK disabled because owner interacted.")

                # If it's a command (handled above) we returned; if not, we can still return to avoid treating owner's DM as ordinary
                return

            # If private message (DM) from others
            if event.is_private:
                await self.handle_dm(event)
            else:
                await self.handle_group_message(event)

        except Exception as e:
            logger.exception(f"Error in _on_new_message: {e}")

    # DM handling for non-owner users
    async def handle_dm(self, event):
        try:
            user_id = event.sender_id

            # If someone else sends a command (not owner), ignore commands
            text = getattr(event.message, 'text', '') or ''
            if text.startswith('/') and user_id != self.owner_id:
                logger.info(f"Ignored command from non-owner {user_id}: {text}")
                return

            # AFK DM handling
            if self.afk_dm_active and user_id != self.owner_id:
                current_time = time.time()
                last = self.user_last_reply.get(user_id, 0)
                # reply at most once per 30 minutes to the same user
                if (current_time - last) >= 1800:
                    try:
                        await event.reply(self.afk_message)
                        self.user_last_reply[user_id] = current_time
                    except Exception as e:
                        logger.error(f"Failed to send AFK DM reply to {user_id}: {e}")

            # Specific user auto-reply
            if user_id in self.reply_settings:
                self.user_message_count[user_id] += 1
                if self.user_message_count[user_id] == 1:
                    try:
                        await event.reply(self.reply_settings[user_id])
                    except Exception as e:
                        logger.error(f"Failed to send auto reply to {user_id}: {e}")
                    # schedule reset
                    asyncio.create_task(self.reset_user_count(user_id))

        except Exception as e:
            logger.exception(f"Error handling DM: {e}")

    async def reset_user_count(self, user_id):
        await asyncio.sleep(1800)
        self.user_message_count[user_id] = 0

    # Group message handling (mentions & replies)
    async def handle_group_message(self, event):
        try:
            # Only respond when bot is mentioned or message replies to a bot message
            is_mentioned = getattr(event.message, 'mentioned', False)

            # Also check if the message is a reply to a bot message
            if not is_mentioned and getattr(event.message, 'reply_to', None):
                try:
                    replied = await event.get_reply_message()
                    if replied and replied.sender_id == self.bot_user_id:
                        is_mentioned = True
                except Exception:
                    pass

            if not is_mentioned:
                return

            # AFK group reply
            if self.afk_group_active:
                try:
                    await event.reply(self.afk_message)
                except Exception as e:
                    logger.error(f"Failed to send AFK group reply: {e}")

            # Chat-specific auto reply (chat id)
            chat_id = event.chat_id
            if chat_id in self.reply_settings:
                try:
                    await event.reply(self.reply_settings[chat_id])
                except Exception as e:
                    logger.error(f"Failed to send chat auto-reply in {chat_id}: {e}")

        except Exception as e:
            logger.exception(f"Error handling group message: {e}")

    # ---------- Commands (owner-only) ----------
    async def handle_command(self, event):
        try:
            text = (event.message.text or "").strip()
            logger.info(f"Owner command received: {text}")

            if not text:
                return

            if text.startswith('/spam '):
                await self.handle_spam_command(event, text)
            elif text.startswith('/stop_spam'):
                await self.handle_stop_spam_command(event)
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
            else:
                # If owner sent plain text that is not a command, we already disabled AFK earlier.
                # You might want to support owner short commands here.
                pass

        except Exception as e:
            logger.exception(f"Error handling owner command: {e}")
            try:
                await event.reply(f"❌ Command error: {e}")
            except:
                pass

    # ---------- Individual command implementations ----------
    async def handle_debug_command(self, event):
        try:
            debug_info = f"""🔍 Debug Info:

Your User ID: {event.sender_id}
Set Owner ID: {self.owner_id}
Match: {'YES' if event.sender_id == self.owner_id else 'NO'}
Bot User ID: {self.bot_user_id}
Message Type: {'DM' if event.is_private else 'Group'}
Command Text: `{event.message.text}`
"""
            await event.reply(debug_info)
        except Exception as e:
            await event.reply(f"Debug error: {e}")

    async def handle_help_command(self, event):
        help_text = """🤖 Bot Commands:

Spam:
• /spam <message> <delay_seconds>
• /stop_spam

Replies:
• /setReplyFor <id> <msg>
• /resetreplyfor <id>
• /clear_reply
• /listreply

AFK:
• /afk_group <msg>
• /afk_group_off
• /afk_dm <msg>
• /afk_dm_off
• /afk <msg>
• /afk_off

Info:
• /help
• /status
• /debug
"""
        await event.reply(help_text)

    async def handle_status_command(self, event):
        uptime = self.get_uptime()
        active_spam_count = len([t for t in self.spam_tasks if not t.done()])
        reply_count = len(self.reply_settings)
        status_text = f"""📊 Bot Status:

⏱️ Uptime: {uptime}
🔄 Spam Status: {'Active' if self.spam_active and active_spam_count>0 else 'Inactive'}
• Active spam tasks: {active_spam_count}

😴 AFK:
• Group AFK: {'On' if self.afk_group_active else 'Off'}
• DM AFK: {'On' if self.afk_dm_active else 'Off'}
• Message: "{self.afk_message}"

💬 Auto-Reply:
• Active replies: {reply_count}

🤖 Bot ID: {self.bot_user_id}
"""
        await event.reply(status_text)

    # Better spam parsing: allow spaces in message by splitting from right
    async def handle_spam_command(self, event, text):
        try:
            # Expect: /spam <message> <delay_seconds>
            # We rsplit once so last token is delay
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await event.reply("Usage: /spam <message> <delay_seconds>")
                return

            rest = parts[1].rstrip()
            if ' ' not in rest:
                await event.reply("Usage: /spam <message> <delay_seconds>")
                return

            message, delay_str = rest.rsplit(' ', 1)
            delay = int(delay_str)

            # Stop existing spam tasks first
            await self.stop_all_spam_tasks()

            dialogs = await self.client.get_dialogs()

            async def spam_chat(dialog, msg, delay_sec):
                try:
                    while True:
                        try:
                            await self.client.send_message(dialog.entity, msg)
                        except Exception as e:
                            # likely permissions or floodwait; log and break to avoid continuous errors
                            logger.error(f"Error sending to {getattr(dialog, 'name', repr(dialog))}: {e}")
                            break
                        await asyncio.sleep(delay_sec)
                except asyncio.CancelledError:
                    logger.info("Spam task cancelled")
                except Exception as e:
                    logger.exception(f"Spam task exception: {e}")

            spam_count = 0
            for dialog in dialogs:
                # skip users (private)
                if getattr(dialog, 'is_user', False):
                    continue
                # only groups/channels
                if getattr(dialog, 'is_group', False) or getattr(dialog, 'is_channel', False):
                    try:
                        task = asyncio.create_task(spam_chat(dialog, message, delay))
                        self.spam_tasks.add(task)
                        spam_count += 1
                    except Exception as e:
                        logger.error(f"Failed to start spam in dialog {getattr(dialog, 'name', '')}: {e}")

            if spam_count == 0:
                await event.reply("❌ No groups/channels found to spam in!")
                return

            self.spam_active = True
            self.save_settings()
            await event.reply(f"✅ Started spamming in {spam_count} groups/channels with {delay}s delay.")
        except ValueError:
            await event.reply("❌ Delay must be an integer (seconds).")
        except Exception as e:
            logger.exception(f"Error starting spam: {e}")
            await event.reply(f"❌ Error: {e}")

    async def handle_stop_spam_command(self, event):
        try:
            stopped = await self.stop_all_spam_tasks()
            self.spam_active = False
            self.save_settings()
            await event.reply(f"✅ Stopped {stopped} spam tasks")
        except Exception as e:
            await event.reply(f"❌ Error stopping spam: {e}")

    async def stop_all_spam_tasks(self):
        stopped_count = 0
        for task in list(self.spam_tasks):
            if not task.done():
                task.cancel()
                stopped_count += 1
        self.spam_tasks.clear()
        return stopped_count

    async def handle_set_reply_command(self, event, text):
        try:
            # /setReplyFor <id> <message>
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /setReplyFor <id> <message>")
                return
            target_id = int(parts[1])
            message = parts[2]
            self.reply_settings[target_id] = message
            self.save_settings()
            await event.reply(f"✅ Reply set for ID {target_id}")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")

    async def handle_reset_reply_command(self, event, text):
        try:
            parts = text.split(' ')
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
        for tid, msg in self.reply_settings.items():
            try:
                entity = await self.client.get_entity(tid)
                if hasattr(entity, 'username') and entity.username:
                    name = f"@{entity.username}"
                elif hasattr(entity, 'title'):
                    name = entity.title
                else:
                    name = f"ID {tid}"
            except:
                name = f"ID {tid}"
            lines.append(f"• {name}: {msg}")
        await event.reply("📋 Active Replies:\n" + "\n".join(lines))

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
        self.afk_message = parts[1] if len(parts) >= 2 else "Currently offline"
        self.afk_group_active = True
        self.afk_dm_active = True
        self.save_settings()
        await event.reply(f"✅ AFK enabled for group & DM: {self.afk_message}")

    async def handle_afk_off_command(self, event):
        self.afk_group_active = False
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ All AFK modes disabled")


# ---------- Flask runner ----------
def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)


# ---------- Entrypoint ----------
async def main():
    # Start Flask in background thread (health checks)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    bot = TelegramBot()
    while True:
        try:
            await bot.start()
        except Exception as e:
            logger.exception(f"Bot crashed: {e}")
            logger.info("Restarting in 10 seconds...")
            await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
