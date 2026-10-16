import os
import asyncio
import pytest
from src.config import settings
from src.db import init_db


@pytest.fixture(autouse=True)
def isolate_environment_and_settings(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting the developer's real .env file,
    and isolates in-memory settings and SQLite database per test.
    """
    test_env = tmp_path / ".test.env"
    monkeypatch.setenv("ENV_FILE_PATH", str(test_env))
    monkeypatch.setenv("TESTING", "true")
    test_db = tmp_path / "test_tweets.sqlite"
    monkeypatch.setattr(settings, "db_path", str(test_db))

    asyncio.run(init_db())

    # Snapshot current settings
    orig_dict = settings.model_dump()

    yield

    # Restore settings after test
    for k, v in orig_dict.items():
        try:
            setattr(settings, k, v)
        except Exception:
            pass
