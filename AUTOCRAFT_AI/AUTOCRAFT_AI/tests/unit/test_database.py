"""SQLite initialization, error reporting, and persistence tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.database import SQLiteStore


class TestSQLiteStore(unittest.TestCase):
    def test_missing_database_is_initialized_and_data_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state" / "autocraft.db"

            store = SQLiteStore(db_path)
            self.assertTrue(db_path.is_file())
            self.assertTrue(store.check_health()["healthy"])

            task = store.create_task("persist this task")
            store.add_log(task["task_id"], phase="TEST", message="persisted")

            reopened_store = SQLiteStore(db_path)
            persisted_task = reopened_store.get_task(task["task_id"])

            self.assertIsNotNone(persisted_task)
            self.assertEqual(persisted_task["goal"], "persist this task")
            self.assertEqual(persisted_task["logs"][0]["message"], "persisted")
            connection = sqlite3.connect(str(db_path))
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0],
                    "delete",
                )
            finally:
                connection.close()

    def test_invalid_database_path_reports_the_sqlite_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path_is_directory = Path(directory)

            store = SQLiteStore(db_path_is_directory)
            health = store.check_health()

            self.assertFalse(health["healthy"])
            self.assertIn("unable to open database file", health["error"])
            self.assertEqual(health["db_path"], str(db_path_is_directory.resolve()))
            self.assertTrue(db_path_is_directory.is_dir())


if __name__ == "__main__":
    unittest.main()
