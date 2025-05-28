import re
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from telegram import InlineKeyboardButton, \
    InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup
from datetime import datetime, timedelta  # timedelta استفاده نشده، می‌توان حذف کرد
from telegram.ext import Application

import os
import time
import signal
import sys
import logging
import asyncio
from telegram.ext import PicklePersistence

import mysql.connector
from mysql.connector import pooling

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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MYSQL_CONFIG = {
    "host": os.getenv("MYSQLHOST"),
    "port": int(os.getenv("MYSQLPORT", "3306")),
    "user": os.getenv("MYSQLUSER"),
    "password": os.getenv("MYSQLPASSWORD"),
    "database": os.getenv("MYSQLDATABASE"),
    "pool_size": 5,
    "pool_name": "mypool",
    "connect_timeout": 30
}

SQLALCHEMY_URL = (
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{MYSQL_CONFIG['password']}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
)

MAX_RETRIES = 3
DB_RECONNECT_INTERVAL = 60  # seconds
# ─── وضعیت گفتگو ─────────────────────────────────────────────────
CHOOSING = 0

# ─── دکمه‌های اصلی ───────────────────────────────────────────────
MAIN_MARKUP = ReplyKeyboardMarkup([
    ["تغییر دانشگاه", "غذای امروز؟"],
    ["غذای این هفته؟"],
], resize_keyboard=True)

# ─── تنظیمات دانشگاه‌ها ───────────────────────────────────────────
UNIVERSITY_CONFIG = {
    "خوارزمی": {
        "day_of_week": "wed",  # چهارشنبه
        "hour": 12,
        "minute": 0,
        "reminder_message": "⏰ یادآوری: ۲۴ ساعت تا پایان مهلت رزرو غذای دانشگاه خوارزمی باقی مانده!"
    },
    "تهران": {
        "day_of_week": "tue",  # سه‌شنبه
        "hour": 12,
        "minute": 0,
        "reminder_message": "⏰ یادآوری: ۲۴ ساعت تا پایان مهلت رزرو غذای دانشگاه تهران باقی مانده!"
    },
}

# ─── متغیرهای سراسری ───────────────────────────────────────────
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


# ─── توابع دیتابیس (همزمان و غیرهمزمان) ─────────────────────────
def init_db_pool():
    global db_pool
    try:
        logging.info("Attempting to initialize database connection pool...")
        db_pool = mysql.connector.pooling.MySQLConnectionPool(**MYSQL_CONFIG)
        conn = db_pool.get_connection()
        if conn.is_connected():
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
            logging.info("Database connection pool initialized and tested successfully.")
            return True
        else:
            logging.error("Failed to establish a test connection from the pool.")
            return False
    except mysql.connector.Error as err:
        logging.error(f"Error initializing database pool: {err}")
        return False


def get_db_connection():
    global db_pool
    if not db_pool:
        logging.warning("Database pool not initialized. Attempting to initialize.")
        if not init_db_pool():
            logging.error("Failed to re-initialize database pool in get_db_connection.")
            return None
    try:
        conn = db_pool.get_connection()
        if conn.is_connected():
            return conn
        else:  # اتصال وجود دارد اما برقرار نیست
            logging.warning("Retrieved a non-connected connection from pool. Re-initializing pool.")
            if init_db_pool():  # تلاش برای مقداردهی مجدد پول
                return db_pool.get_connection() if db_pool else None
            return None
    except mysql.connector.Error as err:
        logging.error(f"Error getting connection from pool: {err}")
        if err.errno == mysql.connector.errorcode.CR_CONN_HOST_ERROR or \
                err.errno == mysql.connector.errorcode.CR_SERVER_GONE_ERROR:
            logging.info("Attempting to re-initialize DB pool due to connection error.")
            if init_db_pool():
                return db_pool.get_connection() if db_pool else None
        return None


