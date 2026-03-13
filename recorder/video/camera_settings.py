import logging
import re
import sys
from typing import Any, Dict

from ..utils.utils import _extract_range, run_capture_cmd
from ..video.constants import (DEFAULT_CAMERA_FPS, DEVNODE_PATTERN,
                               FFMPEG_UNSUPPORTED_CONTROLS, IS_LINUX, IS_MAC,
                               MAC_PIXEL_FORMAT_MAP, V4L2_AUTO_EXPOSURE_MODE,
                               V4L2_CONTROL_MAP, V4L2_MANUAL_EXPOSURE_MODE)
from ..video.ffmpeg_utils import (_get_supported_modes,
                                  _probe_mac_supported_fps,
                                  _probe_mac_supported_ui_formats)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _empty_capabilities() -> Dict[str, Any]:
    return {
        "pixel_formats": [],
        "fps": [],
        "modes": [],
        "supports_auto_exposure": False,
        "supports_auto_focus": False,
        "brightness_range": None,
        "hue_range": None,
        "saturation_range": None,
    }


def _linux_camera_capabilities(devnode: str) -> Dict[str, Any]:
    caps = _empty_capabilities()
    fmt_text, _ = run_capture_cmd(["v4l2-ctl", "-d", devnode, "--list-formats-ext"], check=False)
    ctrl_text, _ = run_capture_cmd(["v4l2-ctl", "-d", devnode, "-L"], check=False)

    caps["pixel_formats"] = sorted(set(re.findall(r"\[\d+\]:\s*'([A-Za-z0-9]{4})'", fmt_text)))
    caps["fps"] = sorted(
        {
            int(round(float(x)))
            for x in re.findall(r"\(([\d.]+)\s*fps\)", fmt_text)
            if float(x) > 0
        }
    )
    caps["supports_auto_exposure"] = bool(re.search(r"^\s*exposure_auto\b", ctrl_text, re.MULTILINE))
    caps["supports_auto_focus"] = bool(re.search(r"^\s*focus_auto\b", ctrl_text, re.MULTILINE))
    caps["brightness_range"] = _extract_range(ctrl_text, "brightness")
    caps["hue_range"] = _extract_range(ctrl_text, "hue")
    caps["saturation_range"] = _extract_range(ctrl_text, "saturation")
    exposure_menu = _parse_v4l2_menu(ctrl_text, "exposure_auto")
    if exposure_menu:
        caps["exposure_auto_menu"] = exposure_menu
        caps["exposure_auto_auto"] = _pick_v4l2_menu_value(exposure_menu, prefer_auto=True)
        caps["exposure_auto_manual"] = _pick_v4l2_menu_value(exposure_menu, prefer_auto=False)

    # Parse V4L2 size/fps mode associations from --list-formats-ext output.
    mode_fps: dict[tuple[int, int], set[int]] = {}
    current_mode: tuple[int, int] | None = None
    for line in fmt_text.splitlines():
        size_match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size_match:
            current_mode = (int(size_match.group(1)), int(size_match.group(2)))
            mode_fps.setdefault(current_mode, set())
            continue
        if current_mode is None:
            continue
        fps_match = re.search(r"\(([\d.]+)\s*fps\)", line)
        if fps_match:
            fps_value = int(round(float(fps_match.group(1))))
            if fps_value > 0:
                mode_fps[current_mode].add(fps_value)

    caps["modes"] = [
        {"width": w, "height": h, "fps": sorted(list(fps_values))}
        for (w, h), fps_values in sorted(mode_fps.items())
        if fps_values
    ]
    return caps


def _parse_v4l2_menu(text: str, control_name: str) -> dict[str, int]:
    menu: dict[str, int] = {}
    in_menu = False
    header_re = re.compile(rf"^\s*{re.escape(control_name)}\b.*\(\s*menu\s*\)", re.IGNORECASE)
    item_re = re.compile(r"^\s+(\d+)\s*:\s*(.+)$")
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not in_menu:
            if header_re.search(line):
                in_menu = True
            continue
        if line.strip() == "":
            break
        if line and not line.startswith((" ", "\t")):
            break
        m = item_re.match(line)
        if m:
            value = int(m.group(1))
            label = m.group(2).strip()
            menu[label] = value
    return menu


def _pick_v4l2_menu_value(menu: dict[str, int], prefer_auto: bool) -> int | None:
    if not menu:
        return None
    if prefer_auto:
        for label, value in menu.items():
            lname = label.lower()
            if "manual" in lname:
                continue
            if "auto" in lname or "priority" in lname or "aperture" in lname:
                return value
    else:
        for label, value in menu.items():
            if "manual" in label.lower():
                return value
    fallback = V4L2_AUTO_EXPOSURE_MODE if prefer_auto else V4L2_MANUAL_EXPOSURE_MODE
    if fallback in menu.values():
        return fallback
    return next(iter(menu.values()))


def _mac_camera_capabilities(devnode: str, device_index: int | None) -> Dict[str, Any]:
    caps = _empty_capabilities()
    device = str(device_index) if device_index is not None else reformat_devnode_for_ffmpeg(devnode)
    modes = _get_supported_modes(device)
    caps["modes"] = [
        {"width": w, "height": h, "fps": fps_values}
        for w, h, fps_values in modes
    ]
    caps["pixel_formats"] = _probe_mac_supported_ui_formats(device, modes)
    caps["fps"] = _probe_mac_supported_fps(modes)
    return caps


