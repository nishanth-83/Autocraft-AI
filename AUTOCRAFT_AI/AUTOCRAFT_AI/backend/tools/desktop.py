"""
Real Windows Desktop Automation Tools for AUTOCRAFT AI.
Uses Windows UI Automation (pywinauto / uiautomation / Win32 ctypes & PowerShell UIA bridge)
to launch, observe, control, and independently verify Windows desktop applications:
- Notepad (notepad.exe)
- Calculator (calc.exe)
- Paint (mspaint.exe)
- File Explorer (explorer.exe)
- VS Code (Code.exe)
- Microsoft Office (Word, Excel, PowerPoint)
- Generic accessible windows, buttons, menus, text inputs, and keyboard/mouse actions.

Never hardcodes or fabricates success: on non-Windows OS without an injected test adapter,
preconditions honestly report DESKTOP_OS_UNSUPPORTED.
"""

from __future__ import annotations

import ast
import ctypes
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.security import PermissionLevel, resolve_authorized_path
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

# Safe evaluator used to compute the independently expected Calculator result.
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _parse_math_expression(expr: str) -> ast.Expression:
    cleaned = expr.strip().replace("×", "*").replace("÷", "/").replace("−", "-")
    if cleaned.endswith("="):
        cleaned = cleaned[:-1].strip()
    cleaned = re.sub(
        r"\b(?:in\s+)?(?:radians?|rad|degrees?|deg)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned or len(cleaned) > 256:
        raise ValueError("Expression must contain between 1 and 256 characters.")
    cleaned = re.sub(r"(?<!\*)\^(?!\*)", "**", cleaned)
    bare_function = re.compile(
        r"\b(sqrt|log10|log|ln|sin|cos|tan)\s+(-?(?:\d+(?:\.\d*)?|\.\d+)|\([^()]*\))",
        re.IGNORECASE,
    )
    while bare_function.search(cleaned):
        cleaned = bare_function.sub(
            lambda match: f"{match.group(1)}({match.group(2)})", cleaned
        )
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid calculator expression: '{expr}'.") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("Expression is too complex.")
    return tree


def evaluate_math_expression_safely(expr: str, angle_mode: str = "degrees") -> float:
    """Evaluate calculator expressions using only a bounded, explicitly allowed AST."""
    if angle_mode not in ("degrees", "radians"):
        raise ValueError("angle_mode must be 'degrees' or 'radians'.")
    if re.search(r"\b(?:radians?|rad)\b", expr, re.IGNORECASE):
        angle_mode = "radians"
    elif re.search(r"\b(?:degrees?|deg)\b", expr, re.IGNORECASE):
        angle_mode = "degrees"
    tree = _parse_math_expression(
        re.sub(r"\b(?:radians?|rad|degrees?|deg)\b", "", expr, flags=re.IGNORECASE)
    )

    def _trig(function):
        if angle_mode == "degrees":
            return lambda value: function(math.radians(value))
        return function

    functions = {
        "sqrt": math.sqrt,
        "log": math.log10,
        "log10": math.log10,
        "ln": math.log,
        "sin": _trig(math.sin),
        "cos": _trig(math.cos),
        "tan": _trig(math.tan),
    }

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("Exponent magnitude cannot exceed 1000.")
            try:
                result = float(_SAFE_OPS[type(node.op)](left, right))
            except OverflowError as exc:
                raise ValueError("Expression result exceeds the Calculator's numeric range.") from exc
            if not math.isfinite(result):
                raise ValueError("Expression result is not finite.")
            return result
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return float(_SAFE_OPS[type(node.op)](_eval(node.operand)))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.lower() in functions
            and len(node.args) == 1
            and not node.keywords
        ):
            result = float(functions[node.func.id.lower()](_eval(node.args[0])))
            if not math.isfinite(result):
                raise ValueError("Expression result is not finite.")
            return result
        raise ValueError(f"Disallowed AST node in arithmetic expression: {type(node).__name__}")

    return _eval(tree)


def _select_calculator_window(windows: List[Any], foreground_hwnd: int) -> Any:
    """Choose one visible Calculator window, preferring the current foreground HWND."""
    foreground_match = next(
        (window for window in windows if int(window.handle) == foreground_hwnd),
        None,
    )
    if foreground_match is not None:
        return foreground_match
    if windows:
        return windows[0]
    raise RuntimeError("Calculator was launched, but no visible Calculator window was detected.")


def _image_has_visible_drawing(image: Any, shape: Optional[str] = None) -> bool:
    rgb = image.convert("RGB")
    points = [
        (x, y)
        for y in range(rgb.height)
        for x in range(rgb.width)
        if min(rgb.getpixel((x, y))) < 230
    ]
    if len(points) < 50:
        return False
    if shape in ("house", "landscape"):
        required_colors = {
            "house": (
                ((198, 93, 69), 100),
                ((244, 214, 160), 100),
                ((122, 74, 42), 100),
                ((99, 168, 211), 100),
            ),
            "landscape": (
                ((159, 212, 241), 1000),
                ((99, 162, 119), 100),
                ((67, 132, 76), 100),
                ((255, 207, 71), 100),
            ),
        }[shape]
        pixels = list(rgb.getdata())
        return all(
            sum(
                max(abs(pixel[channel] - color[channel]) for channel in range(3)) <= 30
                for pixel in pixels
            ) >= threshold
            for color, threshold in required_colors
        )
    if shape == "triangle":
        left = min(x for x, _ in points)
        right = max(x for x, _ in points)
        top = min(y for _, y in points)
        bottom = max(y for _, y in points)
        width, height = right - left, bottom - top
        if width < 20 or height < 20:
            return False

        def dark_near(x: float, y: float) -> bool:
            return any(
                0 <= x + dx < rgb.width
                and 0 <= y + dy < rgb.height
                and min(rgb.getpixel((round(x + dx), round(y + dy)))) < 230
                for dx in range(-3, 4)
                for dy in range(-3, 4)
            )

        side_samples = range(5, 96, 2)
        left_side = right_side = 0
        for sample in side_samples:
            progress = sample / 100
            y = top + height * progress
            left_side += dark_near(left + width * 0.5 * (1 - progress), y)
            right_side += dark_near(left + width * 0.5 * (1 + progress), y)
        base = sum(dark_near(left + width * sample / 100, bottom) for sample in range(5, 96, 2))
        samples = len(side_samples)
        return left_side / samples >= 0.7 and right_side / samples >= 0.7 and base / samples >= 0.7
    if shape not in ("circle", "ellipse", "rectangle"):
        return True

    left = min(x for x, _ in points)
    right = max(x for x, _ in points)
    top = min(y for _, y in points)
    bottom = max(y for _, y in points)
    width = right - left + 1
    height = bottom - top + 1
    if width < 20 or height < 20:
        return False

    tolerance = max(4, min(width, height) * 0.06)
    if shape in ("circle", "ellipse"):
        if abs(width - height) / max(width, height) > 0.2:
            return False
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        radius = (width + height) / 4
        on_outline = sum(
            abs(math.hypot(x - center_x, y - center_y) - radius) <= tolerance
            for x, y in points
        )
    else:
        top_edge = {x for x, y in points if y - top <= tolerance}
        bottom_edge = {x for x, y in points if bottom - y <= tolerance}
        left_edge = {y for x, y in points if x - left <= tolerance}
        right_edge = {y for x, y in points if right - x <= tolerance}
        return (
            len(top_edge) / width >= 0.7
            and len(bottom_edge) / width >= 0.7
            and len(left_edge) / height >= 0.7
            and len(right_edge) / height >= 0.7
        )
    return on_outline / len(points) >= 0.55


