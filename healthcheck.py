import logging
import os
import time
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
import motor.motor_asyncio
from aiogram import Bot
from aiogram.utils.exceptions import (
    TelegramAPIError, 
    BotBlocked, 
    UserDeactivated,
    ChatNotFound
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OGS_API_URL = "https://online-go.com/api/v1"
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_IDS", "").split(",")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("healthcheck.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('go_club_healthcheck')

# Initialize MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client.go_club_db
users_collection = db.users
events_collection = db.events
matches_collection = db.matches
subscriptions_collection = db.subscriptions
health_logs_collection = db.health_logs

class HealthCheck:
    """Monitors and checks the health of the Go Club bot system."""
    
    def __init__(self):
        self.bot = Bot(token=API_TOKEN)
        self.start_time = datetime.now()
        self.api_call_counts = {
            "telegram": 0,
            "ogs": 0
        }
        self.api_errors = {
            "telegram": 0,
            "ogs": 0
        }
        self.inactive_users = set()
        
    async def log_health_check(self, check_type: str, details: Dict, status: str = "ok"):
        """Log a health check to the database."""
        log_entry = {
            "type": check_type,
            "details": details,
            "status": status,
            "timestamp": datetime.now()
        }
        
        try:
            await health_logs_collection.insert_one(log_entry)
            logger.debug(f"Health check logged: {check_type} - {status}")
        except Exception as e:
            logger.error(f"Failed to log health check: {e}")
    
    async def send_admin_alert(self, message: str, level: str = "info"):
        """Send alert messages to admin chat IDs."""
        emoji_map = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🚨",
            "success": "✅"
        }
        
        emoji = emoji_map.get(level.lower(), "ℹ️")
        formatted_message = f"{emoji} *Health Alert*\n\n{message}"
        
        for admin_id in ADMIN_CHAT_IDS:
            if admin_id:
                try:
                    await self.bot.send_message(
                        admin_id, 
                        formatted_message, 
                        parse_mode="Markdown"
                    )
                    self.api_call_counts["telegram"] += 1
                except Exception as e:
                    self.api_errors["telegram"] += 1
                    logger.error(f"Failed to send alert to admin {admin_id}: {e}")
    
    async def check_telegram_api(self):
        """Check if the Telegram Bot API is working properly."""
        start_time = time.time()
        try:
            # Get bot info as a simple API check
            me = await self.bot.get_me()
            response_time = time.time() - start_time
            
            details = {
                "bot_username": me.username,
                "bot_id": me.id,
                "response_time_ms": int(response_time * 1000)
            }
            
            await self.log_health_check("telegram_api", details)
            logger.info(f"Telegram API check: OK ({details['response_time_ms']}ms)")
            self.api_call_counts["telegram"] += 1
            return True
            
        except TelegramAPIError as e:
            response_time = time.time() - start_time
            error_details = {
                "error": str(e),
                "response_time_ms": int(response_time * 1000)
            }
            
            await self.log_health_check("telegram_api", error_details, status="error")
            logger.error(f"Telegram API check failed: {e}")
            
            await self.send_admin_alert(
                f"Telegram API check failed: {str(e)}\n"
                f"Response time: {int(response_time * 1000)}ms",
                level="error"
            )
            self.api_errors["telegram"] += 1
            return False
    
    async def check_mongodb_connection(self):
        """Check if the MongoDB connection is working properly."""
        start_time = time.time()
        try:
            # Ping the database
            result = await db.command("ping")
            response_time = time.time() - start_time
            
            details = {
                "response_time_ms": int(response_time * 1000),
                "status": result
            }
            
            await self.log_health_check("mongodb", details)
            logger.info(f"MongoDB check: OK ({details['response_time_ms']}ms)")
            return True
            
        except Exception as e:
            response_time = time.time() - start_time
            error_details = {
                "error": str(e),
                "response_time_ms": int(response_time * 1000)
            }
            
            await self.log_health_check("mongodb", error_details, status="error")
            logger.error(f"MongoDB check failed: {e}")
            
            await self.send_admin_alert(
                f"MongoDB connection check failed: {str(e)}\n"
                f"Response time: {int(response_time * 1000)}ms",
                level="error"
            )
            return False
    
    async def check_ogs_api(self):
        """Check if the Online Go Server API is working properly."""
        start_time = time.time()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{OGS_API_URL}/ui/config") as resp:
                    response_time = time.time() - start_time
                    
                    if resp.status != 200:
                        raise Exception(f"API returned status code {resp.status}")
                    
                    data = await resp.json()
                    
                    details = {
                        "response_time_ms": int(response_time * 1000),
                        "status_code": resp.status
                    }
                    
                    await self.log_health_check("ogs_api", details)
                    logger.info(f"OGS API check: OK ({details['response_time_ms']}ms)")
                    self.api_call_counts["ogs"] += 1
                    return True
                    
        except Exception as e:
            response_time = time.time() - start_time
            error_details = {
                "error": str(e),
                "response_time_ms": int(response_time * 1000)
            }
            
            await self.log_health_check("ogs_api", error_details, status="error")
            logger.error(f"OGS API check failed: {e}")
            
            await self.send_admin_alert(
                f"OGS API check failed: {str(e)}\n"
                f"Response time: {int(response_time * 1000)}ms\n"
                f"This may affect user rank synchronization and stats.",
                level="warning"
            )
            self.api_errors["ogs"] += 1
            return False
    
    async def check_inactive_users(self, days: int = 30):
        """Check for users who haven't interacted with the bot in a while."""
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            # Find users who haven't logged in recently
            inactive_pipeline = [
                {
                    "$match": {
                        "last_activity": {"$lt": cutoff_date}
                    }
                },
                {
                    "$project": {
                        "telegram_id": 1,
                        "name": 1,
                        "days_inactive": {
                            "$divide": [
                                {"$subtract": [datetime.now(), "$last_activity"]},
                                1000 * 60 * 60 * 24  # Convert ms to days
                            ]
                        }
                    }
                }
            ]
            
            inactive_users = await users_collection.aggregate(inactive_pipeline).to_list(length=None)
            
            details = {
                "inactive_user_count": len(inactive_users),
                "days_threshold": days
            }
            
            await self.log_health_check("inactive_users", details)
            
            if inactive_users:
                inactive_list = "\n".join([
                    f"• {user['name']} - {int(user['days_inactive'])} days"
                    for user in inactive_users[:10]  # Show only first 10
                ])
                
                if len(inactive_users) > 10:
                    inactive_list += f"\n...and {len(inactive_users) - 10} more"
                
                await self.send_admin_alert(
                    f"Found {len(inactive_users)} users inactive for more than {days} days:\n\n"
                    f"{inactive_list}\n\n"
                    f"Consider sending a re-engagement message.",
                    level="info"
                )
                
                # Update our set of inactive users
                self.inactive_users = set(user["telegram_id"] for user in inactive_users)
            
            return inactive_users
            
        except Exception as e:
            error_details = {"error": str(e)}
            await self.log_health_check("inactive_users", error_details, status="error")
            logger.error(f"Failed to check inactive users: {e}")
            return []
    
    async def verify_user_communications(self, sample_size: int = 5):
        """Test if the bot can communicate with a sample of users."""
        try:
            # Get a sample of recent users
            recent_users = await users_collection.find().sort(
                "last_activity", -1
            ).limit(sample_size).to_list(length=None)
            
            if not recent_users:
                logger.warning("No users found to verify communications")
                return True
            
            blocked_count = 0
            deactivated_count = 0
            success_count = 0
            
            for user in recent_users:
                user_id = user.get("telegram_id")
                if not user_id:
                    continue
                
                try:
                    # Try to get chat info as a lightweight check
                    await self.bot.get_chat(user_id)
                    success_count += 1
                    
                except BotBlocked:
                    blocked_count += 1
                    logger.info(f"User {user_id} has blocked the bot")
                    
                except UserDeactivated:
                    deactivated_count += 1
                    logger.info(f"User {user_id} has deactivated their account")
                    
                except ChatNotFound:
                    logger.info(f"Chat with user {user_id} not found")
                    
                except Exception as e:
                    logger.error(f"Error checking communication with user {user_id}: {e}")
                
                self.api_call_counts["telegram"] += 1
            
            details = {
                "sample_size": sample_size,
                "success_count": success_count,
                "blocked_count": blocked_count,
                "deactivated_count": deactivated_count
            }
            
            await self.log_health_check("user_communications", details)
            
            if blocked_count > 0 or deactivated_count > 0:
                await self.send_admin_alert(
                    f"User communications check:\n"
                    f"• Sample size: {sample_size}\n"
                    f"• Success: {success_count}\n"
                    f"• Blocked by user: {blocked_count}\n"
                    f"• Deactivated accounts: {deactivated_count}",
                    level="info"
                )
                
            return True
            
        except Exception as e:
            error_details = {"error": str(e)}
            await self.log_health_check("user_communications", error_details, status="error")
            logger.error(f"Failed to verify user communications: {e}")
            return False
    
    async def check_upcoming_events(self, days: int = 7):
        """Check for upcoming events and verify their data integrity."""
        try:
            cutoff_date = datetime.now() + timedelta(days=days)
            
            # Find upcoming events
            upcoming_events = await events_collection.find({
                "date_time": {"$gte": datetime.now(), "$lte": cutoff_date}
            }).sort("date_time", 1).to_list(length=None)
            
            if not upcoming_events:
                logger.info(f"No upcoming events in the next {days} days")
                return True
            
            issues_found = []
            
            for event in upcoming_events:
                # Check for missing or invalid fields
                if not event.get("title"):
                    issues_found.append(f"Event {event['_id']} has no title")
                
                if not event.get("date_time"):
                    issues_found.append(f"Event {event['_id']} has no date/time")
                
                if not event.get("location"):
                    issues_found.append(f"Event {event['_id']} has no location")
                
                # Check for valid creator
                creator_id = event.get("created_by")
                if creator_id:
                    creator = await users_collection.find_one({"telegram_id": creator_id})
                    if not creator:
                        issues_found.append(f"Event {event['_id']} has invalid creator ID {creator_id}")
            
            details = {
                "events_count": len(upcoming_events),
                "days_ahead": days,
                "issues_count": len(issues_found)
            }
            
            await self.log_health_check("upcoming_events", details)
            
            if issues_found:
                issues_text = "\n".join([f"• {issue}" for issue in issues_found])
                await self.send_admin_alert(
                    f"Issues found with upcoming events:\n\n{issues_text}",
                    level="warning"
                )
                return False
            else:
                logger.info(f"Found {len(upcoming_events)} valid upcoming events")
                return True
                
        except Exception as e:
            error_details = {"error": str(e)}
            await self.log_health_check("upcoming_events", error_details, status="error")
            logger.error(f"Failed to check upcoming events: {e}")
            return False
    
    async def run_all_health_checks(self):
        """Run all health checks and generate a report."""
        logger.info("Starting comprehensive health check")
        
        results = {
            "telegram_api": await self.check_telegram_api(),
            "mongodb": await self.check_mongodb_connection(),
            "ogs_api": await self.check_ogs_api(),
            "user_communications": await self.verify_user_communications(),
            "upcoming_events": await self.check_upcoming_events()
        }
        
        # Get inactive users but don't include in results dict
        inactive_users = await self.check_inactive_users()
        
        # Calculate overall status
        overall_status = "Healthy" if all(results.values()) else "Issues Detected"
        
        # Prepare report
        uptime = datetime.now() - self.start_time
        uptime_str = str(uptime).split('.')[0]  # Remove microseconds
        
        report = f"*Go Club Bot Health Report*\n\n"
        report += f"📊 *Overall Status:* {overall_status}\n"
        report += f"⏱️ *Uptime:* {uptime_str}\n"
        report += f"🔄 *API Calls:* Telegram: {self.api_call_counts['telegram']}, OGS: {self.api_call_counts['ogs']}\n"
        report += f"⚠️ *API Errors:* Telegram: {self.api_errors['telegram']}, OGS: {self.api_errors['ogs']}\n\n"
        
        report += "*Component Status:*\n"
        for component, status in results.items():
            emoji = "✅" if status else "❌"
            report += f"{emoji} {component.replace('_', ' ').title()}\n"
        
        report += f"\n*Inactive Users:* {len(inactive_users)}\n"
        
        # Log the comprehensive check
        await self.log_health_check("comprehensive", {
            "results": results,
            "overall_status": overall_status,
            "uptime_seconds": uptime.total_seconds(),
            "api_calls": self.api_call_counts,
            "api_errors": self.api_errors,
            "inactive_users_count": len(inactive_users)
        })
        
        # Send report to admins
        level = "success" if overall_status == "Healthy" else "warning"
        await self.send_admin_alert(report, level=level)
        
        logger.info(f"Health check completed: {overall_status}")
        return overall_status == "Healthy"


async def run_health_check_schedule():
    """Run health checks on a schedule."""
    checker = HealthCheck()
    
    logger.info("Starting health check scheduler")
    
    try:
        # Run an initial comprehensive check
        await checker.run_all_health_checks()
        
        while True:
            # Every hour, check API endpoints
            for _ in range(24):  # 24 hours
                await asyncio.sleep(60 * 60)  # 1 hour
                await checker.check_telegram_api()
                await checker.check_ogs_api()
                await checker.check_mongodb_connection()
            
            # Once a day, run comprehensive check
            await checker.run_all_health_checks()
            
    except asyncio.CancelledError:
        logger.info("Health check scheduler stopped")
    except Exception as e:
        logger.error(f"Error in health check scheduler: {e}")
        # Wait a bit and restart
        await asyncio.sleep(60)
        await run_health_check_schedule()


if __name__ == "__main__":
    """Run health checks directly for debugging or as a standalone script."""
    loop = asyncio.get_event_loop()
    
    try:
        # Run a one-time comprehensive check
        checker = HealthCheck()
        loop.run_until_complete(checker.run_all_health_checks())
    except KeyboardInterrupt:
        logger.info("Health check interrupted by user")
    finally:
        loop.close()
