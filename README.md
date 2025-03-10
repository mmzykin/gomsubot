# Go Club Bot - Installation and Maintenance Guide

## Table of Contents
1. [Introduction](#introduction)
2. [System Requirements](#system-requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Bot](#running-the-bot)
6. [Maintenance Tasks](#maintenance-tasks)
7. [Monitoring](#monitoring)
8. [Security](#security)
9. [Backup and Restore](#backup-and-restore)
10. [Troubleshooting](#troubleshooting)
11. [Extending the Bot](#extending-the-bot)

## Introduction

The Go Club Bot is a Telegram bot designed to manage a Go club community. It provides features for user registration, rank tracking, event management, match recording, mentorship programs, and more. This guide covers how to set up, configure, and maintain the bot.

## System Requirements

- Python 3.8 or higher
- MongoDB 4.2 or higher
- Sufficient disk space for database backups
- 1GB RAM minimum (2GB+ recommended)
- Network connectivity for Telegram API and OGS API communication

### Required Python Packages
```
aiogram==2.14.3
motor==2.5.1
pymongo==3.12.0
aiohttp==3.7.4
python-dotenv==0.19.1
psutil==5.8.0
```

## Installation

1. **Clone the repository**

```bash
git clone https://github.com/your-organization/go-club-bot.git
cd go-club-bot
```

2. **Create and activate a virtual environment**

```bash
python -m venv venv
source venv/bin/activate  # On Windows, use: venv\Scripts\activate
```

3. **Install dependencies**

```bash
pip install -r requirements.txt
```

4. **Set up MongoDB**

Install MongoDB following the official documentation for your platform. Create a new database named `go_club_db`.

```bash
# Example for MongoDB setup on Ubuntu
sudo apt-get install mongodb
sudo systemctl start mongodb
sudo systemctl enable mongodb
```

## Configuration

1. **Create an environment file**

Create a `.env` file in the project root:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
MONGO_URI=mongodb://localhost:27017/go_club_db
ADMIN_CHAT_IDS=123456789,987654321
BACKUP_DIR=./backups
MAX_BACKUP_AGE_DAYS=30
SECURITY_SECRET=your_secret_key_for_hmac
RATE_LIMIT_ENABLED=True
DEBUG_MODE=False
```

2. **Obtain a Telegram Bot Token**

Create a new bot through the [BotFather](https://t.me/botfather) on Telegram and note the API token.

3. **Set Admin IDs**

Add the Telegram user IDs of all administrators in the `ADMIN_CHAT_IDS` environment variable, separated by commas.

## Running the Bot

The Go Club Bot can be run in different modes depending on your needs:

1. **Normal Bot Mode**

This starts the bot with all features enabled, including background tasks for maintenance and health checks.

```bash
python main.py --mode bot
```

2. **Maintenance Mode**

This runs only the maintenance tasks without starting the bot.

```bash
python main.py --mode maintenance
```

3. **Health Check Mode**

This runs a health check and reports the results without starting the bot.

```bash
python main.py --mode health
```

4. **Restore Mode**

This restores the database from a backup file.

```bash
python main.py --mode restore --backup ./backups/go_club_backup_20230101_120000.tar.gz
```

### Setting up as a Service

On Linux systems, you can set up the bot as a systemd service to ensure it runs continuously and restarts after failures.

Create a file in `/etc/systemd/system/go-club-bot.service`:

```ini
[Unit]
Description=Go Club Telegram Bot
After=network.target mongodb.service

[Service]
User=youruser
WorkingDirectory=/path/to/go-club-bot
ExecStart=/path/to/go-club-bot/venv/bin/python main.py --mode bot
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl enable go-club-bot
sudo systemctl start go-club-bot
```

## Maintenance Tasks

### Scheduled Maintenance

The bot includes a maintenance module that automatically performs the following tasks:

1. **Database Backups**
   - Performed daily at 3 AM
   - Stored in the directory specified by `BACKUP_DIR`

2. **Old Backup Cleanup**
   - Removes backups older than `MAX_BACKUP_AGE_DAYS`
   - Runs after each new backup

3. **Event Archiving**
   - Archives events older than 90 days
   - Runs weekly

4. **Subscription Management**
   - Updates expired mentor-mentee subscriptions
   - Runs daily at 6 AM

5. **Health Checks**
   - Basic API checks run hourly
   - Comprehensive checks run daily

### Manual Maintenance

You can also manually run maintenance tasks:

```bash
# Run all maintenance tasks
python main.py --mode maintenance

# Run health check only
python main.py --mode health

# Restore from backup
python main.py --mode restore --backup /path/to/backup.tar.gz
```

## Monitoring

### Health Checks

The bot includes a comprehensive health check system that monitors:

1. **Telegram API connectivity**
2. **MongoDB connection**
3. **OGS API connectivity**
4. **User communication status**
5. **Event data integrity**
6. **Inactive user tracking**

Health check results are:

- Logged to the `health_logs` collection in MongoDB
- Sent to administrator chat IDs as alerts
- Available via the health check mode

### Logs

The bot maintains several log files:

- `go_club_bot.log` - Main bot logs
- `healthcheck.log` - Health check specific logs
- `security.log` - Security event logs

Log levels can be adjusted by setting the `DEBUG_MODE` environment variable.

## Security

### User Management

Administrators can block and unblock users directly through the bot:

```
/block [user_id] [duration_days or 'permanent'] [reason]
/unblock [user_id]
/security_status
```

### Rate Limiting

The bot implements rate limiting to prevent abuse:

- 30 messages per minute per user
- 20 callback queries per minute per user

Rate limit violations are logged and administrators are notified of repeated violations.

### Input Validation

All user inputs are validated and sanitized to prevent injection attacks:

- Suspicious messages are flagged for review
- Users with repeated suspicious activity may be automatically blocked
- HMAC verification is used for secure data validation

## Backup and Restore

### Automatic Backups

The bot creates daily MongoDB backups using `mongodump` and compresses them to save space.

### Manual Backup

You can manually trigger a backup:

```bash
python maintenance.py
```

### Restoring from Backup

To restore the database from a backup:

```bash
python main.py --mode restore --backup /path/to/backup.tar.gz
```

This will:
1. Extract the backup
2. Run `mongorestore` with the `--drop` option (replacing existing collections)
3. Send a notification to administrators

## Troubleshooting

### Common Issues

1. **Bot not responding**
   - Check if the bot process is running
   - Verify Telegram API connectivity
   - Check for rate limiting by Telegram

2. **Database connection failures**
   - Ensure MongoDB service is running
   - Verify connection string is correct
   - Check MongoDB logs for errors

3. **OGS API issues**
   - OGS may have API limits or downtime
   - Check connectivity to online-go.com
   - Verify that user ranks are updating properly

### Logs and Diagnostics

For detailed diagnostics:

1. Enable debug mode by setting `DEBUG_MODE=True` in `.env`
2. Check the log files for specific error messages
3. Run a health check: `python main.py --mode health`

## Extending the Bot

### Adding New Features

The bot is modular and can be extended by adding new handlers or modules.

1. **New Command Handlers**
   - Add new functions to `bot.py` using the aiogram decorator syntax
   - Register them in the `register_all_handlers` function

2. **New Background Tasks**
   - Add new async functions to appropriate modules
   - Start them with `asyncio.create_task()` in the startup function

3. **New API Integrations**
   - Add new API client functions to a dedicated module
   - Follow the pattern in the existing OGS API integration

### Database Schema

When extending the database schema:

1. Update indexes in the `create_database_indexes` function in `maintenance.py`
2. Add appropriate validation in the `security.py` module
3. Document the changes for future maintenance
