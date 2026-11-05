"""Record a few seconds from the selected audio device with configured settings and play the recording back."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
import sounddevice as sd

from .constants import RATE_WARN_TOLERANCE
from .devices import (audio_stream_closed, audio_stream_opened,
                      default_output_device_index)
from .levels import (SignalLevels, as_float_samples, dbfs, level_problem,
                     signal_levels, silent_channel_message, silent_channels)
from .lsl_audio import AudioLSLStreamer, AudioStreamSettings
from .rate_meter import rate_deviation

# Playback is normalized to this peak level (dBFS), with the gain capped
NORMALIZED_PEAK_DBFS = -3.0
MAX_NORMALIZE_GAIN_DB = 40.0
# Below this peak there is nothing but noise to amplify
MIN_NORMALIZABLE_PEAK_DBFS = -80.0


# ---------------------------------------------------------------------------
# Result of a test recording
# ---------------------------------------------------------------------------

@dataclass
class AudioTestResult:
    samples: np.ndarray  # float32, shape (frames, channels), in [-1, 1]
    samplerate: int  # the rate the stream was opened at
    configured_rate: int
    measured_rate: Optional[float]  # frames per second of real time; None if too short to tell
    measured_window_s: float
    levels: SignalLevels
    input_overflows: int
    device_name: Optional[str]
    warnings: List[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.samplerate) if self.samplerate else 0.0

    @property
    def ok(self) -> bool:
        return not self.warnings


def summarize_test(
    samples: np.ndarray,
    configured_rate: int,
    opened_rate: int,
    meter_summary: dict,
    device_name: Optional[str] = None,
    channel_labels: Optional[Sequence[str]] = None,
) -> AudioTestResult:
    """Turn a finished test recording into a result with plain-language warnings."""
    levels = signal_levels(samples)
    measured = meter_summary.get("measured_srate")
    overflows = int(meter_summary.get("input_overflows") or 0)
    warnings: List[str] = []

    if opened_rate != configured_rate:
        warnings.append(
            f"The device did not accept {configured_rate} Hz; it was opened at {opened_rate} Hz instead."
        )
    deviation = rate_deviation(measured, opened_rate)
    if deviation is not None and deviation > RATE_WARN_TOLERANCE:
        warnings.append(
            f"Samples arrived at {measured:.0f} Hz, {deviation:.1%} off from the configured {opened_rate} Hz."
        )
    if len(samples):
        problem = level_problem(levels)
        if problem:
            warnings.append(f"Peak {levels.peak_dbfs:.1f} dBFS: {problem}.")
        for channel in silent_channels(levels):
            warnings.append(silent_channel_message(channel, channel_labels))
    else:
        warnings.append("No samples were recorded.")
    if overflows:
        warnings.append(f"{overflows} input overflow(s) reported by the driver; samples may have been lost.")

    return AudioTestResult(
        samples=samples,
        samplerate=opened_rate,
        configured_rate=configured_rate,
        measured_rate=measured,
        measured_window_s=float(meter_summary.get("measured_window_s") or 0.0),
        levels=levels,
        input_overflows=overflows,
        device_name=device_name,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@dataclass
class LevelReading:
    """Levels since the last read (for a live meter)."""
    peak_dbfs: float  # loudest sample since the previous reading
    rms_dbfs: float  # of the latest chunk
    peak_hold_dbfs: float  # loudest sample so far
    clipped: bool  # a sample reached full scale at any point so far


class AudioTestSession:
    """A short recording kept in memory. Thread-safe between the audio callback
    and the GUI thread that polls it."""

    def __init__(
        self,
        settings: AudioStreamSettings,
        duration_s: float = 5.0,
        status_cb: Optional[Callable[[str, str], None]] = None,
    ):
        self.settings = settings
        self.duration_s = float(duration_s)
        self.status_cb = status_cb
        # AudioLSLStreamer.start() replaces settings.samplerate by the rate the device
        # accepted, so the rate that was asked for is remembered here
        self._requested_rate = int(settings.samplerate)
        self._streamer: Optional[AudioLSLStreamer] = None
        self._lock = threading.Lock()
        self._chunks: List[np.ndarray] = []
        self._frames = 0
        self._peak_since_read = 0.0
        self._latest_rms = 0.0
        self._peak_hold = 0.0
        self._clipped = False
        self.recording = False

    def start(self) -> None:
        """Open the device and start recording. Raises if it cannot be opened."""
        self._streamer = AudioLSLStreamer(self.settings, sample_cb=self._on_samples, status_cb=self.status_cb)
        self._streamer.start()
        self.recording = True

    def _on_samples(self, _timestamps, x) -> None:
        f = as_float_samples(x, self.settings.sample_format).astype(np.float32)
        levels = signal_levels(f)
        with self._lock:
            self._chunks.append(f)
            self._frames += len(f)
            self._peak_since_read = max(self._peak_since_read, levels.peak)
            self._latest_rms = levels.rms
            self._peak_hold = max(self._peak_hold, levels.peak)
            self._clipped = self._clipped or levels.peak >= 0.999

    @property
    def elapsed_s(self) -> float:
        with self._lock:
            frames = self._frames
        return frames / float(self.settings.samplerate) if self.settings.samplerate else 0.0

    @property
    def reached_duration(self) -> bool:
        return self.elapsed_s >= self.duration_s

    def read_levels(self) -> LevelReading:
        with self._lock:
            reading = LevelReading(
                peak_dbfs=dbfs(self._peak_since_read),
                rms_dbfs=dbfs(self._latest_rms),
                peak_hold_dbfs=dbfs(self._peak_hold),
                clipped=self._clipped,
            )
            self._peak_since_read = 0.0
        return reading

    def stop(self) -> AudioTestResult:
        """Stop recording and summarize it (recording at most `duration_s`)."""
        self.cancel()
        with self._lock:
            chunks = list(self._chunks)
        channels = max(int(self.settings.channels or 1), 1)
        samples = np.concatenate(chunks) if chunks else np.zeros((0, channels), dtype=np.float32)
        samples = samples[: int(round(self.duration_s * self.settings.samplerate))]
        meter = self._streamer.rate_meter.summary() if self._streamer else {}
        return summarize_test(
            samples,
            configured_rate=self._requested_rate,
            opened_rate=int(self.settings.samplerate),
            meter_summary=meter,
            device_name=self.settings.device_name,
            channel_labels=self.settings.channel_labels,
        )

    def cancel(self) -> None:
        """Stop the stream and release the device (idempotent)."""
        if self.recording and self._streamer:
            self.recording = False
            self._streamer.stop()


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def normalize_for_playback(samples: np.ndarray) -> Tuple[np.ndarray, float]:
    """Scale samples so their peak reaches NORMALIZED_PEAK_DBFS (never attenuating, and with
    the gain capped). Returns (samples, gain in dB). Playback only: the recording is untouched."""
    peak_db = dbfs(float(np.max(np.abs(samples)))) if samples.size else MIN_NORMALIZABLE_PEAK_DBFS
    if peak_db < MIN_NORMALIZABLE_PEAK_DBFS or peak_db >= NORMALIZED_PEAK_DBFS:
        return samples, 0.0
    gain_db = min(NORMALIZED_PEAK_DBFS - peak_db, MAX_NORMALIZE_GAIN_DB)
    return (samples * (10.0 ** (gain_db / 20.0))).astype(np.float32), gain_db


def fit_channels(samples: np.ndarray, max_output_channels: int) -> np.ndarray:
    """Adapt recorded channels to what an output device can play: mono is played on both
    channels of a stereo device; more than two channels are reduced to their first two;
    stereo goes to a mono-only device as a mix."""
    x = samples if samples.ndim == 2 else samples.reshape(-1, 1)
    channels = x.shape[1]
    if max_output_channels >= 2:
        if channels == 1:
            return np.repeat(x, 2, axis=1)
        return np.ascontiguousarray(x[:, :2])
    return np.mean(x[:, :2], axis=1, keepdims=True).astype(np.float32)


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Linear-interpolation resampling (adequate for listening checks)."""
    if source_rate == target_rate or len(samples) == 0:
        return samples
    n_out = max(int(round(len(samples) * target_rate / float(source_rate))), 1)
    x_old = np.arange(len(samples), dtype=np.float64) / source_rate
    x_new = np.arange(n_out, dtype=np.float64) / target_rate
    return np.stack(
        [np.interp(x_new, x_old, samples[:, c]) for c in range(samples.shape[1])], axis=1,
    ).astype(np.float32)


