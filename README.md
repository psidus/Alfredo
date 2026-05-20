# Alfredo: AI OS Multi-Agent Orchestrator

Alfredo is an intelligent, multi-agent orchestrator that connects a conversational **Master AI** and specialized task agents with a user interface (**Streamlit Dashboard**) and a background worker (**Telegram Bot**). It allows you to run, build, and optimize dynamic workflows using Crews (CrewAI) directly from Telegram or the Web UI.

---

## 🚀 Quick Start (Docker + Desktop Icon)

The easiest way to get Alfredo up and running on Windows is using **Docker** and the automated **Desktop Shortcut**.

### 1. Prerequisites
- Install and launch [Docker Desktop](https://www.docker.com/products/docker-desktop/).
- Ensure you have Python installed locally (only needed once to create the shortcut).

### 2. Setup Configuration
1. Clone this repository:
   ```bash
   git clone <your-repo-url>
   cd Alfredo
   ```
2. Copy `.env.example` to `.env` and fill in your API keys (e.g., `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`):
   ```bash
   cp .env.example .env
   ```

### 3. Create the Desktop Shortcut
Run the shortcut creator script:
```bash
python create_docker_shortcut.py
```
This will automatically generate an **`Alfredo (Docker)`** shortcut icon on your Windows Desktop.

### 4. Run Alfredo
- Double-click the **`Alfredo (Docker)`** icon on your Desktop.
- The terminal will launch, build the Docker images (on the first run), start the services in the background, and open your web browser automatically at `http://localhost:8501`.
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
3. Run the Streamlit Dashboard:
   ```bash
   streamlit run ui/dashboard.py
   ```
4. Run the Telegram Bot:
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
