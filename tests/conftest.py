import os
import pytest
from src.config import settings


@pytest.fixture(autouse=True)
def isolate_environment_and_settings(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting the developer's real .env file,
    and isolates in-memory settings per test.
    """
    test_env = tmp_path / ".test.env"
    monkeypatch.setenv("ENV_FILE_PATH", str(test_env))
    monkeypatch.setenv("TESTING", "true")

    # Snapshot current settings
    orig_dict = settings.model_dump()

    yield

    # Restore settings after test
    for k, v in orig_dict.items():
        try:
            setattr(settings, k, v)
        except Exception:
            pass
