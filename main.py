import os
import asyncio
import logging
import json
import sqlite3
import threading
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from keep_alive import keep_alive

# ===== CONFIG =====
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("SESSION", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== DATABASE =====
class BotDatabase:
    def __init__(self, db_path="settings.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._create_tables()

    def _connect(self):
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
            cur.execute("REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (key, json.dumps(value)))
            conn.commit()

    def get(self, key: str, default=None):
        with self.lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
            return default

# ===== BOT CLASS =====
class TelegramBot:
    def __init__(self):
        self.client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
        self.db = BotDatabase()
        print("🔑 ENV DEBUG:")
        print("API_ID:", os.getenv("API_ID"))
        print("API_HASH:", os.getenv("API_HASH"))
        print("SESSION_STRING (first 20 chars):", str(os.getenv("SESSION_STRING"))[:20])
        print("OWNER_ID:", os.getenv("OWNER_ID"))
        # Load settings
        self.reply_settings = self.db.get("reply_settings", {})
        self.afk_group_active = self.db.get("afk_group_active", False)
        self.afk_dm_active = self.db.get("afk_dm_active", False)
        self.afk_message = self.db.get("afk_message", "I'm AFK right now.")
        self.spam_active = self.db.get("spam_active", False)

    async def start(self):
        await self.client.start()
        logger.info("✅ Bot started!")

        # AFK handler
        @self.client.on(events.NewMessage(incoming=True))
        async def afk_handler(event):
            if event.sender_id == OWNER_ID:
                return
            if event.is_private and self.afk_dm_active:
                await event.reply(self.afk_message)
            elif event.is_group and self.afk_group_active:
                await event.reply(self.afk_message)

        # Spam handler
        @self.client.on(events.NewMessage(incoming=True))
        async def spam_handler(event):
            if self.spam_active and event.sender_id != OWNER_ID:
                await event.reply("⚠️ Please stop spamming!")

        # Command handler
        @self.client.on(events.NewMessage(outgoing=True, pattern=r'^/'))
        async def command_handler(event):
            if event.sender_id != OWNER_ID:
                return

            parts = event.raw_text.split(" ", 2)
            cmd = parts[0].lower()

            if cmd == "/start":
                await event.edit("🤖 Bot is running!")

            elif cmd == "/afk":
                if len(parts) > 1:
                    self.afk_message = parts[1]
                self.afk_group_active = True
                self.afk_dm_active = True
                self.db.set("afk_group_active", True)
                self.db.set("afk_dm_active", True)
                self.db.set("afk_message", self.afk_message)
                await event.edit(f"✅ AFK enabled!\nMessage: {self.afk_message}")

            elif cmd == "/afk_off":
                self.afk_group_active = False
                self.afk_dm_active = False
                self.db.set("afk_group_active", False)
                self.db.set("afk_dm_active", False)
                await event.edit("❌ AFK disabled!")

            elif cmd == "/spam_on":
                self.spam_active = True
                self.db.set("spam_active", True)
                await event.edit("🚨 Spam filter ON")

            elif cmd == "/spam_off":
                self.spam_active = False
                self.db.set("spam_active", False)
                await event.edit("✅ Spam filter OFF")

            elif cmd == "/setreply":
                if len(parts) < 3:
                    await event.edit("❌ Usage: /setreply <keyword> <response>")
                    return
                keyword, response = parts[1], parts[2]
                self.reply_settings[keyword] = response
                self.db.set("reply_settings", self.reply_settings)
                await event.edit(f"✅ Reply set: {keyword} → {response}")

            elif cmd == "/delreply":
                if len(parts) < 2:
                    await event.edit("❌ Usage: /delreply <keyword>")
                    return
                keyword = parts[1]
                if keyword in self.reply_settings:
                    del self.reply_settings[keyword]
                    self.db.set("reply_settings", self.reply_settings)
                    await event.edit(f"🗑️ Reply deleted: {keyword}")
                else:
                    await event.edit("⚠️ No such reply found")

            elif cmd == "/replies":
                if not self.reply_settings:
                    await event.edit("ℹ️ No replies set")
                else:
                    msg = "\n".join([f"{k} → {v}" for k, v in self.reply_settings.items()])
                    await event.edit("📋 Current replies:\n" + msg)

        # Auto-reply handler
        @self.client.on(events.NewMessage(incoming=True))
        async def auto_reply(event):
            if event.sender_id == OWNER_ID:
                return
            text = event.raw_text.lower()
            for k, v in self.reply_settings.items():
                if k.lower() in text:
                    await event.reply(v)
                    break

        await self.client.run_until_disconnected()

# ===== MAIN =====
if __name__ == "__main__":
    keep_alive()  # start flask

    try:
        client.start()
        me = client.loop.run_until_complete(client.get_me())
        print(f"✅ Logged in as: {me.first_name} (ID: {me.id})")
    except Exception as e:
        print(f"❌ Login failed: {e}")

    with client:
        client.loop.run_until_complete(main())
