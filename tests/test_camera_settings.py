"""Tests for recorder.video.camera_settings.

This module is mostly platform-specific device wrangling, but underneath sits a
lot of pure logic worth testing: devnode normalization, v4l2 menu parsing,
Windows capability validator, control-summary formatting, on-disk capabilities
cache, and the verified/unverified semantics of set_frame_rate.
Subprocess and platform seams are faked so everything runs on any host.
"""
from __future__ import annotations

import json
import re

import pytest

from recorder.video import camera_settings as cs
from recorder.video.camera_settings import (
    _capabilities_cache_key, _empty_capabilities, _linux_camera_capabilities,
    _load_cached_capabilities, _mac_camera_capabilities, _parse_v4l2_menu,
    _pick_v4l2_menu_value, _store_cached_capabilities,
    _validate_against_windows_capabilities, format_control_value,
    get_control_settings_string, set_camera_controls, set_frame_rate,
    summarize_control_application)

# ---------------------------------------------------------------------------
# _capabilities_cache_key / _empty_capabilities
# ---------------------------------------------------------------------------

def test_capabilities_cache_key_format():
    """The cache key is the pipe-joined 'os|commit|device' triple."""
    assert _capabilities_cache_key(
        os_name="darwin",
        git_hash="abc123",
        device_name="USB Cam",
    ) == "darwin|abc123|USB Cam"


def test_empty_capabilities_has_expected_shape():
    """The empty-capabilities template has the expected keys and neutral
    defaults (empty lists, False flags, None ranges)."""
    caps = _empty_capabilities()
    assert caps["pixel_formats"] == []
    assert caps["fps"] == []
    assert caps["modes"] == []
    assert caps["supports_auto_exposure"] is False
    assert caps["brightness_range"] is None


# ---------------------------------------------------------------------------
# _parse_v4l2_menu / _pick_v4l2_menu_value
# ---------------------------------------------------------------------------

MENU_TEXT = """
     exposure_auto 0x009a0901 (menu)   : min=0 max=3 default=3 value=1
\t\t\t\t0: Auto Mode
\t\t\t\t1: Manual Mode
\t\t\t\t2: Shutter Priority Mode
\t\t\t\t3: Aperture Priority Mode
     exposure_absolute 0x009a0902 (int) : min=3 max=2047
"""


def test_parse_v4l2_menu_reads_items_until_next_control():
    """The menu block for a control is parsed into a {label: value} map, and
    parsing stops at the next (non-indented) control line."""
    menu = _parse_v4l2_menu(text=MENU_TEXT, control_name="exposure_auto")
    assert menu == {
        "Auto Mode": 0,
        "Manual Mode": 1,
        "Shutter Priority Mode": 2,
        "Aperture Priority Mode": 3,
    }


def test_parse_v4l2_menu_missing_control_is_empty():
    """A control that isn't present in the output yields an empty menu."""
    assert _parse_v4l2_menu(text=MENU_TEXT, control_name="focus_auto") == {}


def test_pick_v4l2_menu_value_prefers_auto():
    """With prefer_auto=True the first auto-like entry ('Auto Mode') is chosen."""
    menu = _parse_v4l2_menu(text=MENU_TEXT, control_name="exposure_auto")
    assert _pick_v4l2_menu_value(menu=menu, prefer_auto=True) == 0


def test_pick_v4l2_menu_value_prefers_manual():
    """With prefer_auto=False the manual entry ('Manual Mode') is chosen."""
    menu = _parse_v4l2_menu(text=MENU_TEXT, control_name="exposure_auto")
    assert _pick_v4l2_menu_value(menu=menu, prefer_auto=False) == 1


def test_pick_v4l2_menu_value_empty_menu_returns_none():
    """An empty menu has nothing to pick, so None is returned."""
    assert _pick_v4l2_menu_value(menu={}, prefer_auto=True) is None


# ---------------------------------------------------------------------------
# _validate_against_windows_capabilities
# ---------------------------------------------------------------------------

WIN_CAPS = {
    "pixel_formats": ["YUYV", "MJPG"],
    "fps": [30, 60],
    "modes": [
        {"width": 1280, "height": 720, "fps": [30, 60]},
        {"width": 640, "height": 480, "fps": [30]},
    ],
}


def test_windows_caps_all_valid():
    """Settings fully covered by the cached capabilities land entirely in the
    'valid' partition, with nothing invalid."""
    valid, invalid = _validate_against_windows_capabilities(
        settings={"pixel_format": "yuyv", "width": 1280, "height": 720, "fps": 30},
        caps=WIN_CAPS,
    )
    assert valid == {"pixel_format": "yuyv", "width": 1280, "height": 720, "fps": 30}
    assert invalid == {}


