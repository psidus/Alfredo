"""
Setup: Scientific Literature Scout workflow.

Agents:
  - Dora  → General Researcher (specialized in {specialization})
  - Scientific Writer → Academic Writer & Synthesizer (specialized in {specialization})

Pipeline:
  1. Search Scientific Literature (Dora + search_web)
  2. Curate Top 5 Scientific Papers (Scientific Writer + no tools)
"""
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from core.db_manager import DBManager

WORKFLOW_NAME = "Scientific Literature Scout"


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
                 required_inputs=None, agent_specialization=None, human_validation=False, model_id=None):
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
        model_id=model_id,
    )
    if task_id:
        db.update_task(task_id, **kwargs)
        print(f"  [+] Updated task '{name}' (ID: {task_id}, model_id: {model_id})")
        return task_id
    task_id = db.create_task(**kwargs)
    print(f"  [+] Created task '{name}' (ID: {task_id}, model_id: {model_id})")
    return task_id


def setup_scientific_research_workflow():
    db = DBManager()
    print(f"Connecting via: {type(db).__name__}")

    all_models = db.read_all_models()
    # 1. Researcher model (needs rock-solid Function Calling / Tool Calling for search_web)
    # llama3.1:latest is the gold standard in Ollama for tool calling in CrewAI
    tool_models = [
        m for m in all_models
        if m.get("is_local") and "llama3.1" in (m.get("model_name") or "").lower()
    ] or [
        m for m in all_models
        if m.get("is_local") and m.get("supports_tools")
    ]

    # 2. Writer/Curator model (needs superior text comprehension, synthesis & strict Markdown formatting)
    # qwen2.5:14b or qwen3:14b delivers exceptional academic writing and instruction following without tool overhead
    writer_models = [
        m for m in all_models
        if m.get("is_local") and "qwen2.5:14b" in (m.get("model_name") or "").lower()
    ] or [
        m for m in all_models
        if m.get("is_local") and "qwen3:14b" in (m.get("model_name") or "").lower()
    ] or [
        m for m in all_models
        if m.get("is_local") and "qwen" in (m.get("model_name") or "").lower()
    ] or [
        m for m in all_models if m.get("is_local")
    ]

    dora_model_rec = tool_models[0] if tool_models else all_models[0]
    writer_model_rec = writer_models[0] if writer_models else all_models[0]

    dora_model_id = dora_model_rec["id"]
    writer_model_id = writer_model_rec["id"]

    print(f"  -> Task 1 Model (Tool-compatible): {dora_model_rec['provider']} / {dora_model_rec['model_name']} (ID: {dora_model_id}, VRAM: {dora_model_rec.get('vram_gb')}GB)")
    print(f"  -> Task 2 Model (Synthesis/Writer): {writer_model_rec['provider']} / {writer_model_rec['model_name']} (ID: {writer_model_id}, VRAM: {writer_model_rec.get('vram_gb')}GB)")

    # --- 1. Configure Dora as general Researcher ---
    dora = db.read_agent_by_name("Dora")
    dora_role = "{specialization} Researcher & Data Gatherer"
    dora_backstory = (
        "You are a relentless investigator dedicated to {specialization}. "
        "You excel at searching the web and compiling accurate, source-backed findings. "
        "You do not invent facts: every claim should be grounded in retrieved results. "
        "You adapt strictly to the specialization and tools assigned by the current task."
    )
    dora_tools = ["search_scientific_literature", "vector_search", "read_atomic_memory", "write_atomic_memory"]

    if dora:
        dora_id = dora["id"]
        db.update_agent(
            dora_id,
            name="Dora",
            role=dora_role,
            backstory=dora_backstory,
            model_id=dora_model_id,
            tools=dora_tools,
        )
        print(f"  [+] Updated Dora (ID: {dora_id}) -> model: {dora_model_rec['model_name']}")
    else:
        dora_id = db.create_agent(
            name="Dora",
            role=dora_role,
            backstory=dora_backstory,
            model_id=dora_model_id,
            tools=dora_tools,
        )
        print(f"  [+] Created Dora (ID: {dora_id}) -> model: {dora_model_rec['model_name']}")
        dora = db.read_agent(dora_id)

    # --- 2. Configure Scientific Writer ---
    sci_writer = db.read_agent_by_name("Scientific Writer")
    writer_role = "Academic Writer & Literature Synthesizer specialized in {specialization}"
    writer_backstory = (
        "You are a meticulous scientific writer and literature synthesizer specialized in {specialization}. "
        "You analyze dense technical publications, preprints, and experimental findings to produce clear, "
        "structured, and rigorous digests. You preserve verified source URLs and never hallucinate citations or links."
    )

    if sci_writer:
        writer_id = sci_writer["id"]
        db.update_agent(
            writer_id,
            name="Scientific Writer",
            role=writer_role,
            backstory=writer_backstory,
            model_id=writer_model_id,
            tools=[],
        )
        print(f"  [+] Updated Scientific Writer (ID: {writer_id}) -> model: {writer_model_rec['model_name']}")
    else:
        writer_id = db.create_agent(
            name="Scientific Writer",
            role=writer_role,
            backstory=writer_backstory,
            model_id=writer_model_id,
            tools=[],
        )
        print(f"  [+] Created Scientific Writer (ID: {writer_id}) -> model: {writer_model_rec['model_name']}")

    # --- 3. Setup Tasks ---
    print("Setting up tasks...")
    task_1_id = _upsert_task(
        db,
        name="Search Scientific Literature",
        description=(
            "Perform a scientific literature search on the topic: '{topic}'.\n\n"
            "Call search_scientific_literature exactly once with the topic as query and max_results=8. "
            "The tool returns Crossref records: title, authors, journal, year, DOI, URL, and an abstract only when Crossref provides one.\n\n"
            "Copy 6 to 8 papers from that tool result. For each paper write:\n"
            "1. Full Article Title\n"
            "2. Authors and Publication Year\n"
            "3. Journal\n"
            "4. Direct Verified Link (https://doi.org/... or the URL returned by the tool)\n"
            "5. Abstract, only if the tool included one. Otherwise write 'Not stated in the retrieved record'.\n\n"
            "STRICT INTEGRITY RULES:\n"
            "- Only include papers actually returned by the search_scientific_literature tool.\n"
            "- NEVER fabricate, invent, or hallucinate titles, authors, DOIs, URLs, methods, or findings.\n"
            "- If a paper has no accessible link, state 'URL not available'. Never output synthetic placeholder numbers (e.g. '00123' or 'PMC12345678').\n"
            "- Your final message is the paper list itself. Do not describe the tool call."
        ),
        expected_output=(
            "A structured collection of 6-8 retrieved scientific papers, each with exact Title, "
            "Authors, Year, Journal, and verified DOI Link/URL. Abstract only when the tool returned one."
        ),
        agent_id=dora_id,
        tools=["search_scientific_literature"],
        required_inputs=[{
            "key": "topic",
            "prompt": "Scientific research topic or field of study"
        }],
        agent_specialization="Scientific literature researcher and preprint scout",
        human_validation=False,
        model_id=dora_model_id,
    )

    task_2_id = _upsert_task(
        db,
        name="Curate Top 5 Scientific Papers",
        description=(
            "Analyze and synthesize the scientific research material gathered below:\n\n"
            "Research material:\n{previous_result}\n\n"
            "Original research topic: '{topic}'.\n\n"
            "Your task is to select and curate the TOP 5 most relevant, impactful, and rigorous "
            "scientific papers from the collected material, and format them into a clean bibliographic digest.\n\n"
            "Strict Rules:\n"
            "1. Select exactly 5 distinct papers from the gathered research.\n"
            "2. Use ONLY titles, authors, years, journals, and URLs that appear in the research material. Do NOT invent links, methods, or findings.\n"
            "3. Format each of the 5 papers as follows:\n\n"
            "### [Number]. [Article Title]\n"
            "- **Authors / Year**: [from the research material]\n"
            "- **Link / Source**: [Verified URL from research material, or 'URL not available']\n"
            "- **What the record says**: [1-2 sentences using only the title, journal, and abstract if one was retrieved. If the abstract is missing, write 'Not stated in the retrieved record'.]\n\n"
            "4. Conclude with 2-3 sentences on how these 5 titles relate to '{topic}', without adding facts that are not in the records.\n"
            "5. Do NOT output placeholder strings like '[Exact URL]' or tables of Method 1 / Finding 1. If no URL was provided, write 'URL not available'."
        ),
        expected_output=(
            "A clean Markdown document presenting exactly 5 curated scientific papers with verified links "
            "and only the facts present in the retrieved records, followed by a brief synthesis."
        ),
        agent_id=writer_id,
        tools=[],
        agent_specialization="Academic paper curator and synthesis editor",
        human_validation=False,
        model_id=writer_model_id,
    )

    # --- 4. Setup Workflow DAG ---
    dag = [
        {"id": "node_0", "task_id": task_1_id, "depends_on": [], "execution_level": 1, "model_tier": "default"},
        {"id": "node_1", "task_id": task_2_id, "depends_on": ["node_0"], "execution_level": 2, "model_tier": "default"},
    ]

    p = _ph(db)
    workflow_id = _fetchone_id(db, f"SELECT id FROM workflows WHERE name = {p}", (WORKFLOW_NAME,))
    if workflow_id:
        db.update_workflow(
            workflow_id,
            WORKFLOW_NAME,
            dag,
            False,
            [],
            "Final output is the 5-paper curated scientific literature digest in Markdown.",
        )
        print(f"  [+] Updated workflow '{WORKFLOW_NAME}' (ID: {workflow_id})")
    else:
        workflow_id = db.create_workflow(
            name=WORKFLOW_NAME,
            task_ids=dag,
            requires_human_check=False,
            expected_exports=[],
            export_instructions="Final output is the 5-paper curated scientific literature digest in Markdown.",
        )
        print(f"  [+] Created workflow '{WORKFLOW_NAME}' (ID: {workflow_id})")

    print(f"\nSuccessfully configured '{WORKFLOW_NAME}'!")
    print(f"  - Workflow ID: {workflow_id}")
    print(f"  - Dora ID: {dora_id}")
    print(f"  - Scientific Writer ID: {writer_id}")
    print(f"  - Task 1 (Search): {task_1_id}")
    print(f"  - Task 2 (Curate & Synthesize): {task_2_id}")

    return {
        "workflow_id": workflow_id,
        "dora_id": dora_id,
        "writer_id": writer_id,
        "task_ids": [task_1_id, task_2_id],
    }


if __name__ == "__main__":
    setup_scientific_research_workflow()
