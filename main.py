import os
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from flask import Flask
import threading

# ===== CONFIG =====
API_ID = int(os.environ.get("API_ID", "12345"))   # replace with your api_id
API_HASH = os.environ.get("API_HASH", "your_api_hash")
SESSION_STRING = os.environ.get("SESSION_STRING", "your_session_string")
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789"))

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask keep-alive
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"
def run():
    app.run(host="0.0.0.0", port=8080)
threading.Thread(target=run).start()

# ===== BOT =====
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

spamming = False
spam_task = None

async def spam_loop(event, text, delay):
    global spamming
    while spamming:
        try:
            await event.respond(text)
        except Exception as e:
            logger.error(f"Error sending spam: {e}")
        await asyncio.sleep(delay)

@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/spam (.+) (\d+)$"))
async def handler(event):
    global spamming, spam_task
    if spamming:
        await event.respond("❌ Already spamming! Use /spam_off first.")
        return
    text = event.pattern_match.group(1)
    delay = int(event.pattern_match.group(2))
    spamming = True
    await event.respond(f"✅ Spamming started: '{text}' every {delay} sec.")
    spam_task = asyncio.create_task(spam_loop(event, text, delay))

@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/spam_off$"))
async def stop_handler(event):
    global spamming, spam_task
    spamming = False
    if spam_task:
        spam_task.cancel()
        spam_task = None
    await event.respond("🛑 Spamming stopped.")

print("Bot is running...")
client.start()
client.run_until_disconnected()
