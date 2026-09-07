from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cache_path() -> Path:
    root = _project_root()
    return root / ".cache"


def _logs_path() -> Path:
    """Directory for persistent, app-level session logs."""
    root = _project_root()
    return root / "logs"
