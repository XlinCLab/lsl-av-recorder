"""Tests for the pure helpers of recorder.gui.audio_test_panel (no QApplication needed)."""
from __future__ import annotations

import numpy as np

from recorder.audio.audio_test import summarize_test
from recorder.gui.audio_test_panel import format_result_html, meter_value


def test_meter_value_spans_the_range_and_clamps():
    assert meter_value(-60.0) == 0
    assert meter_value(0.0) == 600
    assert meter_value(-30.0) == 300
    assert meter_value(-180.0) == 0  # digital silence
    assert meter_value(6.0) == 600  # above full scale


def _result(measured, samples=None, opened=44100, configured=44100):
    if samples is None:
        t = np.arange(1000) / 1000
        samples = (0.1 * np.sin(2 * np.pi * 5 * t)).reshape(-1, 1).astype(np.float32)
    summary = {"measured_srate": measured, "measured_window_s": 3.0, "input_overflows": 0}
    return summarize_test(samples, configured, opened, summary, "Scarlett 2i2 USB: Audio")


def test_result_text_for_a_healthy_recording():
    text = format_result_html(_result(44080.0))
    assert "✓ Delivered rate: 44,080 Hz (configured 44,100 Hz)" in text
    assert "No problems detected" in text
    assert "Scarlett 2i2 USB: Audio" in text


def test_result_text_flags_a_wrong_rate_and_lists_the_warning():
    text = format_result_html(_result(47940.0))
    assert "⚠ Delivered rate: 47,940 Hz" in text
    assert "<b>Warnings</b>" in text and "off from the configured" in text


def test_result_text_says_when_the_rate_could_not_be_measured():
    assert "not measured" in format_result_html(_result(None))


def test_result_text_marks_a_silent_channel_and_escapes_device_names():
    samples = np.column_stack([0.2 * np.ones(100), np.zeros(100)]).astype(np.float32)
    result = _result(44100.0, samples)
    result.device_name = "Mic <USB> & Co"
    text = format_result_html(result)
    assert "peak per channel: -14.0, silent dBFS" in text
    assert "Mic &lt;USB&gt; &amp; Co" in text
