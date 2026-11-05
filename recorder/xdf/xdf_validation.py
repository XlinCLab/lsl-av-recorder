"""Post-hoc validation of a recorded XDF file against the AppConfig/LSL streams
that were supposed to produce it.

Used by the GUI's "Test recording settings" button: run a short real recording,
then load the resulting XDF file with pyxdf and check that each configured
stream actually shows up with the right shape (sample rate, channel count,
roughly the expected number of samples for the recording's duration), so
config/hardware problems surface immediately instead of at the start of a
real session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import pyxdf
from pylsl import StreamInfo

from ..audio.channel_labels import label_problems, resolve_channel_labels
from ..audio.constants import RATE_VALIDATION_TOLERANCE
from ..audio.device_identity import normalize_device_name
from ..audio.levels import (as_float_samples, level_problem, signal_levels,
                            silent_channel_message, silent_channels)
from ..config import AppConfig

# Sample/frame counts are compared to duration * nominal_srate within this
# relative tolerance. Generous because startup latency (first LSL
# time_correction, first camera frame, device buffering) eats into the
# window and real capture rates jitter around the nominal rate.
DEFAULT_COUNT_TOLERANCE = 0.3
# Nominal-vs-effective sample rate comparisons use a tighter tolerance since
# these are just round-trip/plumbing checks (the declared rate is written
# verbatim into the XDF header), not a measurement of hardware timing.
DEFAULT_RATE_TOLERANCE = 0.05
# Audio sample counts are checked tighter than other streams, since an audio device's rate is steady
AUDIO_COUNT_TOLERANCE = 0.15


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    # A passed check that still deserves the user's attention (e.g. a very quiet signal)
    warning: bool = False


@dataclass
class ValidationReport:
    checks: List[CheckResult]
    xdf_path: str

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if c.passed and c.warning]

    def summary(self) -> str:
        n_ok = sum(1 for c in self.checks if c.passed)
        text = f"{n_ok}/{len(self.checks)} checks passed"
        if self.warnings:
            text += f", {len(self.warnings)} warning(s)"
        return text

    def detailed_text(self) -> str:
        lines = []
        for c in self.checks:
            mark = "FAIL" if not c.passed else ("WARN" if c.warning else "PASS")
            lines.append(f"[{mark}] {c.name}: {c.detail}")
        return "\n".join(lines)


def _check(name: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult(name=name, passed=passed, detail=detail)


def _warn(name: str, detail: str) -> CheckResult:
    """A check that passes but is flagged for attention."""
    return CheckResult(name=name, passed=True, detail=detail, warning=True)


def _check_close(name: str, expected: float, actual: float, rel_tol: float) -> CheckResult:
    if expected == 0:
        passed = abs(actual) < 1e-9
    else:
        passed = abs(actual - expected) <= rel_tol * abs(expected)
    return _check(name=name, passed=passed, detail=f"expected={expected}, actual={actual}")


def _check_count_in_range(name: str, actual_n: int, expected_n: float, rel_tol: float) -> CheckResult:
    lo = expected_n * (1 - rel_tol)
    hi = expected_n * (1 + rel_tol)
    passed = lo <= actual_n <= hi
    return _check(
        name=name,
        passed=passed,
        detail=f"expected~={expected_n:.0f} (+/-{rel_tol:.0%}), actual={actual_n}",
    )


def _validate_audio(cfg: AppConfig, by_name: dict, expected_duration_s: float) -> List[CheckResult]:
    checks: List[CheckResult] = []
    name = cfg.Audio.StreamName or "Audio"
    stream = by_name.get(name)
    if stream is None:
        checks.append(_check(
            name=f"Audio stream <{name}> present",
            passed=False,
            detail="not found in XDF"
        ))
        return checks
    checks.append(_check(name=f"Audio stream <{name}> present", passed=True, detail="found"))

    info = stream["info"]
    n_samples = stream["time_series"].shape[0]
    actual_channels = int(info["channel_count"][0])
    checks.append(_check(
        name="Audio channel count",
        passed=actual_channels == cfg.Audio.Channels,
        detail=f"expected={cfg.Audio.Channels}, actual={actual_channels}",
    ))
    actual_format = str((info.get("channel_format") or [None])[0])
    checks.append(_check(
        name="Audio sample format",
        passed=actual_format == cfg.Audio.SampleFormat,
        detail=f"expected={cfg.Audio.SampleFormat}, actual={actual_format}",
    ))
    effective_srate = float(info["effective_srate"] or 0.0)
    checks.append(_check_close(
        name="Audio effective sample rate",
        expected=cfg.Audio.SampleRate,
        actual=effective_srate,
        rel_tol=DEFAULT_RATE_TOLERANCE,
    ))
    checks.append(_check_count_in_range(
        name="Audio sample count",
        actual_n=n_samples,
        expected_n=expected_duration_s * cfg.Audio.SampleRate,
        rel_tol=AUDIO_COUNT_TOLERANCE,
    ))
    checks.extend(_validate_audio_properties(cfg, stream, n_samples))
    return checks


def _first(values) -> Optional[str]:
    """First value of a pyxdf header/footer field (a list of strings), or None."""
    if isinstance(values, (list, tuple)) and values:
        return values[0]
    return None


def _validate_audio_properties(cfg: AppConfig, stream: dict, n_samples: int) -> List[CheckResult]:
    """Checks that the audio stream has the properties that were configured:
    declared and really delivered sample rate, capture bit depth, input device and
    signal level."""
    checks: List[CheckResult] = []
    info = stream["info"]
    desc = _first(info.get("desc"))
    desc = desc if isinstance(desc, dict) else {}
    footer = (stream.get("footer") or {}).get("info") or {}

    nominal = _first(info.get("nominal_srate"))
    if nominal is not None:
        checks.append(_check(
            "Audio sample rate in header",
            float(nominal) == float(cfg.Audio.SampleRate),
            f"expected={cfg.Audio.SampleRate}, actual={float(nominal):g}"
            + ("" if float(nominal) == float(cfg.Audio.SampleRate)
               else " (the device did not accept the configured rate)"),
        ))

    # The effective rate above is derived from timestamps built from the configured rate, so
    # it cannot reveal a device running at a different rate. The measured rate counts frames
    # per second of real time.
    measured = _first(footer.get("measured_srate"))
    if measured is not None:
        checks.append(_check_close(
            "Audio measured sample rate (real time)", float(cfg.Audio.SampleRate), float(measured),
            RATE_VALIDATION_TOLERANCE,
        ))
    else:
        checks.append(_warn(
            "Audio measured sample rate (real time)",
            "not available (recording too short to measure, or not recorded)",
        ))

    capture_bit_depth = _first(desc.get("capture_bit_depth"))
    if capture_bit_depth is not None:
        ok = int(capture_bit_depth) == int(cfg.Audio.BitDepth)
        checks.append(_check(
            "Audio capture bit depth", ok,
            f"expected={cfg.Audio.BitDepth}, actual={capture_bit_depth}"
            + ("" if ok else " (the device refused the configured capture format)"),
        ))

    device_name = _first(desc.get("device_name"))
    if device_name and cfg.Audio.DeviceName:
        ok = normalize_device_name(device_name) == normalize_device_name(cfg.Audio.DeviceName)
        checks.append(_check(
            "Audio input device", ok,
            f"expected={normalize_device_name(cfg.Audio.DeviceName)}, actual={normalize_device_name(device_name)}",
        ))

    overflows = _first(footer.get("input_overflows"))
    if overflows is not None and int(overflows) > 0:
        checks.append(_warn(
            "Audio input overflows",
            f"{overflows} overflow(s) reported by the audio driver; samples may have been lost",
        ))

    expected_labels = resolve_channel_labels(cfg.Audio.ChannelLabels, cfg.Audio.Channels)
    checks.extend(_audio_label_check(info, expected_labels, cfg.Audio.ChannelLabels, cfg.Audio.Channels))

    if n_samples > 0:
        checks.extend(_audio_level_checks(
            stream["time_series"], _first(info.get("channel_format")), expected_labels,
        ))
    return checks


def _header_channel_labels(info: dict) -> Optional[List[str]]:
    """Channel labels from a stream header's <desc><channels>, or None if it has none."""
    desc = _first(info.get("desc"))
    if not isinstance(desc, dict):
        return None
    channels = _first(desc.get("channels"))
    if not isinstance(channels, dict):
        return None
    labels = []
    for channel in channels.get("channel") or []:
        label = _first(channel.get("label")) if isinstance(channel, dict) else None
        labels.append(label or "")
    return labels or None


