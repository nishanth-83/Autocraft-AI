"""
Integration Tests for AUTOCRAFT AI:
- Full Autonomous Execution Loop (OBSERVE -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER)
- Transient failure retry and bounded retry exhaustion
- Missing application fallback replanning (VS Code -> Notepad fallback)
- Destructive action approval gating (WAITING_APPROVAL -> Approve/Deny)
- Task cancellation & Emergency Stop controls
- Real Chromium Browser Automation against a live local HTTP server (open_url, search, click, scroll, read_content, fill_form, CAPTCHA block detection)
- FastAPI Backend & Built Frontend Dashboard integration
"""

from __future__ import annotations

import http.server
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

from backend.config import AgentConfig
from backend.main import create_app
from backend.security import PermissionLevel
from backend.tools.base import (
    PreconditionResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolExecutionResult,
    VerificationResult,
)
from backend.tools.browser import (
    PlaywrightBrowserSession,
    SeleniumChromiumBrowserSession,
    _validate_url,
    close_browser_session,
    get_browser_session,
)
from backend.tools.desktop import get_desktop_driver, set_desktop_driver
from tests.unit.test_desktop_mocked import MockWindowsDesktopDriver


TEST_HTML_INDEX = """<!DOCTYPE html>
<html>
<head><title>AUTOCRAFT Local Test Portal</title></head>
<body style="height: 2000px;">
  <h1>Welcome to AUTOCRAFT Test Portal</h1>
  <p id="intro">Real browser DOM rendering verification page.</p>
  <form id="search-form" action="/search" method="GET">
    <input type="search" name="q" id="search-input" value="" />
    <button type="submit" id="search-btn">Search</button>
  </form>
  <form id="profile-form">
    <input type="text" name="username" id="username" value="" />
    <input type="text" name="department" id="department" value="" />
  </form>
  <a href="/details" id="details-link">View More Details</a>
</body>
</html>
"""

TEST_HTML_DETAILS = """<!DOCTYPE html>
<html>
<head><title>Details Page - AUTOCRAFT</title></head>
<body>
  <h1>Detailed Specifications</h1>
  <p>Navigation via element click succeeded.</p>
</body>
</html>
"""

TEST_HTML_CAPTCHA = """<!DOCTYPE html>
<html>
<head><title>Security Challenge</title></head>
<body>
  <div class="g-recaptcha" data-sitekey="test">Verify you are human</div>
</body>
</html>
"""


