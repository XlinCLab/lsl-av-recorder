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


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class ValidationReport:
    checks: List[CheckResult]
    xdf_path: str

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def summary(self) -> str:
        n_ok = sum(1 for c in self.checks if c.passed)
        return f"{n_ok}/{len(self.checks)} checks passed"

    def detailed_text(self) -> str:
        lines = []
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"[{mark}] {c.name}: {c.detail}")
        return "\n".join(lines)


def _check(name: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult(name=name, passed=passed, detail=detail)


def _check_close(name: str, expected: float, actual: float, rel_tol: float) -> CheckResult:
    if expected == 0:
        passed = abs(actual) < 1e-9
    else:
        passed = abs(actual - expected) <= rel_tol * abs(expected)
    return _check(name, passed, f"expected={expected}, actual={actual}")


def _check_count_in_range(name: str, actual_n: int, expected_n: float, rel_tol: float) -> CheckResult:
    lo = expected_n * (1 - rel_tol)
    hi = expected_n * (1 + rel_tol)
    passed = lo <= actual_n <= hi
    return _check(
        name, passed,
        f"expected~={expected_n:.0f} (+/-{rel_tol:.0%}), actual={actual_n}",
    )


def _validate_audio(cfg: AppConfig, by_name: dict, expected_duration_s: float) -> List[CheckResult]:
    checks: List[CheckResult] = []
    name = cfg.Audio.StreamName or "Audio"
    stream = by_name.get(name)
    if stream is None:
        checks.append(_check(f"Audio stream <{name}> present", False, "not found in XDF"))
        return checks
    checks.append(_check(f"Audio stream <{name}> present", True, "found"))

    info = stream["info"]
    n_samples = stream["time_series"].shape[0]
    actual_channels = int(info["channel_count"][0])
    checks.append(_check(
        "Audio channel count", actual_channels == cfg.Audio.Channels,
        f"expected={cfg.Audio.Channels}, actual={actual_channels}",
    ))
    effective_srate = float(info["effective_srate"] or 0.0)
    checks.append(_check_close(
        "Audio effective sample rate", cfg.Audio.SampleRate, effective_srate, DEFAULT_RATE_TOLERANCE,
    ))
    checks.append(_check_count_in_range(
        "Audio sample count", n_samples, expected_duration_s * cfg.Audio.SampleRate, DEFAULT_COUNT_TOLERANCE,
    ))
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
            checks.append(_check(
                f"LSL <{name}> sample count", n_samples > 0,
                f"{n_samples} samples (irregular-rate stream)",
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
