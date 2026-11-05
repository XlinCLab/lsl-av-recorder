"""Tests for recorder.audio.levels: peak/RMS levels and what is wrong with them."""
from __future__ import annotations

import numpy as np
import pytest

from recorder.audio.levels import (FLOOR_DBFS, as_float_samples, dbfs,
                                   level_problem, signal_levels,
                                   silent_channels)


def test_dbfs_of_known_amplitudes():
    assert dbfs(1.0) == pytest.approx(0.0)
    assert dbfs(0.5) == pytest.approx(-6.02, abs=0.01)
    assert dbfs(0.1) == pytest.approx(-20.0)
    assert dbfs(0.0) == FLOOR_DBFS


def test_int16_samples_are_scaled_by_full_scale():
    x = np.array([[16384], [-32768]], dtype=np.int16)
    assert as_float_samples(x).tolist() == [[0.5], [-1.0]]
    assert as_float_samples(np.array([0.25], dtype=np.float32), "float32").tolist() == [0.25]


def test_signal_levels_per_channel_and_overall():
    x = np.array([[0.5, 0.0], [-0.5, 0.01]])
    levels = signal_levels(x)
    assert levels.peak == 0.5
    assert levels.channel_peaks == [0.5, 0.01]
    assert levels.peak_dbfs == pytest.approx(-6.02, abs=0.01)
    assert levels.rms == pytest.approx(np.sqrt((0.25 + 0.25 + 0.0001) / 4))


def test_signal_levels_of_nothing_is_silence():
    levels = signal_levels(np.zeros((0, 2)))
    assert levels.peak == 0.0 and levels.channel_peaks == []


def test_one_dimensional_input_is_a_single_channel():
    assert signal_levels(np.array([0.2, -0.4])).channel_peaks == [0.4]


@pytest.mark.parametrize(
    "peak, word",
    [(1.0, "clipping"), (0.0, "silent"), (0.003, "very quiet")],
)
def test_level_problem_names_the_problem(peak, word):
    problem = level_problem(signal_levels(np.array([peak, -peak])))
    assert problem and word in problem


def test_level_problem_none_for_a_healthy_signal():
    assert level_problem(signal_levels(np.array([0.2, -0.2]))) is None


def test_silent_channel_next_to_an_active_one_is_reported():
    x = np.column_stack([0.2 * np.ones(10), np.zeros(10)])
    assert silent_channels(signal_levels(x)) == [2]


def test_no_silent_channel_reported_when_everything_is_quiet_or_mono():
    assert silent_channels(signal_levels(np.zeros((10, 2)))) == []
    assert silent_channels(signal_levels(np.full((10, 1), 0.2))) == []