def test_windows_caps_all_invalid():
    """Settings the capabilities can positively reject (unknown format, absent
    mode, unsupported fps) all land in the 'invalid' partition."""
    valid, invalid = _validate_against_windows_capabilities(
        settings={"pixel_format": "NV12", "width": 800, "height": 600, "fps": 25},
        caps=WIN_CAPS,
    )
    assert valid == {}
    assert invalid == {"pixel_format": "NV12", "width": 800, "height": 600, "fps": 25}


def test_windows_caps_fps_left_out_when_no_pool():
    """With no fps pool and no matched mode, fps can't be judged, so it lands in
    neither partition rather than being wrongly failed."""
    valid, invalid = _validate_against_windows_capabilities(
        settings={"fps": 30},
        caps={"pixel_formats": []},
    )
    assert "fps" not in valid
    assert "fps" not in invalid


def test_windows_caps_fps_checked_against_matched_mode():
    """fps is validated against the specific matched mode's rates:
    60 is valid globally but invalid for 640x480, which only lists 30."""
    valid, invalid = _validate_against_windows_capabilities(
        settings={"width": 640, "height": 480, "fps": 60},
        caps=WIN_CAPS,
    )
    assert invalid == {"fps": 60}
    assert valid == {"width": 640, "height": 480}


# ---------------------------------------------------------------------------
# get_control_settings_string / format_control_value / summarize
# ---------------------------------------------------------------------------

def test_get_control_settings_string():
    """A control dict is rendered as a comma-joined 'key=value' string."""
    result = get_control_settings_string(
        controls={
            "width": 640,
            "height": 480,
        },
    )
    assert result == "width=640,height=480"


def test_format_control_value_auto_controls_render_on_off():
    """Auto controls render as ON/OFF according to each control's own numeric
    convention (auto_exposure 0 == auto; auto_focus 1 == auto)."""
    assert format_control_value(key="auto_exposure", value=0) == "ON"
    assert format_control_value(key="auto_exposure", value=1) == "OFF"
    assert format_control_value(key="auto_focus", value=1) == "ON"
    assert format_control_value(key="auto_focus", value=0) == "OFF"


def test_format_control_value_plain_control_is_stringified():
    """A non-auto control's value is just stringified."""
    assert format_control_value(key="brightness", value=128) == "128"


def test_summarize_control_application_groups_outcomes():
    """The summary lists the devnode and separately labels applied, queued
    (unverified) and failed controls."""
    summary = summarize_control_application(
        devnode="2",
        applied={"brightness": 150},
        failed={"fps": 120},
        unverified={"width": 640},
    )
    summary = "\n".join(" ".join(line) for line in summary)
    assert "Successfully set brightness=150" in summary
    assert re.search(r"Queued.+width=640", summary) is not None
    assert "Failed to set fps=120" in summary


# ---------------------------------------------------------------------------
# On-disk capabilities cache
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    """Point the module's cache path at a temp file so store/load tests never
    touch the real project .cache."""
    path = tmp_path / "camera_capabilities.json"
    monkeypatch.setattr(cs, "CAMERA_CAPS_CACHE", path)
    return path


def test_cache_store_and_load_round_trip(cache_file):
    """Capabilities stored under a key are read back unchanged under that key."""
    os_name = "darwin"
    device_name = "Cam"
    key = _capabilities_cache_key(
        os_name=os_name,
        git_hash="abc123",
        device_name=device_name,
    )
    caps = {"fps": [30]}
    _store_cached_capabilities(
        cache_key=key,
        caps=caps,
        os_name=os_name,
        device_name=device_name,
    )
    assert _load_cached_capabilities(cache_key=key) == caps


def test_cache_load_miss_returns_none(cache_file):
    """Trying to load a key that was never stored returns None."""
    _store_cached_capabilities(
        cache_key="darwin|hash1|Cam",
        caps={"fps": [30]},
        os_name="darwin",
        device_name="Cam",
    )
    assert _load_cached_capabilities(cache_key="darwin|other|Cam") is None


def test_cache_load_none_when_file_absent(cache_file):
    """Loading when no cache file exists yet returns None (not an error)."""
    assert _load_cached_capabilities(cache_key="anything") is None