def execute_query(query, params=None, commit=False, fetch=None):
    """اجرای کوئری با خطایابی و تلاش مجدد (همزمان)"""
    retries = 0
    while retries < MAX_RETRIES:
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                logging.error("Database connection error, cannot get connection.")
                time.sleep(retries + 1)  # افزایش زمان انتظار
                retries += 1
                if retries >= MAX_RETRIES and db_pool:  # تلاش برای ریست کردن پول قبل از آخرین تلاش
                    logging.warning("Resetting db_pool before last retry")
                    init_db_pool()
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
            return result  # موفقیت آمیز بود، نتیجه را برگردان و از حلقه خارج شو
        except mysql.connector.Error as err:
            retries += 1
            logging.error(f"Database error (attempt {retries}/{MAX_RETRIES}): {err}")
            if err.errno == mysql.connector.errorcode.ER_LOCK_DEADLOCK or \
                    err.errno == mysql.connector.errorcode.ER_LOCK_WAIT_TIMEOUT:
                time.sleep(retries * 2)  # انتظار بیشتر برای deadlock
            else:
                time.sleep(retries)

            if retries >= MAX_RETRIES:
                logging.error("Maximum database retries reached. Raising exception.")
                raise  # پس از حداکثر تلاش، خطا را منتشر کن
        finally:
            if conn and conn.is_connected():
                conn.close()
    return None  # اگر به هر دلیلی (مثلا خطای اتصال اولیه) به اینجا برسد


# NEW: تابع wrapper برای اجرای execute_query به صورت غیرهمزمان
async def execute_query_async(query, params=None, commit=False, fetch=None):
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,  # Uses default ThreadPoolExecutor
            execute_query,
            query,
            params,
            commit,
            fetch
        )
        return result
    except mysql.connector.Error as e:  # اگر خود execute_query خطا را raise کند
        logging.error(f"Async DB call failed after retries: {e}")
        # می‌توانید در اینجا تصمیم بگیرید که خطا را مجددا raise کنید یا None برگردانید
        # بستگی به نیاز برنامه دارد
        raise


def create_required_tables():
    try:
        # از آنجایی که این تابع در زمان راه‌اندازی و به صورت همزمان اجرا می‌شود،
        # نیازی به نسخه async نیست مگر اینکه init_db_pool هم async شود.
        conn = get_db_connection()  # این تابع باید بتواند پول را در صورت نیاز مجددا مقداردهی کند
        if not conn:
            logging.error("Fatal error: Cannot connect to DB for creating tables.")
            return False

        cursor = conn.cursor()
        users_table = """
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            university VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        failed_reminders_table = """
        CREATE TABLE IF NOT EXISTS failed_reminders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            university VARCHAR(50) NOT NULL,
            message TEXT NOT NULL,
            retry_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scheduled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, -- این فیلد در DDL اولیه شما بود
            INDEX (chat_id),
            INDEX (retry_count)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        cursor.execute(users_table)
        cursor.execute(failed_reminders_table)
        conn.commit()
        cursor.close()
        conn.close()
        logging.info("Required tables checked/created successfully.")
        return True
    except mysql.connector.Error as err:
        logging.error(f"Error creating tables: {err}")
        return False


# توابع مربوط به غذا (بدون تغییر زیاد، جز فراخوانی execute_query_async)
def clean_food_name(food):
    return re.sub(r"(،|\(|\[)?\s*(رایگان|\d{2,3}(,\d{3})?)\s*(تومان|ریال)?\)?$", "", food).strip()


