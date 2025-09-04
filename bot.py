import re
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup
from datetime import datetime
from telegram.ext import Application

from collections import defaultdict

import os
import time
import signal
import sys
import logging
import asyncio
from datetime import datetime, timedelta
from telegram.ext import PicklePersistence

import mysql.connector
from mysql.connector import pooling

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy import Column, Integer, String

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes, CallbackContext
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MYSQL_CONFIG = {
    "host": os.getenv("DB_HOST"),
    # "host": "db",
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    # "password": "admin123",
    "database": os.getenv("DB_NAME"),
    # "database": "GoshneYad",
    "pool_size": 5,
    "pool_name": "mypool",
    "connect_timeout": 30
}

SQLALCHEMY_URL = (
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{MYSQL_CONFIG['password']}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
)

MAX_RETRIES = 3
DB_RECONNECT_INTERVAL = 60

# Batching and Delay Configuration
BATCH_SIZE = 50
DELAY_BETWEEN_MESSAGES = 0.1
DELAY_BETWEEN_BATCHES = 1

RETRY_BATCH_SIZE = 20
RETRY_DELAY_BETWEEN_MESSAGES = 0.2
RETRY_DELAY_BETWEEN_BATCHES = 2
# ─── Conversation info  ─────────────────────────────────────────────────
CHOOSING = 0
# ─── Rate Limiting Configuration ────────────────────────────────────
USER_LAST_REQUEST = defaultdict(float)
USER_PROCESSING = set()
REQUEST_COOLDOWN = 2
# ─── Main Buttons ───────────────────────────────────────────────
MAIN_MARKUP = ReplyKeyboardMarkup([
    ["تغییر دانشگاه", "غذای امروز؟"],
    ["غذای این هفته؟"],
], resize_keyboard=True)

# ─── University Configs  ───────────────────────────────────────────
UNIVERSITY_CONFIG = {
    "خوارزمی": {
        "day_of_week": "wed",
        "hour": 11,
        "minute": 0,
        "reminder_message": "⏰🤓رزرو کن غذاتو! همش یه روز مونده تا به جمع گشنه های شنبه و یکشنبه اضافه شی"
    },
    "تهران": {
        "day_of_week": "tue",
        "hour": 12,
        "minute": 0,
        "reminder_message": "⏰🤓رزرو کن غذاتو! همش یه روز مونده تا به جمع گشنه های شنبه و یکشنبه اضافه شی"
    },
    "خوارزمی تهران": {
        "day_of_week": "wed",
        "hour": 11,
        "minute": 0,
        "reminder_message": "⏰🤓رزرو کن غذاتو! همش یه روز مونده تا به جمع گشنه های شنبه و یکشنبه اضافه شی"
    }
}

# ─── Global Variables  ───────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo

    tehran_tz = ZoneInfo("Asia/Tehran")
except ImportError:
    import pytz

    tehran_tz = pytz.timezone("Asia/Tehran")

db_pool = None
bot_app = None
scheduler = AsyncIOScheduler(
    jobstores={
        'default': SQLAlchemyJobStore(url=SQLALCHEMY_URL)
    },
    job_defaults={
        'coalesce': True,
        'misfire_grace_time': 3600
    },
    timezone=tehran_tz
)


# ─── DataBase Operations  ────────────────────────────────────────────────
def init_db_pool():
    global db_pool
    try:
        logging.info("Try to connect to database pool (create)")
        db_pool = mysql.connector.pooling.MySQLConnectionPool(**MYSQL_CONFIG)

        conn = get_db_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
            logging.info("connected to database succesfully")
            return True
        else:
            logging.error("cannot connect to db")
            return False
    except mysql.connector.Error as err:
        logging.error(f"fatal error in connecting to pool{err}")
        return False


