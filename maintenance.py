import logging
import os
import time
import json
import signal
import asyncio
import platform
import psutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union

import motor.motor_asyncio
import aiohttp
import aiogram
from pymongo import MongoClient
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("go_club_bot.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('go_club_maintenance')

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_IDS", "").split(",")
BACKUP_DIR = os.getenv("BACKUP_DIR", "./backups")
MAX_BACKUP_AGE_DAYS = int(os.getenv("MAX_BACKUP_AGE_DAYS", "30"))

# Ensure backup directory exists
os.makedirs(BACKUP_DIR, exist_ok=True)

# Initialize MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client.go_club_db
users_collection = db.users
events_collection = db.events
matches_collection = db.matches
subscriptions_collection = db.subscriptions
maintenance_collection = db.maintenance_logs

class MaintenanceManager:
    """Handles database maintenance, backups, and system monitoring."""
    
    def __init__(self):
        self.bot = None
        self.start_time = datetime.now()
        self.sync_client = MongoClient(MONGO_URI)
        self.sync_db = self.sync_client.go_club_db
        
    async def initialize_bot(self):
        """Initialize bot connection for sending alerts."""
        if not self.bot:
            self.bot = aiogram.Bot(token=TELEGRAM_BOT_TOKEN)
    
    async def send_admin_alert(self, message: str, level: str = "info"):
        """Send alert messages to admin chat IDs."""
        await self.initialize_bot()
        
        emoji_map = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🚨",
            "success": "✅"
        }
        
        emoji = emoji_map.get(level.lower(), "ℹ️")
        formatted_message = f"{emoji} *Maintenance Alert*\n\n{message}"
        
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
    
    async def log_maintenance_action(self, action: str, details: Dict, success: bool = True):
        """Log maintenance actions to the database."""
        log_entry = {
            "action": action,
            "details": details,
            "success": success,
            "timestamp": datetime.now()
        }
        
        try:
            await maintenance_collection.insert_one(log_entry)
            logger.info(f"Maintenance log recorded: {action}")
        except Exception as e:
            logger.error(f"Failed to log maintenance action: {e}")
    
    async def create_database_backup(self):
        """Create a backup of the MongoDB database."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"go_club_backup_{timestamp}")
        
        try:
            # Run mongodump command
            os.makedirs(backup_path, exist_ok=True)
            
            # Construct mongodump command based on URI
            # Extract credentials and host info from MONGO_URI
            uri_parts = MONGO_URI.split('@')
            if len(uri_parts) > 1:
                auth_parts = uri_parts[0].split('://')[-1].split(':')
                if len(auth_parts) > 1:
                    username = auth_parts[0]
                    password = auth_parts[1]
                    host_parts = uri_parts[1].split('/')
                    host = host_parts[0]
                    
                    cmd = f"mongodump --host {host} --username {username} --password {password} " \
                          f"--db go_club_db --out {backup_path}"
                else:
                    cmd = f"mongodump --uri \"{MONGO_URI}\" --out {backup_path}"
            else:
                cmd = f"mongodump --uri \"{MONGO_URI}\" --out {backup_path}"
                
            start_time = time.time()
            result = os.system(cmd)
            end_time = time.time()
            
            if result == 0:
                # Compress the backup
                archive_name = f"{backup_path}.tar.gz"
                compress_cmd = f"tar -czf {archive_name} -C {BACKUP_DIR} {os.path.basename(backup_path)}"
                compress_result = os.system(compress_cmd)
                
                if compress_result == 0:
                    # Remove the uncompressed directory
                    os.system(f"rm -rf {backup_path}")
                    
                    backup_size = os.path.getsize(archive_name)
                    details = {
                        "path": archive_name,
                        "size_bytes": backup_size,
                        "duration_seconds": end_time - start_time
                    }
                    
                    await self.log_maintenance_action(
                        "database_backup", 
                        details, 
                        success=True
                    )
                    
                    logger.info(f"Database backup created at {archive_name} ({backup_size} bytes)")
                    await self.send_admin_alert(
                        f"Database backup created successfully.\n"
                        f"Location: {archive_name}\n"
                        f"Size: {backup_size / (1024 * 1024):.2f} MB\n"
                        f"Duration: {end_time - start_time:.2f} seconds",
                        level="success"
                    )
                    return True
                else:
                    raise Exception("Failed to compress backup")
            else:
                raise Exception(f"mongodump command failed with exit code {result}")
                
        except Exception as e:
            error_msg = f"Database backup failed: {str(e)}"
            logger.error(error_msg)
            await self.log_maintenance_action(
                "database_backup", 
                {"error": str(e)}, 
                success=False
            )
            await self.send_admin_alert(error_msg, level="error")
            return False
    
    async def cleanup_old_backups(self):
        """Remove backup files older than MAX_BACKUP_AGE_DAYS."""
        try:
            count = 0
            now = datetime.now()
            backup_files = [f for f in os.listdir(BACKUP_DIR) if f.startswith("go_club_backup_")]
            
            for backup_file in backup_files:
                file_path = os.path.join(BACKUP_DIR, backup_file)
                # Get the file's creation time
                file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                
                # If the file is older than MAX_BACKUP_AGE_DAYS days, delete it
                if now - file_time > timedelta(days=MAX_BACKUP_AGE_DAYS):
                    os.remove(file_path)
                    count += 1
                    logger.info(f"Removed old backup file: {file_path}")
            
            if count > 0:
                await self.log_maintenance_action(
                    "cleanup_old_backups", 
                    {"files_removed": count}, 
                    success=True
                )
                await self.send_admin_alert(
                    f"Cleanup completed. Removed {count} old backup files.",
                    level="info"
                )
            return True
            
        except Exception as e:
            error_msg = f"Failed to clean up old backups: {str(e)}"
            logger.error(error_msg)
            await self.log_maintenance_action(
                "cleanup_old_backups", 
                {"error": str(e)}, 
                success=False
            )
            await self.send_admin_alert(error_msg, level="error")
            return False
    
    async def cleanup_old_events(self, days_old: int = 90):
        """Archive old events."""
        try:
            cutoff_date = datetime.now() - timedelta(days=days_old)
            
            # Find old events
            old_events = await events_collection.find(
                {"date_time": {"$lt": cutoff_date}}
            ).to_list(length=None)
            
            if not old_events:
                logger.info(f"No events older than {days_old} days found for archiving")
                return True
            
            # Archive old events (insert to archive collection and remove from active)
            archive_collection = db.archived_events
            result = await archive_collection.insert_many(old_events)
            
            if result.acknowledged:
                # Extract IDs of the archived events
                archived_ids = [event["_id"] for event in old_events]
                
                # Remove archived events from the active collection
                delete_result = await events_collection.delete_many(
                    {"_id": {"$in": archived_ids}}
                )
                
                await self.log_maintenance_action(
                    "archive_old_events", 
                    {"count": len(old_events), "days_old": days_old}, 
                    success=True
                )
                
                logger.info(f"Archived {len(old_events)} events older than {days_old} days")
                await self.send_admin_alert(
                    f"Event archiving completed.\n"
                    f"Archived {len(old_events)} events older than {days_old} days.",
                    level="info"
                )
                return True
            else:
                raise Exception("Failed to insert events into archive collection")
                
        except Exception as e:
            error_msg = f"Failed to archive old events: {str(e)}"
            logger.error(error_msg)
            await self.log_maintenance_action(
                "archive_old_events", 
                {"error": str(e), "days_old": days_old}, 
                success=False
            )
            await self.send_admin_alert(error_msg, level="error")
            return False
    
    async def update_expired_subscriptions(self):
        """Update status of expired mentorship subscriptions."""
        try:
            now = datetime.now()
            
            # Find active subscriptions that have expired
            expired_subs = await subscriptions_collection.find(
                {
                    "status": "active",
                    "end_date": {"$lt": now}
                }
            ).to_list(length=None)
            
            if not expired_subs:
                logger.info("No expired subscriptions found")
                return True
            
            # Update expired subscriptions to "expired" status
            update_ids = [sub["_id"] for sub in expired_subs]
            update_result = await subscriptions_collection.update_many(
                {"_id": {"$in": update_ids}},
                {"$set": {"status": "expired", "expired_at": now}}
            )
            
            if update_result.modified_count > 0:
                await self.log_maintenance_action(
                    "update_expired_subscriptions", 
                    {"count": update_result.modified_count}, 
                    success=True
                )
                
                logger.info(f"Updated {update_result.modified_count} expired subscriptions")
                
                # Notify affected users
                for sub in expired_subs:
                    try:
                        # Notify mentee
                        mentee_id = sub.get("mentee_id")
                        mentor_name = sub.get("mentor_name", "your mentor")
                        
                        if mentee_id:
                            await self.bot.send_message(
                                mentee_id,
                                f"Your mentorship subscription with {mentor_name} has expired. "
                                f"If you wish to continue, please renew your subscription."
                            )
                        
                        # Notify mentor
                        mentor_id = sub.get("mentor_id")
                        mentee_name = sub.get("mentee_name", "A mentee")
                        
                        if mentor_id:
                            await self.bot.send_message(
                                mentor_id,
                                f"Your mentorship with {mentee_name} has expired. "
                                f"They have been notified and may choose to renew."
                            )
                    except Exception as e:
                        logger.error(f"Failed to notify user about expired subscription: {e}")
                
                await self.send_admin_alert(
                    f"Updated {update_result.modified_count} expired subscriptions.",
                    level="info"
                )
                return True
            else:
                logger.info("No subscriptions were updated (already processed)")
                return True
                
        except Exception as e:
            error_msg = f"Failed to update expired subscriptions: {str(e)}"
            logger.error(error_msg)
            await self.log_maintenance_action(
                "update_expired_subscriptions", 
                {"error": str(e)}, 
                success=False
            )
            await self.send_admin_alert(error_msg, level="error")
            return False
    
    async def create_database_indexes(self):
        """Create and optimize MongoDB indexes."""
        try:
            # User collection indexes
            await users_collection.create_index("telegram_id", unique=True)
            await users_collection.create_index("ogs_username")
            await users_collection.create_index("rank")
            await users_collection.create_index("is_mentor")
            
            # Events collection indexes
            await events_collection.create_index("date_time")
            await events_collection.create_index("created_by")
            
            # Matches collection indexes
            await matches_collection.create_index("date")
            await matches_collection.create_index("player1_id")
            await matches_collection.create_index("player2_id")
            
            # Subscriptions collection indexes
            await subscriptions_collection.create_index("mentor_id")
            await subscriptions_collection.create_index("mentee_id")
            await subscriptions_collection.create_index("status")
            await subscriptions_collection.create_index("end_date")
            
            await self.log_maintenance_action(
                "create_database_indexes", 
                {"collections": ["users", "events", "matches", "subscriptions"]}, 
                success=True
            )
            
            logger.info("Database indexes created/updated successfully")
            return True
            
        except Exception as e:
            error_msg = f"Failed to create database indexes: {str(e)}"
            logger.error(error_msg)
            await self.log_maintenance_action(
                "create_database_indexes", 
                {"error": str(e)}, 
                success=False
            )
            await self.send_admin_alert(error_msg, level="error")
            return False
    
    async def check_system_health(self):
        """Check system resources and health."""
        try:
            # Get system stats
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Get bot uptime
            uptime = datetime.now() - self.start_time
            uptime_str = str(uptime).split('.')[0]  # Remove microseconds
            
            # Get MongoDB stats
            db_stats = self.sync_db.command("dbStats")
            
            health_data = {
                "timestamp": datetime.now(),
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "disk_percent": disk.percent,
                "uptime_seconds": uptime.total_seconds(),
                "db_size_mb": db_stats.get("dataSize", 0) / (1024 * 1024),
                "db_collections": db_stats.get("collections", 0),
                "db_indexes": db_stats.get("indexes", 0),
                "system": platform.system(),
                "python_version": platform.python_version()
            }
            
            # Log health check to database
            await maintenance_collection.insert_one({
                "action": "health_check",
                "details": health_data,
                "timestamp": datetime.now()
            })
            
            # Check for warning thresholds
            warnings = []
            if cpu_percent > 80:
                warnings.append(f"High CPU usage: {cpu_percent}%")
            if memory.percent > 85:
                warnings.append(f"High memory usage: {memory.percent}%")
            if disk.percent > 90:
                warnings.append(f"Low disk space: {disk.percent}% used")
            
            if warnings:
                warning_msg = "⚠️ System resource warnings:\n• " + "\n• ".join(warnings)
                logger.warning(warning_msg)
                await self.send_admin_alert(
                    warning_msg + f"\n\nBot uptime: {uptime_str}",
                    level="warning"
                )
            
            logger.info(f"Health check completed. CPU: {cpu_percent}%, Memory: {memory.percent}%, Disk: {disk.percent}%")
            return health_data
            
        except Exception as e:
            error_msg = f"Failed to check system health: {str(e)}"
            logger.error(error_msg)
            await self.log_maintenance_action(
                "health_check", 
                {"error": str(e)}, 
                success=False
            )
            await self.send_admin_alert(error_msg, level="error")
            return None
    
    async def run_all_maintenance(self):
        """Run all maintenance tasks in sequence."""
        logger.info("Starting full maintenance routine")
        
        # Initialize bot for alerts
        await self.initialize_bot()
        
        # Run all maintenance tasks
        await self.create_database_backup()
        await self.cleanup_old_backups()
        await self.cleanup_old_events()
        await self.update_expired_subscriptions()
        await self.create_database_indexes()
        health_data = await self.check_system_health()
        
        # Generate summary
        uptime = str(datetime.now() - self.start_time).split('.')[0]
        
        summary = (
            "✅ *Maintenance Complete*\n\n"
            f"*System Stats:*\n"
            f"• CPU: {health_data['cpu_percent']}%\n"
            f"• Memory: {health_data['memory_percent']}%\n"
            f"• Disk: {health_data['disk_percent']}%\n"
            f"• DB Size: {health_data['db_size_mb']:.2f} MB\n"
            f"• Bot Uptime: {uptime}\n\n"
            f"All maintenance tasks completed. Check logs for details."
        )
        
        await self.send_admin_alert(summary, level="success")
        logger.info("Full maintenance routine completed")


async def run_maintenance_schedule():
    """Run the maintenance tasks on a schedule."""
    manager = MaintenanceManager()
    
    # Send startup notification
    await manager.initialize_bot()
    await manager.send_admin_alert(
        "🚀 Maintenance scheduler started. Running initial health check...",
        level="info"
    )
    
    # Run initial setup tasks
    await manager.create_database_indexes()
    health_data = await manager.check_system_health()
    
    try:
        while True:
            # Run health check every hour
            await asyncio.sleep(3600)  # 1 hour
            await manager.check_system_health()
            
            # Get current hour
            current_hour = datetime.now().hour
            
            # Run backup at 3 AM
            if current_hour == 3:
                await manager.create_database_backup()
                await manager.cleanup_old_backups()
            
            # Run subscription updates at 6 AM
            if current_hour == 6:
                await manager.update_expired_subscriptions()
            
            # Run event cleanup at 4 AM on Sundays
            if current_hour == 4 and datetime.now().weekday() == 6:  # 0 is Monday, 6 is Sunday
                await manager.cleanup_old_events()
    
    except asyncio.CancelledError:
        logger.info("Maintenance scheduler stopped")
    except Exception as e:
        logger.error(f"Error in maintenance scheduler: {e}")
        await manager.send_admin_alert(
            f"🚨 Maintenance scheduler error: {str(e)}\nRestarting scheduler...",
            level="error"
        )
        # Wait a bit and retry
        await asyncio.sleep(60)
        return await run_maintenance_schedule()


def handle_exit(signum, frame):
    """Handle program termination."""
    logger.info(f"Received signal {signum}, shutting down maintenance scheduler")
    # Close MongoDB connection
    if 'client' in globals():
        client.close()
    if hasattr(asyncio, 'all_tasks'):
        for task in asyncio.all_tasks(loop=asyncio.get_event_loop()):
            task.cancel()
    elif hasattr(asyncio, 'Task'):
        for task in asyncio.Task.all_tasks(loop=asyncio.get_event_loop()):
            task.cancel()
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.stop()


async def restore_database(backup_path: str):
    """Restore database from a backup file."""
    manager = MaintenanceManager()
    await manager.initialize_bot()
    
    try:
        # Extract the backup
        extract_dir = os.path.join(BACKUP_DIR, "temp_restore")
        os.makedirs(extract_dir, exist_ok=True)
        
        extract_cmd = f"tar -xzf {backup_path} -C {extract_dir}"
        extract_result = os.system(extract_cmd)
        
        if extract_result != 0:
            raise Exception(f"Failed to extract backup file: Exit code {extract_result}")
        
        # Find the extracted directory
        backup_dirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
        if not backup_dirs:
            raise Exception("No backup directory found in the archive")
        
        backup_dir = os.path.join(extract_dir, backup_dirs[0])
        
        # Run mongorestore
        uri_parts = MONGO_URI.split('@')
        if len(uri_parts) > 1:
            auth_parts = uri_parts[0].split('://')[-1].split(':')
            if len(auth_parts) > 1:
                username = auth_parts[0]
                password = auth_parts[1]
                host_parts = uri_parts[1].split('/')
                host = host_parts[0]
                
                cmd = f"mongorestore --host {host} --username {username} --password {password} " \
                      f"--db go_club_db {backup_dir}/go_club_db --drop"
            else:
                cmd = f"mongorestore --uri \"{MONGO_URI}\" {backup_dir}/go_club_db --drop"
        else:
            cmd = f"mongorestore --uri \"{MONGO_URI}\" {backup_dir}/go_club_db --drop"
        
        restore_result = os.system(cmd)
        
        # Clean up
        os.system(f"rm -rf {extract_dir}")
        
        if restore_result == 0:
            await manager.log_maintenance_action(
                "database_restore", 
                {"backup_path": backup_path}, 
                success=True
            )
            
            logger.info(f"Database restored successfully from {backup_path}")
            await manager.send_admin_alert(
                f"Database restored successfully from backup: {os.path.basename(backup_path)}",
                level="success"
            )
            return True
        else:
            raise Exception(f"mongorestore command failed with exit code {restore_result}")
        
    except Exception as e:
        error_msg = f"Database restoration failed: {str(e)}"
        logger.error(error_msg)
        await manager.log_maintenance_action(
            "database_restore", 
            {"error": str(e), "backup_path": backup_path}, 
            success=False
        )
        await manager.send_admin_alert(error_msg, level="error")
        return False


if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    # Set up the event loop
    loop = asyncio.get_event_loop()
    
    # Check command line arguments for restore operation
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "restore" and len(sys.argv) > 2:
        backup_path = sys.argv[2]
        logger.info(f"Starting database restore from {backup_path}")
        loop.run_until_complete(restore_database(backup_path))
    else:
        # Start the maintenance scheduler
        logger.info("Starting maintenance scheduler")
        try:
            loop.run_until_complete(run_maintenance_schedule())
        except KeyboardInterrupt:
            logger.info("Maintenance scheduler interrupted by user")
        finally:
            loop.close()