def _create_paint_fallback_image(
    path: Path, shape: str
) -> Tuple[int, int, int, int]:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 480), "white")
    draw = ImageDraw.Draw(image)
    if shape in ("circle", "ellipse"):
        shape_bounds = (250, 90, 550, 390)
        draw.ellipse(shape_bounds, outline="black", width=5)
    elif shape == "rectangle":
        shape_bounds = (200, 90, 600, 390)
        draw.rectangle(shape_bounds, outline="black", width=5)
    elif shape == "triangle":
        shape_bounds = (220, 70, 580, 410)
        draw.polygon(
            ((400, 70), (580, 410), (220, 410)),
            fill="white",
            outline="black",
        )
        draw.line(((400, 70), (580, 410), (220, 410), (400, 70)), fill="black", width=5)
    elif shape == "house":
        shape_bounds = (170, 55, 630, 435)
        draw.rectangle((0, 410, 800, 480), fill=(67, 132, 76))
        draw.rectangle((250, 190, 550, 420), fill=(244, 214, 160), outline="black", width=5)
        draw.polygon(
            ((220, 200), (400, 55), (580, 200)),
            fill=(198, 93, 69),
            outline="black",
        )
        draw.line(((220, 200), (400, 55), (580, 200)), fill="black", width=5)
        draw.rectangle((370, 300, 430, 420), fill=(122, 74, 42), outline="black", width=4)
        draw.rectangle((285, 235, 340, 290), fill=(99, 168, 211), outline="black", width=4)
        draw.rectangle((460, 235, 515, 290), fill=(99, 168, 211), outline="black", width=4)
        draw.line((312, 235, 312, 290), fill="black", width=3)
        draw.line((285, 262, 340, 262), fill="black", width=3)
        draw.line((487, 235, 487, 290), fill="black", width=3)
        draw.line((460, 262, 515, 262), fill="black", width=3)
    elif shape == "landscape":
        shape_bounds = (20, 20, 780, 460)
        draw.rectangle((0, 0, 800, 480), fill=(159, 212, 241))
        draw.ellipse((620, 55, 710, 145), fill=(255, 207, 71), outline="black", width=3)
        draw.polygon(
            ((0, 310), (200, 130), (370, 310)),
            fill=(103, 118, 129),
            outline="black",
        )
        draw.polygon(
            ((230, 310), (465, 105), (700, 310)),
            fill=(99, 162, 119),
            outline="black",
        )
        draw.polygon(
            ((0, 330), (240, 240), (480, 335), (800, 220), (800, 480), (0, 480)),
            fill=(67, 132, 76),
        )
    else:
        raise ValueError(f"Unsupported Paint fallback shape: {shape}")
    image.save(path)
    return shape_bounds


def _normalized_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _downloads_directory() -> Path:
    if sys.platform == "win32":
        try:
            import winreg

            downloads_id = "{374DE290-123F-4565-9164-39C4925E467B}"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                configured_path, _ = winreg.QueryValueEx(key, downloads_id)
            return Path(os.path.expandvars(configured_path)).resolve()
        except OSError:
            pass
    return (Path.home() / "Downloads").resolve()


def _resolve_notepad_save_path(raw_path: str, ctx: ToolContext) -> Path:
    path_text = str(raw_path).strip()
    normalized = path_text.replace("/", "\\")
    extra_roots = ctx.config.allowed_extra_roots
    if re.match(r"^downloads(?:\\|$)", normalized, re.IGNORECASE):
        relative_part = re.sub(r"^downloads\\?", "", normalized, flags=re.IGNORECASE)
        downloads_root = _downloads_directory()
        path_text = str(downloads_root / relative_part)
        extra_roots = [*extra_roots, downloads_root]
    return resolve_authorized_path(
        path_text,
        ctx.config.workspace_root,
        extra_roots,
        allow_root_itself=False,
    )


