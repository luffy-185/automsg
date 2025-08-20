import os
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Set
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChatAdminRequiredError

# ===== Logging Setup =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== Keep Alive =====
try:
    from keep_alive import keep_alive
    keep_alive()
    logger.info("Keep-alive service started")
except ImportError:
    logger.warning("keep_alive.py not found - running without keepalive")

# ===== Config Validation =====
def validate_config():
    """Validate required environment variables"""
    required_vars = ["API_ID", "API_HASH", "SESSION", "OWNER_ID"]
    missing = []
    
    for var in required_vars:
        if not os.environ.get(var):
            missing.append(var)
    
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

validate_config()

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION = os.environ.get("SESSION")
OWNER_ID = int(os.environ.get("OWNER_ID"))

# Rate limiting settings
MAX_MESSAGE_LENGTH = 4096
SPAM_DELAY_MIN = 1  # minimum 1 second delay
SPAM_DELAY_MAX = 3600  # maximum 1 hour delay
DM_COOLDOWN_MINUTES = 30

# Initialize client with session recovery
client = None

def create_client(use_session=True):
    """Create Telegram client with optional session recovery"""
    global client
    try:
        if use_session and SESSION:
            logger.info("Creating client with existing session...")
            client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
        else:
            logger.info("Creating client with fresh session...")
            session_name = f"userbot_session_{OWNER_ID}"
            client = TelegramClient(session_name, API_ID, API_HASH)
        
        # Set client parameters for better stability
        client.flood_sleep_threshold = 60
        return client
    except Exception as e:
        logger.error(f"Error creating client: {e}")
        return None

# Create initial client
client = create_client()

# ===== Globals with Type Hints =====
start_time = datetime.now()
spam_tasks: Dict[int, asyncio.Task] = {}
reply_db: Dict[int, str] = {}
dm_cooldown: Dict[int, datetime] = {}
setup_mode: Dict[int, int] = {}
afk_msg_group: Optional[str] = None
afk_msg_dm: Optional[str] = None
afk_all: bool = False
blocked_chats: Set[int] = set()  # Chats where spam is blocked
afk_disabled_groups: Set[int] = set()  # Groups where AFK is temporarily disabled
afk_disabled_users: Set[int] = set()   # Users where AFK DM is temporarily disabled

# ===== Utility Functions =====
def is_owner(sender_id: int) -> bool:
    """Check if sender is the bot owner"""
    return sender_id == OWNER_ID

def validate_message_length(text: str) -> bool:
    """Validate message length to prevent issues"""
    return len(text) <= MAX_MESSAGE_LENGTH

def sanitize_text(text: str) -> str:
    """Basic text sanitization"""
    # Remove potential harmful characters
    return text.replace('\x00', '').strip()[:MAX_MESSAGE_LENGTH]

def format_placeholders(text: str, sender, chat) -> str:
    """Format message placeholders safely"""
    try:
        formatted_text = text
        
        if "{name}" in formatted_text:
            name = getattr(sender, 'first_name', '') or 'Unknown'
            formatted_text = formatted_text.replace("{name}", sanitize_text(name))
            
        if "{id}" in formatted_text:
            formatted_text = formatted_text.replace("{id}", str(sender.id))
            
        if "{chat}" in formatted_text and chat:
            chat_name = getattr(chat, "title", "Private Chat")
            formatted_text = formatted_text.replace("{chat}", sanitize_text(chat_name))
            
        return sanitize_text(formatted_text)
    except Exception as e:
        logger.error(f"Error formatting placeholders: {e}")
        return "Error formatting message"

def uptime() -> str:
    """Get bot uptime"""
    delta = datetime.now() - start_time
    return str(delta).split(".")[0]

