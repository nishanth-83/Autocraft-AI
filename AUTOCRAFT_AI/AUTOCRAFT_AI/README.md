# AUTOCRAFT AI — Autonomous Desktop AI Agent

**AUTOCRAFT AI** is a production-oriented autonomous desktop AI agent built from scratch with a Python + FastAPI backend, a React + TypeScript operator dashboard, SQLite state persistence, real browser automation (Playwright & Chromium WebDriver), real Windows 10/11 UI Automation (`pywinauto` & native Win32 APIs), confined filesystem tools, and a deterministic offline self-planning engine supporting **English**, **Tamil (தமிழ்)**, and **Thanglish**.

## Core Capabilities

1. **Deterministic Offline Self-Planning Engine (`backend/planner/`)**
   - Understands **English**, **Tamil**, and **Thanglish** instructions offline without requiring Ollama or external AI APIs.
   - Converts goals into ordered executable steps (`PlanStep`) with explicit dependencies (`depends_on`), tool bindings, parameters, and verification conditions.
   - Validates every plan against the `ToolRegistry`, JSON Schemas, DAG cycle detection, and security policies before executing any action.

2. **Autonomous Closed-Loop Execution (`backend/execution/loop.py`)**
   - Implements `OBSERVE -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER`.
   - Never reports success without verified execution evidence.
   - Supports bounded retries, fallback tool replanning (`REPLAN_FALLBACK`), human-in-the-loop approval gates (`WAITING_APPROVAL`), task cancellation, and an immediate **Emergency Stop** kill switch.

3. **Real Windows Desktop Automation (`backend/tools/desktop.py`)**
   - Supports Notepad (`notepad.exe`), Calculator (`calc.exe`), Paint (`mspaint.exe`), File Explorer (`explorer.exe`), VS Code (`Code.exe`), Microsoft Office (`WINWORD.EXE`, `EXCEL.EXE`, `POWERPNT.EXE`), and accessible UIA controls.
   - Independently verifies exact Notepad text (including multiline sequences), Calculator display values, and Paint shape/saved-image pixels. Calculator expressions support arithmetic, powers, parentheses, square roots, common logarithms, natural logarithms, and trigonometry (degrees by default; radians on request).
   - Paint selects the requested shape tool, verifies the visible result, and reports whether it drew on the canvas or loaded a verified image when Windows ignores synthetic canvas input. Requested save formats are selected explicitly.

4. **Real Browser Automation (`backend/tools/browser.py`)**
   - Controls Google Chrome, Microsoft Edge, or Chromium via **Playwright** (`playwright.sync_api`) or **Selenium Chromium WebDriver**.
   - Supports arbitrary valid HTTP(S) URLs and direct navigation to Amazon, Flipkart, YouTube, Wikipedia, and Python.org, plus searching, clicking accessible elements, scrolling, reading visible DOM text, and filling forms.
   - Never bypasses CAPTCHA or authentication walls (`SECURITY_CAPTCHA_OR_AUTH_BLOCKED`).

5. **Confined Filesystem Management (`backend/tools/filesystem.py`)**
   - Supports creating folders and files, reading, writing, appending, moving/renaming, copying, organizing folders by category, listing directories, and deleting with mandatory approval.
   - Enforces strict path confinement inside `./workspace` and verifies file contents via SHA-256 readback hashes.

## Quick Start on Windows 10/11

1. Extract `AUTOCRAFT_AI.zip` and open Command Prompt or PowerShell in the `AUTOCRAFT_AI` directory.
2. Run the setup script:
   ```cmd
   setup_windows.bat
   ```
   *(Or in PowerShell: `.\setup_windows.ps1`)*
3. Launch the agent and dashboard:
   ```cmd
   start_autocraft.bat
   ```
   *(Or in PowerShell: `.\start_autocraft.ps1`)*
4. Open **`http://127.0.0.1:8000`** in your browser.

The setup scripts install frontend dependencies and build the production dashboard
with Vite. To rebuild it after making frontend changes, run `npm run build` from
the `frontend` directory.

The default browser channel is Playwright-managed Chromium, which the setup
scripts install. Browser health is reported only after the configured browser
has launched successfully. Windows UI automation additionally requires an
interactive Windows desktop session; running in WSL, a container, or a
non-interactive Windows session is reported as unsupported.

## Running Tests

```bash
python run_tests.py
```
Or with `pytest`:
```cmd
venv\Scripts\pytest -v
```

See `docs/ARCHITECTURE.md`, `docs/FINAL_TEST_REPORT.md`, and `docs/KNOWN_LIMITATIONS.md` for detailed documentation.
