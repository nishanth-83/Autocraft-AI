"""
Executable Plan Validator for AUTOCRAFT AI.
Validates generated plans against the ToolRegistry, JSON schemas, dependency DAG rules,
and security/approval policies before any action is executed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

import jsonschema

from backend.config import AgentConfig
from backend.planner.engine import PlanStep
from backend.security import evaluate_action_security
from backend.tools.registry import ToolRegistry


@dataclass
class PlanValidationReport:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    approval_required_steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "approval_required_steps": self.approval_required_steps,
        }


class PlanValidator:
    """Validates ordered executable plans prior to entering the ACT phase."""

    def __init__(self, registry: ToolRegistry, config: AgentConfig) -> None:
        self.registry = registry
        self.config = config

    def validate(self, steps: List[PlanStep]) -> PlanValidationReport:
        errors: List[str] = []
        warnings: List[str] = []
        approval_steps: List[str] = []

        if not isinstance(steps, list) or len(steps) == 0:
            return PlanValidationReport(
                valid=False,
                errors=["Plan is empty; at least one executable step is required."],
            )

        if len(steps) > self.config.max_plan_steps:
            errors.append(
                f"Plan contains {len(steps)} steps, exceeding max_plan_steps ({self.config.max_plan_steps})."
            )

        seen_ids: Set[str] = set()
        step_map: Dict[str, PlanStep] = {}

        for idx, step in enumerate(steps):
            if not isinstance(step, PlanStep):
                errors.append(f"Step at index {idx} is malformed (not a PlanStep instance).")
                continue

            if not step.step_id or not step.step_id.strip():
                errors.append(f"Step at index {idx} has an empty step_id.")
                continue

            if step.step_id in seen_ids:
                errors.append(f"Duplicate step_id '{step.step_id}' detected in plan.")
            seen_ids.add(step.step_id)
            step_map[step.step_id] = step

            # Verify tool exists in ToolRegistry
            if not self.registry.has_tool(step.tool_name):
                errors.append(
                    f"Step '{step.step_id}' references unregistered tool '{step.tool_name}'."
                )
                continue

            tool_def = self.registry.get(step.tool_name)

            # Validate input parameters against tool's JSON Schema
            if not isinstance(step.parameters, dict):
                errors.append(f"Step '{step.step_id}' parameters must be a JSON object/dict.")
                continue

            try:
                tool_def.validate_input(step.parameters)
            except jsonschema.ValidationError as exc:
                errors.append(
                    f"Step '{step.step_id}' ({step.tool_name}) failed input schema validation: {exc.message}"
                )

            # Validate fallback tool if specified
            if step.fallback_tool:
                if not self.registry.has_tool(step.fallback_tool):
                    errors.append(
                        f"Step '{step.step_id}' references unregistered fallback tool '{step.fallback_tool}'."
                    )
                elif step.fallback_parameters is not None:
                    fb_def = self.registry.get(step.fallback_tool)
                    try:
                        fb_def.validate_input(step.fallback_parameters)
                    except jsonschema.ValidationError as exc:
                        errors.append(
                            f"Step '{step.step_id}' fallback tool '{step.fallback_tool}' failed schema validation: {exc.message}"
                        )

            # Evaluate security & approval requirements
            sec_decision = evaluate_action_security(
                tool_name=step.tool_name,
                permission_level=tool_def.permission_level,
                parameters=step.parameters,
                workspace_root=self.config.workspace_root,
                allowed_extra_roots=self.config.allowed_extra_roots,
            )
            if sec_decision.blocked:
                errors.append(
                    f"Step '{step.step_id}' ({step.tool_name}) blocked by security policy: {sec_decision.block_reason}"
                )
            elif sec_decision.requires_approval or step.requires_approval:
                step.requires_approval = True
                step.approval_reason = step.approval_reason or sec_decision.reason
                approval_steps.append(step.step_id)

            if not tool_def.supported_on_current_platform:
                warnings.append(
                    f"Step '{step.step_id}' uses tool '{step.tool_name}' which requires Windows live support."
                )

        # Dependency validation & cycle detection
        for step in steps:
            if not isinstance(step, PlanStep):
                continue
            for dep_id in step.depends_on:
                if dep_id == step.step_id:
                    errors.append(f"Step '{step.step_id}' cannot depend on itself.")
                elif dep_id not in step_map:
                    errors.append(
                        f"Step '{step.step_id}' depends on non-existent step '{dep_id}'."
                    )

        if not errors and self._has_dependency_cycle(step_map):
            errors.append("Circular dependency detected in plan step dependency graph.")

        return PlanValidationReport(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            approval_required_steps=approval_steps,
        )

    @staticmethod
    def _has_dependency_cycle(step_map: Dict[str, PlanStep]) -> bool:
        visited: Dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=visited

        def _dfs(node_id: str) -> bool:
            state = visited.get(node_id, 0)
            if state == 1:
                return True
            if state == 2:
                return False
            visited[node_id] = 1
            step = step_map.get(node_id)
            if step:
                for dep in step.depends_on:
                    if _dfs(dep):
                        return True
            visited[node_id] = 2
            return False

        for sid in step_map:
            if visited.get(sid, 0) == 0:
                if _dfs(sid):
                    return True
        return False
