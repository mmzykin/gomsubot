from bson.objectid import ObjectId
import logging
import os
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union

import aiohttp
import motor.motor_asyncio
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ParseMode,
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OGS_API_URL = "https://online-go.com/api/v1"

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize bot and dispatcher
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Initialize MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client.go_club_db
users_collection = db.users
events_collection = db.events
matches_collection = db.matches
subscriptions_collection = db.subscriptions

# Constants
RANKS = [
    "30k", "29k", "28k", "27k", "26k", "25k", "24k", "23k", "22k", "21k",
    "20k", "19k", "18k", "17k", "16k", "15k", "14k", "13k", "12k", "11k",
    "10k", "9k", "8k", "7k", "6k", "5k", "4k", "3k", "2k", "1k",
    "1d", "2d", "3d", "4d", "5d", "6d", "7d", "8d", "9d"
]

MENTOR_MINIMUM_RANK = "3k"  # Minimum rank required to become a mentor

# Define states for conversation handling
class RegistrationForm(StatesGroup):
    name = State()
    rank = State()
    ogs_username = State()

class EventForm(StatesGroup):
    title = State()
    description = State()
    date = State()
    time = State()
    location = State()

class MatchForm(StatesGroup):
    opponent = State()
    result = State()
    ogs_link = State()

class MentorForm(StatesGroup):
    description = State()
    availability = State()
    price = State()

class MenteeForm(StatesGroup):
    mentor_id = State()
    message = State()

# Utility Functions
def get_rank_index(rank: str) -> int:
    try:
        return RANKS.index(rank)
    except ValueError:
        return -1
def register_all_handlers(dp):
    """Register all message handlers."""
    # All handlers are already registered with decorators
    # This function exists to provide an interface for main.py
    pass
    
def is_rank_sufficient_for_mentor(rank: str) -> bool:
    mentor_min_index = get_rank_index(MENTOR_MINIMUM_RANK)
    user_rank_index = get_rank_index(rank)
    return user_rank_index >= mentor_min_index

async def fetch_ogs_data(username: str) -> Dict:
    async with aiohttp.ClientSession() as session:
        try:
            # Search for user
            async with session.get(f"{OGS_API_URL}/players", params={"username": username}) as resp:
                if resp.status != 200:
                    return {"error": "Failed to fetch OGS data"}
                
                data = await resp.json()
                if not data.get("results"):
                    return {"error": "User not found on OGS"}
                
                user_id = data["results"][0]["id"]
                
                # Get detailed user info
                async with session.get(f"{OGS_API_URL}/players/{user_id}/") as user_resp:
                    if user_resp.status != 200:
                        return {"error": "Failed to fetch detailed user data"}
                    
                    user_data = await user_resp.json()
                    
                    # Get recent games
                    async with session.get(f"{OGS_API_URL}/players/{user_id}/games/") as games_resp:
                        if games_resp.status != 200:
                            return {"error": "Failed to fetch user games"}
                        
                        games_data = await games_resp.json()
                        
                        return {
                            "id": user_id,
                            "username": user_data.get("username"),
                            "rank": user_data.get("ranking"),
                            "wins": user_data.get("wins", 0),
                            "losses": user_data.get("losses", 0),
                            "recent_games": games_data.get("results", [])[:5]
                        }
        except Exception as e:
            return {"error": f"Error fetching OGS data: {str(e)}"}

async def update_user_ogs_stats(user_id: int) -> None:
    user = await users_collection.find_one({"telegram_id": user_id})
    if not user or not user.get("ogs_username"):
        return
    
    ogs_data = await fetch_ogs_data(user["ogs_username"])
    if "error" not in ogs_data:
        await users_collection.update_one(
            {"telegram_id": user_id},
            {"$set": {
                "ogs_rank": ogs_data.get("rank"),
                "ogs_wins": ogs_data.get("wins"),
                "ogs_losses": ogs_data.get("losses"),
                "last_ogs_update": datetime.now()
            }}
        )

def create_leaderboard_message(users: List[Dict]) -> str:
    if not users:
        return "No players in the leaderboard yet."
    
    ranked_users = sorted(users, key=lambda x: get_rank_index(x.get("rank", "30k")), reverse=True)
    
    message = "🏆 *Go Club Leaderboard* 🏆\n\n"
    for i, user in enumerate(ranked_users, 1):
        name = user.get("name", "Unknown")
        rank = user.get("rank", "N/A")
        wins = user.get("wins", 0)
        losses = user.get("losses", 0)
        
        message += f"{i}. *{name}* - {rank} ({wins}W/{losses}L)\n"
    
    return message

def create_event_message(event: Dict) -> str:
    title = event.get("title", "Event")
    description = event.get("description", "No description")
    date = event.get("date", "TBD")
    time = event.get("time", "TBD")
    location = event.get("location", "TBD")
    
    message = f"📅 *{title}* 📅\n\n"
    message += f"📝 *Description*: {description}\n"
    message += f"📆 *Date*: {date}\n"
    message += f"🕒 *Time*: {time}\n"
    message += f"📍 *Location*: {location}\n"
    
    return message

def create_mentor_message(mentor: Dict) -> str:
    name = mentor.get("name", "Unknown")
    rank = mentor.get("rank", "N/A")
    description = mentor.get("mentor_description", "No description")
    availability = mentor.get("mentor_availability", "Not specified")
    price = mentor.get("mentor_price", "Not specified")
    
    message = f"👨‍🏫 *Mentor: {name}* ({rank})\n\n"
    message += f"📝 *About*: {description}\n"
    message += f"🕒 *Availability*: {availability}\n"
    message += f"💰 *Price*: {price}\n"
    
    return message

