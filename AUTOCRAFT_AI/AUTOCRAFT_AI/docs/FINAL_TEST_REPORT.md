# AUTOCRAFT AI — Final Test Report

## Summary

The project was tested on an interactive Windows 11 desktop with Chrome, Edge,
Notepad, Calculator, and network access available. The live browser tests did not
encounter a CAPTCHA.

| Test | Result |
| --- | --- |
| Complete backend suite (`python -m pytest -q`) | 29 passed, 1 skipped; 3 subtests passed |
| Real Windows and external-browser suite | 6 passed |
| Frontend tests (`npm test`) | 6 passed, 0 failed |
| Frontend production build (`npm run build`) | Passed (`tsc` and Vite) |
| Live dashboard/browser interaction check | Passed |

The single backend skip is the non-Windows-only honesty check, which is
intentionally skipped on Windows. No test failures remain in the final runs.

## Real Windows and Browser Results

`python -m pytest -q tests/windows_live` ran all six live tests successfully:

- **Notepad:** Opened Notepad, entered `AUTOCRAFT AI TEST 123`, verified the
  exact text, then immediately ran a second task and verified `Hello AUTOCRAFT`.
- **Calculator:** Entered `448*33` and independently read back `14784`.
- **Chrome:** Opened the requested Chrome channel and navigated to the real
  `https://www.flipkart.com/` website.
- **Microsoft Edge:** Opened the requested Edge channel and navigated to the
  real `https://www.wikipedia.org/` website.
- **Paint and File Explorer:** Verified live application/window launches.

The live dashboard check used a fresh backend on port 8001 with a temporary
workspace and database, so it did not modify the normal project task history.
Through the actual browser UI, it previewed a Notepad plan, ran two consecutive
Notepad tasks with exact verified results, cancelled an active task using
**Stop**, cleared task history, and activated/reset **Emergency Stop**.
The task field remained editable and focused after Preview, Stop, Clear History,
Emergency Stop, and reset. Browser keyboard checks exercised Ctrl+A, Ctrl+C,
Ctrl+V, and End navigation. At a 706 px viewport, page content width was 691 px,
so there was no horizontal overflow.

## Root Causes Addressed

- Browser phrases such as “open Flipkart in Chrome” were not consistently
  translated into a supported browser-navigation plan; browser selection and
  URL parsing could also lose required details.
- The prior production dashboard builder recreated the DOM on each update,
  destroying the focused text field and selection.
- Notepad's launcher PID and Windows 11 UI Automation `Document` control did not
  match the automation's original assumptions.
- Calculator window selection used an ambiguous UIA title query. Windows may
  expose multiple Calculator top-level handles, and the selected UIA wrapper
  requires wrapper-based descendant lookup for `CalculatorResults`.
- Task completion, cancellation during active calls, and SQLite connection
  cleanup needed stricter handling to avoid unverified success or locked DB
  files.

## Frontend Checks and Warnings

The six frontend checks passed, including task-input availability, backend
route consistency, verified progress handling, and dashboard rendering. The
production build completed TypeScript checking and generated the Vite bundle.
There is no configured ESLint/lint script.

Non-blocking warnings observed during validation:

- Vite's CommonJS Node API is deprecated.
- `ast.Num` and Starlette's legacy `httpx` TestClient integration emit
  deprecation warnings.
- pywinauto may warn that `WaitForInputIdle` failed for the Notepad launcher;
  the live Notepad window and entered text were nevertheless detected and
  verified.

## Run Locally on Windows

From the project directory:

```powershell
.\setup_windows.bat
.\start_autocraft.bat
```

Then open `http://127.0.0.1:8000`. The PowerShell setup alternative is
`.\setup_windows.ps1`, and the PowerShell launcher is `.\start_autocraft.ps1`.

To run the backend tests:

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m pytest -q tests\windows_live
```

To test/build the frontend:

```powershell
Set-Location .\frontend
npm test
npm run build
```
