from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cache_path() -> Path:
    root = _project_root()
    return root / ".cache"
