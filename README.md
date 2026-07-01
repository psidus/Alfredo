# Alfredo: AI OS Multi-Agent Orchestrator

## 🚨 The AI Pain Point
The AI revolution is here, but integrating it into real-world workflows comes with significant challenges:
- **Sky-high Costs:** API calls, external vector databases, and enterprise AI subscriptions add up quickly.
- **Complex Integration:** Gluing together different models, databases, and tools requires building complex and brittle systems from scratch.
- **AI Hallucinations:** Large Language Models often invent credible but entirely false information because they lack grounding in your actual data.
- **Data Privacy Risks:** Sending sensitive corporate data to external APIs is a massive security concern.

## 🦸‍♂️ Enter Alfredo
**Alfredo** is an intelligent, multi-agent orchestrator designed to solve these exact problems. It acts as a bridge between a conversational **Master AI**, specialized task agents, a user-friendly Web UI (**Streamlit Dashboard**), and a background worker (**Telegram Bot**). 

With Alfredo, you have a cohesive operating system to securely and cost-effectively bring AI into your workflow.

### ✨ Core Capabilities
- **RAG Creation (Vectorial & Non-Vectorial):** Ground your AI with your actual data. Build robust Retrieval-Augmented Generation systems using local databases or advanced vector stores to completely eliminate hallucinations.
- **Agent Coordination:** Orchestrate multiple specialized AI agents (powered by CrewAI) that work together seamlessly to solve complex tasks.
- **Tool Connections:** Easily equip your agents with custom tools to interact with APIs, databases, or local files.
- **Local Execution:** Run your models and workflows locally, guaranteeing 100% data privacy and drastically cutting down API costs.
- **Workflow Automation:** Visually build, run, and optimize dynamic workflows directly from the Web UI or trigger them on-the-go via Telegram.
- **API Integration:** Seamlessly integrate Alfredo's capabilities into applications currently under development and maintain them easily via robust APIs.
- **Docker Ready & Desktop Shortcut:** Fully containerized environment with automated dependency setup and a 1-click desktop icon generator for effortless local installation.

---

## 🚀 Quick Start (Docker + Desktop Icon)

The easiest way to get Alfredo up and running on Windows is using **Docker** and the automated **Desktop Shortcut**. All required dependencies, libraries, and tools will be downloaded automatically during the first run.

### 1. Prerequisites
- Install and launch [Docker Desktop](https://www.docker.com/products/docker-desktop/).
- Ensure you have Python installed locally (only needed once to create the shortcut).

### 2. Setup Configuration
1. Clone this repository to your machine:
   ```bash
   git clone <your-repo-url>
   cd Alfredo
   ```
2. Copy `.env.example` to `.env`. **This is crucial for privacy**: `.env` is ignored by git, meaning your API keys will stay on your computer and will not be shared.
   ```bash
   cp .env.example .env
   ```
3. Open the `.env` file and fill in your API keys (e.g., `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`).

### 3. Create the Desktop Shortcut
Run the shortcut creator script to generate a desktop icon:
```bash
python create_docker_shortcut.py
```
This will automatically generate an **`Alfredo (Docker)`** shortcut icon on your Windows Desktop.

### 4. Run Alfredo
- Double-click the **`Alfredo (Docker)`** icon on your Desktop.
- The terminal will launch, download all required dependencies automatically, build the Docker images (on the first run).
- It will automatically populate your local database with a **"Startapp evaluation" example workflow**, start the services in the background, and open your web browser automatically at `http://localhost:8501`.
- To stop the application, return to the opened terminal window and **press any key**. It will cleanly shut down the Docker containers.

---

## 🛠️ Manual Installation (Local Virtual Environment)

If you prefer not to use Docker, you can run Alfredo directly on your machine:

1. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use: venv\Scripts\activate
   ```
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Populate the database with the example workflow (Optional but recommended):
   ```bash
   python seed_startapp_example.py
   ```
4. Run the Streamlit Dashboard:
   ```bash
   streamlit run ui/dashboard.py
   ```
5. Run the Telegram Bot:
   ```bash
   python bot.py
   ```

---

## 🔑 Environment Variables

Alfredo uses a `.env` file for configuration. Here are the core variables:

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | API Key for Google Gemini LLM models. |
| `TELEGRAM_BOT_TOKEN` | Token generated via BotFather on Telegram. |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated list of Telegram user IDs allowed to interact with the bot. |
| `MASTER_AI_MODEL_ID` | Default LLM model identifier for the Master AI. |

---

## ⚖️ License

This project is licensed under the **PolyForm Non-Commercial License 1.0.0**. You are free to view, modify, and distribute this software for personal and non-commercial purposes. Commercial use or exploitation of this software requires prior written permission from the author.
