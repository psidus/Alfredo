import asyncio
import logging
import os
import sys

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Add parent directory to path to allow imports from core and db
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.data_manager import load_env
from core.db_manager import DBManager
from core.master_ai import MasterAI
from core.crew_builder import build_crew

# Load environment variables
load_env()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize DB and MasterAI
db = DBManager()
master_ai = MasterAI()

# --- Configuration from .env ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_IDS = os.getenv("TELEGRAM_ALLOWED_USER_IDS")

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN not found in environment variables.")
    sys.exit(1)

if not TELEGRAM_ALLOWED_USER_IDS:
    logger.warning("TELEGRAM_ALLOWED_USER_IDS not set. Bot will be accessible to anyone.")
    ALLOWED_USER_IDS = []
else:
    try:
        ALLOWED_USER_IDS = [int(uid.strip()) for uid in TELEGRAM_ALLOWED_USER_IDS.split(",")]
    except ValueError:
        logger.critical(
            "Invalid TELEGRAM_ALLOWED_USER_IDS format. Must be comma-separated integers."
        )
        sys.exit(1)

# --- ConversationHandler States ---
SHOW_TASKS = 1  
CAPTURE_EDITS = 2
ASK_MISSING_INPUT = 3 # New state for guided prompts
EXECUTION = 4