async def get_entity_info(client, identifier):
    """Get entity info from username or ID with robust error handling"""
    try:
        # Handle string identifiers (usernames)
        if isinstance(identifier, str):
            if identifier.startswith('@'):
                identifier = identifier[1:]
            
            # Try to get entity with retries for session issues
            for attempt in range(3):
                try:
                    entity = await client.get_entity(identifier)
                    break
                except Exception as e:
                    if attempt == 2:  # Last attempt
                        logger.error(f"Failed to get entity after 3 attempts for {identifier}: {e}")
                        return None, None
                    logger.warning(f"Attempt {attempt + 1} failed for {identifier}: {e}")
                    await asyncio.sleep(1)
        else:
            # Handle numeric IDs
            try:
                entity_id = int(identifier)
                for attempt in range(3):
                    try:
                        entity = await client.get_entity(entity_id)
                        break
                    except Exception as e:
                        if attempt == 2:  # Last attempt
                            logger.error(f"Failed to get entity after 3 attempts for ID {entity_id}: {e}")
                            return None, None
                        logger.warning(f"Attempt {attempt + 1} failed for ID {entity_id}: {e}")
                        await asyncio.sleep(1)
            except ValueError:
                logger.error(f"Invalid identifier format: {identifier}")
                return None, None
        
        # Extract display name safely
        if hasattr(entity, 'username') and entity.username:
            display_name = f"@{entity.username}"
        elif hasattr(entity, 'title') and entity.title:
            display_name = entity.title
        elif hasattr(entity, 'first_name') and entity.first_name:
            name_parts = [entity.first_name]
            if hasattr(entity, 'last_name') and entity.last_name:
                name_parts.append(entity.last_name)
            display_name = " ".join(name_parts)
        else:
            display_name = f"ID: {entity.id}"
            
        return entity.id, display_name
        
    except Exception as e:
        logger.error(f"Critical error getting entity info for {identifier}: {e}")
        # Return fallback for numeric IDs
        if isinstance(identifier, (int, str)) and str(identifier).isdigit():
            return int(identifier), f"ID: {identifier}"
        return None, None

def validate_spam_params(message: str, delay: int) -> tuple[bool, str]:
    """Validate spam parameters"""
    if not validate_message_length(message):
        return False, f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"
    
    if delay < SPAM_DELAY_MIN:
        return False, f"Delay too short (minimum {SPAM_DELAY_MIN} seconds)"
        
    if delay > SPAM_DELAY_MAX:
        return False, f"Delay too long (maximum {SPAM_DELAY_MAX} seconds)"
    
    return True, "Valid"

# ===== Enhanced Command Handler =====
@client.on(events.NewMessage(pattern=r"^/"))
async def command_handler(event):
    """Handle bot commands with improved error handling"""
    if not is_owner(event.sender_id):
        return

    try:
        chat_id = event.chat_id
        args = event.raw_text.split()
        cmd = args[0].lower()

        global afk_msg_group, afk_msg_dm, afk_all

        # ---- Enhanced Spam Command ----
        if cmd == "/spam":
            if len(args) == 2 and args[1].lower() == "off":
                await stop_spam(chat_id, event)
            elif len(args) >= 3:
                await start_spam(args, chat_id, event)
            else:
                await event.reply("❌ Usage: /spam <message> <delay_seconds> OR /spam off")

        # ---- Status Command ----
        elif cmd == "/status":
            await show_status(event)

        # ---- Help Command ----
        elif cmd == "/help":
            await show_help(event)

        # ---- Reply Management ----
        elif cmd == "/setreplyfor" and len(args) == 2:
            await setup_reply(args[1], event)

        elif cmd == "/listreplies":
            await list_replies(event)

        elif cmd == "/delreplyfor" and len(args) == 2:
            await delete_reply(args[1], event)

        elif cmd == "/clearreplies":
            await clear_replies(event)

        # ---- AFK Commands ----
        elif cmd == "/afk_group" and len(args) > 1:
            afk_msg_group = sanitize_text(" ".join(args[1:]))
            afk_disabled_groups.clear()  # Re-enable for all groups
            await event.reply(f"✅ AFK Group set: {afk_msg_group}")

        elif cmd == "/afk_dm" and len(args) > 1:
            afk_msg_dm = sanitize_text(" ".join(args[1:]))
            afk_disabled_users.clear()  # Re-enable for all users
            await event.reply(f"✅ AFK DM set: {afk_msg_dm}")

        elif cmd == "/afk_all" and len(args) > 1:
            afk_all = True
            msg = sanitize_text(" ".join(args[1:]))
            afk_msg_group = msg
            afk_msg_dm = msg
            afk_disabled_groups.clear()
            afk_disabled_users.clear()
            await event.reply(f"✅ AFK All set: {msg}")

        elif cmd == "/afk_off":
            afk_msg_group = None
            afk_msg_dm = None
            afk_all = False
            afk_disabled_groups.clear()
            afk_disabled_users.clear()
            await event.reply("✅ All AFK disabled")

        elif cmd == "/afk_group_off":
            afk_msg_group = None
            afk_disabled_groups.clear()
            if afk_all:
                afk_all = False
            await event.reply("✅ AFK Group disabled")

        elif cmd == "/afk_dm_off":
            afk_msg_dm = None
            afk_disabled_users.clear()
            if afk_all:
                afk_all = False
            await event.reply("✅ AFK DM disabled")

        # ---- Check AFK Status ----
        elif cmd == "/afkgroup":
            if afk_msg_group:
                disabled_count = len(afk_disabled_groups)
                await event.reply(f"📱 AFK Group: `{afk_msg_group}`\n🚫 Disabled in {disabled_count} chats")
            else:
                await event.reply("❌ AFK Group is OFF")

        elif cmd == "/afkdm":
            if afk_msg_dm:
                disabled_count = len(afk_disabled_users)
                await event.reply(f"💬 AFK DM: `{afk_msg_dm}`\n🚫 Disabled for {disabled_count} users")
            else:
                await event.reply("❌ AFK DM is OFF")

        # ---- Enhanced Reply List ----
        elif cmd == "/replylist":
            await enhanced_reply_list(event)

        # ---- Block/Unblock Chat ----
        elif cmd == "/block":
            blocked_chats.add(chat_id)
            await event.reply("🚫 Chat blocked from spam")

        elif cmd == "/unblock":
            blocked_chats.discard(chat_id)
            await event.reply("✅ Chat unblocked")

        else:
            await event.reply("❌ Unknown command. Use /help for available commands.")

    except Exception as e:
        logger.error(f"Error in command handler: {e}")
        await event.reply(f"❌ Error processing command: {str(e)}")

