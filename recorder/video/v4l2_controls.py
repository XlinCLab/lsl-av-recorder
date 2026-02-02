from __future__ import annotations
import subprocess
from typing import Any, Dict

def set_control(devnode: str, control: str, value: Any) -> bool:
    cmd = ["v4l2-ctl", "-d", devnode, "-c", f"{control}={value}"]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def apply_controls(devnode: str, controls: Dict[str, Any]) -> Dict[str, Any]:
    applied, failed = {}, {}
    for k, v in controls.items():
        ok = set_control(devnode, k, v)
        (applied if ok else failed)[k] = v
    return {"devnode": devnode, "applied": applied, "failed": failed}
