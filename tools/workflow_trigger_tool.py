import logging
import threading
from typing import Optional
from crewai.tools import tool
from core.db_manager import DBManager

def _run_in_background(run_id: int):
    try:
        from core.crew_builder import execute_run_with_resume
        # Small delay to ensure the database transaction is fully committed and the agent finishes writing its output
        import time
        time.sleep(2)
        logging.info(f"Background thread starting execution for Run ID {run_id}")
        result = execute_run_with_resume(run_id)
        
        # Notify completion and mark as completed
        db = DBManager()
        db.update_run(run_id, status='completed', result=str(result))
        run = db.read_run(run_id)
        if run:
            wf_id = run.get('workflow_id')
            wf = db.read_workflow(wf_id)
            wf_name = wf.get('name') if wf else f"Workflow {wf_id}"
            
            from core.notification_manager import NotificationManager
            notifier = NotificationManager()
            notifier.notify_workflow_completion(wf_name, run.get('result', 'Success'))
    except Exception as e:
        logging.error(f"Background execution failed for run {run_id}: {e}")
        try:
            db = DBManager()
            db.update_run(run_id, status='failed', result=str(e))
        except Exception as inner_e:
            logging.error(f"Failed to update run status to failed: {inner_e}")

@tool
def trigger_next_batch(workflow_name: str, new_offset: int, context: Optional[str] = None, document_type: Optional[str] = None) -> str:
    """
    Self-triggers a new background execution of a workflow in Alfredo.
    Use this to continue reading a document from a new offset.
    - workflow_name: the exact name of the workflow (e.g., 'Thermo Data Explorer').
    - new_offset: the offset chunk to start from in the new execution.
    - context: optional string to pass memory/context to the next chunk (e.g. 'Currently reading Viscosity Liquid table').
    - document_type: optional string indicating the global target property type (e.g. 'BIPs', 'eNRTL', 'Pure Components').
    Returns a success message if the run was queued.
    """
    try:
        db = DBManager()
        workflows = db.read_all_workflows()
        
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
        run_inputs = {'offset': new_offset}
        if context:
            run_inputs['context'] = context
        if document_type:
            run_inputs['document_type'] = document_type
            
        run_id = db.create_run(
            workflow_id=workflow_id,
            status='running',
            inputs=run_inputs,
            source='self-trigger'
        )
        
        logging.info(f"Self-triggered next batch for '{workflow_name}' at offset {new_offset}. Run ID: {run_id}")
        
        # Spawn the background thread to actually execute it
        thread = threading.Thread(target=_run_in_background, args=(run_id,), daemon=True)
        thread.start()
        
        return f"SUCCESS. Triggered new execution of '{workflow_name}' (Run ID {run_id}) starting at offset {new_offset}."
        
    except Exception as e:
        logging.error(f"Error triggering next batch: {e}")
        return f"Error triggering next batch: {str(e)}"
