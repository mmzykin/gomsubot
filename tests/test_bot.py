import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from bson.objectid import ObjectId
from aiogram import types
from aiogram.dispatcher import FSMContext

# Import modules to test
import bot

# Mock environment variables and MongoDB
@pytest.fixture
def setup_mocks():
    # Mock MongoDB collections
    bot.users_collection = AsyncMock()
    bot.events_collection = AsyncMock()
    bot.matches_collection = AsyncMock()
    bot.subscriptions_collection = AsyncMock()
    
    # Mock bot for sending messages
    bot.bot = AsyncMock()
    
    # Create a mock FSMContext
    async def mock_get_data():
        return {}
    
    state = AsyncMock()
    state.get_data = mock_get_data
    state.finish = AsyncMock()
    
    return {
        'state': state
    }

@pytest.mark.asyncio
async def test_cmd_start_new_user(setup_mocks):
    # Create mock message
    message = AsyncMock()
    message.from_user.id = 123456789
    
    # Configure mock to return None (new user)
    bot.users_collection.find_one.return_value = None
    
    # Call the function
    await bot.cmd_start(message)
    
    # Check user is prompted to register
    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert 'register' in args[0].lower()
    
@pytest.mark.asyncio
async def test_cmd_start_existing_user(setup_mocks):
    # Create mock message
    message = AsyncMock()
    message.from_user.id = 123456789
    
    # Configure mock to return a user
    bot.users_collection.find_one.return_value = {
        'name': 'Test User',
        'is_admin': False,
        'is_mentor': False
    }
    
    # Call the function
    await bot.cmd_start(message)
    
    # Check welcome back message
    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert 'welcome back' in args[0].lower()
    
@pytest.mark.asyncio
async def test_fetch_ogs_data_success():
    username = "testplayer"
    
    # Mock aiohttp ClientSession
    with patch('aiohttp.ClientSession') as mock_session:
        # Configure successful responses
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(side_effect=[
            {"results": [{"id": 12345, "username": username}]},
            {"username": username, "ranking": "5k", "wins": 10, "losses": 5},
            {"results": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}, {"id": 6}]}
        ])
        
        # Configure session context managers
        mock_cm = MagicMock()
        mock_cm.__aenter__.return_value = mock_resp
        mock_session.return_value.__aenter__.return_value.get.return_value = mock_cm
        
        # Call the function
        result = await bot.fetch_ogs_data(username)
        
        # Check result
        assert "error" not in result
        assert result["username"] == username
        assert result["rank"] == "5k"
        assert result["wins"] == 10
        assert result["losses"] == 5
        assert len(result["recent_games"]) == 5  # Should only take first 5

@pytest.mark.asyncio
async def test_process_name(setup_mocks):
    # Create mock message and state
    message = AsyncMock()
    message.text = "Test Player"
    state = setup_mocks['state']
    
    # Mock state proxy context manager
    state_proxy = {}
    async def mock_state_proxy():
        return state_proxy
    
    state.__call__ = mock_state_proxy
    
    # Call the function
    await bot.process_name(message, state)
    
    # Check state was updated
    assert state_proxy.get('name') == "Test Player"
    
    # Check next state was set
    state.next.assert_called_once()
    
    # Check keyboard was sent
    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert 'rank' in args[0].lower()
    assert 'reply_markup' in kwargs