def get_db_connection():
    global db_pool
    if db_pool:
        try:
            conn = db_pool.get_connection()
            if conn.is_connected():
                return conn
            init_db_pool()
            return db_pool.get_connection() if db_pool else None
        except mysql.connector.Error as err:
            logging.error(f"pool error {err}")
            return None
    return None


def execute_query(query, params=None, commit=False, fetch=None):
    retries = 0
    while retries < MAX_RETRIES:
        try:
            conn = get_db_connection()
            if not conn:
                logging.error("database connection error")
                time.sleep(1)
                retries += 1
                continue

            cursor = conn.cursor()
            cursor.execute(query, params)

            result = None
            if fetch == "one":
                result = cursor.fetchone()
            elif fetch == "all":
                result = cursor.fetchall()

            if commit:
                conn.commit()

            cursor.close()
            conn.close()

            return result
        except mysql.connector.Error as err:
            retries += 1
            logging.error(f"خطای دیتابیس ({retries}/{MAX_RETRIES}): {err}")
            if retries >= MAX_RETRIES:
                logging.error("maximum tries failed.")
                raise
            time.sleep(1)


def create_required_tables():
    try:
        conn = get_db_connection()
        if not conn:
            logging.error("fatal error in creating tables (connection error)")
            return False

        cursor = conn.cursor()

        users_table = """
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            university VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
        """

        failed_reminders_table = """
        CREATE TABLE IF NOT EXISTS failed_reminders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            university VARCHAR(50) NOT NULL,
            message TEXT NOT NULL,
            retry_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scheduled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX (chat_id)
        )
        """

        cursor.execute(users_table)
        cursor.execute(failed_reminders_table)
        conn.commit()
        cursor.close()
        conn.close()

        logging.info("tables created successfully")
        return True

    except mysql.connector.Error as err:
        logging.error(f"fatal in creating tables: {err}")
        return False


def clean_food_name(food):
    return re.sub(r"(،|\(|\[)?\s*(رایگان|\d{2,3}(,\d{3})?)\s*(تومان|ریال)?\)?$", "", food).strip()


def get_today_name():
    today = datetime.now()
    weekday = today.weekday()

    days_mapping = {
        0: "دوشنبه",
        1: "سه شنبه",
        2: "چهارشنبه",
        3: "پنج شنبه",
        4: "جمعه",
        5: "شنبه",
        6: "یکشنبه"
    }

    return days_mapping[weekday]


def parse_food_schedule(html, university=None):
    try:
        soup = BeautifulSoup(html, "html.parser")
        schedule = {}

        day_containers = soup.find_all("div", class_="dayContainer")

        for day_container in day_containers:
            day_span = day_container.find(class_="day")
            date_span = day_container.find(class_="date")

            if day_span:
                day_name = day_span.get_text(strip=True)
                date = date_span.get_text(strip=True) if date_span else ""

                schedule[day_name] = {
                    "تاریخ": date,
                    "صبحانه": [],
                    "ناهار": [],
                    "شام": []
                }

                current_element = day_container

                while True:
                    current_element = current_element.find_next_sibling()
                    if not current_element or (
                            current_element.get('class') and 'dayContainer' in current_element.get('class')):
                        break

                    time_meal = current_element.find("span", class_="TimeMeal")
                    current_meal_type = None

                    if time_meal:
                        meal_text = time_meal.get_text(strip=True).lower()
                        if "صبحانه" in meal_text:
                            current_meal_type = "صبحانه"
                        elif "ناهار" in meal_text or "نهار" in meal_text:
                            current_meal_type = "ناهار"
                        elif "شام" in meal_text:
                            current_meal_type = "شام"

                    if current_meal_type:
                        meal_divs = current_element.find_all("div", id="MealDiv")
                        for meal_div in meal_divs:
                            food_labels = meal_div.find_all("label", class_="reserveFoodCheckBox")
                            for label in food_labels:
                                if label.get('for') and label.get_text(strip=True):
                                    food_text = clean_food_name(label.get_text(strip=True))
                                    if food_text and food_text not in schedule[day_name][current_meal_type]:
                                        schedule[day_name][current_meal_type].append(food_text)

        return schedule

    except Exception as e:
        print(f"خطا در خواندن برنامه غذایی: {e}")
        return {
            day: {"تاریخ": "", "صبحانه": [], "ناهار": [], "شام": []}
            for day in ["شنبه", "یکشنبه", "دوشنبه", "سه شنبه", "چهارشنبه", "پنج شنبه"]
        }


