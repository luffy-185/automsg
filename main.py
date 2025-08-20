import os
import asyncio
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ===== Keep-alive =====
try:
    from keep_alive import keep_alive
    keep_alive()
except ImportError:
    print("⚠️ keep_alive.py not found - continuing without it.")

# ===== CONFIG =====
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", ""))
SESSION = os.environ.get("SESSION", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

# ===== GLOBALS =====
start_time = datetime.now()
spam_tasks = {}            # {chat_id: asyncio.Task}
reply_db = {}              # {id: reply_text}
dm_cooldown = {}           # {user_id: datetime of last reply}
afk_groups = set()
afk_dm = set()
afk_both = False

# ===== HELP TEXT =====
HELP_TEXT = """
📌 Commands:
/spam <msg> <delay> - Start spam in this chat
/spam off - Stop spam
/status - Show uptime & active chats
/setReplyFor <id> - Set auto-reply for user/chat
/delReplyFor <id> - Delete a specific auto-reply
/clearReplies - Clear all auto-replies
/afk_group - Set AFK for groups
/afk_dm - Set AFK for DMs
/afk_both - Set AFK for all
/afk_off - Disable AFK
/help - Show this help
"""

# ===== UTILS =====
def is_owner(sender_id):
    return sender_id == OWNER_ID

def format_placeholders(text, sender, chat):
    text = text.replace("{name}", getattr(sender, "first_name", "") or "")
    text = text.replace("{id}", str(sender.id))
    text = text.replace("{chat}", getattr(chat, "title", "Private Chat") or "")
    return text

def uptime():
    return str(datetime.now() - start_time).split(".")[0]

# ===== COMMAND HANDLER =====
@client.on(events.NewMessage(pattern=r"^/"))
async def command_handler(event):
    if not is_owner(event.sender_id):
        return

    chat_id = event.chat_id
    args = event.raw_text.split()
    cmd = args[0].lower()

    # ---- Spam ----
    if cmd == "/spam":
        if len(args) >= 2 and args[1].lower() == "off":
            task = spam_tasks.pop(chat_id, None)
            if task:
                task.cancel()
                await event.reply("✅ Spam stopped in this chat.")
            else:
                await event.reply("⚠️ No spam running here.")
        elif len(args) >= 3:
            msg = " ".join(args[1:-1])
            try:
                delay = int(args[-1])
            except:
                await event.reply("❌ Invalid delay")
                return

            if chat_id in spam_tasks:
                spam_tasks[chat_id].cancel()

            async def spammer():
                while True:
                    await client.send_message(chat_id, msg)
                    await asyncio.sleep(delay)

            spam_tasks[chat_id] = asyncio.create_task(spammer())
            await event.reply(f"✅ Spamming started: `{msg}` every {delay}s")
        else:
            await event.reply("❌ Usage: /spam <msg> <delay> OR /spam off")

    # ---- Status ----
    elif cmd == "/status":
        active = ", ".join(str(cid) for cid in spam_tasks.keys()) or "None"
        msg = f"⏱ Uptime: {uptime()}\n💬 Active spams: {active}\n🤖 Auto-replies: {len(reply_db)}\nAFK Groups: {afk_groups}\nAFK DMs: {afk_dm}\nAFK Both: {afk_both}"
        await event.reply(msg)

    # ---- Help ----
    elif cmd == "/help":
        await event.reply(HELP_TEXT)

    # ---- Set Reply ----
    elif cmd == "/setreplyfor" and len(args) == 2:
        target_id = int(args[1])
        await event.reply(f"✍️ Send the reply message for ID `{target_id}`")
        @client.on(events.NewMessage(from_users=OWNER_ID))
        async def save_reply(event2):
            reply_db[target_id] = event2.raw_text
            await event2.reply(f"✅ Reply set for {target_id}")
            client.remove_event_handler(save_reply)

    elif cmd == "/delreplyfor" and len(args) == 2:
        target_id = int(args[1])
        reply_db.pop(target_id, None)
        await event.reply(f"🗑 Deleted reply for {target_id}")

    elif cmd == "/clearReplies":
        reply_db.clear()
        await event.reply("🗑 All auto-replies cleared.")

    # ---- AFK ----
    elif cmd == "/afk_group":
        afk_groups.add(chat_id)
        await event.reply("✅ AFK for groups enabled.")
    elif cmd == "/afk_dm":
        afk_dm.add(chat_id)
        await event.reply("✅ AFK for DMs enabled.")
    elif cmd == "/afk_both":
        afk_both = True
        await event.reply("✅ AFK for all chats enabled.")
    elif cmd == "/afk_off":
        afk_groups.discard(chat_id)
        afk_dm.discard(chat_id)
        afk_both = False
        await event.reply("🛑 AFK disabled for this chat.")

# ===== MESSAGE HANDLER =====
@client.on(events.NewMessage)
async def auto_reply(event):
    sender = await event.get_sender()
    chat = await event.get_chat()
    is_private = event.is_private
    chat_id = event.chat_id
    sender_id = event.sender_id

    # remove AFK if owner sends message
    if sender_id == OWNER_ID:
        if is_private: afk_dm.discard(chat_id)
        else: afk_groups.discard(chat_id)
        return

    # DM replies
    if is_private:
        if sender_id in reply_db or afk_dm or afk_both:
            last = dm_cooldown.get(sender_id)
            if not last or datetime.now() - last > timedelta(minutes=30):
                msg_text = reply_db.get(sender_id, "I'm away right now.")
                await event.reply(format_placeholders(msg_text, sender, chat))
                dm_cooldown[sender_id] = datetime.now()

    # Group replies
    else:
        if chat_id in reply_db or chat_id in afk_groups or afk_both:
            if event.is_reply or (client.me and client.me.username and f"@{client.me.username}" in event.raw_text):
                msg_text = reply_db.get(chat_id, "I'm away right now.")
                await event.reply(format_placeholders(msg_text, sender, chat))

# ===== RUN =====
print("🚀 Userbot started...")
client.start()
client.run_until_disconnected()
