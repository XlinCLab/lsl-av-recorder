from __future__ import annotations

import glob
import json
import os
import threading
from dataclasses import asdict
from time import sleep
from typing import Callable, Dict, List, Optional

import numpy as np
from ..audio.lsl_audio import (AudioLSLStreamer, AudioStreamSettings,
                               _dtype_format)
from pylsl import StreamInfo
from ..config import AppConfig, VideoCamConfig
from ..naming import build_paths
from ..video.video_recorder import VideoRecorder
from ..xdf.xdf_writer import XDFWriter
from ..lsl.lsl_inlet_recorder import LslInletRecorder, lsl_format_to_xdf


class RunController:
    def __init__(
        self,
        cfg: AppConfig,
        status_cb: Optional[Callable[[str], None]] = None,
        lsl_streams: Optional[List[StreamInfo]] = None,
    ):
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
        self.lsl_streams = lsl_streams or []
        self.lsl_recorders: List[LslInletRecorder] = []

        # Buffering config (software buffers before XDF writes)
        self.audio_buffer_seconds: float = max(0.0, float(self.cfg.Audio.BufferSeconds))
        self.video_buffer_frames: int = max(0, int(self.cfg.Video.BufferFrames))
        self._audio_buffer_target: int = 0
        self._audio_buf_lock = threading.Lock()
        self._audio_ts_buf: List[np.ndarray] = []
        self._audio_samples_buf: List[np.ndarray] = []
        self._audio_buf_n: int = 0
        self._video_buf_lock = threading.Lock()
        self._video_ts_buf: Dict[str, List[float]] = {}
        self._video_idx_buf: Dict[str, List[int]] = {}

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
        # Check that the specified output directory is empty
        # If not, avoid accidentally overwriting previous results by creating runN subdirectory
        is_empty = len(os.listdir(self.outdir)) == 0
        if not is_empty:
            n = 2
            while os.path.exists(os.path.join(self.outdir, f"run{n:02d}")):
                n += 1
            self.outdir = os.path.join(self.outdir, f"run{n:02d}")
            os.makedirs(self.outdir, exist_ok=True)

    def _write_metadata(self):
        meta_path = os.path.join(self.outdir, f"{self.base_name}_session.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "prompts": asdict(self.cfg.Prompts),
                    "output": asdict(self.cfg.Output),
                    "audio": asdict(self.cfg.Audio),
                    "labrecorder": asdict(self.cfg.LabRecorder),
                    "lsl_streams": [self._lsl_stream_meta(s) for s in self.lsl_streams],
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
        
        # Get path to XDF file
        # As a safety measure against accidentally overwriting a previous XDF file,
        # this will raise an error if the output directory already contains an XDF file 
        xdf_path = self._get_xdf_path()
        
        # Initialize input streams
        self._initialize_streams()

        # Start streams (roughly) simultaneously
        self._start_streams()

        # Start XDF writer and add streams
        xdf_writer = self._initialize_xdf_writer(xdf_path=xdf_path)
        xdf_writer.start()
        self.info(f"XDF writer started")
        self._add_streams_to_xdf_writer(xdf_writer)
        self._initialize_lsl_recorders(xdf_writer)
        self._start_lsl_recorders()

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

        # Stop LSL inlet recorders
        self._stop_lsl_recorders()

        # XDF
        if self.xdf:
            self._flush_audio_buffer()
            self._flush_video_buffer()
            self.xdf.stop()
            self.xdf = None
            self.info("XDF writer stopped")

        self._running = False
        self.info("Run stopped")

    def _lsl_stream_meta(self, stream: StreamInfo) -> Dict[str, object]:
        def safe_get(fn, key: str, out: Dict[str, object]):
            try:
                out[key] = fn()
            except Exception:
                pass

        meta: Dict[str, object] = {}
        safe_get(stream.name, "name", meta)
        safe_get(stream.type, "type", meta)
        safe_get(stream.channel_count, "channel_count", meta)
        safe_get(stream.nominal_srate, "nominal_srate", meta)
        safe_get(stream.source_id, "source_id", meta)
        safe_get(stream.uid, "uid", meta)
        safe_get(stream.hostname, "hostname", meta)
        return meta

    def _get_xdf_path(self) -> str:
        base_name = self.base_name
        if not base_name.endswith(".xdf"):
            base_name += ".xdf"
        xdf_path = os.path.join(self.outdir, base_name)

        # Verify that there is no existing xdf file already in this directory
        xdf_contents = glob.glob(os.path.join(self.outdir, "*.xdf"))
        if len(xdf_contents) > 0:
            raise ValueError(f"Specified output directory {self.outdir} already contains an XDF file!")
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

        for stream in self.lsl_streams:
            try:
                fmt, _ = lsl_format_to_xdf(stream.channel_format())
                sid = xdf_writer.add_lsl_stream(
                    name=stream.name(),
                    stype=stream.type(),
                    channel_count=stream.channel_count(),
                    srate=stream.nominal_srate(),
                    fmt=fmt,
                    source_id=stream.source_id() or stream.uid(),
                    extra={"hostname": stream.hostname(), "uid": stream.uid()},
                    key=f"lsl:{stream.uid()}",
                )
                self.info(f"Initialized LSL stream <{stream.name()}> in XDF")
            except Exception as exc:
                self.warning(f"Skipping LSL stream due to error: {exc}")

    def _initialize_lsl_recorders(self, xdf_writer: XDFWriter):
        self.lsl_recorders = []
        if not self.lsl_streams:
            return
        for stream in self.lsl_streams:
            try:
                sid = xdf_writer.streams.get(f"lsl:{stream.uid()}")
                if sid is None:
                    self.warning(f"LSL stream not registered in XDF: {stream.name()}")
                    continue
                rec = LslInletRecorder(stream, sid, xdf_writer)
                self.lsl_recorders.append(rec)
                self.info(f"Initialized LSL inlet for stream <{stream.name()}>")
            except Exception as exc:
                self.warning(f"Failed to initialize LSL inlet for {stream.name()}: {exc}")

    def _start_lsl_recorders(self):
        for rec in self.lsl_recorders:
            rec.start()

    def _stop_lsl_recorders(self):
        for rec in self.lsl_recorders:
            rec.stop()
        self.lsl_recorders = []

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
            if self.audio_buffer_seconds > 0:
                self._audio_buffer_target = max(
                    1, int(self.audio_buffer_seconds * self.audio_settings.samplerate)
                )
                self.info(
                    f"Audio buffering enabled: ~{self.audio_buffer_seconds:.3f}s "
                    f"({self._audio_buffer_target} samples)"
                )
            self.audio = AudioLSLStreamer(
                self.audio_settings,
                status_cb=self.log,
                sample_cb=self._on_audio_samples,
            )
            self.info("Initialized audio stream")

        # Initialize video streams
        if self.video_enabled:
            if self.video_buffer_frames > 0:
                self.info(f"Video buffering enabled: {self.video_buffer_frames} frames")
            for cam in self.cams:
                self._video_ts_buf.setdefault(cam.Label, [])
                self._video_idx_buf.setdefault(cam.Label, [])
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
            if self._audio_buffer_target <= 0:
                self.xdf.write_audio(self.audio_sid, timestamps, samples)
                return

            batch = self._buffer_audio_samples(timestamps, samples)
            if batch is not None:
                ts, x = batch
                self.xdf.write_audio(self.audio_sid, ts, x)

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
        if self.video_buffer_frames <= 0:
            self.xdf.write_video_frames(
                sid,
                timestamps=[timestamp],
                frame_indices=[frame_index],
            )
            return

        batch = self._buffer_video_frames(label, timestamp, frame_index)
        if batch is not None:
            ts, idx = batch
            self.xdf.write_video_frames(sid, ts, idx)

    def _buffer_audio_samples(self, timestamps: np.ndarray, samples: np.ndarray):
        with self._audio_buf_lock:
            self._audio_ts_buf.append(timestamps)
            self._audio_samples_buf.append(samples)
            self._audio_buf_n += len(timestamps)
            if self._audio_buf_n < self._audio_buffer_target:
                return None
            ts_chunks = self._audio_ts_buf
            sample_chunks = self._audio_samples_buf
            self._audio_ts_buf = []
            self._audio_samples_buf = []
            self._audio_buf_n = 0

        ts = ts_chunks[0] if len(ts_chunks) == 1 else np.concatenate(ts_chunks)
        x = sample_chunks[0] if len(sample_chunks) == 1 else np.concatenate(sample_chunks, axis=0)
        return ts, x

    def _flush_audio_buffer(self):
        if self._audio_buffer_target <= 0 or not self.xdf or self.audio_sid is None:
            return
        with self._audio_buf_lock:
            if self._audio_buf_n == 0:
                return
            ts_chunks = self._audio_ts_buf
            sample_chunks = self._audio_samples_buf
            self._audio_ts_buf = []
            self._audio_samples_buf = []
            self._audio_buf_n = 0
        ts = ts_chunks[0] if len(ts_chunks) == 1 else np.concatenate(ts_chunks)
        x = sample_chunks[0] if len(sample_chunks) == 1 else np.concatenate(sample_chunks, axis=0)
        self.xdf.write_audio(self.audio_sid, ts, x)

    def _buffer_video_frames(self, label: str, timestamp: float, frame_index: int):
        with self._video_buf_lock:
            ts_buf = self._video_ts_buf.setdefault(label, [])
            idx_buf = self._video_idx_buf.setdefault(label, [])
            ts_buf.append(float(timestamp))
            idx_buf.append(int(frame_index))
            if len(ts_buf) < self.video_buffer_frames:
                return None
            ts = ts_buf
            idx = idx_buf
            self._video_ts_buf[label] = []
            self._video_idx_buf[label] = []
        return np.asarray(ts, dtype=np.float64), np.asarray(idx, dtype=np.int64)

    def _flush_video_buffer(self):
        if self.video_buffer_frames <= 0 or not self.xdf:
            return
        with self._video_buf_lock:
            labels = list(self._video_ts_buf.keys())
            if not labels:
                return
            data = []
            for label in labels:
                ts = self._video_ts_buf.get(label, [])
                idx = self._video_idx_buf.get(label, [])
                if ts and idx:
                    data.append((label, ts, idx))
                self._video_ts_buf[label] = []
                self._video_idx_buf[label] = []
        for label, ts, idx in data:
            sid = self.video_sids.get(label)
            if sid is None:
                continue
            self.xdf.write_video_frames(
                sid,
                np.asarray(ts, dtype=np.float64),
                np.asarray(idx, dtype=np.int64),
            )
