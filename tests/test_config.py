"""Tests for cfg-file parsing (recorder.config.load_cfg)."""

from __future__ import annotations

import configparser

from recorder.audio.constants import DEFAULT_SAMPLING_RATE
from recorder.config import (AppConfig, AppPrompts, AudioConfig,
                             LabRecorderConfig, OutputConfig, VideoConfig,
                             _get_bool, load_cfg)

# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------

def test_appconfig_defaults_are_independent_instances():
    """Each AppConfig gets its own nested dataclasses/lists (via default_factory),
    so mutating one instance never leaks into another."""
    a = AppConfig()
    b = AppConfig()
    assert a.Prompts is not b.Prompts
    assert a.Video.Cams is not b.Video.Cams
    assert isinstance(a.Prompts, AppPrompts)
    assert isinstance(a.Output, OutputConfig)
    assert isinstance(a.Audio, AudioConfig)
    assert isinstance(a.LabRecorder, LabRecorderConfig)
    assert isinstance(a.Video, VideoConfig)


# ---------------------------------------------------------------------------
# Missing file / empty file -> all defaults
# ---------------------------------------------------------------------------

def test_load_cfg_missing_file_returns_defaults(tmp_path):
    """A path that doesn't exist yields an all-defaults config rather than raising an error."""
    cfg = load_cfg(path=str(tmp_path / "does_not_exist.cfg"))
    assert cfg.Audio.SampleRate == DEFAULT_SAMPLING_RATE
    assert cfg.Audio.Enabled is False
    assert cfg.Video.Cams == []
    assert cfg.Prompts.Subject == ""
    assert cfg.Output.StudyRoot == "./recordings"


def test_load_cfg_empty_file_returns_defaults(write_cfg):
    """An empty (but present) cfg file also yields all defaults."""
    cfg = load_cfg(path=write_cfg(""))
    assert cfg.Audio.SampleRate == DEFAULT_SAMPLING_RATE
    assert cfg.Audio.Enabled is False
    assert cfg.Video.Cams == []
    assert cfg.Prompts.Subject == ""
    assert cfg.Output.StudyRoot == "./recordings"


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

def test_load_cfg_session_and_output(write_cfg):
    """The [Session] and [Output] sections populate the Prompts and Output dataclasses."""
    path = write_cfg(
        """
        [Session]
        Subject = S01
        Session = 2
        Block = reading

        [Output]
        StudyRoot = /data/study
        PathTemplate = sub-%p/rec
        """
    )
    cfg = load_cfg(path=path)
    assert cfg.Prompts.Subject == "S01"
    assert cfg.Prompts.Session == "2"
    assert cfg.Prompts.Block == "reading"
    assert cfg.Output.StudyRoot == "/data/study"
    assert cfg.Output.PathTemplate == "sub-%p/rec"


def test_load_cfg_audio_enabled_and_types(write_cfg):
    """[Audio] values are coerced to their declared types
    (bool Enabled, int SampleRate/Channels, str StreamName)."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Audio]
            Enabled = yes
            SampleRate = 44100
            Channels = 2
            StreamName = Mic
            """
        )
    )
    assert cfg.Audio.Enabled is True
    assert cfg.Audio.SampleRate == 44100
    assert cfg.Audio.Channels == 2
    assert cfg.Audio.StreamName == "Mic"


def test_load_cfg_audio_empty_device_becomes_none(write_cfg):
    """A blank Device value normalizes to None (meaning 'auto-select')."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Audio]
            Device =
            """
        )
    )
    assert cfg.Audio.Device is None


def test_load_cfg_audio_device_preserved_when_set(write_cfg):
    """A non-blank Device value is preserved verbatim."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Audio]
            Device = USB Mic
            """
        )
    )
    assert cfg.Audio.Device == "USB Mic"


def test_load_cfg_labrecorder_blank_host_falls_back_to_default(write_cfg):
    """A blank Host falls back to the default loopback address; Enabled/Port are still parsed."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [LabRecorder]
            Enabled = true
            Host =
            Port = 9999
            """
        )
    )
    assert cfg.LabRecorder.Enabled is True
    assert cfg.LabRecorder.Host == "127.0.0.1"
    assert cfg.LabRecorder.Port == 9999


# ---------------------------------------------------------------------------
# Deprecated buffering keys per stream migrate into Buffering section
# ---------------------------------------------------------------------------

