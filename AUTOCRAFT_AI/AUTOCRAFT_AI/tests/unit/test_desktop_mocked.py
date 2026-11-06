"""
Mocked Windows Desktop Automation Tests for AUTOCRAFT AI.
Clearly separated from live Windows UIA tests:
- Uses an explicit MockWindowsDesktopDriver to test Notepad, Calculator, Paint,
  File Explorer, VS Code, Office, and UIA control verification logic.
- Also verifies that the production Win32UIADesktopDriver never fabricates success
  on non-Windows hosts.
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from PIL import Image, ImageDraw

from backend.config import AgentConfig
from backend.main import build_default_registry
from backend.tools.base import ToolContext
from backend.tools.desktop import (
    DesktopAutomationDriver,
    Win32UIADesktopDriver,
    _create_paint_fallback_image,
    _image_has_visible_drawing,
    evaluate_math_expression_safely,
    get_desktop_driver,
    _select_calculator_window,
    set_desktop_driver,
)


class MockWindowsDesktopDriver(DesktopAutomationDriver):
    """Deterministic mock driver for unit testing desktop tool dispatch and verification."""

    def __init__(
        self,
        tamper_notepad: bool = False,
        tamper_calc: bool = False,
        tamper_paint: bool = False,
    ) -> None:
        self.tamper_notepad = tamper_notepad
        self.tamper_calc = tamper_calc
        self.tamper_paint = tamper_paint

    def is_supported(self) -> bool:
        return True

    def discover_applications(self) -> Dict[str, Dict[str, Any]]:
        return {
            "notepad": {"available": True, "executable": r"C:\Windows\System32\notepad.exe"},
            "calculator": {"available": True, "executable": r"C:\Windows\System32\calc.exe"},
            "paint": {"available": True, "executable": r"C:\Windows\System32\mspaint.exe"},
            "explorer": {"available": True, "executable": r"C:\Windows\explorer.exe"},
        }

    def list_open_windows(self) -> List[Dict[str, Any]]:
        return [{"hwnd": 1001, "title": "Untitled - Notepad", "class_name": "Notepad", "pid": 4200}]

    def open_notepad(self) -> Dict[str, Any]:
        return {
            "app": "notepad.exe",
            "pid": 4200,
            "hwnd": 1001,
            "window_title": "Untitled - Notepad",
        }

    def notepad_write_and_read(
        self, text: str, save_path: Optional[Path] = None
    ) -> Dict[str, Any]:
        observed = "CORRUPTED_TEXT" if self.tamper_notepad else text
        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(observed, encoding="utf-8")
        return {
            "app": "notepad.exe",
            "pid": 4200,
            "hwnd": 1001,
            "entered_text": text,
            "observed_text": observed,
            "saved_path": str(save_path) if save_path else None,
            "saved_verified": save_path is not None and save_path.is_file(),
        }

    def calculator_compute(
        self, expression: str, angle_mode: str = "degrees"
    ) -> Dict[str, Any]:
        expected = evaluate_math_expression_safely(expression, angle_mode)
        observed = expected + 999.0 if self.tamper_calc else expected
        return {
            "app": "calc.exe",
            "expression": expression,
            "raw_display": f"Display is {observed}",
            "observed_result": observed,
            "expected_result": expected,
            "angle_mode": angle_mode,
        }

    def open_calculator(self) -> Dict[str, Any]:
        return {
            "app": "calc.exe",
            "pid": 4300,
            "hwnd": 1002,
            "window_title": "Calculator",
        }

    def launch_paint(
        self,
        draw_box: bool = False,
        draw_shape: Optional[str] = None,
        save_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        shape = draw_shape or ("rectangle" if draw_box else None)
        if save_path is not None and shape:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            if shape in ("triangle", "house", "landscape"):
                _create_paint_fallback_image(save_path, shape)
            else:
                image = Image.new("RGB", (200, 200), "white")
                drawing = ImageDraw.Draw(image)
                if shape in ("circle", "ellipse"):
                    drawing.ellipse((40, 40, 160, 160), outline="black", width=4)
                else:
                    drawing.rectangle((40, 40, 160, 160), outline="black", width=4)
                image.save(save_path)
            with Image.open(save_path) as saved_image:
                saved_image.load()
                image_verified = _image_has_visible_drawing(saved_image, shape)
        else:
            image_verified = False
        return {
            "app": "mspaint.exe",
            "pid": 5100,
            "hwnd": 2002,
            "window_title": "Untitled - Paint",
            "draw_box_executed": shape == "rectangle",
            "draw_shape": shape,
            "drawing_verified": shape is not None and not self.tamper_paint and (
                save_path is None or image_verified
            ),
            "saved_path": str(save_path) if save_path else None,
            "saved_verified": bool(save_path and save_path.is_file()),
            "image_verified": image_verified,
        }

    def open_file_explorer(self, folder_path: Path) -> Dict[str, Any]:
        return {
            "app": "explorer.exe",
            "pid": 6100,
            "target_path": str(folder_path),
            "path_exists": folder_path.is_dir(),
            "window_detected": True,
        }

    def launch_vscode(self, target_path: Optional[Path] = None) -> Dict[str, Any]:
        raise FileNotFoundError("VS Code (Code.exe) is not installed on this mock system.")

    def launch_office_app(self, app_name: str, file_path: Optional[Path] = None) -> Dict[str, Any]:
        return {"app": app_name, "executable": f"{app_name}.exe", "pid": 7100}

    def interact_control(
        self,
        window_title: str,
        control_name: str,
        control_type: str,
        action: str,
        value: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "window_title": window_title,
            "control_name": control_name,
            "control_type": control_type,
            "action": action,
            "control_text_after": value or "OK",
        }

    def perform_input_action(
        self,
        action_type: str,
        keys: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> Dict[str, Any]:
        return {"action_type": action_type, "foreground_hwnd": 1001}


class TestDesktopToolsMocked(unittest.TestCase):
    def setUp(self) -> None:
        self.original_driver = get_desktop_driver()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name) / "workspace"
        self.config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root / "test.db",
        )
        self.registry = build_default_registry()
        self.ctx = ToolContext(config=self.config, task_id="t1", step_id="s1")

    def tearDown(self) -> None:
        set_desktop_driver(self.original_driver)
        self.temp_dir.cleanup()

    def test_notepad_mocked_success_and_verification_failure_detection(self) -> None:
        # 1. Valid Notepad write + readback verification
        set_desktop_driver(MockWindowsDesktopDriver(tamper_notepad=False))
        pre, ex, ver = self.registry.dispatch(
            "desktop.notepad_write",
            {"text": "Verified Notepad Entry", "save_path": "note_out.txt"},
            self.ctx,
        )
        self.assertTrue(pre.satisfied)
        self.assertTrue(ex.success)
        self.assertTrue(ver.verified)
        self.assertTrue((self.ws_root / "note_out.txt").is_file())

        # 2. Tampered Notepad readback -> Verification MUST fail (never fabricates success)
        set_desktop_driver(MockWindowsDesktopDriver(tamper_notepad=True))
        pre2, ex2, ver2 = self.registry.dispatch(
            "desktop.notepad_write",
            {"text": "Expected Text"},
            self.ctx,
        )
        self.assertFalse(ver2.verified)
        self.assertFalse(ex2.success)
        self.assertEqual(ex2.error_code, "VERIFICATION_FAILED")

    def test_notepad_preserves_generated_multiline_sequence(self) -> None:
        text = "\n".join(str(number) for number in range(1, 401))
        set_desktop_driver(MockWindowsDesktopDriver())
        _, execution, verification = self.registry.dispatch(
            "desktop.notepad_write",
            {"text": text, "save_path": "numbers.txt"},
            self.ctx,
        )
        self.assertTrue(verification.verified)
        self.assertTrue(execution.success)
        self.assertEqual(execution.output["observed_text"], text)
        self.assertEqual(
            (self.ws_root / "numbers.txt").read_text(encoding="utf-8"),
            text,
        )

    def test_notepad_saves_and_verifies_content_in_downloads(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver())
        with tempfile.TemporaryDirectory() as downloads:
            downloads_path = Path(downloads)
            with patch(
                "backend.tools.desktop._downloads_directory",
                return_value=downloads_path,
            ):
                _, execution, verification = self.registry.dispatch(
                    "desktop.notepad_write",
                    {
                        "text": "abcdefghijklmnopqrstuvwxyz",
                        "save_path": "Downloads\\alphabet.txt",
                    },
                    self.ctx,
                )
            saved_file = downloads_path / "alphabet.txt"
            self.assertTrue(execution.success)
            self.assertTrue(verification.verified, verification.failure_reason)
            self.assertTrue(saved_file.is_file())
            self.assertEqual(
                saved_file.read_text(encoding="utf-8"),
                "abcdefghijklmnopqrstuvwxyz",
            )

    def test_calculator_mocked_success_and_mismatch_detection(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver(tamper_calc=False))
        pre, ex, ver = self.registry.dispatch(
            "desktop.calculator_compute",
            {"expression": "125 + 375"},
            self.ctx,
        )
        self.assertTrue(ver.verified)
        self.assertEqual(ver.evidence["observed_result"], 500.0)

        # Tampered Calculator display -> Verification MUST fail
        set_desktop_driver(MockWindowsDesktopDriver(tamper_calc=True))
        pre2, ex2, ver2 = self.registry.dispatch(
            "desktop.calculator_compute",
            {"expression": "10 * 5"},
            self.ctx,
        )
        self.assertFalse(ver2.verified)
        self.assertFalse(ex2.success)

    def test_opening_notepad_and_calculator_does_not_generate_content_or_compute(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver())
        for tool_name in ("desktop.notepad_open", "desktop.calculator_open"):
            with self.subTest(tool_name=tool_name):
                _, execution, verification = self.registry.dispatch(
                    tool_name, {}, self.ctx
                )
                self.assertTrue(execution.success)
                self.assertTrue(verification.verified, verification.failure_reason)

    def test_calculator_window_selection_disambiguates_foreground(self) -> None:
        first = type("Window", (), {"handle": 101})()
        foreground = type("Window", (), {"handle": 202})()

        self.assertIs(_select_calculator_window([first, foreground], 202), foreground)
        self.assertIs(_select_calculator_window([first, foreground], 303), first)
        with self.assertRaisesRegex(RuntimeError, "no visible Calculator window"):
            _select_calculator_window([], 303)

    def test_safe_calculator_expression_parser(self) -> None:
        cases = {
            "2 + 3 * 4": 14.0,
            "(2 + 3) * 4": 20.0,
            "2^5": 32.0,
            "sqrt 81": 9.0,
            "log 14": 1.146128035678238,
            "log10(1000)": 3.0,
            "ln 1": 0.0,
            "sin 30": 0.5,
            "cos(60)": 0.5,
            "tan 45": 1.0,
            "sin 1 radians": 0.8414709848078965,
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertAlmostEqual(
                    evaluate_math_expression_safely(expression), expected, places=7
                )
        self.assertAlmostEqual(
            evaluate_math_expression_safely("sin 1", "radians"), math.sin(1), places=7
        )
        for expression in ("__import__('os').system('whoami')", "2 ** 1001", "1 / 0"):
            with self.subTest(expression=expression), self.assertRaises(
                (ValueError, ZeroDivisionError)
            ):
                evaluate_math_expression_safely(expression)

    def test_paint_and_explorer_mocked_verification(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver())
        _, ex_paint, ver_paint = self.registry.dispatch(
            "desktop.paint_launch", {"draw_box": True}, self.ctx
        )
        self.assertTrue(ex_paint.success)
        self.assertTrue(ver_paint.verified)
        self.assertEqual(ver_paint.evidence["hwnd"], 2002)

        (self.ws_root / "projects").mkdir(parents=True, exist_ok=True)
        _, ex_exp, ver_exp = self.registry.dispatch(
            "desktop.explorer_open", {"path": "projects"}, self.ctx
        )
        self.assertTrue(ex_exp.success)
        self.assertTrue(ver_exp.verified)

    def test_paint_circle_and_saved_image_are_verified(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver())
        _, execution, verification = self.registry.dispatch(
            "desktop.paint_launch",
            {"draw_shape": "circle", "save_path": "paint/circle.png"},
            self.ctx,
        )
        image_path = self.ws_root / "paint" / "circle.png"
        self.assertTrue(execution.success)
        self.assertTrue(verification.verified, verification.failure_reason)
        self.assertTrue(image_path.is_file())
        with Image.open(image_path) as image:
            self.assertEqual(image.size, (200, 200))
            self.assertLess(image.convert("RGB").getpixel((40, 100))[0], 200)

    def test_paint_drawing_failure_is_not_reported_as_success(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver(tamper_paint=True))
        _, execution, verification = self.registry.dispatch(
            "desktop.paint_launch",
            {"draw_shape": "circle"},
            self.ctx,
        )
        self.assertFalse(verification.verified)
        self.assertFalse(execution.success)
        self.assertIn("drawing", verification.failure_reason.lower())

    def test_paint_image_verification_checks_requested_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            circle_path = Path(directory) / "circle.png"
            _create_paint_fallback_image(circle_path, "circle")
            with Image.open(circle_path) as image:
                self.assertTrue(_image_has_visible_drawing(image, "circle"))
                self.assertFalse(_image_has_visible_drawing(image, "rectangle"))
            self.assertFalse(
                _image_has_visible_drawing(Image.new("RGB", (800, 600), "white"), "circle")
            )

    def test_paint_saves_and_verifies_circle_and_rectangle(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver())
        for shape in ("circle", "rectangle"):
            with self.subTest(shape=shape):
                filename = f"{shape}.png"
                _, execution, verification = self.registry.dispatch(
                    "desktop.paint_launch",
                    {"draw_shape": shape, "save_path": filename},
                    self.ctx,
                )
                saved_image = self.ws_root / filename
                self.assertTrue(execution.success)
                self.assertTrue(verification.verified, verification.failure_reason)
                self.assertTrue(saved_image.is_file())
                with Image.open(saved_image) as image:
                    image.load()
                    self.assertTrue(_image_has_visible_drawing(image, shape))

    def test_paint_saves_and_verifies_triangle_house_and_landscape(self) -> None:
        set_desktop_driver(MockWindowsDesktopDriver())
        for shape in ("triangle", "house", "landscape"):
            with self.subTest(shape=shape):
                filename = f"{shape}.png"
                _, execution, verification = self.registry.dispatch(
                    "desktop.paint_launch",
                    {"draw_shape": shape, "save_path": filename},
                    self.ctx,
                )
                saved_image = self.ws_root / filename
                self.assertTrue(execution.success)
                self.assertTrue(verification.verified, verification.failure_reason)
                self.assertTrue(saved_image.is_file())
                with Image.open(saved_image) as image:
                    image.load()
                    self.assertTrue(_image_has_visible_drawing(image, shape))

    @unittest.skipIf(sys.platform == "win32", "Non-Windows honesty check runs only on non-Windows OS")
    def test_real_win32_driver_honestly_reports_unsupported_on_linux(self) -> None:
        set_desktop_driver(Win32UIADesktopDriver())
        pre, ex, ver = self.registry.dispatch(
            "desktop.notepad_write", {"text": "Should not fake success"}, self.ctx
        )
        self.assertFalse(pre.satisfied)
        self.assertFalse(ex.success)
        self.assertFalse(ver.verified)
        self.assertEqual(ex.error_code, "DESKTOP_OS_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
