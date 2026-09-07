import logging
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def run_capture_cmd(
    cmd: list[str],
    check: bool = True,
) -> tuple[str, subprocess.CalledProcessError | None]:
    """Run a command, capturing stdout+stderr and optionally enforcing a zero exit status."""
    try:
        logger.debug(f"Running command:\n\t{cmd}")
        result = subprocess.run(
            cmd,
            check=check,
            capture_output=True,
            text=True,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return out, None
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "") + (e.stderr or "")
        logger.debug(f"Command failed: {e.returncode}")
        return out, e


def _extract_range(text: str, name: str):
    m = re.search(
        rf"^\s*{name}\b.*\(\w+\)\s*:\s*min=(-?\d+)\s+max=(-?\d+)",
        text,
        re.MULTILINE,
    )
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _extract_default(text: str, name: str):
    m = re.search(
        rf"^\s*{name}\b.*\bdefault=(-?\d+)\b",
        text,
        re.MULTILINE,
    )
    if not m:
        return None
    return int(m.group(1))


def get_environment_info(root: Path) -> Dict[str, str]:
    """Identify the exact code and machine a session ran on:
    git commit, OS/platform description, hostname, and Python version."""
    return {
        "commit": get_commit_hash(root),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
    }


def get_commit_hash(root: Path) -> str:
    """Retrieve the git commit hash for the current project, with the current date as fallback."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        sha = (result.stdout or "").strip()
        if re.match(r"[0-9a-fA-F]{7,40}$", sha):
            return sha[:12]
        return "unknown"
    except Exception:
        logger.warning("Could not retrive git commit hash, using current date instead.")
        return datetime.now(timezone.utc).strftime("%Y%m%d")