def get_camera_capabilities(devnode: str,
                            device_index: int | None = None
                            ) -> Dict[str, Any]:
    if IS_LINUX:
        return _linux_camera_capabilities(devnode)
    if IS_MAC:
        return _mac_camera_capabilities(devnode, device_index)
    raise OSError(f"Unsupported OS: {sys.platform}")


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
    
    _, error = run_capture_cmd(cmd)
    return error is None


def set_camera_controls(devnode: str, control_settings: dict) -> dict:
    settings_to_apply = {k: v for k, v in control_settings.items() if v is not None}
    successful_settings = {}

    # Build video size argument
    width = settings_to_apply.get("width")
    height = settings_to_apply.get("height")
    pixel_format = settings_to_apply.get("pixel_format")
    video_size = None
    if width and height:
        video_size = f"{width}x{height}"
    elif (width and not height) or (height and not width):
        raise ValueError("Both height and width dimensions are required")

    if IS_LINUX: # Linux V4L2 method
        # Handle width, height, and pixel format via V4L2 format API
        fmt_parts = []
        if width and height:
            settings_to_apply.pop("width")
            settings_to_apply.pop("height")
            fmt_parts.extend([f"width={width}", f"height={height}"])
        if pixel_format:
            settings_to_apply.pop("pixel_format")
            fmt_parts.append(f"pixelformat={pixel_format}")

        if fmt_parts:
            fmt_cmd = [
                "v4l2-ctl",
                "-d", devnode,
                "--set-fmt-video=" + ",".join(fmt_parts),
            ]
            _, error = run_capture_cmd(fmt_cmd)
            if error is None:
                if width and height:
                    successful_settings["width"] = width
                    successful_settings["height"] = height
                if pixel_format:
                    successful_settings["pixel_format"] = pixel_format
        
        # Handle other settings
        if settings_to_apply:
            v4l2_settings = {
                V4L2_CONTROL_MAP.get(k, k): v for k, v in settings_to_apply.items()
            }
            setting_str = get_control_settings_string(v4l2_settings)
            cmd = ["v4l2-ctl", "-d", devnode, "-c", setting_str]
            _, error = run_capture_cmd(cmd)
            if error is None:
                successful_settings.update(settings_to_apply)

    elif IS_MAC:  # FFMPEG for MaCOS
        devnode = reformat_devnode_for_ffmpeg(devnode)

        # Reject unsupported controls explicitly
        for parameter in control_settings:
            if parameter in FFMPEG_UNSUPPORTED_CONTROLS:
                logger.warning(f"{parameter} not supported on macOS via ffmpeg; skipping.")
                settings_to_apply.pop(parameter)

        mac_pixel_format = None
        if "pixel_format" in settings_to_apply:
            mac_pixel_format = MAC_PIXEL_FORMAT_MAP.get(
                str(settings_to_apply["pixel_format"]).upper(),
                str(settings_to_apply["pixel_format"]).lower(),
            )

        # Build ffmpeg filter chain. `eq` supports brightness/saturation;
        # hue is a dedicated filter.
        vf_filters = []
        eq_parts = []
        if "brightness" in settings_to_apply:
            brightness = float(settings_to_apply["brightness"]) / 100.0
            eq_parts.append(f"brightness={brightness:.3f}")
        if "saturation" in settings_to_apply:
            saturation = float(settings_to_apply["saturation"]) / 100.0
            eq_parts.append(f"saturation={saturation:.3f}")
        if eq_parts:
            vf_filters.append("eq=" + ":".join(eq_parts))
        if "hue" in settings_to_apply:
            vf_filters.append(f"hue=h={settings_to_apply['hue']}")

        cmd = [
            "ffmpeg",
            "-f", "avfoundation",
        ]

        if video_size:
            cmd += ["-video_size", video_size]
        if mac_pixel_format:
            cmd += ["-pixel_format", mac_pixel_format]

        cmd += [
            "-framerate", str(DEFAULT_CAMERA_FPS),
            "-i", f"{devnode}:none",
        ]

        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]

        cmd += [
            "-t", "0.1",  # tiny test duration
            "-f", "null",
            "-"
        ]
        _, error = run_capture_cmd(cmd)
        if error is None:
            successful_settings.update(settings_to_apply)

    else:
        raise ValueError(f"Unsupported OS: {sys.platform}")

    return successful_settings


def apply_camera_controls(devnode: str, controls: Dict[str, Any]) -> Dict[str, Any]:
    applied, failed = {}, {}

    # Handle frame rate separately
    fps = controls.pop('fps', None)
    if fps:
        success = set_frame_rate(devnode, fps)
        (applied if success else failed)['fps'] = fps

    # Handle other camera recording settings
    if controls:
        settings_results = set_camera_controls(devnode, controls)
        for k, v in controls.items():
            (applied if k in settings_results else failed)[k] = v
    
    # Return successfully applied and failed settings
    return {"devnode": devnode, "applied": applied, "failed": failed}


def summarize_control_application(devnode: str,
                                  applied: dict,
                                  failed: dict
                                  ) -> str:
    """Generate a summary string of camera setting application results."""
    summary = [f"devnode: {devnode}"]
    for k, v in applied.items():
        summary.append(f"INFO: Successfully set {k}={v}")
    for k, v in failed.items():
        summary.append(f"ERROR: Failed to set {k}={v}")
    return '\n'.join(summary)
