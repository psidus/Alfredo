"""
Setup: Topic Radar → Video Brief workflow.

Agents:
  - Dora  → generic {specialization} Researcher (converted in place)
  - Wordsmith → generic copywriter (created if missing)

Differentiation is task-level: agent_specialization + tools per task.
"""
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from core.db_manager import DBManager

WORKFLOW_NAME = "Topic Radar → Video Brief"


def _ph(db):
    """SQL placeholder: %s for Postgres, ? for SQLite."""
    return "%s" if type(db).__name__ == "PostgresManager" else "?"


def _fetchone_id(db, sql, params):
    db.cursor.execute(sql, params)
    row = db.cursor.fetchone()
    if not row:
        return None
    return row["id"] if isinstance(row, dict) else row[0]


def _upsert_task(db, *, name, description, expected_output, agent_id, tools,
                 required_inputs=None, agent_specialization=None, human_validation=False):
    p = _ph(db)
    task_id = _fetchone_id(db, f"SELECT id FROM tasks WHERE name = {p}", (name,))
    kwargs = dict(
        description=description,
        expected_output=expected_output,
        agent_id=agent_id,
        tools=tools,
        required_inputs=required_inputs or [],
        agent_specialization=agent_specialization,
        name=name,
        human_validation=human_validation,
    )
    if task_id:
        db.update_task(task_id, **kwargs)
        print(f"  Updated task '{name}' (ID: {task_id})")
        return task_id
    task_id = db.create_task(**kwargs)
    print(f"  Created task '{name}' (ID: {task_id})")
    return task_id


