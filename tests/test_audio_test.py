"""Tests for recorder.audio.audio_test: the in-memory test recording and its playback."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd

from recorder.audio.audio_test import (AudioPlayer, AudioTestSession,
                                       fit_channels, normalize_for_playback,
                                       resample_linear, summarize_test)
from recorder.audio.devices import active_audio_stream_count
from recorder.audio.lsl_audio import AudioStreamSettings
from recorder.audio.rate_meter import RateMeter


def _tone(frames=1000, channels=1, level=0.1, rate=1000):
    t = np.arange(frames) / rate
    return np.column_stack([level * np.sin(2 * np.pi * 5 * t)] * channels).astype(np.float32)


def _summary(rate=44100.0, overflows=0, window=4.0):
    return {"measured_srate": rate, "measured_window_s": window, "input_overflows": overflows}


# ---------------------------------------------------------------------------
# summarize_test
# ---------------------------------------------------------------------------

def test_healthy_recording_has_no_warnings():
    result = summarize_test(_tone(), 44100, 44100, _summary(44080.0), "Mic")
    assert result.ok and result.warnings == []
    assert result.measured_rate == 44080.0
    assert result.device_name == "Mic"


def test_device_running_faster_than_its_label_is_reported():
    """The Nov 3 failure: about 48 kHz arriving on a stream opened at 44.1 kHz."""
    result = summarize_test(_tone(), 44100, 44100, _summary(47940.0))
    assert not result.ok
    assert "47940" in result.warnings[0] and "off from the configured" in result.warnings[0]


def test_device_running_slower_than_its_label_plays_fast():
    result = summarize_test(_tone(), 48000, 48000, _summary(44100.0))
    assert "Samples arrived at 44100 Hz" in result.warnings[0]


def test_rate_the_device_would_not_accept_is_reported():
    result = summarize_test(_tone(), 44100, 48000, _summary(48000.0))
    assert any("did not accept 44100 Hz" in w and "48000" in w for w in result.warnings)


def test_unmeasurable_rate_is_not_a_warning():
    result = summarize_test(_tone(), 44100, 44100, _summary(None, window=0.0))
    assert result.ok and result.measured_rate is None


def test_signal_problems_are_reported():
    assert "silent" in summarize_test(np.zeros((1000, 1), np.float32), 1000, 1000, _summary(1000.0)).warnings[0]
    assert "very quiet" in summarize_test(_tone(level=0.003), 1000, 1000, _summary(1000.0)).warnings[0]
    assert "clipping" in summarize_test(_tone(level=1.0), 1000, 1000, _summary(1000.0)).warnings[0]


def test_silent_channel_is_reported_with_a_hint_about_the_input():
    samples = np.column_stack([_tone()[:, 0], np.zeros(1000, np.float32)])
    result = summarize_test(samples, 1000, 1000, _summary(1000.0))
    assert any("Channel 2 is silent" in w and "different input" in w for w in result.warnings)


def test_overflows_and_empty_recordings_are_reported():
    assert any("2 input overflow" in w for w in summarize_test(_tone(), 1000, 1000, _summary(1000.0, 2)).warnings)
    empty = summarize_test(np.zeros((0, 1), np.float32), 1000, 1000, _summary(None))
    assert empty.warnings == ["No samples were recorded."] and empty.duration_s == 0.0


# ---------------------------------------------------------------------------
# Playback preparation
# ---------------------------------------------------------------------------

def test_normalize_raises_a_quiet_signal_to_minus_3_dbfs():
    quiet = _tone(level=0.01)
    louder, gain_db = normalize_for_playback(quiet)
    assert np.max(np.abs(louder)) == pytest.approx(10 ** (-3 / 20), rel=1e-3)
    assert gain_db == pytest.approx(20 * np.log10(10 ** (-3 / 20) / 0.01), abs=0.01)


def test_normalize_never_attenuates_and_leaves_silence_alone():
    loud = _tone(level=0.9)
    out, gain = normalize_for_playback(loud)
    assert gain == 0.0 and out is loud
    silence = np.zeros((100, 1), np.float32)
    out, gain = normalize_for_playback(silence)
    assert gain == 0.0 and not out.any()


def test_normalize_caps_the_gain():
    _, gain_db = normalize_for_playback(_tone(level=1e-3 * 0.4))  # about -68 dBFS needs +65 dB
    assert gain_db == 40.0


def test_fit_channels():
    mono = _tone(channels=1)
    assert fit_channels(mono, 2).shape == (1000, 2)
    assert np.array_equal(fit_channels(mono, 2)[:, 0], fit_channels(mono, 2)[:, 1])
    assert fit_channels(_tone(channels=6), 2).shape == (1000, 2)
    assert fit_channels(_tone(channels=2), 1).shape == (1000, 1)
    assert fit_channels(_tone(channels=2), 8).shape == (1000, 2)


def test_resample_linear_changes_length_and_keeps_the_signal():
    ramp = np.linspace(0, 1, 1000, dtype=np.float32).reshape(-1, 1)
    out = resample_linear(ramp, 1000, 2000)
    assert len(out) == 2000 and out.dtype == np.float32
    assert out[0, 0] == 0.0 and out[-1, 0] == pytest.approx(1.0, abs=1e-3)
    assert np.all(np.diff(out[:, 0]) >= 0)
    assert resample_linear(ramp, 1000, 1000) is ramp


# ---------------------------------------------------------------------------
# AudioTestSession
# ---------------------------------------------------------------------------

class _CapturingInputStream:
    """Stand-in for sd.InputStream that lets a test deliver audio by calling the callback."""
    instances = []

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.channels = kwargs["channels"]
        self.stopped = False
        _CapturingInputStream.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def close(self):
        pass

    def deliver(self, frames, level=0.1, rate=1000):
        indata = (level * np.sin(np.arange(frames) * 0.1)).astype(np.float32)
        indata = np.repeat(indata.reshape(-1, 1), self.channels, axis=1)
        self.callback(indata, frames, SimpleNamespace(currentTime=0.0, inputBufferAdcTime=0.0), None)


@pytest.fixture
def session_env(fake_sd, monkeypatch):
    fake_sd.add_hostapi("ALSA")
    idx = fake_sd.add_device("Mic", max_input_channels=2, default_samplerate=1000, supported_rates=[1000])
    _CapturingInputStream.instances = []
    monkeypatch.setattr(sd, "InputStream", _CapturingInputStream, raising=False)
    return idx


def _session(idx, duration_s=2.0, **kw):
    settings = AudioStreamSettings(device=idx, samplerate=1000, channels=2, bitdepth=32, **kw)
    return AudioTestSession(settings, duration_s=duration_s)


def test_session_records_in_memory_and_reports_progress(session_env):
    session = _session(session_env)
    session.start()
    stream = _CapturingInputStream.instances[0]
    assert session.recording and active_audio_stream_count() == 1

    stream.deliver(500)
    assert session.elapsed_s == pytest.approx(0.5) and not session.reached_duration
    stream.deliver(1500)
    assert session.reached_duration


def test_session_stop_returns_exactly_the_requested_duration(session_env):
    session = _session(session_env, duration_s=2.0)
    session.start()
    stream = _CapturingInputStream.instances[0]
    for _ in range(5):
        stream.deliver(500)  # 2.5 s delivered

    result = session.stop()

    assert result.samples.shape == (2000, 2) and result.samples.dtype == np.float32
    assert result.configured_rate == 1000 and result.samplerate == 1000
    assert stream.stopped and active_audio_stream_count() == 0
    session.stop()  # a second stop is harmless
    assert active_audio_stream_count() == 0


def test_session_uses_the_rate_meter_for_the_measured_rate(session_env):
    session = _session(session_env)
    session.start()
    clock = SimpleNamespace(t=0.0)
    session._streamer.rate_meter = RateMeter(clock=lambda: clock.t)
    stream = _CapturingInputStream.instances[0]
    for _ in range(40):  # 40 chunks of 100 frames, one every 0.1 s = 1000 Hz
        clock.t += 0.1
        stream.deliver(100)

    result = session.stop()

    assert result.measured_rate == pytest.approx(1000.0, rel=0.01)
    assert not any("arrived at" in w for w in result.warnings)


def test_session_flags_a_device_delivering_faster_than_its_label(session_env):
    session = _session(session_env, duration_s=3.0)
    session.start()
    clock = SimpleNamespace(t=0.0)
    session._streamer.rate_meter = RateMeter(clock=lambda: clock.t)
    stream = _CapturingInputStream.instances[0]
    for _ in range(60):  # 100 frames every 0.0625 s = 1600 Hz, on a stream labelled 1000 Hz
        clock.t += 0.0625
        stream.deliver(100)

    result = session.stop()

    assert any("Samples arrived at" in w and "off from the configured" in w for w in result.warnings)


def test_level_readings_report_the_peak_since_the_last_read(session_env):
    session = _session(session_env)
    session.start()
    stream = _CapturingInputStream.instances[0]
    stream.deliver(100, level=0.5)
    first = session.read_levels()
    assert first.peak_dbfs == pytest.approx(-6.0, abs=0.5) and not first.clipped

    stream.deliver(100, level=0.05)
    second = session.read_levels()
    assert second.peak_dbfs == pytest.approx(-26.0, abs=0.5)
    assert second.peak_hold_dbfs == pytest.approx(first.peak_dbfs, abs=0.01)

    stream.deliver(100, level=1.0)
    assert session.read_levels().clipped


def test_cancel_releases_the_device_without_a_result(session_env):
    session = _session(session_env)
    session.start()
    session.cancel()
    assert not session.recording and active_audio_stream_count() == 0


def test_session_start_failure_leaves_nothing_open(fake_sd, monkeypatch):
    idx = fake_sd.add_device("Mic", max_input_channels=1, default_samplerate=1000, supported_rates=[500])
    session = AudioTestSession(AudioStreamSettings(device=idx, samplerate=1000, channels=1))
    with pytest.raises(RuntimeError):
        session.start()
    assert not session.recording and active_audio_stream_count() == 0


# ---------------------------------------------------------------------------
# AudioPlayer
# ---------------------------------------------------------------------------

class _FakeOutputStream:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.finished_callback = kwargs["finished_callback"]
        self.stopped = self.closed = False
        _FakeOutputStream.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


@pytest.fixture
def player_env(fake_sd, monkeypatch):
    fake_sd.add_hostapi("ALSA")
    idx = fake_sd.add_device(
        "Speakers", max_output_channels=2, default_samplerate=48000, supported_rates=[48000, 44100],
    )
    fake_sd.set_default_output(idx)
    _FakeOutputStream.instances = []
    monkeypatch.setattr(sd, "OutputStream", _FakeOutputStream, raising=False)
    return idx


def _drain(stream, frames_per_call=100):
    """Run the playback callback like PortAudio would; returns the frames it produced."""
    out = []
    while True:
        buf = np.zeros((frames_per_call, stream.kwargs["channels"]), dtype=np.float32)
        try:
            stream.callback(buf, frames_per_call, None, None)
        except sd.CallbackStop:
            out.append(buf)
            return np.concatenate(out)
        out.append(buf)


def test_playback_plays_mono_on_both_channels_at_the_recorded_rate(player_env):
    player = AudioPlayer()
    samples = _tone(frames=250, channels=1)
    info = player.play(samples, 44100)

    stream = _FakeOutputStream.instances[0]
    assert (info.samplerate, info.channels, info.resampled) == (44100, 2, False)
    assert active_audio_stream_count() == 1
    played = _drain(stream)
    assert np.allclose(played[:250, 0], samples[:, 0]) and np.allclose(played[:250, 1], samples[:, 0])
    assert not played[250:].any()  # padded with silence after the end


def test_playback_resamples_when_the_output_device_rejects_the_rate(player_env):
    player = AudioPlayer()
    info = player.play(_tone(frames=1000), 22050)  # the output only does 44100 or 48000

    assert info.resampled and info.samplerate == 48000
    assert _FakeOutputStream.instances[0].kwargs["samplerate"] == 48000


def test_playback_can_normalize_a_quiet_recording(player_env):
    player = AudioPlayer()
    info = player.play(_tone(frames=250, level=0.01), 44100, normalize=True)
    assert info.gain_db > 20
    played = _drain(_FakeOutputStream.instances[0])
    assert np.max(np.abs(played)) == pytest.approx(10 ** (-3 / 20), rel=1e-2)


def test_poll_releases_the_device_after_playback_finishes(player_env):
    player = AudioPlayer()
    player.play(_tone(frames=100), 44100)
    stream = _FakeOutputStream.instances[0]
    assert player.poll() is True and player.playing  # not finished yet

    stream.finished_callback()  # PortAudio reports the end of the stream
    assert player.poll() is False and not player.playing
    assert stream.stopped and stream.closed and active_audio_stream_count() == 0


def test_stop_ends_playback_and_releases_the_device(player_env):
    player = AudioPlayer()
    player.play(_tone(frames=10000), 44100)
    stream = _FakeOutputStream.instances[0]
    player.stop()
    assert not player.playing and stream.closed and active_audio_stream_count() == 0
    with pytest.raises(sd.CallbackStop):
        stream.callback(np.zeros((10, 2), np.float32), 10, None, None)


def test_starting_a_new_playback_stops_the_previous_one(player_env):
    player = AudioPlayer()
    player.play(_tone(), 44100)
    player.play(_tone(), 44100)
    assert _FakeOutputStream.instances[0].closed and active_audio_stream_count() == 1
