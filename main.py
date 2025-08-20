import os
import asyncio
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ===== Keep Alive =====
try:
    from keep_alive import keep_alive
    keep_alive()
except ImportError:
    print("⚠️ keep_alive.py not found - running without keepalive.")

# ===== Config =====
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("SESSION", "")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

# ===== Globals =====
start_time = datetime.now()
spam_tasks = {}          # {chat_id: asyncio.Task}
reply_db = {}            # {id: reply_text} -> setReplyFor
dm_cooldown = {}         # {user_id: datetime} -> 30 min cooldown
setup_mode = {}          # {owner_id: target_id} -> waiting for reply text

afk_msg_group = None
afk_msg_dm = None
afk_all = False

# ===== Utils =====
def is_owner(sender_id):
    return sender_id == OWNER_ID

def format_placeholders(text, sender, chat):
    if "{name}" in text:
        text = text.replace("{name}", sender.first_name if sender.first_name else "")
    if "{id}" in text:
        text = text.replace("{id}", str(sender.id))
    if "{chat}" in text and chat:
        text = text.replace("{chat}", getattr(chat, "title", "Private Chat"))
    return text

def uptime():
    delta = datetime.now() - start_time
    return str(delta).split(".")[0]

# ===== Command Handler =====
@client.on(events.NewMessage(pattern=r"^/"))
async def command_handler(event):
    if not is_owner(event.sender_id):
        return

    chat_id = event.chat_id
    args = event.raw_text.split()
    cmd = args[0].lower()
    global afk_msg_group, afk_msg_dm, afk_all

    # ---- Spam ----
    if cmd == "/spam":
        if len(args) == 2 and args[1].lower() == "off":
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

            async def spam_loop():
                while True:
                    await client.send_message(chat_id, msg)
                    await asyncio.sleep(delay)

            spam_tasks[chat_id] = asyncio.create_task(spam_loop())
            await event.reply(f"✅ Spamming `{msg}` every {delay}s")
        else:
            await event.reply("❌ Usage: /spam <msg> <delay> OR /spam off")

    # ---- Status ----
    elif cmd == "/status":
        active_spams = ", ".join(str(cid) for cid in spam_tasks.keys()) or "None"
        reply_count = len(reply_db)
        msg = (
            f"⏱ Uptime: {uptime()}\n"
            f"💬 Active spams: {active_spams}\n"
            f"🤖 Auto-replies: {reply_count}\n"
            f"AFK Group: {afk_msg_group if afk_msg_group else '❌ Off'}\n"
            f"AFK DM: {afk_msg_dm if afk_msg_dm else '❌ Off'}\n"
            f"AFK All: {'✅' if afk_all else '❌'}"
        )
        await event.reply(msg)

    # ---- Help ----
    elif cmd == "/help":
        help_text = """
📌 Commands:
/spam <msg> <delay>  - Start spam
/spam off            - Stop spam
/status              - Show uptime & active chats
/setReplyFor <id>    - Set auto-reply
/listReplies         - Show all replies
/delReplyFor <id>    - Delete a reply
/clearReplies        - Clear all replies
/afk_group <msg>     - AFK for groups
/afk_dm <msg>        - AFK for DMs
/afk_all <msg>       - AFK both groups + DMs
/afk_off             - Disable all AFK
/help                - Show this help
"""
        await event.reply(help_text)

    # ---- Set Reply ----
    elif cmd == "/setreplyfor" and len(args) == 2:
        target_id = int(args[1])
        setup_mode[event.sender_id] = target_id
        await event.reply(f"✍️ Send the reply message for ID `{target_id}`")

    elif cmd == "/listreplies":
        if reply_db:
            msg = "\n".join([f"{k}: {v}" for k, v in reply_db.items()])
        else:
            msg = "❌ No auto-replies set."
        await event.reply(msg)

    elif cmd == "/delreplyfor" and len(args) == 2:
        target_id = int(args[1])
        if target_id in reply_db:
            reply_db.pop(target_id)
            await event.reply(f"🗑 Deleted reply for {target_id}")
        else:
            await event.reply("❌ No reply found for that ID.")

    elif cmd == "/clearreplies":
        reply_db.clear()
        await event.reply("🗑 All auto-replies cleared.")

    # ---- AFK ----
    elif cmd == "/afk_group" and len(args) > 1:
        afk_msg_group = " ".join(args[1:])
        await event.reply(f"✅ AFK Group set: {afk_msg_group}")

    elif cmd == "/afk_dm" and len(args) > 1:
        afk_msg_dm = " ".join(args[1:])
        await event.reply(f"✅ AFK DM set: {afk_msg_dm}")

    elif cmd == "/afk_all" and len(args) > 1:
        afk_all = True
        msg = " ".join(args[1:])
        afk_msg_group = msg
        afk_msg_dm = msg
        await event.reply(f"✅ AFK All set: {msg}")

    elif cmd == "/afk_off":
        afk_msg_group = None
        afk_msg_dm = None
        afk_all = False
        await event.reply("✅ All AFK disabled")

# ===== Message Handler =====
@client.on(events.NewMessage)
async def auto_reply(event):
    sender = await event.get_sender()
    chat = await event.get_chat()
    global afk_msg_group, afk_msg_dm, afk_all

    # handle owner setup mode for setReplyFor
    if event.sender_id == OWNER_ID:
        if event.sender_id in setup_mode:
            target_id = setup_mode.pop(event.sender_id)
            reply_db[target_id] = event.raw_text
            await event.reply(f"✅ Reply set for {target_id}")
        # cancel AFK if owner sends message
        if not event.is_private:
            afk_msg_group = None
        else:
            afk_msg_dm = None
        return

    # ---- setReplyFor ----
    if event.is_private:
        if sender.id in reply_db:
            last = dm_cooldown.get(sender.id)
            if not last or datetime.now() - last > timedelta(minutes=30):
                msg = format_placeholders(reply_db[sender.id], sender, chat)
                await event.reply(msg)
                dm_cooldown[sender.id] = datetime.now()
    else:
        if chat.id in reply_db:
            if event.is_reply or (client.me and client.me.username and f"@{client.me.username}" in event.raw_text):
                msg = format_placeholders(reply_db[chat.id], sender, chat)
                await event.reply(msg)

    # ---- AFK ----
    # Groups
    if afk_msg_group and not event.is_private:
        if event.is_reply or (client.me and client.me.username and f"@{client.me.username}" in event.raw_text):
            msg = format_placeholders(afk_msg_group, sender, chat)
            await event.reply(msg)
    # DMs
    if afk_msg_dm and event.is_private:
        last = dm_cooldown.get(sender.id)
        if not last or datetime.now() - last > timedelta(minutes=30):
            msg = format_placeholders(afk_msg_dm, sender, chat)
            await event.reply(msg)
            dm_cooldown[sender.id] = datetime.now()

# ===== Run Bot =====
print("🚀 Userbot started...")
client.start()
client.run_until_disconnected()
