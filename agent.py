import imghdr
import os, json, time, uuid, logging, threading
from queue import Queue, Empty
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# --- Load Environment Variables ---
# It's best practice to load these from a .env file for security and flexibility.
# Create a file named `.env` in the same directory as agent.py with your actual keys:
# GEMINI_API_KEY="YOUR_GEMINI_KEY_HERE"
# TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN_HERE"
# ALLOWED_USERS="YOUR_TELEGRAM_ID_HERE,ANOTHER_ID_HERE" (comma-separated if multiple)
from dotenv import load_dotenv
load_dotenv()

# ---- Direct API keys (NOT real, these are placeholders for .env) ----
# Accessing env variables using os.getenv
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDnsQ_EaKER5TghXoh8mpkoy_tXZoaYZ58")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7851395212:AAG9O6MMmVrVBQtpCSJF7N5xgvnNnvNNwbQ")
# Parse ALLOWED_USERS from string to list of integers
ALLOWED_USERS_STR = os.getenv("ALLOWED_USERS", "21706160")
ALLOWED_USERS = [int(x.strip()) for x in ALLOWED_USERS_STR.split(',') if x.strip().isdigit()]

# Your API_HASH is often used with Telethon or other Telegram API libraries,
# but `python-telegram-bot` usually doesn't need it directly for basic bot functions.
# Keeping it here as you had it, but be aware its usage might depend on other parts of your project.
API_HASH = os.getenv("API_HASH", "548b91f0e7cd2e44bbee05190620d9f4") # Placeholder

# ---- Config ----
MODEL = "gemini-2.5-flash"
OUTPUT_DIR = os.path.join(os.getcwd(), "tg_outputs")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "history.json")
MAX_WORKERS = 2
HEADLESS = True # Changed to True for background operation by default, can be changed.
TASK_TIMEOUT = 180

# ---- Logging & folders ----
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# Configure logging
logger = logging.getLogger("InteractiveTelegramAgent")
logger.setLevel(logging.INFO)

