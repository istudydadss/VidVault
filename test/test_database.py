import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.database import SQLITE_BUSY_TIMEOUT_MS, create_database_engine


class DatabaseConfigurationTest(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_uses_wal_and_waits_for_short_write_locks(self):
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "configured.db"
            engine = create_database_engine(
                f"sqlite+aiosqlite:///{database_path}",
            )

            async with engine.connect() as connection:
                journal_mode = (
                    await connection.exec_driver_sql("PRAGMA journal_mode")
                ).scalar_one()
                busy_timeout = (
                    await connection.exec_driver_sql("PRAGMA busy_timeout")
                ).scalar_one()

            await engine.dispose()

        self.assertEqual(journal_mode, "wal")
        self.assertEqual(busy_timeout, SQLITE_BUSY_TIMEOUT_MS)


if __name__ == "__main__":
    unittest.main()
