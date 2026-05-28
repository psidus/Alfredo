import asyncio
import logging
import os
import sys
import io

# Force UTF-8 encoding for stdout/stderr to prevent CrewAI emoji crashes
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

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
    
    import html as html_module
    def esc(text):
        """Escape HTML entities in dynamic text to prevent Telegram parse errors."""
        return html_module.escape(str(text)) if text else ""

    summary = "<b>📋 Proposed Workflow Plan:</b>\n\n"

    # 1. Expected Exports
    expected_exports = plan.get("expected_exports", [])
    if expected_exports:
        exports_str = ", ".join([esc(x).upper() for x in expected_exports])
        summary += f"<b>📁 Expected Files:</b> {exports_str}\n\n"

    # 2. Agents Section
    summary += "<b>👥 Team Composition:</b>\n"
    agents = plan.get("agents", [])
    if not agents:
        summary += "<i>No agents defined yet.</i>\n"
    for i, agent in enumerate(agents):
        role_str = agent.get('role', '').replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
        goal_str = agent.get('goal', '').replace("{specialization}", "").strip()
        
        # Safeguard any other unreplaced brackets just in case
        import re
        role_str = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[\1]', role_str)
        goal_str = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[\1]', goal_str)
        
        summary += f"{i+1}. <b>{esc(role_str)}</b>\n   🎯 <i>Goal:</i> {esc(goal_str)}\n"

    # 2. Tasks Section
    summary += "\n<b>📝 Execution Steps:</b>\n"
    tasks = plan.get("tasks", [])
    if not tasks:
        summary += "<i>No tasks defined yet.</i>\n"
    for i, task in enumerate(tasks):
        desc = task.get('description', '')
        short_desc = (desc[:120] + '...') if len(desc) > 120 else desc
        
        raw_role = task.get('agent_role', '')
        clean_role = raw_role.replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
        clean_role = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[\1]', clean_role)
        
        assignee_text = esc(clean_role)
        specialization = task.get('agent_specialization')
        if specialization:
            assignee_text += f" <b>{{{esc(specialization)}}}</b>"
            
        summary += f"{i+1}. {esc(short_desc)}\n   👤 <i>Assignee:</i> {assignee_text}\n"
        # Show required inputs that will be collected before execution
        req_inputs = task.get('required_inputs') or []
        if req_inputs:
            keys = []
            for ri in req_inputs:
                if isinstance(ri, dict):
                    keys.append(f"<code>{esc(ri.get('key', '?'))}</code>")
                elif isinstance(ri, str):
                    keys.append(f"<code>{esc(ri)}</code>")
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
    # Clear any stale plan state from a previous session
    context.user_data["final_plan"] = None
    context.user_data["plan_confirmed"] = False
    context.user_data["pending_inputs"] = []
    context.user_data["collected_inputs"] = {}
    context.user_data["execution_context"] = {}
    
    # Wipe the saved JSON context from the database to start completely fresh
    db.update_context(str(update.effective_chat.id), last_output="", accumulated_context="")

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

    past_context_record = db.get_context(str(chat_id))
    accumulated_context = past_context_record.get('accumulated_context') if past_context_record else None

    result = await asyncio.to_thread(master_ai.chat_plan, user_input, saved_context=accumulated_context)

    context.user_data["chat_history"].append({"role": "user", "content": user_input})
    context.user_data["chat_history"].append({"role": "assistant", "content": result["response"]})

    await status_msg.edit_text(result["response"])
    
    if result.get("status") == "export" and accumulated_context:
        plan = result.get("plan", {})
        exports = plan.get("expected_exports", [])
        if exports:
            await status_msg.edit_text(result["response"] + "\n\n<i>Generating requested files...</i>", parse_mode=ParseMode.HTML)
            try:
                export_dir = os.path.join("exports", f"chat_{chat_id}")
                generated_files = await asyncio.to_thread(
                    master_ai.generate_export_files, 
                    accumulated_context,  # final_text fallback
                    exports,
                    export_dir,           # output_dir
                    accumulated_context   # global_context
                )
                for file_path in generated_files:
                    if os.path.exists(file_path):
                        with open(file_path, "rb") as f:
                            await context.bot.send_document(chat_id=chat_id, document=f)
            except Exception as e:
                logger.error(f"Dynamic export failed: {e}")
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error generating files: {e}")
        return PLANNING_MODE

    return PLANNING_MODE


