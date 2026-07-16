import json
import logging
import re
import sys
from typing import Any, Dict

from ..utils.constants import _project_root
from ..utils.utils import (_extract_default, _extract_range, get_commit_hash,
                           run_capture_cmd)
from ..video.constants import (AUTO_VALUE_BY_CONTROL, BRIGHTNESS_RANGE,
                               CAMERA_CAPS_CACHE, DEFAULT_CAMERA_FPS,
                               DEVNODE_PATTERN, FFMPEG_UNSUPPORTED_CONTROLS,
                               HUE_RANGE, IS_LINUX, IS_MAC, IS_WINDOWS,
                               PIXEL_FORMAT_MAP, SATURATION_RANGE,
                               V4L2_AUTO_EXPOSURE_MODE, V4L2_AUTO_FOCUS_MODE,
                               V4L2_CONTROL_MAP, V4L2_MANUAL_EXPOSURE_MODE)
from ..video.dshow_capture import (get_windows_camera_capabilities,
                                   set_windows_camera_controls)
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


def _windows_capabilities_for_device(devnode: str, device_name: str | None) -> Dict[str, Any] | None:
    """Look up previously-probed Windows capabilities for this device from the
    on-disk cache, using the same key `get_camera_capabilities` stores under.
    Returns None if nothing has been cached yet (e.g. capabilities were never
    probed this session/commit), in which case callers have no basis to judge
    whether a setting is actually supported."""
    os_name = sys.platform
    git_hash = get_commit_hash(_project_root())
    name_key = (device_name or devnode or "unknown").strip()
    cache_key = _capabilities_cache_key(os_name, git_hash, name_key)
    return _load_cached_capabilities(cache_key)


