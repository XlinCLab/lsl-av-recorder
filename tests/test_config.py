"""Tests for cfg-file parsing (recorder.config.load_cfg)."""

from __future__ import annotations

import configparser

from recorder.audio.constants import DEFAULT_SAMPLING_RATE
from recorder.config import (AppConfig, AppPrompts, AudioConfig,
                             LabRecorderConfig, OutputConfig, VideoCamConfig,
                             VideoConfig, _get_bool, load_cfg, save_cfg)
from recorder.xdf.xdf_writer import (FULL_BUFFER_BLOCK_THREAD_POLICY,
                                     FULL_BUFFER_DEFAULT_POLICY,
                                     FULL_BUFFER_POLICIES)

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
    """The bare 'drop' alias resolves to the default drop policy."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Buffering]
            WriterDropPolicy = drop
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == FULL_BUFFER_DEFAULT_POLICY


def test_writer_drop_policy_accepts_known_values(write_cfg):
    """Each explicit policy value is accepted and stored unchanged."""
    for policy in FULL_BUFFER_POLICIES:
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
    assert cfg.Buffering.WriterDropPolicy == FULL_BUFFER_BLOCK_THREAD_POLICY


def test_writer_drop_policy_invalid_falls_back_to_default(write_cfg):
    """An unrecognized policy falls back to the default."""
    cfg = load_cfg(
        path=write_cfg(
            """
            [Buffering]
            WriterDropPolicy = nonsense
            """
        )
    )
    assert cfg.Buffering.WriterDropPolicy == FULL_BUFFER_DEFAULT_POLICY


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


# ---------------------------------------------------------------------------
# save_cfg
# ---------------------------------------------------------------------------

def test_save_cfg_round_trips_session_output_audio_labrecorder(tmp_path):
    """Every scalar field across Session/Output/Audio/LabRecorder survives a
    save then load unchanged."""
    cfg = AppConfig()
    cfg.Prompts.ExperimentName = "MyStudy"
    cfg.Prompts.Subject = "S02"
    cfg.Prompts.Session = "2"
    cfg.Prompts.Block = "reading"
    cfg.Prompts.Acquisition = "default"
    cfg.Prompts.Run = "03"
    cfg.Output.StudyRoot = "/data/study"
    cfg.Output.PathTemplate = "sub-%p/rec"
    cfg.Audio.Enabled = True
    cfg.Audio.Device = "USB Mic"
    cfg.Audio.SampleRate = 44100
    cfg.Audio.BitDepth = 24
    cfg.Audio.Channels = 2
    cfg.Audio.StreamName = "Mic"
    cfg.LabRecorder.Enabled = True
    cfg.LabRecorder.Host = "192.168.1.5"
    cfg.LabRecorder.Port = 9999

    path = str(tmp_path / "out.cfg")
    save_cfg(cfg, path)
    reloaded = load_cfg(path)

    assert reloaded.Prompts == cfg.Prompts
    assert reloaded.Output == cfg.Output
    assert reloaded.Audio == cfg.Audio
    assert reloaded.LabRecorder == cfg.LabRecorder


def test_save_cfg_empty_audio_device_round_trips_to_none(tmp_path):
    """A None Device (auto-select) is written as blank and reloads to None,
    matching load_cfg's own blank-Device convention."""
    cfg = AppConfig()
    cfg.Audio.Device = None

    path = str(tmp_path / "out.cfg")
    save_cfg(cfg, path)
    assert load_cfg(path).Audio.Device is None


def test_save_cfg_omits_none_camera_fields_without_corrupting_reload(tmp_path):
    """DeviceName/Brightness/Hue/Saturation left at None (e.g. a camera
    whose controls were never applied) must not be written as the literal
    string "None". Omitting the key entirely lets load_cfg's own
    fallback-to-None apply, exactly as if the key had never been set."""
    cfg = AppConfig()
    cam = VideoCamConfig()
    cam.Enabled = True
    cam.DeviceName = None
    cam.Brightness = None
    cam.Hue = None
    cam.Saturation = None
    cfg.Video.Cams = [cam]
    cfg.Video.MaxCams = 1

    path = str(tmp_path / "out.cfg")
    save_cfg(cfg, path)
    reloaded = load_cfg(path)

    assert len(reloaded.Video.Cams) == 1
    reloaded_cam = reloaded.Video.Cams[0]
    assert reloaded_cam.DeviceName is None
    assert reloaded_cam.Brightness is None
    assert reloaded_cam.Hue is None
    assert reloaded_cam.Saturation is None


def test_save_cfg_round_trips_camera_with_all_fields_set(tmp_path):
    """A fully-configured camera (every optional field populated) survives
    a save/reload unchanged."""
    cfg = AppConfig()
    cam = VideoCamConfig()
    cam.Enabled = True
    cam.DeviceIndex = 1
    cam.DevNode = "1"
    cam.DeviceName = "FaceTime HD Camera"
    cam.Label = "Cam2"
    cam.FPS = 15
    cam.Width = 1280
    cam.Height = 720
    cam.AutoExposure = True
    cam.AutoFocus = False
    cam.Brightness = 128
    cam.Hue = 0
    cam.Saturation = 100
    cam.PixelFormat = "YUYV"
    cfg.Video.Cams = [cam]
    cfg.Video.MaxCams = 1

    path = str(tmp_path / "out.cfg")
    save_cfg(cfg, path)
    reloaded_cam = load_cfg(path).Video.Cams[0]

    assert reloaded_cam == cam


def test_save_cfg_bumps_maxcams_to_fit_actual_camera_count(tmp_path):
    """load_cfg only scans VideoCam sections up to Video.MaxCams.
    If MaxCams is stale (e.g. lower than the number of cameras actually
    configured), save_cfg must widen it so every camera is still found on
    reload, not silently dropped."""
    cfg = AppConfig()
    cfg.Video.MaxCams = 1
    cfg.Video.Cams = [VideoCamConfig(Label="Cam1"), VideoCamConfig(Label="Cam2")]

    path = str(tmp_path / "out.cfg")
    save_cfg(cfg, path)
    reloaded = load_cfg(path)

    assert reloaded.Video.MaxCams >= 2
    assert [c.Label for c in reloaded.Video.Cams] == ["Cam1", "Cam2"]


def test_save_cfg_no_cameras_writes_no_camera_sections(tmp_path):
    """An empty camera list produces no VideoCam sections, and reloading
    yields an empty camera list back."""
    cfg = AppConfig()
    cfg.Video.Cams = []

    path = str(tmp_path / "out.cfg")
    save_cfg(cfg, path)
    assert load_cfg(path).Video.Cams == []
