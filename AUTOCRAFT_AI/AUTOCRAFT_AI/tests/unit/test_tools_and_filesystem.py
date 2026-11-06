"""
Unit tests for ToolRegistry, Filesystem Tools, Path Confinement,
Overwrite/Delete Approval Enforcement, and Sensitive Data Redaction.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config import AgentConfig
from backend.main import build_default_registry
from backend.security import (
    SecurityViolationError,
    redact_sensitive_data,
    resolve_authorized_path,
    validate_command_safety,
)
from backend.tools.base import ToolContext
from backend.tools.registry import ToolNotFoundError


class TestToolsAndFilesystem(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name) / "workspace"
        self.config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root / "test.db",
        )
        self.registry = build_default_registry()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _ctx(self, approved: bool = False) -> ToolContext:
        return ToolContext(
            config=self.config,
            task_id="task_unit",
            step_id="step_1",
            approved=approved,
        )

    def test_tool_registry_lookup_and_capabilities(self) -> None:
        all_tools = self.registry.list_tools()
        self.assertGreaterEqual(len(all_tools), 20)
        self.assertTrue(self.registry.has_tool("fs.create_file"))
        self.assertTrue(self.registry.has_tool("desktop.notepad_write"))
        self.assertTrue(self.registry.has_tool("browser.open_url"))

        calc_tools = self.registry.find_by_capability("calculator")
        self.assertEqual(len(calc_tools), 1)
        self.assertEqual(calc_tools[0].name, "desktop.calculator_compute")

        with self.assertRaises(ToolNotFoundError):
            self.registry.get("nonexistent.tool")

    def test_tool_registry_rejects_duplicate_registration(self) -> None:
        tool = self.registry.get("fs.create_file")
        original_count = len(self.registry.list_tools())

        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register(tool)

        self.assertEqual(len(self.registry.list_tools()), original_count)

    def test_disabled_desktop_tools_are_not_advertised_as_supported(self) -> None:
        disabled_config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root / "disabled.db",
            enable_live_desktop_automation=False,
        )
        registry = build_default_registry(disabled_config)
        metadata = {tool.name: tool.to_metadata_dict() for tool in registry.list_tools()}

        self.assertFalse(metadata["desktop.notepad_write"]["supported_on_current_platform"])

        precondition, execution, _ = registry.dispatch(
            "desktop.notepad_write",
            {"text": "must not be typed"},
            ToolContext(
                config=disabled_config,
                task_id="task_disabled",
                step_id="step_disabled",
            ),
        )
        self.assertFalse(precondition.satisfied)
        self.assertFalse(execution.success)
        self.assertEqual(execution.error_code, "DESKTOP_AUTOMATION_DISABLED")

    def test_filesystem_lifecycle_and_verification(self) -> None:
        ctx = self._ctx(approved=False)

        # 1. Create folder
        pre, ex, ver = self.registry.dispatch("fs.create_folder", {"path": "docs"}, ctx)
        self.assertTrue(pre.satisfied)
        self.assertTrue(ex.success)
        self.assertTrue(ver.verified)
        self.assertTrue((self.ws_root / "docs").is_dir())

        # 2. Create file and verify SHA-256 readback
        pre, ex, ver = self.registry.dispatch(
            "fs.create_file",
            {"path": "docs/hello.txt", "content": "Hello AUTOCRAFT"},
            ctx,
        )
        self.assertTrue(ex.success)
        self.assertTrue(ver.verified)
        self.assertEqual(ver.evidence["size_bytes"], 15)

        # 3. Attempt to overwrite existing file without approval -> MUST FAIL precondition
        pre_ov, ex_ov, ver_ov = self.registry.dispatch(
            "fs.create_file",
            {"path": "docs/hello.txt", "content": "Overwritten without permission"},
            ctx,
        )
        self.assertFalse(pre_ov.satisfied)
        self.assertFalse(ex_ov.success)
        self.assertEqual(ex_ov.error_code, "OVERWRITE_APPROVAL_REQUIRED")
        self.assertEqual((self.ws_root / "docs/hello.txt").read_text("utf-8"), "Hello AUTOCRAFT")

        # 4. Append to file
        pre, ex, ver = self.registry.dispatch(
            "fs.append_file",
            {"path": "docs/hello.txt", "content": " - v2"},
            ctx,
        )
        self.assertTrue(ver.verified)

        # 5. Read file
        pre, ex, ver = self.registry.dispatch("fs.read_file", {"path": "docs/hello.txt"}, ctx)
        self.assertTrue(ver.verified)
        self.assertEqual(ex.output["content"], "Hello AUTOCRAFT - v2")

        # 6. Copy file
        pre, ex, ver = self.registry.dispatch(
            "fs.copy_file",
            {"source": "docs/hello.txt", "destination": "docs/hello_copy.txt"},
            ctx,
        )
        self.assertTrue(ver.verified)
        self.assertEqual(ver.evidence["source_sha256"], ver.evidence["destination_sha256"])

        # 7. Rename/move file
        pre, ex, ver = self.registry.dispatch(
            "fs.move_or_rename",
            {"source": "docs/hello_copy.txt", "destination": "docs/hello_renamed.txt"},
            ctx,
        )
        self.assertTrue(ver.verified)
        self.assertFalse((self.ws_root / "docs/hello_copy.txt").exists())
        self.assertTrue((self.ws_root / "docs/hello_renamed.txt").is_file())

        # 8. Organize folder
        (self.ws_root / "messy").mkdir()
        (self.ws_root / "messy/report.pdf").write_bytes(b"%PDF-1.4")
        (self.ws_root / "messy/photo.png").write_bytes(b"PNGDATA")
        (self.ws_root / "messy/script.py").write_text("print(1)", encoding="utf-8")
        pre, ex, ver = self.registry.dispatch("fs.organize_folder", {"path": "messy"}, ctx)
        self.assertTrue(ver.verified)
        self.assertEqual(ex.output["moved_count"], 3)
        self.assertTrue((self.ws_root / "messy/Documents/report.pdf").is_file())
        self.assertTrue((self.ws_root / "messy/Images/photo.png").is_file())
        self.assertTrue((self.ws_root / "messy/Code/script.py").is_file())

        # 9. Delete file without approval -> MUST FAIL precondition
        pre_del, ex_del, ver_del = self.registry.dispatch(
            "fs.delete_path", {"path": "docs/hello_renamed.txt"}, self._ctx(approved=False)
        )
        self.assertFalse(pre_del.satisfied)
        self.assertEqual(ex_del.error_code, "APPROVAL_REQUIRED")
        self.assertTrue((self.ws_root / "docs/hello_renamed.txt").exists())

        # 10. Delete file WITH approval -> succeeds and verifies removal
        pre_del2, ex_del2, ver_del2 = self.registry.dispatch(
            "fs.delete_path", {"path": "docs/hello_renamed.txt"}, self._ctx(approved=True)
        )
        self.assertTrue(ver_del2.verified)
        self.assertFalse((self.ws_root / "docs/hello_renamed.txt").exists())

    def test_filesystem_saves_verified_prior_step_output_to_downloads(self) -> None:
        summary = (
            "Wikipedia article research for: cybersecurity\n"
            "Title: Computer security\n"
            "URL: https://en.wikipedia.org/wiki/Computer_security\n"
            "Relevant content: Computer security protects computer systems and networks."
        )
        ctx = self._ctx()
        ctx.step_results["step_2"] = {"summary": summary}
        with tempfile.TemporaryDirectory() as downloads:
            downloads_path = Path(downloads) / "Downloads"
            self.assertFalse(downloads_path.exists())
            with patch(
                "backend.tools.desktop._downloads_directory",
                return_value=downloads_path,
            ):
                pre, execution, verification = self.registry.dispatch(
                    "fs.create_file",
                    {
                        "path": "Downloads\\cybersecurity_research.txt",
                        "content": "",
                        "content_from_step": {
                            "step_id": "step_2",
                            "output_key": "summary",
                        },
                    },
                    ctx,
                )
            saved_file = downloads_path / "cybersecurity_research.txt"
            self.assertTrue(pre.satisfied, pre.reason)
            self.assertTrue(execution.success, execution.error_message)
            self.assertTrue(verification.verified, verification.failure_reason)
            self.assertTrue(downloads_path.is_dir())
            self.assertEqual(saved_file.read_text(encoding="utf-8"), summary)
            self.assertIn("Computer security protects computer systems", saved_file.read_text(encoding="utf-8"))

    def test_security_confinement_and_secret_redaction(self) -> None:
        # Block path traversal
        with self.assertRaises(SecurityViolationError):
            resolve_authorized_path("../outside.txt", self.ws_root)

        # Block credential filenames
        with self.assertRaises(SecurityViolationError):
            resolve_authorized_path(".env", self.ws_root)
        with self.assertRaises(SecurityViolationError):
            resolve_authorized_path("keys/id_rsa", self.ws_root)

        # Block destructive commands
        with self.assertRaises(SecurityViolationError):
            validate_command_safety("rm -rf /")
        with self.assertRaises(SecurityViolationError):
            validate_command_safety("format C: /q")

        # Sensitive data redaction
        raw = {
            "username": "arun",
            "user_password": "SuperSecretPassword123!",
            "otp_code": "928374",
            "nested": {"api_key": "sk-live-99999999"},
            "log": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        }
        redacted = redact_sensitive_data(raw)
        self.assertEqual(redacted["username"], "arun")
        self.assertEqual(redacted["user_password"], "[REDACTED]")
        self.assertEqual(redacted["otp_code"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["api_key"], "[REDACTED]")
        self.assertIn("[REDACTED_TOKEN]", redacted["log"])


if __name__ == "__main__":
    unittest.main()
