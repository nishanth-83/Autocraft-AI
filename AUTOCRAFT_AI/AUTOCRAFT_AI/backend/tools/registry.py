"""
Central Tool Registry for AUTOCRAFT AI.
Manages registration, discovery, schema validation, precondition checks,
execution dispatch, independent verification, and error handling.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from backend.security import SecurityViolationError
from backend.tools.base import (
    PreconditionResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolErrorDiagnosis,
    ToolExecutionResult,
    VerificationResult,
)


class ToolNotFoundError(KeyError):
    """Raised when a requested tool is not registered."""


class ToolRegistry:
    """Modular registry for desktop, browser, filesystem, and application tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a validated ToolDefinition."""
        if not tool.name or "." not in tool.name:
            raise ValueError(f"Tool name '{tool.name}' must follow '<namespace>.<action>' format.")
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        """Return a registered tool by name or raise ToolNotFoundError."""
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' is not registered in ToolRegistry.")
        return self._tools[name]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def list_tools(
        self,
        category: Optional[ToolCategory] = None,
        only_supported: bool = False,
    ) -> List[ToolDefinition]:
        tools = list(self._tools.values())
        if category is not None:
            tools = [t for t in tools if t.category == category]
        if only_supported:
            tools = [t for t in tools if t.supported_on_current_platform]
        return sorted(tools, key=lambda t: t.name)

    def find_by_capability(self, capability: str) -> List[ToolDefinition]:
        cap_lower = capability.strip().lower()
        return [
            t
            for t in self._tools.values()
            if any(c.lower() == cap_lower for c in t.capabilities)
        ]

    def dispatch(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        context: ToolContext,
    ) -> Tuple[PreconditionResult, ToolExecutionResult, VerificationResult]:
        """
        Full lifecycle dispatch of a single tool:
        1. Look up tool in registry.
        2. Validate input parameters against input_schema.
        3. Check tool preconditions.
        4. Execute tool action.
        5. Validate output schema (when execution succeeded).
        6. Run independent verification method and collect real evidence.
        """
        tool = self.get(tool_name)

        # 1. Input schema validation
        try:
            tool.validate_input(parameters)
        except jsonschema.ValidationError as exc:
            pre = PreconditionResult(
                satisfied=False,
                reason=f"Input schema validation failed: {exc.message}",
                details={"schema_path": list(exc.schema_path)},
            )
            exec_res = ToolExecutionResult(
                success=False,
                error_code="INVALID_TOOL_ARGUMENTS",
                error_message=f"Invalid arguments for '{tool_name}': {exc.message}",
                retryable=False,
            )
            ver_res = VerificationResult(
                verified=False,
                summary="Skipped verification due to invalid tool input arguments.",
                failure_reason=exec_res.error_message,
            )
            return pre, exec_res, ver_res

        # 2. Preconditions check
        try:
            pre = tool.preconditions_fn(parameters, context)
        except SecurityViolationError as exc:
            pre = PreconditionResult(satisfied=False, reason=str(exc))
            exec_res = ToolExecutionResult(
                success=False,
                error_code="SECURITY_VIOLATION",
                error_message=str(exc),
                retryable=False,
            )
            ver_res = VerificationResult(
                verified=False,
                summary="Blocked by security policy precondition.",
                failure_reason=str(exc),
            )
            return pre, exec_res, ver_res
        except Exception as exc:
            pre = PreconditionResult(satisfied=False, reason=f"Precondition check error: {exc}")

        if not pre.satisfied:
            err_code = pre.details.get("error_code", "PRECONDITION_FAILED")
            exec_res = ToolExecutionResult(
                success=False,
                error_code=err_code,
                error_message=pre.reason or "Tool preconditions were not satisfied.",
                retryable=bool(pre.details.get("retryable", False)),
            )
            ver_res = VerificationResult(
                verified=False,
                summary="Preconditions not met; action was not executed.",
                failure_reason=pre.reason,
                evidence=pre.details,
            )
            return pre, exec_res, ver_res

        # 3. Check cancellation before acting
        if context.cancel_requested():
            exec_res = ToolExecutionResult(
                success=False,
                error_code="TASK_CANCELLED",
                error_message="Task cancelled before tool execution.",
                retryable=False,
            )
            ver_res = VerificationResult(
                verified=False,
                summary="Cancelled prior to execution.",
                failure_reason="Task cancelled by user.",
            )
            return pre, exec_res, ver_res

        # 4. Execute actual tool method
        try:
            exec_res = tool.execute_fn(parameters, context)
        except Exception as exc:
            if tool.error_handler_fn is not None:
                diag: ToolErrorDiagnosis = tool.error_handler_fn(exc, parameters)
                exec_res = ToolExecutionResult(
                    success=False,
                    error_code=diag.error_code,
                    error_message=diag.message,
                    retryable=diag.retryable,
                    output={"recovery_suggestion": diag.recovery_suggestion},
                )
            elif isinstance(exc, SecurityViolationError):
                exec_res = ToolExecutionResult(
                    success=False,
                    error_code="SECURITY_VIOLATION",
                    error_message=str(exc),
                    retryable=False,
                )
            else:
                exec_res = ToolExecutionResult(
                    success=False,
                    error_code="TOOL_EXECUTION_EXCEPTION",
                    error_message=f"{type(exc).__name__}: {exc}",
                    retryable=False,
                )

        if not exec_res.success:
            ver_res = VerificationResult(
                verified=False,
                summary=f"Tool execution failed: {exec_res.error_message}",
                failure_reason=exec_res.error_message,
                evidence=exec_res.output,
            )
            return pre, exec_res, ver_res

        # 5. Validate output schema
        try:
            tool.validate_output(exec_res.output)
        except jsonschema.ValidationError as exc:
            ver_res = VerificationResult(
                verified=False,
                summary=f"Tool output schema validation failed: {exc.message}",
                failure_reason=f"Output schema mismatch: {exc.message}",
                evidence=exec_res.output,
            )
            exec_res.success = False
            exec_res.error_code = "OUTPUT_SCHEMA_INVALID"
            exec_res.error_message = ver_res.failure_reason
            return pre, exec_res, ver_res

        # 6. Independent Verification
        try:
            ver_res = tool.verify_fn(parameters, exec_res, context)
        except Exception as exc:
            ver_res = VerificationResult(
                verified=False,
                summary=f"Verification raised an exception: {exc}",
                failure_reason=str(exc),
                evidence=exec_res.output,
            )

        if not ver_res.verified:
            exec_res.success = False
            exec_res.error_code = exec_res.error_code or "VERIFICATION_FAILED"
            exec_res.error_message = ver_res.failure_reason or ver_res.summary

        return pre, exec_res, ver_res
