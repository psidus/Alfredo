# Remote AI OS v2
Official Documentation: Agents and Workflow

## 1. The Dynamic Agent Architecture

Unlike traditional setups with dozens of highly specific agents, this AI OS uses a **Dynamic Persona Architecture**. Agents are designed as flexible, generic templates. Their specific expertise is injected dynamically at runtime using the `{specialization}` variable at the Task level. 

Furthermore, the system enforces a strict security protocol: **Regular agents only have read-only tools**, while the **Master AI** (or a designated Executor) is the only entity with access to modification tools (write files, execute commands).

### 🔍 The Observers (Read-Only Tools)

**The Generic Researcher**
- **ROLE**: `{specialization}` Researcher & Data Gatherer
- **GOAL**: Extract, synthesize, and quickly retrieve relevant information regarding `{specialization}` from the web or local files.
- **BACKSTORY**: You are a relentless investigator dedicated to `{specialization}`. You excel at navigating complex directories and scraping the web to compile highly accurate reports. You do not modify anything; your sole purpose is to gather data and summarize it accurately.
- **ALLOWED TOOLS**: `search_web`, `read_file`, `tabular_query` (Read-only access).

**The Generic Analyst (Thinker)**
- **ROLE**: `{specialization}` Analyst & Strategist
- **GOAL**: Analyze collected data and provide critical insights, feasibility checks, or creative solutions focused on `{specialization}`.
- **BACKSTORY**: You are a brilliant thinker specialized in `{specialization}`. You evaluate raw inputs to identify flaws, uncover hidden potential, or ground ideas in reality. You format your findings strictly according to the task's expected output.
- **ALLOWED TOOLS**: Usually none, or limited read-only tools. Rely entirely on input data and context.

**The Generic Wordsmith**
- **ROLE**: Professional Copywriter specialized in `{specialization}`
- **GOAL**: Draft, format, and refine written content tailoring the tone to the target audience.
- **BACKSTORY**: You are a master of language. You take raw data and transform it into grammatically perfect text suitable for `{specialization}` (e.g., corporate emails, technical docs, creative writing).
- **ALLOWED TOOLS**: None.

### ⚙️ The Orchestrator (Write & Execute Tools)

**The Master AI (Orchestrator & Executor)**
- **ROLE**: Chief Orchestrator & System Modifier
- **GOAL**: Design execution plans, synthesize the team's output, and safely execute system modifications or file creations.
- **BACKSTORY**: You are the central brain and project manager. You review the specialized agents' work. You are the **ONLY** agent authorized to use write tools or terminal commands. You break down complex execution steps and strictly enforce safety protocols before altering the local environment.
- **ALLOWED TOOLS**: `write_file`, `terminal_executor`, `app_database_tool` (Full System Access).

### 🧩 Task-Driven Execution
In this architecture, the **Task** is where the magic happens. A Task defines:
1. **The Input Collection**: What variables (like `{user_input}`) or context from previous tasks are passed to the agent.
2. **The Specialization**: Injecting a specific domain (e.g., "Chemical Engineering" or "Startup Risk Analysis") into the generic agent's prompt.
3. **The Exact Output Definition**: Instructing the agent exactly how to format the data (e.g., "A JSON array", "A bulleted list", "A Python script") so the next agent or the Master AI can seamlessly process it.

---

## 2. Tutorial: Creating a Workflow (Startup Ideas Brainstorming)

This section explains how to use the AI OS Dashboard to create your own workflows and assign agents to specific tasks, using the **Startup Ideas Brainstorming** workflow as a practical example.

### How to Create Agents in the Dashboard
Before creating a workflow, you must ensure the required agents exist in your database.
1. Navigate to the **Agent Caserma** section in the UI.
2. Click **Create New Agent**.
3. Fill in the **Role**, **Goal**, and **Backstory** using the templates from the *Agent Roster* above (e.g., create "The Visionary", "The Devil's Advocate", and "The Pragmatic Realist").
4. Select the appropriate Language Model for each agent based on their reasoning needs.
5. Save the agents.

### How to Build the Workflow
Once your agents are ready, you can string them together into a logical sequence using the **Task & Workflow Builder**.

1. Navigate to the **Workflow Builder** tab.
2. Click **Create New Workflow** and name it `Startup Ideas Brainstorming`.
3. In this workflow, we will analyze the user's initial startup idea (passed dynamically as the `{user_input}` variable) from different strategic perspectives.

#### Step 1: Create Task 1 (Analysis of Potential)
- **Task Name**: Analysis of Potential
- **Assigned Agent**: Select **The Visionary** from the dropdown menu.
- **Description**: `Analyze the following user startup idea: {user_input}. Focus exclusively on positive aspects, future scalability, and competitive advantages. What is the best-case scenario? How could it revolutionize the user's workflow?`
- **Expected Output**: `A bulleted list of at least 5 main strengths and 3 long-term opportunities generated by the idea.`
- *Click "Add Task" to append this to the workflow.*

#### Step 2: Create Task 2 (Stress Test & Risk Assessment)
- **Task Name**: Stress Test & Risk Assessment
- **Assigned Agent**: Select **The Devil's Advocate** from the dropdown menu.
- **Description**: `Critically evaluate the following user startup idea: {user_input}. Find every reason it might fail. Consider technical limitations, ethical concerns, cost/benefit ratios, and potential maintenance headaches.`
- **Expected Output**: `A detailed 'Red Flag' report identifying at least 5 critical risks and 3 reasons why the user should proceed with caution or change direction.`
- *Click "Add Task".*

#### Step 3: Create Task 3 (Practical Feasibility Check)
- **Task Name**: Practical Feasibility Check
- **Assigned Agent**: Select **The Pragmatic Realist**.
- **Description**: `Evaluate the execution requirements for: {user_input}. What tools are needed? How long will it take to build an MVP? Is the technology currently available in the local environment?`
- **Expected Output**: `A technical feasibility score (1-10) and a step-by-step 30-day implementation roadmap.`
- *Click "Add Task".*

#### Step 4: Create Task 4 (Final Synthesis)
- **Task Name**: Final Synthesis
- **Assigned Agent**: Select **The Orchestrator (Master AI)**.
- **Description**: `Review the reports generated by the Visionary, Devil's Advocate, and Realist. Balance their views and provide a final executive summary to the user. Should we proceed, modify, or discard the startup idea?`
- **Expected Output**: `A final 'Go/No-Go' recommendation with a summary balancing optimism, caution, and operational reality.`
- *Click "Add Task" and then **Save Workflow**.*

### Running the Workflow
To execute this process:
1. Go to the main execution console or trigger it via the bot interface.
2. Select your new `Startup Ideas Brainstorming` workflow.
3. Provide your startup idea as the `user_input` (e.g., *"A local-first AI operating system that uses small models to securely manage local files without sending data to the cloud"*).
4. The Master AI will dynamically pass this variable to the Visionary, Devil's Advocate, and Realist, and then synthesize their findings into a comprehensive business evaluation.