def get_today_name():
    """دریافت نام روز هفته امروز به فارسی"""
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

    # ترتیب روزهای هفته به فارسی
    days_order = ["شنبه", "یکشنبه", "دوشنبه", "سه شنبه", "چهارشنبه", "پنج شنبه", "جمعه"]

    # ترکیب همه روزهای موجود در هر دو منو
    all_days = set(menu1.keys()) | set(menu2.keys())

    # ایجاد دیکشنری مرتب شده بر اساس ترتیب روزهای هفته
    for day in days_order:
        if day in all_days:
            merged_menu[day] = {
                'تاریخ': menu1.get(day, {}).get('تاریخ', '') or menu2.get(day, {}).get('تاریخ', ''),
                'صبحانه': menu1.get(day, {}).get('صبحانه', []),
                'ناهار': menu1.get(day, {}).get('ناهار', []),
                'شام': menu2.get(day, {}).get('شام', [])
            }
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
    """پردازش سوالات مربوط به منوی غذا"""
    try:
        chat_id = update.effective_chat.id
        message_text = update.message.text.lower()
        is_today = "امروز" in message_text or "today" in message_text

        # دریافت دانشگاه کاربر
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

        # دریافت منوی غذا
        try:
            if university == "خوارزمی":
                with open("kharazmi_menu.html", "r", encoding="utf-8") as f:
                    html = f.read()
            else:
                with open("tehran_menu_lunch.html", "r", encoding="utf-8") as f:
                    html_lunch = f.read()
                with open("tehran_menu_dinner.html", "r", encoding="utf-8") as f:
                    html_dinner = f.read()
        except FileNotFoundError:
            logging.error(f"فایل منوی {university} یافت نشد.")
            await update.message.reply_text(
                f"اطلاعات منوی دانشگاه {university} در دسترس نیست. لطفاً بعداً تلاش کنید.",
                reply_markup=MAIN_MARKUP
            )
            return

        # پردازش HTML و استخراج برنامه غذایی
        if (university == "خوارزمی"):
            schedule = parse_food_schedule(html, university)
        else:
            temp1 = parse_food_schedule(html_lunch, university)
            temp2 = parse_food_schedule(html_dinner, university)
            schedule = merge_weekly_menus(temp1, temp2)

        if is_today:
            today_name = get_today_name()
            if today_name == "جمعه":
                await update.message.reply_text("📵 امروز (جمعه) غذا سرو نمی‌شود.", reply_markup=MAIN_MARKUP)
                return

            # بررسی برای دانشگاه تهران
            if university == "تهران" and today_name not in schedule:
                await update.message.reply_text(f"📵 امروز ({today_name}) در دانشگاه تهران غذا سرو نمی‌شود.",
                                                reply_markup=MAIN_MARKUP)
                return

            meals = schedule.get(today_name, {})
            response = f"🍽 منوی امروز ({today_name}) دانشگاه {university}:\n\n"
            response += format_meals(meals)
        else:

            response = f"🗓 منوی هفته جاری دانشگاه {university}:\n\n"
            for day, meals in schedule.items():
                response += f"📅 {day}:\n{format_meals(meals)}\n\n"

        await update.message.reply_text(response, reply_markup=MAIN_MARKUP)

    except Exception as e:
        logging.error(f"خطا در پردازش سوال غذا: {e}")
        await update.message.reply_text(
            "متأسفانه در دریافت اطلاعات غذا مشکلی پیش آمد. لطفا دوباره تلاش کنید.",
            reply_markup=MAIN_MARKUP
        )


async def today_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = "غذای امروز"  # برای سازگاری با handle_food_query
    await handle_food_query(update, context)


async def week_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = "غذای این هفته"  # برای سازگاری با handle_food_query
    await handle_food_query(update, context)


def setup_food_handlers(application):
    """ثبت تمام هندلرهای مربوط به غذا"""
    application.add_handler(CommandHandler("today", today_food))
    application.add_handler(CommandHandler("week", week_food))
    application.add_handler(MessageHandler(filters.Regex(r'^(غذای امروز|منوی امروز)$'), today_food))
    application.add_handler(MessageHandler(filters.Regex(r'^(غذای هفته|منوی هفته)$'), week_food))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))


