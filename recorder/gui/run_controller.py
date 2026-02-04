from __future__ import annotations

import json
import os
from dataclasses import asdict
from time import sleep
from typing import Callable, Dict, List, Optional

from ..audio.lsl_audio import (AudioLSLStreamer, AudioStreamSettings,
                               _dtype_format)
from ..config import AppConfig, VideoCamConfig
from ..naming import build_paths
from ..video.video_recorder import VideoRecorder
from ..xdf.xdf_writer import XDFWriter


class RunController:
    def __init__(self, cfg: AppConfig, status_cb: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.status_cb = status_cb
        self._running = False

        # Audio and video streams
        self.audio_enabled = self.cfg.Audio.Enabled
        self.audio_settings: Optional[AudioStreamSettings] = None
        self.audio: Optional[AudioLSLStreamer] = None
        self.video_enabled = self.cfg.Video.Enabled
        self.cams = self._get_active_cams()
        self.videos: List[VideoRecorder] = []
        self.audio_sid: Optional[int] = None
        self.video_sids: Dict[str, int] = {}

        # XDF writer
        self.xdf: Optional[XDFWriter] = None

        # Create output directory and write run metadata
        self.paths: Dict[str, str] = build_paths(self.cfg.Output, self.cfg.Prompts)
        self.outdir: str = self.paths["base_dir"]
        self.base_name: str = self.paths["base_name"]
        self._setup_paths()
        self._write_metadata()

    def log(self, msg: str, loglevel: str = "INFO"):
        if self.status_cb:
            self.status_cb(msg, loglevel)
    
    def info(self, msg: str):
        self.log(msg, loglevel="INFO")

    def warning(self, msg: str):
        self.log(msg, loglevel="WARNING")

    def error(self, msg: str):
        self.log(msg, loglevel="ERROR")
    
    def _setup_paths(self):
        os.makedirs(self.outdir, exist_ok=True)

    def _write_metadata(self):
        meta_path = os.path.join(self.outdir, f"{self.base_name}_session.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "prompts": asdict(self.cfg.Prompts),
                    "output": asdict(self.cfg.Output),
                    "audio": asdict(self.cfg.Audio),
                    "video": {
                        "Enabled": self.video_enabled,
                        "Cams": [asdict(c) for c in self.cams if c.Enabled] if self.video_enabled else []
                    },
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        self.info(f"Wrote run metadata to {meta_path}")

    def start(self):
        if self._running:
            return
        
        # Initialize input streams
        self._initialize_streams()

        # Start streams (roughly) simultaneously
        self._start_streams()

        # Start XDF writer and add streams
        xdf_writer = self._initialize_xdf_writer()
        xdf_writer.start()
        self.info(f"XDF writer started")
        self._add_streams_to_xdf_writer(xdf_writer)

        # Once all streams initialized, started, and added to XDF writer,
        # connect XDF writer to RunController to begin recording stream data to XDF
        self.xdf = xdf_writer
        self._running = True
        self.info("Started recording streams in XDF")
        self.info(f"Run started in {self.outdir}")

    def stop(self):
        if not self._running:
            return

        # Stop audio
        if self.audio:
            try:
                self.audio.stop()
            finally:
                self.audio = None

        # Stop video
        for vr in self.videos:
            try:
                vr.stop()
            except Exception as exc:
                self.error(f"Skipped stopping video {vr} due to exception: {exc}")
        self.videos.clear()

        # XDF
        if self.xdf:
            self.xdf.stop()
            self.xdf = None
            self.info("XDF writer stopped")

        self._running = False
        self.info("Run stopped")

    @staticmethod
    def _resolve_xdf_path(
        root: str,
        template: str,
        participant: str,
        session: str,
        task: str,
        run: str,
        acq: str = "",
    ) -> str:
        path = template
        path = path.replace("%p", participant)
        path = path.replace("%s", session)
        path = path.replace("%b", task)
        path = path.replace("%r", run)
        path = path.replace("%a", acq or "default")

        if not path.endswith(".xdf"):
            path += ".xdf"

        return os.path.join(root, path)

    def _get_xdf_path(self) -> str:
        root = os.path.abspath(self.cfg.Output.StudyRoot)
        template = self.cfg.Output.PathTemplate
        prompts = self.cfg.Prompts
        xdf_path = self._resolve_xdf_path(
            root=root,
            template=template,
            participant=prompts.Subject,
            session=prompts.Session,
            task=prompts.Block,
            run=prompts.Run,
            acq=prompts.Acquisition,
        )
        # Create containing directory for xdf file
        outdir = os.path.dirname(xdf_path)
        os.makedirs(outdir, exist_ok=True)
        return xdf_path

    def _initialize_xdf_writer(self, xdf_path: str = None) -> XDFWriter:
        if xdf_path is None:
            xdf_path = self._get_xdf_path()
        xdf_writer = XDFWriter(xdf_path)
        self.info(f"Initialized XDF writer with output file: {xdf_path}")
        return xdf_writer

    def _add_streams_to_xdf_writer(self, xdf_writer: XDFWriter):
        if self.video_enabled:
            for cam in self.cams:
                video_path = self._get_video_output_path(cam)
                sid = xdf_writer.add_video_stream(
                    name=f"Camera-{cam.Label}",
                    camera_id=str(cam.DeviceIndex),
                    video_path=video_path,
                    width=cam.Width,
                    height=cam.Height,
                    fps=cam.FPS,
                )
                self.video_sids[cam.Label] = sid
                self.info(f"Initialized video stream from camera <{cam.Label}> in XDF")
        if self.audio_enabled:
            self.audio_sid = xdf_writer.add_audio_stream(
                name=self.audio_settings.stream_name,
                samplerate=self.audio_settings.samplerate,
                channels=self.audio_settings.channels,
                fmt=_dtype_format(self.audio_settings.bitdepth),
                source_id=self.audio_settings.source_id,
            )
            self.info(f"Initialized audio stream <{self.audio_settings.stream_name}> in XDF")

    def _get_audio_stream_settings(self) -> AudioStreamSettings:
        aset = AudioStreamSettings(
            device=None,
            samplerate=self.cfg.Audio.SampleRate,
            channels=self.cfg.Audio.Channels,
            bitdepth=self.cfg.Audio.BitDepth,
            stream_name=self.cfg.Audio.StreamName or "Audio",
            stream_type="Audio",
            source_id=f"audio:{self.cfg.Audio.Device or 'default'}",
        )
        if self.cfg.Audio.Device is not None:
            try:
                aset.device = int(self.cfg.Audio.Device)
            except ValueError:
                aset.device = self.cfg.Audio.Device
        return aset

    def _get_active_cams(self) -> List:
        if not self.video_enabled:
            return []
        active_cams = [c for c in self.cfg.Video.Cams if c.Enabled]
        for cam in active_cams:
            if cam.FPS is None or float(cam.FPS) <= 0:
                self.warning(f"Camera {cam.label} sampling rate is {cam.FPS}")
            if cam.Width is None or float(cam.Width) <= 0:
                self.warning(f"Camera {cam.label} width is {cam.Width}")
            if cam.Height is None or float(cam.Height) <= 0:
                self.warning(f"Camera {cam.label} height is {cam.Height}")
        return active_cams

    def _get_video_output_path(self, cam: VideoCamConfig):
        video_path = os.path.join(
            self.outdir,
            f"{self.base_name}_cam-{cam.Label}.{self.cfg.Video.Container}",
        )
        return video_path

    def _initialize_video_stream(self, cam: VideoCamConfig):
        video_path = self._get_video_output_path(cam)
        vr = VideoRecorder(
            cam_cfg=cam,
            output_path=video_path,
            status_cb=self.log,
            frame_cb=lambda ts, idx, label=cam.Label: self._on_video_frame(
                label, ts, idx
            ),
        )
        self.videos.append(vr)
        self.info(f"Initialized video stream for camera {cam.Label}")

    def _initialize_streams(self):
        # Initialize audio stream
        if self.audio_enabled:
            self.audio_settings = self._get_audio_stream_settings()
            self.audio = AudioLSLStreamer(
                self.audio_settings,
                status_cb=self.log,
                sample_cb=self._on_audio_samples,
            )
            self.info("Initialized audio stream")

        # Initialize video streams
        if self.video_enabled:
            for cam in self.cams:
                self._initialize_video_stream(cam)

    def _start_streams(self, sleep_timer: float | int = 3):
        # NB: Start video before audio
        if self.video_enabled:
            for vr in self.videos:
                vr.start()
                self.info(f"Video capture started: {vr.cam.Label}")
        if self.audio_enabled:
            self.audio.start()
            self.info("Audio capture started")
        # Sleep for N seconds before continuing in order
        # to give the streams a chance to "warm up"
        sleep(sleep_timer)


    # -------------------------
    # Callbacks from recorders
    # -------------------------

    def _on_audio_samples(self, timestamps, samples):
        """
        Called by AudioLSLStreamer
        timestamps: (n,)
        samples: (n, channels)
        """
        if not self.xdf or self.xdf._started is False:
            return
        if self.xdf and self.audio_sid is not None:
            self.xdf.write_audio(self.audio_sid, timestamps, samples)

    def _on_video_frame(self, label: str, timestamp: float, frame_index: int):
        """
        Called by VideoRecorder per frame
        """
        if not self.xdf or self.xdf._started is False:
            return

        sid = self.video_sids.get(label)
        if sid is None:
            return

        # write single-sample chunk (simple, safe)
        self.xdf.write_video_frames(
            sid,
            timestamps=[timestamp],
            frame_indices=[frame_index],
        )


