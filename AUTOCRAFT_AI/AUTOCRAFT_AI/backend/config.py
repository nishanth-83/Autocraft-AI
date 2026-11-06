"""
Configuration management and validation for AUTOCRAFT AI.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Load environment file if present
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE_DIR = PROJECT_ROOT / "workspace"
DEFAULT_DB_PATH = PROJECT_ROOT / "workspace" / "autocraft_state.db"


class ConfigurationError(ValueError):
    """Raised when configuration values fail validation."""


@dataclass
class AgentConfig:
    """Validated runtime configuration for AUTOCRAFT AI."""

    app_name: str = "AUTOCRAFT AI"
    version: str = "1.0.0"
    host: str = "127.0.0.1"
    port: int = 8000
    workspace_root: Path = field(default_factory=lambda: DEFAULT_WORKSPACE_DIR)
    database_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    allowed_extra_roots: List[Path] = field(default_factory=list)
    browser_channel: str = "chromium"  # "chrome", "msedge", or "chromium"
    browser_headless: bool = True
    browser_timeout_ms: int = 15000
    step_timeout_seconds: float = 30.0
    max_step_retries: int = 2
    max_plan_steps: int = 25
    require_approval_for_overwrite: bool = True
    require_approval_for_delete: bool = True
    require_approval_for_sensitive_forms: bool = True
    enable_live_desktop_automation: bool = field(
        default_factory=lambda: sys.platform == "win32"
    )
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if isinstance(self.workspace_root, str):
            self.workspace_root = Path(self.workspace_root)
        if isinstance(self.database_path, str):
            self.database_path = Path(self.database_path)

        self.workspace_root = self.workspace_root.resolve()
        self.database_path = self.database_path.resolve()
        self.allowed_extra_roots = [Path(p).resolve() for p in self.allowed_extra_roots]
        self.validate()

    def validate(self) -> None:
        """Validate configuration parameters and ensure directories are safe."""
        if not (1 <= int(self.port) <= 65535):
            raise ConfigurationError(f"Invalid port number: {self.port}. Must be 1-65535.")

        if self.browser_channel not in ("chrome", "msedge", "chromium", "auto"):
            raise ConfigurationError(
                f"Invalid browser_channel '{self.browser_channel}'. "
                "Supported values: 'chrome', 'msedge', 'chromium', 'auto'."
            )

        if self.max_step_retries < 0 or self.max_step_retries > 5:
            raise ConfigurationError(
                f"Invalid max_step_retries ({self.max_step_retries}). Must be between 0 and 5."
            )

        if self.step_timeout_seconds <= 0 or self.step_timeout_seconds > 300:
            raise ConfigurationError(
                f"Invalid step_timeout_seconds ({self.step_timeout_seconds}). Must be between 0.1 and 300."
            )

        if self.max_plan_steps < 1 or self.max_plan_steps > 100:
            raise ConfigurationError(
                f"Invalid max_plan_steps ({self.max_plan_steps}). Must be between 1 and 100."
            )

        # Ensure workspace directory exists
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, workspace_override: Optional[Path] = None, db_override: Optional[Path] = None) -> "AgentConfig":
        """Create and validate configuration from environment variables."""
        ws_env = os.getenv("AUTOCRAFT_WORKSPACE_ROOT")
        db_env = os.getenv("AUTOCRAFT_DATABASE_PATH")

        workspace_root = workspace_override or (Path(ws_env) if ws_env else DEFAULT_WORKSPACE_DIR)
        database_path = db_override or (Path(db_env) if db_env else (Path(workspace_root) / "autocraft_state.db"))

        extra_roots_raw = os.getenv("AUTOCRAFT_ALLOWED_PATHS", "")
        extra_roots = [
            Path(p.strip()) for p in extra_roots_raw.split(os.pathsep) if p.strip()
        ]

        headless_raw = os.getenv("AUTOCRAFT_BROWSER_HEADLESS", "true").strip().lower()
        browser_headless = headless_raw in ("1", "true", "yes", "on")

        desktop_raw = os.getenv("AUTOCRAFT_ENABLE_DESKTOP")
        if desktop_raw is not None:
            enable_desktop = desktop_raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            enable_desktop = sys.platform == "win32"

        return cls(
            host=os.getenv("AUTOCRAFT_HOST", "127.0.0.1"),
            port=int(os.getenv("AUTOCRAFT_PORT", "8000")),
            workspace_root=workspace_root,
            database_path=database_path,
            allowed_extra_roots=extra_roots,
            browser_channel=os.getenv("AUTOCRAFT_BROWSER_CHANNEL", "chromium").strip().lower(),
            browser_headless=browser_headless,
            browser_timeout_ms=int(os.getenv("AUTOCRAFT_BROWSER_TIMEOUT_MS", "15000")),
            step_timeout_seconds=float(os.getenv("AUTOCRAFT_STEP_TIMEOUT_SECONDS", "30.0")),
            max_step_retries=int(os.getenv("AUTOCRAFT_MAX_STEP_RETRIES", "2")),
            max_plan_steps=int(os.getenv("AUTOCRAFT_MAX_PLAN_STEPS", "25")),
            enable_live_desktop_automation=enable_desktop,
            log_level=os.getenv("AUTOCRAFT_LOG_LEVEL", "INFO").upper(),
        )