def test_cache_store_evicts_stale_commit_for_same_device(cache_file):
    """Storing a new commit's caps for the same OS+device overwrites 
    the previous commit's entry, so only the newest survives."""
    os_name = "darwin"
    device_name = "Cam"
    _store_cached_capabilities(
        cache_key="darwin|hash1|Cam",
        caps={"fps": [30]},
        os_name=os_name,
        device_name=device_name,
    )
    _store_cached_capabilities(
        cache_key="darwin|hash2|Cam",
        caps={"fps": [60]},
        os_name=os_name,
        device_name=device_name,
    )
    data = json.loads(cache_file.read_text())
    assert list(data.keys()) == [f"{os_name}|hash2|{device_name}"]


def test_cache_store_keeps_other_devices(cache_file):
    """Overwriting is scoped per device: storing caps for one device leaves other devices' entries intact."""
    os_name = "darwin"
    _store_cached_capabilities(
        cache_key="darwin|hash1|CamA",
        caps={"fps": [30]},
        os_name=os_name,
        device_name="CamA",
    )
    _store_cached_capabilities(
        cache_key="darwin|hash2|CamB",
        caps={"fps": [60]},
        os_name=os_name,
        device_name="CamB",
    )
    data = json.loads(cache_file.read_text())
    assert set(data.keys()) == {f"{os_name}|hash1|CamA", f"{os_name}|hash2|CamB"}


# ---------------------------------------------------------------------------
# _linux_camera_capabilities (parses v4l2-ctl output via faked run_capture_cmd)
# ---------------------------------------------------------------------------

V4L2_FORMATS = """
ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t[1]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.017s (60.000 fps)
"""

V4L2_CONTROLS = """
                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
                            hue 0x00980903 (int)    : min=-2000 max=2000 step=1 default=0 value=0
"""


def test_linux_capabilities_parses_formats_and_controls(monkeypatch):
    """The two v4l2-ctl calls (formats + controls) are parsed into the full
    capabilities dict: pixel formats, frame rates, control ranges/defaults, and
    the discrete resolution modes."""
    def fake_run(cmd, check=True):
        if "--list-formats-ext" in cmd:
            return V4L2_FORMATS, None
        return V4L2_CONTROLS, None

    monkeypatch.setattr(cs, "run_capture_cmd", fake_run)
    caps = _linux_camera_capabilities(devnode="/dev/video0")

    assert caps["pixel_formats"] == ["MJPG", "YUYV"]
    assert caps["fps"] == [30, 60]
    assert caps["brightness_range"] == (0, 255)
    assert caps["brightness_default"] == 128
    assert caps["hue_range"] == (-2000, 2000)
    assert {(m["width"], m["height"]) for m in caps["modes"]} == {
        (1280, 720),
        (640, 480),
        (1920, 1080),
    }


# ---------------------------------------------------------------------------
# set_frame_rate: verified/unverified semantics across platforms
# ---------------------------------------------------------------------------

def test_set_frame_rate_linux_success(monkeypatch, as_platform):
    """On Linux a successful v4l2-ctl call reports (success=True, verified=True)."""
    as_platform(cs, "linux")
    monkeypatch.setattr(cs, "run_capture_cmd", lambda cmd, check=True: ("", None))
    assert set_frame_rate(devnode="/dev/video0", fps=30) == (True, True)


def test_set_frame_rate_linux_failure(monkeypatch, as_platform):
    """On Linux a failing v4l2-ctl call reports (success=False, verified=True):
    the attempt was actually made and rejected."""
    as_platform(cs, "linux")
    monkeypatch.setattr(cs, "run_capture_cmd", lambda cmd, check=True: ("err", RuntimeError("boom")))
    assert set_frame_rate(devnode="/dev/video0", fps=30) == (False, True)


def test_set_frame_rate_windows_no_cache_is_unverified(monkeypatch, as_platform):
    """On Windows with no probed capabilities cached, the fps is accepted but
    reported as unverified (verified=False) since it's only queued for capture open."""
    as_platform(cs, "windows")
    monkeypatch.setattr(cs, "_windows_capabilities_for_device", lambda devnode, device_name: None)
    assert set_frame_rate(devnode="0", fps=30, device_name="Cam") == (True, False)


def test_set_frame_rate_windows_cached_valid(monkeypatch, as_platform):
    """On Windows with cached capabilities, a supported fps reports
    (success=True, verified=True)."""
    as_platform(cs, "windows")
    monkeypatch.setattr(
        cs,
        "_windows_capabilities_for_device",
        lambda devnode, device_name: {"fps": [30, 60], "modes": []},
    )
    assert set_frame_rate(devnode="0", fps=30, device_name="Cam") == (True, True)


