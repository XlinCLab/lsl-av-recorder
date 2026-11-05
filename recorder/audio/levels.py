"""Signal level measurement shared by the test recording checks and the audio test."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from .channel_labels import default_channel_label
from .constants import (BITDEPTH_CONVERSION_FLOAT, CLIPPING_PEAK,
                        QUIET_PEAK_DBFS, SILENCE_PEAK_DBFS)

# Level reported for digital silence instead of minus infinity
FLOOR_DBFS = -180.0


def dbfs(value: float) -> float:
    """Level of a linear amplitude (full scale = 1.0) in dB relative to full scale."""
    return 20.0 * math.log10(value) if value > 0 else FLOOR_DBFS


def as_float_samples(samples, channel_format: Optional[str] = None) -> np.ndarray:
    """Samples as float64 in [-1, 1]: int16 is scaled by full scale (32768)."""
    x = np.asarray(samples)
    if channel_format == "int16" or x.dtype == np.int16:
        return x.astype(np.float64) / BITDEPTH_CONVERSION_FLOAT
    return x.astype(np.float64)


@dataclass(frozen=True)
class SignalLevels:
    peak: float  # linear, over all channels
    rms: float
    channel_peaks: List[float]

    @property
    def peak_dbfs(self) -> float:
        return dbfs(self.peak)

    @property
    def rms_dbfs(self) -> float:
        return dbfs(self.rms)

    @property
    def channel_peak_dbfs(self) -> List[float]:
        return [dbfs(p) for p in self.channel_peaks]


def signal_levels(samples) -> SignalLevels:
    """Peak (overall and per channel) and RMS of float samples shaped (n,) or (n, channels)."""
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0:
        return SignalLevels(0.0, 0.0, [])
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    channel_peaks = [float(p) for p in np.max(np.abs(x), axis=0)]
    return SignalLevels(
        peak=max(channel_peaks),
        rms=float(np.sqrt(np.mean(x ** 2))),
        channel_peaks=channel_peaks,
    )


def level_problem(levels: SignalLevels) -> Optional[str]:
    """What is wrong with the level of a recorded signal, if anything."""
    if levels.peak >= CLIPPING_PEAK:
        return "signal reaches full scale (clipping); lower the input gain"
    if levels.peak_dbfs < SILENCE_PEAK_DBFS:
        return "recording is silent; check that the microphone is connected, unmuted and on the selected input"
    if levels.peak_dbfs < QUIET_PEAK_DBFS:
        return "signal is very quiet; check the input gain and/or microphone distance"
    return None


def silent_channels(levels: SignalLevels) -> List[int]:
    """1-based numbers of channels that are silent although another channel carries a signal
    (typically the microphone is plugged into a different input)."""
    if len(levels.channel_peaks) < 2 or levels.peak_dbfs < QUIET_PEAK_DBFS:
        return []
    return [
        i + 1 for i, db in enumerate(levels.channel_peak_dbfs) if db < SILENCE_PEAK_DBFS
    ]


def channel_name(channel: int, labels: Optional[Sequence[str]] = None) -> str:
    """'Channel 2', or 'Channel 2 (Participant B)' when a label is assigned (`channel` is 1-based)."""
    label = labels[channel - 1] if labels and channel <= len(labels) else ""
    if label and label != default_channel_label(channel):
        return f"Channel {channel} ({label})"
    return f"Channel {channel}"


def silent_channel_message(channel: int, labels: Optional[Sequence[str]] = None) -> str:
    """Warning for a channel that is silent while another one carries a signal."""
    return f"{channel_name(channel, labels)} is silent!"