# --- Whitelist Check Decorator ---
def whitelist_check(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            logger.warning(
                f"Unauthorized access attempt by user ID: {user_id} ({update.effective_user.username})"
            )
            await update.message.reply_text(
                "You are not authorized to use this bot. Your User ID has been logged."
            )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


# --- Command Handlers (from M4_T1 and M4_T2) ---
@whitelist_check
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with inline buttons for available workflows."""
    user = update.effective_user
    logger.info(f"User {user.first_name} ({user.id}) started the bot.")

    workflows = db.read_all_workflows()
    if not workflows:
        await update.message.reply_text(
            "No workflows found in the database. Please add some via the Streamlit UI."
        )
        return

    keyboard = []
    for wf in workflows:
        keyboard.append(
            [InlineKeyboardButton(wf["name"], callback_data=f"workflow_{wf['id']}")]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Hello! I am Alfredo, your AI Assistant.\n\nYou can **type a request** directly or choose a workflow from the list below:", 
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )


@whitelist_check
async def workflow_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Entry point for ConversationHandler.
    Displays workflow tasks and prompts for user edits or direct execution.
    """
    query = update.callback_query
    await query.answer()

    workflow_id = int(query.data.split("_")[1])
    context.user_data["current_workflow_id"] = workflow_id
    logger.info(f"User {query.from_user.id} selected workflow ID: {workflow_id}")

    workflow = db.read_workflow(workflow_id)
    if not workflow:
        await query.edit_message_text("Error: Workflow not found.")
        return ConversationHandler.END

    task_ids = workflow["task_ids"]
    # db_manager handles individual task fetching if needed, but here we just need descriptions
    tasks = [db.read_task(tid) for tid in task_ids if tid]

    tasks_message = f"<b>Pre-Flight Check: Workflow '{workflow['name']}'</b>\n\n<b>Tasks:</b>\n"
    for i, task in enumerate(tasks):
        if task:
            tasks_message += f"{i + 1}. <i>{task['description']}</i>\n"

    tasks_message += (
        "\n<i>Please type any specific instructions or edits for this run, "
        "or click 'Execute As Is' to proceed with default parameters.</i>"
    )

    keyboard = [[InlineKeyboardButton("🚀 Execute As Is", callback_data="execute_default")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        tasks_message, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )

    return CAPTURE_EDITS


@whitelist_check
async def handle_user_edits(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Captures user text input for Master AI analysis.
    """
    user_input = update.message.text
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    logger.info(f"User {user_id} provided edits: {user_input[:100]}...")

    # Security Check: Sanitize input length
    if len(user_input) > 2000:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Your input is too long. Please keep it under 2000 characters.",
        )
        return CAPTURE_EDITS # Stay in the same state

    await context.bot.send_message(
        chat_id=chat_id, text="Analyzing instructions via Master AI..."
    )

    try:
        # Master AI processes user input to derive execution context
        routing_result = await asyncio.to_thread(master_ai.evaluate_intent, user_input)
        
        execution_context = routing_result.get("extracted_params", {})
        if not execution_context and user_input:
             execution_context = {"user_input": user_input}
             
        context.user_data["execution_context"] = execution_context
        
        # Check for missing inputs
        missing_details = routing_result.get("missing_inputs_details", [])
        if missing_details:
            context.user_data["missing_inputs_queue"] = missing_details
            return await ask_next_missing_input(update, context)

    except Exception as e:
        logger.error(f"Master AI processing failed for user {user_id}: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Master AI encountered an error. Proceeding with raw input.",
        )
        context.user_data["execution_context"] = {"user_input": user_input}

    await execute_crew(update, context)
    return ConversationHandler.END


@whitelist_check
async def execute_as_is(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Triggers crew execution with default parameters, but checks if an idea is needed first.
    """
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    workflow_id = context.user_data.get("current_workflow_id")
    
    workflow = db.read_workflow(workflow_id)
    workflow_name = workflow["name"].lower() if workflow else ""

    # Logic: If it's a brainstorming/creative workflow, we SHOULD have an idea.
    # We check if the user has provided ANY context yet.
    if not context.user_data.get("execution_context"):
        logger.info(f"User {user_id} chose 'As Is' for '{workflow_name}'. Checking if input is required.")
        
        # Heuristic: If name contains 'brainstorm' or 'crea', prompt for an idea.
        if any(keyword in workflow_name for keyword in ["brainstorm", "crea", "idea", "progetto"]):
             await context.bot.send_message(
                 chat_id=chat_id,
                 text="💡 <b>Attenzione:</b> Questo workflow richiede un'idea di partenza.\n\n<i>Per favore, scrivi qui sotto l'idea o l'argomento su cui vuoi lavorare:</i>",
                 parse_mode=ParseMode.HTML
             )
             return CAPTURE_EDITS

    logger.info(f"User {user_id} proceeding with execution.")
    await context.bot.send_message(
        chat_id=chat_id, text="🚀 Avvio esecuzione in corso..."
    )
    # If we already have context (e.g. from a previous message), keep it.
    if "execution_context" not in context.user_data:
        context.user_data["execution_context"] = {}

    await execute_crew(update, context)
    return ConversationHandler.END


async def ask_next_missing_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Helper to ask the next question in the missing inputs queue.
    """
    queue = context.user_data.get("missing_inputs_queue", [])
    if not queue:
        await execute_crew(update, context)
        return ConversationHandler.END

    next_input = queue.pop(0)
    context.user_data["current_missing_input_key"] = next_input["key"]
    
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"❓ <b>Domanda:</b> {next_input['prompt']}",
        parse_mode=ParseMode.HTML
    )
    return ASK_MISSING_INPUT


@whitelist_check
async def handle_missing_input_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Captures the answer for a missing input and proceeds to the next one or execution.
    """
    answer = update.message.text
    key = context.user_data.get("current_missing_input_key")
    
    if key:
        if "execution_context" not in context.user_data:
            context.user_data["execution_context"] = {}
        context.user_data["execution_context"][key] = answer
        logger.info(f"Captured input for {key}: {answer}")

    return await ask_next_missing_input(update, context)


async def execute_crew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Helper function to build and execute the CrewAI crew.
    Handles asynchronous execution and error reporting.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    workflow_id = context.user_data.get("current_workflow_id")
    execution_context = context.user_data.get("execution_context", {})

    if not workflow_id:
        logger.error(f"User {user_id}: Workflow ID not found in user_data for execution.")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Error: Could not retrieve workflow details. Please `/start` again.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="Assembling Crew and beginning execution. This may take a while...",
    )

    try:
        # Build the CrewAI crew dynamically
        crew = await asyncio.to_thread(build_crew, workflow_id)
        if not crew:
            raise ValueError(f"Failed to build crew for workflow ID {workflow_id}")

        # CRITICAL ARCHITECTURE FIX: Execute CrewAI in a separate thread
        # to prevent blocking the Telegram bot's event loop.
        logger.info(f"User {user_id}: Kicking off CrewAI for workflow ID {workflow_id} with context: {execution_context}")
        final_result = await asyncio.to_thread(crew.kickoff, inputs=execution_context)

        logger.info(f"User {user_id}: CrewAI execution finished for workflow ID {workflow_id}.")
        # Use HTML and pre tags for cleaner output without markdown escaping issues
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ <b>Execution Complete!</b>\n\n<pre>{final_result}</pre>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(
            f"CrewAI execution failed for user {user_id}, workflow ID {workflow_id}: {e}",
            exc_info=True,
        )
        # Security Wrapper: Do not send raw exception details to Telegram
        error_message = (
            "An error occurred during crew execution. "
            "Please check the local logs for more details. "
            "If the issue persists, contact support."
        )
        await context.bot.send_message(chat_id=chat_id, text=error_message)

    finally:
        # Clean up user_data for the next conversation
        context.user_data.clear()


@whitelist_check
async def free_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles natural language messages outside of a specific workflow context.
    Uses Master AI to route the request to the best matching workflow.
    """
    user_input = update.message.text
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    logger.info(f"Free chat from user {user_id}: {user_input[:100]}...")
    
    # 1. Analyze intent via Master AI
    status_msg = await update.message.reply_text("🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML)
    
    try:
        routing_result = await asyncio.to_thread(master_ai.evaluate_intent, user_input)
        
        if routing_result.get("status") == "success" and routing_result.get("workflow_id"):
            workflow_id = int(routing_result["workflow_id"])
            workflow = db.read_workflow(workflow_id)
            
            if workflow:
                await status_msg.edit_text(f"🎯 Intent matched: <b>{workflow['name']}</b>", parse_mode=ParseMode.HTML)
                
                context.user_data["current_workflow_id"] = workflow_id
                context.user_data["execution_context"] = routing_result.get("extracted_params", {"user_input": user_input})
                
                # Check for missing inputs
                missing_details = routing_result.get("missing_inputs_details", [])
                if missing_details:
                    context.user_data["missing_inputs_queue"] = missing_details
                    await ask_next_missing_input(update, context)
                    return # We stay in the global MessageHandler but state is managed if part of Conv
                
                # Execute immediately
                await execute_crew(update, context)
                return
        
        # 2. Fallback if no intent matched
        await status_msg.edit_text(
            "I couldn't match your request to a specific workflow. 🧐\n\n"
            "Try being more specific or choose one from the list using /start."
        )
        
    except Exception as e:
        logger.error(f"Free chat routing failed: {e}", exc_info=True)
        await status_msg.edit_text("Sorry, I encountered an error while processing your request.")

@whitelist_check
async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancels the current conversation."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} canceled the conversation.")
    await update.message.reply_text(
        "Operation aborted. Use /start to see available workflows."
    )
    context.user_data.clear()
    return ConversationHandler.END


# --- Main Function ---
def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ConversationHandler for workflow selection and execution
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(workflow_selection_callback, pattern="^workflow_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat_handler)
        ],
        states={
            CAPTURE_EDITS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_edits),
                CallbackQueryHandler(execute_as_is, pattern="^execute_default$"),
            ],
            ASK_MISSING_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_missing_input_answer)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation), CommandHandler("stop", cancel_conversation)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    
    # Global handler for 'stop' command (literal word)
    application.add_handler(MessageHandler(filters.Regex(r'^(?i)stop$') & ~filters.COMMAND, cancel_conversation))

    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()