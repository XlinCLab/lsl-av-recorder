from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .audio.constants import (DEFAULT_BIT_DEPTH, DEFAULT_N_CHANNELS,
                              DEFAULT_SAMPLING_RATE)
from .video.constants import (DEFAULT_CAMERA_FPS, DEFAULT_HEIGHT,
                              DEFAULT_PIXEL_FORMAT, DEFAULT_PREVIEW_FPS,
                              DEFAULT_WIDTH, DEVNODE_PATTERN)

logger = logging.getLogger(__name__)


@dataclass
class AppPrompts:
    ExperimentName: str = ""
    Subject: str = ""
    Session: str = ""
    Block: str = ""
    Acquisition: str = ""
    Run: str = ""

@dataclass
class OutputConfig:
    StudyRoot: str = "./recordings"
    PathTemplate: str = "sub-%p/ses-%s/beh/sub-%p_ses-%s_task-%b_acq-%a_run-%r"

@dataclass
class AudioConfig:
    Enabled: bool = False
    Device: Optional[str] = None
    SampleRate: int = DEFAULT_SAMPLING_RATE
    BitDepth: int = DEFAULT_BIT_DEPTH
    Channels: int = DEFAULT_N_CHANNELS
    StreamName: str = "Audio"

@dataclass
class LabRecorderConfig:
    Enabled: bool = False
    Host: str = "127.0.0.1"
    Port: int = 22345

@dataclass
class VideoCamConfig:
    Enabled: bool = False
    DeviceIndex: int = 0
    DevNode: str = "/dev/video0"
    Label: str = "Cam"
    FPS: int = DEFAULT_CAMERA_FPS
    Width: int = DEFAULT_WIDTH
    Height: int = DEFAULT_HEIGHT
    AutoExposure: Optional[bool] = False
    Exposure: Optional[int] = None
    AutoFocus: Optional[bool] = False
    Focus: Optional[int] = None
    Brightness: Optional[int] = None
    Contrast: Optional[int] = None
    Hue: Optional[int] = None
    Saturation: Optional[int] = None
    Zoom: Optional[int] = None
    PixelFormat: str = DEFAULT_PIXEL_FORMAT

@dataclass
class VideoConfig:
    Enabled: bool = False
    MaxCams: int = 4
    Codec: str = "mp4v"
    Container: str = "mp4"
    PreviewFPS: int = DEFAULT_PREVIEW_FPS
    Cams: List[VideoCamConfig] = field(default_factory=list)

@dataclass
class BufferingConfig:
    AudioBufferSeconds: float = 0.0
    VideoBufferFrames: int = 0
    WriterQueueSize: int = 256
    WriterDropPolicy: str = "drop_oldest"  # drop_oldest | drop_newest | block

@dataclass
class AppConfig:
    Prompts: AppPrompts = field(default_factory=AppPrompts)
    Output: OutputConfig = field(default_factory=OutputConfig)
    Audio: AudioConfig = field(default_factory=AudioConfig)
    LabRecorder: LabRecorderConfig = field(default_factory=LabRecorderConfig)
    Video: VideoConfig = field(default_factory=VideoConfig)
    Buffering: BufferingConfig = field(default_factory=BufferingConfig)

def _get_bool(cp: configparser.ConfigParser, section: str, key: str, default: bool=False) -> bool:
    try:
        return cp.getboolean(section, key)
    except Exception:
        return default

