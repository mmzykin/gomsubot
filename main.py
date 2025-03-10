import logging
import asyncio
import os
import argparse
from aiogram import Bot, Dispatcher, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

# Import original bot file as a module
from bot import dp, bot, setup_bot_commands, register_all_handlers

# Import maintenance modules
from maintenance import MaintenanceManager, run_maintenance_schedule
from healthcheck import HealthCheck, run_health_check_schedule
from security import setup_security_for_bot

# Configure logging
logging_level = logging.DEBUG if DEBUG_MODE else logging.INFO
logging.basicConfig(
    level=logging_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("go_club_bot.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('go_club_bot')

async def startup(dispatcher: Dispatcher):
    """Perform startup actions."""
    logger.info("Starting Go Club Bot")
    
    # Setup commands
    await setup_bot_commands(bot)
    
    # Setup security module
    logger.info("Initializing security module")
    await setup_security_for_bot(dispatcher)
    
    # Initialize maintenance module (but don't start scheduler yet)
    logger.info("Initializing maintenance module")
    maintenance = MaintenanceManager()
    await maintenance.initialize_bot()
    
    # Create database indexes
    await maintenance.create_database_indexes()
    
    # Run initial health check
    logger.info("Running initial health check")
    health = HealthCheck()
    await health.check_mongodb_connection()
    await health.check_telegram_api()
    
    # Start the background tasks if not in debug mode
    if not DEBUG_MODE:
        logger.info("Starting background tasks")
        asyncio.create_task(run_maintenance_schedule())
        asyncio.create_task(run_health_check_schedule())
    
    # Send startup notification to admins
    admin_chat_ids = os.getenv("ADMIN_CHAT_IDS", "").split(",")
    for admin_id in admin_chat_ids:
        if admin_id:
            try:
                await bot.send_message(
                    admin_id,
                    "🚀 Go Club Bot has started successfully!\n\n"
                    f"Debug mode: {'Enabled' if DEBUG_MODE else 'Disabled'}"
                )
            except Exception as e:
                logger.error(f"Failed to send startup notification to admin {admin_id}: {e}")

async def shutdown(dispatcher: Dispatcher):
    """Perform shutdown actions."""
    logger.info("Shutting down Go Club Bot")
    
    # Send shutdown notification to admins
    admin_chat_ids = os.getenv("ADMIN_CHAT_IDS", "").split(",")
    for admin_id in admin_chat_ids:
        if admin_id:
            try:
                await bot.send_message(
                    admin_id,
                    "🛑 Go Club Bot is shutting down. Maintenance or restart in progress."
                )
            except Exception as e:
                logger.error(f"Failed to send shutdown notification to admin {admin_id}: {e}")
    
    # Close storage
    await dispatcher.storage.close()
    await dispatcher.storage.wait_closed()
    
    # Close bot session
    session = await bot.get_session()
    if session:
        await session.close()

def start_bot():
    """Start the bot normally."""
    # Register all handlers from the original bot file
    register_all_handlers(dp)
    
    # Start the bot with startup and shutdown handlers
    executor.start_polling(
        dp, 
        on_startup=startup, 
        on_shutdown=shutdown,
        skip_updates=True
    )

def start_maintenance_only():
    """Start only the maintenance module without the full bot."""
    async def run_once():
        maintenance = MaintenanceManager()
        await maintenance.initialize_bot()
        await maintenance.run_all_maintenance()
    
    # Run maintenance synchronously
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_once())

def start_healthcheck_only():
    """Run a standalone health check without starting the full bot."""
    async def run_once():
        health = HealthCheck()
        await health.run_all_health_checks()
    
    # Run health check synchronously
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_once())

def restore_database(backup_path):
    """Restore the database from a backup file."""
    from maintenance import restore_database as restore_func
    
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(restore_func(backup_path))
    
    if result:
        print(f"Database restored successfully from {backup_path}")
    else:
        print(f"Failed to restore database from {backup_path}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Go Club Bot')
    parser.add_argument('--mode', default='bot', choices=['bot', 'maintenance', 'health', 'restore'],
                      help='Mode to run (default: bot)')
    parser.add_argument('--backup', help='Backup file path (for restore mode)')
    
    args = parser.parse_args()
    
    if args.mode == 'bot':
        # Start the full bot
        start_bot()
    elif args.mode == 'maintenance':
        # Run maintenance only
        start_maintenance_only()
    elif args.mode == 'health':
        # Run health check only
        start_healthcheck_only()
    elif args.mode == 'restore':
        # Restore database from backup
        if not args.backup:
            print("Error: --backup parameter is required for restore mode")
        else:
            restore_database(args.backup)