# Command Handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    user = await users_collection.find_one({"telegram_id": message.from_user.id})
    
    if not user:
        # New user
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add(KeyboardButton("Register"))
        await message.answer(
            "Welcome to the Go Club Bot! 🎉\n\n"
            "This bot helps you track your Go progress, join events, "
            "find matches, and connect with mentors.\n\n"
            "Please register to access all features.",
            reply_markup=keyboard
        )
    else:
        # Existing user
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        keyboard.add(
            KeyboardButton("Leaderboard"),
            KeyboardButton("Events"),
            KeyboardButton("My Profile"),
            KeyboardButton("Record Match"),
            KeyboardButton("Find Mentor"),
            KeyboardButton("Help")
        )
        
        # Add admin and mentor buttons if applicable
        if user.get("is_admin", False):
            keyboard.add(KeyboardButton("Admin Panel"))
        
        if user.get("is_mentor", False):
            keyboard.add(KeyboardButton("Mentor Panel"))
        
        await message.answer(
            f"Welcome back, {user.get('name', 'Go player')}! 👋\n\n"
            "What would you like to do today?",
            reply_markup=keyboard
        )

@dp.message_handler(commands=['register'])
async def cmd_register(message: types.Message):
    await RegistrationForm.name.set()
    await message.answer("Let's get you registered! What's your name?")

