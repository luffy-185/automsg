import asyncio
import time
import os
from collections import defaultdict
from typing import Dict, Set, Optional
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel
import json
from datetime import datetime, timedelta
import logging
from flask import Flask
import threading

# Setup logging
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
        # Get environment variables
        self.api_id = int(os.getenv('API_ID'))
        self.api_hash = os.getenv('API_HASH')
        self.session_string = os.getenv('SESSION_STRING')
        
        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
        
        # Bot state
        self.reply_settings: Dict[int, str] = {}
        self.user_message_count: Dict[int, int] = defaultdict(int)
        self.user_last_reply: Dict[int, float] = {}
        self.afk_group_active = False
        self.afk_dm_active = False
        self.afk_message = "Currently offline"
        self.spam_tasks: Set[asyncio.Task] = set()
        self.bot_user_id = None
        self.start_time = time.time()
        self.spam_active = False
        
        # Load settings
        self.load_settings()
        
    def save_settings(self):
        """Save bot settings to file"""
        settings = {
            'reply_settings': self.reply_settings,
            'afk_group_active': self.afk_group_active,
            'afk_dm_active': self.afk_dm_active,
            'afk_message': self.afk_message,
            'spam_active': self.spam_active
        }
        try:
            with open('bot_settings.json', 'w') as f:
                json.dump(settings, f)
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
    
    def load_settings(self):
        """Load bot settings from file"""
        try:
            if os.path.exists('bot_settings.json'):
                with open('bot_settings.json', 'r') as f:
                    settings = json.load(f)
                    self.reply_settings = {int(k): v for k, v in settings.get('reply_settings', {}).items()}
                    self.afk_group_active = settings.get('afk_group_active', False)
                    self.afk_dm_active = settings.get('afk_dm_active', False)
                    self.afk_message = settings.get('afk_message', "Currently offline")
                    self.spam_active = settings.get('spam_active', False)
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
    
    def get_uptime(self):
        """Get bot uptime"""
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
        """Start the bot"""
        try:
            await self.client.start()
            self.bot_user_id = (await self.client.get_me()).id
            logger.info(f"Bot started! User ID: {self.bot_user_id}")
            
            # Register event handlers
            self.client.add_event_handler(self.handle_message, events.NewMessage)
            self.client.add_event_handler(self.handle_outgoing, events.NewMessage(outgoing=True))
            
            logger.info("Bot is running...")
            
            # Keep the bot running
            await self.client.run_until_disconnected()
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            await asyncio.sleep(10)
            await self.start()  # Restart on error
    
    async def handle_outgoing(self, event):
        """Handle outgoing messages to disable AFK when user sends a message"""
        try:
            # Check if this is not an AFK message
            if event.message.text != self.afk_message:
                # Disable AFK if user sends a different message
                if self.afk_group_active or self.afk_dm_active:
                    self.afk_group_active = False
                    self.afk_dm_active = False
                    self.save_settings()
                    logger.info("AFK disabled - user sent a message")
        except Exception as e:
            logger.error(f"Error handling outgoing message: {e}")
    
    async def handle_message(self, event):
        """Handle incoming messages"""
        try:
            if event.is_private:
                await self.handle_dm(event)
            else:
                await self.handle_group_message(event)
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    async def handle_dm(self, event):
        """Handle direct messages"""
        try:
            user_id = event.sender_id
            
            # Check if this is a command from the bot owner
            if user_id == self.bot_user_id:
                await self.handle_command(event)
                return
            
            # If someone else sends a command, ignore it completely
            if event.message.text and event.message.text.startswith('/'):
                return  # Just return, do nothing
            
            # Handle AFK DM for non-command messages
            if self.afk_dm_active:
                current_time = time.time()
                if user_id not in self.user_last_reply or (current_time - self.user_last_reply[user_id]) >= 1800:
                    await event.reply(self.afk_message)
                    self.user_last_reply[user_id] = current_time
            
            # Handle specific user replies for non-command messages
            if user_id in self.reply_settings:
                self.user_message_count[user_id] += 1
                if self.user_message_count[user_id] == 1:
                    await event.reply(self.reply_settings[user_id])
                    asyncio.create_task(self.reset_user_count(user_id))
        except Exception as e:
            logger.error(f"Error handling DM: {e}")
    
    async def reset_user_count(self, user_id):
        """Reset user message count after 30 minutes"""
        await asyncio.sleep(1800)  # 30 minutes
        self.user_message_count[user_id] = 0
    
    async def handle_group_message(self, event):
        """Handle group messages"""
        try:
            chat_id = event.chat_id
            
            # Check if bot is mentioned
            is_mentioned = False
            
            # Check for mentions
            if event.message.mentioned:
                is_mentioned = True
            
            # Check for replies to bot's messages
            if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                try:
                    replied_msg = await event.get_reply_message()
                    if replied_msg and replied_msg.sender_id == self.bot_user_id:
                        is_mentioned = True
                except:
                    pass
            
            if not is_mentioned:
                return
            
            # Handle AFK group
            if self.afk_group_active:
                await event.reply(self.afk_message)
            
            # Handle specific chat replies
            if chat_id in self.reply_settings:
                await event.reply(self.reply_settings[chat_id])
        except Exception as e:
            logger.error(f"Error handling group message: {e}")
    
    async def handle_command(self, event):
        """Handle bot commands"""
        try:
            text = event.message.text.strip()
            
            if text.startswith('/spam '):
                await self.handle_spam_command(event, text)
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
            elif text == '/stop_spam':
                await self.handle_stop_spam_command(event)
        except Exception as e:
            logger.error(f"Error handling command: {e}")
    
    async def handle_help_command(self, event):
        """Handle help command"""
        help_text = """🤖 **Bot Commands:**

**Spam Commands:**
• `/spam <msg> <delay>` - Spam message with delay in all chats
• `/stop_spam` - Stop all spam tasks

**Reply Commands:**
• `/setReplyFor <id> <msg>` - Set auto-reply for chat/user
• `/resetreplyfor <id>` - Remove reply for specific ID  
• `/clear_reply` - Remove all replies
• `/listreply` - List all active replies

**AFK Commands:**
• `/afk_group <msg>` - Enable AFK for groups (mentions only)
• `/afk_group_off` - Disable group AFK
• `/afk_dm <msg>` - Enable AFK for DMs
• `/afk_dm_off` - Disable DM AFK
• `/afk <msg>` - Enable both group & DM AFK
• `/afk_off` - Disable all AFK

**Info Commands:**
• `/help` - Show this help
• `/status` - Show bot status and uptime"""

        await event.reply(help_text)
    
    async def handle_status_command(self, event):
        """Handle status command"""
        uptime = self.get_uptime()
        
        # Count active spam tasks
        active_spam_count = len([task for task in self.spam_tasks if not task.done()])
        
        # Count active replies
        reply_count = len(self.reply_settings)
        
        status_text = f"""📊 **Bot Status:**

**⏱️ Uptime:** {uptime}

**🔄 Spam Status:** {'🟢 Active' if self.spam_active and active_spam_count > 0 else '🔴 Inactive'}
• Active Tasks: {active_spam_count}

**😴 AFK Status:**
• Group AFK: {'🟢 On' if self.afk_group_active else '🔴 Off'}
• DM AFK: {'🟢 On' if self.afk_dm_active else '🔴 Off'}
• Message: "{self.afk_message}"

**💬 Auto-Reply:**
• Active Replies: {reply_count}

**🤖 Bot ID:** {self.bot_user_id}
**📱 Phone:** {self.phone[-4:].rjust(len(self.phone), '*')}"""

        await event.reply(status_text)
    
    async def handle_spam_command(self, event, text):
        """Handle spam command"""
        try:
            parts = text.split(' ', 2)
            if len(parts) < 3:
                await event.reply("Usage: /spam <message> <delay_seconds>")
                return
            
            message = parts[1]
            delay = int(parts[2])
            
            # Stop existing spam tasks
            await self.stop_all_spam_tasks()
            
            # Get all chats
            dialogs = await self.client.get_dialogs()
            
            async def spam_chat(dialog, msg, delay_sec):
                """Spam a specific chat"""
                try:
                    while True:
                        await self.client.send_message(dialog.entity, msg)
                        await asyncio.sleep(delay_sec)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Error spamming {dialog.name}: {e}")
            
            # Start spam tasks for each chat
            spam_count = 0
            for dialog in dialogs:
                if not dialog.is_user or dialog.entity.id != self.bot_user_id:
                    task = asyncio.create_task(spam_chat(dialog, message, delay))
                    self.spam_tasks.add(task)
                    spam_count += 1
            
            self.spam_active = True
            self.save_settings()
            
            await event.reply(f"✅ Started spamming '{message}' with {delay}s delay in {spam_count} chats")
            
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_stop_spam_command(self, event):
        """Handle stop spam command"""
        try:
            stopped_count = await self.stop_all_spam_tasks()
            self.spam_active = False
            self.save_settings()
            await event.reply(f"✅ Stopped {stopped_count} spam tasks")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def stop_all_spam_tasks(self):
        """Stop all spam tasks"""
        stopped_count = 0
        for task in self.spam_tasks:
            if not task.done():
                task.cancel()
                stopped_count += 1
        self.spam_tasks.clear()
        return stopped_count
    
    async def handle_set_reply_command(self, event, text):
        """Handle setReplyFor command"""
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
        """Handle resetreplyfor command"""
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
        """Handle clear_reply command"""
        self.reply_settings.clear()
        self.save_settings()
        await event.reply("✅ All replies cleared")
    
    async def handle_list_reply_command(self, event):
        """Handle listreply command"""
        if not self.reply_settings:
            await event.reply("❌ No active replies")
            return
        
        reply_list = []
        for target_id, message in self.reply_settings.items():
            try:
                entity = await self.client.get_entity(target_id)
                if hasattr(entity, 'username') and entity.username:
                    name = f"@{entity.username}"
                elif hasattr(entity, 'title'):
                    name = entity.title
                else:
                    name = f"ID {target_id}"
                reply_list.append(f"• {name}: {message}")
            except:
                reply_list.append(f"• ID {target_id}: {message}")
        
        await event.reply("📋 **Active Replies:**\n" + "\n".join(reply_list))
    
    async def handle_afk_group_command(self, event, text):
        """Handle afk_group command"""
        try:
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await event.reply("Usage: /afk_group <message>")
                return
            
            self.afk_message = parts[1]
            self.afk_group_active = True
            self.save_settings()
            
            await event.reply(f"✅ AFK group activated with message: {self.afk_message}")
            
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_afk_group_off_command(self, event):
        """Handle afk_group_off command"""
        self.afk_group_active = False
        self.save_settings()
        await event.reply("✅ AFK group deactivated")
    
    async def handle_afk_dm_command(self, event, text):
        """Handle afk_dm command"""
        try:
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await event.reply("Usage: /afk_dm <message>")
                return
            
            self.afk_message = parts[1]
            self.afk_dm_active = True
            self.save_settings()
            
            await event.reply(f"✅ AFK DM activated with message: {self.afk_message}")
            
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_afk_dm_off_command(self, event):
        """Handle afk_dm_off command"""
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ AFK DM deactivated")
    
    async def handle_afk_command(self, event, text):
        """Handle afk command (activate both group and dm)"""
        try:
            parts = text.split(' ', 1)
            if len(parts) >= 2:
                self.afk_message = parts[1]
            else:
                self.afk_message = "Currently offline"
            
            self.afk_group_active = True
            self.afk_dm_active = True
            self.save_settings()
            
            await event.reply(f"✅ AFK activated for groups and DMs with message: {self.afk_message}")
            
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def handle_afk_off_command(self, event):
        """Handle afk_off command"""
        self.afk_group_active = False
        self.afk_dm_active = False
        self.save_settings()
        await event.reply("✅ All AFK modes deactivated")

def run_flask():
    """Run Flask server in a separate thread"""
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

async def main():
    """Main function"""
    # Start Flask server in background thread for health checks
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start the bot
    bot = TelegramBot()
    while True:
        try:
            await bot.start()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            logger.info("Restarting in 10 seconds...")
            await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