# ─── توابع لاگینگ و زمان‌بندی (تغییرات عمده) ───────────────────
def setup_logging():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stdout)]  # اطمینان از لاگ در کنسول
    )
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    logging.getLogger('mysql.connector').setLevel(logging.WARNING)  # کاهش لاگ‌های mysql


async def job_listener(event):
    if event.exception:
        logging.error(f"Job ID {event.job_id} failed: {event.exception}")
    else:
        logging.info(f"Job ID {event.job_id} executed successfully.")


# NEW: Helper function to get user chat_ids for a university asynchronously
async def get_user_chat_ids_for_university_async(university_name: str) -> list[int]:
    query = "SELECT chat_id FROM users WHERE university = %s"
    params = (university_name,)
    try:
        result = await execute_query_async(query, params, fetch="all")
        return [row[0] for row in result] if result else []
    except Exception as e:  # اگر execute_query_async خطا را raise کند
        logging.error(f"Failed to get users for {university_name}: {e}")
        return []


# NEW: Helper function to save a failed reminder asynchronously
async def save_failed_reminder_async(chat_id: int, university: str, message: str):
    query = "INSERT INTO failed_reminders (chat_id, university, message) VALUES (%s, %s, %s)"
    params = (chat_id, university, message)
    try:
        await execute_query_async(query, params, commit=True)
        logging.info(f"Saved failed reminder for chat_id {chat_id} ({university}) to DB.")
    except Exception as e:  # اگر execute_query_async خطا را raise کند
        logging.error(f"Error saving failed reminder for chat_id {chat_id} to DB: {e}")


async def send_reminders_to_university_users(university_name: str):  # <<-- پارامتر context حذف شد
    global bot_app  # دسترسی به متغیر گلوبال
    if not bot_app or not bot_app.bot:
        logging.error(f"bot_app not properly initialized. Cannot send reminders for {university_name}.")
        return

    bot = bot_app.bot
    # logging.info(f"Job started: Sending reminders for university: {university_name} using bot: {await bot.get_my_name()}") # get_my_name async است
    logging.info(f"Job started: Sending reminders for university: {university_name}")

    config = UNIVERSITY_CONFIG.get(university_name)
    if not config:
        logging.error(f"University config not found for {university_name}. Aborting job.")
        return

    message_text = config['reminder_message']
    user_chat_ids = await get_user_chat_ids_for_university_async(university_name)

    if not user_chat_ids:
        logging.info(f"No users found for university: {university_name}. Job finished.")
        return

    logging.info(f"Found {len(user_chat_ids)} users for {university_name}. Starting to send messages.")
    successful_sends = 0
    failed_sends = 0

    for chat_id in user_chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=message_text)
            successful_sends += 1
            logging.debug(f"Reminder sent to {chat_id} for {university_name}")
            await asyncio.sleep(0.1)
        except Exception as e:
            failed_sends += 1
            logging.error(f"Failed to send reminder to {chat_id} ({university_name}): {e}")
            await save_failed_reminder_async(chat_id, university_name, message_text)

    logging.info(
        f"Job finished for {university_name}. Successful sends: {successful_sends}, Failed attempts: {failed_sends}")


def schedule_university_wide_reminders(university_name: str,
                                       application: Application):  # application برای لاگ و اطلاعات اولیه مفید است
    config = UNIVERSITY_CONFIG.get(university_name)
    if not config:
        logging.error(f"Cannot schedule reminders: Config not found for {university_name}")
        return

    job_id = f"university_reminder_{university_name}"

    scheduler.add_job(
        send_reminders_to_university_users,
        'cron',
        day_of_week=config['day_of_week'],
        hour=config['hour'],
        minute=config['minute'],
        id=job_id,
        kwargs={'university_name': university_name},
        replace_existing=True,
        misfire_grace_time=300
    )
    logging.info(
        f"Scheduled/Updated university-wide reminder: {job_id} for {university_name} at {config['day_of_week']} {config['hour']}:{config['minute']}")


