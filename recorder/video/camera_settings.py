import json
import logging
import re
import sys
from typing import Any, Dict

from ..utils.constants import _project_root
from ..utils.utils import (_extract_default, _extract_range, get_commit_hash,
                           run_capture_cmd)
from ..video.constants import (BRIGHTNESS_RANGE, CAMERA_CAPS_CACHE,
                               DEFAULT_CAMERA_FPS, DEVNODE_PATTERN,
                               FFMPEG_UNSUPPORTED_CONTROLS, HUE_RANGE,
                               IS_LINUX, IS_MAC, IS_WINDOWS, PIXEL_FORMAT_MAP,
                               SATURATION_RANGE, V4L2_AUTO_EXPOSURE_MODE,
                               V4L2_CONTROL_MAP, V4L2_MANUAL_EXPOSURE_MODE)
from ..video.dshow_capture import get_windows_camera_capabilities
from ..video.ffmpeg_utils import (_get_supported_modes,
                                  _probe_mac_supported_fps,
                                  _probe_mac_supported_ui_formats,
                                  probe_avfoundation_mode)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _capabilities_cache_key(os_name: str, git_hash: str, device_name: str) -> str:
    return f"{os_name}|{git_hash}|{device_name}"


def _load_cached_capabilities(cache_key: str) -> Dict[str, Any] | None:
    if not CAMERA_CAPS_CACHE.exists():
        return None
    try:
        data = json.loads(CAMERA_CAPS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(cache_key)
    if not isinstance(entry, dict):
        return None
    caps = entry.get("caps")
    if not isinstance(caps, dict):
        return None
    return caps


def _store_cached_capabilities(
    cache_key: str,
    caps: Dict[str, Any],
    os_name: str,
    device_name: str,
) -> None:
    try:
        CAMERA_CAPS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    data: Dict[str, Any] = {}
    if CAMERA_CAPS_CACHE.exists():
        try:
            data = json.loads(CAMERA_CAPS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    # Remove older entries for the same device on this OS (different commit hash).
    for key in list(data.keys()):
        if key == cache_key:
            continue
        parts = str(key).split("|", 2)
        if len(parts) == 3 and parts[0] == os_name and parts[2] == device_name:
            data.pop(key, None)
    data[cache_key] = {"caps": caps}
    try:
        CAMERA_CAPS_CACHE.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        return


def _empty_capabilities() -> Dict[str, Any]:
    return {
        "pixel_formats": [],
        "fps": [],
        "modes": [],
        "supports_auto_exposure": False,
        "supports_auto_focus": False,
        "brightness_range": None,
        "brightness_default": None,
        "hue_range": None,
        "hue_default": None,
        "saturation_range": None,
        "saturation_default": None,
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
    caps["brightness_default"] = _extract_default(ctrl_text, "brightness")
    caps["hue_range"] = _extract_range(ctrl_text, "hue")
    caps["hue_default"] = _extract_default(ctrl_text, "hue")
    caps["saturation_range"] = _extract_range(ctrl_text, "saturation")
    caps["saturation_default"] = _extract_default(ctrl_text, "saturation")
    exposure_menu = _parse_v4l2_menu(ctrl_text, "exposure_auto")
    if exposure_menu:
        caps["exposure_auto_menu"] = exposure_menu
        caps["exposure_auto_auto"] = _pick_v4l2_menu_value(exposure_menu, prefer_auto=True)
        caps["exposure_auto_manual"] = _pick_v4l2_menu_value(exposure_menu, prefer_auto=False)

    # Parse V4L2 size/fps mode associations from --list-formats-ext output.
    mode_fps: dict[tuple[int, int], set[int]] = {}
    mode_fps_by_format: dict[str, dict[tuple[int, int], set[int]]] = {}
    current_mode: tuple[int, int] | None = None
    current_format: str | None = None
    for line in fmt_text.splitlines():
        fmt_match = re.search(r"^\s*\[\d+\]:\s*'([A-Za-z0-9]{4})'", line)
        if fmt_match:
            current_format = fmt_match.group(1).upper()
            mode_fps_by_format.setdefault(current_format, {})
            current_mode = None
            continue
        size_match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size_match:
            current_mode = (int(size_match.group(1)), int(size_match.group(2)))
            mode_fps.setdefault(current_mode, set())
            if current_format:
                mode_fps_by_format[current_format].setdefault(current_mode, set())
            continue
        if current_mode is None:
            continue
        fps_match = re.search(r"\(([\d.]+)\s*fps\)", line)
        if fps_match:
            fps_value = int(round(float(fps_match.group(1))))
            if fps_value > 0:
                mode_fps[current_mode].add(fps_value)
                if current_format:
                    mode_fps_by_format[current_format][current_mode].add(fps_value)

    caps["modes"] = [
        {"width": w, "height": h, "fps": sorted(list(fps_values))}
        for (w, h), fps_values in sorted(mode_fps.items())
        if fps_values
    ]
    if mode_fps_by_format:
        caps["modes_by_format"] = {
            fmt: [
                {"width": w, "height": h, "fps": sorted(list(fps_values))}
                for (w, h), fps_values in sorted(modes.items())
                if fps_values
            ]
            for fmt, modes in mode_fps_by_format.items()
        }
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


def _mac_camera_capabilities(
    devnode: str,
    device_index: int | None,
    progress_cb=None,
) -> Dict[str, Any]:
    caps = _empty_capabilities()
    device = str(device_index) if device_index is not None else reformat_devnode_for_ffmpeg(devnode)
    if progress_cb:
        try:
            progress_cb(5, "Probing supported modes...")
        except Exception:
            pass
    modes = _get_supported_modes(device)
    if progress_cb:
        try:
            progress_cb(20, "Probing pixel formats...")
        except Exception:
            pass
    caps["modes"] = [
        {"width": w, "height": h, "fps": fps_values}
        for w, h, fps_values in modes
    ]
    def _format_progress(done: int, total: int, fmt: str) -> None:
        if not total:
            return
        pct = 20 + int(80 * (done / total))
        msg = f"Probing pixel formats ({done}/{total}): {fmt}"
        progress_cb(pct, msg)

    caps["pixel_formats"] = _probe_mac_supported_ui_formats(
        device,
        modes,
        progress_cb=_format_progress if progress_cb else None,
    )
    caps["fps"] = _probe_mac_supported_fps(modes)
    caps["brightness_range"] = BRIGHTNESS_RANGE
    caps["hue_range"] = HUE_RANGE
    caps["saturation_range"] = SATURATION_RANGE
    if progress_cb:
        try:
            progress_cb(100, "Finalizing...")
        except Exception:
            pass
    return caps


def _windows_camera_capabilities(
    devnode: str, device_index: int | None, progress_cb=None
) -> Dict[str, Any]:
    index = device_index if device_index is not None else int(devnode)
    return get_windows_camera_capabilities(index, progress_cb=progress_cb)


def get_camera_capabilities(
    devnode: str,
    device_index: int | None = None,
    *,
    device_name: str | None = None,
    progress_cb=None,
) -> Dict[str, Any]:
    os_name = sys.platform
    git_hash = get_commit_hash(_project_root())
    name_key = (device_name or devnode or "unknown").strip()
    cache_key = _capabilities_cache_key(os_name, git_hash, name_key)
    cached = _load_cached_capabilities(cache_key)
    if cached is not None:
        if progress_cb:
            try:
                progress_cb(100, "Loaded cached capabilities.")
            except Exception:
                pass
        cached = dict(cached)
        cached["_from_cache"] = True
        return cached

    if IS_LINUX:
        caps = _linux_camera_capabilities(devnode)
    elif IS_MAC:
        caps = _mac_camera_capabilities(devnode, device_index, progress_cb=progress_cb)
    elif IS_WINDOWS:
        caps = _windows_camera_capabilities(devnode, device_index, progress_cb=progress_cb)
    else:
        raise OSError(f"Unsupported OS: {sys.platform}")
    if progress_cb:
        try:
            progress_cb(100, "Finalizing...")
        except Exception:
            pass
    _store_cached_capabilities(cache_key, caps, os_name, name_key)
    return caps


def reformat_devnode_for_ffmpeg(devnode: str) -> str:
    """Convert devnode to string format (raw digit index of node) expected by ffmpeg."""
    # If already all digits, return this
    if re.match(r"\d+$", devnode):
        return devnode
    
    # Ensure the input string matches the expected pattern before modifying
    if not DEVNODE_PATTERN.match(devnode):
        raise ValueError(f"Unrecognized devnode: {devnode}")

    return DEVNODE_PATTERN.sub(r"\1", devnode)


def probe_mac_mode_support(
    devnode: str,
    device_index: int | None,
    width: int | None,
    height: int | None,
    fps: int,
    pixel_format: str | None = None,
) -> bool:
    if not IS_MAC:
        return True
    device = str(device_index) if device_index is not None else reformat_devnode_for_ffmpeg(devnode)
    ff_pf = None
    if pixel_format:
        ff_pf = PIXEL_FORMAT_MAP.get(str(pixel_format).upper(), str(pixel_format).lower())
    return probe_avfoundation_mode(device, width, height, fps, ff_pf)


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

    elif IS_WINDOWS:
        # No DirectShow pre-flight application/validation is implemented yet.
        # FPS is applied directly by OpenCV (CAP_DSHOW) when the capture opens for recording.
        return True

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

        # Color controls are handled in software for macOS capture
        color_controls = {}
        for key in ("brightness", "hue", "saturation"):
            if key in settings_to_apply:
                color_controls[key] = settings_to_apply.pop(key)
        if not settings_to_apply and color_controls:
            successful_settings.update(color_controls)
            return successful_settings

        mac_pixel_format = None
        if "pixel_format" in settings_to_apply:
            mac_pixel_format = PIXEL_FORMAT_MAP.get(
                str(settings_to_apply["pixel_format"]).upper(),
                str(settings_to_apply["pixel_format"]).lower(),
            )

        # Build ffmpeg filter chain for remaining non-color controls.
        vf_filters = []

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
        if color_controls:
            successful_settings.update(color_controls)

    elif IS_WINDOWS:
        # No DirectShow pre-flight application/validation is implemented yet.
        # Width/height/pixel_format are applied directly by OpenCV (CAP_DSHOW) when the
        # capture opens for recording; fine-grained controls (brightness/hue/saturation,
        # auto-exposure/auto-focus) are not yet supported at all on Windows. 
        # Accept everything here rather than blocking Start, since the GUI already disables
        # controls that `get_camera_capabilities` reports as unsupported.
        successful_settings.update(settings_to_apply)

    else:
        raise ValueError(f"Unsupported OS: {sys.platform}")

    return successful_settings


def apply_camera_controls(devnode: str, controls: Dict[str, Any]) -> Dict[str, Any]:
    applied, failed = {}, {}

    # Handle camera recording settings (format/size first)
    fps = controls.pop('fps', None)
    if controls:
        settings_results = set_camera_controls(devnode, controls)
        for k, v in controls.items():
            (applied if k in settings_results else failed)[k] = v

    # Handle frame rate after format/size to respect driver constraints
    if fps:
        success = set_frame_rate(devnode, fps)
        (applied if success else failed)['fps'] = fps
    
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