def _audio_label_check(info: dict, expected: List[str], configured: str, n_channels: int) -> List[CheckResult]:
    """The labels in the XDF header are the configured ones, one per channel."""
    checks: List[CheckResult] = []
    for problem in label_problems(configured, n_channels):
        checks.append(_warn("Audio channel labels", problem))
    actual = _header_channel_labels(info)
    if actual is None:
        checks.append(_check("Audio channel labels in header", False, "no channel labels in the XDF header"))
    else:
        checks.append(_check(
            "Audio channel labels in header", actual == expected,
            f"expected={expected}, actual={actual}",
        ))
    return checks


def _audio_level_checks(samples, channel_format: Optional[str], labels: List[str]) -> List[CheckResult]:
    """Peak and RMS level in dBFS, flagged when the signal is silent, very quiet or clipping.
    If >1 channel: whether one of them is silent while another carries a signal."""
    levels = signal_levels(as_float_samples(samples, channel_format))
    detail = f"peak {levels.peak_dbfs:.1f} dBFS, RMS {levels.rms_dbfs:.1f} dBFS"
    problem = level_problem(levels)
    checks = [
        _warn("Audio signal level", f"{detail}: {problem}") if problem
        else _check("Audio signal level", True, detail)
    ]
    if len(levels.channel_peaks) > 1:
        silent = silent_channels(levels)
        per_channel = ", ".join(
            f"{labels[i] if i < len(labels) else i + 1}: {db:.1f}"
            for i, db in enumerate(levels.channel_peak_dbfs)
        )
        if silent:
            checks.extend(_warn("Audio channel levels", silent_channel_message(c, labels)) for c in silent)
        else:
            checks.append(_check("Audio channel levels", True, f"peak per channel (dBFS): {per_channel}"))
    return checks


