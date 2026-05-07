import asyncio
import logging
import os
import sys

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
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
from core.notification_manager import NotificationManager
from core.human_in_the_loop import has_pending_request, provide_human_input

# Load environment variables
load_env()

# Configure UTF-8 for Windows terminals
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Initialize DB and MasterAI
db = DBManager()
master_ai = MasterAI()
notifier = NotificationManager()

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
PLANNING_MODE = 1


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


def format_plan_summary(plan: dict) -> str:
    """Formats the JSON plan into a human-readable summary for Telegram."""
    if not plan:
        return ""
    
    summary = "<b>📋 Proposed Workflow Plan:</b>\n\n"
    
    # 1. Agents Section
    summary += "<b>👥 Team Composition:</b>\n"
    agents = plan.get("agents", [])
    if not agents:
        summary += "<i>No agents defined yet.</i>\n"
    for i, agent in enumerate(agents):
        summary += f"{i+1}. <b>{agent.get('role')}</b>\n   🎯 <i>Goal:</i> {agent.get('goal')}\n"
    
    # 2. Tasks Section
    summary += "\n<b>📝 Execution Steps:</b>\n"
    tasks = plan.get("tasks", [])
    if not tasks:
        summary += "<i>No tasks defined yet.</i>\n"
    for i, task in enumerate(tasks):
        desc = task.get('description', '')
        # Truncate long descriptions for readability
        short_desc = (desc[:120] + '...') if len(desc) > 120 else desc
        summary += f"{i+1}. {short_desc}\n   👤 <i>Assignee:</i> {task.get('agent_role')}\n"
    
    summary += "\n<i>Do you want to proceed or make any changes?</i>"
    return summary


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
        "👋 Hello! I am Alfredo, your AI Assistant.\n\nYou can <b>type a request</b> directly or choose a workflow from the list below:", 
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )


@whitelist_check
async def workflow_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    workflow_id = int(query.data.split("_")[1])
    workflow = db.read_workflow(workflow_id)
    if not workflow:
        await query.edit_message_text("Error: Workflow not found.")
        return ConversationHandler.END

    context.user_data["base_workflow"] = workflow
    context.user_data["chat_history"] = []

    await query.edit_message_text(f"📝 <b>Initializing Planner for '{workflow['name']}'...</b>", parse_mode=ParseMode.HTML)

    initial_prompt = f"I want to run the workflow '{workflow['name']}'. What do you need from me?"
    result = await asyncio.to_thread(master_ai.chat_plan, initial_prompt, [], workflow)

    context.user_data["chat_history"].append({"role": "user", "content": initial_prompt})
    context.user_data["chat_history"].append({"role": "assistant", "content": result["response"]})

    await context.bot.send_message(chat_id=update.effective_chat.id, text=result["response"])
    return PLANNING_MODE

@whitelist_check
async def free_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    chat_id = update.effective_chat.id

    if has_pending_request(str(chat_id)):
        provide_human_input(str(chat_id), user_input)
        await context.bot.send_message(chat_id=chat_id, text="✅ Reply sent to the agent. Resuming execution...")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML)

    context.user_data["base_workflow"] = None
    context.user_data["chat_history"] = []

    result = await asyncio.to_thread(master_ai.chat_plan, user_input)

    context.user_data["chat_history"].append({"role": "user", "content": user_input})
    context.user_data["chat_history"].append({"role": "assistant", "content": result["response"]})

    await status_msg.edit_text(result["response"])
    return PLANNING_MODE

