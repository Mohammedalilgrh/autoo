import os, json, time, uuid, logging, threading
from queue import Queue, Empty
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By

from dotenv import load_dotenv
load_dotenv()

# --- Environment Variables ---
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDnsQ_EaKER5TghXoh8mpkoy_tXZoaYZ58")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7851395212:AAG9O6MMmVrVBQtpCSJF7N5xgvnNnvNNwbQ")
ALLOWED_USERS_STR = os.getenv("ALLOWED_USERS", "21706160")
ALLOWED_USERS = [int(x.strip()) for x in ALLOWED_USERS_STR.split(',') if x.strip().isdigit()]

API_HASH = os.getenv("API_HASH", "548b91f0e7cd2e44bbee05190620d9f4")

# ---- Config ----
MODEL = "gemini-2.5-flash"
OUTPUT_DIR = os.path.join(os.getcwd(), "tg_outputs")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "history.json")
MAX_WORKERS = 2
HEADLESS = False  # Show browser
TASK_TIMEOUT = 300  # Allow longer tasks
SCROLL_PAUSE = 1.0  # Delay for scrolling
TYPING_DELAY = 0.05  # Delay between keystrokes

# ---- Logging ----
os.makedirs(OUTPUT_DIR, exist_ok=True)
logger = logging.getLogger("PowerBrowserAgent")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(OUTPUT_DIR, "agent.log"), encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(ch)

# ---- Imports that need logging/config ----
from browser_use import Agent, Browser
from browser_use.llm import ChatGoogle
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ---- Task Queue & Workers ----
task_queue = Queue()
results = {}
workers_status = {}
shutdown_event = threading.Event()

# ---- Helpers ----
def append_history(entry):
    try:
        if os.path.exists(HISTORY_FILE) and os.path.getsize(HISTORY_FILE) > 0:
            with open(HISTORY_FILE, "r+", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = []
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
    return (not ALLOWED_USERS) or (user_id in ALLOWED_USERS)

def structured_prompt(task_text):
    return f"Perform this task in browser, step by step, ask user if needed: {task_text}"

# ---- Agent Worker ----
def create_worker_agent():
    llm = ChatGoogle(model=MODEL, api_key=GEMINI_KEY)
    browser = Browser(headless=HEADLESS)
    agent = Agent(task="initialize", llm=llm, browser=browser)
    return agent, browser

def perform_mouse_actions(browser, actions):
    """Simulate mouse actions in browser."""
    chain = ActionChains(browser.driver)
    for act in actions:
        if act["type"] == "click":
            elem = browser.driver.find_element(By.CSS_SELECTOR, act["selector"])
            chain.move_to_element(elem).click()
        elif act["type"] == "scroll":
            browser.driver.execute_script(f"window.scrollBy(0,{act.get('amount', 300)});")
            time.sleep(SCROLL_PAUSE)
        elif act["type"] == "hover":
            elem = browser.driver.find_element(By.CSS_SELECTOR, act["selector"])
            chain.move_to_element(elem)
    chain.perform()

def fill_input_fields(browser, fields):
    """Fill input fields with typing simulation."""
    chain = ActionChains(browser.driver)
    for f in fields:
        elem = browser.driver.find_element(By.CSS_SELECTOR, f["selector"])
        elem.clear()
        for ch in f["value"]:
            elem.send_keys(ch)
            time.sleep(TYPING_DELAY)

def worker_loop(worker_id):
    agent, browser = create_worker_agent()
    workers_status[worker_id] = {"status": "idle", "current_task": None}
    logger.info(f"Worker {worker_id} started.")

    while not shutdown_event.is_set():
        try:
            item = task_queue.get(timeout=1)
        except Empty:
            continue

        tid = item["id"]
        task_text = item["task"]
        chat_id = item.get("chat_id")
        workers_status[worker_id].update({"status": "busy", "current_task": tid})
        entry = {"id": tid, "task": task_text, "worker": worker_id, "start": datetime.utcnow().isoformat() + "Z"}
        append_history({"event": "start", **entry})

        try:
            agent.task = structured_prompt(task_text)
            result = agent.run_sync(timeout=TASK_TIMEOUT)

            # Optional: If agent returns mouse/scroll actions
            if hasattr(agent, "actions") and agent.actions:
                perform_mouse_actions(browser, agent.actions)
            if hasattr(agent, "inputs") and agent.inputs:
                fill_input_fields(browser, agent.inputs)

            entry.update({"end": datetime.utcnow().isoformat() + "Z", "status": "success", "result": str(result)})
            results[tid] = entry
            append_history({"event": "done", **entry})

            if chat_id:
                msg_text = f"✅ Task Completed (ID: `{tid}`)\nTask: {task_text}\nResult: {str(result)[:1500]}"
                if len(msg_text) > 4000:
                    msg_text = msg_text[:3900] + "\n... (result truncated)"
                bot.send_message(chat_id=chat_id, text=msg_text, parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.exception(f"Task {tid} failed for worker {worker_id}")
            entry.update({"end": datetime.utcnow().isoformat() + "Z", "status": "failed", "error": str(e)})
            append_history({"event": "failed", **entry})
            results[tid] = entry
            if chat_id:
                bot.send_message(chat_id=chat_id, text=f"❌ Task `{tid}` failed: {str(e)[:1000]}")

        finally:
            workers_status[worker_id].update({"status": "idle", "current_task": None})
            task_queue.task_done()
            try:
                browser.close()
            except:
                pass
            agent, browser = create_worker_agent()

    try:
        browser.close()
    except:
        pass
    logger.info(f"Worker {worker_id} shutting down.")

def start_workers(n=MAX_WORKERS):
    executor = ThreadPoolExecutor(max_workers=n)
    for i in range(n):
        wid = f"worker-{i+1}"
        executor.submit(worker_loop, wid)
        workers_status[wid] = {"status": "starting", "current_task": None}
    logger.info(f"Started {n} workers.")
    return executor

def submit_task_for_chat(task_text, chat_id=None):
    tid = str(uuid.uuid4())[:8]
    task_queue.put({"id": tid, "task": task_text, "chat_id": chat_id, "created": datetime.utcnow().isoformat() + "Z"})
    logger.info(f"Submitted task {tid}.")
    append_history({"event": "submitted", "id": tid, "task": task_text, "chat_id": chat_id})
    return tid

# ---- Telegram Handlers ----
bot = Bot(token=TELEGRAM_BOT_TOKEN)

async def auth_check(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        logger.warning(f"Unauthorized access attempt from user ID: {user_id}")
        await update.message.reply_text("⚠️ You are not allowed to use this bot.")
        return False
    return True

async def start_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update, context): return
    await update.message.reply_text("Hi! Send a task or use /submit <task>.")

async def submit_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update, context): return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Write: /submit <your task>.")
        return
    tid = submit_task_for_chat(text, chat_id=update.effective_chat.id)
    await update.message.reply_text(f"✅ Task received! ID: `{tid}`.")

async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update, context): return
    text = update.message.text.strip()
    if not text: return
    tid = submit_task_for_chat(text, chat_id=update.effective_chat.id)
    await update.message.reply_text(f"✅ Your message is received as task! ID: `{tid}`.")

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text(f"Error: `{context.error}`")

# ---- Main ----
def main():
    logger.info("Starting workers...")
    worker_executor = start_workers(MAX_WORKERS)
    logger.info("Starting Telegram bot...")

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("submit", submit_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler)

    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    finally:
        logger.info("Shutting down workers...")
        shutdown_event.set()
        worker_executor.shutdown(wait=True)
        logger.info("All workers shut down.")

if __name__ == "__main__":
    main()