def _validate_against_windows_capabilities(
    settings: Dict[str, Any], caps: Dict[str, Any]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Check width/height/pixel_format/fps (whichever are present in `settings`)
    against a previously-probed Windows capabilities dict.
    Returns (valid, invalid) partitions of `settings`; 
    a key is left out of both if `caps` doesn't contain enough information to judge it."""
    valid: Dict[str, Any] = {}
    invalid: Dict[str, Any] = {}

    pixel_format = settings.get("pixel_format")
    supported_formats = {str(f).upper() for f in caps.get("pixel_formats") or []}
    if pixel_format is not None:
        if supported_formats and str(pixel_format).upper() not in supported_formats:
            invalid["pixel_format"] = pixel_format
        else:
            valid["pixel_format"] = pixel_format

    modes_by_format = caps.get("modes_by_format") or {}
    fmt_key = str(pixel_format).upper() if pixel_format is not None else None
    modes = modes_by_format.get(fmt_key, []) if fmt_key and fmt_key in modes_by_format else (caps.get("modes") or [])

    width, height = settings.get("width"), settings.get("height")
    matched_mode = None
    if width and height:
        matched_mode = next(
            (m for m in modes
             if int(m.get("width", -1)) == int(width) and int(m.get("height", -1)) == int(height)),
            None,
        )
        if matched_mode is None:
            invalid["width"] = width
            invalid["height"] = height
        else:
            valid["width"] = width
            valid["height"] = height

    fps = settings.get("fps")
    if fps is not None:
        fps_pool = matched_mode.get("fps") if matched_mode else caps.get("fps")
        if fps_pool:
            if int(round(float(fps))) in {int(f) for f in fps_pool}:
                valid["fps"] = fps
            else:
                invalid["fps"] = fps

    return valid, invalid


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


def set_frame_rate(
    devnode: str,
    fps: float,
    *,
    width: int | None = None,
    height: int | None = None,
    pixel_format: str | None = None,
    device_name: str | None = None,
) -> tuple[bool, bool]:
    """Returns (success, verified).
    `verified` is False only for the Windows case where no capability probe 
    has been cached yet for this device, so there is no basis to confirm or reject the requested fps.
    Rather, it is simply queued to be applied when the DirectShow graph opens."""
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
        # No DirectShow pre-flight application is implemented. 
        # Frame rate (fps) is applied directly when the capture opens for recording
        # but it can still be checked against a previously-probed capabilities cache, if one exists.
        caps = _windows_capabilities_for_device(devnode, device_name)
        if caps is None:
            return True, False
        _, invalid = _validate_against_windows_capabilities(
            {
                "fps": fps,
                "width": width,
                "height": height,
                "pixel_format": pixel_format
            },
            caps
        )
        return "fps" not in invalid, True

    # Linux / MacOS
    _, error = run_capture_cmd(cmd)
    return error is None, True


def set_camera_controls(devnode: str,
                        control_settings: dict,
                        device_name: str | None = None,
                        ) -> tuple[dict, set]:
    """Returns (successful_settings, unverified_keys), where unverified_keys is
    a subset of successful_settings' keys that were accepted without being
    checked against actual device capabilities (Windows-only, when no
    capability probe has been cached yet for this device)."""
    settings_to_apply = {k: v for k, v in control_settings.items() if v is not None}
    successful_settings = {}
    unverified_keys: set = set()

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
            return successful_settings, unverified_keys

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
        # Width/height/pixel_format are applied by WindowsDShowVideoCapture itself
        # when the capture opens for recording
        device_index = int(devnode)
        color_controls = {}
        for key in ("brightness", "hue", "saturation"):
            if key in settings_to_apply:
                color_controls[key] = settings_to_apply.pop(key)

        auto_exposure_val = settings_to_apply.pop("auto_exposure", None)
        auto_focus_val = settings_to_apply.pop("auto_focus", None)
        auto_exposure = None if auto_exposure_val is None else auto_exposure_val == V4L2_AUTO_EXPOSURE_MODE
        auto_focus = None if auto_focus_val is None else auto_focus_val == V4L2_AUTO_FOCUS_MODE

        if color_controls or auto_exposure is not None or auto_focus is not None:
            applied = set_windows_camera_controls(
                device_index,
                brightness=color_controls.get("brightness"),
                hue=color_controls.get("hue"),
                saturation=color_controls.get("saturation"),
                auto_exposure=auto_exposure,
                auto_focus=auto_focus,
            )
            if "brightness" in applied:
                successful_settings["brightness"] = color_controls["brightness"]
            if "hue" in applied:
                successful_settings["hue"] = color_controls["hue"]
            if "saturation" in applied:
                successful_settings["saturation"] = color_controls["saturation"]
            if "auto_exposure" in applied:
                successful_settings["auto_exposure"] = auto_exposure_val
            if "auto_focus" in applied:
                successful_settings["auto_focus"] = auto_focus_val

        # Remaining keys at this point are width/height/pixel_format, 
        # which are not applied by any pre-flight call on Windows.
        # These are only applied when WindowsDShowVideoCapture opens the graph.
        # Check them against a previously-probed capabilities cache, if available.
        if settings_to_apply:
            caps = _windows_capabilities_for_device(devnode, device_name)
            if caps is not None:
                valid, _invalid = _validate_against_windows_capabilities(settings_to_apply, caps)
                successful_settings.update(valid)
            else:
                successful_settings.update(settings_to_apply)
                unverified_keys.update(settings_to_apply.keys())

    else:
        raise ValueError(f"Unsupported OS: {sys.platform}")

    return successful_settings, unverified_keys


def apply_camera_controls(devnode: str,
                          controls: Dict[str, Any],
                          device_name: str | None = None,
                          ) -> Dict[str, Any]:
    applied, unverified, failed = {}, {}, {}

    # Handle camera recording settings (format/size first)
    fps = controls.pop('fps', None)
    width = controls.get('width')
    height = controls.get('height')
    pixel_format = controls.get('pixel_format')
    if controls:
        settings_results, unverified_keys = set_camera_controls(
            devnode=devnode, control_settings=controls,
            device_name=device_name
        )
        for k, v in controls.items():
            if k not in settings_results:
                failed[k] = v
            elif k in unverified_keys:
                unverified[k] = v
            else:
                applied[k] = v

    # Handle frame rate after format/size to respect driver constraints
    if fps:
        success, verified = set_frame_rate(
            devnode=devnode,
            fps=fps,
            width=width,
            height=height,
            pixel_format=pixel_format,
            device_name=device_name,
        )
        if not success:
            failed['fps'] = fps
        elif verified:
            applied['fps'] = fps
        else:
            unverified['fps'] = fps

    # Return settings: 
    # - successfully applied
    # - unverified (accepted but not checked againstactual device capabilities)
    # - failed
    return {"devnode": devnode, "applied": applied, "unverified": unverified, "failed": failed}


def format_control_value(key: str, value) -> str:
    """Format auto-exposure and auto-focus settings as 'ON' vs. 'OFF' rather
    than 0 vs. 1, which have opposite meanings for auto-focus and auto-exposure."""
    if key in AUTO_VALUE_BY_CONTROL:
        return "ON" if value == AUTO_VALUE_BY_CONTROL[key] else "OFF"
    return str(value)


def summarize_control_application(devnode: str,
                                  applied: dict,
                                  failed: dict,
                                  unverified: dict | None = None,
                                  ) -> str:
    """Generate a summary string of camera setting application results."""
    summary = [f"devnode: {devnode}"]
    for k, v in applied.items():
        summary.append(f"INFO: Successfully set {k}={format_control_value(k, v)}")
    for k, v in (unverified or {}).items():
        summary.append(
            f"INFO: Queued (not yet verified against known camera capabilities; "
            f"will be applied when capture starts) {k}={format_control_value(k, v)}"
        )
    for k, v in failed.items():
        summary.append(f"ERROR: Failed to set {k}={format_control_value(k, v)}")
    return '\n'.join(summary)
