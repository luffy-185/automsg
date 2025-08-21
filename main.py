import os
import asyncio
import time
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# 🌐 Import keep_alive Flask server
try:
    from keep_alive import keep_alive
    KEEP_ALIVE = True
except ImportError:
    KEEP_ALIVE = False

# ===== CONFIG =====
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# ===== TELETHON CLIENT =====
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ===== GLOBALS =====
spam_tasks = {}  # {chat_id: asyncio.Task}
start_time = time.time()


# ===== HELPERS =====
def is_owner(sender_id):
    return sender_id == OWNER_ID

def uptime():
    return int(time.time() - start_time)


# ===== COMMAND HANDLERS =====
@client.on(events.NewMessage(pattern=r"^/spam (.+) (\d+)$"))
async def spam_handler(event):
    if not is_owner(event.sender_id):
        return

    msg = event.pattern_match.group(1)
    delay = int(event.pattern_match.group(2))
    chat_id = event.chat_id

    # cancel old task if exists
    if chat_id in spam_tasks:
        spam_tasks[chat_id].cancel()

    async def spam_loop():
        while True:
            try:
                await client.send_message(chat_id, msg)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Spam error in {chat_id}: {e}")
                break

    spam_tasks[chat_id] = asyncio.create_task(spam_loop())
    await event.reply(f"✅ Spam started in this chat every {delay}s.")


@client.on(events.NewMessage(pattern=r"^/spam off$"))
async def spam_off_handler(event):
    if not is_owner(event.sender_id):
        return

    chat_id = event.chat_id
    if chat_id in spam_tasks:
        spam_tasks[chat_id].cancel()
        del spam_tasks[chat_id]
        await event.reply("🛑 Spam stopped in this chat.")
    else:
        await event.reply("⚠️ No active spam in this chat.")


@client.on(events.NewMessage(pattern=r"^/status$"))
async def status_handler(event):
    if not is_owner(event.sender_id):
        return

    active_chats = list(spam_tasks.keys())
    msg = "📊 **Bot Status**\n"
    msg += f"⏱ Uptime: {uptime()}s\n"
    msg += f"💬 Active spam chats: {len(active_chats)}\n"
    if active_chats:
        msg += "➡️ " + ", ".join([str(c) for c in active_chats])
    await event.reply(msg)


@client.on(events.NewMessage(pattern=r"^/help$"))
async def help_handler(event):
    if not is_owner(event.sender_id):
        return

    help_text = (
        "🤖 **AutoSpam Bot Commands**\n\n"
        "/spam <msg> <delay> → start spamming in this chat\n"
        "/spam off → stop spamming in this chat\n"
        "/status → show uptime & active chats\n"
        "/help → show this help message"
    )
    await event.reply(help_text)


# ===== MAIN =====
if __name__ == "__main__":
    if KEEP_ALIVE:
        keep_alive()
    print("🚀 Bot starting...")
    client.start()
    client.run_until_disconnected()
