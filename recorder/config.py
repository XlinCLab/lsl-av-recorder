from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from typing import List, Optional


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
    SampleRate: int = 48000
    BitDepth: int = 32
    Channels: int = 1
    StreamName: str = "Audio"

@dataclass
class VideoCamConfig:
    Enabled: bool = False
    DeviceIndex: int = 0
    DevNode: str = "/dev/video0"
    Label: str = "Cam"
    FPS: int = 30
    Width: int = 1280
    Height: int = 720
    AutoExposure: Optional[bool] = False
    Exposure: Optional[int] = None
    AutoFocus: Optional[bool] = False
    Focus: Optional[int] = None
    Brightness: Optional[int] = None
    Contrast: Optional[int] = None
    Hue: Optional[int] = None
    Saturation: Optional[int] = None
    Zoom: Optional[int] = None

@dataclass
class VideoConfig:
    Enabled: bool = False
    MaxCams: int = 4
    Codec: str = "mp4v"
    Container: str = "mp4"
    PreviewFPS: int = 15
    Cams: List[VideoCamConfig] = field(default_factory=list)

@dataclass
class AppConfig:
    Prompts: AppPrompts = field(default_factory=AppPrompts)
    Output: OutputConfig = field(default_factory=OutputConfig)
    Audio: AudioConfig = field(default_factory=AudioConfig)
    Video: VideoConfig = field(default_factory=VideoConfig)

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

    if cp.has_section("Video"):
        s = "Video"
        cfg.Video.Enabled = _get_bool(cp, s, "Enabled", False)
        cfg.Video.MaxCams = cp.getint(s, "MaxCams", fallback=cfg.Video.MaxCams)
        cfg.Video.Codec = cp.get(s, "Codec", fallback=cfg.Video.Codec)
        cfg.Video.Container = cp.get(s, "Container", fallback=cfg.Video.Container)
        cfg.Video.PreviewFPS = cp.getint(s, "PreviewFPS", fallback=cfg.Video.PreviewFPS)

    cams: List[VideoCamConfig] = []
    for i in range(1, cfg.Video.MaxCams + 1):
        sec = f"VideoCam{i}"
        vc = VideoCamConfig()
        if cp.has_section(sec):
            vc.Enabled = _get_bool(cp, sec, "Enabled", vc.Enabled)
            vc.DeviceIndex = cp.getint(sec, "DeviceIndex", fallback=vc.DeviceIndex)
            vc.DevNode = cp.get(sec, "DevNode", fallback=vc.DevNode)
            vc.Label = cp.get(sec, "Label", fallback=vc.Label)
            vc.FPS = cp.getint(sec, "FPS", fallback=vc.FPS)
            vc.Width = cp.getint(sec, "Width", fallback=vc.Width)
            vc.Height = cp.getint(sec, "Height", fallback=vc.Height)
        cams.append(vc)
    cfg.Video.Cams = cams
    return cfg
