"""
Base definitions, schemas, and result structures for the AUTOCRAFT AI Tool Registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from backend.config import AgentConfig
from backend.security import PermissionLevel


class ToolCategory(str, Enum):
    FILESYSTEM = "filesystem"
    DESKTOP = "desktop"
    BROWSER = "browser"
    SYSTEM = "system"


@dataclass
class PreconditionResult:
    satisfied: bool
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecutionResult:
    success: bool
    output: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
        }


@dataclass
class VerificationResult:
    verified: bool
    summary: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    failure_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "summary": self.summary,
            "evidence": self.evidence,
            "failure_reason": self.failure_reason,
        }


@dataclass
class ToolErrorDiagnosis:
    error_code: str
    category: str
    message: str
    retryable: bool
    recovery_suggestion: str


@dataclass
class ToolContext:
    """Execution context passed to every tool's precondition, execute, and verify functions."""

    config: AgentConfig
    task_id: str
    step_id: str
    step_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    approved: bool = False
    cancel_requested: Callable[[], bool] = lambda: False


@dataclass
class ToolDefinition:
    """
    Complete specification for a registered tool in AUTOCRAFT AI.
    Defines capabilities, input/output schemas, preconditions, execution,
    independent verification, error handling, and permission requirements.
    """

    name: str
    category: ToolCategory
    description: str
    capabilities: List[str]
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    permission_level: PermissionLevel
    precondition_descriptions: List[str]
    verification_description: str
    preconditions_fn: Callable[[Dict[str, Any], ToolContext], PreconditionResult]
    execute_fn: Callable[[Dict[str, Any], ToolContext], ToolExecutionResult]
    verify_fn: Callable[[Dict[str, Any], ToolExecutionResult, ToolContext], VerificationResult]
    error_handler_fn: Optional[Callable[[Exception, Dict[str, Any]], ToolErrorDiagnosis]] = None
    supported_on_current_platform: bool = True

    def validate_input(self, parameters: Dict[str, Any]) -> None:
        """Validate input arguments against the tool's JSON Schema."""
        jsonschema.validate(instance=parameters, schema=self.input_schema)

    def validate_output(self, output: Dict[str, Any]) -> None:
        """Validate tool execution output against the tool's output JSON Schema."""
        jsonschema.validate(instance=output, schema=self.output_schema)

    def to_metadata_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "capabilities": self.capabilities,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "permission_level": self.permission_level.value,
            "preconditions": self.precondition_descriptions,
            "verification_method": self.verification_description,
            "supported_on_current_platform": self.supported_on_current_platform,
        }