def merge_weekly_menus(menu1, menu2):
    merged_menu = {}
    days_order = ["شنبه", "یکشنبه", "دوشنبه", "سه شنبه", "چهارشنبه", "پنج شنبه", "جمعه"]
    all_days = set(menu1.keys()) | set(menu2.keys())

    for day in days_order:
        if day in all_days:
            merged_menu[day] = {
                'تاریخ': menu1.get(day, {}).get('تاریخ', '') or menu2.get(day, {}).get('تاریخ', ''),
                'صبحانه': menu1.get(day, {}).get('صبحانه', []),
                'ناهار': [],
                'شام': []
            }

            lunch_items_from_lunch_file = menu1.get(day, {}).get('ناهار', [])
            dinner_items_from_dinner_file = menu2.get(day, {}).get('شام', [])

            if day == "پنجشنبه":
                if dinner_items_from_dinner_file and not lunch_items_from_lunch_file:
                    merged_menu[day]['ناهار'] = dinner_items_from_dinner_file
                    merged_menu[day]['شام'] = []
                else:
                    merged_menu[day]['ناهار'] = lunch_items_from_lunch_file
                    merged_menu[day]['شام'] = dinner_items_from_dinner_file
            else:
                merged_menu[day]['ناهار'] = lunch_items_from_lunch_file
                merged_menu[day]['شام'] = dinner_items_from_dinner_file
    return merged_menu


def format_meals(meals):
    """قالب‌بندی وعده‌های غذایی"""
    if not meals:
        return "⚠️ اطلاعات منو موجود نیست"

    message = ""
    message += "🍳 صبحانه:\n"
    message += "".join(f"    • {f}\n" for f in meals['صبحانه']) or "    • موجود نیست\n"
    message += "🍛 ناهار:\n"
    message += "".join(f"    • {f}\n" for f in meals['ناهار']) or "    • موجود نیست\n"
    message += "🍲 شام:\n"
    message += "".join(f"    • {f}\n" for f in meals['شام']) or "    • موجود نیست\n"
    return message


async def handle_food_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not is_valid_food_request(update.message.text):
        await update.message.reply_text("❌ درخواست معتبر نیست.", reply_markup=MAIN_MARKUP)
        return

    try:
        is_allowed, reason = check_rate_limit(chat_id)

        if not is_allowed:
            if reason == "processing":
                await update.message.reply_text(
                    "⏳ درخواست قبلی شما در حال پردازش است. لطفاً صبر کنید...",
                    reply_markup=MAIN_MARKUP
                )
                return
            elif reason.startswith("cooldown"):
                remaining_time = reason.split(":")[1]
                await update.message.reply_text(
                    f"⏳ لطفاً {remaining_time} ثانیه صبر کنید و سپس دوباره تلاش کنید.",
                    reply_markup=MAIN_MARKUP
                )
                return

        # شروع پردازش
        add_user_to_processing(chat_id)
        update_user_request_time(chat_id)

        try:
            await process_food_query_internal(update, context)
        finally:
            remove_user_from_processing(chat_id)

    except Exception as e:
        logging.error(f"خطا در handle_food_query: {e}", exc_info=True)
        remove_user_from_processing(chat_id)
        await update.message.reply_text(
            "خطایی رخ داد. لطفاً دوباره تلاش کنید.",
            reply_markup=MAIN_MARKUP
        )
    finally:
        remove_user_from_processing(chat_id)