def setup_topic_radar_workflow():
    db = DBManager()
    print(f"DB backend: {type(db).__name__}")
    if type(db).__name__ != "PostgresManager":
        print(
            "WARNING: writing to SQLite. The Docker dashboard uses Postgres. "
            "Re-run inside the container, e.g.: "
            "docker exec alfredo_dashboard python tools/setup_topic_radar_workflow.py"
        )

    # --- 1. Convert Dora into a general Researcher ---
    dora = db.read_agent_by_name("Dora")
    dora_role = "{specialization} Researcher & Data Gatherer"
    dora_backstory = (
        "You are a relentless investigator dedicated to {specialization}. "
        "You excel at searching the web and compiling accurate, source-backed findings. "
        "You do not invent facts: every claim should be grounded in retrieved results. "
        "You adapt strictly to the specialization and tools assigned by the current task."
    )
    # Keep caserma tools for other workflows; runtime aligns to task tools.
    dora_tools = ["search_web", "vector_search", "read_atomic_memory", "write_atomic_memory"]

    if dora:
        dora_id = dora["id"]
        db.update_agent(
            dora_id,
            name="Dora",
            role=dora_role,
            backstory=dora_backstory,
            model_id=dora.get("model_id"),
            tools=dora_tools,
        )
        print(f"Updated Dora (ID: {dora_id}) → general Researcher")
    else:
        dora_id = db.create_agent(
            name="Dora",
            role=dora_role,
            backstory=dora_backstory,
            model_id=None,
            tools=dora_tools,
        )
        print(f"Created Dora (ID: {dora_id})")
        dora = db.read_agent(dora_id)

    # --- 2. Wordsmith (create or update) ---
    wordsmith = db.read_agent_by_name("Wordsmith")
    ws_role = "Professional Copywriter specialized in {specialization}"
    ws_backstory = (
        "You are a master of language dedicated to {specialization}. "
        "You transform research into clear, engaging content for a non-expert audience. "
        "You follow the task specialization and output format strictly. You do not invent sources."
    )
    ws_model = None
    if wordsmith and wordsmith.get("model_id"):
        ws_model = wordsmith.get("model_id")
    elif dora and dora.get("model_id"):
        ws_model = dora.get("model_id")

    if wordsmith:
        wordsmith_id = wordsmith["id"]
        db.update_agent(
            wordsmith_id,
            name="Wordsmith",
            role=ws_role,
            backstory=ws_backstory,
            model_id=ws_model,
            tools=[],
        )
        print(f"Updated Wordsmith (ID: {wordsmith_id})")
    else:
        wordsmith_id = db.create_agent(
            name="Wordsmith",
            role=ws_role,
            backstory=ws_backstory,
            model_id=ws_model,
            tools=[],
        )
        print(f"Created Wordsmith (ID: {wordsmith_id})")

    # --- 3. Tasks (specialization + tools per task) ---
    print("Upserting tasks...")
    task_1_id = _upsert_task(
        db,
        name="Scan News",
        description=(
            "Research the topic '{topic}'. Use search_web with several distinct queries "
            "focused on the most recent news and articles (prefer 2025–2026), for example: "
            "'{topic} latest news', '{topic} recent developments', '{topic} industry update'. "
            "Collect the strongest sources. For each item include: title, link, short snippet, "
            "and date if available. Do NOT create thematic categories yet — only gather sources."
        ),
        expected_output=(
            "A structured list of recent sources (title, link, snippet, date if known) "
            "relevant to '{topic}', with no thematic clustering."
        ),
        agent_id=dora_id,
        tools=["search_web"],
        required_inputs=[{"key": "topic", "prompt": "Argomento di ricerca (es. idrogeno, energia)"}],
        agent_specialization="Recent news and web article scouting",
        human_validation=False,
    )

    task_2_id = _upsert_task(
        db,
        name="Propose Themes",
        description=(
            "From the research below, propose exactly 5 distinct thematic subcategories "
            "for deeper coverage of the original topic.\n\n"
            "Research material:\n{previous_result}\n\n"
            "Rules: exactly 5 options, numbered 1–5; each option has a short title and one "
            "sentence explaining why it matters now; options must not overlap; do NOT use tools; "
            "do NOT write a long essay."
        ),
        expected_output=(
            "A numbered list of exactly 5 thematic options (1–5). Each line: "
            "'N. Title — one-sentence rationale'."
        ),
        agent_id=dora_id,
        tools=[],
        agent_specialization="Thematic curator for news clustering",
        human_validation=True,
    )

    task_3_id = _upsert_task(
        db,
        name="Deep Brief",
        description=(
            "Produce an up-to-date summary / briefing on the selected theme.\n\n"
            "Selected theme / validation feedback:\n{previous_result}\n\n"
            "Original topic context: '{topic}'.\n"
            "If multiple numbered options appear and no single choice is clear, deep-dive option 1 "
            "and state that assumption. Use search_web for targeted follow-up queries on that theme. "
            "Write a clear update for a professional non-expert: what happened recently, why it matters, "
            "open questions, and cite sources with links."
        ),
        expected_output=(
            "A briefing of ~400–700 words with: headline, key takeaways (bullets), "
            "narrative update, and 3–5 cited sources with links."
        ),
        agent_id=dora_id,
        tools=["search_web"],
        agent_specialization="Deep-dive news reporter and synthesizer",
        human_validation=False,
    )

    task_4_id = _upsert_task(
        db,
        name="Video Outline",
        description=(
            "Using the briefing below, design a divulgative video plan for a general audience.\n\n"
            "Briefing:\n{previous_result}\n\n"
            "Deliver: suggested title; target duration 5–8 minutes; hook for the first 15 seconds; "
            "4–6 timed sections; visual / B-roll ideas per section; one style tip (tone, what to avoid); "
            "optional short ~150-word script for a Reel/Short. Do NOT invent new facts beyond the briefing."
        ),
        expected_output=(
            "A ready-to-shoot video brief: title, duration, hook, timed sections with visuals, "
            "style tip, and optional short script."
        ),
        agent_id=wordsmith_id,
        tools=[],
        agent_specialization="divulgative video scripting and structure",
        human_validation=False,
    )

    # --- 4. Workflow DAG ---
    dag = [
        {"id": "node_0", "task_id": task_1_id, "depends_on": [], "execution_level": 1, "model_tier": "default"},
        {"id": "node_1", "task_id": task_2_id, "depends_on": ["node_0"], "execution_level": 1, "model_tier": "default"},
        {"id": "node_2", "task_id": task_3_id, "depends_on": ["node_1"], "execution_level": 1, "model_tier": "default"},
        {"id": "node_3", "task_id": task_4_id, "depends_on": ["node_2"], "execution_level": 1, "model_tier": "default"},
    ]

    p = _ph(db)
    workflow_id = _fetchone_id(db, f"SELECT id FROM workflows WHERE name = {p}", (WORKFLOW_NAME,))
    if workflow_id:
        db.update_workflow(
            workflow_id,
            WORKFLOW_NAME,
            dag,
            True,
            [],
            "Final outputs are the Deep Brief and Video Outline text.",
        )
        print(f"Updated workflow '{WORKFLOW_NAME}' (ID: {workflow_id})")
    else:
        workflow_id = db.create_workflow(
            name=WORKFLOW_NAME,
            task_ids=dag,
            requires_human_check=True,
            expected_exports=[],
            export_instructions="Final outputs are the Deep Brief and Video Outline text.",
        )
        print(f"Created workflow '{WORKFLOW_NAME}' (ID: {workflow_id})")

    print(f"  Tasks: Scan News={task_1_id}, Propose Themes={task_2_id}, Deep Brief={task_3_id}, Video Outline={task_4_id}")
    print(f"  Agents: Dora={dora_id}, Wordsmith={wordsmith_id}")
    return {
        "workflow_id": workflow_id,
        "dora_id": dora_id,
        "wordsmith_id": wordsmith_id,
        "task_ids": [task_1_id, task_2_id, task_3_id, task_4_id],
    }


if __name__ == "__main__":
    setup_topic_radar_workflow()