def test_deprecated_audio_buffer_seconds_migrates(write_cfg):
    """The legacy [Audio] BufferSeconds key is migrated into
    Buffering.AudioBufferSeconds so old config files keep working."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Audio]
            BufferSeconds = 1.5
            """
        )
    )
    assert cfg.Buffering.AudioBufferSeconds == 1.5


def test_deprecated_video_buffer_frames_migrates(write_cfg):
    """The legacy [Video] BufferFrames key is migrated into
    Buffering.VideoBufferFrames."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Video]
            BufferFrames = 8
            """
        )
    )
    assert cfg.Buffering.VideoBufferFrames == 8


# ---------------------------------------------------------------------------
# WriterDropPolicy normalization
# ---------------------------------------------------------------------------

def test_writer_drop_policy_alias_drop_maps_to_drop_newest(write_cfg):
    """The bare 'drop' alias resolves to the concrete 'drop_newest' policy."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Buffering]
            WriterDropPolicy = drop
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == "drop_newest"


def test_writer_drop_policy_accepts_known_values(write_cfg):
    """Each explicit policy value is accepted and stored unchanged."""
    for policy in ("drop_oldest", "drop_newest", "block"):
        cfg = load_cfg(
            path=write_cfg(
                f"""
                [Buffering]
                WriterDropPolicy = {policy}
                """
            )
        )
        assert cfg.Buffering.WriterDropPolicy == policy


def test_writer_drop_policy_is_case_insensitive(write_cfg):
    """Policy values are matched case-insensitively (BLOCK -> block)."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Buffering]
            WriterDropPolicy = BLOCK
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == "block"


def test_writer_drop_policy_invalid_falls_back_to_default(write_cfg):
    """An unrecognized policy falls back to the default (drop_oldest)."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Buffering]
            WriterDropPolicy = nonsense
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == "drop_oldest"


# ---------------------------------------------------------------------------
# Camera enumeration
# ---------------------------------------------------------------------------

def test_video_cam_sections_parsed_up_to_maxcams(write_cfg):
    """Only VideoCam sections with an index within MaxCams are parsed."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Video]
            MaxCams = 2

            [VideoCam1]
            Enabled = true
            Label = Left

            [VideoCam3]
            Enabled = true
            Label = OutOfRange
            """
        )
    )
    # VideoCam3 is beyond MaxCams=2 and must be ignored
    assert len(cfg.Video.Cams) == 1
    assert cfg.Video.Cams[0].Label == "Left"


def test_video_cam_devnode_derives_device_index(write_cfg):
    """A /dev/videoN DevNode derives the numeric DeviceIndex (video3 -> 3)."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Video]
            MaxCams = 1

            [VideoCam1]
            Enabled = true
            DevNode = /dev/video3
            """
        )
    )
    assert cfg.Video.Cams[0].DeviceIndex == 3


def test_video_cam_pixel_format_uppercased(write_cfg):
    """PixelFormat is normalized to upper case (yuyv -> YUYV)."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Video]
            MaxCams = 1

            [VideoCam1]
            Enabled = true
            PixelFormat = yuyv
            """
        )
    )
    assert cfg.Video.Cams[0].PixelFormat == "YUYV"


# ---------------------------------------------------------------------------
# _get_bool
# ---------------------------------------------------------------------------

def test_get_bool_reads_truthy_and_falsy(write_cfg):
    """_get_bool accepts every configparser truthy/falsy spelling
    (on/off, true/false, yes/no)."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string("[S]\nyes_key = on\nno_key = off\n")
    assert _get_bool(cp=cp, section="S", key="yes_key") is True
    assert _get_bool(cp=cp, section="S", key="no_key") is False
    cp.read_string("[S]\nyes_key = true\nno_key = false\n")
    assert _get_bool(cp=cp, section="S", key="yes_key") is True
    assert _get_bool(cp=cp, section="S", key="no_key") is False
    cp.read_string("[S]\nyes_key = yes\nno_key = no\n")
    assert _get_bool(cp=cp, section="S", key="yes_key") is True
    assert _get_bool(cp=cp, section="S", key="no_key") is False


def test_get_bool_falls_back_on_missing_or_invalid(write_cfg):
    """A missing key or an unparseable value both fall back to the supplied
    default instead of raising an error."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string("[S]\nbad = maybe\n")
    assert _get_bool(cp=cp, section="S", key="absent", default=True) is True
    assert _get_bool(cp=cp, section="S", key="bad", default=False) is False