@whitelist_check
async def confirm_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    context.user_data["plan_confirmed"] = True
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.error(f"Failed to remove inline keyboard: {e}")
        
    logger.info("User confirmed plan via inline button. Routing to collect_required_inputs.")
    return await collect_required_inputs(update, context)


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

    # SAFETY NET: If the plan was already confirmed and decomposed, skip re-planning.
    # This catches the case where the LLM asked conversationally for required_inputs
    # instead of returning status "ready", and the user answered that question.
    # In this case, we must go straight to input collection / execution.
    if context.user_data.get("final_plan") and context.user_data.get("plan_confirmed"):
        logger.info("handle_planning_chat: Plan already confirmed. Routing to collect_required_inputs.")
        # Treat user's message as the first input answer if we have pending_inputs
        pending = context.user_data.get("pending_inputs", [])
        if pending:
            # Re-route to input collection handler logic directly
            return await handle_input_collection(update, context)
        # Otherwise just proceed to execution
        await execute_crew(update, context)
        return ConversationHandler.END

    # If we have a decomposed plan waiting for confirmation:
    if context.user_data.get("final_plan") and not context.user_data.get("plan_confirmed"):
        user_input_lower = user_input.strip().lower()
        if user_input_lower in ["go", "confirm", "proceed", "yes", "ok"]:
            context.user_data["plan_confirmed"] = True
            logger.info("handle_planning_chat: User confirmed plan via text. Routing to collect_required_inputs.")
            return await collect_required_inputs(update, context)


    chat_history = context.user_data.get("chat_history", [])
    base_workflow = context.user_data.get("base_workflow")

    status_msg = await update.message.reply_text(
        "🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML
    )

    past_context_record = db.get_context(str(chat_id))
    accumulated_context = past_context_record.get('accumulated_context') if past_context_record else None

    result = await asyncio.to_thread(master_ai.chat_plan, user_input, chat_history, base_workflow, accumulated_context)

    chat_history.append({"role": "user", "content": user_input})
    chat_history.append({"role": "assistant", "content": result["response"]})
    context.user_data["chat_history"] = chat_history

    await status_msg.edit_text(result["response"])

    if result.get("status") == "export" and accumulated_context:
        plan = result.get("plan", {})
        exports = plan.get("expected_exports", [])
        if exports:
            await status_msg.edit_text(result["response"] + "\n\n<i>Generating requested files...</i>", parse_mode=ParseMode.HTML)
            try:
                export_dir = os.path.join("exports", f"chat_{chat_id}")
                generated_files = await asyncio.to_thread(
                    master_ai.generate_export_files, 
                    accumulated_context,  # final_text fallback
                    exports,
                    export_dir,           # output_dir
                    accumulated_context   # global_context
                )
                for file_path in generated_files:
                    if os.path.exists(file_path):
                        with open(file_path, "rb") as f:
                            await context.bot.send_document(chat_id=chat_id, document=f)
            except Exception as e:
                logger.error(f"Dynamic export failed: {e}")
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error generating files: {e}")
        return PLANNING_MODE

    if result.get("status") == "ready":
        plan = result.get("plan")
        is_modified = result.get("modified", True)  # default True for safety
        use_predefined = base_workflow and not is_modified
        logger.info(f"handle_planning_chat: status=READY | is_modified={is_modified} | use_predefined={use_predefined} | plan_is_none={plan is None}")

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
                    try:
                        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    except Exception:
                        pass
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

            # --- Send expanded plan summary (robustly) ---
            # This MUST NOT crash, or collect_required_inputs will never be reached.
            try:
                final_summary = format_plan_summary(plan)
                full_text = f"✅ <b>Plan Expanded!</b>\n\n{final_summary}"
                # Truncate if it exceeds Telegram's 4096 char limit
                if len(full_text) > 4000:
                    full_text = full_text[:3950] + "\n\n<i>... (plan truncated for display)</i>"
                
                keyboard = [[InlineKeyboardButton("✅ Confirm & Proceed", callback_data="confirm_plan")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=full_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            except Exception as summary_err:
                logger.error(f"Failed to send plan summary: {summary_err}. Falling back to plain text.")
                try:
                    # Fallback: send without HTML parse mode
                    task_count = len(plan.get('tasks', []))
                    agent_count = len(plan.get('agents', []))
                    
                    keyboard = [[InlineKeyboardButton("✅ Confirm & Proceed", callback_data="confirm_plan")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ Plan Expanded!\n\n{agent_count} agents, {task_count} tasks ready to execute.\n\nDo you want to proceed or make any changes?",
                        reply_markup=reply_markup
                    )
                except Exception as fallback_err:
                    logger.error(f"Even fallback summary failed: {fallback_err}")
            
            # Save the expanded plan as the final execution plan
            context.user_data["final_plan"] = plan
            context.user_data["plan_confirmed"] = False  # Wait for explicit confirmation
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

        logger.info("handle_planning_chat: Plan decomposed. Waiting for confirmation.")
        # Wait for user confirmation
        return PLANNING_MODE


    else:
        # Still planning — show draft plan if available
        logger.info(f"handle_planning_chat: status=PLANNING. Staying in PLANNING_MODE.")
        draft_plan = result.get("plan")
        if draft_plan:
            try:
                draft_summary = format_plan_summary(draft_plan)
                if len(draft_summary) > 4000:
                    draft_summary = draft_summary[:3950] + "\n\n<i>... (truncated)</i>"
                await context.bot.send_message(
                    chat_id=chat_id, text=draft_summary, parse_mode=ParseMode.HTML
                )
            except Exception as draft_err:
                logger.error(f"Failed to send draft summary: {draft_err}")

    return PLANNING_MODE


# --- Required Inputs Collection Phase ---

async def collect_required_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Collects all required inputs from the confirmed workflow/plan before execution.
    
    SOURCE PRIORITY (to avoid LLM hallucinations on prompts):
    1. If a base_workflow exists: ALWAYS read required_inputs from DB task records (authoritative).
    2. Only if there is NO base_workflow (fully dynamic plan): read from final_plan tasks.
    
    Deduplicates by key, then asks the user each question in sequence.
    Returns COLLECTING_INPUTS if questions remain, or ConversationHandler.END after execute_crew.
    """
    chat_id = update.effective_chat.id
    base_workflow = context.user_data.get("base_workflow")
    final_plan = context.user_data.get("final_plan")

    all_inputs = []
    seen_keys = set()

    if base_workflow:
        # AUTHORITATIVE SOURCE: Predefined workflow — always read from DB task records.
        # This guarantees correct preset prompts are shown regardless of what the LLM generated.
        task_ids = base_workflow.get('task_ids') or []
        logger.info(f"collect_required_inputs: Reading inputs from DB for {len(task_ids)} tasks in workflow '{base_workflow.get('name')}'")
        for tid in task_ids:
            t_rec = db.read_task(tid)
            if t_rec:
                for ri in (t_rec.get('required_inputs') or []):
                    if isinstance(ri, dict):
                        key = ri.get("key")
                        if key and key not in seen_keys:
                            all_inputs.append(ri)
                            seen_keys.add(key)
                            logger.info(f"  Added required input '{key}' from DB task {tid}")
                    elif isinstance(ri, str):
                        key = ri
                        if key and key not in seen_keys:
                            all_inputs.append({"key": key, "prompt": f"Please provide a value for '{key}':"})
                            seen_keys.add(key)
                            logger.info(f"  Added required input '{key}' (string) from DB task {tid}")
    elif final_plan:
        # FALLBACK: Fully dynamic (no predefined workflow) — read from LLM-generated plan.
        logger.info(f"collect_required_inputs: No base_workflow. Reading inputs from dynamic plan ({len(final_plan.get('tasks', []))} tasks).")
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

    context.user_data["pending_inputs"] = all_inputs
    context.user_data["collected_inputs"] = {}

    logger.info(f"collect_required_inputs: {len(all_inputs)} total inputs to collect: {[i['key'] for i in all_inputs]}")

    if not all_inputs:
        # No inputs needed — proceed directly to execution
        logger.info("collect_required_inputs: No inputs needed, proceeding to execution.")
        await execute_crew(update, context)
        return ConversationHandler.END

    # Ask the first question
    total = len(all_inputs)
    first = all_inputs[0]
    prompt_text = first.get("prompt") or f"Please provide a value for '{first.get('key')}':"
    logger.info(f"collect_required_inputs: Asking question 1/{total} for key '{first.get('key')}'. Transitioning to COLLECTING_INPUTS state.")
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

    logger.info(f"handle_input_collection: pending={[p.get('key') for p in pending]}, collected_keys={list(collected.keys())}")

    if not pending:
        # No pending questions — should not happen, but handle gracefully
        logger.warning("handle_input_collection: No pending inputs found, forcing execution.")
        exec_ctx = context.user_data.get("execution_context", {})
        exec_ctx.update(collected)
        context.user_data["execution_context"] = exec_ctx
        await execute_crew(update, context)
        return ConversationHandler.END

    # Record the answer for the current (first) pending question
    current = pending.pop(0)
    collected[current["key"]] = user_answer
    context.user_data["pending_inputs"] = pending
    context.user_data["collected_inputs"] = collected
    logger.info(f"handle_input_collection: Recorded answer for key '{current['key']}'. Remaining: {len(pending)}")

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
        logger.info(f"handle_input_collection: All inputs collected. execution_context keys: {list(exec_ctx.keys())}. Launching execute_crew.")

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

    paused_state = context.user_data.get("dynamic_run_state", {})
    start_idx = paused_state.get("start_idx", 0)
    initial_outputs = paused_state.get("initial_task_outputs", {})
    run_id = paused_state.get("run_id")

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
    if not run_id and workflow_id:
        run_id = db.create_run(workflow_id, status='running', inputs=execution_context)

    # Clear any leftover paused flag so we can succeed properly if it goes through
    if "paused" in context.user_data:
        del context.user_data["paused"]
        
    # Fetch previous context if continuing a conversation
    past_context_record = db.get_context(str(chat_id))
    accumulated_context = past_context_record.get('accumulated_context') if past_context_record else None

    try:
        from core.crew_builder import (
            build_crew, build_dynamic_crew,
            execute_run_with_resume, execute_dynamic_crew_with_memory, RateLimitError
        )

        # Update message to show execution started
        await status_msg.edit_text(
            text="🚀 <b>Execution in progress...</b>\n<i>My agents are working for you (Memory-Centric mode).</i>",
            parse_mode=ParseMode.HTML
        )

        # Inject chat_id for tools
        os.environ["CURRENT_CHAT_ID"] = str(chat_id)

        # Background task to keep the 'typing' indicator alive.
        # Wrapped in try/except to prevent silent death from Telegram API errors.
        async def typing_indicator():
            while True:
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:
                    pass  # Swallow Telegram errors — typing is best-effort
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(typing_indicator())

        # Progress callback: sends a status update to Telegram for each task
        loop = asyncio.get_event_loop()

        def on_task_progress(task_idx: int, total_tasks: int, agent_role: str, status: str = "completed"):
            """Called from the worker thread before/after tasks."""
            try:
                if status == "decomposing":
                    msg = (
                        f"🚀 <b>Execution starting...</b>\n"
                        f"🧠 <i>Master AI is optimizing the workflow plan...</i>"
                    )
                elif status == "running":
                    msg = (
                        f"🚀 <b>Execution in progress...</b>\n"
                        f"<i>✅ Completed {task_idx}/{total_tasks} steps</i>\n"
                        f"⏳ <b>Currently Running:</b> Step {task_idx + 1}\n"
                        f"🤖 Agent: <code>{agent_role[:50]}</code>"
                    )
                else:
                    msg = (
                        f"🚀 <b>Execution in progress...</b>\n"
                        f"<i>✅ Step {task_idx + 1}/{total_tasks} completed</i>\n"
                        f"🤖 Agent: <code>{agent_role[:50]}</code>"
                    )
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(text=msg, parse_mode=ParseMode.HTML),
                    loop
                ).result(timeout=10)
            except Exception:
                pass  # Best-effort — don't crash the worker thread

        try:
            # CRITICAL ARCHITECTURE FIX: Execute CrewAI in a separate thread
            # to prevent blocking the Telegram bot's event loop.
            if final_plan:
                logger.info(f"User {user_id}: Kicking off Memory-Centric Dynamic Crew with context: {execution_context}")
                logger.info(f"User {user_id}: Starting execution of Dynamic Workflow.")
                result_tuple = await asyncio.to_thread(
                    execute_dynamic_crew_with_memory, final_plan, execution_context, None, run_id, start_idx, initial_outputs, accumulated_context, str(chat_id), on_task_progress
                )
            else:
                logger.info(f"User {user_id}: Starting execution of Workflow ID {workflow_id} (resume from {start_idx}).")
                result_tuple = await asyncio.to_thread(execute_run_with_resume, run_id, None, accumulated_context, str(chat_id))

            # Unpack the (last_output, global_context) tuple
            if isinstance(result_tuple, tuple) and len(result_tuple) == 2:
                final_result, global_context = result_tuple
            else:
                # Backward compatibility: if somehow a plain string is returned
                final_result = str(result_tuple)
                global_context = None

            logger.info(f"User {user_id}: CrewAI execution finished.")
        except RateLimitError as rle:
            logger.warning(f"RateLimitError caught: {rle}")
            
            # Save state for dynamic workflows to enable resumption
            context.user_data["paused"] = True
            context.user_data["dynamic_run_state"] = {
                "start_idx": rle.current_task_idx,
                "initial_task_outputs": rle.task_outputs,
                "run_id": run_id
            }
            
            keyboard = [[InlineKeyboardButton("🔄 Try Again (Resume)", callback_data="resume_execution")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await status_msg.edit_text(
                    text=(
                        "⚠️ <b>High Demand Error</b>\n\n"
                        "<i>The AI model is currently experiencing high demand. "
                        "Don't worry, your progress has been safely saved!</i>\n\n"
                        "Please wait a moment, then click below to resume execution from where it paused."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            except Exception as tg_err:
                logger.error(f"Failed to send RateLimit message to Telegram: {tg_err}")
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Execution error for User {user_id}: {e}", exc_info=True)
            try:
                import html
                safe_e = html.escape(str(e))[:500]  # Truncate very long errors
                keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data="resume_execution")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await status_msg.edit_text(
                    text=f"❌ <b>Execution Failed</b>\n<i>An error occurred during workflow execution:</i>\n<code>{safe_e}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            except Exception as inner_e:
                logger.error(f"Failed to send error message to Telegram: {inner_e}")
            if run_id:
                db.update_run(run_id, status='failed', result=str(e))
            return ConversationHandler.END
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


        # --- AUTOMATIC POST-PROCESSING: Master AI Refinement ---
        await status_msg.edit_text(
            text="🧠 <b>Refining output...</b>\n<i>Master AI is polishing the final report.</i>",
            parse_mode=ParseMode.HTML
        )

        try:
            # Pass global context (all agents' outputs) to the refiner so the
            # polished report covers the ENTIRE workflow, not just the last task.
            refiner_input = global_context if global_context else str(final_result)
            refined_result = await asyncio.to_thread(master_ai.refine_output, refiner_input)
            logger.info(f"User {user_id}: Output refinement complete.")
        except Exception as refine_err:
            logger.error(f"Output refinement failed: {refine_err}. Using raw output.")
            refined_result = str(final_result)

        # Update run record with the refined result
        if run_id:
            db.update_run(run_id, status='completed', result=refined_result)

        # Save refined output and global context to memory for continuation
        # NOTE: global_context is ALREADY a JSON string from crew_builder.py,
        # so we must NOT json.dumps() it again (that would double-escape it).
        accumulated_context_str = global_context if isinstance(global_context, str) else ""
        db.update_context(str(chat_id), last_output=refined_result, accumulated_context=accumulated_context_str)

        # Send completion notification
        wf_name = "Dynamic Workflow"
        expected_exports = []
        if workflow_id:
            workflow = db.read_workflow(workflow_id)
            wf_name = workflow["name"] if workflow else "Unknown"
            expected_exports = workflow.get("expected_exports", [])
        elif final_plan:
            expected_exports = final_plan.get("expected_exports", [])

        # Safeguard: ensure expected_exports is always a list, never a bare string
        if isinstance(expected_exports, str):
            try:
                import json as _json
                expected_exports = _json.loads(expected_exports)
                if isinstance(expected_exports, str):
                    expected_exports = [expected_exports]
            except (json.JSONDecodeError, ValueError):
                expected_exports = [expected_exports] if expected_exports else []

        notifier.notify_workflow_completion(wf_name, refined_result, chat_id=chat_id)

        # --- GENERATE PHYSICAL FILES ---
        generated_files = []
        export_instructions = None
        if workflow_id:
            workflow_for_instructions = db.read_workflow(workflow_id)
            if workflow_for_instructions:
                export_instructions = workflow_for_instructions.get("export_instructions", "")
        elif final_plan:
            export_instructions = final_plan.get("export_instructions", "")

        if expected_exports:
            await status_msg.edit_text(
                text="📁 <b>Generating requested files...</b>\n<i>Master AI is building your documents from the global context.</i>",
                parse_mode=ParseMode.HTML
            )
            try:
                export_dir = os.path.join("exports", str(run_id) if run_id else f"chat_{chat_id}")
                generated_files = await asyncio.to_thread(
                    master_ai.generate_export_files,
                    str(final_result),
                    expected_exports,
                    export_dir,
                    global_context,
                    export_instructions
                )
                logger.info(f"Generated {len(generated_files)} files for User {user_id}")
            except Exception as e:
                logger.error(f"File generation failed for User {user_id}: {e}")

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
            # SEND GENERATED DOCUMENTS
            for file_path in generated_files:
                if os.path.exists(file_path):
                    with open(file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f
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
                # SEND GENERATED DOCUMENTS (fallback)
                for file_path in generated_files:
                    if os.path.exists(file_path):
                        with open(file_path, "rb") as f:
                            await context.bot.send_document(
                                chat_id=chat_id,
                                document=f
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
        # Clean up user_data for the next conversation ONLY if not paused
        if not context.user_data.get("paused"):
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


@whitelist_check
async def resume_execution_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Try Again' button to resume a paused execution."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    
    # We just call execute_crew again; it will read 'dynamic_run_state'
    await execute_crew(update, context)


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
                CallbackQueryHandler(confirm_plan_callback, pattern=r"^confirm_plan$"),
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
    application.add_handler(CallbackQueryHandler(resume_execution_callback, pattern=r"^resume_execution$"))

    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()