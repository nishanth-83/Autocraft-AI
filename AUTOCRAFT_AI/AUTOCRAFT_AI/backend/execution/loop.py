"""
Autonomous Execution Loop for AUTOCRAFT AI.
Implements the complete closed-loop state machine:
OBSERVE -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER

Key guarantees:
- Checks prerequisites and dependencies before every step.
- Enforces security and approval gates before consequential/destructive actions.
- Collects real execution evidence and performs independent verification.
- Never reports success without verified evidence.
- Supports bounded retries, safe fallback replanning, task cancellation, and emergency stop.
"""

from __future__ import annotations

import platform
import sys
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Optional, Set

from backend.config import AgentConfig
from backend.database import SQLiteStore, utc_now_iso
from backend.execution.recovery import (
    ErrorRecoveryEngine,
    RecoveryActionType,
)
from backend.planner.engine import PlanStep, PlanningError, SelfPlanningEngine
from backend.planner.nlp import detect_language
from backend.planner.validator import PlanValidator
from backend.security import evaluate_action_security
from backend.tools.base import (
    PreconditionResult,
    ToolContext,
    ToolExecutionResult,
    VerificationResult,
)
from backend.tools.browser import (
    PlaywrightBrowserSession,
    SeleniumChromiumBrowserSession,
    close_browser_session,
    get_browser_session,
)
from backend.tools.desktop import get_desktop_driver
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AutonomousOrchestrator:
    """
    Coordinates the OBSERVE -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER lifecycle.
    """

    def __init__(
        self,
        config: AgentConfig,
        store: SQLiteStore,
        registry: ToolRegistry,
    ) -> None:
        self.config = config
        self.store = store
        self.registry = registry
        self.planner = SelfPlanningEngine(default_max_retries=config.max_step_retries)
        self.validator = PlanValidator(registry=registry, config=config)
        self.recovery = ErrorRecoveryEngine()

        self._lock = threading.RLock()
        self._cancelled_tasks: Set[str] = set()
        self._approved_steps: Set[str] = set()
        self._emergency_stopped: bool = False
        self._active_threads: Dict[str, threading.Thread] = {}

    # --- Observation ---

    def observe_environment(self) -> Dict[str, Any]:
        """Collect real state observation of workspace, browser, and desktop environment."""
        ws_root = self.config.workspace_root
        ws_files: List[str] = []
        workspace_error: Optional[str] = None
        try:
            if ws_root.exists() and ws_root.is_dir():
                for item in sorted(ws_root.iterdir()):
                    if not item.name.startswith("autocraft_state.db"):
                        ws_files.append(f"{item.name}{'/' if item.is_dir() else ''}")
        except OSError as exc:
            workspace_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Could not inspect workspace '%s'.", ws_root)

        desktop_driver = get_desktop_driver()
        browser_session = get_browser_session(
            self.config.browser_channel, self.config.browser_headless
        )
        desktop_supported = (
            self.config.enable_live_desktop_automation and desktop_driver.is_supported()
        )
        desktop_error: Optional[str] = None
        open_windows_count = 0
        if not self.config.enable_live_desktop_automation:
            desktop_error = "Disabled by AUTOCRAFT_ENABLE_DESKTOP."
        elif not desktop_driver.is_supported():
            desktop_error = (
                "Requires an interactive Windows desktop session."
                if sys.platform == "win32"
                else f"Requires Windows (win32); current platform is '{sys.platform}'."
            )
        else:
            try:
                open_windows_count = len(desktop_driver.list_open_windows())
            except Exception as exc:
                desktop_error = f"{type(exc).__name__}: {exc}"
                desktop_supported = False
                logger.exception("Could not inspect the Windows desktop session.")

        browser_available = (
            PlaywrightBrowserSession.is_installed()
            or SeleniumChromiumBrowserSession.is_installed()
        )
        browser_error = getattr(browser_session, "availability_error", None)

        return {
            "timestamp": utc_now_iso(),
            "platform": sys.platform,
            "os_release": f"{platform.system()} {platform.release()}",
            "workspace_root": str(ws_root),
            "workspace_entries": ws_files[:50],
            "workspace_error": workspace_error,
            "desktop_automation_supported": desktop_supported,
            "desktop_error": desktop_error,
            "open_windows_count": open_windows_count,
            "browser_engine": browser_session.engine_name(),
            "browser_available": browser_available,
            "browser_error": browser_error,
            "browser_state": browser_session.current_state(),
            "emergency_stop_active": self._emergency_stopped,
        }

    # --- Control Methods ---

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            return self._emergency_stopped or (task_id in self._cancelled_tasks)

    def cancel_task(self, task_id: str, reason: str = "Cancelled by user request.") -> Optional[Dict[str, Any]]:
        with self._lock:
            self._cancelled_tasks.add(task_id)
        task = self.store.get_task(task_id)
        if not task:
            return None
        if task["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
            self.store.add_log(
                task_id=task_id,
                phase="CANCEL",
                level="WARN",
                message=reason,
            )
            self.store.update_task(
                task_id=task_id,
                status="CANCELLED",
                current_phase="IDLE",
                error_summary=reason,
                final_report=f"Task cancelled: {reason}",
            )
        return self.store.get_task(task_id)

    def trigger_emergency_stop(self) -> Dict[str, Any]:
        """Immediately halt all running tasks and close browser sessions."""
        with self._lock:
            self._emergency_stopped = True
            active_tasks = self.store.list_tasks(limit=50)
            halted_ids: List[str] = []
            for t in active_tasks:
                if t["status"] in ("PENDING", "OBSERVING", "PLANNING", "VALIDATING", "RUNNING", "WAITING_APPROVAL", "RECOVERING"):
                    tid = t["task_id"]
                    self._cancelled_tasks.add(tid)
                    self.store.add_log(
                        task_id=tid,
                        phase="EMERGENCY_STOP",
                        level="SECURITY",
                        message="Emergency Stop triggered by user. Halting execution immediately.",
                    )
                    self.store.update_task(
                        task_id=tid,
                        status="CANCELLED",
                        current_phase="EMERGENCY_STOPPED",
                        error_summary="Emergency Stop activated.",
                        final_report="Execution terminated immediately by Emergency Stop.",
                    )
                    halted_ids.append(tid)
        close_browser_session()
        return {
            "emergency_stop_active": True,
            "halted_task_ids": halted_ids,
            "timestamp": utc_now_iso(),
        }

    def reset_emergency_stop(self) -> Dict[str, Any]:
        with self._lock:
            self._emergency_stopped = False
        return {"emergency_stop_active": False, "timestamp": utc_now_iso()}

    # --- Task Submission & Execution ---

    def start_task(self, goal: str, run_async: bool = False) -> Dict[str, Any]:
        """Create a new task and execute it either synchronously or in a background worker thread."""
        if self._emergency_stopped:
            raise RuntimeError(
                "Emergency Stop is currently active. Reset Emergency Stop before starting new tasks."
            )

        lang = detect_language(goal or "")
        task = self.store.create_task(goal=goal or "", detected_language=lang)
        task_id = task["task_id"]

        if run_async:
            t = threading.Thread(
                target=self._run_full_lifecycle,
                args=(task_id,),
                daemon=True,
                name=f"autocraft-{task_id}",
            )
            with self._lock:
                self._active_threads[task_id] = t
            t.start()
            time.sleep(0.05)
            return self.store.get_task(task_id)  # type: ignore[return-value]
        else:
            return self._run_full_lifecycle(task_id)

    def _run_full_lifecycle(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"Task '{task_id}' not found.")

        goal = task["goal"]

        # ==================================================================
        # PHASE 1: OBSERVE
        # ==================================================================
        self.store.update_task(task_id, status="OBSERVING", current_phase="OBSERVE")
        observation = self.observe_environment()
        self.store.update_task(task_id, observation=observation)
        self.store.add_log(
            task_id=task_id,
            phase="OBSERVE",
            level="INFO",
            message=f"Observed environment on platform '{observation['platform']}' ({len(observation['workspace_entries'])} workspace items).",
            evidence=observation,
        )

        if self.is_cancel_requested(task_id):
            return self.cancel_task(task_id)  # type: ignore[return-value]

        # ==================================================================
        # PHASE 2: PLAN
        # ==================================================================
        self.store.update_task(task_id, status="PLANNING", current_phase="PLAN")
        try:
            generated_plan = self.planner.create_plan(goal)
        except PlanningError as exc:
            err_msg = str(exc)
            self.store.add_log(
                task_id=task_id,
                phase="PLAN",
                level="ERROR",
                message=f"Planning failed: {err_msg}",
            )
            return self.store.update_task(  # type: ignore[return-value]
                task_id,
                status="FAILED",
                current_phase="PLAN",
                error_summary=err_msg,
                final_report=f"Task failed during PLANNING phase: {err_msg}",
            )

        plan_step_dicts = [s.to_dict() for s in generated_plan.steps]
        self.store.update_task(
            task_id,
            detected_language=generated_plan.detected_language,
            plan=plan_step_dicts,
        )
        self.store.save_task_steps(task_id, plan_step_dicts)
        self.store.add_log(
            task_id=task_id,
            phase="PLAN",
            level="INFO",
            message=(
                f"Generated deterministic plan with {len(generated_plan.steps)} step(s) "
                f"(language={generated_plan.detected_language})."
            ),
            evidence={"normalized_clauses": generated_plan.normalized_clauses},
        )

        # ==================================================================
        # PHASE 3: VALIDATE
        # ==================================================================
        self.store.update_task(task_id, status="VALIDATING", current_phase="VALIDATE")
        val_report = self.validator.validate(generated_plan.steps)
        if not val_report.valid:
            joined_errors = "; ".join(val_report.errors)
            self.store.add_log(
                task_id=task_id,
                phase="VALIDATE",
                level="ERROR",
                message=f"Plan validation failed: {joined_errors}",
                evidence=val_report.to_dict(),
            )
            return self.store.update_task(  # type: ignore[return-value]
                task_id,
                status="FAILED",
                current_phase="VALIDATE",
                error_summary=joined_errors,
                final_report=f"Task failed during VALIDATE phase: {joined_errors}",
            )

        updated_step_dicts = [s.to_dict() for s in generated_plan.steps]
        self.store.update_task(task_id, plan=updated_step_dicts)
        self.store.save_task_steps(task_id, updated_step_dicts)
        self.store.add_log(
            task_id=task_id,
            phase="VALIDATE",
            level="INFO",
            message="Executable plan validated against ToolRegistry schemas, DAG rules, and security policy.",
            evidence=val_report.to_dict(),
        )

        # ==================================================================
        # PHASE 4-6: ACT -> VERIFY -> RECOVER
        # ==================================================================
        return self._execute_plan_steps(task_id, generated_plan.steps)

    def _execute_plan_steps(self, task_id: str, steps: List[PlanStep]) -> Dict[str, Any]:
        """Execute remaining steps in topological order with verification and bounded recovery."""
        self.store.update_task(task_id, status="RUNNING", current_phase="ACT")

        persisted_task = self.store.get_task(task_id)
        if not persisted_task:
            raise KeyError(f"Task '{task_id}' disappeared from storage.")

        persisted_steps_by_id = {s["step_id"]: s for s in persisted_task["steps"]}
        completed_step_outputs: Dict[str, Dict[str, Any]] = {}
        for sid, sdata in persisted_steps_by_id.items():
            if (
                sdata["status"] == "COMPLETED"
                and sdata.get("verification", {}).get("verified") is True
            ):
                completed_step_outputs[sid] = sdata.get("execution_output", {})

        for step in steps:
            s_persisted = persisted_steps_by_id.get(step.step_id, {})
            if (
                s_persisted.get("status") == "COMPLETED"
                and s_persisted.get("verification", {}).get("verified") is True
            ):
                continue

            if self.is_cancel_requested(task_id):
                return self.cancel_task(task_id)  # type: ignore[return-value]

            # 1. Verify all dependencies are satisfied and COMPLETED
            for dep_id in step.depends_on:
                dep_state = self.store.get_task(task_id)
                dep_map = {s["step_id"]: s for s in (dep_state["steps"] if dep_state else [])}
                if (
                    dep_id not in dep_map
                    or dep_map[dep_id]["status"] != "COMPLETED"
                    or dep_map[dep_id].get("verification", {}).get("verified") is not True
                ):
                    reason = (
                        f"Cannot execute step '{step.step_id}' because dependency "
                        f"'{dep_id}' is not COMPLETED."
                    )
                    self.store.update_step(
                        task_id,
                        step.step_id,
                        status="FAILED",
                        error={"error_code": "DEPENDENCY_UNSATISFIED", "message": reason},
                    )
                    self.store.add_log(
                        task_id=task_id,
                        step_id=step.step_id,
                        phase="ACT",
                        level="ERROR",
                        message=reason,
                    )
                    return self.store.update_task(  # type: ignore[return-value]
                        task_id,
                        status="FAILED",
                        current_phase="ACT",
                        error_summary=reason,
                        final_report=f"Task failed: {reason}",
                    )

            # 2. Check dynamic security & approval requirement before acting
            tool_def = self.registry.get(step.tool_name)
            sec_eval = evaluate_action_security(
                tool_name=step.tool_name,
                permission_level=tool_def.permission_level,
                parameters=step.parameters,
                workspace_root=self.config.workspace_root,
                allowed_extra_roots=self.config.allowed_extra_roots,
            )
            if sec_eval.blocked:
                block_msg = sec_eval.block_reason or "Blocked by security policy."
                self.store.update_step(
                    task_id,
                    step.step_id,
                    status="FAILED",
                    error={"error_code": "SECURITY_VIOLATION", "message": block_msg},
                )
                self.store.add_log(
                    task_id=task_id,
                    step_id=step.step_id,
                    phase="ACT",
                    level="SECURITY",
                    message=block_msg,
                )
                return self.store.update_task(  # type: ignore[return-value]
                    task_id,
                    status="FAILED",
                    current_phase="ACT",
                    error_summary=block_msg,
                    final_report=f"Blocked by Security Policy: {block_msg}",
                )

            approval_key = f"{task_id}:{step.step_id}"
            is_approved = approval_key in self._approved_steps
            if (step.requires_approval or sec_eval.requires_approval) and not is_approved:
                reason = step.approval_reason or sec_eval.reason or "Action requires user approval."
                return self._pause_for_approval(task_id, step, sec_eval.risk_level, reason)

            # 3. Execute step with bounded retry/recovery loop
            step_succeeded = False
            attempt = int(s_persisted.get("attempt_count", 0))

            while not step_succeeded:
                if self.is_cancel_requested(task_id):
                    return self.cancel_task(task_id)  # type: ignore[return-value]

                attempt += 1
                now_str = utc_now_iso()
                self.store.update_task(
                    task_id,
                    status="RUNNING",
                    current_phase="ACT",
                    current_step_id=step.step_id,
                )
                self.store.update_step(
                    task_id,
                    step.step_id,
                    tool_name=step.tool_name,
                    parameters=step.parameters,
                    status="RUNNING",
                    attempt_count=attempt,
                    started_at=now_str,
                )
                self.store.add_log(
                    task_id=task_id,
                    step_id=step.step_id,
                    phase="ACT",
                    level="INFO",
                    message=f"Executing {step.step_id} ({step.tool_name}), attempt {attempt}.",
                    evidence={"parameters": step.parameters},
                )

                ctx = ToolContext(
                    config=self.config,
                    task_id=task_id,
                    step_id=step.step_id,
                    step_results=completed_step_outputs,
                    approved=is_approved,
                    cancel_requested=lambda: self.is_cancel_requested(task_id),
                )

                pre_res, exec_res, ver_res = self._dispatch_with_timeout(
                    step.tool_name, step.parameters, ctx
                )

                if self.is_cancel_requested(task_id):
                    cancel_reason = "Task stopped while an automation step was in progress."
                    self.store.update_step(
                        task_id,
                        step.step_id,
                        status="CANCELLED",
                        execution_output=exec_res.output,
                        verification=ver_res.to_dict(),
                        error={"error_code": "TASK_CANCELLED", "message": cancel_reason},
                        completed_at=utc_now_iso(),
                    )
                    self.store.add_log(
                        task_id=task_id,
                        step_id=step.step_id,
                        phase="CANCEL",
                        level="WARN",
                        message=cancel_reason,
                        evidence={
                            "execution_output": exec_res.output,
                            "verification": ver_res.to_dict(),
                        },
                    )
                    return self.cancel_task(task_id, reason=cancel_reason)  # type: ignore[return-value]

                self.store.update_task(task_id, current_phase="VERIFY")
                self.store.add_log(
                    task_id=task_id,
                    step_id=step.step_id,
                    phase="VERIFY",
                    level="INFO" if ver_res.verified else "WARN",
                    message=ver_res.summary,
                    evidence=ver_res.to_dict(),
                )

                if exec_res.success and ver_res.verified:
                    step_succeeded = True
                    completed_step_outputs[step.step_id] = exec_res.output
                    self.store.update_step(
                        task_id,
                        step.step_id,
                        status="COMPLETED",
                        execution_output=exec_res.output,
                        verification=ver_res.to_dict(),
                        error={},
                        completed_at=utc_now_iso(),
                    )
                    break

                # ==============================================================
                # PHASE 6: RECOVER
                # ==============================================================
                self.store.update_task(task_id, status="RECOVERING", current_phase="RECOVER")
                decision = self.recovery.decide_recovery(
                    step=step,
                    attempt_count=attempt,
                    precondition=pre_res,
                    execution=exec_res,
                    verification=ver_res,
                )
                self.store.add_log(
                    task_id=task_id,
                    step_id=step.step_id,
                    phase="RECOVER",
                    level="RECOVERY",
                    message=decision.reason,
                    evidence=decision.to_dict(),
                )

                if decision.action == RecoveryActionType.REQUEST_APPROVAL:
                    return self._pause_for_approval(
                        task_id, step, "MEDIUM", decision.reason
                    )

                if decision.action == RecoveryActionType.RETRY:
                    time.sleep(0.1)
                    continue

                if decision.action == RecoveryActionType.REPLAN_FALLBACK and decision.fallback_tool:
                    step.tool_name = decision.fallback_tool
                    if decision.fallback_parameters is not None:
                        step.parameters = decision.fallback_parameters
                    step.fallback_tool = None
                    attempt = 0
                    continue

                err_payload = {
                    "error_code": decision.error_code,
                    "error_category": decision.error_category.value,
                    "message": decision.reason,
                }
                self.store.update_step(
                    task_id,
                    step.step_id,
                    status="FAILED",
                    execution_output=exec_res.output,
                    verification=ver_res.to_dict(),
                    error=err_payload,
                    completed_at=utc_now_iso(),
                )
                return self.store.update_task(  # type: ignore[return-value]
                    task_id,
                    status="FAILED",
                    current_phase="RECOVER",
                    current_step_id=step.step_id,
                    observation=self.observe_environment(),
                    error_summary=decision.reason,
                    final_report=f"Task failed at {step.step_id} ({step.title}): {decision.reason}",
                )

        updated_task = self.store.get_task(task_id)
        verified_steps = updated_task["steps"] if updated_task else []
        expected_step_ids = {step.step_id for step in steps}
        persisted_step_ids = {item.get("step_id") for item in verified_steps}
        if not steps or persisted_step_ids != expected_step_ids or any(
            item.get("status") != "COMPLETED"
            or item.get("verification", {}).get("verified") is not True
            for item in verified_steps
        ):
            reason = "Execution stopped because one or more planned steps lack verified completion evidence."
            self.store.add_log(
                task_id=task_id,
                phase="VERIFY",
                level="ERROR",
                message=reason,
            )
            return self.store.update_task(  # type: ignore[return-value]
                task_id,
                status="FAILED",
                current_phase="VERIFY",
                error_summary=reason,
                final_report=f"Task failed verification: {reason}",
            )

        final_obs = self.observe_environment()
        summary_lines = [
            f"Completed {len(steps)} verified step(s) for goal: '{persisted_task['goal']}'."
        ]
        if updated_task:
            for s in updated_task["steps"]:
                ver_sum = s.get("verification", {}).get("summary", "Verified")
                summary_lines.append(f"- [{s['step_id']}] {s['title']}: {ver_sum}")

        final_report = "\n".join(summary_lines)
        self.store.add_log(
            task_id=task_id,
            phase="VERIFY",
            level="INFO",
            message="All task steps executed and independently verified.",
        )
        return self.store.update_task(  # type: ignore[return-value]
            task_id,
            status="COMPLETED",
            current_phase="IDLE",
            current_step_id=None,
            observation=final_obs,
            final_report=final_report,
            error_summary=None,
        )

    def _dispatch_with_timeout(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        ctx: ToolContext,
    ):
        """Run tool dispatch with step timeout enforcement."""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.registry.dispatch, tool_name, parameters, ctx)
        try:
            return future.result(timeout=self.config.step_timeout_seconds)
        except FuturesTimeoutError:
            pre = PreconditionResult(satisfied=True)
            exec_res = ToolExecutionResult(
                success=False,
                error_code="ACTION_TIMEOUT",
                error_message=(
                    f"Tool '{tool_name}' timed out after {self.config.step_timeout_seconds}s."
                ),
                retryable=True,
            )
            ver_res = VerificationResult(
                verified=False,
                summary="Execution timed out prior to verification.",
                failure_reason=exec_res.error_message,
            )
            return pre, exec_res, ver_res
        finally:
            executor.shutdown(wait=False)

    def _pause_for_approval(
        self,
        task_id: str,
        step: PlanStep,
        risk_level: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Pause task execution and record a pending ApprovalRequest in SQLite."""
        existing_pending = [
            a
            for a in self.store.list_pending_approvals(task_id=task_id)
            if a["step_id"] == step.step_id
        ]
        if not existing_pending:
            self.store.create_approval_request(
                task_id=task_id,
                step_id=step.step_id,
                tool_name=step.tool_name,
                action_summary=step.title,
                risk_level=risk_level,
                reason=reason,
                parameters=step.parameters,
            )
        self.store.update_step(
            task_id,
            step.step_id,
            requires_approval=True,
            approval_reason=reason,
            status="WAITING_APPROVAL",
        )
        self.store.add_log(
            task_id=task_id,
            step_id=step.step_id,
            phase="VALIDATE",
            level="SECURITY",
            message=f"Paused for user approval on {step.step_id}: {reason}",
        )
        return self.store.update_task(  # type: ignore[return-value]
            task_id,
            status="WAITING_APPROVAL",
            current_phase="VALIDATE",
            current_step_id=step.step_id,
        )

    def resolve_approval_and_continue(
        self,
        approval_id: str,
        approved: bool,
        resolution_note: str = "",
    ) -> Dict[str, Any]:
        """Resolve a pending approval request and either resume or abort the paused task."""
        appr = self.store.get_approval(approval_id)
        if not appr:
            raise KeyError(f"Approval request '{approval_id}' not found.")
        if appr["status"] != "PENDING":
            raise ValueError(f"Approval request '{approval_id}' is already {appr['status']}.")

        task_id = appr["task_id"]
        step_id = appr["step_id"]
        self.store.resolve_approval(approval_id, approved=approved, resolution_note=resolution_note)

        if not approved:
            reject_msg = f"User denied approval for step '{step_id}' ({appr['tool_name']})."
            self.store.update_step(
                task_id,
                step_id,
                status="FAILED",
                error={"error_code": "APPROVAL_REJECTED", "message": reject_msg},
                completed_at=utc_now_iso(),
            )
            self.store.add_log(
                task_id=task_id,
                step_id=step_id,
                phase="VALIDATE",
                level="SECURITY",
                message=reject_msg,
            )
            return self.store.update_task(  # type: ignore[return-value]
                task_id,
                status="FAILED",
                current_phase="IDLE",
                error_summary=reject_msg,
                final_report=f"Task halted: {reject_msg}",
            )

        with self._lock:
            self._approved_steps.add(f"{task_id}:{step_id}")

        self.store.add_log(
            task_id=task_id,
            step_id=step_id,
            phase="VALIDATE",
            level="SECURITY",
            message=f"User approved execution of {step_id} ({appr['tool_name']}). Resuming task.",
        )
        return self.resume_task(task_id)

    def resume_task(self, task_id: str) -> Dict[str, Any]:
        """
        Safely resume a paused or interrupted task only after re-observing current state
        and reconstructing remaining steps.
        """
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"Task '{task_id}' not found.")
        if task["status"] == "COMPLETED":
            return task

        with self._lock:
            self._cancelled_tasks.discard(task_id)

        obs = self.observe_environment()
        self.store.update_task(task_id, observation=obs)

        reconstructed_steps: List[PlanStep] = []
        for s in task["steps"]:
            reconstructed_steps.append(
                PlanStep(
                    step_id=s["step_id"],
                    step_order=s["step_order"],
                    title=s["title"],
                    tool_name=s["tool_name"],
                    parameters=s["parameters"],
                    depends_on=s["depends_on"],
                    expected_condition=s["expected_condition"],
                    requires_approval=s["requires_approval"],
                    approval_reason=s["approval_reason"],
                    max_retries=s["max_retries"],
                    fallback_tool=s.get("fallback_tool"),
                    fallback_parameters=s.get("fallback_parameters"),
                )
            )

        return self._execute_plan_steps(task_id, reconstructed_steps)
