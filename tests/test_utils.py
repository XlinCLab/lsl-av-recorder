"""Tests for recorder.utils.utils."""

from __future__ import annotations

import re
import subprocess

from recorder.utils import utils
from recorder.utils.utils import (_extract_default, _extract_range,
                                  get_commit_hash)

# Example of `v4l2-ctl -L` output
V4L2_CTRLS = """
                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
                            hue 0x00980903 (int)    : min=-2000 max=2000 step=1 default=0 value=0
                     saturation 0x00980902 (int)    : min=0 max=200 step=1 default=100 value=100
"""


# ---------------------------------------------------------------------------
# _extract_range
# ---------------------------------------------------------------------------

def test_extract_range_parses_min_max():
    assert _extract_range(V4L2_CTRLS, "brightness") == (0, 255)
    assert _extract_range(V4L2_CTRLS, "hue") == (-2000, 2000)
    assert _extract_range(V4L2_CTRLS, "saturation") == (0, 200)


def test_extract_range_missing_control_returns_none():
    assert _extract_range(V4L2_CTRLS, "contrast") is None


# ---------------------------------------------------------------------------
# _extract_default
# ---------------------------------------------------------------------------

def test_extract_default_parses_value():
    assert _extract_default(V4L2_CTRLS, "brightness") == 128
    assert _extract_default(V4L2_CTRLS, "hue") == 0
    assert _extract_default(V4L2_CTRLS, "saturation") == 100


def test_extract_default_missing_control_returns_none():
    assert _extract_default(V4L2_CTRLS, "contrast") is None

# ---------------------------------------------------------------------------
# get_commit_hash
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


def test_get_commit_hash_returns_short_sha(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(stdout="abcdef1234567890\n")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    # Truncated to 12 chars
    assert get_commit_hash(tmp_path) == "abcdef123456"


def test_get_commit_hash_non_hex_output_is_unknown(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(stdout="not-a-sha\n")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    assert get_commit_hash(tmp_path) == "unknown"


def test_get_commit_hash_falls_back_to_date_on_failure(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(returncode=128, cmd=cmd)

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    result = get_commit_hash(tmp_path)
    # Fallback is a UTC YYYYMMDD stamp
    assert re.fullmatch(r"\d{8}", result)
