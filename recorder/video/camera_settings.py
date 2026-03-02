import logging
import re
import subprocess
import sys
from typing import Any, Dict

# Detect operating system
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Regular expression pattern for video devnodes
DEVNODE_PATTERN = re.compile(r"/dev/video(\d+)$")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def capture_cmd_output(cmd: str) -> tuple[str, str]:
    """Run a command via subprocess and capture output result and/or error."""
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.debug(f"STDERR:\n{result.stderr}")
        return result.stderr, None
    except subprocess.CalledProcessError as e:
        logger.debug(f"Return code: {e.returncode}")
        logger.debug(f"STDERR:\n{e.stderr}")
        return None, e


def reformat_devnode_for_ffmpeg(devnode: str) -> str:
    """Convert devnode to string format (raw digit index of node) expected by ffmpeg."""
    # If already all digits, return this
    if re.match(r"\d+$", devnode):
        return devnode
    
    # Ensure the input string matches the expected pattern before modifying
    if not DEVNODE_PATTERN.match(devnode):
        raise ValueError(f"Unrecognized devnode: {devnode}")

    return DEVNODE_PATTERN.sub(r"\1", devnode)


def get_control_settings_string(controls: dict) -> str:
    return ','.join([f'{k}={v}' for k, v in controls.items()])


def set_frame_rate(devnode: str, fps: float) -> bool:
    if IS_LINUX:
        # Linux V4L2 method
        cmd = ["v4l2-ctl", "-d", devnode, f"--set-parm={fps}"]

    elif IS_MAC:
        # On macOS, FPS is chosen when opening device via AVFoundation.
        # We test whether the requested FPS is supported.
        # ffmpeg expects devnode as raw digit index of node
        devnode = reformat_devnode_for_ffmpeg(devnode)
        cmd = [
            "ffmpeg",
            "-f", "avfoundation",
            "-framerate", str(fps),
            "-video_device_index", str(devnode),
            "-i", f"{devnode}:none",
            "-t", "0.1",
            "-f", "null",
            "-"
        ]
    
    result, _ = capture_cmd_output(cmd)
    return result is not None


def set_control(devnode: str, control_settings: str) -> bool:
    if IS_LINUX:
        # Linux V4L2 method
        cmd = ["v4l2-ctl", "-d", devnode, "-c", control_settings]

    elif IS_MAC:
        cmd = [
            "ffmpeg",
            "-f", "avfoundation",
            "-framerate", "30",
            "-video_device_index", str(devnode),
            "-i", "none",
            "-vf", f"eq={control_settings}",
            "-t", "0.1",  # tiny test duration
            "-f", "null", "-"
        ]
    else:
        raise ValueError(f"Unsupported OS: {sys.platform}")
    
    result, _ = capture_cmd_output(cmd)
    return result is not None


def apply_controls(devnode: str, controls: Dict[str, Any]) -> Dict[str, Any]:
    applied, failed = {}, {}

    # Handle frame rate separately
    fps = controls.pop('fps', None)
    if fps:
        success = set_frame_rate(devnode, fps)
        (applied if success else failed)['fps'] = fps

    # Handle other camera recording settings
    if controls:
        control_settings = get_control_settings_string(controls)
        control_set_result = set_control(devnode, control_settings)
        for k, v in controls.items():
            (applied if control_set_result else failed)[k] = v
    
    # Return successfully applied and failed settings
    return {"devnode": devnode, "applied": applied, "failed": failed}
