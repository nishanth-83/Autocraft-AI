# AUTOCRAFT AI — Known Limitations

In accordance with engineering transparency requirements, the following limitations apply to AUTOCRAFT AI v1.0.0:

1. **Deterministic Rule & Template Planner Scope**
   - The offline self-planning engine (`backend/planner/`) uses deterministic regular expressions, lexical normalizers (English, Tamil, and Thanglish), and task templates so that it operates 100% offline without requiring Ollama or external LLM APIs.
   - Highly idiomatic, ambiguous, or open-ended conversational instructions that do not match the supported grammar patterns will raise a `PlanningError` rather than guessing or hallucinating actions.

2. **Windows UI Automation (UIA) Session Requirements**
   - Real desktop automation (`notepad.exe`, `calc.exe`, `mspaint.exe`, `explorer.exe`, `Code.exe`, Microsoft Office) requires a live, unlocked, interactive **Windows 10 or Windows 11 (`win32`)** desktop session.
   - If the Windows screen is locked (`Win+L`) or running inside a headless Windows Server Session 0 service, UIA focus and keyboard/mouse input (`SendInput` / `type_keys`) will be blocked by the Windows Window Manager.
   - On Windows 11, the redesigned tabbed Notepad (`Notepad.exe` UWP/WinUI3) uses a `RichEditD2DPT` / `Document` child control; `Win32UIADesktopDriver` supports both `pywinauto` UIA (`Document` / `Edit`) and `FindWindowExW` for `Edit` and `RichEditD2DPT`, but third-party Notepad replacements may expose different control hierarchies.
   - Paint drawing and image-pixel verification require Pillow and an interactive desktop. If the canvas ignores synthetic mouse input, AUTOCRAFT opens a generated, pixel-verified shape image in Paint and exposes that fallback method; missing tools or unreadable images fail verification rather than reporting success.

3. **UAC / Elevated Application Boundaries (UIPI)**
   - Windows User Interface Privilege Isolation (UIPI) prevents a standard-privilege process from sending UI Automation inputs or Win32 messages to an Administrator-elevated window. AUTOCRAFT AI runs with standard user privileges by design and cannot automate UAC prompts or elevated system administration tools.

4. **CAPTCHA, MFA, and Authentication Walls**
   - By strict security policy, AUTOCRAFT AI **never** bypasses CAPTCHA challenges (`g-recaptcha`, `h-captcha`, `cf-turnstile`) or multi-factor authentication prompts. When a browser navigation encounters a CAPTCHA or security check, execution halts immediately with `SECURITY_CAPTCHA_OR_AUTH_BLOCKED`.

5. **Live Desktop and External Browser Tests**
   - The tests in `tests/windows_live/` exercise real Windows UI Automation and Playwright navigation to Amazon, Flipkart, YouTube, Wikipedia, and Python.org. They require browser availability and network access; CAPTCHA or authentication challenges are reported as blocked and are never bypassed.