async def retry_failed_reminders():
    global bot_app  # دسترسی به متغیر گلوبال
    if not bot_app or not bot_app.bot:
        logging.error("bot_app not properly initialized. Cannot retry failed reminders.")
        return

    bot = bot_app.bot
    logging.info("Starting retry_failed_reminders job.")
    try:
        failed_reminders = await execute_query_async(
            "SELECT id, chat_id, university, message, retry_count FROM failed_reminders WHERE retry_count < %s",
            (MAX_RETRIES,),
            fetch="all"
        )
        if not failed_reminders:
            logging.info("No failed reminders to retry.")
            return

        logging.info(f"Retrying {len(failed_reminders)} failed reminders.")
        retried_successfully = 0
        still_failed = 0

        for reminder_data in failed_reminders:
            reminder_id, chat_id, university, message, retry_count = reminder_data
            try:
                await bot.send_message(chat_id=chat_id, text=message)
                logging.info(f"Successfully retried reminder_id {reminder_id} for user {chat_id}")
                await execute_query_async(
                    "DELETE FROM failed_reminders WHERE id = %s", (reminder_id,), commit=True
                )
                retried_successfully += 1
            except Exception as e:
                still_failed += 1
                logging.warning(f"Failed again to retry reminder_id {reminder_id} for user {chat_id}: {e}")
                new_retry_count = retry_count + 1
                await execute_query_async(
                    "UPDATE failed_reminders SET retry_count = %s WHERE id = %s",
                    (new_retry_count, reminder_id), commit=True
                )
                if new_retry_count >= MAX_RETRIES:
                    logging.error(f"Max retries reached for reminder_id {reminder_id} (user {chat_id}). Giving up.")
            await asyncio.sleep(0.2)  # کمی تاخیر بین تلاش‌های مجدد
        logging.info(
            f"Finished retry_failed_reminders job. Retried successfully: {retried_successfully}, Still failed/marked for no more retries: {still_failed}")

    except mysql.connector.Error as db_err:
        logging.error(f"Database error in retry_failed_reminders: {db_err}")
    except Exception as e:
        logging.error(f"Unexpected error in retry_failed_reminders: {e}", exc_info=True)


