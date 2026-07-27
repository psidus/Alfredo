# core/api_models.py
"""
Pydantic models for the Alfredo Workflow API.
Defines request/response schemas for external app integration.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class TriggerRequest(BaseModel):
    """Request body for triggering a workflow execution."""
    workflow_id: int = Field(..., description="ID of the workflow to execute")
    inputs: Dict[str, Any] = Field(default_factory=dict, description="Input data for the workflow (key-value pairs matching required_inputs)")
    context: Dict[str, Any] = Field(default_factory=dict, description="Optional additional context for the workflow")


class TriggerResponse(BaseModel):
    """Response after successfully triggering a workflow."""
    job_id: int = Field(..., description="Unique ID of the workflow run (for polling)")
    status: str = Field(default="running", description="Initial status of the job")
    message: str = Field(default="Workflow execution started", description="Human-readable confirmation")


class JobStatusResponse(BaseModel):
    """Response for polling a job's current status."""
    job_id: int
    status: str = Field(..., description="'running', 'completed', or 'failed'")
    result: Optional[str] = Field(None, description="Final textual output when status is 'completed'")
    procedure_summary: Optional[str] = Field(None, description="Structured summary of the workflow procedure")
    exports: Optional[List[str]] = Field(None, description="Paths of generated export files")
    handoffs: Optional[List[Dict[str, Any]]] = Field(None, description="Per-node handoff metadata (memory keys / files)")
    progress: Optional[str] = Field(None, description="Current task progress info")
    error: Optional[str] = Field(None, description="Error message if status is 'failed'")


class RequiredInput(BaseModel):
    """Schema for a single required input of a workflow."""
    key: str = Field(..., description="The input variable name")
    prompt: str = Field(default="", description="Human-readable description of what this input expects")


class WorkflowInfo(BaseModel):
    """Public info about a workflow, returned to the SDK."""
    id: int
    name: str
    description: str = Field(default="", description="Workflow description (derived from first task)")
    required_inputs: List[RequiredInput] = Field(default_factory=list, description="Inputs the widget must map to DOM elements")
    has_output: bool = Field(default=True, description="Whether the workflow produces visible output")
    expected_exports: List[str] = Field(default_factory=list, description="Configured export formats for this workflow")


class AppInfo(BaseModel):
    """Public info about a connected app."""
    name: str
    display_name: str
    description: str = ""
    status: str = "active"


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str = "1.1.0"
    api_active: bool = True
