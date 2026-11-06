"""
Error Classification and Safe Recovery Engine for AUTOCRAFT AI.
Classifies errors, detects missing applications and inaccessible controls,
prevents infinite retry loops, and determines whether to retry, replan with a safe fallback,
request user approval, or abort honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from backend.planner.engine import PlanStep
from backend.tools.base import PreconditionResult, ToolExecutionResult, VerificationResult


class ErrorCategory(str, Enum):
    MISSING_APPLICATION = "MISSING_APPLICATION"
    INACCESSIBLE_CONTROL = "INACCESSIBLE_CONTROL"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    FILE_CONFLICT = "FILE_CONFLICT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"
    PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    NETWORK_ERROR = "NETWORK_ERROR"
    TASK_CANCELLED = "TASK_CANCELLED"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"


class RecoveryActionType(str, Enum):
    RETRY = "RETRY"
    REPLAN_FALLBACK = "REPLAN_FALLBACK"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    ABORT_HONESTLY = "ABORT_HONESTLY"


@dataclass
class RecoveryDecision:
    action: RecoveryActionType
    error_category: ErrorCategory
    error_code: str
    reason: str
    fallback_tool: Optional[str] = None
    fallback_parameters: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "error_category": self.error_category.value,
            "error_code": self.error_code,
            "reason": self.reason,
            "fallback_tool": self.fallback_tool,
            "fallback_parameters": self.fallback_parameters,
        }


class ErrorRecoveryEngine:
    """
    Deterministic error classifier and bounded recovery planner.
    Guarantees no infinite retry loops and never masks unresolved failures.
    """

    def classify_error(
        self,
        error_code: Optional[str],
        error_message: Optional[str],
        precondition: PreconditionResult,
        execution: ToolExecutionResult,
        verification: VerificationResult,
    ) -> ErrorCategory:
        code = (error_code or execution.error_code or "UNKNOWN_ERROR").upper()
        msg = (error_message or execution.error_message or verification.failure_reason or "").lower()

        if code == "TASK_CANCELLED" or "cancelled" in msg:
            return ErrorCategory.TASK_CANCELLED
        if code in ("SECURITY_VIOLATION", "SECURITY_CAPTCHA_OR_AUTH_BLOCKED") or "captcha" in msg:
            return ErrorCategory.SECURITY_BLOCKED
        if code in ("OVERWRITE_APPROVAL_REQUIRED", "APPROVAL_REQUIRED"):
            return ErrorCategory.APPROVAL_REQUIRED
        if code == "DESKTOP_OS_UNSUPPORTED":
            return ErrorCategory.PLATFORM_UNSUPPORTED
        if code == "APP_NOT_INSTALLED" or "not installed" in msg:
            return ErrorCategory.MISSING_APPLICATION
        if code in ("UI_CONTROL_NOT_FOUND", "BROWSER_ELEMENT_NOT_FOUND") or "control" in msg or "element" in msg:
            return ErrorCategory.INACCESSIBLE_CONTROL
        if "timeout" in code.lower() or "timed out" in msg or "timeout" in msg:
            return ErrorCategory.ACTION_TIMEOUT
        if code in ("FILE_ALREADY_EXISTS", "IS_A_DIRECTORY"):
            return ErrorCategory.FILE_CONFLICT
        if code == "INVALID_TOOL_ARGUMENTS":
            return ErrorCategory.INVALID_PARAMETERS
        if code == "BROWSER_NETWORK_UNREACHABLE" or "net::" in msg:
            return ErrorCategory.NETWORK_ERROR
        if code == "VERIFICATION_FAILED" or (execution.success and not verification.verified):
            return ErrorCategory.VERIFICATION_FAILURE

        return ErrorCategory.EXECUTION_FAILURE

    def decide_recovery(
        self,
        step: PlanStep,
        attempt_count: int,
        precondition: PreconditionResult,
        execution: ToolExecutionResult,
        verification: VerificationResult,
    ) -> RecoveryDecision:
        error_code = execution.error_code or (
            precondition.details.get("error_code") if not precondition.satisfied else "VERIFICATION_FAILED"
        )
        error_message = (
            execution.error_message
            or precondition.reason
            or verification.failure_reason
            or "Step execution or verification failed."
        )

        category = self.classify_error(
            error_code=error_code,
            error_message=error_message,
            precondition=precondition,
            execution=execution,
            verification=verification,
        )

        # 1. Never retry or replan on cancellation or security violation
        if category in (ErrorCategory.TASK_CANCELLED, ErrorCategory.SECURITY_BLOCKED):
            return RecoveryDecision(
                action=RecoveryActionType.ABORT_HONESTLY,
                error_category=category,
                error_code=str(error_code),
                reason=error_message,
            )

        # 2. If operation requires user approval (e.g. file overwrite discovered at runtime), pause for approval
        if category == ErrorCategory.APPROVAL_REQUIRED:
            return RecoveryDecision(
                action=RecoveryActionType.REQUEST_APPROVAL,
                error_category=category,
                error_code=str(error_code),
                reason=error_message,
            )

        # 3. If application is missing and a safe fallback tool is configured on the step, replan to fallback
        if category == ErrorCategory.MISSING_APPLICATION and step.fallback_tool:
            return RecoveryDecision(
                action=RecoveryActionType.REPLAN_FALLBACK,
                error_category=category,
                error_code=str(error_code),
                reason=(
                    f"Primary application failed ({error_message}). "
                    f"Replanning step '{step.step_id}' to safe fallback tool '{step.fallback_tool}'."
                ),
                fallback_tool=step.fallback_tool,
                fallback_parameters=step.fallback_parameters or step.parameters,
            )

        # 4. Retry bounded times if error is retryable and attempt_count <= step.max_retries
        retryable_categories = {
            ErrorCategory.INACCESSIBLE_CONTROL,
            ErrorCategory.ACTION_TIMEOUT,
            ErrorCategory.VERIFICATION_FAILURE,
            ErrorCategory.NETWORK_ERROR,
        }
        is_retryable = execution.retryable or (category in retryable_categories)
        if is_retryable and attempt_count <= step.max_retries:
            return RecoveryDecision(
                action=RecoveryActionType.RETRY,
                error_category=category,
                error_code=str(error_code),
                reason=(
                    f"Attempt {attempt_count}/{step.max_retries + 1} failed ({error_message}). "
                    f"Retrying step '{step.step_id}' safely."
                ),
            )

        # 5. If retries exhausted and a safe fallback tool is available, replan once
        if step.fallback_tool and step.tool_name != step.fallback_tool:
            return RecoveryDecision(
                action=RecoveryActionType.REPLAN_FALLBACK,
                error_category=category,
                error_code=str(error_code),
                reason=(
                    f"Step '{step.step_id}' exhausted retries ({error_message}). "
                    f"Switching to fallback tool '{step.fallback_tool}'."
                ),
                fallback_tool=step.fallback_tool,
                fallback_parameters=step.fallback_parameters or step.parameters,
            )

        # 6. Otherwise abort honestly and report the exact failure reason
        return RecoveryDecision(
            action=RecoveryActionType.ABORT_HONESTLY,
            error_category=category,
            error_code=str(error_code),
            reason=(
                f"Step '{step.step_id}' ({step.tool_name}) failed after {attempt_count} attempt(s): "
                f"{error_message}"
            ),
        )
