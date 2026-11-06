"""
Authorized Filesystem Tools for AUTOCRAFT AI.
Supports creating folders/files, reading, writing, appending, moving/renaming,
copying, organizing directories, listing contents, and deleting with approval.
Enforces strict path confinement and independent read-back/hash verification.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

from backend.security import (
    PermissionLevel,
    SecurityViolationError,
    resolve_authorized_path,
)
from backend.tools.base import (
    PreconditionResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolErrorDiagnosis,
    ToolExecutionResult,
    VerificationResult,
)
from backend.tools.registry import ToolRegistry

EXTENSION_CATEGORY_MAP: Dict[str, str] = {
    ".txt": "Documents",
    ".md": "Documents",
    ".pdf": "Documents",
    ".doc": "Documents",
    ".docx": "Documents",
    ".rtf": "Documents",
    ".png": "Images",
    ".jpg": "Images",
    ".jpeg": "Images",
    ".gif": "Images",
    ".bmp": "Images",
    ".svg": "Images",
    ".py": "Code",
    ".js": "Code",
    ".ts": "Code",
    ".tsx": "Code",
    ".html": "Code",
    ".css": "Code",
    ".json": "Data",
    ".csv": "Data",
    ".xlsx": "Data",
    ".xml": "Data",
    ".sql": "Data",
    ".zip": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_file_target(path: str, ctx: ToolContext) -> Path:
    path_text = str(path).strip()
    normalized = path_text.replace("/", "\\")
    extra_roots = ctx.config.allowed_extra_roots
    if normalized.lower() == "downloads" or normalized.lower().startswith("downloads\\"):
        from backend.tools.desktop import _downloads_directory

        downloads_root = _downloads_directory()
        relative_part = normalized[len("downloads"):].lstrip("\\")
        path_text = str(downloads_root / relative_part)
        extra_roots = [*extra_roots, downloads_root]
    return resolve_authorized_path(
        path_text,
        ctx.config.workspace_root,
        extra_roots,
        allow_root_itself=False,
    )


def _file_content(params: Dict[str, Any], ctx: ToolContext) -> str:
    reference = params.get("content_from_step")
    if reference is None:
        return str(params.get("content", ""))
    if not isinstance(reference, dict):
        raise ValueError("content_from_step must identify a completed step and output key.")
    step_id = str(reference.get("step_id", ""))
    output_key = str(reference.get("output_key", ""))
    if not step_id or not output_key:
        raise ValueError("content_from_step must include step_id and output_key.")
    step_output = ctx.step_results.get(step_id)
    if step_output is None or output_key not in step_output:
        raise ValueError(
            f"Required output '{output_key}' from completed step '{step_id}' is unavailable."
        )
    content = step_output[output_key]
    if not isinstance(content, str) or not content:
        raise ValueError(
            f"Output '{output_key}' from step '{step_id}' must be non-empty text."
        )
    return content


def _fs_error_handler(exc: Exception, params: Dict[str, Any]) -> ToolErrorDiagnosis:
    if isinstance(exc, SecurityViolationError):
        return ToolErrorDiagnosis(
            error_code="SECURITY_VIOLATION",
            category="SECURITY",
            message=str(exc),
            retryable=False,
            recovery_suggestion="Use a path inside the authorized workspace directory.",
        )
    if isinstance(exc, FileNotFoundError):
        return ToolErrorDiagnosis(
            error_code="FILE_NOT_FOUND",
            category="FILESYSTEM",
            message=str(exc),
            retryable=False,
            recovery_suggestion="Verify the source path exists before reading or moving it.",
        )
    if isinstance(exc, FileExistsError):
        return ToolErrorDiagnosis(
            error_code="FILE_ALREADY_EXISTS",
            category="FILESYSTEM",
            message=str(exc),
            retryable=False,
            recovery_suggestion="Request user approval to overwrite or choose a new filename.",
        )
    if isinstance(exc, PermissionError):
        return ToolErrorDiagnosis(
            error_code="FS_PERMISSION_DENIED",
            category="PERMISSION",
            message=str(exc),
            retryable=False,
            recovery_suggestion="Check OS file permissions or close applications locking the file.",
        )
    return ToolErrorDiagnosis(
        error_code="FS_OPERATION_ERROR",
        category="FILESYSTEM",
        message=f"{type(exc).__name__}: {exc}",
        retryable=False,
        recovery_suggestion="Inspect path parameters and disk availability.",
    )


# --- 1. fs.create_folder ---

def _pre_create_folder(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    resolved = resolve_authorized_path(
        params["path"], ctx.config.workspace_root, ctx.config.allowed_extra_roots
    )
    if resolved.exists() and not resolved.is_dir():
        return PreconditionResult(
            satisfied=False,
            reason=f"Path '{resolved}' already exists as a file, cannot create folder.",
            details={"error_code": "FILE_ALREADY_EXISTS"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(resolved)})


def _exec_create_folder(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    resolved = resolve_authorized_path(
        params["path"], ctx.config.workspace_root, ctx.config.allowed_extra_roots
    )
    existed_before = resolved.exists()
    resolved.mkdir(parents=True, exist_ok=True)
    return ToolExecutionResult(
        success=True,
        output={
            "path": str(resolved),
            "created": not existed_before,
            "exists": resolved.is_dir(),
        },
    )


def _ver_create_folder(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    resolved = Path(res.output["path"])
    is_dir = resolved.exists() and resolved.is_dir()
    return VerificationResult(
        verified=is_dir,
        summary=f"Verified directory exists at '{resolved}'." if is_dir else f"Directory '{resolved}' missing.",
        evidence={"path": str(resolved), "is_dir": is_dir},
        failure_reason=None if is_dir else "Directory does not exist on disk after creation.",
    )


# --- 2. fs.create_file ---

def _pre_create_file(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    resolved = _resolve_file_target(params["path"], ctx)
    overwrite = bool(params.get("overwrite", False))
    if resolved.exists():
        if resolved.is_dir():
            return PreconditionResult(
                satisfied=False,
                reason=f"Target '{resolved}' is an existing directory.",
                details={"error_code": "IS_A_DIRECTORY"},
            )
        if not (overwrite or ctx.approved):
            return PreconditionResult(
                satisfied=False,
                reason=f"File '{resolved.name}' already exists and overwrite approval was not granted.",
                details={"error_code": "OVERWRITE_APPROVAL_REQUIRED"},
            )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(resolved)})


def _exec_create_file(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    resolved = _resolve_file_target(params["path"], ctx)
    content = _file_content(params, ctx)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    resolved.write_bytes(encoded)
    return ToolExecutionResult(
        success=True,
        output={
            "path": str(resolved),
            "bytes_written": len(encoded),
            "sha256": _sha256_bytes(encoded),
        },
    )


def _ver_create_file(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    resolved = Path(res.output["path"])
    expected_content = _file_content(params, ctx)
    if resolved != _resolve_file_target(params["path"], ctx):
        return VerificationResult(
            verified=False,
            summary=f"Created file path '{resolved}' did not match the requested target.",
            failure_reason="Created file path did not match the authorized target.",
        )
    expected_hash = _sha256_bytes(expected_content.encode("utf-8"))
    if not resolved.exists() or not resolved.is_file():
        return VerificationResult(
            verified=False,
            summary=f"File '{resolved}' was not found on disk.",
            failure_reason="File missing after write.",
        )
    actual_bytes = resolved.read_bytes()
    actual_hash = _sha256_bytes(actual_bytes)
    matches = actual_hash == expected_hash
    return VerificationResult(
        verified=matches,
        summary=(
            f"Verified file '{resolved.name}' ({len(actual_bytes)} bytes, SHA-256 matched)."
            if matches
            else f"Content hash mismatch for '{resolved.name}'."
        ),
        evidence={
            "path": str(resolved),
            "exists": True,
            "size_bytes": len(actual_bytes),
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "content_preview": actual_bytes.decode("utf-8", errors="replace")[:200],
        },
        failure_reason=None if matches else "Read-back SHA-256 hash did not match expected content.",
    )


# --- 3. fs.write_file ---

def _pre_write_file(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    resolved = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    if resolved.exists() and resolved.is_dir():
        return PreconditionResult(
            satisfied=False,
            reason=f"Target '{resolved}' is a directory.",
            details={"error_code": "IS_A_DIRECTORY"},
        )
    if resolved.exists() and ctx.config.require_approval_for_overwrite and not ctx.approved:
        return PreconditionResult(
            satisfied=False,
            reason=f"Overwriting existing file '{resolved.name}' requires approval.",
            details={"error_code": "OVERWRITE_APPROVAL_REQUIRED"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(resolved)})


# --- 4. fs.append_file ---

def _pre_append_file(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    resolved = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    if resolved.exists() and resolved.is_dir():
        return PreconditionResult(
            satisfied=False,
            reason=f"Target '{resolved}' is a directory.",
            details={"error_code": "IS_A_DIRECTORY"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(resolved)})


def _exec_append_file(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    resolved = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    addition = str(params["content"])
    with resolved.open("a", encoding="utf-8") as f:
        f.write(addition)
    full_bytes = resolved.read_bytes()
    return ToolExecutionResult(
        success=True,
        output={
            "path": str(resolved),
            "appended_chars": len(addition),
            "total_size_bytes": len(full_bytes),
            "sha256": _sha256_bytes(full_bytes),
        },
    )


def _ver_append_file(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    resolved = Path(res.output["path"])
    addition = str(params["content"])
    if not resolved.is_file():
        return VerificationResult(
            verified=False,
            summary="File missing after append.",
            failure_reason="Target file does not exist.",
        )
    text = resolved.read_text(encoding="utf-8", errors="replace")
    ends_with = text.endswith(addition)
    return VerificationResult(
        verified=ends_with,
        summary=f"Verified appended content in '{resolved.name}'." if ends_with else "Appended suffix not found.",
        evidence={
            "path": str(resolved),
            "total_size_bytes": resolved.stat().st_size,
            "ends_with_expected": ends_with,
        },
        failure_reason=None if ends_with else "File does not end with the appended content.",
    )


# --- 5. fs.read_file ---

def _pre_read_file(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    resolved = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    if not resolved.exists():
        return PreconditionResult(
            satisfied=False,
            reason=f"File '{resolved}' does not exist.",
            details={"error_code": "FILE_NOT_FOUND"},
        )
    if not resolved.is_file():
        return PreconditionResult(
            satisfied=False,
            reason=f"Path '{resolved}' is not a regular file.",
            details={"error_code": "NOT_A_FILE"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(resolved)})


def _exec_read_file(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    resolved = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    raw = resolved.read_bytes()
    content = raw.decode("utf-8", errors="replace")
    return ToolExecutionResult(
        success=True,
        output={
            "path": str(resolved),
            "content": content,
            "size_bytes": len(raw),
            "sha256": _sha256_bytes(raw),
        },
    )


def _ver_read_file(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    resolved = Path(res.output["path"])
    exists = resolved.is_file()
    actual_size = resolved.stat().st_size if exists else -1
    verified = exists and actual_size == res.output.get("size_bytes", -2)
    return VerificationResult(
        verified=verified,
        summary=f"Verified read of '{resolved.name}' ({actual_size} bytes).",
        evidence={
            "path": str(resolved),
            "size_bytes": actual_size,
            "sha256": res.output.get("sha256"),
        },
        failure_reason=None if verified else "File metadata did not match read output.",
    )


# --- 6. fs.move_or_rename ---

def _pre_move(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    src = resolve_authorized_path(
        params["source"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    dst = resolve_authorized_path(
        params["destination"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    if not src.exists():
        return PreconditionResult(
            satisfied=False,
            reason=f"Source path '{src}' does not exist.",
            details={"error_code": "FILE_NOT_FOUND"},
        )
    if dst.exists() and not ctx.approved:
        return PreconditionResult(
            satisfied=False,
            reason=f"Destination '{dst.name}' already exists; approval required to overwrite.",
            details={"error_code": "OVERWRITE_APPROVAL_REQUIRED"},
        )
    return PreconditionResult(
        satisfied=True, details={"source": str(src), "destination": str(dst)}
    )


def _exec_move(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    src = resolve_authorized_path(
        params["source"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    dst = resolve_authorized_path(
        params["destination"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return ToolExecutionResult(
        success=True,
        output={
            "source": str(src),
            "destination": str(dst),
            "destination_exists": dst.exists(),
        },
    )


def _ver_move(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    src = Path(res.output["source"])
    dst = Path(res.output["destination"])
    ok = (not src.exists()) and dst.exists()
    return VerificationResult(
        verified=ok,
        summary=f"Verified move from '{src.name}' to '{dst.name}'." if ok else "Move verification failed.",
        evidence={
            "source_removed": not src.exists(),
            "destination_exists": dst.exists(),
            "destination": str(dst),
        },
        failure_reason=None if ok else "Source still exists or destination is missing.",
    )


# --- 7. fs.copy_file ---

def _pre_copy(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    src = resolve_authorized_path(
        params["source"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    dst = resolve_authorized_path(
        params["destination"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    if not src.is_file():
        return PreconditionResult(
            satisfied=False,
            reason=f"Source file '{src}' does not exist.",
            details={"error_code": "FILE_NOT_FOUND"},
        )
    if dst.exists() and not ctx.approved:
        return PreconditionResult(
            satisfied=False,
            reason=f"Destination '{dst.name}' already exists; approval required to overwrite.",
            details={"error_code": "OVERWRITE_APPROVAL_REQUIRED"},
        )
    return PreconditionResult(satisfied=True)


def _exec_copy(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    src = resolve_authorized_path(
        params["source"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    dst = resolve_authorized_path(
        params["destination"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    return ToolExecutionResult(
        success=True,
        output={
            "source": str(src),
            "destination": str(dst),
            "sha256": _sha256_file(dst),
        },
    )


def _ver_copy(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    src = Path(res.output["source"])
    dst = Path(res.output["destination"])
    if not (src.is_file() and dst.is_file()):
        return VerificationResult(
            verified=False,
            summary="Source or destination missing after copy.",
            failure_reason="Destination file was not created.",
        )
    src_hash = _sha256_file(src)
    dst_hash = _sha256_file(dst)
    ok = src_hash == dst_hash
    return VerificationResult(
        verified=ok,
        summary=f"Verified copy '{dst.name}' matches source SHA-256.",
        evidence={"source_sha256": src_hash, "destination_sha256": dst_hash, "destination": str(dst)},
        failure_reason=None if ok else "SHA-256 mismatch between source and copied file.",
    )


# --- 8. fs.organize_folder ---

def _pre_organize(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    target = resolve_authorized_path(
        params.get("path", "."),
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
    )
    if not target.exists() or not target.is_dir():
        return PreconditionResult(
            satisfied=False,
            reason=f"Directory '{target}' does not exist.",
            details={"error_code": "DIRECTORY_NOT_FOUND"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(target)})


def _exec_organize(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    target = resolve_authorized_path(
        params.get("path", "."),
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
    )
    moved_files: List[Dict[str, str]] = []
    for item in sorted(target.iterdir()):
        if item.is_file() and not item.name.startswith(".") and not item.name.endswith(".db"):
            category = EXTENSION_CATEGORY_MAP.get(item.suffix.lower(), "Other")
            cat_dir = target / category
            cat_dir.mkdir(parents=True, exist_ok=True)
            dest = cat_dir / item.name
            # Avoid overwriting existing file during organization
            if dest.exists():
                continue
            shutil.move(str(item), str(dest))
            moved_files.append({"file": item.name, "category": category, "new_path": str(dest)})

    return ToolExecutionResult(
        success=True,
        output={
            "directory": str(target),
            "moved_count": len(moved_files),
            "moved_files": moved_files,
        },
    )


def _ver_organize(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    moved_files = res.output.get("moved_files", [])
    all_exist = all(Path(entry["new_path"]).is_file() for entry in moved_files)
    return VerificationResult(
        verified=all_exist,
        summary=f"Verified organization of {len(moved_files)} file(s) into category folders.",
        evidence={"moved_count": len(moved_files), "all_verified": all_exist, "files": moved_files},
        failure_reason=None if all_exist else "One or more organized files could not be verified at destination.",
    )


# --- 9. fs.delete_path ---

def _pre_delete(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    target = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    if not target.exists():
        return PreconditionResult(
            satisfied=False,
            reason=f"Target '{target}' does not exist.",
            details={"error_code": "FILE_NOT_FOUND"},
        )
    if not ctx.approved:
        return PreconditionResult(
            satisfied=False,
            reason=f"Deleting '{target.name}' is a destructive operation and requires explicit user approval.",
            details={"error_code": "APPROVAL_REQUIRED"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(target)})


def _exec_delete(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    target = resolve_authorized_path(
        params["path"],
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
        allow_root_itself=False,
    )
    was_dir = target.is_dir()
    if was_dir:
        shutil.rmtree(str(target))
    else:
        target.unlink()
    return ToolExecutionResult(
        success=True,
        output={
            "deleted_path": str(target),
            "was_directory": was_dir,
            "exists_after": target.exists(),
        },
    )


def _ver_delete(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    target = Path(res.output["deleted_path"])
    gone = not target.exists()
    return VerificationResult(
        verified=gone,
        summary=f"Verified '{target.name}' was permanently removed." if gone else "Target still exists on disk.",
        evidence={"deleted_path": str(target), "exists_on_disk": target.exists()},
        failure_reason=None if gone else "Target still exists after deletion.",
    )


# --- 10. fs.list_directory ---

def _pre_list_dir(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    target = resolve_authorized_path(
        params.get("path", "."),
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
    )
    if not target.exists() or not target.is_dir():
        return PreconditionResult(
            satisfied=False,
            reason=f"Directory '{target}' does not exist.",
            details={"error_code": "DIRECTORY_NOT_FOUND"},
        )
    return PreconditionResult(satisfied=True, details={"resolved_path": str(target)})


def _exec_list_dir(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    target = resolve_authorized_path(
        params.get("path", "."),
        ctx.config.workspace_root,
        ctx.config.allowed_extra_roots,
    )
    entries: List[Dict[str, Any]] = []
    for item in sorted(target.iterdir()):
        if item.name == "autocraft_state.db" or item.name.startswith("autocraft_state.db-"):
            continue
        stat = item.stat()
        entries.append(
            {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size_bytes": stat.st_size if item.is_file() else 0,
            }
        )
    return ToolExecutionResult(
        success=True,
        output={
            "path": str(target),
            "entry_count": len(entries),
            "entries": entries,
        },
    )


def _ver_list_dir(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    target = Path(res.output["path"])
    ok = target.is_dir() and isinstance(res.output.get("entries"), list)
    return VerificationResult(
        verified=ok,
        summary=f"Verified directory listing of '{target.name}' ({res.output.get('entry_count', 0)} items).",
        evidence={"path": str(target), "entry_count": res.output.get("entry_count", 0)},
    )


def register_filesystem_tools(registry: ToolRegistry) -> None:
    """Register all authorized filesystem tools into the ToolRegistry."""

    registry.register(
        ToolDefinition(
            name="fs.create_folder",
            category=ToolCategory.FILESYSTEM,
            description="Create a new folder inside the authorized workspace scope.",
            capabilities=["create_folder", "mkdir", "filesystem_directory"],
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "created": {"type": "boolean"},
                    "exists": {"type": "boolean"},
                },
                "required": ["path", "exists"],
            },
            permission_level=PermissionLevel.SAFE_WRITE,
            precondition_descriptions=[
                "Target path must resolve inside the authorized workspace root.",
                "Target path must not conflict with an existing regular file.",
            ],
            verification_description="Confirm os.path.isdir(path) is True after creation.",
            preconditions_fn=_pre_create_folder,
            execute_fn=_exec_create_folder,
            verify_fn=_ver_create_folder,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.create_file",
            category=ToolCategory.FILESYSTEM,
            description="Create a new text file and verify its exact content via SHA-256 readback.",
            capabilities=["create_file", "write_new_file", "filesystem_write"],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "content_from_step": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string", "minLength": 1},
                            "output_key": {"type": "string", "minLength": 1},
                        },
                        "required": ["step_id", "output_key"],
                        "additionalProperties": False,
                    },
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "bytes_written": {"type": "integer"},
                    "sha256": {"type": "string"},
                },
                "required": ["path", "bytes_written", "sha256"],
            },
            permission_level=PermissionLevel.SAFE_WRITE,
            precondition_descriptions=[
                "Path must resolve inside the authorized workspace root.",
                "If file already exists, explicit user approval is required before overwriting.",
            ],
            verification_description="Read file back from disk and verify SHA-256 hash matches written content.",
            preconditions_fn=_pre_create_file,
            execute_fn=_exec_create_file,
            verify_fn=_ver_create_file,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.write_file",
            category=ToolCategory.FILESYSTEM,
            description="Write content to a file in the workspace (requires approval if overwriting an existing file).",
            capabilities=["write_file", "update_file", "save_file"],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "bytes_written": {"type": "integer"},
                    "sha256": {"type": "string"},
                },
                "required": ["path", "bytes_written", "sha256"],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "Path must resolve inside the authorized workspace root.",
                "Approval must be granted if the target file already exists.",
            ],
            verification_description="Read file back from disk and verify SHA-256 hash matches expected content.",
            preconditions_fn=_pre_write_file,
            execute_fn=_exec_create_file,
            verify_fn=_ver_create_file,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.append_file",
            category=ToolCategory.FILESYSTEM,
            description="Append text content to an authorized file and verify the updated suffix.",
            capabilities=["append_file", "add_to_file"],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "appended_chars": {"type": "integer"},
                    "total_size_bytes": {"type": "integer"},
                    "sha256": {"type": "string"},
                },
                "required": ["path", "total_size_bytes"],
            },
            permission_level=PermissionLevel.SAFE_WRITE,
            precondition_descriptions=["Path must resolve inside the authorized workspace root."],
            verification_description="Read file and verify it ends with the newly appended content.",
            preconditions_fn=_pre_append_file,
            execute_fn=_exec_append_file,
            verify_fn=_ver_append_file,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.read_file",
            category=ToolCategory.FILESYSTEM,
            description="Read text content and SHA-256 integrity metadata from an authorized file.",
            capabilities=["read_file", "inspect_file", "filesystem_read"],
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                    "sha256": {"type": "string"},
                },
                "required": ["path", "content", "size_bytes", "sha256"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Path must resolve inside the authorized workspace root and exist as a file."
            ],
            verification_description="Confirm file readability and byte length consistency.",
            preconditions_fn=_pre_read_file,
            execute_fn=_exec_read_file,
            verify_fn=_ver_read_file,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.move_or_rename",
            category=ToolCategory.FILESYSTEM,
            description="Move or rename a file or folder within the authorized workspace.",
            capabilities=["move_file", "rename_file", "relocate_path"],
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "destination": {"type": "string", "minLength": 1},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                    "destination_exists": {"type": "boolean"},
                },
                "required": ["source", "destination", "destination_exists"],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "Both source and destination must be within authorized workspace root.",
                "Source must exist; overwriting an existing destination requires approval.",
            ],
            verification_description="Confirm source path no longer exists and destination path exists.",
            preconditions_fn=_pre_move,
            execute_fn=_exec_move,
            verify_fn=_ver_move,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.copy_file",
            category=ToolCategory.FILESYSTEM,
            description="Copy a file within the authorized workspace and verify SHA-256 equality.",
            capabilities=["copy_file", "duplicate_file"],
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "destination": {"type": "string", "minLength": 1},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                    "sha256": {"type": "string"},
                },
                "required": ["source", "destination", "sha256"],
            },
            permission_level=PermissionLevel.SAFE_WRITE,
            precondition_descriptions=[
                "Both source and destination must resolve inside authorized workspace root."
            ],
            verification_description="Compare SHA-256 digests of source and destination files.",
            preconditions_fn=_pre_copy,
            execute_fn=_exec_copy,
            verify_fn=_ver_copy,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.organize_folder",
            category=ToolCategory.FILESYSTEM,
            description="Organize loose files in a workspace directory into categorized subfolders (Documents, Images, Code, Data, Archives, Other).",
            capabilities=["organize_folder", "sort_files", "clean_directory"],
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "moved_count": {"type": "integer"},
                    "moved_files": {"type": "array"},
                },
                "required": ["directory", "moved_count", "moved_files"],
            },
            permission_level=PermissionLevel.SAFE_WRITE,
            precondition_descriptions=[
                "Target folder must exist inside the authorized workspace root."
            ],
            verification_description="Verify every moved file exists in its assigned category subfolder.",
            preconditions_fn=_pre_organize,
            execute_fn=_exec_organize,
            verify_fn=_ver_organize,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.delete_path",
            category=ToolCategory.FILESYSTEM,
            description="Delete a file or directory within the authorized workspace (ALWAYS requires user approval).",
            capabilities=["delete_file", "delete_folder", "remove_path"],
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "deleted_path": {"type": "string"},
                    "was_directory": {"type": "boolean"},
                    "exists_after": {"type": "boolean"},
                },
                "required": ["deleted_path", "exists_after"],
            },
            permission_level=PermissionLevel.DESTRUCTIVE,
            precondition_descriptions=[
                "Target path must resolve inside authorized workspace root and not be the root itself.",
                "Explicit user approval must be granted prior to deletion.",
            ],
            verification_description="Confirm target path no longer exists on disk.",
            preconditions_fn=_pre_delete,
            execute_fn=_exec_delete,
            verify_fn=_ver_delete,
            error_handler_fn=_fs_error_handler,
        )
    )

    registry.register(
        ToolDefinition(
            name="fs.list_directory",
            category=ToolCategory.FILESYSTEM,
            description="List files and directories inside an authorized workspace folder.",
            capabilities=["list_directory", "ls", "dir"],
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "entry_count": {"type": "integer"},
                    "entries": {"type": "array"},
                },
                "required": ["path", "entry_count", "entries"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=["Directory must exist inside the authorized workspace root."],
            verification_description="Confirm directory exists and entry list was retrieved.",
            preconditions_fn=_pre_list_dir,
            execute_fn=_exec_list_dir,
            verify_fn=_ver_list_dir,
            error_handler_fn=_fs_error_handler,
        )
    )