def load_cfg(path: str) -> AppConfig:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")

    cfg = AppConfig()

    if cp.has_section("Session"):
        s = "Session"
        cfg.Prompts.ExperimentName = cp.get(s, "ExperimentName", fallback=cfg.Prompts.ExperimentName)
        cfg.Prompts.Subject = cp.get(s, "Subject", fallback=cfg.Prompts.Subject)
        cfg.Prompts.Session = cp.get(s, "Session", fallback=cfg.Prompts.Session)
        cfg.Prompts.Block = cp.get(s, "Block", fallback=cfg.Prompts.Block)
        cfg.Prompts.Acquisition = cp.get(s, "Acquisition", fallback=cfg.Prompts.Acquisition)
        cfg.Prompts.Run = cp.get(s, "Run", fallback=cfg.Prompts.Run)

    if cp.has_section("Output"):
        s = "Output"
        cfg.Output.StudyRoot = cp.get(s, "StudyRoot", fallback=cfg.Output.StudyRoot)
        cfg.Output.PathTemplate = cp.get(s, "PathTemplate", fallback=cfg.Output.PathTemplate)

    if cp.has_section("Audio"):
        s = "Audio"
        cfg.Audio.Enabled = _get_bool(cp, s, "Enabled", False)
        dev = cp.get(s, "Device", fallback="").strip()
        cfg.Audio.Device = dev if dev else None
        cfg.Audio.SampleRate = cp.getint(s, "SampleRate", fallback=cfg.Audio.SampleRate)
        cfg.Audio.BitDepth = cp.getint(s, "BitDepth", fallback=cfg.Audio.BitDepth)
        cfg.Audio.Channels = cp.getint(s, "Channels", fallback=cfg.Audio.Channels)
        cfg.Audio.StreamName = cp.get(s, "StreamName", fallback=cfg.Audio.StreamName)
        if cp.has_option(s, "BufferSeconds"):
            cfg.Buffering.AudioBufferSeconds = cp.getfloat(s, "BufferSeconds", fallback=cfg.Buffering.AudioBufferSeconds)
            logger.warning("Audio.BufferSeconds is deprecated; use Buffering.AudioBufferSeconds instead.")

    if cp.has_section("LabRecorder"):
        s = "LabRecorder"
        cfg.LabRecorder.Enabled = _get_bool(cp, s, "Enabled", cfg.LabRecorder.Enabled)
        cfg.LabRecorder.Host = cp.get(s, "Host", fallback=cfg.LabRecorder.Host).strip() or cfg.LabRecorder.Host
        cfg.LabRecorder.Port = cp.getint(s, "Port", fallback=cfg.LabRecorder.Port)

    if cp.has_section("Video"):
        s = "Video"
        cfg.Video.Enabled = _get_bool(cp, s, "Enabled", False)
        cfg.Video.MaxCams = cp.getint(s, "MaxCams", fallback=cfg.Video.MaxCams)
        cfg.Video.Codec = cp.get(s, "Codec", fallback=cfg.Video.Codec)
        cfg.Video.Container = cp.get(s, "Container", fallback=cfg.Video.Container)
        cfg.Video.PreviewFPS = cp.getint(s, "PreviewFPS", fallback=cfg.Video.PreviewFPS)
        if cp.has_option(s, "BufferFrames"):
            cfg.Buffering.VideoBufferFrames = cp.getint(s, "BufferFrames", fallback=cfg.Buffering.VideoBufferFrames)
            logger.warning("Video.BufferFrames is deprecated; use Buffering.VideoBufferFrames instead.")

    if cp.has_section("Buffering"):
        s = "Buffering"
        cfg.Buffering.AudioBufferSeconds = cp.getfloat(s, "AudioBufferSeconds", fallback=cfg.Buffering.AudioBufferSeconds)
        cfg.Buffering.VideoBufferFrames = cp.getint(s, "VideoBufferFrames", fallback=cfg.Buffering.VideoBufferFrames)
        cfg.Buffering.WriterQueueSize = cp.getint(s, "WriterQueueSize", fallback=cfg.Buffering.WriterQueueSize)
        policy = cp.get(s, "WriterDropPolicy", fallback=cfg.Buffering.WriterDropPolicy).strip().lower()
        if policy in ("drop_oldest", "drop_newest", "block", "drop"):
            cfg.Buffering.WriterDropPolicy = "drop_newest" if policy == "drop" else policy
        else:
            logger.warning(f"Invalid Buffering.WriterDropPolicy '{policy}'; falling back to '{cfg.Buffering.WriterDropPolicy}'.")

    cams: List[VideoCamConfig] = []
    for i in range(1, cfg.Video.MaxCams + 1):
        sec = f"VideoCam{i}"
        if not cp.has_section(sec):
            continue
        vc = VideoCamConfig()
        vc.Enabled = _get_bool(cp, sec, "Enabled", vc.Enabled)
        vc.DeviceIndex = cp.getint(sec, "DeviceIndex", fallback=vc.DeviceIndex)
        vc.DevNode = cp.get(sec, "DevNode", fallback=vc.DevNode)
        vc.Label = cp.get(sec, "Label", fallback=vc.Label)
        vc.FPS = cp.getint(sec, "FPS", fallback=vc.FPS)
        vc.Width = cp.getint(sec, "Width", fallback=vc.Width)
        vc.Height = cp.getint(sec, "Height", fallback=vc.Height)
        vc.AutoExposure = _get_bool(cp, sec, "AutoExposure", bool(vc.AutoExposure))
        vc.AutoFocus = _get_bool(cp, sec, "AutoFocus", bool(vc.AutoFocus))
        vc.Brightness = cp.getint(sec, "Brightness", fallback=vc.Brightness)
        vc.Hue = cp.getint(sec, "Hue", fallback=vc.Hue)
        vc.Saturation = cp.getint(sec, "Saturation", fallback=vc.Saturation)
        vc.PixelFormat = cp.get(sec, "PixelFormat", fallback=vc.PixelFormat).upper()
        if vc.DevNode:
            m = DEVNODE_PATTERN.match(vc.DevNode.strip())
            if m:
                try:
                    vc.DeviceIndex = int(m.group(1))
                except Exception as exc:
                    logger.warning("Failed to derive DeviceIndex from DevNode %s: %s", vc.DevNode, exc)
        cams.append(vc)
    cfg.Video.Cams = cams
    return cfg
