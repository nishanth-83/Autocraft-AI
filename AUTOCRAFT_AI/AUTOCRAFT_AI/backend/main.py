"""
FastAPI Backend Application for AUTOCRAFT AI.
Exposes REST API endpoints for:
- Health & diagnostics (/api/health)
- Tool registry metadata (/api/tools)
- Environment observation (/api/observe)
- Deterministic offline plan preview (/api/plan/preview)
- Task creation, execution, listing, inspection, cancellation, and resumption (/api/tasks/*)
- Security approval queue resolution (/api/approvals/*)
- Emergency stop control (/api/emergency-stop, /api/emergency-stop/reset)
- Static serving of the built React + TypeScript Frontend Dashboard (/)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.config import AgentConfig
from backend.database import SQLiteStore
from backend.execution.loop import AutonomousOrchestrator
from backend.planner.engine import PlanningError
from backend.schemas import (
    CancelTaskRequest,
    CreateTaskRequest,
    HealthResponse,
    PlanPreviewRequest,
    ResolveApprovalRequest,
)
from backend.tools.browser import (
    PlaywrightBrowserSession,
    SeleniumChromiumBrowserSession,
    close_browser_session,
    get_browser_session,
    register_browser_tools,
)
from backend.tools.desktop import get_desktop_driver, register_desktop_tools
from backend.tools.filesystem import register_filesystem_tools
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def build_default_registry(config: Optional[AgentConfig] = None) -> ToolRegistry:
    """Create a ToolRegistry and register all filesystem, desktop, and browser tools."""
    registry = ToolRegistry()
    register_filesystem_tools(registry)
    register_desktop_tools(
        registry,
        enabled=config.enable_live_desktop_automation if config is not None else True,
    )
    register_browser_tools(registry)
    return registry


def create_app(config: Optional[AgentConfig] = None) -> FastAPI:
    """Application factory for AUTOCRAFT AI FastAPI server."""
    cfg = config or AgentConfig.from_env()
    store = SQLiteStore(cfg.database_path)
    registry = build_default_registry(cfg)
    orchestrator = AutonomousOrchestrator(config=cfg, store=store, registry=registry)

    app = FastAPI(
        title="AUTOCRAFT AI - Autonomous Desktop Agent API",
        version=cfg.version,
        description="Production-oriented autonomous desktop AI agent with deterministic offline planning, verified execution, and error recovery.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store references on app.state for inspection and testing
    app.state.config = cfg
    app.state.store = store
    app.state.registry = registry
    app.state.orchestrator = orchestrator

    @app.on_event("shutdown")
    def close_browser_sessions() -> None:
        close_browser_session()

    @app.get("/api/health", response_model=HealthResponse)
    def get_health() -> Dict[str, Any]:
        db_health = store.check_health()
        desktop_driver = get_desktop_driver()
        browser_session = get_browser_session(cfg.browser_channel, cfg.browser_headless)
        browser_available = (
            PlaywrightBrowserSession.is_installed()
            or SeleniumChromiumBrowserSession.is_installed()
        )
        browser_error = getattr(browser_session, "availability_error", None)
        desktop_supported = (
            cfg.enable_live_desktop_automation and desktop_driver.is_supported()
        )
        desktop_error: Optional[str] = None
        try:
            discovered_applications = desktop_driver.discover_applications()
        except Exception as exc:
            desktop_supported = False
            desktop_error = f"{type(exc).__name__}: {exc}"
            discovered_applications = {}
            logger.exception("Could not discover desktop applications.")

        if not cfg.enable_live_desktop_automation:
            desktop_error = "Disabled by AUTOCRAFT_ENABLE_DESKTOP."
            discovered_applications = {
                name: {
                    **application,
                    "available": False,
                    "reason": desktop_error,
                    "capabilities": [],
                }
                for name, application in discovered_applications.items()
            }
        elif not desktop_supported and desktop_error is None:
            desktop_error = (
                "Requires an interactive Windows desktop session."
                if sys.platform == "win32"
                else f"Requires Windows (win32); current platform is '{sys.platform}'."
            )

        return {
            "status": (
                "healthy"
                if db_health.get("healthy") and len(registry.list_tools()) > 0
                else "degraded"
            ),
            "app_name": cfg.app_name,
            "version": cfg.version,
            "platform": sys.platform,
            "database": db_health,
            "workspace_root": str(cfg.workspace_root),
            "browser_engine": browser_session.engine_name(),
            "browser_available": browser_available,
            "browser_error": browser_error,
            "desktop_automation_supported": desktop_supported,
            "desktop_error": desktop_error,
            "discovered_applications": discovered_applications,
            "emergency_stop_active": orchestrator.is_cancel_requested("__health__"),
            "registered_tool_count": len(registry.list_tools()),
        }

    @app.get("/api/tools")
    def list_registered_tools() -> Dict[str, Any]:
        tools = [t.to_metadata_dict() for t in registry.list_tools()]
        return {"count": len(tools), "tools": tools}

    @app.get("/api/observe")
    def get_current_observation() -> Dict[str, Any]:
        return orchestrator.observe_environment()

    @app.post("/api/plan/preview")
    def preview_plan(req: PlanPreviewRequest) -> Dict[str, Any]:
        try:
            plan = orchestrator.planner.create_plan(req.goal)
            validation = orchestrator.validator.validate(plan.steps)
            return {
                "plan": plan.to_dict(),
                "validation": validation.to_dict(),
            }
        except PlanningError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/tasks")
    def create_and_run_task(req: CreateTaskRequest) -> Dict[str, Any]:
        try:
            return orchestrator.start_task(goal=req.goal, run_async=req.run_async)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/tasks")
    def list_all_tasks(limit: int = 50) -> Dict[str, Any]:
        tasks = store.list_tasks(limit=limit)
        return {"count": len(tasks), "tasks": tasks}

    @app.get("/api/tasks/{task_id}")
    def get_task_detail(task_id: str) -> Dict[str, Any]:
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        return task

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_active_task(task_id: str, req: Optional[CancelTaskRequest] = None) -> Dict[str, Any]:
        reason = req.reason if req else "Cancelled by user."
        task = orchestrator.cancel_task(task_id, reason=reason)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        return task

    @app.post("/api/tasks/{task_id}/resume")
    def resume_existing_task(task_id: str) -> Dict[str, Any]:
        try:
            return orchestrator.resume_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.delete("/api/tasks/history")
    def clear_task_history() -> Dict[str, Any]:
        deleted = store.clear_history()
        return {"deleted_tasks": deleted}

    @app.get("/api/approvals")
    def list_pending_approvals(task_id: Optional[str] = None) -> Dict[str, Any]:
        approvals = store.list_pending_approvals(task_id=task_id)
        return {"count": len(approvals), "approvals": approvals}

    @app.post("/api/approvals/{approval_id}/resolve")
    def resolve_approval_endpoint(
        approval_id: str, req: ResolveApprovalRequest
    ) -> Dict[str, Any]:
        try:
            task = orchestrator.resolve_approval_and_continue(
                approval_id=approval_id,
                approved=req.approved,
                resolution_note=req.resolution_note,
            )
            return {
                "approval": store.get_approval(approval_id),
                "task": task,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/emergency-stop")
    def activate_emergency_stop() -> Dict[str, Any]:
        return orchestrator.trigger_emergency_stop()

    @app.post("/api/emergency-stop/reset")
    def reset_emergency_stop() -> Dict[str, Any]:
        return orchestrator.reset_emergency_stop()

    # Serve production frontend static build if present
    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def serve_dashboard():
            index_file = frontend_dist / "index.html"
            if index_file.is_file():
                return FileResponse(str(index_file))
            return HTMLResponse("<h1>AUTOCRAFT AI Backend Active</h1>")

    return app


app = create_app()