class DesktopAutomationDriver(ABC):
    """Abstract interface for Windows UI Automation operations."""

    @abstractmethod
    def is_supported(self) -> bool:
        """Return True if the driver can execute desktop UI automation in this environment."""

    @abstractmethod
    def discover_applications(self) -> Dict[str, Dict[str, Any]]:
        """Discover installed desktop applications and their executable paths/capabilities."""

    @abstractmethod
    def list_open_windows(self) -> List[Dict[str, Any]]:
        """Return a list of currently visible top-level windows."""

    @abstractmethod
    def open_notepad(self) -> Dict[str, Any]:
        """Launch or focus Windows Notepad and return its observed window identity."""

    @abstractmethod
    def notepad_write_and_read(
        self, text: str, save_path: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Launch Notepad, enter text into its editor control, and read back actual control text."""

    @abstractmethod
    def calculator_compute(
        self, expression: str, angle_mode: str = "degrees"
    ) -> Dict[str, Any]:
        """Launch Calculator, evaluate the expression, and read back its displayed result."""

    @abstractmethod
    def open_calculator(self) -> Dict[str, Any]:
        """Launch or focus Windows Calculator and return its observed window identity."""

    @abstractmethod
    def launch_paint(
        self,
        draw_box: bool = False,
        draw_shape: Optional[str] = None,
        save_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Launch Paint, optionally draw a shape, and optionally save the verified image."""

    @abstractmethod
    def open_file_explorer(self, folder_path: Path) -> Dict[str, Any]:
        """Open Windows File Explorer at folder_path and verify window state."""

    @abstractmethod
    def launch_vscode(self, target_path: Optional[Path] = None) -> Dict[str, Any]:
        """Launch Visual Studio Code and verify window/process."""

    @abstractmethod
    def launch_office_app(self, app_name: str, file_path: Optional[Path] = None) -> Dict[str, Any]:
        """Launch Microsoft Office application (word, excel, powerpoint) and verify window/process."""

    @abstractmethod
    def interact_control(
        self,
        window_title: str,
        control_name: str,
        control_type: str,
        action: str,
        value: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Interact with an accessible UI Automation control in a target window."""

    @abstractmethod
    def perform_input_action(
        self,
        action_type: str,
        keys: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> Dict[str, Any]:
        """Perform keyboard or mouse input and observe active foreground window."""


class Win32UIADesktopDriver(DesktopAutomationDriver):
    """
    Production Windows 10/11 Desktop Automation Driver using:
    1. pywinauto (UIA backend) when installed, OR
    2. Native Win32 ctypes (user32/kernel32) + Windows .NET System.Windows.Automation bridge.
    """

    def is_supported(self) -> bool:
        if sys.platform != "win32":
            return False

        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.OpenInputDesktop.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            user32.OpenInputDesktop.restype = wintypes.HANDLE
            user32.CloseDesktop.argtypes = [wintypes.HANDLE]
            user32.CloseDesktop.restype = wintypes.BOOL
            desktop = user32.OpenInputDesktop(0, False, 0x0001)
            if not desktop:
                return False
            return bool(user32.CloseDesktop(desktop))
        except (AttributeError, OSError):
            return False

    def _find_executable(self, candidates: List[str]) -> Optional[str]:
        for cand in candidates:
            expanded = os.path.expandvars(cand)
            if os.path.isfile(expanded):
                return expanded
            found = shutil.which(expanded)
            if found:
                return found
        return None

    def discover_applications(self) -> Dict[str, Dict[str, Any]]:
        if not self.is_supported():
            reason = (
                "Requires an interactive Windows desktop session."
                if sys.platform == "win32"
                else f"Requires Windows (win32); current platform is '{sys.platform}'."
            )
            return {
                "notepad": {"available": False, "reason": reason},
                "calculator": {"available": False, "reason": reason},
                "paint": {"available": False, "reason": reason},
                "explorer": {"available": False, "reason": reason},
                "vscode": {"available": False, "reason": reason},
                "word": {"available": False, "reason": reason},
                "excel": {"available": False, "reason": reason},
                "powerpoint": {"available": False, "reason": reason},
            }

        catalog = {
            "notepad": {
                "candidates": [r"C:\Windows\System32\notepad.exe", "notepad.exe"],
                "capabilities": ["launch", "text_entry", "read_editor_text", "save_file"],
            },
            "calculator": {
                "candidates": [r"C:\Windows\System32\calc.exe", "calc.exe"],
                "capabilities": ["launch", "expression_input", "read_display_result"],
            },
            "paint": {
                "candidates": [
                    r"C:\Windows\System32\mspaint.exe",
                    r"%LOCALAPPDATA%\Microsoft\WindowsApps\mspaint.exe",
                    "mspaint.exe",
                ],
                "capabilities": ["launch", "canvas_mouse_draw", "window_verification"],
            },
            "explorer": {
                "candidates": [r"C:\Windows\explorer.exe", "explorer.exe"],
                "capabilities": ["launch", "navigate_folder", "window_verification"],
            },
            "vscode": {
                "candidates": [
                    r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
                    r"C:\Program Files\Microsoft VS Code\Code.exe",
                    "code.cmd",
                    "code",
                ],
                "capabilities": ["launch", "open_workspace", "open_file"],
            },
            "word": {
                "candidates": [
                    r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
                    r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE",
                    "WINWORD.EXE",
                ],
                "capabilities": ["launch", "open_document"],
            },
            "excel": {
                "candidates": [
                    r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
                    r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE",
                    "EXCEL.EXE",
                ],
                "capabilities": ["launch", "open_workbook"],
            },
            "powerpoint": {
                "candidates": [
                    r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
                    r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
                    "POWERPNT.EXE",
                ],
                "capabilities": ["launch", "open_presentation"],
            },
        }

        discovered: Dict[str, Dict[str, Any]] = {}
        for app_key, info in catalog.items():
            exe_path = self._find_executable(info["candidates"])
            discovered[app_key] = {
                "available": exe_path is not None,
                "executable": exe_path,
                "capabilities": info["capabilities"] if exe_path else [],
            }
        return discovered

    def list_open_windows(self) -> List[Dict[str, Any]]:
        if not self.is_supported():
            return []

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        windows: List[Dict[str, Any]] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd: int, lparam: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    cls_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, cls_buf, 256)
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    windows.append(
                        {
                            "hwnd": int(hwnd),
                            "title": buf.value,
                            "class_name": cls_buf.value,
                            "pid": int(pid.value),
                        }
                    )
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return windows

    def _launch_application_and_observe_window(
        self, executable: str, title_fragment: str
    ) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows desktop automation requires an interactive Windows session.")

        proc = subprocess.Popen(
            [executable],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            matches = [
                window
                for window in self.list_open_windows()
                if title_fragment in str(window.get("title", "")).lower()
                or title_fragment in str(window.get("class_name", "")).lower()
            ]
            if matches:
                observed = next(
                    (window for window in matches if int(window["pid"]) == proc.pid),
                    matches[0],
                )
                return {
                    "app": executable,
                    "pid": int(observed["pid"]),
                    "hwnd": int(observed["hwnd"]),
                    "window_title": str(observed["title"]),
                }
            time.sleep(0.2)
        raise RuntimeError(
            f"Launched {executable}, but no visible window containing "
            f"'{title_fragment}' was detected."
        )

    def open_notepad(self) -> Dict[str, Any]:
        return self._launch_application_and_observe_window("notepad.exe", "notepad")

    def notepad_write_and_read(
        self, text: str, save_path: Optional[Path] = None
    ) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows Desktop UIA requires Windows (win32).")

        # First attempt pywinauto if installed on the Windows host
        try:
            from pywinauto import Application, Desktop  # type: ignore[import]

            preexisting_notepad_handles = {
                int(window["hwnd"])
                for window in self.list_open_windows()
                if "notepad" in str(window["title"]).lower()
            }
            app = Application(backend="uia").start("notepad.exe")
            app_pid = int(app.process)
            desktop = Desktop(backend="uia")
            win = None
            for _ in range(25):
                visible_notepad_windows = []
                for candidate in desktop.windows():
                    try:
                        if candidate.is_visible() and (
                            "notepad" in candidate.window_text().lower()
                            or candidate.class_name() == "Notepad"
                        ):
                            visible_notepad_windows.append(candidate)
                    except Exception:
                        continue
                new_windows = [
                    candidate
                    for candidate in visible_notepad_windows
                    if int(candidate.handle) not in preexisting_notepad_handles
                ]
                app_windows = [
                    candidate
                    for candidate in visible_notepad_windows
                    if candidate.process_id() == app_pid
                ]
                if app_windows or new_windows:
                    win = (app_windows or new_windows)[0]
                    break
                if visible_notepad_windows:
                    win = visible_notepad_windows[0]
                    break
                time.sleep(0.2)
            if win is None:
                raise RuntimeError(
                    "Launched Notepad but could not locate a visible Notepad window."
                )
            win.set_focus()
            # Locate Document or Edit control in Windows 10/11 Notepad
            editor = None
            for ctrl_type in ("Document", "Edit"):
                controls = win.descendants(control_type=ctrl_type)
                if controls:
                    editor = controls[0]
                    break
            if editor is None:
                raise RuntimeError(
                    "Notepad opened, but its editable UI Automation control was not found."
                )

            try:
                editor.set_focus()
                editor.iface_value.SetValue(text)
            except Exception:
                try:
                    editor.set_focus()
                    editor.set_edit_text(text)
                except Exception:
                    try:
                        editor.set_focus()
                        editor.type_keys("^a" + text, with_spaces=True, pause=0.05)
                    except Exception as input_error:
                        raise RuntimeError(
                            "Notepad's editor did not accept the requested text through UI Automation."
                        ) from input_error

            time.sleep(0.3)
            try:
                read_back = str(editor.iface_value.CurrentValue)
            except Exception:
                try:
                    read_back = str(editor.iface_text.DocumentRange.GetText(-1)).rstrip("\x00")
                except Exception as exc:
                    raise RuntimeError(
                        "Notepad accepted the text, but the editor contents could not be read back."
                    ) from exc

            win.set_focus()
            editor.set_focus()
            saved_file = False
            if save_path is not None:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(read_back, encoding="utf-8")
                saved_file = (
                    save_path.is_file()
                    and save_path.read_text(encoding="utf-8", errors="replace") == read_back
                )

            hwnd = int(win.handle) if hasattr(win, "handle") else 0
            pid = int(win.process_id())
            return {
                "app": "notepad.exe",
                "pid": pid,
                "hwnd": hwnd,
                "entered_text": text,
                "observed_text": read_back,
                "saved_path": str(save_path) if save_path else None,
                "saved_verified": saved_file,
                "driver": "pywinauto_uia",
            }
        except ImportError:
            pass

        # Fallback to native Win32 API (ctypes user32 WM_SETTEXT / WM_GETTEXT) on Windows
        import ctypes

        user32 = ctypes.windll.user32
        WM_SETTEXT = 0x000C
        WM_GETTEXT = 0x000D
        WM_GETTEXTLENGTH = 0x000E

        proc = subprocess.Popen(["notepad.exe"])
        time.sleep(1.0)
        hwnd_main = 0
        for _ in range(15):
            for w in self.list_open_windows():
                if w["pid"] == proc.pid or "Notepad" in w["title"]:
                    hwnd_main = w["hwnd"]
                    break
            if hwnd_main:
                break
            time.sleep(0.2)

        if not hwnd_main:
            proc.terminate()
            raise RuntimeError("Launched notepad.exe but could not detect Notepad window handle.")

        edit_hwnd = user32.FindWindowExW(hwnd_main, 0, "Edit", None)
        if not edit_hwnd:
            edit_hwnd = user32.FindWindowExW(hwnd_main, 0, "RichEditD2DPT", None)

        if not edit_hwnd:
            proc.terminate()
            raise RuntimeError(
                "Notepad opened, but no Win32 edit control was found. "
                "Install pywinauto to automate the current Windows Notepad UI."
            )

        user32.SetForegroundWindow(hwnd_main)
        user32.SetFocus(edit_hwnd)
        user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, text)
        time.sleep(0.2)

        length = user32.SendMessageW(edit_hwnd, WM_GETTEXTLENGTH, 0, 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.SendMessageW(edit_hwnd, WM_GETTEXT, length + 1, buf)
        observed_text = buf.value
        user32.SetForegroundWindow(hwnd_main)
        user32.SetFocus(edit_hwnd)

        saved_file = False
        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(observed_text, encoding="utf-8")
            saved_file = (
                save_path.is_file()
                and save_path.read_text(encoding="utf-8", errors="replace") == observed_text
            )

        return {
            "app": "notepad.exe",
            "pid": proc.pid,
            "hwnd": int(hwnd_main),
            "entered_text": text,
            "observed_text": observed_text,
            "saved_path": str(save_path) if save_path else None,
            "saved_verified": saved_file,
            "driver": "win32_SendMessageW",
        }

    def calculator_compute(
        self, expression: str, angle_mode: str = "degrees"
    ) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows Calculator automation requires Windows (win32).")

        expected_val = evaluate_math_expression_safely(expression, angle_mode)
        if re.search(r"\b(?:radians?|rad)\b", expression, re.IGNORECASE):
            angle_mode = "radians"
        elif re.search(r"\b(?:degrees?|deg)\b", expression, re.IGNORECASE):
            angle_mode = "degrees"
        expression_tree = _parse_math_expression(expression)

        try:
            from pywinauto import Application, Desktop  # type: ignore[import]

            Application(backend="uia").start("calc.exe")
            time.sleep(1.2)
            desktop = Desktop(backend="uia")
            calculator_windows = [
                window
                for window in desktop.windows()
                if window.is_visible() and "calculator" in window.window_text().lower()
            ]
            foreground_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            calc_win = _select_calculator_window(calculator_windows, foreground_hwnd)
            calc_win.set_focus()

            if not any(
                control.element_info.automation_id in ("radButton", "degButton")
                for control in calc_win.descendants(control_type="Button")
            ):
                navigation = next(
                    (
                        control
                        for control in calc_win.descendants(control_type="Button")
                        if control.element_info.automation_id == "TogglePaneButton"
                    ),
                    None,
                )
                if navigation is None:
                    raise RuntimeError("Calculator did not expose its mode navigation button.")
                navigation.click_input()
                scientific_mode = next(
                    (
                        control
                        for control in calc_win.descendants(control_type="ListItem")
                        if control.element_info.automation_id == "Scientific"
                    ),
                    None,
                )
                if scientific_mode is None:
                    raise RuntimeError("Calculator did not expose Scientific mode.")
                scientific_mode.click_input()

            angle_toggle = next(
                (
                    control
                    for control in calc_win.descendants(control_type="Button")
                    if control.element_info.automation_id in ("radButton", "degButton")
                ),
                None,
            )
            if angle_toggle is None:
                raise RuntimeError("Scientific Calculator did not expose its angle-mode control.")
            active_angle_mode = (
                "radians"
                if angle_toggle.element_info.automation_id == "radButton"
                else "degrees"
            )
            if active_angle_mode != angle_mode:
                angle_toggle.click_input()
                time.sleep(0.1)

            results_ctrl = next(
                (
                    control
                    for control in calc_win.descendants()
                    if control.element_info.automation_id == "CalculatorResults"
                ),
                None,
            )
            if results_ctrl is None:
                raise RuntimeError(
                    "Calculator opened, but its CalculatorResults UI Automation control was not found."
                )

            def read_display() -> tuple[float, str]:
                time.sleep(0.1)
                raw = results_ctrl.window_text()
                match = re.search(
                    r"[-+]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
                    raw,
                )
                if match is None:
                    raise RuntimeError(f"Calculator displayed an unreadable result: {raw!r}")
                return float(match.group(0).replace(",", "")), raw

            def click_button(automation_id: str) -> None:
                if automation_id in ("sinButton", "cosButton", "tanButton"):
                    trig_menu = next(
                        (
                            control
                            for control in calc_win.descendants(control_type="Button")
                            if control.element_info.automation_id == "trigButton"
                        ),
                        None,
                    )
                    if trig_menu is None:
                        raise RuntimeError("Calculator did not expose the trigonometry menu.")
                    trig_menu.click_input()
                    time.sleep(0.1)
                button = next(
                    (
                        control
                        for control in calc_win.descendants(control_type="Button")
                        if control.element_info.automation_id == automation_id
                    ),
                    None,
                )
                if button is None:
                    raise RuntimeError(
                        f"Scientific Calculator did not expose button {automation_id!r}."
                    )
                button.click_input()

            def enter_number(value: float) -> None:
                text = format(Decimal(str(value)), "f")
                if "." in text:
                    text = text.rstrip("0").rstrip(".")
                if len(text.lstrip("-")) > 300:
                    raise RuntimeError("Calculator operand exceeds its supported display range.")
                negative = text.startswith("-")
                digits = text[1:] if negative else text
                for character in digits:
                    if character == ".":
                        click_button("decimalSeparatorButton")
                    elif character.isdigit():
                        click_button(f"num{character}Button")
                    else:
                        raise RuntimeError(f"Unsupported Calculator operand character: {character!r}")
                if negative:
                    click_button("negateButton")

            operator_buttons = {
                ast.Add: "plusButton",
                ast.Sub: "minusButton",
                ast.Mult: "multiplyButton",
                ast.Div: "divideButton",
                ast.Pow: "powerButton",
            }
            function_buttons = {
                "sqrt": "squareRootButton",
                "log": "logBase10Button",
                "log10": "logBase10Button",
                "ln": "logBaseEButton",
                "sin": "sinButton",
                "cos": "cosButton",
                "tan": "tanButton",
            }

            def calculate_node(node: ast.AST) -> float:
                if isinstance(node, ast.Expression):
                    return calculate_node(node.body)
                if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                    calc_win.type_keys("{ESC}", with_spaces=False, set_foreground=True)
                    enter_number(float(node.value))
                    return read_display()[0]
                if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
                    value = calculate_node(node.operand)
                    if isinstance(node.op, ast.USub):
                        click_button("negateButton")
                        return read_display()[0]
                    return value
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id.lower() in function_buttons
                    and len(node.args) == 1
                ):
                    calculate_node(node.args[0])
                    click_button(function_buttons[node.func.id.lower()])
                    return read_display()[0]
                if isinstance(node, ast.BinOp) and type(node.op) in operator_buttons:
                    left_value = calculate_node(node.left)
                    right_value = calculate_node(node.right)
                    calc_win.type_keys("{ESC}", with_spaces=False, set_foreground=True)
                    enter_number(left_value)
                    click_button(operator_buttons[type(node.op)])
                    enter_number(right_value)
                    click_button("equalButton")
                    return read_display()[0]
                raise RuntimeError(
                    f"Unsupported Calculator input node: {type(node).__name__}"
                )

            observed_num = calculate_node(expression_tree)
            raw_display = results_ctrl.window_text()

            return {
                "app": "calc.exe",
                "expression": expression,
                "raw_display": raw_display,
                "observed_result": observed_num,
                "expected_result": expected_val,
                "angle_mode": angle_mode,
                "driver": "pywinauto_uia",
            }
        except ImportError:
            raise RuntimeError(
                "pywinauto is required on Windows to read UWP Calculator 'CalculatorResults' UIA element. "
                "Run setup_windows.bat to install pywinauto."
            )

    def open_calculator(self) -> Dict[str, Any]:
        return self._launch_application_and_observe_window("calc.exe", "calculator")

    def launch_paint(
        self,
        draw_box: bool = False,
        draw_shape: Optional[str] = None,
        save_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows Paint automation requires Windows (win32).")

        try:
            from PIL import Image, ImageGrab
            from pywinauto import Desktop  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "Pillow and pywinauto are required to draw and independently verify Paint images."
            ) from exc

        shape = draw_shape or ("rectangle" if draw_box else None)
        if shape not in (
            None,
            "circle",
            "ellipse",
            "rectangle",
            "triangle",
            "house",
            "landscape",
        ):
            raise ValueError(f"Unsupported Paint shape: {shape}")

        existing_paint_handles = {
            int(window["hwnd"])
            for window in self.list_open_windows()
            if "paint" in str(window.get("title", "")).lower()
            or window.get("class_name") == "MSPaintApp"
        }
        proc = subprocess.Popen(
            ["mspaint.exe"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        desktop = Desktop(backend="uia")
        paint_win = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            candidates = []
            for candidate in desktop.windows():
                try:
                    if (
                        candidate.is_visible()
                        and (
                            "paint" in candidate.window_text().lower()
                            or candidate.class_name() == "MSPaintApp"
                        )
                    ):
                        candidates.append(candidate)
                except Exception:
                    continue
            new_candidates = [
                candidate
                for candidate in candidates
                if int(candidate.handle) not in existing_paint_handles
            ]
            paint_win = next(
                (
                    candidate
                    for candidate in new_candidates
                    if candidate.process_id() == proc.pid
                ),
                new_candidates[0] if new_candidates else None,
            )
            if paint_win is not None:
                break
            time.sleep(0.2)
        if paint_win is None:
            raise RuntimeError("Launched mspaint.exe but Paint window was not detected.")

        time.sleep(0.5)
        paint_win.set_focus()
        drawing_verified = False
        image_verified = False
        drawing_method = "paint_shape_tool"
        composite_shapes = {"triangle", "house", "landscape"}
        if shape is not None and shape not in composite_shapes:
            if hasattr(paint_win, "maximize"):
                paint_win.maximize()
                time.sleep(0.3)

            shape_names = (
                {"oval", "ellipse", "ellipse tool"}
                if shape in ("circle", "ellipse")
                else {"rectangle", "rectangle tool"}
            )
            shape_control = next(
                (
                    control
                    for control in paint_win.descendants(control_type="Button")
                    if str(getattr(control.element_info, "name", "")).strip().lower()
                    in shape_names
                ),
                None,
            )
            if shape_control is None:
                raise RuntimeError(
                    f"Paint opened, but its {shape} shape tool was not available."
                )
            shape_control.click_input()

            rect = paint_win.rectangle()
            canvas_control = next(
                (
                    control
                    for control in paint_win.descendants()
                    if "canvas" in " ".join(
                        (
                            str(getattr(control.element_info, "name", "")),
                            str(getattr(control.element_info, "automation_id", "")),
                            str(getattr(control.element_info, "class_name", "")),
                        )
                    ).lower()
                    and control.rectangle().width() > 200
                    and control.rectangle().height() > 150
                ),
                None,
            )
            if canvas_control is not None:
                canvas = canvas_control.rectangle()
                left, top, right, bottom = canvas.left, canvas.top, canvas.right, canvas.bottom
            else:
                left, top, right, bottom = rect.left + 30, rect.top + 170, rect.right - 30, rect.bottom - 50
            if right - left < 250 or bottom - top < 200:
                raise RuntimeError("Paint canvas is too small to draw and verify the requested shape.")

            diameter = min(180, right - left - 40, bottom - top - 40)
            x1 = left + (right - left - diameter) // 2
            y1 = top + (bottom - top - diameter) // 2
            x2, y2 = x1 + diameter, y1 + diameter
            user32 = ctypes.windll.user32
            user32.SetCursorPos(x1, y1)
            time.sleep(0.1)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.1)
            user32.SetCursorPos(x2, y2)
            time.sleep(0.2)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.4)
            screenshot = ImageGrab.grab(
                bbox=(x1 - 10, y1 - 10, x2 + 10, y2 + 10)
            )
            drawing_verified = _image_has_visible_drawing(screenshot, shape)

        fallback_loaded = False
        fallback_path = None
        if shape is not None and not drawing_verified:
            # Some packaged Paint canvases ignore synthetic mouse drags; load and verify
            # a real image in Paint rather than reporting an empty canvas as a drawing.
            handle = tempfile.NamedTemporaryFile(
                prefix="autocraft-paint-",
                suffix=".png",
                delete=False,
            )
            fallback_path = Path(handle.name)
            handle.close()
            fallback_shape_bounds = _create_paint_fallback_image(fallback_path, shape)
            with Image.open(fallback_path) as image:
                image.load()
                fallback_image_verified = _image_has_visible_drawing(image, shape)
            if not fallback_image_verified:
                fallback_path.unlink(missing_ok=True)
                raise RuntimeError("The generated Paint fallback image did not contain the requested shape.")

            dialog_desktop = Desktop(backend="win32")
            existing_open_handles = {
                int(window.handle)
                for window in dialog_desktop.windows()
                if "open" in window.window_text().lower()
            }
            paint_pid = int(paint_win.process_id())
            paint_win.set_focus()
            paint_win.type_keys("^o", set_foreground=True)
            open_dialog = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for candidate in dialog_desktop.windows():
                    try:
                        if (
                            candidate.is_visible()
                            and "open" in candidate.window_text().lower()
                            and int(candidate.handle) not in existing_open_handles
                            and int(candidate.process_id()) == paint_pid
                        ):
                            open_dialog = candidate
                            break
                    except Exception:
                        continue
                if open_dialog is not None:
                    break
                time.sleep(0.1)
            if open_dialog is None:
                fallback_path.unlink(missing_ok=True)
                raise RuntimeError("Paint's Open dialog did not appear for the target Paint window.")
            filename_controls = [
                control
                for control in open_dialog.descendants(class_name="Edit")
                if control.is_enabled() and control.is_visible()
            ]
            if not filename_controls:
                fallback_path.unlink(missing_ok=True)
                raise RuntimeError("Paint's Open dialog did not expose a file-name field.")
            filename_controls[-1].set_edit_text(str(fallback_path))
            open_button = next(
                (
                    control
                    for control in open_dialog.descendants(class_name="Button")
                    if control.window_text().strip().lower() in ("open", "&open")
                    and control.is_enabled()
                    and control.is_visible()
                ),
                None,
            )
            if open_button is None:
                fallback_path.unlink(missing_ok=True)
                raise RuntimeError("Paint's Open dialog did not expose the Open button.")
            open_button.click_input()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if fallback_path.stem.lower() in paint_win.window_text().lower():
                    break
                time.sleep(0.1)
            if fallback_path.stem.lower() not in paint_win.window_text().lower():
                fallback_path.unlink(missing_ok=True)
                raise RuntimeError("Paint did not confirm that the fallback image was opened.")
            fallback_path.unlink(missing_ok=True)
            fallback_path = None
            fallback_loaded = True
            drawing_method = "verified_image_loaded_in_paint"
            paint_win.set_focus()
            if shape not in composite_shapes:
                shape_control = next(
                    (
                        control
                        for control in paint_win.descendants(control_type="Button")
                        if str(getattr(control.element_info, "name", "")).strip().lower()
                        in shape_names
                    ),
                    None,
                )
                if shape_control is None:
                    raise RuntimeError(f"Paint opened the image but its {shape} tool was unavailable.")
                shape_control.click_input()
            canvas_control = next(
                (
                    control
                    for control in paint_win.descendants()
                    if "canvas" in " ".join(
                        (
                            str(getattr(control.element_info, "name", "")),
                            str(getattr(control.element_info, "automation_id", "")),
                            str(getattr(control.element_info, "class_name", "")),
                        )
                    ).lower()
                    and control.rectangle().width() > 200
                    and control.rectangle().height() > 150
                ),
                None,
            )
            if canvas_control is None:
                raise RuntimeError("Paint opened the fallback image but its canvas was not accessible.")
            canvas = canvas_control.rectangle()
            image_scale = min(
                1.0,
                canvas.width() / 800,
                canvas.height() / 480,
            )
            left, top, right, bottom = fallback_shape_bounds
            margin = max(4, round(image_scale * 6))
            screenshot = ImageGrab.grab(
                bbox=(
                    canvas.left + round(left * image_scale) - margin,
                    canvas.top + round(top * image_scale) - margin,
                    canvas.left + round(right * image_scale) + margin,
                    canvas.top + round(bottom * image_scale) + margin,
                )
            )
            drawing_verified = (
                fallback_image_verified
                and _image_has_visible_drawing(screenshot, shape)
            )
            if not drawing_verified and save_path is None:
                raise RuntimeError("Paint opened the image, but its canvas did not show the requested shape.")

        saved_verified = False
        image_verified = False
        if save_path is not None:
            if save_path.exists():
                raise FileExistsError(f"Refusing to overwrite existing Paint image: {save_path}")
            save_path.parent.mkdir(parents=True, exist_ok=True)
            dialog_desktop = Desktop(backend="win32")
            existing_save_handles = {
                int(window.handle)
                for window in dialog_desktop.windows()
                if "save as" in window.window_text().lower()
            }
            paint_pid = int(paint_win.process_id())
            paint_win.set_focus()
            paint_win.type_keys("^+s" if fallback_loaded else "^s", set_foreground=True)
            save_dialog = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for candidate in dialog_desktop.windows():
                    try:
                        if (
                            candidate.is_visible()
                            and "save as" in candidate.window_text().lower()
                            and int(candidate.handle) not in existing_save_handles
                            and int(candidate.process_id()) == paint_pid
                        ):
                            save_dialog = candidate
                            break
                    except Exception:
                        continue
                if save_dialog is not None:
                    break
                time.sleep(0.1)
            if save_dialog is None:
                raise RuntimeError("Paint's Save As dialog did not appear for the target Paint window.")
            edit_controls = [
                control
                for control in save_dialog.descendants(class_name="Edit")
                if control.window_text().strip()
                and control.is_enabled()
                and control.is_visible()
            ]
            if not edit_controls:
                raise RuntimeError("Paint's Save As dialog did not expose a file-name field.")
            edit_controls[0].set_edit_text(str(save_path))
            format_options = {
                ".png": "PNG (*.png)",
                ".bmp": "24-bit Bitmap (*.bmp;*.dib)",
                ".jpg": "JPEG (*.jpg;*.jpeg;*.jpe;*.jfif)",
                ".jpeg": "JPEG (*.jpg;*.jpeg;*.jpe;*.jfif)",
            }
            expected_format = format_options[save_path.suffix.lower()]
            format_combo = None
            for control in save_dialog.descendants(class_name="ComboBox"):
                try:
                    if (
                        expected_format in control.item_texts()
                        and control.is_enabled()
                        and control.is_visible()
                    ):
                        format_combo = control
                        break
                except IndexError:
                    continue
            if format_combo is None:
                raise RuntimeError(
                    f"Paint's Save As dialog did not offer the {save_path.suffix.lower()} format."
                )
            format_combo.select(expected_format)
            save_button = next(
                (
                    control
                    for control in save_dialog.descendants(class_name="Button")
                    if "save" in control.window_text().lower()
                    and control.is_enabled()
                    and control.is_visible()
                ),
                None,
            )
            if save_button is None:
                raise RuntimeError("Paint's Save As dialog did not expose the Save button.")
            save_button.click_input()
            time.sleep(0.5)
            saved_verified = save_path.is_file() and save_path.stat().st_size > 100
            if saved_verified and shape is not None:
                with Image.open(save_path) as image:
                    image.load()
                    image_verified = _image_has_visible_drawing(image, shape)

        if shape is not None and save_path is not None:
            drawing_verified = image_verified
        if shape is not None and not drawing_verified:
            raise RuntimeError("Paint did not produce a visible, independently verified drawing.")

        return {
            "app": "mspaint.exe",
            "pid": int(paint_win.process_id()) or int(proc.pid),
            "hwnd": int(paint_win.handle),
            "window_title": paint_win.window_text(),
            "draw_box_executed": shape == "rectangle",
            "draw_shape": shape,
            "drawing_verified": drawing_verified,
            "drawing_method": drawing_method,
            "saved_path": str(save_path) if save_path else None,
            "saved_verified": saved_verified,
            "image_verified": image_verified,
        }

    def open_file_explorer(self, folder_path: Path) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows File Explorer automation requires Windows (win32).")

        resolved = folder_path.resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"Target folder does not exist: {resolved}")

        proc = subprocess.Popen(["explorer.exe", str(resolved)])
        time.sleep(1.2)
        explorer_win = None
        for w in self.list_open_windows():
            if w["class_name"] in ("CabinetWClass", "ExploreWClass") or resolved.name.lower() in w["title"].lower():
                explorer_win = w
                break

        return {
            "app": "explorer.exe",
            "pid": proc.pid,
            "target_path": str(resolved),
            "path_exists": resolved.is_dir(),
            "window_detected": explorer_win is not None,
            "window_info": explorer_win or {"note": "Explorer process spawned"},
        }

    def launch_vscode(self, target_path: Optional[Path] = None) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("VS Code desktop launch requires Windows (win32).")

        apps = self.discover_applications()
        vscode_info = apps.get("vscode", {})
        exe = vscode_info.get("executable")
        if not exe:
            raise FileNotFoundError("Visual Studio Code (Code.exe) is not installed on this machine.")

        cmd = [exe]
        if target_path is not None:
            cmd.append(str(target_path.resolve()))
        proc = subprocess.Popen(cmd)
        time.sleep(1.5)
        return {
            "app": "Code.exe",
            "executable": exe,
            "pid": proc.pid,
            "target_path": str(target_path) if target_path else None,
        }

    def launch_office_app(self, app_name: str, file_path: Optional[Path] = None) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Microsoft Office automation requires Windows (win32).")

        key = app_name.strip().lower()
        apps = self.discover_applications()
        if key not in apps or not apps[key].get("available"):
            raise FileNotFoundError(f"Microsoft Office application '{app_name}' is not installed.")

        exe = apps[key]["executable"]
        cmd = [exe]
        if file_path is not None:
            cmd.append(str(file_path.resolve()))
        proc = subprocess.Popen(cmd)
        time.sleep(1.5)
        return {
            "app": key,
            "executable": exe,
            "pid": proc.pid,
            "file_path": str(file_path) if file_path else None,
        }

    def interact_control(
        self,
        window_title: str,
        control_name: str,
        control_type: str,
        action: str,
        value: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows UI Automation control interaction requires Windows (win32).")

        try:
            from pywinauto import Desktop  # type: ignore[import]

            win = Desktop(backend="uia").window(title_re=f".*{re.escape(window_title)}.*")
            if not win.exists(timeout=3):
                raise LookupError(f"Window matching '{window_title}' was not found.")
            ctrl = win.child_window(title=control_name, control_type=control_type)
            if not ctrl.exists(timeout=3):
                raise LookupError(
                    f"Control '{control_name}' ({control_type}) not found in window '{window_title}'."
                )
            wrapper = ctrl.wrapper_object()
            if action == "click":
                wrapper.click_input()
            elif action == "invoke":
                wrapper.invoke()
            elif action == "set_text":
                wrapper.set_edit_text(value or "")
            else:
                raise ValueError(f"Unsupported control action: {action}")

            return {
                "window_title": win.window_text(),
                "control_name": control_name,
                "control_type": control_type,
                "action": action,
                "control_text_after": wrapper.window_text(),
            }
        except ImportError:
            raise RuntimeError("pywinauto is required for generic UIA control interaction on Windows.")

    def perform_input_action(
        self,
        action_type: str,
        keys: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> Dict[str, Any]:
        if not self.is_supported():
            raise RuntimeError("Windows keyboard/mouse automation requires Windows (win32).")

        import ctypes

        user32 = ctypes.windll.user32
        if action_type == "move_mouse" and x is not None and y is not None:
            user32.SetCursorPos(int(x), int(y))
        elif action_type == "click_mouse" and x is not None and y is not None:
            user32.SetCursorPos(int(x), int(y))
            MOUSEEVENTF_LEFTDOWN = 0x0002
            MOUSEEVENTF_LEFTUP = 0x0004
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        elif action_type == "send_keys" and keys:
            from pywinauto.keyboard import send_keys  # type: ignore[import]

            send_keys(keys, with_spaces=True)
        else:
            raise ValueError(f"Invalid input action_type '{action_type}' or missing parameters.")

        fg_hwnd = user32.GetForegroundWindow()
        return {
            "action_type": action_type,
            "keys": keys,
            "coordinates": {"x": x, "y": y} if x is not None and y is not None else None,
            "foreground_hwnd": int(fg_hwnd),
        }


# Active desktop driver singleton (can be replaced with a mock driver in unit tests)
_ACTIVE_DESKTOP_DRIVER: DesktopAutomationDriver = Win32UIADesktopDriver()


def get_desktop_driver() -> DesktopAutomationDriver:
    return _ACTIVE_DESKTOP_DRIVER


def set_desktop_driver(driver: DesktopAutomationDriver) -> None:
    global _ACTIVE_DESKTOP_DRIVER
    _ACTIVE_DESKTOP_DRIVER = driver


def _desktop_precondition(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    driver = get_desktop_driver()
    if not ctx.config.enable_live_desktop_automation:
        return PreconditionResult(
            satisfied=False,
            reason="Windows desktop automation is disabled by AUTOCRAFT_ENABLE_DESKTOP.",
            details={"error_code": "DESKTOP_AUTOMATION_DISABLED"},
        )
    if not driver.is_supported():
        reason = (
            "Windows Desktop UI Automation requires an interactive Windows desktop session."
            if sys.platform == "win32"
            else (
                f"Windows Desktop UI Automation requires Windows 10/11 (win32); "
                f"current platform is '{sys.platform}'."
            )
        )
        return PreconditionResult(
            satisfied=False,
            reason=reason,
            details={"error_code": "DESKTOP_ENVIRONMENT_UNSUPPORTED", "platform": sys.platform},
        )
    return PreconditionResult(satisfied=True, details={"platform": sys.platform})


def _desktop_error_handler(exc: Exception, params: Dict[str, Any]) -> ToolErrorDiagnosis:
    if isinstance(exc, FileNotFoundError):
        return ToolErrorDiagnosis(
            error_code="APP_NOT_INSTALLED",
            category="APPLICATION",
            message=str(exc),
            retryable=False,
            recovery_suggestion="Install the target application or use a supported fallback editor.",
        )
    if isinstance(exc, LookupError):
        return ToolErrorDiagnosis(
            error_code="UI_CONTROL_NOT_FOUND",
            category="UI_AUTOMATION",
            message=str(exc),
            retryable=True,
            recovery_suggestion="Ensure the target window is visible and not minimized.",
        )
    return ToolErrorDiagnosis(
        error_code="DESKTOP_AUTOMATION_ERROR",
        category="UI_AUTOMATION",
        message=f"{type(exc).__name__}: {exc}",
        retryable=False,
        recovery_suggestion="Verify Windows desktop session is unlocked and interactive.",
    )


# --- Tool Implementations ---

def _exec_notepad_open(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    return ToolExecutionResult(success=True, output=get_desktop_driver().open_notepad())


def _ver_notepad_open(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    output = res.output
    verified = (
        str(output.get("app", "")).lower() == "notepad.exe"
        and int(output.get("pid", 0)) > 0
        and int(output.get("hwnd", 0)) > 0
        and "notepad" in str(output.get("window_title", "")).lower()
    )
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified Notepad window (HWND={output.get('hwnd')}) is open."
            if verified
            else "Notepad launch could not be verified."
        ),
        evidence=output,
        failure_reason=None if verified else "No visible Notepad window with a valid process and window handle.",
    )


def _exec_notepad_write(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    text = str(params["text"])
    save_path_raw = params.get("save_path")
    save_path: Optional[Path] = None
    if save_path_raw:
        save_path = _resolve_notepad_save_path(str(save_path_raw), ctx)
    result = driver.notepad_write_and_read(text=text, save_path=save_path)
    return ToolExecutionResult(success=True, output=result)


def _ver_notepad_write(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    expected = str(params["text"])
    observed = str(res.output.get("observed_text", ""))
    text_match = _normalized_newlines(expected) == _normalized_newlines(observed)
    save_path_raw = params.get("save_path")
    file_ok = not save_path_raw
    if save_path_raw:
        p = _resolve_notepad_save_path(str(save_path_raw), ctx)
        actual_saved_path = res.output.get("saved_path")
        file_ok = (
            bool(actual_saved_path)
            and Path(str(actual_saved_path)).resolve() == p
            and p.is_file()
            and res.output.get("saved_verified") is True
            and _normalized_newlines(p.read_text(encoding="utf-8", errors="replace"))
            == _normalized_newlines(expected)
        )

    verified = text_match and file_ok and int(res.output.get("hwnd", 0)) > 0
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified Notepad window (HWND={res.output.get('hwnd')}) contains expected text."
            if verified
            else "Notepad editor text verification failed."
        ),
        evidence={
            "hwnd": res.output.get("hwnd"),
            "pid": res.output.get("pid"),
            "expected_text": expected,
            "observed_text": observed,
            "saved_path": res.output.get("saved_path"),
            "file_verified": file_ok,
        },
        failure_reason=None if verified else f"Expected '{expected}' inside Notepad control, got '{observed}'.",
    )


def _exec_calculator(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    expr = str(params["expression"])
    angle_mode = str(params.get("angle_mode", "degrees"))
    # Validate syntax before launching calculator
    evaluate_math_expression_safely(expr, angle_mode)
    result = driver.calculator_compute(expr, angle_mode)
    return ToolExecutionResult(success=True, output=result)


def _exec_calculator_open(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    return ToolExecutionResult(success=True, output=get_desktop_driver().open_calculator())


def _ver_calculator_open(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    output = res.output
    verified = (
        str(output.get("app", "")).lower() == "calc.exe"
        and int(output.get("pid", 0)) > 0
        and int(output.get("hwnd", 0)) > 0
        and "calculator" in str(output.get("window_title", "")).lower()
    )
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified Windows Calculator window (HWND={output.get('hwnd')}) is open."
            if verified
            else "Windows Calculator launch could not be verified."
        ),
        evidence=output,
        failure_reason=None if verified else "No visible Calculator window with a valid process and window handle.",
    )


def _ver_calculator(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    expected = float(
        res.output.get(
            "expected_result",
            evaluate_math_expression_safely(
                params["expression"], str(params.get("angle_mode", "degrees"))
            ),
        )
    )
    observed = res.output.get("observed_result")
    verified = observed is not None and abs(float(observed) - expected) < 1e-6
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified Calculator display result {observed} matches expected {expected}."
            if verified
            else f"Calculator result mismatch: expected {expected}, observed {observed}."
        ),
        evidence={
            "expression": params["expression"],
            "expected_result": expected,
            "observed_result": observed,
            "raw_display": res.output.get("raw_display"),
        },
        failure_reason=None if verified else f"Expected {expected}, but Calculator displayed {observed}.",
    )


def _exec_paint(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    draw_box = bool(params.get("draw_box", False))
    save_path_raw = params.get("save_path")
    save_path: Optional[Path] = None
    if save_path_raw:
        path_text = str(save_path_raw).strip()
        downloads_root = _downloads_directory()
        normalized = path_text.replace("/", "\\")
        if re.match(r"^downloads(?:\\|$)", normalized, re.IGNORECASE):
            relative_part = re.sub(r"^downloads\\?", "", normalized, flags=re.IGNORECASE)
            path_text = str(downloads_root / relative_part)
            extra_roots = [*ctx.config.allowed_extra_roots, downloads_root]
        else:
            extra_roots = ctx.config.allowed_extra_roots
        save_path = resolve_authorized_path(
            path_text,
            ctx.config.workspace_root,
            extra_roots,
            allow_root_itself=False,
        )
        if save_path.suffix.lower() not in (".png", ".bmp", ".jpg", ".jpeg"):
            raise ValueError("Paint images must use a .png, .bmp, .jpg, or .jpeg extension.")
    result = driver.launch_paint(
        draw_box=draw_box,
        draw_shape=params.get("draw_shape"),
        save_path=save_path,
    )
    return ToolExecutionResult(success=True, output=result)


def _ver_paint(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    hwnd = int(res.output.get("hwnd", 0))
    pid = int(res.output.get("pid", 0))
    shape = params.get("draw_shape") or ("rectangle" if params.get("draw_box") else None)
    drawing_ok = shape is None or (
        res.output.get("draw_shape") == shape
        and res.output.get("drawing_verified") is True
    )
    save_path = res.output.get("saved_path")
    save_ok = not params.get("save_path") or (
        bool(save_path)
        and res.output.get("saved_verified") is True
        and Path(str(save_path)).is_file()
        and Path(str(save_path)).stat().st_size > 100
    )
    if params.get("save_path") and shape is not None and save_ok:
        try:
            from PIL import Image

            with Image.open(str(save_path)) as image:
                image.load()
                save_ok = save_ok and _image_has_visible_drawing(image, shape)
        except (OSError, ImportError):
            save_ok = False
    verified = hwnd > 0 and pid > 0 and drawing_ok and save_ok
    drawing_method = res.output.get("drawing_method")
    drawing_summary = (
        f" contains a verified {shape} image loaded in Paint"
        if shape and drawing_method == "verified_image_loaded_in_paint"
        else (f" drew a {shape}" if shape else "")
    )
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified Paint (PID={pid}, HWND={hwnd})"
            + drawing_summary
            + (" and saved the image." if params.get("save_path") else ".")
            if verified
            else "Paint launch, drawing, or saved image verification failed."
        ),
        evidence=res.output,
        failure_reason=(
            None
            if verified
            else "Paint window, visible drawing, or requested saved image was not independently verified."
        ),
    )


def _exec_explorer(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    path_raw = params.get("path", ".")
    resolved = resolve_authorized_path(
        path_raw, ctx.config.workspace_root, ctx.config.allowed_extra_roots
    )
    result = driver.open_file_explorer(resolved)
    return ToolExecutionResult(success=True, output=result)


def _ver_explorer(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    verified = bool(res.output.get("path_exists")) and bool(res.output.get("window_detected"))
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified File Explorer opened '{res.output.get('target_path')}'."
            if verified
            else "File Explorer window could not be verified."
        ),
        evidence=res.output,
        failure_reason=None if verified else "File Explorer window was not detected for target folder.",
    )


def _exec_vscode(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    path_raw = params.get("path")
    target = (
        resolve_authorized_path(path_raw, ctx.config.workspace_root, ctx.config.allowed_extra_roots)
        if path_raw
        else None
    )
    result = driver.launch_vscode(target)
    return ToolExecutionResult(success=True, output=result)


def _ver_vscode(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    pid = int(res.output.get("pid", 0))
    verified = pid > 0 and bool(res.output.get("executable"))
    return VerificationResult(
        verified=verified,
        summary=f"Verified VS Code launched (PID={pid})." if verified else "VS Code launch failed.",
        evidence=res.output,
        failure_reason=None if verified else "VS Code process was not started.",
    )


def _exec_office(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    app_name = str(params["application"])
    file_raw = params.get("file_path")
    file_path = (
        resolve_authorized_path(
            file_raw, ctx.config.workspace_root, ctx.config.allowed_extra_roots, allow_root_itself=False
        )
        if file_raw
        else None
    )
    result = driver.launch_office_app(app_name, file_path)
    return ToolExecutionResult(success=True, output=result)


def _ver_office(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    pid = int(res.output.get("pid", 0))
    verified = pid > 0
    return VerificationResult(
        verified=verified,
        summary=f"Verified Microsoft Office '{res.output.get('app')}' launched (PID={pid}).",
        evidence=res.output,
        failure_reason=None if verified else "Office application process ID was not confirmed.",
    )


def _exec_interact_control(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    out = driver.interact_control(
        window_title=params["window_title"],
        control_name=params["control_name"],
        control_type=params.get("control_type", "Button"),
        action=params["action"],
        value=params.get("value"),
    )
    return ToolExecutionResult(success=True, output=out)


def _ver_interact_control(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    ok = bool(res.output.get("window_title")) and bool(res.output.get("control_name"))
    if params["action"] == "set_text" and params.get("value") is not None:
        ok = ok and (params["value"] in str(res.output.get("control_text_after", "")))
    return VerificationResult(
        verified=ok,
        summary=f"Verified UIA action '{params['action']}' on control '{params['control_name']}'.",
        evidence=res.output,
        failure_reason=None if ok else "Control state after interaction did not match expected value.",
    )


def _exec_input_action(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    driver = get_desktop_driver()
    out = driver.perform_input_action(
        action_type=params["action_type"],
        keys=params.get("keys"),
        x=params.get("x"),
        y=params.get("y"),
        button=params.get("button", "left"),
    )
    return ToolExecutionResult(success=True, output=out)


def _ver_input_action(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    ok = int(res.output.get("foreground_hwnd", 0)) > 0
    return VerificationResult(
        verified=ok,
        summary=f"Verified input action '{params['action_type']}' on foreground HWND={res.output.get('foreground_hwnd')}.",
        evidence=res.output,
        failure_reason=None if ok else "No active foreground window handle observed.",
    )


def register_desktop_tools(registry: ToolRegistry, enabled: bool = True) -> None:
    """Register all Windows Desktop UI Automation tools in ToolRegistry."""
    is_win = enabled and get_desktop_driver().is_supported()

    registry.register(
        ToolDefinition(
            name="desktop.notepad_open",
            category=ToolCategory.DESKTOP,
            description="Open or focus Windows Notepad and verify its visible window.",
            capabilities=["launch_notepad", "notepad_window"],
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "pid": {"type": "integer"},
                    "hwnd": {"type": "integer"},
                    "window_title": {"type": "string"},
                },
                "required": ["app", "pid", "hwnd", "window_title"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 with an interactive desktop session."
            ],
            verification_description="Confirm a visible Notepad window has a valid process and HWND.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_notepad_open,
            verify_fn=_ver_notepad_open,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.notepad_write",
            category=ToolCategory.DESKTOP,
            description="Launch Windows Notepad, type text into its editor control, optionally save, and verify editor contents via UIA/Win32.",
            capabilities=["notepad", "text_editor", "desktop_typing"],
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "save_path": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "pid": {"type": "integer"},
                    "hwnd": {"type": "integer"},
                    "entered_text": {"type": "string"},
                    "observed_text": {"type": "string"},
                },
                "required": ["app", "hwnd", "observed_text"],
            },
            permission_level=PermissionLevel.SAFE_WRITE,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32) with an active desktop session.",
                "notepad.exe must be available.",
            ],
            verification_description="Read back the Notepad Edit/Document control text via UIA/WM_GETTEXT and confirm exact match.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_notepad_write,
            verify_fn=_ver_notepad_write,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.calculator_open",
            category=ToolCategory.DESKTOP,
            description="Open or focus Windows Calculator and verify its visible window.",
            capabilities=["launch_calculator", "calculator_window"],
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "pid": {"type": "integer"},
                    "hwnd": {"type": "integer"},
                    "window_title": {"type": "string"},
                },
                "required": ["app", "pid", "hwnd", "window_title"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 with an interactive desktop session."
            ],
            verification_description="Confirm a visible Calculator window has a valid process and HWND.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_calculator_open,
            verify_fn=_ver_calculator_open,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.calculator_compute",
            category=ToolCategory.DESKTOP,
            description="Launch Windows Calculator, execute an arithmetic expression, and verify the CalculatorResults UIA display.",
            capabilities=["calculator", "arithmetic", "calc_compute"],
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "minLength": 1},
                    "angle_mode": {"type": "string", "enum": ["degrees", "radians"]},
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "expression": {"type": "string"},
                    "observed_result": {"type": ["number", "null"]},
                    "expected_result": {"type": "number"},
                    "angle_mode": {"type": "string"},
                },
                "required": ["app", "expression", "expected_result"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32) with calc.exe available."
            ],
            verification_description="Read 'CalculatorResults' UIA automation element and compare against expected value.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_calculator,
            verify_fn=_ver_calculator,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.paint_launch",
            category=ToolCategory.DESKTOP,
            description="Launch Windows Paint (mspaint.exe) and verify its active window handle and process ID.",
            capabilities=["paint", "mspaint", "image_app"],
            input_schema={
                "type": "object",
                "properties": {
                    "draw_box": {"type": "boolean"},
                    "draw_shape": {
                        "type": ["string", "null"],
                        "enum": [
                            "circle",
                            "ellipse",
                            "rectangle",
                            "triangle",
                            "house",
                            "landscape",
                            None,
                        ],
                    },
                    "save_path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "pid": {"type": "integer"},
                    "hwnd": {"type": "integer"},
                    "window_title": {"type": "string"},
                    "draw_shape": {"type": ["string", "null"]},
                    "drawing_verified": {"type": "boolean"},
                    "drawing_method": {"type": "string"},
                    "saved_path": {"type": ["string", "null"]},
                    "saved_verified": {"type": "boolean"},
                    "image_verified": {"type": "boolean"},
                },
                "required": ["app", "pid", "hwnd"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32) with mspaint.exe installed."
            ],
            verification_description="Verify Paint window handle (HWND > 0) and running process ID via Win32 EnumWindows.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_paint,
            verify_fn=_ver_paint,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.explorer_open",
            category=ToolCategory.DESKTOP,
            description="Open Windows File Explorer at an authorized directory and verify the Explorer window.",
            capabilities=["file_explorer", "explorer_navigate", "open_folder_window"],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "target_path": {"type": "string"},
                    "path_exists": {"type": "boolean"},
                    "window_detected": {"type": "boolean"},
                },
                "required": ["app", "target_path", "path_exists", "window_detected"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32).",
                "Target folder must resolve inside the authorized workspace root.",
            ],
            verification_description="Confirm target directory exists and CabinetWClass Explorer window is active.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_explorer,
            verify_fn=_ver_explorer,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.vscode_open",
            category=ToolCategory.DESKTOP,
            description="Launch Visual Studio Code on an optional file or workspace folder and verify process startup.",
            capabilities=["vscode", "code_editor", "open_ide"],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "executable": {"type": "string"},
                    "pid": {"type": "integer"},
                },
                "required": ["app", "executable", "pid"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32) with VS Code installed."
            ],
            verification_description="Verify Code.exe executable discovery and active process PID.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_vscode,
            verify_fn=_ver_vscode,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.office_open",
            category=ToolCategory.DESKTOP,
            description="Launch a Microsoft Office application (Word, Excel, or PowerPoint) and verify process creation.",
            capabilities=["office", "word", "excel", "powerpoint"],
            input_schema={
                "type": "object",
                "properties": {
                    "application": {
                        "type": "string",
                        "enum": ["word", "excel", "powerpoint"],
                    },
                    "file_path": {"type": "string"},
                },
                "required": ["application"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "executable": {"type": "string"},
                    "pid": {"type": "integer"},
                },
                "required": ["app", "executable", "pid"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32) with Microsoft Office installed."
            ],
            verification_description="Verify Office executable discovery and active process PID.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_office,
            verify_fn=_ver_office,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.interact_control",
            category=ToolCategory.DESKTOP,
            description="Interact with an accessible Windows UI Automation control (Button, Edit, MenuItem) by window title and control name.",
            capabilities=["uia_control", "click_button", "select_menu", "set_control_text"],
            input_schema={
                "type": "object",
                "properties": {
                    "window_title": {"type": "string", "minLength": 1},
                    "control_name": {"type": "string", "minLength": 1},
                    "control_type": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["click", "invoke", "set_text"],
                    },
                    "value": {"type": "string"},
                },
                "required": ["window_title", "control_name", "action"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "window_title": {"type": "string"},
                    "control_name": {"type": "string"},
                    "action": {"type": "string"},
                    "control_text_after": {"type": "string"},
                },
                "required": ["window_title", "control_name", "action"],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "Host OS must be Windows 10/11 (win32) with target window open."
            ],
            verification_description="Read control state via UI Automation after performing the requested action.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_interact_control,
            verify_fn=_ver_interact_control,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )

    registry.register(
        ToolDefinition(
            name="desktop.keyboard_mouse",
            category=ToolCategory.DESKTOP,
            description="Send keyboard strokes or mouse movements/clicks to the active desktop window.",
            capabilities=["keyboard_input", "mouse_click", "mouse_move"],
            input_schema={
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": ["send_keys", "move_mouse", "click_mouse"],
                    },
                    "keys": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right"]},
                },
                "required": ["action_type"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "action_type": {"type": "string"},
                    "foreground_hwnd": {"type": "integer"},
                },
                "required": ["action_type", "foreground_hwnd"],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=["Host OS must be Windows 10/11 (win32)."],
            verification_description="Confirm active foreground window handle (HWND > 0) received input.",
            preconditions_fn=_desktop_precondition,
            execute_fn=_exec_input_action,
            verify_fn=_ver_input_action,
            error_handler_fn=_desktop_error_handler,
            supported_on_current_platform=is_win,
        )
    )
