import logging
from typing import Optional
from crewai.tools import tool
from core.db_manager import DBManager

@tool
def trigger_next_batch(workflow_name: str, new_offset: int) -> str:
    """
    Self-triggers a new background execution of a workflow in Alfredo.
    Use this to continue reading a document from a new offset.
    - workflow_name: the exact name of the workflow (e.g., 'Thermo Data Explorer').
    - new_offset: the offset chunk to start from in the new execution.
    Returns a success message if the run was queued.
    """
    try:
        db = DBManager()
        workflows = db.get_workflows()
        
        # Find the workflow ID by name
        target_workflow = None
        for wf in workflows:
            if wf.get("name") == workflow_name:
                target_workflow = wf
                break
                
        if not target_workflow:
            return f"Error: Workflow '{workflow_name}' not found."
            
        workflow_id = target_workflow["id"]
        
        # Queue the new run with the new offset as an input
        run_id = db.create_run(
            workflow_id=workflow_id,
            status='running',
            inputs={'offset': new_offset},
            source='self-trigger'
        )
        
        logging.info(f"Self-triggered next batch for '{workflow_name}' at offset {new_offset}. Run ID: {run_id}")
        return f"SUCCESS. Triggered new execution of '{workflow_name}' (Run ID {run_id}) starting at offset {new_offset}."
        
    except Exception as e:
        logging.error(f"Error triggering next batch: {e}")
        return f"Error triggering next batch: {str(e)}"