async def on_startup(application: Application):

    if not scheduler.running: # فقط اگر scheduler در حال اجرا نیست، آن را start کن
        try:
            scheduler.start()
            logging.info("APScheduler started successfully.")
        except Exception as e:
            logging.critical(f"Failed to start APScheduler: {e}", exc_info=True)
            return
    if not scheduler._listeners:  # یک راه ساده برای بررسی اینکه آیا listener ها قبلا اضافه شده‌اند
        scheduler.add_listener(job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
        logging.info("APScheduler event listener added.")

    # زمان‌بندی یادآوری‌های کلی برای هر دانشگاه
    for uni_name in UNIVERSITY_CONFIG.keys():
        schedule_university_wide_reminders(uni_name, application)

    # یک جاب جداگانه برای تلاش مجدد، مثلا هر یک ساعت یا کمتر
    # اطمینان از اینکه job_id تکراری نباشد یا replace_existing=True استفاده شود
    retry_job_id = "retry_failed_reminders_job"
    # فقط اگر جاب وجود ندارد، آن را اضافه کن. replace_existing=True هم همین کار را می‌کند.
    scheduler.add_job(
        retry_failed_reminders,
        'interval',
        minutes=30,
        id=retry_job_id,
        replace_existing=True
    )
    logging.info(f"Scheduled job for retrying failed reminders: {retry_job_id}")

    # اجرای اولیه برای پاکسازی موارد ناموفق در زمان راه‌اندازی
    # این دیگر لازم نیست اگر جاب بالا به درستی کار کند و در اولین اجرا موارد را بررسی کند
    # await retry_failed_reminders(application)

    jobs = scheduler.get_jobs()
    logging.info(f"Total jobs in scheduler after startup: {len(jobs)}")
    for job in jobs:
        logging.info(f"  Job ID: {job.id}, Next run: {job.next_run_time}, Trigger: {str(job.trigger)}")


async def shutdown_operations(application: Application):  # application به عنوان آرگومان پاس داده می‌شود
    logging.info("Bot is shutting down...")
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logging.info("APScheduler shut down.")

    logging.info("Shutdown complete.")


# ─── هندلرهای تلگرام ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"دریافت دستور start از {update.effective_chat.id}")
    try:
        await update.message.reply_text(
            "👋 سلام! لطفاً دانشگاه خود را انتخاب کنید:",
            reply_markup=ReplyKeyboardMarkup(
                [["خوارزمی", "تهران"]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
        return CHOOSING
    except Exception as e:
        logging.error(f"error on start: {e}")
        raise


async def choose_university(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uni = update.message.text
    chat_id = update.effective_chat.id

    if uni not in UNIVERSITY_CONFIG:
        await update.message.reply_text("دانشگاه انتخابی معتبر نیست، لطفا دوباره انتخاب کنید:")
        return CHOOSING

    try:
        # MODIFIED: استفاده از execute_query_async
        await execute_query_async(
            "INSERT INTO users (chat_id, university) VALUES (%s, %s) ON DUPLICATE KEY UPDATE university = %s",
            (chat_id, uni, uni),
            commit=True
        )
        logging.info(f"User {chat_id} selected/updated university to {uni}")



        await update.message.reply_text(
            f"دانشگاه {uni} انتخاب شد. من به شما یادآوری رزرو غذای دانشگاه را در زمان مناسب ارسال خواهم کرد.",
            reply_markup=MAIN_MARKUP
        )
        return ConversationHandler.END
    except mysql.connector.Error as db_err:
        logging.error(f"DB error choosing university for {chat_id}: {db_err}")
        await update.message.reply_text("مشکل در ذخیره انتخاب شما. لطفا بعدا تلاش کنید.")
        return CHOOSING  # یا بازگشت به حالت اولیه
    except Exception as e:
        logging.error(f"Error in choose_university for {chat_id}: {e}", exc_info=True)
        await update.message.reply_text("متأسفانه مشکلی پیش آمد. لطفا دوباره تلاش کنید.")
        return CHOOSING


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """پایان دادن به گفتگو."""
    await update.message.reply_text("عملیات لغو شد.", reply_markup=MAIN_MARKUP)
    return ConversationHandler.END


# ─── تنظیمات و راه‌اندازی ربات ───────────────────────────────────
if __name__ == "__main__":
    setup_logging()

    if not BOT_TOKEN:
        logging.critical("BOT_TOKEN not found in environment variables. Exiting.")
        sys.exit(1)

    if not init_db_pool():
        logging.critical("Failed to initialize database pool on startup. Exiting.")
        sys.exit(1)

    if not create_required_tables():
        logging.critical("Failed to create required database tables. Exiting.")
        sys.exit(1)

    persistence = PicklePersistence(filepath="conversation_states")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(on_startup)
        .post_shutdown(shutdown_operations)
        .build()
    )

    bot_app = application

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(r'^(تغییر دانشگاه|انتخاب دانشگاه)$'), start)
        ],
        states={
            CHOOSING: [MessageHandler(filters.Regex(r'^(خوارزمی|تهران)$'), choose_university)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        name="university_choice_conversation",
        persistent=True
    )
    application.add_handler(conv_handler)

    application.add_handler(MessageHandler(filters.Regex(".*غذای امروز.*"), handle_food_query))
    application.add_handler(MessageHandler(filters.Regex(".*غذای این هفته.*"), handle_food_query))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))



    logging.info("Bot is starting polling...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logging.critical(f"Bot failed to run: {e}", exc_info=True)
        sys.exit(1)