# ===== Command Helper Functions =====
async def stop_spam(chat_id: int, event):
    """Stop spam in a chat"""
    task = spam_tasks.pop(chat_id, None)
    if task and not task.cancelled():
        task.cancel()
        await event.reply("✅ Spam stopped in this chat.")
        logger.info(f"Spam stopped in chat {chat_id}")
    else:
        await event.reply("⚠️ No spam running here.")

async def start_spam(args: list, chat_id: int, event):
    """Start spam in a chat with validation"""
    if chat_id in blocked_chats:
        await event.reply("🚫 Spam is blocked in this chat")
        return

    msg = " ".join(args[1:-1])
    try:
        delay = int(args[-1])
    except ValueError:
        await event.reply("❌ Invalid delay - must be a number")
        return

    valid, error_msg = validate_spam_params(msg, delay)
    if not valid:
        await event.reply(f"❌ {error_msg}")
        return

    # Stop existing spam
    if chat_id in spam_tasks:
        spam_tasks[chat_id].cancel()

    async def spam_loop():
        count = 0
        try:
            while True:
                await client.send_message(chat_id, msg)
                count += 1
                logger.debug(f"Spam message {count} sent to chat {chat_id}")
                await asyncio.sleep(delay)
        except FloodWaitError as e:
            logger.warning(f"Rate limited for {e.seconds} seconds in chat {chat_id}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error in spam loop for chat {chat_id}: {e}")

    spam_tasks[chat_id] = asyncio.create_task(spam_loop())
    await event.reply(f"✅ Spamming `{msg[:50]}{'...' if len(msg) > 50 else ''}` every {delay}s")
    logger.info(f"Spam started in chat {chat_id} with delay {delay}s")

async def show_status(event):
    """Show bot status"""
    active_spams = ", ".join(str(cid) for cid in spam_tasks.keys()) or "None"
    reply_count = len(reply_db)
    blocked_count = len(blocked_chats)
    
    msg = (
        f"🤖 **Bot Status**\n"
        f"⏱ Uptime: `{uptime()}`\n"
        f"💬 Active spams: `{active_spams}`\n"
        f"🤖 Auto-replies: `{reply_count}`\n"
        f"🚫 Blocked chats: `{blocked_count}`\n"
        f"📱 AFK Group: `{afk_msg_group[:30] + '...' if afk_msg_group and len(afk_msg_group) > 30 else afk_msg_group or '❌ Off'}`\n"
        f"💬 AFK DM: `{afk_msg_dm[:30] + '...' if afk_msg_dm and len(afk_msg_dm) > 30 else afk_msg_dm or '❌ Off'}`\n"
        f"🌐 AFK All: `{'✅' if afk_all else '❌'}`"
    )
    await event.reply(msg)