def _validate_video(cfg: AppConfig, by_name: dict, expected_duration_s: float) -> List[CheckResult]:
    checks: List[CheckResult] = []
    for cam in cfg.Video.Cams:
        if not cam.Enabled:
            continue
        name = f"Camera-{cam.Label}"
        stream = by_name.get(name)
        if stream is None:
            checks.append(_check(f"Video stream <{name}> present", False, "not found in XDF"))
            continue
        checks.append(_check(f"Video stream <{name}> present", True, "found"))

        info = stream["info"]
        n_frames = stream["time_series"].shape[0]
        effective_fps = float(info["effective_srate"] or 0.0)
        checks.append(_check_close(
            f"Video <{name}> effective FPS", float(cam.FPS or 0.0), effective_fps, DEFAULT_RATE_TOLERANCE,
        ))
        checks.append(_check_count_in_range(
            f"Video <{name}> frame count", n_frames, expected_duration_s * float(cam.FPS or 0.0),
            DEFAULT_COUNT_TOLERANCE,
        ))

        desc = info.get("desc") or [{}]
        video_path = (desc[0].get("video_path") or [None])[0]
        if not video_path or not os.path.exists(video_path):
            checks.append(_check(f"Video <{name}> file written", False, f"missing: {video_path}"))
        else:
            size = os.path.getsize(video_path)
            checks.append(_check(
                f"Video <{name}> file written", size > 0, f"{video_path} ({size} bytes)",
            ))

        # `width`/`height` in the XDF desc are the frame size the capture backend actually delivered;
        # comparing against the GUI/config settings here catches a resolution mismatch
        actual_width = (desc[0].get("width") or [None])[0]
        actual_height = (desc[0].get("height") or [None])[0]
        if actual_width is not None and actual_height is not None:
            expected_dims = f"{cam.Width}x{cam.Height}"
            actual_dims = f"{actual_width}x{actual_height}"
            checks.append(_check(
                f"Video <{name}> frame size",
                actual_dims == expected_dims,
                f"expected={expected_dims}, actual={actual_dims}",
            ))

        # Pixel format cannot be independently re-derived from the recorded
        # video/XDF data the way fps/frame count can, since every capture backend
        # converts delivered frames to a uniform format regardless of native
        # capture format, so the original pixel format leaves no trace in
        # the output file. This checks only whether the live-verified
        # value VideoRecorder recorded at capture time (written into this
        # stream's description) matches what was configured
        # (skipped if missing or unknown).
        actual_pixel_format = (desc[0].get("pixel_format") or [None])[0]
        configured_pixel_format = cam.PixelFormat
        if (
            actual_pixel_format and actual_pixel_format != "unknown"
            and configured_pixel_format
        ):
            checks.append(_check(
                f"Video <{name}> pixel format",
                str(actual_pixel_format).upper() == str(configured_pixel_format).upper(),
                f"expected={configured_pixel_format}, actual={actual_pixel_format}",
            ))
    return checks