@whitelist_check
async def handle_planning_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    chat_id = update.effective_chat.id

    if has_pending_request(str(chat_id)):
        provide_human_input(str(chat_id), user_input)
        await context.bot.send_message(chat_id=chat_id, text="✅ Reply sent to the agent. Resuming execution...")
        return PLANNING_MODE

    chat_history = context.user_data.get("chat_history", [])
    base_workflow = context.user_data.get("base_workflow")

    status_msg = await update.message.reply_text("🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML)

    result = await asyncio.to_thread(master_ai.chat_plan, user_input, chat_history, base_workflow)

    chat_history.append({"role": "user", "content": user_input})
    chat_history.append({"role": "assistant", "content": result["response"]})
    context.user_data["chat_history"] = chat_history

    await status_msg.edit_text(result["response"])

    if result.get("status") == "ready":
        plan = result.get("plan")
        if plan:
            context.user_data["final_plan"] = plan
            # Show the final plan one last time for clarity
            final_summary = format_plan_summary(plan)
            await context.bot.send_message(
                chat_id=chat_id, 
                text=f"✅ <b>Plan Confirmed!</b>\n\n{final_summary}",
                parse_mode=ParseMode.HTML
            )
            await context.bot.send_message(chat_id=chat_id, text="🚀 Starting execution...")
            
            if base_workflow:
                context.user_data["current_workflow_id"] = base_workflow["id"]
                
            context.user_data["execution_context"] = {"user_input": " ".join([m['content'] for m in chat_history])}
            await execute_crew(update, context)
                
        return ConversationHandler.END
    else:
        # If still planning, but a draft plan exists, show it
        draft_plan = result.get("plan")
        if draft_plan:
            draft_summary = format_plan_summary(draft_plan)
            await context.bot.send_message(
                chat_id=chat_id,
                text=draft_summary,
                parse_mode=ParseMode.HTML
            )

    return PLANNING_MODE


async def execute_crew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Helper function to build and execute the CrewAI crew.
    Handles asynchronous execution and error reporting.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    workflow_id = context.user_data.get("current_workflow_id")
    final_plan = context.user_data.get("final_plan")
    execution_context = context.user_data.get("execution_context", {})

    if not workflow_id and not final_plan:
        logger.error(f"User {user_id}: Neither Workflow ID nor Dynamic Plan found.")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Error: Could not retrieve execution details. Please `/start` again.",
        )
        return

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⚙️ <b>Assembling the Crew...</b>",
        parse_mode=ParseMode.HTML
    )

    # Create a run record if it's a predefined workflow
    run_id = None
    if workflow_id:
        run_id = db.create_run(workflow_id, status='running')

    try:
        from core.crew_builder import build_crew, build_dynamic_crew
        
        # Build the CrewAI crew
        if final_plan:
            logger.info(f"User {user_id}: Using Dynamic Crew Builder for execution.")
            crew = await asyncio.to_thread(build_dynamic_crew, final_plan)
        else:
            logger.info(f"User {user_id}: Using Standard Crew Builder for Workflow ID {workflow_id}.")
            crew = await asyncio.to_thread(build_crew, workflow_id)
            
        if not crew:
            raise ValueError("Failed to build crew.")

        # Update message to show execution started
        await status_msg.edit_text(
            text="🚀 <b>Execution in progress...</b>\n<i>My agents are working for you.</i>",
            parse_mode=ParseMode.HTML
        )

        # Inject chat_id for tools
        os.environ["CURRENT_CHAT_ID"] = str(chat_id)

        # Background task to keep the 'typing' indicator alive
        async def typing_indicator():
            while True:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(typing_indicator())

        try:
            # CRITICAL ARCHITECTURE FIX: Execute CrewAI in a separate thread
            # to prevent blocking the Telegram bot's event loop.
            logger.info(f"User {user_id}: Kicking off CrewAI with context: {execution_context}")
            final_result = await asyncio.to_thread(crew.kickoff, inputs=execution_context)
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        logger.info(f"User {user_id}: CrewAI execution finished.")
        
        # --- AUTOMATIC POST-PROCESSING: Master AI Refinement ---
        # The raw crew output is passed through Master AI for:
        # 1. Formatting & syntax cleanup
        # 2. Ethical review
        # 3. Synthesis into a clear, user-friendly report
        await status_msg.edit_text(
            text="🧠 <b>Refining output...</b>\n<i>Master AI is polishing the final report.</i>",
            parse_mode=ParseMode.HTML
        )
        
        try:
            refined_result = await asyncio.to_thread(master_ai.refine_output, str(final_result))
            logger.info(f"User {user_id}: Output refinement complete.")
        except Exception as refine_err:
            logger.error(f"Output refinement failed: {refine_err}. Using raw output.")
            refined_result = str(final_result)

        # Update run record with the refined result
        if run_id:
            db.update_run(run_id, status='completed', result=refined_result)

        # Save refined output to context memory for continuation
        db.update_context(str(chat_id), refined_result)

        # Send completion notification
        wf_name = "Dynamic Workflow"
        if workflow_id:
            workflow = db.read_workflow(workflow_id)
            wf_name = workflow["name"] if workflow else "Unknown"
            
        notifier.notify_workflow_completion(wf_name, refined_result, chat_id=chat_id)

        keyboard = [
            [
                InlineKeyboardButton("🔄 Continue (Use this result)", callback_data="context_continue"),
                InlineKeyboardButton("🆕 New Conversation", callback_data="context_new")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Delete the status message now that we have the final result
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Send the refined report — use Markdown for better formatting
        # Truncate if needed (Telegram max message length is 4096 chars)
        report_text = refined_result
        if len(report_text) > 3800:
            report_text = report_text[:3800] + "\n\n... _(truncated — full report saved in history)_"

        final_caption = f"✅ *Execution Complete!*\n\n{report_text}\n\n_Choose how to proceed:_"

        try:
            # Attempt sending with Markdown for rich formatting
            await context.bot.send_message(
                chat_id=chat_id,
                text=final_caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        except Exception as e:
            if "Can't parse entities" in str(e):
                logger.warning(f"Markdown parsing failed for message. Falling back to plain text. Error: {e}")
                # Fallback to plain text if Markdown is broken
                fallback_text = f"✅ Execution Complete!\n\n{refined_result}\n\nChoose how to proceed:"
                if len(fallback_text) > 4000:
                    fallback_text = fallback_text[:4000] + "..."
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=fallback_text,
                    reply_markup=reply_markup
                )
            else:
                # If it's a different error, re-raise it
                raise e

    except Exception as e:
        logger.error(f"CrewAI execution failed: {e}", exc_info=True)
        
        # Update run record with failure
        if run_id:
            db.update_run(run_id, status='failed', result=str(e))

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


# Old free_chat_handler removed as it is now an entry point in ConversationHandler

@whitelist_check
async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancels the current conversation."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"User {user_id} canceled the conversation.")
    
    # Critical: Unblock any pending agent threads!
    if has_pending_request(str(chat_id)):
        provide_human_input(str(chat_id), "SYSTEM_ABORT")
        logger.info(f"Aborted pending human-in-the-loop request for chat {chat_id}.")
        
    await update.message.reply_text(
        "Operation aborted. Use /start to see available workflows."
    )
    context.user_data.clear()
    return ConversationHandler.END


@whitelist_check
async def handle_context_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the user's choice to continue or start a new conversation."""
    query = update.callback_query
    await query.answer()
    
    choice = query.data
    chat_id = update.effective_chat.id
    
    if choice == "context_new":
        db.clear_context(str(chat_id))
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text="Memory cleared. Let's start a new conversation! 🆕")
    elif choice == "context_continue":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=chat_id, text="Perfect, I will keep the last result in mind for the next workflow! 🔄")


# --- Main Function ---
def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ConversationHandler for workflow selection and execution
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(workflow_selection_callback, pattern=r"^workflow_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, free_chat_handler)
        ],
        states={
            PLANNING_MODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_planning_chat)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("stop", cancel_conversation)
        ],
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    
    # Global handler for 'stop' command (literal word)
    application.add_handler(MessageHandler(filters.Regex(r'(?i)^stop$') & ~filters.COMMAND, cancel_conversation))
    
    # context handler for callback buttons at the end of a workflow
    application.add_handler(CallbackQueryHandler(handle_context_choice, pattern=r"^context_"))

    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()