async def process_food_query_internal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        message_text = update.message.text.lower()
        is_today = "امروز" in message_text or "today" in message_text

        university_result = execute_query(
            "SELECT university FROM users WHERE chat_id = %s",
            (chat_id,),
            fetch="one"
        )

        if not university_result:
            await update.message.reply_text(
                "ابتدا باید دانشگاه خود را انتخاب کنید. از دستور /start استفاده کنید.",
                reply_markup=MAIN_MARKUP
            )
            return

        university = university_result[0]

        html_content_single = None
        html_content_lunch = None
        html_content_dinner = None

        try:
            if university == "خوارزمی":
                with open("./layouts/kharazmi_menu.html", "r", encoding="utf-8") as f:
                    html_content_single = f.read()
            elif university == "تهران":
                with open("./layouts/tehran_menu_lunch.html", "r", encoding="utf-8") as f:
                    html_content_lunch = f.read()
                with open("./layouts/tehran_menu_dinner.html", "r", encoding="utf-8") as f:
                    html_content_dinner = f.read()
            elif university == "خوارزمی تهران":  
                with open("./layouts/kharazmi_tehran_lunch.html", "r", encoding="utf-8") as f:
                    html_content_lunch = f.read()
                with open("./layouts/kharazmi_tehran_dinner.html", "r", encoding="utf-8") as f:
                    html_content_dinner = f.read()
            else:
                logging.warning(f"University '{university}' has no defined menu loading logic.")
                await update.message.reply_text(f"متاسفانه هنوز اطلاعات منوی دانشگاه {university} در دسترس نیست.",
                                                reply_markup=MAIN_MARKUP)
                return

        except FileNotFoundError:
            logging.error(f"فایل منوی دانشگاه '{university}' یافت نشد.")
            await update.message.reply_text(
                f"اطلاعات منوی دانشگاه {university} در دسترس نیست. لطفاً بعداً تلاش کنید.",
                reply_markup=MAIN_MARKUP
            )
            return
        except Exception as e:
            logging.error(f"خطا در بارگذاری فایل منو برای دانشگاه '{university}': {e}", exc_info=True)
            await update.message.reply_text("خطایی در بارگذاری اطلاعات منو رخ داد. لطفاً بعداً تلاش کنید.",
                                            reply_markup=MAIN_MARKUP)
            return

        schedule = {}

        if university == "خوارزمی":
            if html_content_single:
                schedule = parse_food_schedule(html_content_single, university)
            else:
                logging.warning(f"HTML content for {university} not loaded, cannot parse schedule.")
        elif university == "تهران" or university == "خوارزمی تهران":
            if html_content_lunch and html_content_dinner:
                temp1 = parse_food_schedule(html_content_lunch, university)
                temp2 = parse_food_schedule(html_content_dinner, university)
                schedule = merge_weekly_menus(temp1, temp2)
            else:
                logging.warning(f"Lunch or dinner HTML content for {university} not loaded, cannot parse schedule.")
        else:
            logging.warning(f"No parsing logic defined for loaded content of university: '{university}'")

        if is_today:
            today_name = get_today_name()
            if today_name == "جمعه" and not schedule.get(today_name):
                await update.message.reply_text("📵 امروز (جمعه) غذا سرو نمی‌شود.", reply_markup=MAIN_MARKUP)
                return

            if university == "تهران" and today_name not in schedule:
                await update.message.reply_text(f"📵 امروز ({today_name}) در دانشگاه تهران غذا سرو نمی‌شود.",
                                                reply_markup=MAIN_MARKUP)
                return

            meals_today = schedule.get(today_name, {})

            response = f"🍽 منوی امروز ({today_name}) دانشگاه {university}:\n\n"
            if not meals_today or not any(meals_today.get(m) for m in ["صبحانه", "ناهار", "شام"]):
                response += "⚠️ اطلاعات منو برای امروز موجود نیست یا غذا ارائه نمی‌شود."
            else:
                response += format_meals(meals_today)
        else:  # Weekly menu
            response = f"🗓 منوی هفته جاری دانشگاه {university}:\n\n"
            if not schedule:
                response += "⚠️ اطلاعات منوی این هفته هنوز در دسترس نیست."
            else:
                days_with_food_listed = False
                for day, meals in schedule.items():

                    if day == "جمعه" and not any(meals.get(m) for m in ["صبحانه", "ناهار", "شام"]):
                        response += f"📅 {day} ({meals.get('تاریخ', '')}):\n    معمولاً سرویس غذا وجود ندارد.\n\n"
                        continue

                    formatted_day_meals = format_meals(meals)
                    response += f"📅 {day} ({meals.get('تاریخ', '')}):\n{formatted_day_meals}\n\n"
                    if "موجود نیست" not in formatted_day_meals or "ارائه نمی‌شود" not in formatted_day_meals:
                        days_with_food_listed = True

                if not days_with_food_listed and not schedule:
                    response = f"🗓 منوی هفته جاری دانشگاه {university}:\n\n⚠️ اطلاعات منوی این هفته هنوز در دسترس نیست."

        await update.message.reply_text(response, reply_markup=MAIN_MARKUP)

    except Exception as e:
        logging.error(f"خطا در پردازش سوال غذا: {e}", exc_info=True)
        await update.message.reply_text(
            "متأسفانه در دریافت اطلاعات غذا مشکلی پیش آمد. لطفا دوباره تلاش کنید.",
            reply_markup=MAIN_MARKUP
        )