@dp.message_handler(state=RegistrationForm.name)
async def process_name(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['name'] = message.text
    
    # Create rank selection keyboard
    keyboard = InlineKeyboardMarkup(row_width=5)
    buttons = []
    for rank in RANKS:
        buttons.append(InlineKeyboardButton(rank, callback_data=f"rank_{rank}"))
    
    keyboard.add(*buttons)
    
    await RegistrationForm.next()
    await message.answer("What's your current Go rank?", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('rank_'), state=RegistrationForm.rank)
async def process_rank(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    rank = callback_query.data.split('_')[1]
    
    async with state.proxy() as data:
        data['rank'] = rank
    
    await RegistrationForm.next()
    await bot.send_message(
        callback_query.from_user.id,
        "What's your OGS (Online Go Server) username? If you don't have one, type 'none'."
    )

@dp.message_handler(state=RegistrationForm.ogs_username)
async def process_ogs_username(message: types.Message, state: FSMContext):
    ogs_username = message.text.strip().lower()
    
    async with state.proxy() as data:
        data['ogs_username'] = None if ogs_username == 'none' else ogs_username
    
    # Verify OGS username if provided
    if ogs_username != 'none':
        await message.answer("Checking your OGS profile...")
        ogs_data = await fetch_ogs_data(ogs_username)
        
        if "error" in ogs_data:
            await message.answer(
                f"Could not verify your OGS username: {ogs_data['error']}\n"
                "You can update it later in your profile. Continuing registration..."
            )
        else:
            await message.answer(
                f"OGS profile found!\n"
                f"Username: {ogs_data['username']}\n"
                f"Rank: {ogs_data['rank']}\n"
                f"Record: {ogs_data['wins']}W/{ogs_data['losses']}L"
            )
            
            async with state.proxy() as data:
                data['ogs_id'] = ogs_data['id']
                data['ogs_rank'] = ogs_data['rank']
                data['ogs_wins'] = ogs_data['wins']
                data['ogs_losses'] = ogs_data['losses']
    
    # Save user data
    user_data = {
        "telegram_id": message.from_user.id,
        "username": message.from_user.username,
        "name": data['name'],
        "rank": data['rank'],
        "ogs_username": data['ogs_username'],
        "wins": 0,
        "losses": 0,
        "registered_at": datetime.now(),
        "is_admin": False,
        "is_mentor": False
    }
    
    # Add OGS data if available
    if data.get('ogs_id'):
        user_data.update({
            "ogs_id": data['ogs_id'],
            "ogs_rank": data['ogs_rank'],
            "ogs_wins": data['ogs_wins'],
            "ogs_losses": data['ogs_losses'],
            "last_ogs_update": datetime.now()
        })
    
    # Check if user is eligible to be a mentor based on rank
    if is_rank_sufficient_for_mentor(data['rank']):
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton("Yes, become a mentor", callback_data="become_mentor"),
            InlineKeyboardButton("No, maybe later", callback_data="skip_mentor")
        )
        
        await users_collection.insert_one(user_data)
        await message.answer(
            f"Registration complete! Welcome to the Go Club, {data['name']}!\n\n"
            f"Your rank ({data['rank']}) qualifies you to become a mentor. "
            f"Would you like to register as a mentor and offer your services?",
            reply_markup=keyboard
        )
    else:
        await users_collection.insert_one(user_data)
        
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        keyboard.add(
            KeyboardButton("Leaderboard"),
            KeyboardButton("Events"),
            KeyboardButton("My Profile"),
            KeyboardButton("Record Match"),
            KeyboardButton("Find Mentor"),
            KeyboardButton("Help")
        )
        
        await message.answer(
            f"Registration complete! Welcome to the Go Club, {data['name']}!",
            reply_markup=keyboard
        )
    
    await state.finish()

@dp.message_handler(Text(equals="My Profile", ignore_case=True))
async def show_profile(message: types.Message):
    user = await users_collection.find_one({"telegram_id": message.from_user.id})
    
    if not user:
        await message.answer("You need to register first. Use /register to get started.")
        return
    
    # Update OGS stats if username is available and last update was more than a day ago
    if user.get("ogs_username") and (
        not user.get("last_ogs_update") or 
        datetime.now() - user["last_ogs_update"] > timedelta(days=1)
    ):
        await update_user_ogs_stats(message.from_user.id)
        user = await users_collection.find_one({"telegram_id": message.from_user.id})
    
    # Create profile message
    name = user.get("name", "Unknown")
    rank = user.get("rank", "N/A")
    wins = user.get("wins", 0)
    losses = user.get("losses", 0)
    registered_at = user.get("registered_at", datetime.now()).strftime("%Y-%m-%d")
    
    profile = f"👤 *Profile: {name}*\n\n"
    profile += f"🥋 *Rank*: {rank}\n"
    profile += f"📊 *Club Record*: {wins}W/{losses}L\n"
    profile += f"📅 *Member since*: {registered_at}\n\n"
    
    # Add OGS info if available
    if user.get("ogs_username"):
        ogs_username = user.get("ogs_username")
        ogs_rank = user.get("ogs_rank", "N/A")
        ogs_wins = user.get("ogs_wins", 0)
        ogs_losses = user.get("ogs_losses", 0)
        
        profile += f"🌐 *OGS Profile*\n"
        profile += f"👤 *Username*: {ogs_username}\n"
        profile += f"🥋 *OGS Rank*: {ogs_rank}\n"
        profile += f"📊 *OGS Record*: {ogs_wins}W/{ogs_losses}L\n\n"
    
    # Add mentor status if applicable
    if user.get("is_mentor", False):
        profile += "👨‍🏫 *Mentor Status*: Active\n\n"
    
    # Add admin status if applicable
    if user.get("is_admin", False):
        profile += "👑 *Admin Status*: Active\n\n"
    
    # Create profile actions keyboard
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Update Rank", callback_data="update_rank"),
        InlineKeyboardButton("Update OGS Username", callback_data="update_ogs"),
    )
    
    # Add mentor-related buttons
    if user.get("is_mentor", False):
        keyboard.add(InlineKeyboardButton("Update Mentor Profile", callback_data="update_mentor"))
    elif is_rank_sufficient_for_mentor(user.get("rank", "30k")):
        keyboard.add(InlineKeyboardButton("Become a Mentor", callback_data="become_mentor"))
    
    await message.answer(profile, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@dp.message_handler(Text(equals="Leaderboard", ignore_case=True))
async def show_leaderboard(message: types.Message):
    users = await users_collection.find().to_list(length=100)
    leaderboard = create_leaderboard_message(users)
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Update OGS Stats", callback_data="update_leaderboard"))
    
    await message.answer(leaderboard, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query_handler(lambda c: c.data == "update_leaderboard")
async def update_leaderboard(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id, text="Updating leaderboard...")
    
    users = await users_collection.find({"ogs_username": {"$ne": None}}).to_list(length=50)
    
    update_message = "Updating OGS stats for players...\n"
    await bot.send_message(callback_query.from_user.id, update_message)
    
    for user in users:
        await update_user_ogs_stats(user["telegram_id"])
    
    updated_users = await users_collection.find().to_list(length=100)
    leaderboard = create_leaderboard_message(updated_users)
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Update OGS Stats", callback_data="update_leaderboard"))
    
    await bot.send_message(
        callback_query.from_user.id, 
        "Leaderboard updated!\n\n" + leaderboard,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message_handler(Text(equals="Events", ignore_case=True))
async def show_events(message: types.Message):
    # Get upcoming events
    current_date = datetime.now()
    upcoming_events = await events_collection.find(
        {"date_time": {"$gte": current_date}}
    ).sort("date_time", 1).to_list(length=10)
    
    if not upcoming_events:
        keyboard = InlineKeyboardMarkup()
        
        # Add button to create event if user is admin
        user = await users_collection.find_one({"telegram_id": message.from_user.id})
        if user and user.get("is_admin", False):
            keyboard.add(InlineKeyboardButton("Create Event", callback_data="create_event"))
            await message.answer(
                "There are no upcoming events. As an admin, you can create one!",
                reply_markup=keyboard
            )
        else:
            await message.answer("There are no upcoming events currently. Check back later!")
        
        return
    
    # Create events list with navigation
    events_keyboard = InlineKeyboardMarkup(row_width=1)
    
    for event in upcoming_events:
        event_date = event.get("date", "TBD")
        event_title = event.get("title", "Event")
        events_keyboard.add(
            InlineKeyboardButton(
                f"{event_date} - {event_title}",
                callback_data=f"event_{event['_id']}"
            )
        )
    
    # Add button to create event if user is admin
    user = await users_collection.find_one({"telegram_id": message.from_user.id})
    if user and user.get("is_admin", False):
        events_keyboard.add(InlineKeyboardButton("Create Event", callback_data="create_event"))
    
    await message.answer(
        "📅 *Upcoming Events* 📅\n\n"
        "Select an event to view details:",
        reply_markup=events_keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data.startswith('event_'))
async def show_event_details(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    event_id = callback_query.data.split('_')[1]
    # Handle both string IDs and ObjectId based on the format
    if event_id.startswith("event_"):
        event = await events_collection.find_one({"_id": event_id})
    else:
        try:
            event = await events_collection.find_one({"_id": ObjectId(event_id)})
        except:
            event = await events_collection.find_one({"_id": event_id})
        
    if not event:
        await bot.send_message(
            callback_query.from_user.id,
            "Event not found. It may have been removed."
        )
        return
    
    event_message = create_event_message(event)
    
    # Create event action keyboard
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("RSVP", callback_data=f"rsvp_{event_id}"),
        InlineKeyboardButton("Back to Events", callback_data="show_events")
    )
    
    # Add admin actions if user is admin
    user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
    if user and user.get("is_admin", False):
        keyboard.add(
            InlineKeyboardButton("Edit Event", callback_data=f"edit_event_{event_id}"),
            InlineKeyboardButton("Delete Event", callback_data=f"delete_event_{event_id}")
        )
    
    await bot.send_message(
        callback_query.from_user.id,
        event_message,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message_handler(Text(equals="Record Match", ignore_case=True))
async def record_match_start(message: types.Message):
    # Get list of club members for opponent selection
    users = await users_collection.find(
        {"telegram_id": {"$ne": message.from_user.id}}
    ).sort("name", 1).to_list(length=100)
    
    if not users:
        await message.answer(
            "There are no other club members registered yet. "
            "Invite your friends to join the club!"
        )
        return
    
    # Create opponent selection keyboard
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for user in users:
        keyboard.add(
            InlineKeyboardButton(
                f"{user['name']} ({user['rank']})",
                callback_data=f"opponent_{user['telegram_id']}"
            )
        )
    
    # Add option for external opponent
    keyboard.add(InlineKeyboardButton("External Opponent", callback_data="opponent_external"))
    
    await message.answer(
        "Who did you play against? Select an opponent:",
        reply_markup=keyboard
    )
    
    await MatchForm.opponent.set()

@dp.callback_query_handler(lambda c: c.data.startswith('opponent_'), state=MatchForm.opponent)
async def process_opponent(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    
    opponent_id = callback_query.data.split('_')[1]
    
    async with state.proxy() as data:
        data['opponent_id'] = opponent_id
    
    # Get opponent info if it's a club member
    if opponent_id != "external":
        opponent = await users_collection.find_one({"telegram_id": int(opponent_id)})
        async with state.proxy() as data:
            data['opponent_name'] = opponent['name']
            data['opponent_rank'] = opponent['rank']
    
    # Create result selection keyboard
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("I won", callback_data="result_win"),
        InlineKeyboardButton("I lost", callback_data="result_loss")
    )
    
    await MatchForm.next()
    await bot.send_message(
        callback_query.from_user.id,
        "What was the result of the match?",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('result_'), state=MatchForm.result)
async def process_result(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    
    result = callback_query.data.split('_')[1]
    
    async with state.proxy() as data:
        data['result'] = result
    
    await MatchForm.next()
    await bot.send_message(
        callback_query.from_user.id,
        "Do you have an OGS game link? If yes, please paste it. If no, type 'none'."
    )

@dp.message_handler(state=MatchForm.ogs_link)
async def process_match_ogs_link(message: types.Message, state: FSMContext):
    ogs_link = message.text.strip()
    
    if ogs_link.lower() == 'none':
        ogs_link = None
    
    async with state.proxy() as data:
        data['ogs_link'] = ogs_link
        
        # Process match result
        user_id = message.from_user.id
        opponent_id = data['opponent_id']
        result = data['result']
        
        # Get user data
        user = await users_collection.find_one({"telegram_id": user_id})
        
        # Create match record
        match_data = {
            "date": datetime.now(),
            "player1_id": user_id,
            "player1_name": user['name'],
            "player1_rank": user['rank'],
            "result": result,
            "ogs_link": ogs_link
        }
        
        # Update player records
        if result == "win":
            await users_collection.update_one(
                {"telegram_id": user_id},
                {"$inc": {"wins": 1}}
            )
            
            if opponent_id != "external":
                opponent = await users_collection.find_one({"telegram_id": int(opponent_id)})
                match_data.update({
                    "player2_id": int(opponent_id),
                    "player2_name": opponent['name'],
                    "player2_rank": opponent['rank']
                })
                
                await users_collection.update_one(
                    {"telegram_id": int(opponent_id)},
                    {"$inc": {"losses": 1}}
                )
            else:
                match_data.update({
                    "player2_id": None,
                    "player2_name": "External Opponent",
                    "player2_rank": "Unknown"
                })
        else:  # loss
            await users_collection.update_one(
                {"telegram_id": user_id},
                {"$inc": {"losses": 1}}
            )
            
            if opponent_id != "external":
                opponent = await users_collection.find_one({"telegram_id": int(opponent_id)})
                match_data.update({
                    "player2_id": int(opponent_id),
                    "player2_name": opponent['name'],
                    "player2_rank": opponent['rank']
                })
                
                await users_collection.update_one(
                    {"telegram_id": int(opponent_id)},
                    {"$inc": {"wins": 1}}
                )
            else:
                match_data.update({
                    "player2_id": None,
                    "player2_name": "External Opponent",
                    "player2_rank": "Unknown"
                })
        
        # Save match to database
        await matches_collection.insert_one(match_data)
    
    # Finish the state
    await state.finish()
    
    await message.answer(
        "Match recorded successfully! The leaderboard has been updated."
    )
async def setup_bot_commands(bot):
    """Set up bot commands for the menu."""
    commands = [
        types.BotCommand(command="start", description="Start the bot"),
        types.BotCommand(command="register", description="Register your account"),
        types.BotCommand(command="help", description="Get help using the bot")
    ]
    await bot.set_my_commands(commands)

@dp.message_handler(Text(equals="Find Mentor", ignore_case=True))
async def find_mentor(message: types.Message):
    # Get available mentors
    mentors = await users_collection.find(
        {"is_mentor": True}
    ).sort("rank", -1).to_list(length=50)
    
    if not mentors:
        await message.answer(
            "There are no mentors available at the moment. "
            "Check back later or ask club admins about mentorship opportunities."
        )
        return
    
    # Create mentor selection keyboard
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for mentor in mentors:
        keyboard.add(
            InlineKeyboardButton(
                f"{mentor['name']} ({mentor['rank']})",
                callback_data=f"view_mentor_{mentor['telegram_id']}"
            )
        )
    
    await message.answer(
        "👨‍🏫 *Available Mentors* 👨‍🏫\n\n"
        "Select a mentor to view their profile:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data.startswith('view_mentor_'))
async def view_mentor(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    mentor_id = int(callback_query.data.split('_')[2])
    mentor = await users_collection.find_one({"telegram_id": mentor_id})
    
    if not mentor:
        await bot.send_message(
            callback_query.from_user.id,
            "Mentor not found. They may have deactivated their mentorship."
        )
        return
    
    mentor_message = create_mentor_message(mentor)
    
    # Check if user already has a subscription with this mentor
    subscription = await subscriptions_collection.find_one({
        "mentee_id": callback_query.from_user.id,
        "mentor_id": mentor_id,
        "status": "active"
    })
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    if subscription:
        keyboard.add(
            InlineKeyboardButton("Send Message", callback_data=f"message_mentor_{mentor_id}"),
            InlineKeyboardButton("Cancel Subscription", callback_data=f"cancel_sub_{subscription['_id']}")
        )
        
        mentor_message += "\n✅ *You are currently subscribed to this mentor*"
    else:
        keyboard.add(
            InlineKeyboardButton("Subscribe", callback_data=f"subscribe_{mentor_id}")
        )
    
    keyboard.add(InlineKeyboardButton("Back to Mentor List", callback_data="find_mentors"))
    
    await bot.send_message(
        callback_query.from_user.id,
        mentor_message,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data.startswith('subscribe_'))
async def subscribe_to_mentor(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    mentor_id = int(callback_query.data.split('_')[1])
    mentor = await users_collection.find_one({"telegram_id": mentor_id})
    
    if not mentor:
        await bot.send_message(
            callback_query.from_user.id,
            "Mentor not found. They may have deactivated their mentorship."
        )
        return
    
    # Create payment keyboard
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Pay via Bank Transfer", callback_data=f"pay_bank_{mentor_id}"),
        InlineKeyboardButton("Pay via CryptoCurrency", callback_data=f"pay_crypto_{mentor_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"view_mentor_{mentor_id}")
    )
    
    await bot.send_message(
        callback_query.from_user.id,
        f"You are about to subscribe to {mentor['name']} for mentorship.\n\n"
        f"Monthly fee: {mentor.get('mentor_price', 'Not specified')}\n\n"
        f"Please select your payment method:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('pay_'))
async def process_payment(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    parts = callback_query.data.split('_')
    payment_method = parts[1]
    mentor_id = int(parts[2])
    
    mentor = await users_collection.find_one({"telegram_id": mentor_id})
    user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
    
    if not mentor or not user:
        await bot.send_message(
            callback_query.from_user.id,
            "Error processing your request. Please try again later."
        )
        return
    
    # Simulate payment process (in a real bot, integrate with payment provider)
    # Here we're just creating the subscription record directly
    
    subscription_id = f"sub_{mentor_id}_{callback_query.from_user.id}_{int(datetime.now().timestamp())}"
    
    subscription_data = {
        "_id": subscription_id,
        "mentor_id": mentor_id,
        "mentor_name": mentor['name'],
        "mentee_id": callback_query.from_user.id,
        "mentee_name": user['name'],
        "status": "active",
        "payment_method": payment_method,
        "start_date": datetime.now(),
        "end_date": datetime.now() + timedelta(days=30),
        "price": mentor.get('mentor_price', "Not specified")
    }
    
    await subscriptions_collection.insert_one(subscription_data)
    
    # Notify the mentor
    try:
        await bot.send_message(
            mentor_id,
            f"🎉 New subscription!\n\n"
            f"{user['name']} has subscribed to your mentorship services.\n"
            f"You can now communicate directly with them through this bot."
        )
    except Exception as e:
        logging.error(f"Failed to notify mentor: {e}")
    
    # Provide payment instructions to the user
    if payment_method == "bank":
        instructions = (
            "Please complete your payment using the following bank details:\n\n"
            "Bank: Example Bank\n"
            "Account Name: Go Club\n"
            "Account Number: 1234567890\n"
            "Reference: MENTOR-" + subscription_id
        )
    else:  # crypto
        instructions = (
            "Please complete your payment using the following cryptocurrency address:\n\n"
            "Bitcoin: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\n"
            "Ethereum: 0x742d35Cc6634C0532925a3b844Bc454e4438f44e\n"
            "Reference: MENTOR-" + subscription_id
        )
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Message Mentor", callback_data=f"message_mentor_{mentor_id}"))
    
    await bot.send_message(
        callback_query.from_user.id,
        f"✅ Subscription successful!\n\n"
        f"You are now subscribed to {mentor['name']} for one month.\n\n"
        f"Payment Instructions:\n{instructions}\n\n"
        f"Once your payment is confirmed, you can start messaging your mentor.",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('message_mentor_'))
async def message_mentor_start(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    mentor_id = int(callback_query.data.split('_')[2])
    
    # Verify subscription
    subscription = await subscriptions_collection.find_one({
        "mentee_id": callback_query.from_user.id,
        "mentor_id": mentor_id,
        "status": "active"
    })
    
    if not subscription:
        await bot.send_message(
            callback_query.from_user.id,
            "You don't have an active subscription with this mentor. "
            "Please subscribe first to send messages."
        )
        return
    
    await MenteeForm.message.set()
    
    # Store mentor ID in state
    state = dp.current_state(user=callback_query.from_user.id)
    async with state.proxy() as data:
        data['mentor_id'] = mentor_id
    
    await bot.send_message(
        callback_query.from_user.id,
        "What message would you like to send to your mentor? "
        "You can ask questions about Go strategy, game reviews, or schedule a session."
    )

@dp.message_handler(state=MenteeForm.message)
async def send_message_to_mentor(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        mentor_id = data['mentor_id']
    
    user = await users_collection.find_one({"telegram_id": message.from_user.id})
    mentor = await users_collection.find_one({"telegram_id": mentor_id})
    
    if not user or not mentor:
        await message.answer("Error processing your message. Please try again later.")
        await state.finish()
        return
    
    # Send message to mentor
    try:
        await bot.send_message(
            mentor_id,
            f"📨 *New message from your mentee {user['name']}*:\n\n"
            f"{message.text}\n\n"
            f"Reply with /reply_{message.from_user.id} followed by your message.",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await message.answer(
            "Your message has been sent to your mentor. "
            "You will receive their reply directly in this chat."
        )
    except Exception as e:
        logging.error(f"Failed to send message to mentor: {e}")
        await message.answer(
            "Failed to send your message. The mentor may have blocked the bot. "
            "Please contact club administrators for assistance."
        )
    
    await state.finish()

@dp.message_handler(lambda message: message.text.startswith('/reply_'))
async def reply_to_mentee(message: types.Message):
    # Extract mentee ID and message content
    parts = message.text.split(' ', 1)
    
    if len(parts) < 2:
        await message.answer("Please include a message after /reply_[id]")
        return
    
    mentee_id_str = parts[0].split('_')[1]
    reply_text = parts[1]
    
    try:
        mentee_id = int(mentee_id_str)
    except ValueError:
        await message.answer("Invalid mentee ID format.")
        return
    
    # Verify mentor-mentee relationship
    subscription = await subscriptions_collection.find_one({
        "mentee_id": mentee_id,
        "mentor_id": message.from_user.id,
        "status": "active"
    })
    
    if not subscription:
        await message.answer(
            "You don't have an active mentorship with this user. "
            "They may have cancelled their subscription."
        )
        return
    
    # Get mentor info
    mentor = await users_collection.find_one({"telegram_id": message.from_user.id})
    
    # Send reply to mentee
    try:
        await bot.send_message(
            mentee_id,
            f"📨 *Reply from your mentor {mentor['name']}*:\n\n"
            f"{reply_text}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await message.answer("Your reply has been sent to your mentee.")
    except Exception as e:
        logging.error(f"Failed to send reply to mentee: {e}")
        await message.answer(
            "Failed to send your reply. The mentee may have blocked the bot. "
            "Please contact club administrators for assistance."
        )

@dp.callback_query_handler(lambda c: c.data.startswith('become_mentor'))
async def become_mentor_start(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    # Verify user rank
    user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
    
    if not user:
        await bot.send_message(
            callback_query.from_user.id,
            "You need to register first. Use /register to get started."
        )
        return
    
    if not is_rank_sufficient_for_mentor(user.get('rank', '30k')):
        await bot.send_message(
            callback_query.from_user.id,
            f"Your current rank ({user.get('rank', '30k')}) is not sufficient to become a mentor. "
            f"The minimum required rank is {MENTOR_MINIMUM_RANK}."
        )
        return
    
    await MentorForm.description.set()
    
    await bot.send_message(
        callback_query.from_user.id,
        "Let's set up your mentor profile. "
        "First, please provide a brief description of your teaching approach, "
        "experience, and what students can expect from your mentorship."
    )

@dp.message_handler(state=MentorForm.description)
async def process_mentor_description(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['description'] = message.text
    
    await MentorForm.next()
    await message.answer(
        "Great! Now, please specify your availability for mentoring sessions. "
        "For example: 'Weekdays 6-9 PM, Weekends 10 AM-5 PM'."
    )

@dp.message_handler(state=MentorForm.availability)
async def process_mentor_availability(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['availability'] = message.text
    
    await MentorForm.next()
    await message.answer(
        "Finally, please set your monthly subscription price for mentees. "
        "This should include all services you offer (game reviews, teaching games, etc.)."
    )

@dp.message_handler(state=MentorForm.price)
async def process_mentor_price(message: types.Message, state: FSMContext):
    price = message.text.strip()
    
    async with state.proxy() as data:
        data['price'] = price
        
        # Update user as mentor
        await users_collection.update_one(
            {"telegram_id": message.from_user.id},
            {"$set": {
                "is_mentor": True,
                "mentor_description": data['description'],
                "mentor_availability": data['availability'],
                "mentor_price": price,
                "mentor_since": datetime.now()
            }}
        )
    
    await state.finish()
    
    await message.answer(
        "🎉 Congratulations! You are now registered as a mentor. "
        "Your profile is visible to club members looking for mentorship. "
        "You will receive notifications when someone subscribes to your services."
    )

@dp.message_handler(Text(equals="Admin Panel", ignore_case=True))
async def admin_panel(message: types.Message):
    # Verify user is admin
    user = await users_collection.find_one({"telegram_id": message.from_user.id})
    
    if not user or not user.get("is_admin", False):
        await message.answer("You don't have permission to access the admin panel.")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Create Event", callback_data="create_event"),
        InlineKeyboardButton("Broadcast Announcement", callback_data="broadcast"),
        InlineKeyboardButton("Manage Users", callback_data="manage_users"),
        InlineKeyboardButton("View Subscriptions", callback_data="view_subscriptions")
    )
    
    await message.answer(
        "👑 *Admin Panel* 👑\n\n"
        "Select an action:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data == "create_event")
async def create_event_start(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    # Verify user is admin
    user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
    
    if not user or not user.get("is_admin", False):
        await bot.send_message(
            callback_query.from_user.id,
            "You don't have permission to create events."
        )
        return
    
    await EventForm.title.set()
    
    await bot.send_message(
        callback_query.from_user.id,
        "Let's create a new event. What's the title of the event?"
    )

@dp.message_handler(state=EventForm.title)
async def process_event_title(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['title'] = message.text
    
    await EventForm.next()
    await message.answer("Please provide a description for the event.")

@dp.message_handler(state=EventForm.description)
async def process_event_description(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['description'] = message.text
    
    await EventForm.next()
    await message.answer(
        "When will the event take place? Please use the format YYYY-MM-DD "
        "(e.g., 2025-03-15)."
    )

@dp.message_handler(state=EventForm.date)
async def process_event_date(message: types.Message, state: FSMContext):
    try:
        date_str = message.text.strip()
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        
        async with state.proxy() as data:
            data['date'] = date_str
        
        await EventForm.next()
        await message.answer(
            "What time will the event start? Please use the format HH:MM "
            "(e.g., 18:30)."
        )
    except ValueError:
        await message.answer(
            "Invalid date format. Please use YYYY-MM-DD (e.g., 2025-03-15)."
        )

@dp.message_handler(state=EventForm.time)
async def process_event_time(message: types.Message, state: FSMContext):
    try:
        time_str = message.text.strip()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
        
        async with state.proxy() as data:
            data['time'] = time_str
            
            # Combine date and time
            date_time_str = f"{data['date']} {time_str}"
            data['date_time'] = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
        
        await EventForm.next()
        await message.answer("Where will the event take place?")
    except ValueError:
        await message.answer(
            "Invalid time format. Please use HH:MM (e.g., 18:30)."
        )

@dp.message_handler(state=EventForm.location)
async def process_event_location(message: types.Message, state: FSMContext):
    location = message.text.strip()
    
    async with state.proxy() as data:
        data['location'] = location
        
        # Create event document
        event_data = {
            "_id": f"event_{int(datetime.now().timestamp())}",
            "title": data['title'],
            "description": data['description'],
            "date": data['date'],
            "time": data['time'],
            "date_time": data['date_time'],
            "location": location,
            "created_by": message.from_user.id,
            "created_at": datetime.now(),
            "participants": []
        }
        
        # Save event to database
        await events_collection.insert_one(event_data)
    
    await state.finish()
    
    # Notify club members about the new event
    all_users = await users_collection.find().to_list(length=1000)
    
    event_notification = (
        f"📅 *New Event: {data['title']}* 📅\n\n"
        f"📆 *Date*: {data['date']}\n"
        f"🕒 *Time*: {data['time']}\n"
        f"📍 *Location*: {location}\n\n"
        f"Check the Events section for more details!"
    )
    
    for user in all_users:
        try:
            await bot.send_message(
                user['telegram_id'],
                event_notification,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logging.error(f"Failed to notify user {user['telegram_id']}: {e}")
    
    await message.answer(
        "Event created successfully! All club members have been notified."
    )

@dp.callback_query_handler(lambda c: c.data == "broadcast")
async def broadcast_start(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    # Verify user is admin
    user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
    
    if not user or not user.get("is_admin", False):
        await bot.send_message(
            callback_query.from_user.id,
            "You don't have permission to broadcast announcements."
        )
        return
    
    # Create a state for broadcast message
    class BroadcastForm(StatesGroup):
        message = State()
    
    await BroadcastForm.message.set()
    
    await bot.send_message(
        callback_query.from_user.id,
        "Please enter the announcement message you want to broadcast to all club members. "
        "You can use Markdown formatting."
    )
    
    @dp.message_handler(state=BroadcastForm.message)
    async def process_broadcast_message(message: types.Message, state: FSMContext):
        broadcast_message = message.text
        
        # Get all users
        all_users = await users_collection.find().to_list(length=1000)
        
        # Send confirmation
        await message.answer(
            f"You are about to broadcast the following message to {len(all_users)} members:\n\n"
            f"{broadcast_message}\n\n"
            f"Are you sure? (yes/no)"
        )
        
        # Create a state for confirmation
        class ConfirmBroadcast(StatesGroup):
            confirm = State()
        
        await ConfirmBroadcast.confirm.set()
        
        async with state.proxy() as data:
            data['message'] = broadcast_message
        
        @dp.message_handler(state=ConfirmBroadcast.confirm)
        async def confirm_broadcast(message: types.Message, state: FSMContext):
            if message.text.lower() != "yes":
                await message.answer("Broadcast cancelled.")
                await state.finish()
                return
            
            async with state.proxy() as data:
                broadcast_message = data['message']
            
            # Get all users
            all_users = await users_collection.find().to_list(length=1000)
            
            # Send the broadcast
            success_count = 0
            fail_count = 0
            
            await message.answer("Broadcasting message... Please wait.")
            
            for user in all_users:
                try:
                    await bot.send_message(
                        user['telegram_id'],
                        f"📢 *ANNOUNCEMENT* 📢\n\n{broadcast_message}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    success_count += 1
                except Exception as e:
                    logging.error(f"Failed to send broadcast to user {user['telegram_id']}: {e}")
                    fail_count += 1
            
            await message.answer(
                f"Broadcast complete!\n\n"
                f"✅ Successfully sent to {success_count} members\n"
                f"❌ Failed to send to {fail_count} members"
            )
            
            await state.finish()

@dp.callback_query_handler(lambda c: c.data == "find_mentors")
async def find_mentors_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    # Get available mentors
    mentors = await users_collection.find(
        {"is_mentor": True}
    ).sort("rank", -1).to_list(length=50)
    
    if not mentors:
        await bot.send_message(
            callback_query.from_user.id,
            "There are no mentors available at the moment. "
            "Check back later or ask club admins about mentorship opportunities."
        )
        return
    
    # Create mentor selection keyboard
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for mentor in mentors:
        keyboard.add(
            InlineKeyboardButton(
                f"{mentor['name']} ({mentor['rank']})",
                callback_data=f"view_mentor_{mentor['telegram_id']}"
            )
        )
    
    await bot.send_message(
        callback_query.from_user.id,
        "👨‍🏫 *Available Mentors* 👨‍🏫\n\n"
        "Select a mentor to view their profile:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data == "show_events")
async def show_events_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    # Get upcoming events
    current_date = datetime.now()
    upcoming_events = await events_collection.find(
        {"date_time": {"$gte": current_date}}
    ).sort("date_time", 1).to_list(length=10)
    
    if not upcoming_events:
        keyboard = InlineKeyboardMarkup()
        
        # Add button to create event if user is admin
        user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
        if user and user.get("is_admin", False):
            keyboard.add(InlineKeyboardButton("Create Event", callback_data="create_event"))
            await bot.send_message(
                callback_query.from_user.id,
                "There are no upcoming events. As an admin, you can create one!",
                reply_markup=keyboard
            )
        else:
            await bot.send_message(
                callback_query.from_user.id,
                "There are no upcoming events currently. Check back later!"
            )
        
        return
    
    # Create events list with navigation
    events_keyboard = InlineKeyboardMarkup(row_width=1)
    
    for event in upcoming_events:
        event_date = event.get("date", "TBD")
        event_title = event.get("title", "Event")
        events_keyboard.add(
            InlineKeyboardButton(
                f"{event_date} - {event_title}",
                callback_data=f"event_{event['_id']}"
            )
        )
    
    # Add button to create event if user is admin
    user = await users_collection.find_one({"telegram_id": callback_query.from_user.id})
    if user and user.get("is_admin", False):
        events_keyboard.add(InlineKeyboardButton("Create Event", callback_data="create_event"))
    
    await bot.send_message(
        callback_query.from_user.id,
        "📅 *Upcoming Events* 📅\n\n"
        "Select an event to view details:",
        reply_markup=events_keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data.startswith('rsvp_'))
async def rsvp_event(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    event_id = callback_query.data.split('_')[1]
    user_id = callback_query.from_user.id
    
    # Check if event exists
    event = await events_collection.find_one({"_id": event_id})
    if not event:
        await bot.send_message(
            user_id,
            "The event no longer exists. It may have been cancelled."
        )
        return
    
    # Check if user is already RSVP'd
    participants = event.get("participants", [])
    user_ids = [p["user_id"] for p in participants]
    
    if user_id in user_ids:
        # Remove user from participants
        await events_collection.update_one(
            {"_id": event_id},
            {"$pull": {"participants": {"user_id": user_id}}}
        )
        
        await bot.send_message(
            user_id,
            f"You have cancelled your RSVP for the event '{event['title']}'."
        )
    else:
        # Get user info
        user = await users_collection.find_one({"telegram_id": user_id})
        if not user:
            await bot.send_message(
                user_id,
                "You need to register first. Use /register to get started."
            )
            return
        
        # Add user to participants
        participant_info = {
            "user_id": user_id,
            "name": user["name"],
            "rank": user["rank"],
            "rsvp_time": datetime.now()
        }
        
        await events_collection.update_one(
            {"_id": event_id},
            {"$push": {"participants": participant_info}}
        )
        
        # Send confirmation
        await bot.send_message(
            user_id,
            f"You have successfully RSVP'd for the event '{event['title']}'.\n"
            f"Date: {event['date']}\n"
            f"Time: {event['time']}\n"
            f"Location: {event['location']}"
        )
        
        # Notify event creator
        creator_id = event.get("created_by")
        if creator_id:
            try:
                await bot.send_message(
                    creator_id,
                    f"New RSVP for '{event['title']}'!\n"
                    f"{user['name']} ({user['rank']}) will be attending."
                )
            except Exception as e:
                logging.error(f"Failed to notify event creator: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('cancel_sub_'))
async def cancel_subscription(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    subscription_id = callback_query.data.split('_')[2]
    user_id = callback_query.from_user.id
    
    # Get subscription details
    subscription = await subscriptions_collection.find_one({"_id": ObjectId(subscription_id)})
    
    if not subscription:
        await bot.send_message(
            user_id,
            "Subscription not found. It may have already been cancelled."
        )
        return
    
    # Verify the user is the mentee
    if subscription["mentee_id"] != user_id:
        await bot.send_message(
            user_id,
            "You don't have permission to cancel this subscription."
        )
        return
    
    # Mark subscription as cancelled
    try:
        await subscriptions_collection.update_one(
            {"_id": ObjectId(subscription_id)},
            {"$set": {
                "status": "cancelled",
                "cancelled_at": datetime.now()
            }}
        )
    except:
    # In case _id is stored as a string
        await subscriptions_collection.update_one(
            {"_id": subscription_id},
            {"$set": {
                "status": "cancelled",
                "cancelled_at": datetime.now()
            }}
        )
    
    # Get mentor details
    mentor_id = subscription["mentor_id"]
    mentor = await users_collection.find_one({"user_id": mentor_id})
    mentee = await users_collection.find_one({"user_id": user_id})
    
    # Format mentor's notification
    mentor_message = (
        f"❌ Subscription Cancelled\n\n"
        f"Mentee: {mentee.get('first_name', 'Unknown')}\n"
        f"Duration: {subscription.get('duration', 'Unknown')}\n"
        f"Started: {subscription.get('created_at').strftime('%Y-%m-%d')}\n"
        f"Cancelled: {datetime.now().strftime('%Y-%m-%d')}"
    )
    
    # Notify mentor
    try:
        await bot.send_message(mentor_id, mentor_message)
    except Exception as e:
        logging.error(f"Failed to notify mentor {mentor_id}: {e}")
    
    # Confirm cancellation to mentee
    await bot.send_message(
        user_id,
        "Your subscription has been cancelled successfully. "
        "Thank you for using our mentorship service."
    )
