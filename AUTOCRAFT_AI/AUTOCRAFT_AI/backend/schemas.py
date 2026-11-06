"""
Pydantic API Schemas for AUTOCRAFT AI FastAPI Backend.
Compatible with Pydantic v1 and v2.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CreateTaskRequest(BaseModel):
    goal: str = Field(..., description="Natural language goal in English, Tamil, or Thanglish.")
    run_async: bool = Field(
        default=False,
        description="If True, runs the task asynchronously in a background thread.",
    )


class PlanPreviewRequest(BaseModel):
    goal: str = Field(..., description="Natural language goal to parse and plan without executing.")


class ResolveApprovalRequest(BaseModel):
    approved: bool = Field(..., description="True to approve and execute the paused step, False to deny.")
    resolution_note: str = Field(default="", description="Optional operator note.")


class CancelTaskRequest(BaseModel):
    reason: str = Field(default="Cancelled by operator from dashboard.")


class HealthResponse(BaseModel):
    status: str
    app_name: str
    version: str
    platform: str
    database: Dict[str, Any]
    workspace_root: str
    browser_engine: str
    browser_available: bool
    browser_error: Optional[str] = None
    desktop_automation_supported: bool
    desktop_error: Optional[str] = None
    discovered_applications: Dict[str, Any]
    emergency_stop_active: bool
    registered_tool_count: int