def test_set_frame_rate_windows_cached_invalid(monkeypatch, as_platform):
    """On Windows with cached capabilities, an unsupported fps reports
    (success=False, verified=True); the cache lets it be positively rejected."""
    as_platform(cs, "windows")
    monkeypatch.setattr(
        cs,
        "_windows_capabilities_for_device",
        lambda devnode, device_name: {"fps": [30, 60], "modes": []},
    )
    assert set_frame_rate(devnode="0", fps=25, device_name="Cam") == (False, True)


# ---------------------------------------------------------------------------
# set_camera_controls (macOS): width/height/pixel_format are never tested
# here -- apply_camera_controls strips them out and tests them, together
# with fps, as a single atomic mode probe instead (see below). Only
# brightness/hue/saturation (software-applied, unverifiable) and rejecting
# ffmpeg-unsupported controls remain in scope here.
# ---------------------------------------------------------------------------

def test_set_camera_controls_mac_reports_color_controls_as_applied(as_platform):
    """Brightness/hue/saturation can't be verified via any probe (they're
    applied in software during capture), so they're always reported as
    applied outright."""
    as_platform(cs, "mac")
    successful, unverified = set_camera_controls(
        devnode="1",
        control_settings={"brightness": 128, "hue": 0, "saturation": 100},
    )
    assert successful == {"brightness": 128, "hue": 0, "saturation": 100}
    assert unverified == set()


def test_set_camera_controls_mac_rejects_unsupported_controls(as_platform):
    """auto_exposure/auto_focus aren't supported via ffmpeg on macOS and are
    silently dropped (with a warning) rather than reported as applied."""
    as_platform(cs, "mac")
    successful, unverified = set_camera_controls(
        devnode="1",
        control_settings={
            "brightness": 128,
            "auto_exposure": 0,
            "auto_focus": 0,
        },
    )
    assert successful == {"brightness": 128}
    assert "auto_exposure" not in successful
    assert "auto_focus" not in successful


# ---------------------------------------------------------------------------
# _mac_camera_capabilities: built from whatever probe_avfoundation_capabilities
# (native AVFoundation querying) reports, per pixel format.
# ---------------------------------------------------------------------------

def test_mac_camera_capabilities_reflects_native_modes_by_format(monkeypatch):
    """caps['modes_by_format'], caps['pixel_formats'], caps['modes'] and
    caps['fps'] are all derived directly from what
    probe_avfoundation_capabilities reports natively for the device."""
    monkeypatch.setattr(
        cs, "probe_avfoundation_capabilities",
        lambda device_index, device_name=None: {
            "YUYV": [{"width": 1280, "height": 720, "fps": [30]}],
            "NV12": [{"width": 1280, "height": 720, "fps": [30]}],
        },
    )

    caps = _mac_camera_capabilities(device_index=0)

    assert caps["modes_by_format"] == {
        "YUYV": [{"width": 1280, "height": 720, "fps": [30]}],
        "NV12": [{"width": 1280, "height": 720, "fps": [30]}],
    }
    assert caps["pixel_formats"] == ["NV12", "YUYV"]
    assert caps["modes"] == [{"width": 1280, "height": 720, "fps": [30]}]
    assert caps["fps"] == [30]


def test_mac_camera_capabilities_merges_fps_across_formats_per_resolution(monkeypatch):
    """caps['modes'] (the format-agnostic view) merges the fps values
    reported for a resolution across every format that supports it, since
    different pixel formats can genuinely unlock different fps at the same
    resolution (e.g. a compressed format allowing a higher rate)."""
    monkeypatch.setattr(
        cs, "probe_avfoundation_capabilities",
        lambda device_index, device_name=None: {
            "YUYV": [{"width": 640, "height": 480, "fps": [30, 60]}],
            "MJPG": [{"width": 640, "height": 480, "fps": [15, 30]}],
        },
    )

    caps = _mac_camera_capabilities(device_index=0)

    assert caps["modes"] == [{"width": 640, "height": 480, "fps": [15, 30, 60]}]
    assert caps["fps"] == [15, 30, 60]


def test_mac_camera_capabilities_passes_device_name_through(monkeypatch):
    """device_name is threaded through to the native probe so it can resolve
    the device by name, robust to enumeration order rather than relying on
    a raw index that may not match AVFoundation's own device ordering."""
    seen = {}

    def fake_probe(device_index, device_name=None):
        seen["device_index"] = device_index
        seen["device_name"] = device_name
        return {}

    monkeypatch.setattr(cs, "probe_avfoundation_capabilities", fake_probe)
    _mac_camera_capabilities(device_index=2, device_name="BRIO")

    assert seen == {"device_index": 2, "device_name": "BRIO"}
