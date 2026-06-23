from crewai import Task
try:
    from .identities import pfc_manager, systems_architect, frontend_dev, backend_dev
except ImportError:
    from agents.identities import pfc_manager, systems_architect, frontend_dev, backend_dev

def create_dynamic_tasks(task_data, overall_plan="", full_prd=""):
    context_prompt = f"\n\n--- GLOBAL PROJECT CONTEXT ---\nOVERALL PLAN:\n{overall_plan}\n\nFULL PRD:\n{full_prd}\n--- END CONTEXT ---\n"

    # Task 1: Planning
    planning_task = Task(
        description=(
            f"Analyze the following task extracted from the PRD: '{task_data['title']}'.\n"
            f"Details: {task_data['description']}\n"
            f"Target file: {task_data['target_file']}\n"
            f"{context_prompt}"
            f"Your objective:\n"
            f"- Produce a clear and structured implementation plan.\n"
            f"- Identify required functions, classes, and components.\n"
            f"- Specify how the code should be organized inside the target file.\n\n"
            f"Constraints:\n"
            f"- DO NOT write final code.\n"
            f"- Focus only on architecture and logic.\n"
        ),
        expected_output="A step-by-step implementation plan describing the structure of the code.",
        agent=pfc_manager
    )

    # Task 2: Architectural Validation
    validation_task = Task(
        description=(
            f"Review the implementation plan created by the Manager for the task: '{task_data['title']}'.\n"
            f"{context_prompt}"
            f"Your objective:\n"
            f"- Validate local file I/O operations (YAML, ENV) for safety.\n"
            f"- Ensure secure Telegram routing and asynchronous logic.\n"
            f"- Verify correct inter-process architecture (Streamlit vs Telegram Bot).\n"
            f"- Detect and fix any logical or structural issues in the plan.\n\n"
            f"If issues are found, correct the plan directly."
        ),
        expected_output="A validated and corrected implementation plan, including security notes and fixes.",
        agent=systems_architect
    )

    # Dynamic executor selection: 'ui/' goes to Frontend, others to Backend
    executor_agent = frontend_dev if "ui/" in task_data['target_file'] else backend_dev

    # Task 3: Execution
    coding_task = Task(
        description=(
            f"Using the validated implementation plan, write the EXACT Python source code.\n"
            f"Task to implement: {task_data['description']}\n"
            f"Target file: {task_data['target_file']}\n"
            f"{context_prompt}"
            f"Strict requirements:\n"
            f"- Output ONLY valid Python code.\n"
            f"- Do NOT include markdown formatting in the final output (unless it's within comments).\n"
            f"- Code must be complete and ready to be saved directly into the target file.\n"
        ),
        expected_output=f"Complete Python source code for the file {task_data['target_file']}.",
        agent=executor_agent
    return [planning_task, validation_task, coding_task]
