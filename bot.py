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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("debug_alfredo.log")
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
COLLECTING_INPUTS = 2


# --- Whitelist Check Decorator ---
def whitelist_check(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            logger.warning(
                f"Unauthorized access attempt by user ID: {user_id} ({update.effective_user.username})"
            )
            if update.message:
                await update.message.reply_text(
                    "You are not authorized to use this bot. Your User ID has been logged."
                )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


# --- Shared Helpers ---

async def _send_workflow_list(chat_id: int, bot) -> None:
    """Sends the workflow selection menu to the given chat_id."""
    workflows = db.read_all_workflows()
    if not workflows:
        await bot.send_message(
            chat_id=chat_id,
            text="No workflows found in the database. Please add some via the Streamlit UI."
        )
        return
    keyboard = []
    for wf in workflows:
        keyboard.append(
            [InlineKeyboardButton(wf["name"], callback_data=f"workflow_{wf['id']}")]
        )
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(
        chat_id=chat_id,
        text="👋 Hello! I am Alfredo, your AI Assistant.\n\nYou can <b>type a request</b> directly or choose a workflow from the list below:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )


def _resolve_db_placeholders(text: str, task_record: dict) -> str:
    """
    Resolves DB-set placeholder values in a text string for display purposes.
    Replaces {specialization} with the agent_specialization stored on the task,
    so the bot shows e.g. "specialized in Chemical Engineering" instead of
    "specialized in {specialization}".
    Only resolves values already set in the DB — user-input variables remain as-is.
    """
    if not text:
        return text
    specialization = task_record.get('agent_specialization') or ''
    if specialization:
        text = text.replace('{specialization}', specialization)
    return text


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
        short_desc = (desc[:120] + '...') if len(desc) > 120 else desc
        summary += f"{i+1}. {short_desc}\n   👤 <i>Assignee:</i> {task.get('agent_role')}\n"
        # Show required inputs that will be collected before execution
        req_inputs = task.get('required_inputs') or []
        if req_inputs:
            keys = []
            for ri in req_inputs:
                if isinstance(ri, dict):
                    keys.append(f"<code>{ri.get('key', '?')}</code>")
                elif isinstance(ri, str):
                    keys.append(f"<code>{ri}</code>")
            if keys:
                input_keys = ', '.join(keys)
                summary += f"   📋 <i>Inputs needed:</i> {input_keys}\n"

    summary += "\n<i>Do you want to proceed or make any changes?</i>"
    return summary


# --- Command Handlers ---

@whitelist_check
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with inline buttons for available workflows."""
    user = update.effective_user
    logger.info(f"User {user.first_name} ({user.id}) started the bot.")
    await _send_workflow_list(update.effective_chat.id, context.bot)


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

    await query.edit_message_text(
        f"📝 <b>Loading Workflow '{workflow['name']}'...</b>", parse_mode=ParseMode.HTML
    )

    # Build a natural-language summary directly from DB data (no LLM call here).
    # This guarantees the initial presentation is faithful to the predefined workflow.
    task_ids = workflow.get('task_ids') or []
    task_lines = []
    for i, tid in enumerate(task_ids):
        t_rec = db.read_task(tid)
        if t_rec:
            a_rec = db.read_agent(t_rec['agent_id']) if t_rec.get('agent_id') else None

            # Resolve DB placeholders (e.g. {specialization}) for display
            agent_role = a_rec.get('role', 'Unknown agent') if a_rec else 'Unknown agent'
            agent_display = _resolve_db_placeholders(agent_role, t_rec)

            # Task label: name or truncated description (with placeholders resolved)
            t_label = t_rec.get('name') or t_rec.get('description', '')[:60]
            t_label = _resolve_db_placeholders(t_label, t_rec)

            # Show required input keys as a hint so the user knows what to expect
            req_inputs = t_rec.get('required_inputs') or []
            ri_hint = ''
            if req_inputs:
                ri_hint = f" — <i>needs: {', '.join(ri.get('key', '?') for ri in req_inputs)}</i>"

            task_lines.append(f"{i+1}. <b>{t_label}</b> — {agent_display}{ri_hint}")

    tasks_block = "\n".join(task_lines) if task_lines else "<i>No tasks defined.</i>"
    intro = (
        f"📋 <b>Workflow: {workflow['name']}</b>\n\n"
        f"Here's the predefined plan I'll execute for you:\n\n"
        f"{tasks_block}\n\n"
        f"💬 <i>Reply with your specific inputs or context (e.g. dataset name, objective...), "
        f"or just say <b>\"go\"</b> to start as-is. You can also ask me to customize any step.</i>"
    )

    # Record the intro as the assistant's first message so the conversation flows naturally
    context.user_data["chat_history"].append({"role": "assistant", "content": intro})
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=intro, parse_mode=ParseMode.HTML
    )
    return PLANNING_MODE


@whitelist_check
async def free_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text
    chat_id = update.effective_chat.id

    if has_pending_request(str(chat_id)):
        provide_human_input(str(chat_id), user_input)
        await context.bot.send_message(
            chat_id=chat_id, text="✅ Reply sent to the agent. Resuming execution..."
        )
        return ConversationHandler.END

    status_msg = await update.message.reply_text(
        "🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML
    )

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
        await context.bot.send_message(
            chat_id=chat_id, text="✅ Reply sent to the agent. Resuming execution..."
        )
        return PLANNING_MODE

    chat_history = context.user_data.get("chat_history", [])
    base_workflow = context.user_data.get("base_workflow")

    status_msg = await update.message.reply_text(
        "🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML
    )

    result = await asyncio.to_thread(master_ai.chat_plan, user_input, chat_history, base_workflow)

    chat_history.append({"role": "user", "content": user_input})
    chat_history.append({"role": "assistant", "content": result["response"]})
    context.user_data["chat_history"] = chat_history

    await status_msg.edit_text(result["response"])

    if result.get("status") == "ready":
        plan = result.get("plan")
        is_modified = result.get("modified", True)  # default True for safety
        use_predefined = base_workflow and not is_modified

        # 1. If it's a predefined workflow without changes, build a plan representation from DB
        if use_predefined and not plan:
            task_ids = base_workflow.get('task_ids') or []
            wf_tasks = []
            wf_agents = []
            seen_agent_roles = set()
            
            for tid in task_ids:
                t_rec = db.read_task(tid)
                if t_rec:
                    a_rec = db.read_agent(t_rec['agent_id']) if t_rec.get('agent_id') else None
                    agent_role = a_rec.get('role', 'Unknown Agent') if a_rec else 'Unknown Agent'
                    
                    if a_rec and agent_role not in seen_agent_roles:
                        wf_agents.append({
                            "role": a_rec.get("role"),
                            "goal": a_rec.get("goal"),
                            "backstory": a_rec.get("backstory"),
                            "tools": a_rec.get("tools") or []
                        })
                        seen_agent_roles.add(agent_role)
                        
                    wf_tasks.append({
                        "name": t_rec.get("name") or t_rec.get("description")[:30],
                        "description": t_rec.get("description"),
                        "expected_output": t_rec.get("expected_output"),
                        "agent_role": agent_role,
                        "agent_specialization": t_rec.get("agent_specialization"),
                        "required_inputs": t_rec.get("required_inputs") or []
                    })
            plan = {
                "agents": wf_agents,
                "tasks": wf_tasks
            }

        # 2. Decompose the plan (expand complex tasks) dynamically using Master AI
        if plan:
            decomp_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="🔄 <b>Quality check in progress...</b>\n<i>Alfredo is examining the tasks to decide whether to divide them into more specific and atomic subtasks...</i>",
                parse_mode=ParseMode.HTML
            )
            
            async def typing_indicator():
                while True:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    await asyncio.sleep(4)

            typing_task = asyncio.create_task(typing_indicator())
            
            try:
                # Run decomposer in a separate thread to keep Telegram responsive
                expanded_plan = await asyncio.to_thread(master_ai.decompose_workflow_plan, plan)
                plan = expanded_plan
            except Exception as decomp_err:
                logger.error(f"Decomposition failed: {decomp_err}. Using original plan.")
            finally:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
                try:
                    await decomp_msg.delete()
                except Exception:
                    pass

            final_summary = format_plan_summary(plan)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>Plan Confirmed and Expanded!</b>\n\n{final_summary}",
                parse_mode=ParseMode.HTML
            )
            
            # Save the expanded plan as the final execution plan
            context.user_data["final_plan"] = plan
            if base_workflow:
                context.user_data["current_workflow_id"] = base_workflow["id"]
        else:
            # Fallback (should not happen): run original predefined workflow
            if base_workflow:
                context.user_data["current_workflow_id"] = base_workflow["id"]
                context.user_data["final_plan"] = None

        context.user_data["execution_context"] = {
            "user_input": " ".join([m['content'] for m in chat_history])
        }

        # Transition to input collection before executing
        return await collect_required_inputs(update, context)

    else:
        # Still planning — show draft plan if available
        draft_plan = result.get("plan")
        if draft_plan:
            draft_summary = format_plan_summary(draft_plan)
            await context.bot.send_message(
                chat_id=chat_id, text=draft_summary, parse_mode=ParseMode.HTML
            )

    return PLANNING_MODE


# --- Required Inputs Collection Phase ---

async def collect_required_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Collects all required inputs from the confirmed workflow/plan before execution.
    For predefined workflows: reads required_inputs from DB task records.
    For dynamic plans: reads required_inputs from task objects generated by the LLM.
    Deduplicates by key, then asks the user each question in sequence.
    Returns COLLECTING_INPUTS if questions remain, or ConversationHandler.END after execute_crew.
    """
    chat_id = update.effective_chat.id
    base_workflow = context.user_data.get("base_workflow")
    final_plan = context.user_data.get("final_plan")

    all_inputs = []
    seen_keys = set()

    if final_plan:
        # Dynamic plan: extract required_inputs from each task object
        for task in final_plan.get("tasks", []):
            for ri in (task.get("required_inputs") or []):
                if isinstance(ri, dict):
                    key = ri.get("key")
                    if key and key not in seen_keys:
                        all_inputs.append(ri)
                        seen_keys.add(key)
                elif isinstance(ri, str):
                    key = ri
                    if key and key not in seen_keys:
                        all_inputs.append({"key": key, "prompt": f"Please provide a value for '{key}':"})
                        seen_keys.add(key)
    if base_workflow:
        # Predefined workflow: read required_inputs from DB task records
        task_ids = base_workflow.get('task_ids') or []
        for tid in task_ids:
            t_rec = db.read_task(tid)
            if t_rec:
                for ri in (t_rec.get('required_inputs') or []):
                    if isinstance(ri, dict):
                        key = ri.get("key")
                        if key and key not in seen_keys:
                            all_inputs.append(ri)
                            seen_keys.add(key)
                    elif isinstance(ri, str):
                        key = ri
                        if key and key not in seen_keys:
                            all_inputs.append({"key": key, "prompt": f"Please provide a value for '{key}':"})
                            seen_keys.add(key)

    context.user_data["pending_inputs"] = all_inputs
    context.user_data["collected_inputs"] = {}

    logger.error(f"DEBUG_INPUTS: final_plan_tasks={len(final_plan.get('tasks', [])) if final_plan else 0}, base_workflow={bool(base_workflow)}, all_inputs={all_inputs}")

    if not all_inputs:
        # No inputs needed — proceed directly to execution
        await execute_crew(update, context)
        return ConversationHandler.END

    # Ask the first question
    total = len(all_inputs)
    first = all_inputs[0]
    prompt_text = first.get("prompt") or f"Please provide a value for '{first.get('key')}':"
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📝 <b>Input required (1/{total}):</b>\n\n{prompt_text}",
        parse_mode=ParseMode.HTML
    )
    return COLLECTING_INPUTS


@whitelist_check
async def handle_input_collection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user answers to required input questions, one at a time."""
    user_answer = update.message.text
    chat_id = update.effective_chat.id

    pending = context.user_data.get("pending_inputs", [])
    collected = context.user_data.get("collected_inputs", {})

    if not pending:
        # No pending questions — should not happen, but handle gracefully
        await execute_crew(update, context)
        return ConversationHandler.END

    # Record the answer for the current (first) pending question
    current = pending.pop(0)
    collected[current["key"]] = user_answer
    context.user_data["pending_inputs"] = pending
    context.user_data["collected_inputs"] = collected

    if pending:
        # Ask the next question
        total_done = len(collected)
        total_all = total_done + len(pending)
        next_q = pending[0]
        prompt_text = next_q.get("prompt") or f"Please provide a value for '{next_q.get('key')}':"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📝 <b>Input required ({total_done + 1}/{total_all}):</b>\n\n{prompt_text}",
            parse_mode=ParseMode.HTML
        )
        return COLLECTING_INPUTS
    else:
        # All inputs collected — merge into execution context and start
        exec_ctx = context.user_data.get("execution_context", {})
        exec_ctx.update(collected)
        context.user_data["execution_context"] = exec_ctx

        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ <b>All inputs collected!</b> Starting execution...",
            parse_mode=ParseMode.HTML
        )
        await execute_crew(update, context)
        return ConversationHandler.END


# --- Crew Execution ---

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
        run_id = db.create_run(workflow_id, status='running', inputs=execution_context)

    try:
        from core.crew_builder import (
            build_crew, build_dynamic_crew,
            execute_run_with_resume, execute_dynamic_crew_with_memory
        )

        # Update message to show execution started
        await status_msg.edit_text(
            text="🚀 <b>Execution in progress...</b>\n<i>My agents are working for you (Memory-Centric mode).</i>",
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
            # MEMORY-CENTRIC: Both paths now use task-by-task execution with
            # ephemeral in-memory ChromaDB for inter-agent communication.
            if final_plan:
                logger.info(f"User {user_id}: Kicking off Memory-Centric Dynamic Crew with context: {execution_context}")
                final_result = await asyncio.to_thread(
                    execute_dynamic_crew_with_memory, final_plan, execution_context, None, run_id
                )
            else:
                logger.info(
                    f"User {user_id}: Kicking off Memory-Centric Crew with resume tracking for Run ID {run_id}"
                )
                final_result = await asyncio.to_thread(execute_run_with_resume, run_id)
        except Exception as e:
            logger.error(f"Execution error for User {user_id}: {e}", exc_info=True)
            await status_msg.edit_text(
                text=f"❌ <b>Execution Failed</b>\n<i>An error occurred during workflow execution:</i>\n<code>{e}</code>",
                parse_mode=ParseMode.HTML
            )
            if run_id:
                db.update_run(run_id, status='failed', result=str(e))
            return ConversationHandler.END
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        logger.info(f"User {user_id}: CrewAI execution finished.")

        # --- AUTOMATIC POST-PROCESSING: Master AI Refinement ---
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
            await context.bot.send_message(
                chat_id=chat_id,
                text=final_caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        except Exception as e:
            if "Can't parse entities" in str(e):
                logger.warning(
                    f"Markdown parsing failed for message. Falling back to plain text. Error: {e}"
                )
                fallback_text = f"✅ Execution Complete!\n\n{refined_result}\n\nChoose how to proceed:"
                if len(fallback_text) > 4000:
                    fallback_text = fallback_text[:4000] + "..."
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=fallback_text,
                    reply_markup=reply_markup
                )
            else:
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


# --- Cancellation & Navigation ---

@whitelist_check
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current conversation and offers a restart option."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"User {user_id} canceled the conversation.")

    # Critical: Unblock any pending agent threads!
    if has_pending_request(str(chat_id)):
        provide_human_input(str(chat_id), "SYSTEM_ABORT")
        logger.info(f"Aborted pending human-in-the-loop request for chat {chat_id}.")

    keyboard = [[InlineKeyboardButton("🔄 Restart", callback_data="restart_bot")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⛔ Operation stopped.\n\nPress <b>Restart</b> or use /start to begin a new session.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
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
        await context.bot.send_message(
            chat_id=chat_id, text="Memory cleared. Let's start a new conversation! 🆕"
        )
    elif choice == "context_continue":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Perfect, I will keep the last result in mind for the next workflow! 🔄"
        )


async def handle_restart_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the Restart button shown after cancellation."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await _send_workflow_list(update.effective_chat.id, context.bot)


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
                # "stop" intercept MUST come before the generic text handler
                MessageHandler(
                    filters.Regex(r'(?i)^\s*stop\s*$') & ~filters.COMMAND,
                    cancel_conversation
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_planning_chat),
            ],
            COLLECTING_INPUTS: [
                # Allow stopping even during input collection
                MessageHandler(
                    filters.Regex(r'(?i)^\s*stop\s*$') & ~filters.COMMAND,
                    cancel_conversation
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_collection),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("stop", cancel_conversation),
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    # Global callback handlers (registered after ConversationHandler)
    application.add_handler(CallbackQueryHandler(handle_context_choice, pattern=r"^context_"))
    application.add_handler(CallbackQueryHandler(handle_restart_button, pattern=r"^restart_bot$"))

    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()