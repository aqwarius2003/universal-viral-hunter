"""
Single source of truth for Viral Hunter version and git commit metadata.
"""
from pathlib import Path
import subprocess
from typing import Dict, Any, Optional

_CACHED_VERSION: Optional[str] = None
_CACHED_COMMIT: Optional[str] = None


def get_version() -> str:
    """Returns the semantic version string, e.g. '2.1.0'."""
    global _CACHED_VERSION
    if _CACHED_VERSION is not None:
        return _CACHED_VERSION

    # Try reading from root VERSION file
    candidates = [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path("VERSION"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                ver = p.read_text(encoding="utf-8").strip()
                if ver:
                    _CACHED_VERSION = ver
                    return ver
            except Exception:
                pass

    _CACHED_VERSION = "2.2.7"
    return _CACHED_VERSION


def get_git_commit() -> str:
    """Returns the short git commit hash, or 'unknown'."""
    global _CACHED_COMMIT
    if _CACHED_COMMIT is not None:
        return _CACHED_COMMIT

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        if commit:
            _CACHED_COMMIT = commit
            return commit
    except Exception:
        pass

    _CACHED_COMMIT = "release"
    return _CACHED_COMMIT


def get_version_info() -> Dict[str, Any]:
    """Returns structured version information."""
    version = get_version()
    commit = get_git_commit()
    return {
        "version": f"v{version}",
        "raw_version": version,
        "commit": commit,
        "git_commit": commit,
        "full_string": f"v{version} ({commit})",
    }
