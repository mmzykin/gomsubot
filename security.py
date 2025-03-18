from bson.objectid import ObjectId
import logging
import os
import re
import json
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Union

import motor.motor_asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.dispatcher.handler import CancelHandler
from dotenv import load_dotenv
from aiogram.dispatcher.filters import IDFilter

def is_chat_admin(handler):
    """Decorator to check if user is an admin."""
    admin_ids = [int(id_str) for id_str in ADMIN_CHAT_IDS if id_str.isdigit()]
    return IDFilter(chat_id=admin_ids)(handler)

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SECURITY_SECRET = os.getenv("SECURITY_SECRET", secrets.token_hex(32))
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_IDS", "").split(",")
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "True").lower() == "true"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("security.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('go_club_security')

# Initialize MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client.go_club_db
users_collection = db.users
security_logs_collection = db.security_logs
blocked_users_collection = db.blocked_users
rate_limits_collection = db.rate_limits


class SecurityManager:
    """Handles security-related functions including input validation and rate limiting."""
    
    def __init__(self, bot=None):
        self.bot = bot or Bot(token=API_TOKEN)
        
        # Define patterns for input validation
        self.patterns = {
            "name": re.compile(r'^[A-Za-z0-9\s\-_.]{2,50}$'),
            "rank": re.compile(r'^(3[0]|[1-2][0-9]|[1-9])k$|^([1-9])d$'),
            "ogs_username": re.compile(r'^[A-Za-z0-9\-_.]{3,20}$'),
            "date": re.compile(r'^\d{4}-\d{2}-\d{2}$'),
            "time": re.compile(r'^\d{2}:\d{2}$'),
            "url": re.compile(r'^https?://.+$'),
        }
        
        # Common attack patterns to detect
        self.attack_patterns = [
            re.compile(r'<script.*?>.*?</script>', re.IGNORECASE | re.DOTALL),
            re.compile(r'javascript:', re.IGNORECASE),
            re.compile(r'onload=', re.IGNORECASE),
            re.compile(r'onerror=', re.IGNORECASE),
            re.compile(r'%3Cscript', re.IGNORECASE),  # URL encoded <script
            re.compile(r'%22%3E%3Cscript', re.IGNORECASE),  # URL encoded "><script
            re.compile(r'(?:\'|\").*?(?:OR|AND).*?(?:\'|\")\s*=', re.IGNORECASE),  # SQL injection
            re.compile(r'(?:INSERT|UPDATE|DELETE|DROP|SELECT)\s+(?:FROM|INTO|TABLE)', re.IGNORECASE)  # SQL commands
        ]
    
    async def log_security_event(self, event_type: str, user_id: int, details: Dict, severity: str = "info"):
        """Log a security event to the database."""
        log_entry = {
            "event_type": event_type,
            "user_id": user_id,
            "details": details,
            "severity": severity,
            "timestamp": datetime.now()
        }
        
        try:
            await security_logs_collection.insert_one(log_entry)
            logger.info(f"Security event logged: {event_type} - User: {user_id}")
        except Exception as e:
            logger.error(f"Failed to log security event: {e}")
    
    async def send_admin_alert(self, message: str, level: str = "info"):
        """Send security alerts to admin chat IDs."""
        emoji_map = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🚨",
            "critical": "🆘"
        }
        
        emoji = emoji_map.get(level.lower(), "ℹ️")
        formatted_message = f"{emoji} *Security Alert*\n\n{message}"
        
        for admin_id in ADMIN_CHAT_IDS:
            if admin_id:
                try:
                    await self.bot.send_message(
                        admin_id, 
                        formatted_message, 
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Failed to send alert to admin {admin_id}: {e}")
    
    def validate_input(self, input_type: str, value: str) -> bool:
        """Validate user input against defined patterns."""
        if not value:
            return False
            
        pattern = self.patterns.get(input_type)
        if not pattern:
            return True  # No pattern defined for this input type
            
        return bool(pattern.match(value))
    
    def sanitize_input(self, value: str) -> str:
        """Sanitize user input by removing potentially dangerous characters."""
        if not value:
            return ""
            
        # Basic sanitization
        sanitized = value.replace("<", "&lt;").replace(">", "&gt;")
        sanitized = sanitized.replace("&", "&amp;").replace("\"", "&quot;").replace("'", "&#x27;")
        
        return sanitized
    
    def detect_potential_attack(self, value: str) -> Tuple[bool, str]:
        """Check if input contains potential attack patterns."""
        if not value:
            return False, ""
            
        for pattern in self.attack_patterns:
            match = pattern.search(value)
            if match:
                return True, match.group(0)
                
        return False, ""
    
    async def check_rate_limit(self, user_id: int, action_type: str, limit: int, window_seconds: int) -> bool:
        """
        Check if a user has exceeded their rate limit for a specific action.
        Returns True if within limits, False if exceeded.
        """
        if not RATE_LIMIT_ENABLED:
            return True
            
        try:
            now = datetime.now()
            window_start = now - timedelta(seconds=window_seconds)
            
            # Create a rate limit key
            limit_key = f"{user_id}:{action_type}"
            
            # Get the current count for this user and action
            rate_data = await rate_limits_collection.find_one({"key": limit_key})
            
            if not rate_data:
                # First action, create a new record
                await rate_limits_collection.insert_one({
                    "key": limit_key,
                    "user_id": user_id,
                    "action_type": action_type,
                    "count": 1,
                    "window_start": now,
                    "last_action": now
                })
                return True
                
            # If window has expired, reset counter
            if rate_data["window_start"] < window_start:
                await rate_limits_collection.update_one(
                    {"key": limit_key},
                    {"$set": {
                        "count": 1,
                        "window_start": now,
                        "last_action": now
                    }}
                )
                return True
                
            # Update the counter
            new_count = rate_data["count"] + 1
            await rate_limits_collection.update_one(
                {"key": limit_key},
                {"$set": {
                    "count": new_count,
                    "last_action": now
                }}
            )
            
            # Check if limit exceeded
            if new_count > limit:
                # Log the rate limit violation
                await self.log_security_event(
                    "rate_limit_exceeded",
                    user_id,
                    {
                        "action_type": action_type,
                        "limit": limit,
                        "window_seconds": window_seconds,
                        "count": new_count
                    },
                    severity="warning"
                )
                
                # Only alert admins for significant violations
                if new_count > limit * 2:  # If more than double the limit
                    user = await users_collection.find_one({"telegram_id": user_id})
                    user_name = user.get("name", "Unknown") if user else "Unknown"
                    
                    await self.send_admin_alert(
                        f"Rate limit significantly exceeded:\n"
                        f"User: {user_name} (ID: {user_id})\n"
                        f"Action: {action_type}\n"
                        f"Count: {new_count}/{limit} in {window_seconds} seconds",
                        level="warning"
                    )
                
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking rate limit: {e}")
            # If there's an error, allow the action to proceed
            return True
    
    async def is_user_blocked(self, user_id: int) -> bool:
        """Check if a user is blocked from using the bot."""
        try:
            blocked = await blocked_users_collection.find_one({"user_id": user_id})
            return blocked is not None
        except Exception as e:
            logger.error(f"Error checking if user is blocked: {e}")
            return False
    
    async def block_user(self, user_id: int, reason: str, admin_id: int = None, duration_days: int = None):
        """Block a user from using the bot."""
        try:
            now = datetime.now()
            expiry = now + timedelta(days=duration_days) if duration_days else None
            
            block_data = {
                "user_id": user_id,
                "reason": reason,
                "blocked_at": now,
                "blocked_by": admin_id,
                "expiry": expiry
            }
            
            # Check if already blocked
            existing = await blocked_users_collection.find_one({"user_id": user_id})
            if existing:
                # Update the existing block
                await blocked_users_collection.update_one(
                    {"user_id": user_id},
                    {"$set": block_data}
                )
            else:
                # Create a new block
                await blocked_users_collection.insert_one(block_data)
            
            # Log the block
            await self.log_security_event(
                "user_blocked",
                user_id,
                {
                    "reason": reason,
                    "admin_id": admin_id,
                    "duration_days": duration_days
                },
                severity="warning"
            )
            
            # Get user info
            user = await users_collection.find_one({"telegram_id": user_id})
            user_name = user.get("name", "Unknown") if user else "Unknown"
            
            # Notify admins
            await self.send_admin_alert(
                f"User blocked from bot:\n"
                f"User: {user_name} (ID: {user_id})\n"
                f"Reason: {reason}\n"
                f"Duration: {f'{duration_days} days' if duration_days else 'Permanent'}",
                level="warning"
            )
            
            # Try to notify the user
            try:
                duration_text = f" for {duration_days} days" if duration_days else ""
                await self.bot.send_message(
                    user_id,
                    f"You have been blocked from using this bot{duration_text}.\n"
                    f"Reason: {reason}\n\n"
                    f"If you believe this is in error, please contact the club administrators."
                )
            except Exception as e:
                logger.error(f"Failed to notify user about block: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error blocking user: {e}")
            return False
    
    async def unblock_user(self, user_id: int, admin_id: int = None):
        """Unblock a previously blocked user."""
        try:
            # Remove the block
            result = await blocked_users_collection.delete_one({"user_id": user_id})
            
            if result.deleted_count == 0:
                return False  # User wasn't blocked
            
            # Log the unblock
            await self.log_security_event(
                "user_unblocked",
                user_id,
                {"admin_id": admin_id},
                severity="info"
            )
            
            # Get user info
            user = await users_collection.find_one({"telegram_id": user_id})
            user_name = user.get("name", "Unknown") if user else "Unknown"
            
            # Notify admins
            await self.send_admin_alert(
                f"User unblocked:\n"
                f"User: {user_name} (ID: {user_id})",
                level="info"
            )
            
            # Try to notify the user
            try:
                await self.bot.send_message(
                    user_id,
                    "You have been unblocked and can now use the bot again."
                )
            except Exception as e:
                logger.error(f"Failed to notify user about unblock: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error unblocking user: {e}")
            return False
    
    async def cleanup_expired_blocks(self):
        """Remove blocks that have expired."""
        try:
            now = datetime.now()
            
            # Find expired blocks
            expired = await blocked_users_collection.find({
                "expiry": {"$lt": now, "$ne": None}
            }).to_list(length=None)
            
            for block in expired:
                user_id = block.get("user_id")
                await self.unblock_user(user_id)
            
            return len(expired)
            
        except Exception as e:
            logger.error(f"Error cleaning up expired blocks: {e}")
            return 0
    
    def generate_hmac(self, data: str) -> str:
        """Generate an HMAC signature for data validation."""
        key = SECURITY_SECRET.encode()
        message = data.encode()
        h = hmac.new(key, message, hashlib.sha256)
        return h.hexdigest()
    
    def verify_hmac(self, data: str, signature: str) -> bool:
        """Verify an HMAC signature for data validation."""
        expected = self.generate_hmac(data)
        return hmac.compare_digest(expected, signature)
    
    async def process_suspicious_message(self, message: types.Message, reason: str):
        """Handle potentially malicious messages."""
        user_id = message.from_user.id
        user_name = message.from_user.full_name
        chat_id = message.chat.id
        
        # Log the suspicious activity
        details = {
            "message_id": message.message_id,
            "chat_id": chat_id,
            "text": message.text if message.text else "[NO TEXT]",
            "reason": reason
        }
        
        await self.log_security_event(
            "suspicious_message",
            user_id,
            details,
            severity="warning"
        )
        
        # Check how many suspicious messages this user has sent recently
        recent_suspicious = await security_logs_collection.count_documents({
            "event_type": "suspicious_message",
            "user_id": user_id,
            "timestamp": {"$gt": datetime.now() - timedelta(hours=24)}
        })
        
        # If this is a repeated offense, consider blocking the user
        if recent_suspicious >= 3:
            await self.block_user(
                user_id, 
                f"Automated block: Multiple suspicious messages ({reason})", 
                duration_days=1
            )
            
            try:
                # Delete the suspicious message if possible
                await self.bot.delete_message(chat_id, message.message_id)
            except Exception as e:
                logger.error(f"Failed to delete suspicious message: {e}")
        
        # Alert admins for manual review
        await self.send_admin_alert(
            f"Suspicious message detected:\n"
            f"User: {user_name} (ID: {user_id})\n"
            f"Reason: {reason}\n"
            f"Content: {message.text if message.text else '[NO TEXT]'}\n"
            f"Recent suspicious activity count: {recent_suspicious}",
            level="warning"
        )


class SecurityMiddleware(BaseMiddleware):
    """Middleware to handle security checks for all incoming messages."""
    
    def __init__(self):
        super(SecurityMiddleware, self).__init__()
        self.security = SecurityManager()
    
    async def on_pre_process_message(self, message: types.Message, data: dict):
        """Run security checks before processing any message."""
        user_id = message.from_user.id
        
        # Check if user is blocked
        if await self.security.is_user_blocked(user_id):
            # Log the attempt
            await self.security.log_security_event(
                "blocked_user_attempt",
                user_id,
                {"message_id": message.message_id},
                severity="info"
            )
            
            # Send a notification that they're blocked
            await message.reply(
                "You are currently blocked from using this bot. "
                "Please contact the club administrators if you believe this is in error."
            )
            
            # Stop processing the message
            raise CancelHandler()
        
        # Check rate limits for messages
        if not await self.security.check_rate_limit(user_id, "message", 30, 60):  # 30 messages per minute
            await message.reply(
                "You're sending messages too quickly. Please slow down."
            )
            raise CancelHandler()
        
        # Check for potential attacks in text messages
        if message.text:
            is_attack, pattern = self.security.detect_potential_attack(message.text)
            if is_attack:
                await self.security.process_suspicious_message(
                    message, 
                    f"Potential attack pattern detected: {pattern}"
                )
                await message.reply(
                    "Your message contains potentially harmful content and will not be processed."
                )
                raise CancelHandler()
        
        # Update user's last activity timestamp
        try:
            await users_collection.update_one(
                {"telegram_id": user_id},
                {"$set": {"last_activity": datetime.now()}}
            )
        except Exception as e:
            logger.error(f"Failed to update user activity: {e}")
    
    async def on_pre_process_callback_query(self, callback_query: types.CallbackQuery, data: dict):
        """Run security checks before processing callback queries."""
        user_id = callback_query.from_user.id
        
        # Check if user is blocked
        if await self.security.is_user_blocked(user_id):
            # Log the attempt
            await self.security.log_security_event(
                "blocked_user_callback_attempt",
                user_id,
                {"callback_data": callback_query.data},
                severity="info"
            )
            
            # Send a notification that they're blocked
            await callback_query.answer(
                "You are currently blocked from using this bot.", 
                show_alert=True
            )
            
            # Stop processing the callback
            raise CancelHandler()
        
        # Check rate limits for callbacks
        if not await self.security.check_rate_limit(user_id, "callback", 20, 60):  # 20 callbacks per minute
            await callback_query.answer(
                "You're interacting too quickly. Please slow down.", 
                show_alert=True
            )
            raise CancelHandler()
        
        # Update user's last activity timestamp
        try:
            await users_collection.update_one(
                {"telegram_id": user_id},
                {"$set": {"last_activity": datetime.now()}}
            )
        except Exception as e:
            logger.error(f"Failed to update user activity: {e}")


async def setup_security_for_bot(dp: Dispatcher):
    """Configure and attach security features to the bot."""
    # Initialize the security module
    security = SecurityManager(dp.bot)
    
    # Add security middleware
    dp.middleware.setup(SecurityMiddleware())
    
    # Register security-related command handlers
    
    @dp.message_handler(commands=['block'], is_chat_admin=True)
    async def cmd_block_user(message: types.Message):
        """Command to block a user from using the bot. Admin only."""
        args = message.get_args().split()
        
        if len(args) < 2:
            await message.reply(
                "Usage: /block [user_id] [duration_days or 'permanent'] [reason]"
            )
            return
        
        try:
            user_id = int(args[0])
            
            if args[1].lower() == 'permanent':
                duration_days = None
            else:
                duration_days = int(args[1])
                
            reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"
            
            # Check if the user exists
            user = await users_collection.find_one({"telegram_id": user_id})
            if not user:
                await message.reply(f"User with ID {user_id} not found in the database.")
                return
            
            # Block the user
            success = await security.block_user(
                user_id, 
                reason, 
                admin_id=message.from_user.id,
                duration_days=duration_days
            )
            
            if success:
                await message.reply(
                    f"User {user.get('name', user_id)} has been blocked "
                    f"{'permanently' if duration_days is None else f'for {duration_days} days'}.\n"
                    f"Reason: {reason}"
                )
            else:
                await message.reply("Failed to block user. Check logs for details.")
                
        except ValueError:
            await message.reply("Invalid user ID or duration. Please use numeric values.")
        except Exception as e:
            logger.error(f"Error in block command: {e}")
            await message.reply(f"An error occurred: {str(e)}")
    
    @dp.message_handler(commands=['unblock'], is_chat_admin=True)
    async def cmd_unblock_user(message: types.Message):
        """Command to unblock a previously blocked user. Admin only."""
        args = message.get_args().split()
        
        if not args:
            await message.reply("Usage: /unblock [user_id]")
            return
        
        try:
            user_id = int(args[0])
            
            # Unblock the user
            success = await security.unblock_user(
                user_id,
                admin_id=message.from_user.id
            )
            
            if success:
                # Get user info
                user = await users_collection.find_one({"telegram_id": user_id})
                user_name = user.get("name", "Unknown") if user else str(user_id)
                
                await message.reply(f"User {user_name} has been unblocked.")
            else:
                await message.reply("User is not currently blocked or an error occurred.")
                
        except ValueError:
            await message.reply("Invalid user ID. Please use a numeric value.")
        except Exception as e:
            logger.error(f"Error in unblock command: {e}")
            await message.reply(f"An error occurred: {str(e)}")
    
    @dp.message_handler(commands=['security_status'], is_chat_admin=True)
    async def cmd_security_status(message: types.Message):
        """Command to check security status and metrics. Admin only."""
        try:
            # Get blocked users count
            blocked_count = await blocked_users_collection.count_documents({})
            
            # Get recent suspicious events
            recent_suspicious = await security_logs_collection.count_documents({
                "event_type": "suspicious_message",
                "timestamp": {"$gt": datetime.now() - timedelta(days=1)}
            })
            
            # Get recent rate limit violations
            rate_limit_violations = await security_logs_collection.count_documents({
                "event_type": "rate_limit_exceeded",
                "timestamp": {"$gt": datetime.now() - timedelta(days=1)}
            })
            
            # Format the status message
            status = (
                "📊 *Security Status Report*\n\n"
                f"🚫 *Blocked Users:* {blocked_count}\n"
                f"⚠️ *Suspicious Messages (24h):* {recent_suspicious}\n"
                f"🔄 *Rate Limit Violations (24h):* {rate_limit_violations}\n\n"
                f"Security module is active and functioning normally."
            )
            
            await message.reply(status, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Error in security_status command: {e}")
            await message.reply(f"An error occurred: {str(e)}")
    
    # Run periodic security tasks
    async def periodic_security_tasks():
        """Run security tasks on a schedule."""
        while True:
            try:
                # Clean up expired blocks
                num_unblocked = await security.cleanup_expired_blocks()
                if num_unblocked > 0:
                    logger.info(f"Cleaned up {num_unblocked} expired blocks")
                
                # Wait 1 hour before next check
                await asyncio.sleep(60 * 60)
                
            except Exception as e:
                logger.error(f"Error in periodic security tasks: {e}")
                await asyncio.sleep(300)  # Wait 5 minutes if an error occurs
    
    # Start the periodic tasks
    asyncio.create_task(periodic_security_tasks())
    
    return security


if __name__ == "__main__":
    """Run security module directly for testing or maintenance."""
    async def test_security():
        security = SecurityManager()
        
        # Test input validation
        test_inputs = {
            "name": ["John Doe", "a", "John123", "<script>alert('xss')</script>"],
            "rank": ["3k", "1d", "30k", "10dan", "invalid"],
            "ogs_username": ["player123", "p", "player-name", "player@name"],
            "date": ["2023-01-01", "23-01-01", "2023/01/01", "invalid"],
            "time": ["10:30", "25:00", "10-30", "invalid"]
        }
        
        print("Testing input validation:")
        for input_type, values in test_inputs.items():
            print(f"\n{input_type.upper()}:")
            for value in values:
                result = security.validate_input(input_type, value)
                attack, pattern = security.detect_potential_attack(value)
                print(f"  '{value}': Valid: {result}, Attack: {attack} {f'({pattern})' if attack else ''}")
        
        # Test HMAC generation
        test_data = "test_security_data"
        hmac_sig = security.generate_hmac(test_data)
        print(f"\nHMAC Test: {hmac_sig}")
        print(f"Verify valid: {security.verify_hmac(test_data, hmac_sig)}")
        print(f"Verify invalid: {security.verify_hmac(test_data, hmac_sig[:-1] + '0')}")
    
    # Run the test
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(test_security())