# File handler for logs
fh = logging.FileHandler(os.path.join(OUTPUT_DIR, "agent_live.log"), encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(fh)

# Console handler for logs
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(ch)

# ---- Imports (placed after initial config and logging setup) ----
# This order is important if these imports rely on environment variables or specific logging.
from browser_use import Agent, Browser
from browser_use.llm import ChatGoogle
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ---- Queues & state ----
task_queue = Queue()
results = {}
workers_status = {}
shutdown_event = threading.Event()

# ---- Helpers ----
def append_history(entry):
    """Appends an entry to the history file."""
    try:
        if os.path.exists(HISTORY_FILE) and os.path.getsize(HISTORY_FILE) > 0:
            with open(HISTORY_FILE, "r+", encoding="utf-8") as f:
                f.seek(0)
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = [] # Handle empty or malformed JSON
                data.append(entry)
                f.seek(0)
                f.truncate()
                json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump([entry], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to append to history file: {e}")

def is_allowed(user_id):
    """Checks if a user ID is in the allowed list."""
    # If ALLOWED_USERS list is empty, all users are allowed.
    return (not ALLOWED_USERS) or (user_id in ALLOWED_USERS)

def save_screenshot(agent_or_browser, task_id):
    """Saves a screenshot from the agent or browser."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"screenshot_{task_id}_{ts}.png"
    path = os.path.join(SCREENSHOT_DIR, fname)
    try:
        if hasattr(agent_or_browser, "screenshot"):
            agent_or_browser.screenshot(path)
            logger.info(f"Screenshot saved to {path}")
            return path
        else:
            logger.warning("Object does not have a 'screenshot' method.")
    except Exception:
        logger.exception("Screenshot failed")
    return None

def structured_prompt(task_text):
    """Formats the task text into a structured prompt for the agent."""
    return f"نفّذ المهمة التالية بدقة، وإذا واجهت أي خطوة تحتاج تدخل المستخدم أخبره مباشرة:\n{task_text}"

# ---- Agent Worker ----
def create_worker_agent():
    """Initializes and returns an Agent and Browser instance for a worker."""
    llm = ChatGoogle(model=MODEL, api_key=GEMINI_KEY)
    browser = Browser(headless=HEADLESS)
    # The 'task' argument for Agent is typically the initial task.
    # It will be overridden by `agent.task = structured_prompt(task_text)` later.
    agent = Agent(task="initialize", llm=llm, browser=browser)
    return agent, browser

def worker_loop(worker_id):
    """The main loop for each worker thread."""
    agent, browser = create_worker_agent()
    workers_status[worker_id] = {"status": "idle", "current_task": None}
    logger.info(f"Worker {worker_id} started.")

    while not shutdown_event.is_set():
        try:
            item = task_queue.get(timeout=1) # Wait for a task, with a timeout to check shutdown_event
        except Empty:
            continue # No task, check shutdown_event again

        tid = item["id"]
        task_text = item["task"]
        chat_id = item.get("chat_id")

        logger.info(f"Worker {worker_id} received task {tid}: {task_text}")
        workers_status[worker_id].update({"status": "busy", "current_task": tid})
        entry = {"id": tid, "task": task_text, "worker": worker_id, "start": datetime.utcnow().isoformat() + "Z"}
        append_history({"event": "start", **entry})

        try:
            agent.task = structured_prompt(task_text)
            result = agent.run_sync(timeout=TASK_TIMEOUT)
            ss = save_screenshot(agent, tid) # Pass task_id for unique screenshot names
            entry.update({"end": datetime.utcnow().isoformat() + "Z", "status": "success", "result": str(result), "screenshot": ss})
            results[tid] = entry
            append_history({"event": "done", **entry})

            if chat_id:
                msg_text = f"✅ *Task Completed* (ID: `{tid}`)\nTask: {task_text}\nResult: {str(result)[:1500]}"
                # Ensure the message is not too long for Telegram (max 4096 characters)
                if len(msg_text) > 4000:
                    msg_text = msg_text[:3900] + "\n... (result truncated)"
                bot.send_message(chat_id=chat_id, text=msg_text, parse_mode=ParseMode.MARKDOWN)
                if ss and os.path.exists(ss):
                    try:
                        with open(ss, "rb") as photo_file:
                            bot.send_photo(chat_id=chat_id, photo=photo_file)
                    except Exception as photo_e:
                        logger.error(f"Failed to send photo for task {tid}: {photo_e}")
                        bot.send_message(chat_id=chat_id, text=f"⚠️ Failed to send screenshot for task `{tid}`.")

        except Exception as e:
            logger.exception(f"Task {tid} failed for worker {worker_id}")
            entry.update({"end": datetime.utcnow().isoformat() + "Z", "status": "failed", "error": str(e)})
            append_history({"event": "failed", **entry})
            results[tid] = entry

            if chat_id:
                bot.send_message(chat_id=chat_id, text=f"❌ Task `{tid}` failed or needs your intervention: {str(e)[:1000]}") # Truncate error for Telegram

        finally:
            workers_status[worker_id].update({"status": "idle", "current_task": None})
            task_queue.task_done()
            logger.info(f"Worker {worker_id} finished task {tid}.")
            # Close the browser instance after each task to prevent memory leaks,
            # or if 'browser-use' handles this internally, you might not need it here.
            # However, for robustness, it's often good to clean up resources.
            try:
                browser.close()
            except Exception as e:
                logger.warning(f"Failed to close browser for worker {worker_id}: {e}")
            agent, browser = create_worker_agent() # Re-initialize for next task

    logger.info(f"Worker {worker_id} shutting down.")
    try:
        browser.close() # Ensure browser is closed on shutdown
    except Exception as e:
        logger.warning(f"Failed to close browser for worker {worker_id} during shutdown: {e}")


def start_workers(n=MAX_WORKERS):
    """Starts the specified number of worker threads."""
    executor = ThreadPoolExecutor(max_workers=n)
    for i in range(n):
        wid = f"worker-{i+1}"
        executor.submit(worker_loop, wid)
        workers_status[wid] = {"status": "starting", "current_task": None}
    logger.info(f"Started {n} workers.")
    return executor

def submit_task_for_chat(task_text, chat_id=None):
    """Submits a new task to the queue."""
    tid = str(uuid.uuid4())[:8] # Generate a short unique ID
    task_queue.put({"id": tid, "task": task_text, "chat_id": chat_id, "created": datetime.utcnow().isoformat() + "Z"})
    logger.info(f"Submitted task {tid}.")
    append_history({"event": "submitted", "id": tid, "task": task_text, "chat_id": chat_id})
    return tid

# ---- Telegram Handlers ----
# Initialize Bot instance globally for easy access in worker_loop (for sending messages back)
bot = Bot(token=TELEGRAM_BOT_TOKEN)

async def auth_check(update, context: ContextTypes.DEFAULT_TYPE):
    """Checks if the user is authorized to use the bot."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        logger.warning(f"Unauthorized access attempt from user ID: {user_id}")
        await update.message.reply_text("⚠️ ليس لديك صلاحية استخدام هذا البوت.")
        return False
    return True

async def start_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if not await auth_check(update, context): return
    await update.message.reply_text("مرحباً! أنا هنا لمساعدتك في أتمتة مهام المتصفح. أرسل نص المهمة مباشرةً أو استخدم /submit <مهمتك>.")

async def submit_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /submit command."""
    if not await auth_check(update, context): return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("اكتب: /submit <مهمتك> لتحديد مهمة لي.")
        return
    tid = submit_task_for_chat(text, chat_id=update.effective_chat.id)
    await update.message.reply_text(f"✅ تم استلام المهمة بنجاح! رقم المهمة: `{tid}`. سيتم إخطارك عند الانتهاء.")
    logger.info(f"Telegram user {update.effective_user.id} submitted task {tid} via /submit.")

async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Handles plain text messages as tasks."""
    if not await auth_check(update, context): return
    text = update.message.text.strip()
    if not text: # Ignore empty messages
        return
    tid = submit_task_for_chat(text, chat_id=update.effective_chat.id)
    await update.message.reply_text(f"✅ تم استلام رسالتك كمهمة! رقم المهمة: `{tid}`. سيتم إخطارك عند الانتهاء.")
    logger.info(f"Telegram user {update.effective_user.id} submitted task {tid} via direct message.")

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Log Errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            f"An error occurred: `{context.error}`. Please try again or report this issue."
        )


# ---- main ----
def main():
    """Main function to start workers and the Telegram bot."""
    logger.info("Starting workers...")
    worker_executor = start_workers(MAX_WORKERS)
    logger.info("Starting Telegram bot...")

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("submit", submit_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler) # Add error handler

    # Start the bot
    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
    except Exception as e:
        logger.exception(f"Telegram bot encountered an error: {e}")
    finally:
        logger.info("Shutting down workers...")
        shutdown_event.set() # Signal workers to stop
        worker_executor.shutdown(wait=True) # Wait for workers to finish current tasks
        logger.info("All workers shut down.")
        logger.info("Bot application gracefully shut down.")

if __name__ == "__main__":
    main()

