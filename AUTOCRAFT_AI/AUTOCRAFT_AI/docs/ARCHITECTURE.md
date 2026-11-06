# AUTOCRAFT AI — System Architecture Documentation

## 1. Architectural Overview

AUTOCRAFT AI is designed around a strict separation of concerns across five core subsystems:

1. **Multilingual Self-Planning Engine (`backend/planner/`)**: Offline deterministic natural language parser and template-based DAG planner supporting English, Tamil (`தமிழ்`), and Thanglish.
2. **Modular Tool Registry (`backend/tools/`)**: Contract-enforced registry where every tool declares its capabilities, JSON input/output schemas, preconditions, execution handler, independent verification handler, error classifier, and permission level.
3. **Closed-Loop Autonomous Orchestrator (`backend/execution/loop.py`)**: State machine implementing `OBSERVE → PLAN → VALIDATE → ACT → VERIFY → RECOVER` with bounded retries, fallback tool replanning, human-in-the-loop approval gates, and emergency stop controls.
4. **Security & Persistence Layer (`backend/security.py`, `backend/database.py`)**: Canonical workspace path confinement, command blocklisting, automatic sensitive-data redaction, and thread-safe SQLite storage.
5. **Operator Dashboard (`frontend/src/`)**: React + TypeScript interface reflecting real-time backend state, verification evidence, recovery logs, and approval queues.

---

## 2. Autonomous Execution Loop (`OBSERVE → PLAN → VALIDATE → ACT → VERIFY → RECOVER`)

Every task submitted to `POST /api/tasks` transitions through six explicit phases:

### Phase 1: `OBSERVE`
`AutonomousOrchestrator.observe_environment()` captures a live snapshot of:
- Host OS platform (`win32`, `linux`, etc.) and release version.
- Current files and directories inside the authorized `workspace_root`.
- Windows Desktop UIA support status and open top-level window count.
- Active browser automation engine (`playwright` or `selenium_chromium`) and current page state.
- Emergency stop status.

### Phase 2: `PLAN`
`SelfPlanningEngine.create_plan(goal)`:
1. Detects the instruction language (`en`, `ta`, or `thanglish`).
2. Splits compound sentences into ordered atomic clauses while preserving quoted literals and keeping paired application actions (e.g., `"open notepad and type ..."`, `"open Paint and draw a circle"`) unified.
3. Normalizes Tamil and Thanglish clauses into canonical action structures.
4. Matches each clause against deterministic tool templates, wiring sequential `depends_on` dependencies and optional `fallback_tool` definitions.
5. Rejects empty, malformed, or unsupported goals with a `PlanningError`—never confusing understanding a goal with completing it.

YouTube requests that ask to open a first result are parsed into an ordered YouTube navigation step and the registered `browser.youtube_first_result` action. Only the extracted search phrase is submitted. The action waits for playable video results, opens the first non-ad result, and verifies the player; `play` requests additionally require observed playback progress. Consent, CAPTCHA, and sign-in barriers are reported rather than bypassed. Opening Notepad or Calculator uses separate launch tools, while text entry and arithmetic remain distinct actions.

### Phase 3: `VALIDATE`
`PlanValidator.validate(steps)` inspects the generated plan before any tool is invoked:
- Ensures the plan is non-empty and within `max_plan_steps`.
- Confirms every `tool_name` (and `fallback_tool`) is registered in `ToolRegistry`.
- Validates every step's `parameters` against the tool's JSON Schema (`jsonschema.validate`).
- Verifies the dependency graph is a valid Directed Acyclic Graph (DAG) with no missing step IDs, self-dependencies, or cycles (via 3-color depth-first search).
- Runs `evaluate_action_security()` on every step:
  - Immediately blocks path traversal outside `workspace_root`, access to credential files (`.env`, `id_rsa`, `.pem`), or destructive shell commands.
  - Marks steps requiring human authorization (`requires_approval = True`) for file deletion, overwriting existing files, or submitting sensitive forms.

