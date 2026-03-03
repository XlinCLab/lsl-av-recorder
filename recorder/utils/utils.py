import logging
import re
import subprocess

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
