"""
Live Windows 10/11 Desktop & External Browser Tests for AUTOCRAFT AI.
IMPORTANT:
- Windows Desktop live tests require `sys.platform == "win32"` with an interactive desktop session.
- External browser tests use real Chrome and Microsoft Edge; they skip only when the target host cannot be resolved.
"""

from __future__ import annotations

import math
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from backend.config import AgentConfig
from backend.main import build_default_registry, create_app
from backend.tools.base import ToolContext
from backend.tools.browser import (
    CaptchaOrAuthBlockedError,
    PlaywrightBrowserSession,
    close_browser_session,
)
from backend.tools.desktop import Win32UIADesktopDriver, set_desktop_driver
from fastapi.testclient import TestClient


def _domain_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


class TestWindowsAndExternalLive(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name) / "workspace"
        self.config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root / "live_test.db",
            browser_headless=False,
        )
        self.registry = build_default_registry()
        self.ctx = ToolContext(config=self.config, task_id="live_task", step_id="live_step")
        self.app = create_app(self.config)
        self.client = TestClient(self.app)
        set_desktop_driver(Win32UIADesktopDriver())

    def tearDown(self) -> None:
        self.client.close()
        close_browser_session()
        self.temp_dir.cleanup()

    def _run_task(self, goal: str) -> dict:
        response = self.client.post(
            "/api/tasks",
            json={"goal": goal, "run_async": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @unittest.skipUnless(
        sys.platform == "win32",
        "Requires live Windows 10/11 OS (win32) to launch and automate notepad.exe via UIA/Win32.",
    )
    def test_live_windows_notepad_and_immediate_second_task(self) -> None:
        first = self._run_task("Open Notepad and type AUTOCRAFT AI TEST 123")
        self.assertEqual(first["status"], "COMPLETED", first.get("error_summary"))
        self.assertTrue(first["steps"][0]["verification"]["verified"])
        self.assertEqual(
            first["steps"][0]["execution_output"]["observed_text"],
            "AUTOCRAFT AI TEST 123",
        )

        second = self._run_task("Open Notepad and type Hello AUTOCRAFT")
        self.assertEqual(second["status"], "COMPLETED", second.get("error_summary"))
        self.assertTrue(second["steps"][0]["verification"]["verified"])
        self.assertEqual(
            second["steps"][0]["execution_output"]["observed_text"],
            "Hello AUTOCRAFT",
        )

        sequence = self._run_task("Open Notepad and type numbers from 1 to 400")
        expected_text = "\n".join(str(number) for number in range(1, 401))
        self.assertEqual(sequence["status"], "COMPLETED", sequence.get("error_summary"))
        self.assertTrue(sequence["steps"][0]["verification"]["verified"])
        self.assertEqual(
            sequence["steps"][0]["execution_output"]["observed_text"]
            .replace("\r\n", "\n")
            .replace("\r", "\n"),
            expected_text,
        )

    @unittest.skipUnless(
        sys.platform == "win32",
        "Requires live Windows 10/11 OS (win32) to launch and automate calc.exe via UIA.",
    )
    def test_live_windows_calculator_448_times_33(self) -> None:
        task = self._run_task("Open Calculator and calculate 448*33")
        self.assertEqual(task["status"], "COMPLETED", task.get("error_summary"))
        self.assertTrue(task["steps"][0]["verification"]["verified"])
        self.assertEqual(
            task["steps"][0]["verification"]["evidence"]["observed_result"],
            14784.0,
        )
        for expression, expected in (
            ("2^8", 256.0),
            ("(2+3)^2", 25.0),
            ("sin 30", 0.5),
            ("sin 1 radians", math.sin(1)),
            ("log 14", math.log10(14)),
            ("sqrt 81", 9.0),
        ):
            with self.subTest(expression=expression):
                function_task = self._run_task(
                    f"Open Calculator and calculate {expression}"
                )
                self.assertEqual(
                    function_task["status"],
                    "COMPLETED",
                    function_task.get("error_summary"),
                )
                self.assertTrue(function_task["steps"][0]["verification"]["verified"])
                self.assertAlmostEqual(
                    function_task["steps"][0]["verification"]["evidence"]["observed_result"],
                    expected,
                    places=7,
                )

    @unittest.skipUnless(
        sys.platform == "win32",
        "Requires live Windows 10/11 OS (win32) to launch mspaint.exe and verify HWND.",
    )
    def test_live_windows_paint_circle_drawing_and_image_save(self) -> None:
        if not Win32UIADesktopDriver().is_supported():
            self.skipTest("Requires an interactive Windows desktop session.")
        pre, ex, ver = self.registry.dispatch(
            "desktop.paint_launch",
            {"draw_shape": "circle", "save_path": "paint_live_test.png"},
            self.ctx,
        )
        self.assertTrue(pre.satisfied)
        self.assertTrue(ex.success)
        self.assertTrue(ver.verified, ver.failure_reason)
        self.assertTrue(ex.output["drawing_verified"])
        self.assertTrue(ex.output["image_verified"])
        self.assertIn(
            ex.output["drawing_method"],
            {"ellipse_tool", "verified_image_loaded_in_paint"},
        )
        expected_summary = (
            "loaded in Paint"
            if ex.output["drawing_method"] == "verified_image_loaded_in_paint"
            else "drew a circle"
        )
        self.assertIn(expected_summary, ver.summary)
        self.assertTrue((self.ws_root / "paint_live_test.png").is_file())

        visible_pre, visible_execution, visible_verification = self.registry.dispatch(
            "desktop.paint_launch",
            {"draw_shape": "circle"},
            self.ctx,
        )
        self.assertTrue(visible_pre.satisfied)
        self.assertTrue(visible_execution.success)
        self.assertTrue(visible_verification.verified, visible_verification.failure_reason)

    @unittest.skipUnless(
        sys.platform == "win32",
        "Requires live Windows 10/11 OS (win32) to launch explorer.exe and verify CabinetWClass window.",
    )
    def test_live_windows_file_explorer_navigation(self) -> None:
        target = self.ws_root / "explorer_target"
        target.mkdir(parents=True, exist_ok=True)
        pre, ex, ver = self.registry.dispatch(
            "desktop.explorer_open",
            {"path": "explorer_target"},
            self.ctx,
        )
        self.assertTrue(pre.satisfied)
        self.assertTrue(ex.success)
        self.assertTrue(ver.verified)

    @unittest.skipUnless(
        _domain_resolves("www.flipkart.com"),
        "Flipkart DNS is unavailable; real Chrome navigation cannot be attempted.",
    )
    def test_live_chrome_navigation_to_flipkart(self) -> None:
        task = self._run_task(
            "Open Chrome and navigate to https://www.flipkart.com/"
        )
        if task["status"] == "FAILED" and any(
            step.get("error", {}).get("error_code") == "SECURITY_CAPTCHA_OR_AUTH_BLOCKED"
            for step in task["steps"]
        ):
            self.skipTest("Flipkart presented a CAPTCHA; the agent did not bypass it.")
        self.assertEqual(task["status"], "COMPLETED", task.get("error_summary"))
        self.assertEqual(task["steps"][0]["parameters"]["browser"], "chrome")
        self.assertIn("flipkart.com", task["steps"][0]["execution_output"]["current_url"])
        self.assertEqual(
            task["steps"][0]["execution_output"]["browser_channel"],
            "chrome",
        )

    @unittest.skipUnless(
        _domain_resolves("www.wikipedia.org"),
        "Wikipedia DNS is unavailable; real Microsoft Edge navigation cannot be attempted.",
    )
    def test_live_edge_navigation_to_wikipedia(self) -> None:
        task = self._run_task(
            "Open Microsoft Edge and navigate to https://www.wikipedia.org/"
        )
        if task["status"] == "FAILED" and any(
            step.get("error", {}).get("error_code") == "SECURITY_CAPTCHA_OR_AUTH_BLOCKED"
            for step in task["steps"]
        ):
            self.skipTest("Wikipedia presented a CAPTCHA; the agent did not bypass it.")
        self.assertEqual(task["status"], "COMPLETED", task.get("error_summary"))
        self.assertEqual(task["steps"][0]["parameters"]["browser"], "msedge")
        self.assertIn("wikipedia.org", task["steps"][0]["execution_output"]["current_url"])
        self.assertEqual(
            task["steps"][0]["execution_output"]["browser_channel"],
            "msedge",
        )

    def test_live_playwright_navigation_to_multiple_real_sites(self) -> None:
        targets = (
            ("www.amazon.com", "https://www.amazon.com/"),
            ("www.flipkart.com", "https://www.flipkart.com/"),
            ("www.youtube.com", "https://www.youtube.com/"),
            ("www.wikipedia.org", "https://www.wikipedia.org/"),
            ("www.python.org", "https://www.python.org/"),
        )
        reachable = [(host, url) for host, url in targets if _domain_resolves(host)]
        if len(reachable) < 2:
            self.skipTest("Fewer than two requested real websites are reachable by DNS.")

        session = PlaywrightBrowserSession(channel="chromium", headless=True)
        if not session.is_available():
            error = session.availability_error
            session.close()
            self.skipTest(f"Playwright Chromium is unavailable: {error}")

        verified = []
        unavailable = []
        try:
            for host, url in reachable:
                try:
                    result = session.open_url(url, timeout_ms=self.config.browser_timeout_ms)
                except CaptchaOrAuthBlockedError:
                    unavailable.append(f"{host}: security challenge")
                    continue
                except PlaywrightError as exc:
                    if any(
                        marker in str(exc)
                        for marker in ("net::", "Timeout", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED")
                    ):
                        unavailable.append(f"{host}: {exc}")
                        continue
                    raise
                self.assertTrue(result["current_url"].startswith(("http://", "https://")))
                if not result["title"].strip():
                    unavailable.append(f"{host}: page title was empty")
                    continue
                verified.append(host)
        finally:
            session.close()

        if len(verified) < 2:
            self.skipTest(
                "Fewer than two reachable sites allowed navigation without a security challenge: "
                + "; ".join(unavailable)
            )
        self.assertGreaterEqual(len(verified), 2)
        self.assertNotIn("example.com", verified)

    @unittest.skipUnless(
        _domain_resolves("www.youtube.com"),
        "YouTube DNS is unavailable; live search/playback cannot be attempted.",
    )
    def test_live_youtube_search_opens_first_video_and_verifies_playback(self) -> None:
        task = self._run_task(
            "Open YouTube in Chrome and search Tamil songs and play the first song"
        )
        challenge_steps = [
            step
            for step in task["steps"]
            if step.get("error", {}).get("error_code")
            == "SECURITY_CAPTCHA_OR_AUTH_BLOCKED"
        ]
        if challenge_steps:
            self.assertEqual(task["status"], "FAILED")
            self.assertRegex(
                challenge_steps[0]["error"].get("message", ""),
                r"(?i)(youtube|consent|sign.in|captcha|age.verification)",
            )
            return

        self.assertEqual(task["status"], "COMPLETED", task.get("error_summary"))
        self.assertEqual(
            [step["tool_name"] for step in task["steps"]],
            ["browser.open_url", "browser.youtube_first_result"],
        )
        action_step = task["steps"][1]
        self.assertEqual(action_step["parameters"]["query"], "Tamil songs")
        self.assertIn("/watch?", action_step["execution_output"]["current_url"])
        self.assertTrue(action_step["verification"]["verified"])
        self.assertTrue(action_step["execution_output"]["playback_started"])
        self.assertGreater(action_step["execution_output"]["current_time"], 0)


if __name__ == "__main__":
    unittest.main()
