#!/usr/bin/env python3
"""
Unified Test Suite Runner for AUTOCRAFT AI.
Executes:
1. Backend Unit Tests (tests/unit)
2. Backend & Real Local Browser Integration Tests (tests/integration)
3. Windows Live & External Browser Tests (tests/windows_live) — reporting exact pass/skip/fail counts
4. Frontend TypeScript + React Build & Component Tests (scripts/test_frontend.cjs)
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    print("=" * 76)
    print("AUTOCRAFT AI — AUTOMATED VERIFICATION & TEST SUITE")
    print(f"Host Platform: {sys.platform} | Python: {sys.version.split()[0]}")
    print("=" * 76)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(PROJECT_ROOT / "tests"), pattern="test_*.py")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "-" * 76)
    print("RUNNING FRONTEND TYPESCRIPT + REACT COMPONENT TESTS")
    print("-" * 76)
    fe_proc = subprocess.run(
        ["node", str(PROJECT_ROOT / "scripts" / "test_frontend.cjs")],
        cwd=str(PROJECT_ROOT / "frontend"),
        capture_output=True,
        text=True,
    )
    print(fe_proc.stdout)
    if fe_proc.stderr:
        print(fe_proc.stderr, file=sys.stderr)

    total_py = result.testsRun
    skipped_py = len(result.skipped)
    failed_py = len(result.failures) + len(result.errors)
    passed_py = total_py - skipped_py - failed_py

    print("=" * 76)
    print("FINAL TEST SUMMARY")
    print(f"Python Tests Total   : {total_py}")
    print(f"  Passed (Executed)  : {passed_py}")
    print(f"  Skipped (Live Env) : {skipped_py}")
    print(f"  Failed / Errors    : {failed_py}")
    print(f"Frontend Tests Status: {'PASSED' if fe_proc.returncode == 0 else 'FAILED'}")
    if result.skipped:
        print("\nSkipped Live Tests (Environment-Specific):")
        for test_case, reason in result.skipped:
            print(f"  - [SKIPPED] {test_case}: {reason}")
    print("=" * 76)

    if failed_py > 0 or fe_proc.returncode != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