### Phase 4: `ACT`
For each step in topological order:
- Verifies all `depends_on` upstream steps have status `COMPLETED`.
- If `requires_approval` is `True` and the step has not yet been approved by the operator, transitions the task to `WAITING_APPROVAL`, creates an `ApprovalRequest` record in SQLite, and pauses execution cleanly.
- Checks the cancellation token and emergency stop flag.
- Dispatches the tool inside a bounded `ThreadPoolExecutor` enforcing `step_timeout_seconds`:
  1. Re-validates input schema.
  2. Executes `tool.preconditions_fn(parameters, context)`.
  3. Executes `tool.execute_fn(parameters, context)`.
  4. Validates output against `tool.output_schema`.

### Phase 5: `VERIFY`
Immediately after `execute_fn` succeeds, `ToolRegistry.dispatch` calls `tool.verify_fn(parameters, exec_result, context)`:
- **Filesystem verification**: checks `os.path.isdir`, `os.path.isfile`, file byte length, and computes a fresh SHA-256 hash from disk to confirm the written or copied bytes match expectations.
- **Desktop verification**: reads back the Notepad editor control text via UIA/`WM_GETTEXT`, safely evaluates calculator expressions before comparing them to the `CalculatorResults` UIA value, and checks Paint's requested shape pixels in the canvas or saved image in addition to its `HWND` and `PID`. If Paint ignores synthetic canvas input, it opens an independently verified shape image in Paint and reports that method; saved image formats are selected explicitly.
- **Browser verification**: normalizes arbitrary valid HTTP(S) URLs, checks the final HTTP(S) URL after same-site redirects, page title, DOM readiness, `window.scrollY`, input values, and visible body text. Navigation settles on DOM readiness rather than blindly replaying a failed navigation; CAPTCHA/auth challenges are never bypassed. YouTube playback is separately verified from the HTML5 player's ready state, paused state, and advancing current time.
- If `verify_fn` returns `verified=False`, the step is marked failed with `VERIFICATION_FAILED` and enters `RECOVER`.

### Phase 6: `RECOVER`
When preconditions, execution, or verification fail, `ErrorRecoveryEngine.decide_recovery()` classifies the error into an `ErrorCategory` and selects a deterministic `RecoveryActionType`:
- `REQUEST_APPROVAL`: if runtime preconditions discover an existing file conflict (`OVERWRITE_APPROVAL_REQUIRED`), pauses the task and requests user approval.
- `RETRY`: for transient errors (`ACTION_TIMEOUT`, `INACCESSIBLE_CONTROL`, `VERIFICATION_FAILURE`, `NETWORK_ERROR`) while `attempt_count <= step.max_retries`.
- `REPLAN_FALLBACK`: if a primary application is missing (`APP_NOT_INSTALLED`) or retries are exhausted and a safe `fallback_tool` is defined on the step, rewrites the step to use the fallback tool and resets the attempt counter once.
- `ABORT_HONESTLY`: halts execution immediately when retries are exhausted or a non-recoverable error (`SECURITY_BLOCKED`, `PLATFORM_UNSUPPORTED`, `TASK_CANCELLED`) occurs, recording the exact failure details in SQLite.

---

## 3. Security Architecture

- **Workspace Confinement**: `resolve_authorized_path()` resolves symlinks and relative segments (`Path.resolve()`) and verifies `resolved.relative_to(workspace_root)`. Any attempt to escape via `../` raises `SecurityViolationError`.
- **Anti-Self-Modification**: paths resolving inside `backend/` or `frontend/` are unconditionally rejected.
- **Credential & Secret Protection**:
  - Prohibited filenames (`.env*`, `id_rsa*`, `*.pem`, `*.pfx`, `*.key`, `credentials.json`) cannot be read, modified, or deleted.
  - `redact_sensitive_data()` recursively scrubs dictionary keys and inline patterns matching passwords, OTPs, API keys, bearer tokens, and payment card numbers before any record is written to SQLite.
- **CAPTCHA & Auth Non-Bypass**: `browser.py` inspects loaded DOM content for CAPTCHA widgets (`g-recaptcha`, `h-captcha`, `cf-turnstile`) and halts with `SECURITY_CAPTCHA_OR_AUTH_BLOCKED`.
- **Emergency Stop**: `POST /api/emergency-stop` sets a global kill switch, cancels all active tasks, and closes open browser sessions immediately.
