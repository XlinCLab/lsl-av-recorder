"""Tests for recorder.audio.rate_meter: measuring the rate at which audio really arrives."""
from __future__ import annotations

import pytest

from recorder.audio.rate_meter import RateMeter, rate_deviation


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _feed(meter, clock, *, device_rate, chunk=1024, seconds=10.0, jitter=0.0):
    """Deliver `seconds` of audio from a device producing `device_rate` frames per second."""
    interval = chunk / device_rate
    for i in range(int(seconds / interval)):
        clock.t += interval + (jitter if i % 2 else -jitter)
        meter.update(chunk)


def test_measures_the_rate_at_which_frames_arrive():
    clock = _Clock()
    meter = RateMeter(clock=clock)
    _feed(meter, clock, device_rate=44100)
    assert meter.measured_rate == pytest.approx(44100, rel=0.002)


def test_reveals_a_device_running_faster_than_its_label():
    """Frames arriving at 48 kHz are measured as 48 kHz, whatever rate the stream was opened with."""
    clock = _Clock()
    meter = RateMeter(clock=clock)
    _feed(meter, clock, device_rate=48000)
    measured = meter.measured_rate
    assert measured == pytest.approx(48000, rel=0.002)
    assert rate_deviation(measured, 44100) == pytest.approx(0.0884, abs=0.002)


def test_callback_jitter_averages_out():
    clock = _Clock()
    meter = RateMeter(clock=clock)
    _feed(meter, clock, device_rate=48000, jitter=0.004)
    assert meter.measured_rate == pytest.approx(48000, rel=0.005)


def test_startup_burst_is_left_out_of_the_measurement():
    """Buffered data delivered in a burst at start must not inflate the rate."""
    clock = _Clock()
    meter = RateMeter(clock=clock)
    for _ in range(20):  # 20 chunks within 20 ms, as when buffered startup data is flushed
        clock.t += 0.001
        meter.update(1024)
    _feed(meter, clock, device_rate=44100, seconds=6.0)
    assert meter.measured_rate == pytest.approx(44100, rel=0.005)


def test_too_short_to_measure_returns_none():
    clock = _Clock()
    meter = RateMeter(clock=clock)
    _feed(meter, clock, device_rate=44100, seconds=1.5)  # barely past the 1 s warm-up
    assert meter.measured_rate is None


def test_no_callbacks_returns_none():
    meter = RateMeter(clock=_Clock())
    assert meter.measured_rate is None
    assert meter.window_s == 0.0


def test_counts_callbacks_frames_and_overflows():
    clock = _Clock()
    meter = RateMeter(clock=clock)
    for overflow in (False, True, True, False):
        clock.t += 0.1
        meter.update(100, overflow=overflow)
    summary = meter.summary()
    assert summary["callbacks"] == 4
    assert summary["total_frames"] == 400
    assert summary["input_overflows"] == 2


def test_rate_deviation_handles_unknowns():
    assert rate_deviation(None, 44100) is None
    assert rate_deviation(44100, 0) is None
    assert rate_deviation(44100, 44100) == 0.0