# ─── Rate Limiting Functions ────────────────────────────────────────
def check_rate_limit(chat_id):
    current_time = time.time()

    if chat_id in USER_PROCESSING:
        return False, "processing"

    last_request_time = USER_LAST_REQUEST.get(chat_id, 0)  # استفاده از get
    if current_time - last_request_time < REQUEST_COOLDOWN:
        remaining_time = REQUEST_COOLDOWN - (current_time - last_request_time)
        return False, f"cooldown:{remaining_time:.1f}"

    return True, "allowed"

def update_user_request_time(chat_id):
    USER_LAST_REQUEST[chat_id] = time.time()

def add_user_to_processing(chat_id):
    USER_PROCESSING.add(chat_id)
    logging.info(f"User {chat_id} added to processing. Current processing users: {len(USER_PROCESSING)}")

def remove_user_from_processing(chat_id):
    USER_PROCESSING.discard(chat_id)
    logging.info(f"User {chat_id} removed from processing. Current processing users: {len(USER_PROCESSING)}")


# ─── Filter Commands    ────────────────────────────────────────

VALID_PATTERNS = [
    r'^غذای\s*امروز\؟?$',
    r'^منوی\s*امروز\؟?$',
    r'^غذای\s*(این\s*هفته|هفته)\؟?$',
    r'^منوی\s*هفته\؟?$'
]

def is_valid_food_request(text: str) -> bool:
    """بررسی می‌کند که پیام کاربر جزء درخواست‌های معتبر است یا نه."""
    text = (text or "").strip().lower()
    return any(re.match(p, text) for p in VALID_PATTERNS)

# ─── Food Commands    ────────────────────────────────────────
async def today_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = "غذای امروز"
    await handle_food_query(update, context)


async def week_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = "غذای این هفته"
    await handle_food_query(update, context)


def setup_food_handlers(application):
    application.add_handler(CommandHandler("today", today_food))
    application.add_handler(CommandHandler("week", week_food))
    application.add_handler(MessageHandler(filters.Regex(r'^(غذای امروز|منوی امروز)$'), today_food))
    application.add_handler(MessageHandler(filters.Regex(r'^(غذای هفته|منوی هفته)$'), week_food))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))


# ───   Logging and Timing Funcs  ───────────────────────────────────────
def setup_logging():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logging.getLogger('apscheduler').setLevel(logging.WARNING)


