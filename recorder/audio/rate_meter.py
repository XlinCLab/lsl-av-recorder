"""Measure the rate at which an audio device really delivers samples.

An audio stream's timestamps are built from its *configured* sample rate, so
they can never reveal a device that runs at a different rate than it was
configured for. The only independent reference is real time:
how many frames arrived per second of wall-clock time.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional

# Frames delivered while the stream settles
# (buffered startup data often arrives in a burst)
# are excluded from the measurement
DEFAULT_WARMUP_S = 1.0
# Minimum measurement window:
# a shorter measurement window than this is too noisy to report a rate
DEFAULT_MIN_WINDOW_S = 1.0


class RateMeter:
    """Counts frames per wall-clock second between audio callbacks."""

    def __init__(
        self,
        warmup_s: float = DEFAULT_WARMUP_S,
        min_window_s: float = DEFAULT_MIN_WINDOW_S,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.warmup_s = float(warmup_s)
        self.min_window_s = float(min_window_s)
        self._clock = clock
        self.callbacks = 0
        self.total_frames = 0
        self.input_overflows = 0
        self._t_first: Optional[float] = None
        self._base: Optional[tuple[float, int]] = None  # (time, frames) at the end of the warm-up
        self._last: Optional[tuple[float, int]] = None  # (time, frames) at the latest callback

    def update(self, frames: int, overflow: bool = False) -> None:
        now = self._clock()
        self.callbacks += 1
        self.total_frames += int(frames)
        if overflow:
            self.input_overflows += 1
        if self._t_first is None:
            self._t_first = now
        if self._base is None and (now - self._t_first) >= self.warmup_s:
            self._base = (now, self.total_frames)
        self._last = (now, self.total_frames)

    @property
    def window_s(self) -> float:
        """Length of the measured window in seconds (0 until the warm-up has passed)."""
        if self._base is None or self._last is None:
            return 0.0
        return self._last[0] - self._base[0]

    @property
    def measured_rate(self) -> Optional[float]:
        """Frames per second of real time, or None if the window is too short to tell."""
        if self._base is None or self._last is None:
            return None
        window = self._last[0] - self._base[0]
        if window < self.min_window_s:
            return None
        return (self._last[1] - self._base[1]) / window

    def summary(self) -> Dict[str, object]:
        return {
            "measured_srate": self.measured_rate,
            "measured_window_s": self.window_s,
            "callbacks": self.callbacks,
            "total_frames": self.total_frames,
            "input_overflows": self.input_overflows,
        }


def rate_deviation(measured: Optional[float], configured: float) -> Optional[float]:
    """Relative deviation of a measured rate from the configured one (None if unknown)."""
    if measured is None or not configured:
        return None
    return abs(measured - configured) / float(configured)