async def show_help(event):
    """Show help message"""
    help_text = """
📌 **Available Commands:**

**Spam Management:**
• `/spam <message> <delay>` - Start spam in current chat
• `/spam off` - Stop spam in current chat
• `/block` - Block spam in current chat
• `/unblock` - Unblock spam in current chat

**Auto-Reply:**
• `/setReplyFor <id/@username>` - Set auto-reply for user/chat
• `/listReplies` or `/replylist` - Show all saved replies with names
• `/delReplyFor <id/@username>` - Delete specific auto-reply
• `/clearReplies` - Clear all auto-replies

**AFK System:**
• `/afk_group <message>` - AFK for all groups
• `/afk_dm <message>` - AFK for all DMs
• `/afk_all <message>` - AFK for both groups and DMs
• `/afk_off` - Turn off all AFK
• `/afk_group_off` - Turn off only group AFK
• `/afk_dm_off` - Turn off only DM AFK

**AFK Status Check:**
• `/afkgroup` - Check group AFK status
• `/afkdm` - Check DM AFK status

**Utilities:**
• `/status` - Show bot status and uptime
• `/help` - Show this help message

**Placeholders:** `{name}`, `{id}`, `{chat}`

**Note:** When you send a message in a group/DM while AFK is active, AFK will be temporarily disabled for that specific chat/user until you set it again.
"""
    await event.reply(help_text)

async def setup_reply(target_identifier: str, event):
    """Setup auto-reply for a target (supports both ID and username)"""
    try:
        target_id, display_name = await get_entity_info(client, target_identifier)
        
        if target_id is None:
            await event.reply(f"❌ Could not find user/chat: `{target_identifier}`")
            return
            
        setup_mode[event.sender_id] = target_id
        await event.reply(f"✍️ Send the reply message for **{display_name}** (`{target_id}`)\n(Use /cancel to abort)")
        
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")

async def enhanced_reply_list(event):
    """Enhanced reply list with usernames and chat names"""
    if not reply_db:
        await event.reply("❌ No auto-replies set.")
        return
        
    msg = "📝 **Auto-Replies:**\n"
    for target_id, reply_text in reply_db.items():
        try:
            # Try to get the entity info for better display
            entity_id, display_name = await get_entity_info(client, target_id)
            if display_name and entity_id:
                identifier = f"{display_name}"
            else:
                identifier = f"ID: {target_id}"
        except Exception as e:
            logger.warning(f"Could not get info for {target_id}: {e}")
            identifier = f"ID: {target_id}"
            
        preview = reply_text[:50] + "..." if len(reply_text) > 50 else reply_text
        msg += f"• **{identifier}**: {preview}\n"
    
    await event.reply(msg)

async def list_replies(event):
    """List all auto-replies (legacy function - redirects to enhanced)"""
    await enhanced_reply_list(event)

async def delete_reply(target_identifier: str, event):
    """Delete an auto-reply (supports both ID and username)"""
    try:
        target_id, display_name = await get_entity_info(client, target_identifier)
        
        if target_id is None:
            await event.reply(f"❌ Could not find user/chat: `{target_identifier}`")
            return
            
        if target_id in reply_db:
            reply_db.pop(target_id)
            await event.reply(f"🗑 Deleted reply for **{display_name}** (`{target_id}`)")
        else:
            await event.reply(f"❌ No reply found for **{display_name}** (`{target_id}`)")
            
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")

async def clear_replies(event):
    """Clear all auto-replies"""
    count = len(reply_db)
    reply_db.clear()
    await event.reply(f"🗑 Cleared {count} auto-replies")

# ===== Enhanced Message Handler =====
@client.on(events.NewMessage)
async def auto_reply(event):
    """Handle incoming messages with improved error handling"""
    try:
        # Add safety checks for event object
        if not event or not hasattr(event, 'sender_id'):
            logger.warning("Received invalid event object")
            return
            
        # Safe entity retrieval with error handling
        sender = None
        chat = None
        
        try:
            sender = await event.get_sender()
        except Exception as e:
            logger.error(f"Error getting sender: {e}")
            # Try to get basic info from event
            sender_id = getattr(event, 'sender_id', None)
            if not sender_id:
                return
            # Create a minimal sender object
            class MinimalSender:
                def __init__(self, id):
                    self.id = id
                    self.first_name = "Unknown"
            sender = MinimalSender(sender_id)
            
        try:
            chat = await event.get_chat()
        except Exception as e:
            logger.error(f"Error getting chat: {e}")
            # Create minimal chat object
            class MinimalChat:
                def __init__(self, id):
                    self.id = getattr(event, 'chat_id', id)
                    self.title = "Unknown Chat"
            chat = MinimalChat(getattr(event, 'chat_id', sender.id))

        if not sender:
            logger.warning("Could not get sender information")
            return

        global afk_msg_group, afk_msg_dm

        # Handle owner setup mode for setReplyFor
        if event.sender_id == OWNER_ID:
            if event.sender_id in setup_mode:
                if event.raw_text.strip().lower() == "/cancel":
                    setup_mode.pop
