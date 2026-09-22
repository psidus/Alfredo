import asyncio
import json
import logging
import os
import sys
import io
import time

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

# --- Post-processing watchdog (seconds) ---
# Hard bounds so a workflow cannot sit forever on review or before the final message.
# Inner Master AI calls are shorter; these are the last safety net.
REVIEW_TIMEOUT = int(os.getenv("ALFREDO_REVIEW_TIMEOUT", "60"))
REFINE_TIMEOUT = int(os.getenv("ALFREDO_REFINE_TIMEOUT", "110"))
SUMMARIZE_TIMEOUT = int(os.getenv("ALFREDO_SUMMARIZE_TIMEOUT", "60"))
EXPORT_TIMEOUT = int(os.getenv("ALFREDO_EXPORT_TIMEOUT", "180"))

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
AWAITING_FEEDBACK = 3


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

async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, parse_mode=None, reply_markup=None):
    """Sends a message, splitting it into chunks if it exceeds Telegram's limit, with graceful plain text fallback."""
    import re
    MAX_LEN = 3900
    if len(text) <= MAX_LEN:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except Exception as e:
            if parse_mode:
                logger.warning(f"send_long_message single chunk failed with parse_mode={parse_mode}: {e}. Retrying as plain text.")
                plain_text = re.sub(r'<[^>]+>', '', text)
                if not plain_text.strip():
                    plain_text = text
                await context.bot.send_message(chat_id=chat_id, text=plain_text, parse_mode=None, reply_markup=reply_markup)
                return
            raise

    chunks = []
    current_chunk = ""
    for line in text.split('\n'):
        if len(current_chunk) + len(line) + 1 > MAX_LEN:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line + "\n"
            while len(current_chunk) > MAX_LEN:
                chunks.append(current_chunk[:MAX_LEN])
                current_chunk = current_chunk[MAX_LEN:]
        else:
            current_chunk += line + "\n"
    if current_chunk:
        chunks.append(current_chunk)

    valid_chunks = [c for c in chunks if c.strip()]
    if not valid_chunks:
        return

    for i, chunk in enumerate(valid_chunks):
        markup = reply_markup if i == len(valid_chunks) - 1 else None
        chunk_text = chunk.strip()
        try:
            await context.bot.send_message(chat_id=chat_id, text=chunk_text, parse_mode=parse_mode, reply_markup=markup)
        except Exception as e:
            logger.warning(f"Failed to send chunk with parse_mode {parse_mode}. Falling back to plain text. Error: {e}")
            plain_chunk = re.sub(r'<[^>]+>', '', chunk_text)
            if not plain_chunk.strip():
                plain_chunk = chunk_text
            await context.bot.send_message(chat_id=chat_id, text=plain_chunk, parse_mode=None, reply_markup=markup)

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


_WORKFLOW_MENU_PHRASES = (
    "show workflows",
    "show me the workflows",
    "show me available workflows",
    "show available workflows",
    "show me the available workflows",
    "show saved workflows",
    "show me saved workflows",
    "list workflows",
    "list the workflows",
    "list available workflows",
    "available workflows",
    "saved workflows",
    "workflow list",
    "workflows list",
    "show me the list of workflows",
    "mostrami i workflow",
    "mostrami i workflows",
    "mostra i workflow",
    "mostra i workflows",
    "mostrami i workflow disponibili",
    "mostra i workflow disponibili",
    "workflow disponibili",
    "workflows disponibili",
    "elenco workflow",
    "elenco dei workflow",
    "lista workflow",
    "lista dei workflow",
    "workflow salvati",
    "workflows salvati",
)


def _wants_workflow_menu(text: str) -> bool:
    """True when the user is asking to see the saved-workflow start menu."""
    if not text:
        return False
    lowered = text.strip().lower()
    return any(phrase in lowered for phrase in _WORKFLOW_MENU_PHRASES)


_CONFIRM_WORDS = frozenset({
    "go", "confirm", "proceed", "yes", "ok", "y",
    "procedi", "confermo", "vai", "si", "sì", "okey", "okay",
})

_READY_RESPONSE_PHRASES = (
    "ready to proceed",
    "i am ready",
    "i'm ready",
    "will prompt",
    "system will prompt",
    "prompt for",
)