async def send_reminder_to_individual_user(chat_id, message, university):
    """Sends a reminder to a single user and handles individual errors."""
    global bot_app
    if not bot_app:
        logging.error("bot_app not initialized. Cannot send message.")
        try:
            execute_query(
                "INSERT INTO failed_reminders (chat_id, university, message, retry_count) VALUES (%s, %s, %s, %s)",
                (chat_id, university, message, 0),
                commit=True
            )
            logging.info(
                f"bot_app not ready. Failed reminder for {chat_id} ({university}) saved to DB for later retry.")
        except Exception as db_err:
            logging.error(f"Error saving reminder (due to bot_app not ready) for {chat_id} to DB: {db_err}")
        return

    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=message)
        logging.info(f"Sent reminder to: {chat_id} ({university})")
    except Exception as e:
        logging.error(f"Failed to send reminder to {chat_id} for {university}: {e}")
        try:
            execute_query(
                "INSERT INTO failed_reminders (chat_id, university, message) VALUES (%s, %s, %s)",
                (chat_id, university, message),
                commit=True
            )
            logging.info(f"Failed reminder for {chat_id} ({university}) saved to DB.")
        except Exception as db_err:
            logging.error(f"Error saving failed reminder for {chat_id} to DB: {db_err}")


async def process_reminder_for_university(university_name):
    """Fetches users for a university and sends reminders in batches."""
    if university_name not in UNIVERSITY_CONFIG:
        logging.error(f"University configuration not found for: {university_name}")
        return

    config = UNIVERSITY_CONFIG[university_name]
    reminder_message = config['reminder_message']
    logging.info(f"Starting batched reminder process for {university_name}...")

    users_to_remind = []
    try:
        user_records = execute_query(
            "SELECT chat_id FROM users WHERE university = %s",
            (university_name,),
            fetch="all"
        )
        if user_records:
            users_to_remind = [record[0] for record in user_records]
    except Exception as e:
        logging.error(f"Error fetching users for {university_name}: {e}")
        return

    if not users_to_remind:
        logging.info(f"No users found for {university_name} to send reminders.")
        return

    logging.info(f"Found {len(users_to_remind)} users for {university_name}. Processing in batches of {BATCH_SIZE}.")

    for i in range(0, len(users_to_remind), BATCH_SIZE):
        batch_chat_ids = users_to_remind[i:i + BATCH_SIZE]
        logging.info(f"Processing batch {i // BATCH_SIZE + 1} for {university_name} with {len(batch_chat_ids)} users.")

        for chat_id in batch_chat_ids:
            await send_reminder_to_individual_user(chat_id, reminder_message, university_name)
            await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

        if i + BATCH_SIZE < len(users_to_remind):
            logging.info(f"Waiting {DELAY_BETWEEN_BATCHES}s before next batch for {university_name}.")
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    logging.info(f"Finished batched reminder process for {university_name}.")


def schedule_university_reminders():
    """Schedules one job per university to send batched reminders."""
    global scheduler
    if not scheduler:
        logging.error("Scheduler not initialized. Cannot schedule university reminders.")
        return

    for university_name, config in UNIVERSITY_CONFIG.items():
        job_id = f"batched_reminder_{university_name}"
        try:
            scheduler.add_job(
                process_reminder_for_university,
                'cron',
                day_of_week=config['day_of_week'],
                hour=config['hour'],
                minute=config['minute'],
                id=job_id,
                kwargs={'university_name': university_name},
                replace_existing=True
            )
            logging.info(
                f"Scheduled/Updated batched reminders for {university_name} (Job ID: {job_id}) at {config['day_of_week']} {config['hour']}:{config['minute']:02d}")
        except Exception as e:
            logging.error(f"Failed to schedule job for {university_name}: {e}")


async def job_listener(event):
    """گوش دادن به رویدادهای job scheduler"""
    if event.exception:
        logging.error(f"Job ID : {event.job_id} error occured {event.exception}")
    else:
        logging.info(f"Job ID: {event.job_id} done successfully")


