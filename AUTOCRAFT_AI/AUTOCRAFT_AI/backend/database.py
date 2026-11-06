"""
SQLite persistence layer for AUTOCRAFT AI tasks, steps, approvals, logs, and configuration.
Ensures sensitive data (passwords, OTPs, keys) is redacted before storage.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from backend.security import redact_sensitive_data

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    """Thread-safe SQLite storage manager for task state, step history, approvals, and audit logs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self._lock = threading.RLock()
        self.initialization_error: Optional[str] = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
        except (OSError, sqlite3.Error) as exc:
            self.initialization_error = f"{type(exc).__name__}: {exc}"
            logger.exception("SQLite initialization failed for '%s'.", self.db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        schema_sql = """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                detected_language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'PENDING',
                current_phase TEXT NOT NULL DEFAULT 'IDLE',
                current_step_id TEXT,
                observation_json TEXT NOT NULL DEFAULT '{}',
                plan_json TEXT NOT NULL DEFAULT '[]',
                final_report TEXT,
                error_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_steps (
                step_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_order INTEGER NOT NULL,
                title TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                depends_on_json TEXT NOT NULL DEFAULT '[]',
                expected_condition TEXT NOT NULL DEFAULT '',
                requires_approval INTEGER NOT NULL DEFAULT 0,
                approval_reason TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 2,
                fallback_tool TEXT,
                fallback_parameters_json TEXT,
                execution_output_json TEXT NOT NULL DEFAULT '{}',
                verification_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                action_summary TEXT NOT NULL,
                risk_level TEXT NOT NULL DEFAULT 'HIGH',
                reason TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS execution_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                step_id TEXT,
                phase TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'INFO',
                message TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                timestamp TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS config_store (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """
        with self._lock, self._connect() as conn:
            conn.executescript(schema_sql)
            conn.commit()

    def check_health(self) -> Dict[str, Any]:
        """Verify SQLite connectivity and return table counts."""
        if self.initialization_error is not None:
            return {
                "healthy": False,
                "db_path": str(self.db_path),
                "error": self.initialization_error,
            }
        try:
            with self._lock, self._connect() as conn:
                tasks_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                pending_approvals = conn.execute(
                    "SELECT COUNT(*) FROM approvals WHERE status = 'PENDING'"
                ).fetchone()[0]
            return {
                "healthy": True,
                "db_path": str(self.db_path),
                "total_tasks": tasks_count,
                "pending_approvals": pending_approvals,
            }
        except (OSError, sqlite3.Error) as exc:
            logger.exception("SQLite health check failed for '%s'.", self.db_path)
            return {
                "healthy": False,
                "db_path": str(self.db_path),
                "error": f"{type(exc).__name__}: {exc}",
            }

    # --- Task Operations ---

    def create_task(self, goal: str, detected_language: str = "en", task_id: Optional[str] = None) -> Dict[str, Any]:
        tid = task_id or f"task_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        safe_goal = redact_sensitive_data(goal)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, goal, detected_language, status, current_phase,
                    observation_json, plan_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'PENDING', 'OBSERVE', '{}', '[]', ?, ?)
                """,
                (tid, safe_goal, detected_language, now, now),
            )
            conn.commit()
        return self.get_task(tid)  # type: ignore[return-value]

    def update_task(
        self,
        task_id: str,
        *,
        detected_language: Optional[str] = None,
        status: Optional[str] = None,
        current_phase: Optional[str] = None,
        current_step_id: Optional[str] = "__UNSET__",
        observation: Optional[Dict[str, Any]] = None,
        plan: Optional[List[Dict[str, Any]]] = None,
        final_report: Optional[str] = None,
        error_summary: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        values: List[Any] = []

        if detected_language is not None:
            fields.append("detected_language = ?")
            values.append(detected_language)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if current_phase is not None:
            fields.append("current_phase = ?")
            values.append(current_phase)
        if current_step_id != "__UNSET__":
            fields.append("current_step_id = ?")
            values.append(current_step_id)
        if observation is not None:
            fields.append("observation_json = ?")
            values.append(json.dumps(redact_sensitive_data(observation)))
        if plan is not None:
            fields.append("plan_json = ?")
            values.append(json.dumps(redact_sensitive_data(plan)))
        if final_report is not None:
            fields.append("final_report = ?")
            values.append(redact_sensitive_data(final_report))
        if error_summary is not None:
            fields.append("error_summary = ?")
            values.append(redact_sensitive_data(error_summary))

        if not fields:
            return self.get_task(task_id)

        fields.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(task_id)

        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?",
                tuple(values),
            )
            conn.commit()
        return self.get_task(task_id)

    def save_task_steps(self, task_id: str, steps: List[Dict[str, Any]]) -> None:
        """Replace or insert ordered steps for a task."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
            for step in steps:
                pk = (
                    f"{task_id}_{step['step_id']}"
                    if not str(step["step_id"]).startswith(task_id)
                    else step["step_id"]
                )
                conn.execute(
                    """
                    INSERT INTO task_steps (
                        step_id, task_id, step_order, title, tool_name,
                        parameters_json, depends_on_json, expected_condition,
                        requires_approval, approval_reason, status,
                        attempt_count, max_retries, fallback_tool, fallback_parameters_json,
                        execution_output_json, verification_json, error_json,
                        started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pk,
                        task_id,
                        int(step["step_order"]),
                        step["title"],
                        step["tool_name"],
                        json.dumps(redact_sensitive_data(step.get("parameters", {}))),
                        json.dumps(step.get("depends_on", [])),
                        step.get("expected_condition", ""),
                        1 if step.get("requires_approval") else 0,
                        step.get("approval_reason"),
                        step.get("status", "PENDING"),
                        int(step.get("attempt_count", 0)),
                        int(step.get("max_retries", 2)),
                        step.get("fallback_tool"),
                        json.dumps(redact_sensitive_data(step.get("fallback_parameters")))
                        if step.get("fallback_parameters") is not None
                        else None,
                        json.dumps(redact_sensitive_data(step.get("execution_output", {}))),
                        json.dumps(redact_sensitive_data(step.get("verification", {}))),
                        json.dumps(redact_sensitive_data(step.get("error", {}))),
                        step.get("started_at"),
                        step.get("completed_at"),
                    ),
                )
            conn.commit()

    def update_step(
        self,
        task_id: str,
        step_id: str,
        *,
        tool_name: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        requires_approval: Optional[bool] = None,
        approval_reason: Optional[str] = None,
        status: Optional[str] = None,
        attempt_count: Optional[int] = None,
        execution_output: Optional[Dict[str, Any]] = None,
        verification: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        fields: List[str] = []
        values: List[Any] = []

        if tool_name is not None:
            fields.append("tool_name = ?")
            values.append(tool_name)
        if parameters is not None:
            fields.append("parameters_json = ?")
            values.append(json.dumps(redact_sensitive_data(parameters)))
        if requires_approval is not None:
            fields.append("requires_approval = ?")
            values.append(1 if requires_approval else 0)
        if approval_reason is not None:
            fields.append("approval_reason = ?")
            values.append(approval_reason)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if attempt_count is not None:
            fields.append("attempt_count = ?")
            values.append(int(attempt_count))
        if execution_output is not None:
            fields.append("execution_output_json = ?")
            values.append(json.dumps(redact_sensitive_data(execution_output)))
        if verification is not None:
            fields.append("verification_json = ?")
            values.append(json.dumps(redact_sensitive_data(verification)))
        if error is not None:
            fields.append("error_json = ?")
            values.append(json.dumps(redact_sensitive_data(error)))
        if started_at is not None:
            fields.append("started_at = ?")
            values.append(started_at)
        if completed_at is not None:
            fields.append("completed_at = ?")
            values.append(completed_at)

        if not fields:
            return

        pk = f"{task_id}_{step_id}" if not step_id.startswith(task_id) else step_id
        values.append(pk)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE task_steps SET {', '.join(fields)} WHERE step_id = ?",
                tuple(values),
            )
            conn.commit()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            step_rows = conn.execute(
                "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_order ASC",
                (task_id,),
            ).fetchall()
            approval_rows = conn.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
            log_rows = conn.execute(
                "SELECT * FROM execution_logs WHERE task_id = ? ORDER BY log_id ASC",
                (task_id,),
            ).fetchall()

        return self._deserialize_task(row, step_rows, approval_rows, log_rows)

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                tid = row["task_id"]
                step_rows = conn.execute(
                    "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_order ASC",
                    (tid,),
                ).fetchall()
                approval_rows = conn.execute(
                    "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at ASC",
                    (tid,),
                ).fetchall()
                log_rows = conn.execute(
                    "SELECT * FROM execution_logs WHERE task_id = ? ORDER BY log_id ASC",
                    (tid,),
                ).fetchall()
                results.append(self._deserialize_task(row, step_rows, approval_rows, log_rows))
            return results

    def clear_history(self) -> int:
        """Delete completed, failed, or cancelled tasks from history."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM tasks WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')"
            )
            conn.commit()
            return cur.rowcount

    # --- Approval Operations ---

    def create_approval_request(
        self,
        task_id: str,
        step_id: str,
        tool_name: str,
        action_summary: str,
        risk_level: str,
        reason: str,
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        approval_id = f"appr_{uuid.uuid4().hex[:10]}"
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, task_id, step_id, tool_name, action_summary,
                    risk_level, reason, parameters_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    approval_id,
                    task_id,
                    step_id,
                    tool_name,
                    action_summary,
                    risk_level,
                    reason,
                    json.dumps(redact_sensitive_data(parameters)),
                    now,
                ),
            )
            conn.commit()
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if not row:
                return None
            return self._deserialize_approval(row)

    def list_pending_approvals(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE task_id = ? AND status = 'PENDING' ORDER BY created_at ASC",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE status = 'PENDING' ORDER BY created_at ASC"
                ).fetchall()
            return [self._deserialize_approval(r) for r in rows]

    def resolve_approval(
        self, approval_id: str, approved: bool, resolution_note: str = ""
    ) -> Optional[Dict[str, Any]]:
        status = "APPROVED" if approved else "REJECTED"
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, resolved_at = ?, resolution_note = ?
                WHERE approval_id = ?
                """,
                (status, now, redact_sensitive_data(resolution_note), approval_id),
            )
            conn.commit()
        return self.get_approval(approval_id)

    # --- Logging Operations ---

    def add_log(
        self,
        task_id: str,
        phase: str,
        message: str,
        level: str = "INFO",
        step_id: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        safe_msg = redact_sensitive_data(message)
        safe_ev = redact_sensitive_data(evidence or {})
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO execution_logs (
                    task_id, step_id, phase, level, message, evidence_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, step_id, phase, level, safe_msg, json.dumps(safe_ev), now),
            )
            conn.commit()
            log_id = cur.lastrowid
        return {
            "log_id": log_id,
            "task_id": task_id,
            "step_id": step_id,
            "phase": phase,
            "level": level,
            "message": safe_msg,
            "evidence": safe_ev,
            "timestamp": now,
        }

    # --- Deserialization helpers ---

    @staticmethod
    def _deserialize_step(row: sqlite3.Row) -> Dict[str, Any]:
        raw_sid = str(row["step_id"])
        tid = str(row["task_id"])
        clean_sid = raw_sid[len(tid) + 1 :] if raw_sid.startswith(f"{tid}_") else raw_sid
        return {
            "step_id": clean_sid,
            "task_id": tid,
            "step_order": row["step_order"],
            "title": row["title"],
            "tool_name": row["tool_name"],
            "parameters": json.loads(row["parameters_json"] or "{}"),
            "depends_on": json.loads(row["depends_on_json"] or "[]"),
            "expected_condition": row["expected_condition"],
            "requires_approval": bool(row["requires_approval"]),
            "approval_reason": row["approval_reason"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "max_retries": row["max_retries"],
            "fallback_tool": row["fallback_tool"],
            "fallback_parameters": (
                json.loads(row["fallback_parameters_json"])
                if row["fallback_parameters_json"]
                else None
            ),
            "execution_output": json.loads(row["execution_output_json"] or "{}"),
            "verification": json.loads(row["verification_json"] or "{}"),
            "error": json.loads(row["error_json"] or "{}"),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _deserialize_approval(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "approval_id": row["approval_id"],
            "task_id": row["task_id"],
            "step_id": row["step_id"],
            "tool_name": row["tool_name"],
            "action_summary": row["action_summary"],
            "risk_level": row["risk_level"],
            "reason": row["reason"],
            "parameters": json.loads(row["parameters_json"] or "{}"),
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolution_note": row["resolution_note"],
        }

    @staticmethod
    def _deserialize_log(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "log_id": row["log_id"],
            "task_id": row["task_id"],
            "step_id": row["step_id"],
            "phase": row["phase"],
            "level": row["level"],
            "message": row["message"],
            "evidence": json.loads(row["evidence_json"] or "{}"),
            "timestamp": row["timestamp"],
        }

    def _deserialize_task(
        self,
        row: sqlite3.Row,
        step_rows: List[sqlite3.Row],
        approval_rows: List[sqlite3.Row],
        log_rows: List[sqlite3.Row],
    ) -> Dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "goal": row["goal"],
            "detected_language": row["detected_language"],
            "status": row["status"],
            "current_phase": row["current_phase"],
            "current_step_id": row["current_step_id"],
            "observation": json.loads(row["observation_json"] or "{}"),
            "plan": json.loads(row["plan_json"] or "[]"),
            "steps": [self._deserialize_step(s) for s in step_rows],
            "approvals": [self._deserialize_approval(a) for a in approval_rows],
            "logs": [self._deserialize_log(l) for l in log_rows],
            "final_report": row["final_report"],
            "error_summary": row["error_summary"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
