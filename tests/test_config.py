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
    a = AppConfig()
    b = AppConfig()
    # default_factory must give each AppConfig its own nested dataclasses/lists
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
    cfg = load_cfg(str(tmp_path / "does_not_exist.cfg"))
    assert cfg.Audio.SampleRate == DEFAULT_SAMPLING_RATE
    assert cfg.Audio.Enabled is False
    assert cfg.Video.Cams == []
    assert cfg.Prompts.Subject == ""
    assert cfg.Output.StudyRoot == "./recordings"


def test_load_cfg_empty_file_returns_defaults(write_cfg):
    cfg = load_cfg(write_cfg(""))
    assert cfg.Audio.SampleRate == DEFAULT_SAMPLING_RATE
    assert cfg.Audio.Enabled is False
    assert cfg.Video.Cams == []
    assert cfg.Prompts.Subject == ""
    assert cfg.Output.StudyRoot == "./recordings"


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

def test_load_cfg_session_and_output(write_cfg):
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
    cfg = load_cfg(path)
    assert cfg.Prompts.Subject == "S01"
    assert cfg.Prompts.Session == "2"
    assert cfg.Prompts.Block == "reading"
    assert cfg.Output.StudyRoot == "/data/study"
    assert cfg.Output.PathTemplate == "sub-%p/rec"


def test_load_cfg_audio_enabled_and_types(write_cfg):
    cfg = load_cfg(
        write_cfg(
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
    cfg = load_cfg(
        write_cfg(
            """
            [Audio]
            Device =
            """
        )
    )
    assert cfg.Audio.Device is None


def test_load_cfg_audio_device_preserved_when_set(write_cfg):
    cfg = load_cfg(
        write_cfg(
            """
            [Audio]
            Device = USB Mic
            """
        )
    )
    assert cfg.Audio.Device == "USB Mic"


def test_load_cfg_labrecorder_blank_host_falls_back_to_default(write_cfg):
    cfg = load_cfg(
        write_cfg(
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
    cfg = load_cfg(
        write_cfg(
            """
            [Audio]
            BufferSeconds = 1.5
            """
        )
    )
    assert cfg.Buffering.AudioBufferSeconds == 1.5


def test_deprecated_video_buffer_frames_migrates(write_cfg):
    cfg = load_cfg(
        write_cfg(
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
    cfg = load_cfg(
        write_cfg(
            """
            [Buffering]
            WriterDropPolicy = drop
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == "drop_newest"


def test_writer_drop_policy_accepts_known_values(write_cfg):
    for policy in ("drop_oldest", "drop_newest", "block"):
        cfg = load_cfg(
            write_cfg(
                f"""
                [Buffering]
                WriterDropPolicy = {policy}
                """
            )
        )
        assert cfg.Buffering.WriterDropPolicy == policy


def test_writer_drop_policy_is_case_insensitive(write_cfg):
    cfg = load_cfg(
        write_cfg(
            """
            [Buffering]
            WriterDropPolicy = BLOCK
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == "block"


def test_writer_drop_policy_invalid_falls_back_to_default(write_cfg):
    cfg = load_cfg(
        write_cfg(
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
    cfg = load_cfg(
        write_cfg(
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
    cfg = load_cfg(
        write_cfg(
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
    cfg = load_cfg(
        write_cfg(
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
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string("[S]\nyes_key = on\nno_key = off\n")
    assert _get_bool(cp, "S", "yes_key") is True
    assert _get_bool(cp, "S", "no_key") is False
    cp.read_string("[S]\nyes_key = true\nno_key = false\n")
    assert _get_bool(cp, "S", "yes_key") is True
    assert _get_bool(cp, "S", "no_key") is False
    cp.read_string("[S]\nyes_key = yes\nno_key = no\n")
    assert _get_bool(cp, "S", "yes_key") is True
    assert _get_bool(cp, "S", "no_key") is False


def test_get_bool_falls_back_on_missing_or_invalid(write_cfg):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string("[S]\nbad = maybe\n")
    # Missing key -> default; unparseable value -> default
    assert _get_bool(cp, "S", "absent", default=True) is True
    assert _get_bool(cp, "S", "bad", default=False) is False