async def retry_failed_reminders():
    """Retries sending failed reminders in batches."""
    global bot_app
    if not bot_app:
        logging.warning("bot_app not ready, skipping retry_failed_reminders for now.")
        return

    try:
        failed_reminders_data = execute_query(
            "SELECT id, chat_id, university, message, retry_count FROM failed_reminders WHERE retry_count < %s ORDER BY created_at",
            (MAX_RETRIES,),
            fetch="all"
        )

        if not failed_reminders_data:
            logging.debug("No failed reminders to retry.")
            return

        logging.info(
            f"Retrying {len(failed_reminders_data)} failed reminders in batches of {RETRY_BATCH_SIZE}...")  # RETRY_BATCH_SIZE defined globally

        for i in range(0, len(failed_reminders_data), RETRY_BATCH_SIZE):
            batch = failed_reminders_data[i:i + RETRY_BATCH_SIZE]
            logging.info(f"Processing retry batch {i // RETRY_BATCH_SIZE + 1} with {len(batch)} reminders.")

            for reminder_item in batch:
                reminder_id, chat_id, university, message, retry_count = reminder_item
                try:
                    await bot_app.bot.send_message(chat_id=chat_id, text=message)
                    logging.info(f"Successfully retried reminder ID {reminder_id} for user {chat_id} ({university}).")
                    execute_query(
                        "DELETE FROM failed_reminders WHERE id = %s",
                        (reminder_id,),
                        commit=True
                    )
                except Exception as e:
                    new_retry_count = retry_count + 1
                    logging.warning(
                        f"Retry {new_retry_count}/{MAX_RETRIES} failed for reminder ID {reminder_id} to user {chat_id}: {e}")
                    execute_query(
                        "UPDATE failed_reminders SET retry_count = %s WHERE id = %s",
                        (new_retry_count, reminder_id),
                        commit=True
                    )
                    if new_retry_count >= MAX_RETRIES:
                        logging.error(f"Max retries reached for reminder ID {reminder_id}. Will not attempt again.")

                await asyncio.sleep(RETRY_DELAY_BETWEEN_MESSAGES)

            if i + RETRY_BATCH_SIZE < len(failed_reminders_data):
                logging.info(f"Waiting {RETRY_DELAY_BETWEEN_BATCHES}s before next retry batch.")
                await asyncio.sleep(RETRY_DELAY_BETWEEN_BATCHES)

        logging.info("Finished processing failed reminders.")

    except Exception as e:
        logging.error(f"Error in retry_failed_reminders process: {e}", exc_info=True)