def _is_user_confirmation(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower() in _CONFIRM_WORDS


_CUSTOMIZE_HINTS = (
    "change the", "change this", "change that", "change agent", "change task",
    "add a ", "add an ", "add task", "add step", "add agent",
    "remove ", "modify ", "customise ", "customize ",
    "instead of", "replace ",
    "aggiungi ", "modifica ", "cambia ", "togli ", "rimuovi ",
)


def _history_requests_customization(chat_history: list, latest_user_input: str = "") -> bool:
    """True if the user asked to customize the loaded workflow (not just confirm it)."""
    texts = []
    for m in chat_history or []:
        if m.get("role") == "user":
            texts.append(m.get("content") or "")
    if latest_user_input:
        texts.append(latest_user_input)
    for t in texts:
        lowered = t.strip().lower()
        if not lowered or _is_user_confirmation(lowered):
            continue
        if any(h in lowered for h in _CUSTOMIZE_HINTS):
            return True
    return False


def _normalize_planner_result(
    result: dict,
    user_input: str = "",
    base_workflow: dict = None,
    chat_history: list = None,
) -> dict:
    """
    Normalize Master AI planner output and recover from common LLM mistakes.

    LLMs often write "I am ready... the system will prompt shortly" while leaving
    status="planning", which leaves the bot stuck and never asks for required_inputs.
    """
    if not isinstance(result, dict):
        return {"status": "planning", "response": str(result), "plan": None, "modified": True}

    status = str(result.get("status") or "planning").strip().lower()
    result["status"] = status

    if "response" not in result or result.get("response") is None:
        result["response"] = "I'm processing the plan..."

    if "modified" not in result:
        result["modified"] = True if not base_workflow else False

    user_confirmed = _is_user_confirmation(user_input)
    response_l = str(result.get("response") or "").lower()
    looks_ready = any(p in response_l for p in _READY_RESPONSE_PHRASES)
    wants_custom = _history_requests_customization(chat_history, user_input)

    # Predefined workflow + user said go / response claims readiness → force ready
    if status == "planning" and base_workflow and (user_confirmed or looks_ready):
        logger.warning(
            "Planner returned status=planning but readiness was detected "
            f"(user_confirmed={user_confirmed}, looks_ready={looks_ready}). Forcing status=ready."
        )
        result["status"] = "ready"
        status = "ready"

    # Run predefined workflows as-is unless the user asked for customization.
    # Prevents a false modified=true from sending the bot into a long decompose hang
    # after the "ready / will prompt shortly" message.
    if base_workflow and status == "ready" and not wants_custom and (user_confirmed or looks_ready):
        if result.get("modified"):
            logger.warning(
                "Planner marked modified=true on a plain confirmation with no customization "
                "requests. Forcing modified=false to collect inputs immediately."
            )
        result["modified"] = False

    return result


def _build_plan_from_workflow(base_workflow: dict) -> dict:
    """Build an execution plan representation from DB workflow/task records."""
    task_ids = base_workflow.get("task_ids") or []
    wf_tasks = []
    wf_agents = []
    seen_agent_roles = set()

    def _build_task_rep(step):
        if isinstance(step, int):
            t_id = step
            dag_props = {"id": f"node_{t_id}", "execution_level": 1, "depends_on": []}
        elif isinstance(step, dict) and step.get("type") == "batch_loop":
            tasks_rep = []
            for inner_step in step.get("task_ids", []):
                rep = _build_task_rep(inner_step)
                if rep:
                    tasks_rep.append(rep)
            return {
                "type": "batch_loop",
                "id": step.get("id"),
                "execution_level": step.get("execution_level", 1),
                "depends_on": step.get("depends_on", []),
                "batch_size": step.get("batch_size"),
                "source_variable": step.get("source_variable"),
                "tasks": tasks_rep,
            }
        else:
            t_id = step.get("task_id")
            dag_props = {
                "id": step.get("id"),
                "execution_level": step.get("execution_level", 1),
                "depends_on": step.get("depends_on", []),
            }

        if t_id is None:
            return None
        t_rec = db.read_task(int(t_id))
        if not t_rec:
            return None

        a_rec = db.read_agent(t_rec["agent_id"]) if t_rec.get("agent_id") else None
        agent_role = "Unknown Agent"
        if a_rec:
            agent_role = a_rec.get("role") or agent_role
            if agent_role not in seen_agent_roles:
                wf_agents.append({
                    "role": a_rec.get("role"),
                    "goal": a_rec.get("goal"),
                    "backstory": a_rec.get("backstory"),
                    "tools": a_rec.get("tools") or [],
                    "model_id": a_rec.get("model_id"),
                })
                seen_agent_roles.add(agent_role)

        desc = t_rec.get("description") or ""
        model_id = t_rec.get("model_id") or (a_rec.get("model_id") if a_rec else None)
        return {
            **dag_props,
            "name": t_rec.get("name") or (desc[:30] if desc else f"Task {t_id}"),
            "description": desc,
            "expected_output": t_rec.get("expected_output"),
            "agent_role": agent_role,
            "agent_specialization": t_rec.get("agent_specialization"),
            "required_inputs": t_rec.get("required_inputs") or [],
            "tools": t_rec.get("tools") or [],
            "vector_dbs": t_rec.get("vector_dbs") or [],
            "human_validation": bool(t_rec.get("human_validation")),
            "model_id": model_id,
            "db_task_id": t_rec.get("id"),
        }

    for step in task_ids:
        rep = _build_task_rep(step)
        if rep:
            wf_tasks.append(rep)

    return {"agents": wf_agents, "tasks": wf_tasks}


async def _present_workflow_menu(chat_id: int, bot, user_data: dict) -> int:
    """Show the /start dual-mode menu and end the current conversation."""
    user_data.clear()
    await _send_workflow_list(chat_id, bot)
    return ConversationHandler.END


def _resolve_db_placeholders(text: str, task_record: dict) -> str:
    """
    Resolves DB-set placeholder values in a text string for display purposes.
    Replaces {specialization} with the agent_specialization stored on the task,
    so the bot shows e.g. "specialized in Chemical Engineering" instead of
    "specialized in {specialization}".
    Only resolves values already set in the DB — user-input variables remain as-is.
    """
    if not text:
        return ""
    text_str = str(text)
    specialization = (task_record.get('agent_specialization') or '') if isinstance(task_record, dict) else ''
    if specialization:
        text_str = text_str.replace('{specialization}', str(specialization))
    return text_str


def format_plan_summary(plan: dict, as_html: bool = True) -> str:
    """Formats the JSON plan into a comprehensive, human-readable summary for Telegram showing ALL tasks."""
    if not plan or not isinstance(plan, dict):
        return ""
    
    import html as html_module
    import re

    def esc(text):
        """Escape HTML entities in dynamic text if as_html is True."""
        if text is None:
            return ""
        text_str = str(text)
        if as_html:
            return html_module.escape(text_str)
        return text_str

    b_open, b_close = ("<b>", "</b>") if as_html else ("", "")
    i_open, i_close = ("<i>", "</i>") if as_html else ("", "")
    c_open, c_close = ("<code>", "</code>") if as_html else ("", "")

    summary = f"{b_open}📋 Proposed Workflow Plan:{b_close}\n\n"

    # 1. Expected Exports
    expected_exports = plan.get("expected_exports") or []
    if expected_exports:
        exports_str = ", ".join([esc(x).upper() for x in expected_exports if x])
        if exports_str:
            summary += f"{b_open}📁 Expected Files:{b_close} {exports_str}\n\n"

    # 2. Agents Section
    summary += f"{b_open}👥 Team Composition:{b_close}\n"
    agents = plan.get("agents") or []
    if not agents:
        summary += f"{i_open}No agents defined yet.{i_close}\n"
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue
        raw_role = agent.get('role') or 'Unnamed Agent'
        role_str = str(raw_role).replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
        
        raw_goal = agent.get('goal') or ''
        goal_str = str(raw_goal).replace("{specialization}", "").strip()
        
        # Safeguard any other unreplaced brackets just in case
        role_str = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[\1]', role_str)
        goal_str = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[\1]', goal_str)
        
        summary += f"{i+1}. {b_open}{esc(role_str)}{b_close}\n"
        if goal_str:
            summary += f"   🎯 {i_open}Goal:{i_close} {esc(goal_str)}\n"

    # 3. Tasks Section - Show ALL tasks completely
    tasks = plan.get("tasks") or []
    summary += f"\n{b_open}📝 Execution Steps ({len(tasks)} tasks):{b_close}\n"
    if not tasks:
        summary += f"{i_open}No tasks defined yet.{i_close}\n"
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        task_name = task.get('name') or task.get('task_name') or f"Task {i+1}"
        desc = task.get('description') or ''
        
        raw_role = task.get('agent_role') or ''
        clean_role = str(raw_role).replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
        clean_role = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[\1]', clean_role)
        
        assignee_text = esc(clean_role)
        specialization = task.get('agent_specialization')
        if specialization:
            assignee_text += f" [{esc(specialization)}]"
            
        summary += f"\n{b_open}{i+1}. {esc(task_name)}{b_close}\n"
        if desc and str(desc).strip() != str(task_name).strip():
            summary += f"   📄 {esc(str(desc).strip())}\n"
        if assignee_text:
            summary += f"   👤 {i_open}Assignee:{i_close} {assignee_text}\n"

        tools = task.get('tools') or []
        if tools:
            tools_str = ", ".join([f"{c_open}{esc(t)}{c_close}" for t in tools if t])
            if tools_str:
                summary += f"   🛠 {i_open}Tools:{i_close} {tools_str}\n"

        # Show required inputs that will be collected before execution
        req_inputs = task.get('required_inputs') or []
        if req_inputs:
            keys = []
            for ri in req_inputs:
                if isinstance(ri, dict):
                    keys.append(f"{c_open}{esc(ri.get('key', '?'))}{c_close}")
                elif isinstance(ri, str):
                    keys.append(f"{c_open}{esc(ri)}{c_close}")
            if keys:
                input_keys = ', '.join(keys)
                summary += f"   📋 {i_open}Inputs needed:{i_close} {input_keys}\n"

        if task.get('human_validation'):
            summary += f"   ⚠️ {i_open}Requires Human Approval before proceeding{i_close}\n"

    summary += f"\n{i_open}Do you want to proceed or make any changes?{i_close}"
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
    context.user_data["plan_reviewed"] = False
    context.user_data["pending_inputs"] = []
    context.user_data["collected_inputs"] = {}
    context.user_data["execution_context"] = {}
    context.user_data["current_workflow_id"] = workflow["id"]
    context.user_data["plan_customized"] = False
    
    # Wipe the saved JSON context from the database to start completely fresh
    db.update_context(str(update.effective_chat.id), last_output="", accumulated_context="")

    import html as html_module

    try:
        await query.edit_message_text(
            f"📝 <b>Loading Workflow '{html_module.escape(str(workflow['name']))}'...</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Failed to edit workflow loading message: {e}")

    # Build a natural-language summary directly from DB data (no LLM call here).
    # This guarantees the initial presentation is faithful to the predefined workflow.
    task_ids = workflow.get('task_ids') or []
    task_lines = []
    
    def _format_db_task(task_db_id, prefix=""):
        t_rec = db.read_task(int(task_db_id))
        if not t_rec: return ""
        a_rec = db.read_agent(t_rec['agent_id']) if t_rec.get('agent_id') else None
        agent_role = a_rec.get('role', 'Unknown agent') if a_rec else 'Unknown agent'
        agent_display = html_module.escape(_resolve_db_placeholders(agent_role, t_rec))
        t_name = t_rec.get('name')
        t_desc = t_rec.get('description') or ''
        t_label = html_module.escape(_resolve_db_placeholders(t_name or t_desc, t_rec))
        req_inputs = t_rec.get('required_inputs') or []
        ri_keys = []
        for ri in req_inputs:
            if isinstance(ri, dict):
                ri_keys.append(html_module.escape(str(ri.get('key', '?'))))
            elif ri:
                ri_keys.append(html_module.escape(str(ri)))
        ri_hint = f"\n   📋 <i>Inputs needed: {', '.join(ri_keys)}</i>" if ri_keys else ""
        tools = t_rec.get('tools') or []
        tools_hint = f"\n   🛠 <i>Tools: {html_module.escape(', '.join(str(t) for t in tools))}</i>" if tools else ""
        desc_block = ""
        # Keep intro short: show name + assignee, not the full multi-paragraph description
        # (full text can exceed Telegram limits and break HTML parsing).
        return f"{prefix}<b>{t_label}</b> — <i>{agent_display}</i>{tools_hint}{ri_hint}"

    for i, step in enumerate(task_ids):
        is_batch = isinstance(step, dict) and step.get("type") == "batch_loop"
        if not is_batch:
            tid = step if isinstance(step, int) else step.get("task_id")
            line = _format_db_task(tid, f"{i+1}. ")
            if line: task_lines.append(line)
        else:
            task_lines.append(f"{i+1}. 🔄 <b>Batch Loop</b> (Size: {html_module.escape(str(step.get('batch_size', '?')))})")
            for inner_id in step.get("task_ids", []):
                line = _format_db_task(inner_id, "    ↳ ")
                if line: task_lines.append(line)

    tasks_block = "\n".join(task_lines) if task_lines else "<i>No tasks defined.</i>"
    wf_name_esc = html_module.escape(str(workflow['name']))
    intro = (
        f"📋 <b>Workflow: {wf_name_esc}</b>\n\n"
        f"Here's the predefined plan I'll execute for you:\n\n"
        f"{tasks_block}\n\n"
        f"💬 <i>Reply with your specific inputs or context (e.g. dataset name, objective...), "
        f"or just say <b>\"go\"</b> to have the Master AI review the plan. You can also ask me to customize any step.</i>"
    )

    # Record the intro as the assistant's first message so the conversation flows naturally
    context.user_data["chat_history"].append({"role": "assistant", "content": intro})
    keyboard = [[InlineKeyboardButton("🔍 Review plan with Master AI", callback_data="confirm_plan")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=intro,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except Exception as send_err:
        logger.error(f"Failed to send HTML workflow intro: {send_err}. Falling back to plain text.")
        plain = (
            f"Workflow: {workflow['name']}\n\n"
            "Say \"go\" or tap Review to let the Master AI examine the plan before you approve it."
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=plain,
            reply_markup=reply_markup,
        )

    # Pre-build the DB plan; Master AI review runs on go / Review button (before approval).
    try:
        context.user_data["final_plan"] = _build_plan_from_workflow(workflow)
        context.user_data["plan_confirmed"] = False
        context.user_data["plan_reviewed"] = False
    except Exception as build_err:
        logger.error(f"Failed to pre-build plan on workflow selection: {build_err}")
        context.user_data["final_plan"] = None
        context.user_data["plan_reviewed"] = False

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

    if _wants_workflow_menu(user_input):
        return await _present_workflow_menu(chat_id, context.bot, context.user_data)

    status_msg = await update.message.reply_text(
        "🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML
    )

    context.user_data["base_workflow"] = None
    context.user_data["chat_history"] = []
    context.user_data["plan_customized"] = True

    past_context_record = db.get_context(str(chat_id))
    accumulated_context = past_context_record.get('accumulated_context') if past_context_record else None

    result = await asyncio.to_thread(master_ai.chat_plan, user_input, saved_context=accumulated_context)
    result = _normalize_planner_result(result, user_input=user_input, base_workflow=None, chat_history=[])

    if result.get("status") == "show_workflows":
        try:
            await status_msg.delete()
        except Exception:
            pass
        return await _present_workflow_menu(chat_id, context.bot, context.user_data)

    context.user_data["chat_history"].append({"role": "user", "content": user_input})
    context.user_data["chat_history"].append({"role": "assistant", "content": result["response"]})

    try:
        await status_msg.edit_text(result["response"])
    except Exception as edit_err:
        logger.error(f"Failed to edit free-chat status message: {edit_err}")
        await context.bot.send_message(chat_id=chat_id, text=result["response"])

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

    # Free-chat ready plan: Master AI review, then user approval, then inputs
    if result.get("status") == "ready" and result.get("plan"):
        context.user_data["execution_context"] = {"user_input": user_input}
        return await review_and_present_plan(update, context, plan=result["plan"])

    return PLANNING_MODE


async def review_and_present_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: dict = None) -> int:
    """
    Master AI quality-check on a local Ollama model, then show the reviewed plan.
    Always a single pass (no Gemini). Required inputs are collected after approval.
    """
    chat_id = update.effective_chat.id
    base_workflow = context.user_data.get("base_workflow")
    if not plan:
        plan = context.user_data.get("final_plan")
    if not plan and base_workflow:
        try:
            plan = _build_plan_from_workflow(base_workflow)
        except Exception as build_err:
            logger.error(f"review_and_present_plan: failed to build plan: {build_err}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Could not prepare the workflow plan: {build_err}\nPlease try again or type /start.",
            )
            return PLANNING_MODE

    if not plan:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ No plan is available to review. Please type /start and select a workflow.",
        )
        return PLANNING_MODE

    if base_workflow:
        context.user_data["current_workflow_id"] = base_workflow["id"]

    original_n = len(plan.get("tasks") or [])
    model_label = f"{getattr(master_ai, 'model_provider', '?')}/{getattr(master_ai, 'model_name', '?')}"
    is_local = bool(getattr(master_ai, "is_local", False)) or str(
        getattr(master_ai, "model_provider", "")
    ).lower() in ("ollama", "lmstudio", "lm_studio", "bionic", "vllm", "llama.cpp")

    if not is_local:
        logger.error(f"Master AI review refused: model is not local ({model_label}).")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Master AI is set to <code>{model_label}</code>, which is not a local model. "
                "Showing the original plan. "
                "Set MASTER_AI_MODEL_NAME to an Ollama model and restart the bot."
            ),
            parse_mode=ParseMode.HTML,
        )
        context.user_data["final_plan"] = plan
        context.user_data["plan_reviewed"] = True
        context.user_data["plan_confirmed"] = False
        context.user_data["review_model"] = model_label
        keyboard = [[InlineKeyboardButton("✅ Approve original plan", callback_data="confirm_plan")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            summary = format_plan_summary(plan, as_html=True)
            await send_long_message(
                context=context,
                chat_id=chat_id,
                text=f"📋 <b>Original plan (review skipped)</b>\n\n{summary}",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Original plan. Click below to approve.",
                reply_markup=reply_markup,
            )
        return PLANNING_MODE

    decomp_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔄 <b>Master AI is reviewing the plan...</b>\n"
            f"🧠 Model: <code>{model_label}</code>"
        ),
        parse_mode=ParseMode.HTML,
    )

    async def typing_indicator():
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(typing_indicator())
    context.user_data["typing_task"] = typing_task
    try:
        keep_saved_plan = not context.user_data.get("plan_customized")
        reviewed = await asyncio.wait_for(
            asyncio.to_thread(master_ai.review_workflow_plan, plan, keep_saved_plan),
            timeout=REVIEW_TIMEOUT,
        )
        if reviewed:
            plan = reviewed
        new_n = len(plan.get("tasks") or [])
        if new_n != original_n:
            context.user_data["plan_customized"] = True
            logger.info(f"Review expanded plan {original_n} → {new_n} tasks; execution will use reviewed plan.")
    except Exception as decomp_err:
        logger.error(f"Plan review failed: {decomp_err}. Using original plan.")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Master AI review had an issue. Showing the original plan so you can still approve it.",
        )
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

    context.user_data["review_model"] = model_label

    keyboard = [[InlineKeyboardButton("✅ Approve reviewed plan", callback_data="confirm_plan")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    summary_sent = False
    n_final = len(plan.get("tasks") or [])
    review_header = (
        f"✅ <b>Master AI review complete</b>\n"
        f"🧠 Model: <code>{model_label}</code>\n"
        f"📝 Execution steps: <b>{n_final}</b>\n\n"
    )

    try:
        final_summary = format_plan_summary(plan, as_html=True)
        full_text = f"{review_header}{final_summary}"
        await send_long_message(
            context=context,
            chat_id=chat_id,
            text=full_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        summary_sent = True
    except Exception as summary_err:
        logger.error(f"Failed to send HTML plan summary: {summary_err}. Falling back to full plain text.")
        try:
            plain_summary = format_plan_summary(plan, as_html=False)
            fallback_text = (
                f"✅ Master AI review complete\n"
                f"Model: {model_label}\n"
                f"Execution steps: {n_final}\n\n{plain_summary}"
            )
            await send_long_message(
                context=context,
                chat_id=chat_id,
                text=fallback_text,
                parse_mode=None,
                reply_markup=reply_markup,
            )
            summary_sent = True
        except Exception as fallback_err:
            logger.error(f"Even fallback summary failed: {fallback_err}")

    if not summary_sent:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ <b>Master AI review complete</b>\n\nPlease click below to approve the plan:",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception as failsafe_err:
            logger.error(f"Failsafe plan confirmation delivery failed: {failsafe_err}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Master AI review complete\n\nPlease click below to approve the plan:",
                reply_markup=reply_markup,
            )

    context.user_data["final_plan"] = plan
    context.user_data["plan_reviewed"] = True
    context.user_data["plan_confirmed"] = False
    logger.info("review_and_present_plan: waiting for user approval of reviewed plan.")
    return PLANNING_MODE


@whitelist_check
async def confirm_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.error(f"Failed to remove inline keyboard: {e}")

    # First click after selecting a workflow = Master AI review, not execution.
    if not context.user_data.get("plan_reviewed"):
        if not context.user_data.get("final_plan"):
            base_workflow = context.user_data.get("base_workflow")
            if base_workflow:
                try:
                    context.user_data["final_plan"] = _build_plan_from_workflow(base_workflow)
                    context.user_data["current_workflow_id"] = base_workflow["id"]
                except Exception as build_err:
                    logger.error(f"confirm_plan_callback: failed to build plan: {build_err}")
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"⚠️ Could not start the workflow: {build_err}\nPlease type /start and try again.",
                    )
                    return PLANNING_MODE
        logger.info("User requested plan review via inline button.")
        return await review_and_present_plan(update, context)

    context.user_data["plan_confirmed"] = True
    seed = context.user_data.pop("seed_input_answer", None)
    logger.info("User approved reviewed plan via inline button. Routing to collect_required_inputs.")
    return await collect_required_inputs(update, context, seed_answer=seed)


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
        context.user_data['execution_task'] = asyncio.create_task(execute_crew(update, context))
        return ConversationHandler.END

    if _wants_workflow_menu(user_input) and not context.user_data.get("base_workflow"):
        return await _present_workflow_menu(chat_id, context.bot, context.user_data)

    hist_for_custom = context.user_data.get("chat_history") or []
    if context.user_data.get("base_workflow") and _history_requests_customization(hist_for_custom, user_input):
        if not _is_user_confirmation(user_input):
            context.user_data["plan_customized"] = True
            logger.info("handle_planning_chat: User requested customization — will use dynamic plan execution.")

    # If we have a plan waiting for Master AI review or user approval:
    if context.user_data.get("final_plan") and not context.user_data.get("plan_confirmed"):
        if _is_user_confirmation(user_input):
            if not context.user_data.get("plan_reviewed"):
                logger.info("handle_planning_chat: User said go — running Master AI plan review.")
                return await review_and_present_plan(update, context)
            context.user_data["plan_confirmed"] = True
            seed = context.user_data.pop("seed_input_answer", None)
            logger.info("handle_planning_chat: User approved reviewed plan via text.")
            return await collect_required_inputs(update, context, seed_answer=seed)

        # Topic typed before review: save it, then run Master AI review first.
        base_wf = context.user_data.get("base_workflow")
        hist = context.user_data.get("chat_history") or []
        if (
            base_wf
            and not context.user_data.get("plan_reviewed")
            and not _history_requests_customization(hist, user_input)
        ):
            context.user_data["seed_input_answer"] = user_input
            context.user_data["execution_context"] = {"user_input": user_input}
            logger.info(
                "handle_planning_chat: Saving user text as future input, running Master AI review first."
            )
            return await review_and_present_plan(update, context)


    chat_history = context.user_data.get("chat_history", [])
    base_workflow = context.user_data.get("base_workflow")

    status_msg = await update.message.reply_text(
        "🔎 <i>Alfredo is thinking...</i>", parse_mode=ParseMode.HTML
    )

    past_context_record = db.get_context(str(chat_id))
    accumulated_context = past_context_record.get('accumulated_context') if past_context_record else None

    result = await asyncio.to_thread(master_ai.chat_plan, user_input, chat_history, base_workflow, accumulated_context)
    result = _normalize_planner_result(
        result,
        user_input=user_input,
        base_workflow=base_workflow,
        chat_history=chat_history,
    )

    if result.get("status") == "show_workflows":
        try:
            await status_msg.delete()
        except Exception:
            pass
        return await _present_workflow_menu(chat_id, context.bot, context.user_data)

    chat_history.append({"role": "user", "content": user_input})
    chat_history.append({"role": "assistant", "content": result["response"]})
    context.user_data["chat_history"] = chat_history

    try:
        await status_msg.edit_text(result["response"])
    except Exception as edit_err:
        logger.error(f"Failed to edit planning status message: {edit_err}")
        await context.bot.send_message(chat_id=chat_id, text=result["response"])

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
        is_modified = bool(result.get("modified", True))
        use_predefined = bool(base_workflow) and not is_modified
        logger.info(
            f"handle_planning_chat: status=READY | is_modified={is_modified} | "
            f"use_predefined={use_predefined} | plan_is_none={plan is None}"
        )

        user_msgs = [
            m["content"] for m in chat_history
            if m.get("role") == "user" and not _is_user_confirmation(m.get("content", ""))
        ]
        context.user_data["execution_context"] = {
            "user_input": "\n".join(user_msgs)
        }

        if use_predefined or not plan:
            try:
                plan = _build_plan_from_workflow(base_workflow) if base_workflow else plan
            except Exception as build_err:
                logger.error(f"Failed to build plan from predefined workflow: {build_err}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Could not prepare the workflow plan: {build_err}\nPlease try again or type /start."
                )
                return PLANNING_MODE

        if plan:
            return await review_and_present_plan(update, context, plan=plan)

        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ The plan was marked ready but no tasks were found. Please type /start and try again."
        )
        return PLANNING_MODE


    else:
        # Still planning — show draft plan if available
        logger.info(f"handle_planning_chat: status=PLANNING. Staying in PLANNING_MODE.")
        draft_plan = result.get("plan")
        if draft_plan:
            try:
                draft_summary = format_plan_summary(draft_plan, as_html=True)
                await send_long_message(
                    context=context, chat_id=chat_id, text=draft_summary, parse_mode=ParseMode.HTML
                )
            except Exception as draft_err:
                logger.error(f"Failed to send HTML draft summary: {draft_err}. Falling back to plain text.")
                try:
                    plain_draft = format_plan_summary(draft_plan, as_html=False)
                    await send_long_message(
                        context=context, chat_id=chat_id, text=plain_draft, parse_mode=None
                    )
                except Exception as fallback_draft_err:
                    logger.error(f"Failed to send plain draft summary: {fallback_draft_err}")

    return PLANNING_MODE


# --- Required Inputs Collection Phase ---

async def collect_required_inputs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    seed_answer: str = None,
) -> int:
    """
    Collects all required inputs from the confirmed workflow/plan before execution.
    
    SOURCE PRIORITY (to avoid LLM hallucinations on prompts):
    1. If a base_workflow exists: ALWAYS read required_inputs from DB task records (authoritative).
    2. Only if there is NO base_workflow (fully dynamic plan): read from final_plan tasks.
    
    Deduplicates by key, then asks the user each question in sequence.
    If seed_answer is provided and there is at least one required input, apply it as the
    first answer without re-asking that question.
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
        def _extract_tids(steps):
            tids = []
            for step in steps:
                if isinstance(step, int):
                    tids.append(step)
                elif isinstance(step, dict):
                    if step.get("type") == "batch_loop":
                        tids.extend(_extract_tids(step.get("task_ids", [])))
                    elif "task_id" in step:
                        tids.append(step["task_id"])
            return tids
            
        flat_tids = _extract_tids(task_ids)
                
        logger.info(f"collect_required_inputs: Reading inputs from DB for {len(flat_tids)} tasks in workflow '{base_workflow.get('name')}'")
        for tid in flat_tids:
            t_rec = db.read_task(int(tid))
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

    collected = {}
    if seed_answer and all_inputs:
        first = all_inputs.pop(0)
        key = first.get("key")
        collected[key] = seed_answer
        logger.info(f"collect_required_inputs: Seeded first input '{key}' from user message.")

    context.user_data["pending_inputs"] = all_inputs
    context.user_data["collected_inputs"] = collected

    logger.info(f"collect_required_inputs: {len(all_inputs)} remaining inputs: {[i['key'] for i in all_inputs]}")

    if not all_inputs:
        # No inputs needed — proceed directly to execution
        logger.info("collect_required_inputs: No inputs required. Launching execute_crew.")
        exec_ctx = context.user_data.get("execution_context") or {}
        exec_ctx.update(collected)
        context.user_data["execution_context"] = exec_ctx
        context.user_data['execution_task'] = asyncio.create_task(execute_crew(update, context))
        return ConversationHandler.END

    # Ask the next question
    total = len(all_inputs) + len(collected)
    asked_n = len(collected) + 1
    first = all_inputs[0]
    prompt_text = first.get("prompt") or f"Please provide a value for '{first.get('key')}':"
    logger.info(f"collect_required_inputs: Asking question {asked_n}/{total} for key '{first.get('key')}'. Transitioning to COLLECTING_INPUTS state.")
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📝 <b>Input required ({asked_n}/{total}):</b>\n\n{prompt_text}",
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
        context.user_data['execution_task'] = asyncio.create_task(execute_crew(update, context))
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
        context.user_data['execution_task'] = asyncio.create_task(execute_crew(update, context))
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
    plan_reviewed = bool(context.user_data.get("plan_reviewed"))
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
        n_plan = len((final_plan or {}).get("tasks") or [])
        await status_msg.edit_text(
            text=(
                f"🚀 <b>Execution in progress...</b>\n"
                f"<i>Running the reviewed plan ({n_plan or '?'} steps).</i>"
            ),
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
        context.user_data["typing_task"] = typing_task

        # Progress callback: sends a status update to Telegram for each task
        loop = asyncio.get_event_loop()

        def on_task_progress(task_idx: int, total_tasks: int, agent_role: str, status: str = "completed", model_label: str = ""):
            """Called from the worker thread before/after tasks."""
            nonlocal status_msg
            try:
                model_line = f"\n🧠 Model: <code>{model_label}</code>" if model_label else ""
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
                        f"{model_line}"
                    )
                else:
                    msg = (
                        f"🚀 <b>Execution in progress...</b>\n"
                        f"<i>✅ Step {task_idx + 1}/{total_tasks} completed</i>\n"
                        f"🤖 Agent: <code>{agent_role[:50]}</code>"
                        f"{model_line}"
                    )
                
                async def update_status():
                    nonlocal status_msg
                    try:
                        # When a task starts, delete the old status and send a new one 
                        # so it pops to the bottom of the chat (especially after HITL)
                        if status == "running":
                            try:
                                await status_msg.delete()
                            except Exception:
                                pass
                            status_msg = await context.bot.send_message(
                                chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML
                            )
                        else:
                            await status_msg.edit_text(text=msg, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                
                asyncio.run_coroutine_threadsafe(update_status(), loop).result(timeout=10)
            except Exception:
                pass  # Best-effort — don't crash the worker thread

        def on_flight_change(in_flight_list: list):
            """Called from the worker thread when parallel active agents change."""
            nonlocal status_msg
            if not in_flight_list: return
            try:
                if len(in_flight_list) > 1:
                    tasks_str = ", ".join(in_flight_list)
                    msg = f"⏳ <b>In Progress:</b> {len(in_flight_list)} Agents running in parallel ({tasks_str})"
                else:
                    msg = f"⏳ <b>In Progress:</b> Executing {in_flight_list[0]}"
                
                async def update_flight_status():
                    try:
                        await status_msg.edit_text(text=msg, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                
                asyncio.run_coroutine_threadsafe(update_flight_status(), loop).result(timeout=10)
            except Exception:
                pass

        try:
            # CRITICAL ARCHITECTURE FIX: Execute CrewAI in a separate thread
            # to prevent blocking the Telegram bot's event loop.
            # Always execute the reviewed/final plan when it has tasks.
            # The DB DAG is the original (e.g. 2 tasks) and must not override a 4-step review.
            plan_tasks = (final_plan or {}).get("tasks") or []
            if plan_tasks:
                n_steps = len(plan_tasks)
                logger.info(
                    f"User {user_id}: Executing {'reviewed ' if plan_reviewed else ''}plan "
                    f"({n_steps} steps) dynamically — not the original DB DAG."
                )
                result_tuple = await asyncio.to_thread(
                    execute_dynamic_crew_with_memory, final_plan, execution_context, None, run_id, start_idx, initial_outputs, accumulated_context, str(chat_id), on_task_progress, on_flight_change
                )
            elif workflow_id:
                logger.info(f"User {user_id}: Starting execution of Workflow ID {workflow_id} (resume from {start_idx}) on DB task models.")
                result_tuple = await asyncio.to_thread(execute_run_with_resume, run_id, on_task_progress, accumulated_context, str(chat_id), on_flight_change)
            else:
                raise ValueError("No workflow_id or final_plan available for execution.")

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
                if getattr(rle, "is_cloud", False):
                    err_title = "⚠️ <b>Cloud model high demand</b>"
                    err_body = (
                        "<i>A cloud provider (Gemini/Google) returned 503. "
                        "Progress is saved. Retry will not switch models automatically.</i>"
                    )
                else:
                    err_title = "⚠️ <b>Local model busy or timed out</b>"
                    err_body = (
                        "<i>Ollama did not finish this step (busy, timeout, or VRAM). "
                        "Progress is saved. Resume uses the same local model.</i>"
                    )
                await status_msg.edit_text(
                    text=(
                        f"{err_title}\n\n{err_body}\n\n"
                        "Please wait a moment, then click below to resume from where it paused."
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
        # (Typing indicator intentionally kept alive through post-processing;
        # it is cancelled in the outer finally below.)

        # Resolve the per-workflow output-token cap (0 = auto/default handled by MasterAI)
        _max_tokens = 0
        if workflow_id:
            _wf_record = db.read_workflow(workflow_id)
            _max_tokens = int((_wf_record or {}).get("max_tokens") or 0)
        elif final_plan:
            _max_tokens = int(final_plan.get("max_tokens") or 0)

        # --- AUTOMATIC POST-PROCESSING: Master AI Refinement ---
        try:
            await status_msg.edit_text(
                text="🧠 <b>Refining output...</b>\n<i>Master AI is polishing the final report.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass  # status_msg may have been deleted/replaced by on_task_progress

        _refine_start = time.monotonic()
        try:
            # Prefer whichever step actually contains papers. The last step is often
            # only a tool trace, while the search step holds the DOIs.
            from core.master_ai import select_refine_source
            _refiner_input = select_refine_source(
                str(final_result) if final_result else "",
                global_context if isinstance(global_context, str) else None,
            )
            refined_result = await asyncio.wait_for(
                asyncio.to_thread(master_ai.refine_output, _refiner_input, "English", _max_tokens or None),
                timeout=REFINE_TIMEOUT,
            )
            logger.info(f"User {user_id}: Output refinement complete ({time.monotonic() - _refine_start:.1f}s).")
        except asyncio.TimeoutError:
            logger.error(f"User {user_id}: Output refinement timed out after {REFINE_TIMEOUT}s. Using raw output.")
            refined_result = str(final_result)
        except Exception as refine_err:
            logger.error(f"Output refinement failed: {refine_err}. Using raw output.")
            refined_result = str(final_result)

        # Update run record with the refined result (keep structured meta if present)
        if run_id:
            try:
                from core.workflow_contracts import parse_run_result_payload, serialize_run_result_payload, assemble_workflow_result
                existing = db.read_run(run_id)
                _, payload = parse_run_result_payload((existing or {}).get("result"))
                if not payload:
                    try:
                        outs = (existing or {}).get("task_outputs")
                        if isinstance(outs, str):
                            outs = json.loads(outs)
                        if isinstance(outs, dict):
                            payload = outs.get("__workflow_meta__")
                    except Exception:
                        payload = None
                if payload:
                    payload = dict(payload)
                    payload["final_result"] = refined_result
                    db.update_run(run_id, status='completed', result=serialize_run_result_payload(payload))
                else:
                    db.update_run(run_id, status='completed', result=refined_result)
            except Exception:
                db.update_run(run_id, status='completed', result=refined_result)

        # Deliver the report before memory compression and file export.
        # Those extra LLM calls must not keep the chat waiting on the final message.
        wf_name = "Dynamic Workflow"
        expected_exports = []
        if workflow_id:
            workflow = db.read_workflow(workflow_id)
            wf_name = workflow["name"] if workflow else "Unknown"
            expected_exports = workflow.get("expected_exports", [])
        elif final_plan:
            expected_exports = final_plan.get("expected_exports", [])

        context.user_data["last_run_id"] = run_id
        context.user_data["last_workflow_name"] = wf_name

        if isinstance(expected_exports, str):
            try:
                expected_exports = json.loads(expected_exports)
                if isinstance(expected_exports, str):
                    expected_exports = [expected_exports]
            except (json.JSONDecodeError, ValueError):
                expected_exports = [expected_exports] if expected_exports else []

        keyboard = [
            [
                InlineKeyboardButton("🔄 Continue", callback_data="context_continue"),
                InlineKeyboardButton("🆕 Reset", callback_data="context_new"),
                InlineKeyboardButton("📝 Leave Feedback", callback_data="context_feedback")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await status_msg.delete()
        except Exception:
            pass

        report_text = refined_result
        final_caption = f"✅ *Execution Complete!*\n\n{report_text}\n\n_Choose how to proceed:_"
        try:
            await send_long_message(
                context=context,
                chat_id=chat_id,
                text=final_caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(
                f"Markdown parsing/sending failed for final report. Falling back to plain text chunked. Error: {e}"
            )
            fallback_text = f"✅ Execution Complete!\n\n{refined_result}\n\nChoose how to proceed:"
            await send_long_message(
                context=context,
                chat_id=chat_id,
                text=fallback_text,
                parse_mode=None,
                reply_markup=reply_markup
            )

        if notifier.default_chat_id and str(notifier.default_chat_id) != str(chat_id):
            notifier.notify_workflow_completion(wf_name, refined_result)

        _summarize_start = time.monotonic()
        try:
            accumulated_context_str = await asyncio.wait_for(
                asyncio.to_thread(master_ai.summarize_global_context, global_context, _max_tokens or None),
                timeout=SUMMARIZE_TIMEOUT,
            )
            logger.info(f"User {user_id}: Context summarization complete ({time.monotonic() - _summarize_start:.1f}s).")
        except asyncio.TimeoutError:
            logger.error(f"User {user_id}: Context summarization timed out after {SUMMARIZE_TIMEOUT}s. Using raw context.")
            accumulated_context_str = global_context if isinstance(global_context, str) else ""
        except Exception as summarize_err:
            logger.error(f"Context summarization failed: {summarize_err}. Using raw context.")
            accumulated_context_str = global_context if isinstance(global_context, str) else ""

        db.update_context(str(chat_id), last_output=refined_result, accumulated_context=accumulated_context_str)

        generated_files = []
        export_instructions = None
        if workflow_id:
            workflow_for_instructions = db.read_workflow(workflow_id)
            if workflow_for_instructions:
                export_instructions = workflow_for_instructions.get("export_instructions", "")
        elif final_plan:
            export_instructions = final_plan.get("export_instructions", "")

        if expected_exports:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="📁 Generating the requested files...",
                )
            except Exception:
                pass
            try:
                export_dir = os.path.join("exports", str(run_id) if run_id else f"chat_{chat_id}")
                _export_start = time.monotonic()
                generated_files = await asyncio.wait_for(
                    asyncio.to_thread(
                        master_ai.generate_export_files,
                        str(final_result),
                        expected_exports,
                        export_dir,
                        global_context,
                        export_instructions,
                        _max_tokens or None,
                    ),
                    timeout=EXPORT_TIMEOUT,
                )
                logger.info(f"Generated {len(generated_files)} files for User {user_id} ({time.monotonic() - _export_start:.1f}s)")
            except asyncio.TimeoutError:
                logger.error(f"File generation timed out after {EXPORT_TIMEOUT}s.")
            except Exception as e:
                logger.error(f"File generation failed for User {user_id}: {e}")

        for file_path in generated_files:
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f
                        )
                except Exception as doc_err:
                    logger.error(f"Failed to send generated document {file_path}: {doc_err}")

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
        # Stop the typing indicator (kept alive through post-processing).
        typing_task = context.user_data.get("typing_task")
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

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

    # Stop the running CrewAI thread if any
    try:
        from core.crew_builder import abort_crew_execution
        abort_crew_execution(str(chat_id))
        logger.info(f"Aborted running CrewAI execution for chat {chat_id}.")
    except Exception as e:
        logger.error(f"Failed to abort CrewAI execution: {e}")

    # Immediately stop typing indicator if running
    if "typing_task" in context.user_data:
        context.user_data["typing_task"].cancel()
        logger.info(f"Cancelled typing indicator for chat {chat_id}.")

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
    """Handles the user's choice to continue, reset, or leave feedback."""
    query = update.callback_query
    await query.answer()

    choice = query.data
    chat_id = update.effective_chat.id

    if choice == "context_new":
        db.clear_context(str(chat_id))
        context.user_data.clear()
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id, text="Memory cleared. Starting a new session."
        )
        await _send_workflow_list(chat_id, context.bot)
    elif choice == "context_continue":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Perfect, I will keep the last result in mind for the next workflow! 🔄"
        )
    elif choice == "context_feedback":
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "📝 *Leave your feedback below.*\n\n"
                "Tell me what should have been done differently. "
                "For example:\n"
                "- _\"The report should use bullet points, not paragraphs\"_\n"
                "- _\"Use metric units, never imperial\"_\n"
                "- _\"Always include source URLs\"_\n\n"
                "Your feedback will be saved and automatically applied "
                "to future similar workflows.\n\n"
                "Type your feedback now (or /skip to cancel):"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        # Set flag so the next text message is captured as feedback
        context.user_data["awaiting_feedback"] = True