class _LocalTestHttpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/details")
            self.end_headers()
            return
        if self.path.startswith("/details"):
            body = TEST_HTML_DETAILS.encode("utf-8")
        elif self.path.startswith("/captcha"):
            body = TEST_HTML_CAPTCHA.encode("utf-8")
        elif self.path.startswith("/search"):
            body = (
                "<html><head><title>Search Results</title></head>"
                "<body><h1>Search Results for Query</h1><p>Found 3 matching records.</p>"
                "<a data-autocraft-search-result href='/details'>First result</a></body></html>"
            ).encode("utf-8")
        else:
            body = TEST_HTML_INDEX.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestExecutionLoopAndApiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = socketserver.TCPServer(("0.0.0.0", 0), _LocalTestHttpHandler)
        cls.port = cls.httpd.server_address[1]
        # Use 127.0.0.2 loopback address so managed Chromium URLBlocklist on 127.0.0.1 does not interfere
        cls.base_url = f"http://127.0.0.2:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        close_browser_session()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        self.orig_desktop = get_desktop_driver()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name) / "workspace"
        self.config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root / "autocraft_test.db",
            max_step_retries=2,
        )
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        set_desktop_driver(self.orig_desktop)
        self.temp_dir.cleanup()

    def test_api_health_tools_and_plan_preview(self) -> None:
        with (
            patch.object(
                PlaywrightBrowserSession,
                "is_available",
                side_effect=AssertionError("health checks must not launch a browser"),
            ),
            patch.object(
                SeleniumChromiumBrowserSession,
                "is_available",
                side_effect=AssertionError("health checks must not launch a browser"),
            ),
        ):
            res = self.client.get("/api/health")
            observation_res = self.client.get("/api/observe")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "healthy")
        self.assertTrue(data["database"]["healthy"])
        self.assertGreaterEqual(data["registered_tool_count"], 24)
        self.assertEqual(observation_res.status_code, 200)
        self.assertFalse(observation_res.json()["browser_state"]["active"])

        tools_res = self.client.get("/api/tools")
        self.assertEqual(tools_res.status_code, 200)
        self.assertEqual(tools_res.json()["count"], data["registered_tool_count"])

        prev_res = self.client.post(
            "/api/plan/preview",
            json={"goal": 'create folder logs and then create file logs/app.txt with content "ready"'},
        )
        self.assertEqual(prev_res.status_code, 200)
        prev = prev_res.json()
        self.assertTrue(prev["validation"]["valid"])
        self.assertEqual(len(prev["plan"]["steps"]), 2)

        # Serve built frontend dashboard from /
        index_res = self.client.get("/")
        self.assertEqual(index_res.status_code, 200)
        self.assertIn("AUTOCRAFT AI", index_res.text)

    def test_api_health_reports_database_initialization_failure(self) -> None:
        invalid_config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root,
        )
        client = TestClient(create_app(invalid_config))
        try:
            response = client.get("/api/health")
        finally:
            client.close()

        self.assertEqual(response.status_code, 200)
        health = response.json()
        self.assertEqual(health["status"], "degraded")
        self.assertFalse(health["database"]["healthy"])
        self.assertIn("unable to open database file", health["database"]["error"])

    def test_approval_workflow_approve_and_reject(self) -> None:
        # 1. Create a file first
        r1 = self.client.post(
            "/api/tasks",
            json={"goal": 'create file secret_notes.txt with content "classify"'},
        )
        self.assertEqual(r1.json()["status"], "COMPLETED")
        self.assertTrue((self.ws_root / "secret_notes.txt").is_file())

        # 2. Request deletion -> Must pause at WAITING_APPROVAL
        r2 = self.client.post(
            "/api/tasks",
            json={"goal": "delete file secret_notes.txt"},
        )
        task_del = r2.json()
        self.assertEqual(task_del["status"], "WAITING_APPROVAL")
        self.assertTrue((self.ws_root / "secret_notes.txt").is_file())

        approvals = self.client.get("/api/approvals").json()["approvals"]
        self.assertEqual(len(approvals), 1)
        appr_id = approvals[0]["approval_id"]

        # 3. Deny approval -> Task transitions to FAILED and file remains untouched
        r_deny = self.client.post(
            f"/api/approvals/{appr_id}/resolve",
            json={"approved": False, "resolution_note": "Do not delete yet"},
        )
        self.assertEqual(r_deny.status_code, 200)
        self.assertEqual(r_deny.json()["task"]["status"], "FAILED")
        self.assertTrue((self.ws_root / "secret_notes.txt").is_file())

        # 4. Submit another delete request and APPROVE it -> File is deleted and verified
        r3 = self.client.post(
            "/api/tasks",
            json={"goal": "delete file secret_notes.txt"},
        )
        appr_id_2 = r3.json()["approvals"][0]["approval_id"]
        r_approve = self.client.post(
            f"/api/approvals/{appr_id_2}/resolve",
            json={"approved": True, "resolution_note": "Confirmed deletion"},
        )
        self.assertEqual(r_approve.status_code, 200)
        self.assertEqual(r_approve.json()["task"]["status"], "COMPLETED")
        self.assertFalse((self.ws_root / "secret_notes.txt").exists())

    def test_error_recovery_transient_retry_and_fallback_replan(self) -> None:
        # A. Test VS Code missing -> automatic fallback replanning to Notepad (with MockWindowsDesktopDriver)
        set_desktop_driver(MockWindowsDesktopDriver())
        res_fb = self.client.post(
            "/api/tasks",
            json={"goal": "open vscode at workspace"},
        )
        task_fb = res_fb.json()
        self.assertEqual(task_fb["status"], "COMPLETED")
        self.assertEqual(task_fb["steps"][0]["tool_name"], "desktop.notepad_write")
        self.assertTrue(task_fb["steps"][0]["verification"]["verified"])

        # B. Test transient error retry on custom flaky tool registered in registry
        orchestrator = self.app.state.orchestrator
        registry = self.app.state.registry
        call_counter = {"count": 0}

        def _flaky_exec(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
            call_counter["count"] += 1
            if call_counter["count"] == 1:
                return ToolExecutionResult(
                    success=False,
                    error_code="ACTION_TIMEOUT",
                    error_message="Simulated transient timeout on first attempt",
                    retryable=True,
                )
            return ToolExecutionResult(success=True, output={"attempts": call_counter["count"]})

        registry.register(
            ToolDefinition(
                name="system.flaky_test",
                category=ToolCategory.SYSTEM,
                description="Flaky test tool that succeeds on attempt 2",
                capabilities=["flaky_test"],
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"attempts": {"type": "integer"}}},
                permission_level=PermissionLevel.SAFE_READ,
                precondition_descriptions=[],
                verification_description="Verify attempts >= 2",
                preconditions_fn=lambda p, c: PreconditionResult(satisfied=True),
                execute_fn=_flaky_exec,
                verify_fn=lambda p, r, c: VerificationResult(
                    verified=r.output.get("attempts", 0) >= 2,
                    summary="Recovered on retry attempt 2",
                    evidence=r.output,
                ),
            )
        )

        from backend.planner.engine import PlanStep

        task_rec = orchestrator.store.create_task("flaky test goal")
        tid = task_rec["task_id"]
        step = PlanStep(
            step_id="step_1",
            step_order=1,
            title="Flaky step",
            tool_name="system.flaky_test",
            parameters={},
            max_retries=2,
        )
        orchestrator.store.save_task_steps(tid, [step.to_dict()])
        final_task = orchestrator._execute_plan_steps(tid, [step])
        self.assertEqual(final_task["status"], "COMPLETED")
        self.assertEqual(final_task["steps"][0]["attempt_count"], 2)

    def test_cancellation_and_emergency_stop(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingDesktopDriver(MockWindowsDesktopDriver):
            def notepad_write_and_read(self, text: str, save_path=None) -> Dict[str, Any]:
                started.set()
                release.wait(timeout=10)
                return super().notepad_write_and_read(text, save_path)

        set_desktop_driver(BlockingDesktopDriver())
        response = self.client.post(
            "/api/tasks",
            json={"goal": "Open Notepad and type stop-test", "run_async": True},
        )
        task_id = response.json()["task_id"]
        self.assertTrue(started.wait(timeout=5), "Task never entered its active desktop step.")
        cancel_res = self.client.post(
            f"/api/tasks/{task_id}/cancel",
            json={"reason": "User stopped the running task"},
        )
        self.assertEqual(cancel_res.status_code, 200)
        self.assertEqual(cancel_res.json()["status"], "CANCELLED")
        release.set()
        worker = self.app.state.orchestrator._active_threads[task_id]
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "Cancelled task worker did not stop.")
        cancelled = self.client.get(f"/api/tasks/{task_id}").json()
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["steps"][0]["status"], "CANCELLED")

        # Activate Emergency Stop
        started.clear()
        release.clear()
        response = self.client.post(
            "/api/tasks",
            json={"goal": "Open Notepad and type emergency-test", "run_async": True},
        )
        emergency_task_id = response.json()["task_id"]
        self.assertTrue(started.wait(timeout=5), "Emergency-stop task never entered its active step.")
        em_res = self.client.post("/api/emergency-stop")
        self.assertEqual(em_res.status_code, 200)
        self.assertTrue(em_res.json()["emergency_stop_active"])
        release.set()
        emergency_worker = self.app.state.orchestrator._active_threads[emergency_task_id]
        emergency_worker.join(timeout=5)
        self.assertFalse(emergency_worker.is_alive(), "Emergency-stopped task worker did not stop.")
        emergency_task = self.client.get(f"/api/tasks/{emergency_task_id}").json()
        self.assertEqual(emergency_task["status"], "CANCELLED")
        self.assertNotEqual(emergency_task["status"], "COMPLETED")

        # Starting a new task while Emergency Stop is active must return HTTP 409
        blocked_res = self.client.post("/api/tasks", json={"goal": "create folder blocked_dir"})
        self.assertEqual(blocked_res.status_code, 409)

        # Reset Emergency Stop and confirm task creation works again
        reset_res = self.client.post("/api/emergency-stop/reset")
        self.assertFalse(reset_res.json()["emergency_stop_active"])
        ok_res = self.client.post("/api/tasks", json={"goal": "create folder allowed_dir"})
        self.assertEqual(ok_res.status_code, 200)
        self.assertEqual(ok_res.json()["status"], "COMPLETED")

    def test_clear_history_removes_tasks(self) -> None:
        created = self.client.post(
            "/api/tasks",
            json={"goal": 'create file history.txt with content "history"'},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["status"], "COMPLETED")
        self.assertGreater(self.client.get("/api/tasks").json()["count"], 0)

        cleared = self.client.delete("/api/tasks/history")
        self.assertEqual(cleared.status_code, 200)
        self.assertGreater(cleared.json()["deleted_tasks"], 0)
        self.assertEqual(self.client.get("/api/tasks").json()["count"], 0)

    def test_execution_loop_does_not_complete_without_verified_steps(self) -> None:
        orchestrator = self.app.state.orchestrator
        task = orchestrator.store.create_task("empty persisted plan")
        result = orchestrator._execute_plan_steps(task["task_id"], [])
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("verified completion evidence", result["error_summary"])

    def test_execution_loop_fails_when_action_succeeds_but_verification_fails(self) -> None:
        registry = self.app.state.registry
        registry.register(
            ToolDefinition(
                name="system.unverified_test",
                category=ToolCategory.SYSTEM,
                description="Action succeeds but cannot be independently verified",
                capabilities=["unverified_test"],
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"action": {"type": "string"}}},
                permission_level=PermissionLevel.SAFE_READ,
                precondition_descriptions=[],
                verification_description="The verification result must be true",
                preconditions_fn=lambda p, c: PreconditionResult(satisfied=True),
                execute_fn=lambda p, c: ToolExecutionResult(
                    success=True, output={"action": "performed"}
                ),
                verify_fn=lambda p, r, c: VerificationResult(
                    verified=False,
                    summary="Action could not be confirmed",
                    failure_reason="Independent evidence is missing.",
                ),
            )
        )

        from backend.planner.engine import PlanStep

        orchestrator = self.app.state.orchestrator
        task = orchestrator.store.create_task("unverified action")
        step = PlanStep(
            step_id="step_1",
            step_order=1,
            title="Perform unverified action",
            tool_name="system.unverified_test",
            parameters={},
            max_retries=0,
        )
        orchestrator.store.save_task_steps(task["task_id"], [step.to_dict()])
        result = orchestrator._execute_plan_steps(task["task_id"], [step])

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["steps"][0]["status"], "FAILED")
        self.assertFalse(result["steps"][0]["verification"]["verified"])

    def test_browser_sessions_are_selected_by_requested_channel(self) -> None:
        close_browser_session()
        with patch.object(PlaywrightBrowserSession, "is_installed", return_value=False):
            edge_session = get_browser_session("msedge", headless=True)
        self.assertIsInstance(edge_session, SeleniumChromiumBrowserSession)
        self.assertEqual(edge_session.channel, "msedge")
        close_browser_session()

    def test_browser_url_validation_rejects_unsafe_or_malformed_urls(self) -> None:
        self.assertEqual(_validate_url("python.org"), "https://python.org")
        self.assertEqual(_validate_url("http://localhost:8000/"), "http://localhost:8000/")
        for url in (
            "",
            "https://",
            "ftp://python.org",
            "https://user:password@python.org",
            "https://python.org:invalid/",
            "https://python.org/path with spaces",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_url(url)

    def test_real_chromium_browser_automation_on_local_server(self) -> None:
        session = get_browser_session(
            self.config.browser_channel, self.config.browser_headless
        )
        if not session.is_available():
            self.skipTest("No local browser engine available")

        ctx = ToolContext(config=self.config, task_id="t_browser", step_id="s1")
        registry = self.app.state.registry

        # 1. Open URL in real browser
        _, ex_open, ver_open = registry.dispatch(
            "browser.open_url", {"url": f"{self.base_url}/"}, ctx
        )
        self.assertTrue(ex_open.success, f"Browser open failed: {ex_open.error_message}")
        self.assertTrue(ver_open.verified)
        self.assertEqual(ex_open.output["title"], "AUTOCRAFT Local Test Portal")

        _, ex_redirect, ver_redirect = registry.dispatch(
            "browser.open_url", {"url": f"{self.base_url}/redirect"}, ctx
        )
        self.assertTrue(ex_redirect.success, ex_redirect.error_message)
        self.assertTrue(ver_redirect.verified, ver_redirect.failure_reason)
        self.assertTrue(ex_redirect.output["current_url"].endswith("/details"))
        self.assertEqual(ex_redirect.output["title"], "Details Page - AUTOCRAFT")

        registry.dispatch("browser.open_url", {"url": f"{self.base_url}/"}, ctx)

        # 2. Read visible page content
        _, ex_read, ver_read = registry.dispatch("browser.read_content", {}, ctx)
        self.assertTrue(ver_read.verified)
        self.assertIn("Welcome to AUTOCRAFT Test Portal", ex_read.output["visible_text"])

        # 3. Scroll page and verify scrollY change
        _, ex_scroll, ver_scroll = registry.dispatch(
            "browser.scroll", {"direction": "down", "amount": 400}, ctx
        )
        self.assertTrue(ver_scroll.verified)
        self.assertGreater(ex_scroll.output["scroll_y_after"], ex_scroll.output["scroll_y_before"])

        # 4. Fill form fields and verify DOM input values
        _, ex_form, ver_form = registry.dispatch(
            "browser.fill_form",
            {
                "fields": {"#username": "jarvis_operator", "#department": "robotics"},
                "submit": False,
            },
            ctx,
        )
        self.assertTrue(ver_form.verified)
        self.assertEqual(
            ex_form.output["verified_values"]["#username"], "jarvis_operator"
        )

        # 5. Search using page search input
        _, ex_search, ver_search = registry.dispatch(
            "browser.search",
            {"query": "autonomous verification", "search_url": f"{self.base_url}/"},
            ctx,
        )
        self.assertTrue(ver_search.verified)
        self.assertIn("Search Results", ex_search.output["title"])

        # 6. Select the first visible, non-ad result from the rendered results page.
        _, ex_first_result, ver_first_result = registry.dispatch(
            "browser.click", {"selector": "the first result"}, ctx
        )
        self.assertTrue(ver_first_result.verified)
        self.assertTrue(ex_first_result.output["current_url"].endswith("/details"))

        # 7. Click accessible link element
        registry.dispatch("browser.open_url", {"url": f"{self.base_url}/"}, ctx)
        _, ex_click, ver_click = registry.dispatch(
            "browser.click", {"selector": "#details-link"}, ctx
        )
        self.assertTrue(ver_click.verified)
        self.assertIn("/details", ex_click.output["current_url"])

        # 7. CAPTCHA protection detection -> MUST refuse to bypass and return SECURITY_CAPTCHA_OR_AUTH_BLOCKED
        _, ex_cap, ver_cap = registry.dispatch(
            "browser.open_url", {"url": f"{self.base_url}/captcha"}, ctx
        )
        self.assertFalse(ex_cap.success)
        self.assertFalse(ver_cap.verified)
        self.assertEqual(ex_cap.error_code, "SECURITY_CAPTCHA_OR_AUTH_BLOCKED")

    def test_browser_session_is_safe_across_request_threads(self) -> None:
        session = PlaywrightBrowserSession(channel="chromium", headless=True)
        if not session.is_available():
            error = session.availability_error
            session.close()
            self.skipTest(f"Playwright browser could not initialize: {error}")

        try:
            result = session.open_url(f"{self.base_url}/")
            states: List[Dict[str, Any]] = []
            worker = threading.Thread(target=lambda: states.append(session.current_state()))
            worker.start()
            worker.join()
        finally:
            session.close()

        self.assertEqual(result["title"], "AUTOCRAFT Local Test Portal")
        self.assertEqual(len(states), 1)
        self.assertTrue(states[0]["active"])
        self.assertEqual(states[0]["title"], "AUTOCRAFT Local Test Portal")


if __name__ == "__main__":
    unittest.main()
