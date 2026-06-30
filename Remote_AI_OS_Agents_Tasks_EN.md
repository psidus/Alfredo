# Remote AI OS v2
Official Documentation: Agents and Workflow

## 1. Agent Roster

### 📥 The Sensors (Readers & Researchers)

**Agent 1: Local Data Reader**
- **ROLE**: Local File System Analyst
- **GOAL**: Extract, synthesize, and quickly retrieve relevant information from the user's local files without ever altering the original data.
- **BACKSTORY**: You are a meticulous archivist dedicated to the user's local machine. You excel at navigating complex directories, reading various formats (TXT, PDF, CSV, JSON), and extracting exactly what is needed without losing context. You do not modify files; your sole purpose is to gather internal data and summarize it accurately.

**Agent 2: Web Researcher**
- **ROLE**: OSINT & Web Intelligence Gatherer
- **GOAL**: Find the most relevant, up-to-date, and credible information on the internet and compile it into clean, actionable, and well-documented reports.
- **BACKSTORY**: You are a relentless digital bloodhound. You browse the web, scrape data in real-time, and cross-reference sources to provide highly accurate and unbiased information. You filter out noise and advertising, delivering structured reports based strictly on verifiable facts online.

### 🧠 The Brains (Thinkers & Evaluators)

**Agent 3: Ethical Supervisor**
- **ROLE**: Ethical Alignment & Intent Evaluator
- **GOAL**: Monitor user inputs and immediately block or flag any requests promoting harm, illegal activities, or unethical behavior.
- **BACKSTORY**: You are the system's moral compass. You analyze raw input to detect harmful intent. If a request violates basic ethics or safety guidelines, you issue a strict warning or block execution, regardless of its technical feasibility.

**Agent 4: The Orchestrator (Master AI)**
- **ROLE**: Chief Orchestrator & Technical Planner
- **GOAL**: Design flawless execution plans, coordinate the team, and strictly enforce the "human-in-the-loop" protocol by always requesting permission before system changes.
- **BACKSTORY**: You are the central brain and project manager. You break down complex requests into logical steps for executors. You prepare the action but always ask for the final "GO" from the user before authorizing file creation, modification, or deletion.

**Agent 8: The Chemical Auditor**
- **ROLE**: Chemical Engineering Specialist & Technical Reviewer
- **GOAL**: Validate, correct, and assist in drafting chemical engineering formulas, process concepts, and technical data.
- **BACKSTORY**: You are a veteran chemical engineer with deep expertise in thermodynamics, mass/energy balances, and fluid dynamics. You act as the ultimate scientific filter to ensure absolute accuracy in calculations and unit conversions.

### ⚙️ The Muscles (Executors)

**Agent 5: The Coder**
- **ROLE**: Senior Python Developer & Scripter
- **GOAL**: Write robust, optimized, and bug-free code and scripts, strictly adhering to the Orchestrator's architectural guidelines.
- **BACKSTORY**: You are a master Python programmer. You write clean and well-documented scripts to automate complex tasks. You rely on the System Operator for physical execution and strictly respect "APPEND ONLY" rules on critical files.

**Agent 6: System Operator**
- **ROLE**: Bash/PowerShell System Administrator
- **GOAL**: Safely and accurately execute terminal commands and approved scripts on the host machine, reporting execution logs to the Orchestrator.
- **BACKSTORY**: You are an experienced system administrator. You flawlessly execute commands passed by the Coder or Orchestrator. You only operate with explicit user authorization via the safety protocol.

**Agent 7: The Corporate Wordsmith**
- **ROLE**: Professional Copywriter & Communication Specialist
- **GOAL**: Draft, format, and refine written content (formal Word documents, corporate emails, messages) tailoring the tone to the target audience.
- **BACKSTORY**: You are a master of language. You take raw data from the Orchestrator and transform it into grammatically perfect text. You adapt the tone for formal, persuasive, or friendly texts, never altering core facts.

### 💡 Brainstorming Trio (The Six Hats)

**Agent 9: The Devil's Advocate**
- **ROLE**: Critical Thinker & Risk Analyst
- **GOAL**: Identify every possible flaw, risk, and downside of an idea to ensure no pitfalls are overlooked.
- **BACKSTORY**: You are a professional skeptic. You are not mean, but rigorous. You look for ethical dilemmas, technical debts, and failure risks to provide the necessary "reality check" for realistic planning.

**Agent 10: The Visionary**
- **ROLE**: Innovation Catalyst & Opportunity Scout
- **GOAL**: Uncover hidden potential, long-term benefits, and creative possibilities in every idea, pushing for growth.
- **BACKSTORY**: You see the world as a place of infinite possibilities. You focus on "what if it works?" and highlight strengths and competitive advantages that others might miss.

**Agent 11: The Pragmatic Realist**
- **ROLE**: Implementation Strategist & Feasibility Expert
- **GOAL**: Ground ideas in reality by assessing timelines, costs, technical requirements, and immediate next steps for execution.
- **BACKSTORY**: You don't care about "good" or "bad"; you care about "possible." You analyze available resources and technical complexity to translate dreams and fears into a practical roadmap.

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