def _validate_lsl_streams(
    lsl_streams: List[StreamInfo], by_name: dict, expected_duration_s: float,
) -> List[CheckResult]:
    checks: List[CheckResult] = []
    for stream_info in lsl_streams:
        name = stream_info.name()
        stream = by_name.get(name)
        if stream is None:
            checks.append(_check(f"LSL stream <{name}> present", False, "not found in XDF"))
            continue
        checks.append(_check(f"LSL stream <{name}> present", True, "found"))

        info = stream["info"]
        # NB: pyxdf returns time_series as a plain list (not an ndarray)
        # for string-format streams (e.g. marker/trigger streams),
        # so len() is used here rather than .shape[0] to support both
        n_samples = len(stream["time_series"])

        actual_channels = int(info["channel_count"][0])
        checks.append(_check(
            f"LSL <{name}> channel count", actual_channels == stream_info.channel_count(),
            f"expected={stream_info.channel_count()}, actual={actual_channels}",
        ))

        nominal_srate = stream_info.nominal_srate()
        if nominal_srate > 0:
            effective_srate = float(info["effective_srate"] or 0.0)
            checks.append(_check_close(
                f"LSL <{name}> effective sample rate", nominal_srate, effective_srate, DEFAULT_RATE_TOLERANCE,
            ))
            checks.append(_check_count_in_range(
                f"LSL <{name}> sample count", n_samples, expected_duration_s * nominal_srate,
                DEFAULT_COUNT_TOLERANCE,
            ))
        else:
            # Irregular-rate (event-driven) streams, e.g. markers/triggers,
            # only emit a sample when a real event occurs.
            # Zero samples is a normal outcome if nothing happened to trigger
            # a marker during the test window, not a sign that the stream is broken.
            checks.append(_check(
                f"LSL <{name}> sample count",
                passed=n_samples is not None,
                detail=f"{n_samples} samples (irregular-rate stream)",
            ))

        clock_values = stream.get("clock_values") or []
        checks.append(_check(
            f"LSL <{name}> clock offset measured", len(clock_values) > 0,
            f"{len(clock_values)} time_correction() measurement(s) recorded"
            if clock_values else "no successful time_correction() measurement against this stream",
        ))
    return checks


def validate_test_recording(
    xdf_path: str,
    cfg: AppConfig,
    lsl_streams: Optional[List[StreamInfo]],
    expected_duration_s: float,
) -> ValidationReport:
    """Load `xdf_path` and check it matches what `cfg` (+ selected `lsl_streams`)
    should have produced for a recording lasting ~`expected_duration_s` seconds."""
    lsl_streams = lsl_streams or []
    try:
        streams, _header = pyxdf.load_xdf(xdf_path, synchronize_clocks=False, dejitter_timestamps=False)
    except Exception as exc:
        return ValidationReport(
            checks=[_check("XDF file loads", False, f"{type(exc).__name__}: {exc}")],
            xdf_path=xdf_path,
        )

    checks = [_check("XDF file loads", True, f"{len(streams)} stream(s) found")]
    by_name = {s["info"]["name"][0]: s for s in streams}

    if cfg.Audio.Enabled:
        checks.extend(_validate_audio(cfg, by_name, expected_duration_s))
    if cfg.Video.Enabled:
        checks.extend(_validate_video(cfg, by_name, expected_duration_s))
    if lsl_streams:
        checks.extend(_validate_lsl_streams(lsl_streams, by_name, expected_duration_s))

    if len(checks) == 1:
        checks.append(_check(
            "At least one stream configured", False,
            "Audio, Video, and LSL streams are all disabled/unselected; nothing was tested.",
        ))

    return ValidationReport(checks=checks, xdf_path=xdf_path)
