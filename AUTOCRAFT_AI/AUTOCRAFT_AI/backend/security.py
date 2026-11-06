"""
Security policies, path confinement, sensitive data redaction, and approval gating.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class SecurityViolationError(PermissionError):
    """Raised when an operation violates security boundaries or attempts restricted actions."""


class PermissionLevel(str, Enum):
    """Permission classification for registered tools."""

    SAFE_READ = "SAFE_READ"
    SAFE_WRITE = "SAFE_WRITE"
    CONSEQUENTIAL_WRITE = "CONSEQUENTIAL_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    SENSITIVE_EXTERNAL = "SENSITIVE_EXTERNAL"


@dataclass
class ApprovalDecision:
    """Result of evaluating whether a tool execution requires explicit human approval."""

    requires_approval: bool
    risk_level: str  # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    reason: Optional[str] = None
    blocked: bool = False
    block_reason: Optional[str] = None


# Keys that must never be persisted in plaintext in SQLite or logs
SENSITIVE_KEY_PATTERNS = re.compile(
    r"(password|passwd|pwd|secret|token|api[-_]?key|otp|pin|cvv|cvc|ssn|"
    r"credit[-_]?card|card[-_]?number|private[-_]?key|auth[-_]?header|bearer)",
    re.IGNORECASE,
)

# Inline patterns for OTPs, card numbers, and bearer tokens in strings
INLINE_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CARD]"),
    (re.compile(r"(?i)(password|passwd|pwd|otp|cvv)\s*[:=]\s*['\"]?[^\s,'\"]+['\"]?"), r"\1=[REDACTED]"),
]

# Filenames and extensions that the agent is prohibited from reading or modifying
PROHIBITED_FILENAME_PATTERNS = re.compile(
    r"^(\.env(\..+)?|id_rsa.*|id_ed25519.*|.*\.pem|.*\.pfx|.*\.p12|.*\.key|"
    r"shadow|passwd|sam|system\.hiv|ntds\.dit|wallet\.dat|credentials\.json)$",
    re.IGNORECASE,
)

# Destructive command patterns that must be unconditionally blocked
BLOCKED_COMMAND_PATTERNS = [
    re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),
    re.compile(r"\brm\s+-[rRfF]+\s+(/|[a-zA-Z]:\\|\*)", re.IGNORECASE),
    re.compile(r"\bdel\s+/[fFsSqQ]+\s+[a-zA-Z]:\\", re.IGNORECASE),
    re.compile(r"\brd\s+/[sSqQ]+\s+[a-zA-Z]:\\", re.IGNORECASE),
    re.compile(r"\bcipher\s+/w", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\bbcdedit\b", re.IGNORECASE),
    re.compile(r"\breg\s+delete\b", re.IGNORECASE),
    re.compile(r"\bpowershell.*-enc", re.IGNORECASE),
    re.compile(r"\bshutdown\s+/[sSrR]", re.IGNORECASE),
]

# Keywords in natural language or parameters that trigger mandatory approval
CONSEQUENTIAL_ACTION_KEYWORDS = re.compile(
    r"\b(purchase|buy\s+now|checkout|pay\b|wire\s+transfer|bank\s+transfer|"
    r"send\s+email|send\s+message|post\s+tweet|publish|install\s+software|"
    r"uninstall|msiexec|winget\s+install|choco\s+install)\b",
    re.IGNORECASE,
)


def redact_sensitive_data(data: Any) -> Any:
    """
    Recursively redact passwords, OTPs, tokens, and sensitive fields from dicts, lists, and strings.
    """
    if isinstance(data, dict):
        sanitized: Dict[str, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            if SENSITIVE_KEY_PATTERNS.search(key_str):
                sanitized[key_str] = "[REDACTED]"
            else:
                sanitized[key_str] = redact_sensitive_data(v)
        return sanitized
    if isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_sensitive_data(item) for item in data)
    if isinstance(data, str):
        result = data
        for pattern, replacement in INLINE_SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result
    return data


def resolve_authorized_path(
    target_path: str | Path,
    workspace_root: Path,
    allowed_extra_roots: Optional[List[Path]] = None,
    allow_root_itself: bool = True,
) -> Path:
    """
    Resolve a relative or absolute path and enforce strict confinement within
    the authorized workspace root (or explicitly configured allowed roots).
     Also blocks access to credential files and agent source code self-modification.
    """
    if not target_path or not str(target_path).strip():
        raise SecurityViolationError("Target path cannot be empty.")

    raw_str = str(target_path).strip()

    # Reject null bytes
    if "\x00" in raw_str:
        raise SecurityViolationError("Null byte detected in file path.")

    ws_root = workspace_root.resolve()
    extra_roots = [r.resolve() for r in (allowed_extra_roots or [])]
    valid_roots = [ws_root] + extra_roots

    candidate = Path(raw_str)
    if not candidate.is_absolute():
        resolved = (ws_root / candidate).resolve()
    else:
        resolved = candidate.resolve()

    # Check if resolved path is inside one of the authorized roots
    is_authorized = False
    for root in valid_roots:
        try:
            rel = resolved.relative_to(root)
            if str(rel) == "." and not allow_root_itself:
                raise SecurityViolationError(
                    f"Operation directly on workspace root '{root}' is not permitted."
                )
            is_authorized = True
            break
        except ValueError:
            continue

    if not is_authorized:
        raise SecurityViolationError(
            f"Path '{raw_str}' resolves to '{resolved}', which is outside the authorized "
            f"workspace scope ('{ws_root}')."
        )

    # Prevent modifying the agent's own backend/frontend source code (anti-self-modification)
    backend_dir = Path(__file__).resolve().parent
    project_root = backend_dir.parent
    for protected_dir in (backend_dir, project_root / "frontend"):
        try:
            resolved.relative_to(protected_dir)
            raise SecurityViolationError(
                f"Self-modification of agent source code in '{protected_dir}' is strictly prohibited."
            )
        except ValueError:
            pass

    # Prevent accessing sensitive credential filenames
    if PROHIBITED_FILENAME_PATTERNS.match(resolved.name):
        raise SecurityViolationError(
            f"Access to sensitive or credential file '{resolved.name}' is prohibited by security policy."
        )

    return resolved


def validate_command_safety(text: str) -> None:
    """Ensure a command string or parameter does not contain blocked destructive payloads."""
    if not text:
        return
    for pattern in BLOCKED_COMMAND_PATTERNS:
        if pattern.search(text):
            raise SecurityViolationError(
                f"Blocked destructive command pattern detected: '{pattern.pattern}'."
            )


def evaluate_action_security(
    tool_name: str,
    permission_level: PermissionLevel,
    parameters: Dict[str, Any],
    workspace_root: Path,
    allowed_extra_roots: Optional[List[Path]] = None,
) -> ApprovalDecision:
    """
    Evaluate tool invocation against AUTOCRAFT security policy:
    1. Block prohibited destructive commands or self-modification.
    2. Require approval for deleting files, overwriting existing files, sensitive form submissions,
       financial transactions, sending messages, or software installation.
    """
    # Scan all string parameters for destructive shell commands
    for key, val in parameters.items():
        if isinstance(val, str):
            try:
                validate_command_safety(val)
            except SecurityViolationError as exc:
                return ApprovalDecision(
                    requires_approval=False,
                    risk_level="CRITICAL",
                    blocked=True,
                    block_reason=str(exc),
                )

    # Always require approval for DESTRUCTIVE tools (e.g., fs.delete_path)
    if permission_level == PermissionLevel.DESTRUCTIVE or tool_name == "fs.delete_path":
        target = parameters.get("path", "")
        return ApprovalDecision(
            requires_approval=True,
            risk_level="HIGH",
            reason=f"Destructive operation '{tool_name}' on target '{target}' requires explicit user approval.",
        )

    # Check file overwrite condition for file creation/writing/moving/copying
    if tool_name in ("fs.create_file", "fs.write_file"):
        target_raw = parameters.get("path")
        if target_raw:
            try:
                resolved = resolve_authorized_path(
                    target_raw, workspace_root, allowed_extra_roots, allow_root_itself=False
                )
                if resolved.exists():
                    return ApprovalDecision(
                        requires_approval=True,
                        risk_level="MEDIUM",
                        reason=(
                            f"File '{target_raw}' already exists. Overwriting existing files "
                            "requires explicit user approval."
                        ),
                    )
            except SecurityViolationError as exc:
                return ApprovalDecision(
                    requires_approval=False,
                    risk_level="CRITICAL",
                    blocked=True,
                    block_reason=str(exc),
                )

    if tool_name in ("fs.move_or_rename", "fs.copy_file"):
        dest_raw = parameters.get("destination")
        if dest_raw:
            try:
                resolved_dest = resolve_authorized_path(
                    dest_raw, workspace_root, allowed_extra_roots, allow_root_itself=False
                )
                if resolved_dest.exists():
                    return ApprovalDecision(
                        requires_approval=True,
                        risk_level="MEDIUM",
                        reason=(
                            f"Destination '{dest_raw}' already exists. Overwriting existing files "
                            "requires explicit user approval."
                        ),
                    )
            except SecurityViolationError as exc:
                return ApprovalDecision(
                    requires_approval=False,
                    risk_level="CRITICAL",
                    blocked=True,
                    block_reason=str(exc),
                )

    # Browser form filling requires approval if submitting or touching sensitive fields
    if tool_name == "browser.fill_form":
        fields = parameters.get("fields", {})
        submit = bool(parameters.get("submit", False))
        field_keys_str = " ".join(str(k) for k in fields.keys()) if isinstance(fields, dict) else str(fields)
        if SENSITIVE_KEY_PATTERNS.search(field_keys_str) or submit:
            return ApprovalDecision(
                requires_approval=True,
                risk_level="HIGH",
                reason="Filling or submitting browser forms with sensitive or transactional fields requires user approval.",
            )

    # Check consequential external actions (sending messages, purchases, installations)
    param_dump = " ".join(str(v) for v in parameters.values())
    if permission_level == PermissionLevel.SENSITIVE_EXTERNAL or CONSEQUENTIAL_ACTION_KEYWORDS.search(param_dump):
        return ApprovalDecision(
            requires_approval=True,
            risk_level="HIGH",
            reason=f"Consequential action detected in '{tool_name}'. Explicit user approval is required.",
        )

    return ApprovalDecision(
        requires_approval=False,
        risk_level="LOW",
        reason=None,
    )