@dataclass
class PlaybackInfo:
    device_name: str
    samplerate: int  # rate played at (differs from the recording's if the device needed resampling)
    channels: int
    gain_db: float
    resampled: bool


class AudioPlayer:
    """Plays a recorded test through an output device (non-blocking).

    The GUI polls poll() to learn that playback finished and the device was released."""

    def __init__(self, status_cb: Optional[Callable[[str, str], None]] = None):
        self.status_cb = status_cb
        self._stream = None
        self._counted = False
        self._finished = threading.Event()
        self._stop_requested = False

    @property
    def playing(self) -> bool:
        return self._stream is not None

    def play(
        self,
        samples: np.ndarray,
        samplerate: int,
        output_device: Optional[int] = None,
        normalize: bool = False,
    ) -> PlaybackInfo:
        if self._stream is not None:
            self.stop()
        device = output_device if output_device is not None else default_output_device_index()
        info = sd.query_devices(device) if device is not None else sd.query_devices(kind="output")
        data = samples.reshape(-1, 1) if samples.ndim == 1 else samples
        gain_db = 0.0
        if normalize:
            data, gain_db = normalize_for_playback(data)
        data = fit_channels(data, int(info.get("max_output_channels") or 2)).astype(np.float32)

        rate = int(samplerate)
        resampled = False
        try:
            sd.check_output_settings(device=device, samplerate=rate, channels=data.shape[1], dtype="float32")
        except Exception:
            native = int(round(info.get("default_samplerate") or rate))
            data = resample_linear(data, rate, native)
            rate, resampled = native, True

        position = [0]
        self._finished.clear()
        self._stop_requested = False

        def callback(outdata, frames, _time, _status):
            if self._stop_requested:
                raise sd.CallbackStop
            chunk = data[position[0]: position[0] + frames]
            outdata[: len(chunk)] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):] = 0
                raise sd.CallbackStop
            position[0] += frames

        self._stream = sd.OutputStream(
            samplerate=rate, device=device, channels=data.shape[1], dtype="float32",
            callback=callback, finished_callback=self._finished.set,
        )
        self._stream.start()
        audio_stream_opened()
        self._counted = True
        return PlaybackInfo(
            device_name=str(info.get("name")), samplerate=rate, channels=data.shape[1],
            gain_db=gain_db, resampled=resampled,
        )

    def poll(self) -> bool:
        """Release the device once playback has finished. Returns True while still playing."""
        if self._stream is not None and self._finished.is_set():
            self._release()
        return self._stream is not None

    def stop(self) -> None:
        self._stop_requested = True
        self._release()

    def _release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                if self.status_cb:
                    self.status_cb(f"Audio playback: error releasing the output device: {exc}", "WARNING")
        if self._counted:
            audio_stream_closed()
            self._counted = False