async def choose_university(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uni = update.message.text
    chat_id = update.effective_chat.id

    if uni not in UNIVERSITY_CONFIG:
        keyboard_options_on_error = [
            ["خوارزمی", "تهران"],
            ["خوارزمی تهران"]
        ]
        await update.message.reply_text(
            "🤓دانشگاهی که انتخاب کردی معتبر نیست. لطفاً یکی از گزینه‌های موجود رو انتخاب کن دوست من :",  # Your message
            reply_markup=ReplyKeyboardMarkup(
                keyboard_options_on_error,
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
        return CHOOSING
    try:
        execute_query(
            "INSERT INTO users (chat_id, university) VALUES (%s, %s) ON DUPLICATE KEY UPDATE university = %s",
            (chat_id, uni, uni),
            commit=True
        )
        logging.info(f"User {chat_id} selected/updated university to {uni}.")

        await update.message.reply_text(
            f"یادآوری‌ها مطابق دانشگاه {uni} که ثبت کردی برات فرستاده می‌شن. هواتو دارم 😎🎓",
            reply_markup=MAIN_MARKUP
        )
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"خطا در ذخیره انتخاب دانشگاه برای {chat_id}: {e}", exc_info=True)
        await update.message.reply_text("مشکلی در ذخیره دانشگاه شما پیش آمد. لطفاً دوباره تلاش کنید.",
                                        reply_markup=MAIN_MARKUP)
        return CHOOSING



async def on_startup(application):
    global bot_app
    bot_app = application

    if not scheduler.running:
        scheduler.add_listener(job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
        try:
            scheduler.start(paused=False)
            logging.info("Scheduler started successfully.")
        except Exception as e:
            logging.error(f"Failed to start scheduler: {e}", exc_info=True)
            return

    schedule_university_reminders()

    try:
        jobs = scheduler.get_jobs()
        logging.info(f"Total {len(jobs)} jobs currently scheduled:")
        for job in jobs:

            next_run_display = 'N/A'
            if job.next_run_time:
                try:
                    next_run_display = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S UTC')
                except Exception:
                    next_run_display = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')

            logging.info(f"  Job ID: {job.id}, Trigger: {job.trigger}, Next run: {next_run_display}")
    except Exception as e:
        logging.error(f"Error retrieving scheduled jobs: {e}")

    try:
        if not scheduler.get_job("retry_failed_reminders_job"):
            scheduler.add_job(
                retry_failed_reminders,
                'interval',
                minutes=15,
                id="retry_failed_reminders_job",
                replace_existing=True
            )
            logging.info("Scheduled periodic job for retrying failed reminders.")
        else:
            logging.info("Periodic job for retrying failed reminders already scheduled.")
    except Exception as e:
        logging.error(f"Failed to schedule periodic retry job: {e}")


async def shutdown(application):
    """تنظیمات خاموشی ربات"""
    logging.info("MACHINE IS OFF")

    if scheduler.running:
        scheduler.shutdown()
        logging.info("SCHEDULING STOPPED")

    global db_pool
    if db_pool:
        logging.info("CLOSED POOL OF DB")
        db_pool = None


# ─── Telegram Handlers  ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"دریافت دستور start از {update.effective_chat.id}")
    try:
        keyboard_options = [
            ["خوارزمی", "تهران"],
            ["خوارزمی تهران"]
        ]
        await update.message.reply_text(
            "👋 سلام! لطفاً دانشگاه خود را انتخاب کنید:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard_options,
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
        return CHOOSING
    except Exception as e:
        logging.error(f"error on start: {e}")
        raise


# ───  MACHINE RUNNING CONFIGS AND FUNCS HAHAHAHA:)))───────────────────────────────────
if __name__ == "__main__":
    setup_logging()

    try:
        if not init_db_pool():
            logging.critical("failed to connect to database")
            sys.exit(1)

        if not create_required_tables():
            logging.critical("failed to create requried tables")
            sys.exit(1)

        persistence = PicklePersistence(filepath="conversation_states")

        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r'^(تغییر دانشگاه|انتخاب دانشگاه)$'), start)
            ],
            states={
                CHOOSING: [
                    MessageHandler(filters.Regex(r'^(خوارزمی|تهران|خوارزمی تهران)$'), choose_university)
                    # Updated Regex
                ],
            },
            fallbacks=[
                CommandHandler("cancel", lambda u, c: ConversationHandler.END)
            ],
            name="university_choice",
            persistent=True
        )
        application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
        application.add_handler(conv_handler)

        application.add_handler(MessageHandler(filters.Regex(".*غذای امروز.*"), handle_food_query))
        application.add_handler(MessageHandler(filters.Regex(".*غذای این هفته.*"), handle_food_query))

        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))

        bot_app = application

        application.post_init = on_startup
        application.post_shutdown = shutdown

        logging.info("MACHINE RUNNING")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except mysql.connector.Error as db_error:
        logging.critical(f"DATABASE ERROR ON START: {db_error}")
        asyncio.run(shutdown())
        sys.exit(1)
    except Exception as e:
        logging.critical(f"FAILED TO START MACHINE: {e}")
        asyncio.run(shutdown())
        sys.exit(1)