async def handle_feedback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Captures the user's feedback text after they clicked 'Leave Feedback'.
    Saves it to the persistent LearningMemoryManager.
    """
    # Only process if we are actually awaiting feedback
    if not context.user_data.get("awaiting_feedback"):
        return

    feedback_text = update.message.text
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Clear the flag immediately
    context.user_data["awaiting_feedback"] = False

    # Retrieve run metadata saved at the end of execute_crew
    run_id = context.user_data.get("last_run_id")
    workflow_name = context.user_data.get("last_workflow_name", "Unknown")

    # Extract task descriptions and agent roles from the run record
    task_descriptions = []
    agent_roles = []
    if run_id:
        run_record = db.read_run(run_id)
        if run_record:
            # Parse task_outputs if available
            task_outputs = run_record.get("task_outputs")
            if isinstance(task_outputs, str):
                try:
                    task_outputs = json.loads(task_outputs)
                except (json.JSONDecodeError, ValueError):
                    task_outputs = None
            if isinstance(task_outputs, dict):
                for key, val in task_outputs.items():
                    task_descriptions.append(str(val.get("description", key))[:200])
                    agent_roles.append(str(val.get("agent_role", "Unknown")))

    # Fallback: if we couldn't extract task info, use the workflow name
    if not task_descriptions:
        task_descriptions = [f"Workflow execution: {workflow_name}"]
        agent_roles = ["General"]

    # Save to persistent learning memory
    try:
        from core.learning_memory import get_learning_memory
        lm = get_learning_memory()
        success = lm.save_feedback(workflow_name, task_descriptions, agent_roles, feedback_text)
        if success:
            await update.message.reply_text(
                "✅ *Feedback saved!*\n\n"
                "Your feedback has been stored in the learning database and will be "
                "automatically applied to similar tasks in future workflow executions.",
                parse_mode=ParseMode.MARKDOWN
            )
            logger.info(f"User {user_id}: Feedback saved for workflow '{workflow_name}'")
        else:
            await update.message.reply_text(
                "⚠️ Feedback could not be saved. The learning memory may not be initialized."
            )
    except Exception as e:
        logger.error(f"Failed to save user feedback: {e}")
        await update.message.reply_text(
            f"❌ Error saving feedback: {e}"
        )


async def skip_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /skip command to cancel feedback submission."""
    if context.user_data.get("awaiting_feedback"):
        context.user_data["awaiting_feedback"] = False
        await update.message.reply_text("Feedback cancelled. ✅")
    else:
        await update.message.reply_text("Nothing to skip.")


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
    context.user_data['execution_task'] = asyncio.create_task(execute_crew(update, context))


@whitelist_check
async def hitl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline keyboard button clicks for Human-in-the-Loop validation."""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    
    # Prefer index-based resolution (hitl_{idx}) using options stored on the HITL request.
    chosen_option = None
    parts = (query.data or "").split("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        idx = int(parts[1])
        from core.human_in_the_loop import has_pending_request
        from core.db_manager import DBManager
        with DBManager() as _db:
            req = _db.get_hitl_request(str(chat_id))
            options = (req or {}).get("options") or []
            if 0 <= idx < len(options):
                chosen_option = options[idx]

    # Legacy fallback: resolve from button label / truncated callback payload.
    if not chosen_option:
        chosen_option = "Unknown option"
        if query.message.reply_markup and query.message.reply_markup.inline_keyboard:
            for row in query.message.reply_markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data == query.data:
                        chosen_option = btn.text
                        break
        if chosen_option == "Unknown option":
            legacy_parts = query.data.split('_', 2)
            if len(legacy_parts) >= 3:
                chosen_option = legacy_parts[2]
            else:
                chosen_option = query.data[5:]
    
    if has_pending_request(str(chat_id)):
        provide_human_input(str(chat_id), chosen_option)
        # Remove buttons and show what was selected
        try:
            await query.edit_message_text(
                text=f"{query.message.text_html or query.message.text}\n\n✅ <b>You chose:</b> {chosen_option}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>You chose:</b> {chosen_option}",
                parse_mode=ParseMode.HTML,
            )
    else:
        await query.edit_message_reply_markup(reply_markup=None)

# --- Main Function ---

def main() -> None:
    """Start the bot."""
    # --- Launch API Server in background thread ---
    import threading
    api_port = int(os.getenv("ALFREDO_API_PORT", "8000"))
    try:
        from core.api_server import start_api_server
        api_thread = threading.Thread(
            target=start_api_server,
            kwargs={"host": "0.0.0.0", "port": api_port},
            daemon=True
        )
        api_thread.start()
        logger.info(f"🌐 API Server started on http://localhost:{api_port}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to start API server: {e}. Continuing with Telegram bot only.")

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
                # Allow picking another workflow without /cancel first
                CallbackQueryHandler(workflow_selection_callback, pattern=r"^workflow_"),
                CallbackQueryHandler(confirm_plan_callback, pattern=r"^confirm_plan$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_planning_chat),
            ],
            COLLECTING_INPUTS: [
                # Allow stopping even during input collection
                MessageHandler(
                    filters.Regex(r'(?i)^\s*stop\s*$') & ~filters.COMMAND,
                    cancel_conversation
                ),
                # Allow switching workflow mid-input collection
                CallbackQueryHandler(workflow_selection_callback, pattern=r"^workflow_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_collection),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("stop", cancel_conversation),
            # Last-resort: workflow buttons still work if conversation state is stale
            CallbackQueryHandler(workflow_selection_callback, pattern=r"^workflow_"),
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    # Global callback handlers (registered after ConversationHandler)
    application.add_handler(CallbackQueryHandler(handle_context_choice, pattern=r"^context_"))
    application.add_handler(CallbackQueryHandler(handle_restart_button, pattern=r"^restart_bot$"))
    application.add_handler(CallbackQueryHandler(resume_execution_callback, pattern=r"^resume_execution$"))
    application.add_handler(CallbackQueryHandler(hitl_callback, pattern=r"^hitl_"))

    # Feedback text handler — catches free text ONLY when awaiting_feedback flag is set.
    # group=1 ensures it runs alongside (but independently from) the ConversationHandler.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_feedback_text),
        group=1
    )
    application.add_handler(CommandHandler("skip", skip_feedback))

    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()