from __future__ import annotations
import os
import json
from dataclasses import asdict
from typing import Optional, Callable, Dict, List

from ..config import AppConfig
from ..naming import build_paths
#from ..audio.lsl_audio import AudioLSLStreamer
from ..audio.lsl_audio import AudioStreamSettings
#from ..video.video_recorder import VideoRecorder  # TODO
from ..xdf.xdf_writer import XDFWriter


class RunController:
    def __init__(self, cfg: AppConfig, status_cb: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.status_cb = status_cb
        # self.audio: Optional[AudioLSLStreamer] = None     # TODO
        # self.videos: List[VideoRecorder] = []

        self.xdf: Optional[XDFWriter] = None
        self.audio_sid: Optional[int] = None
        self.video_sids: Dict[str, int] = {}

        self.paths: Optional[dict] = None
        self._running = False

    def log(self, msg: str):
        if self.status_cb:
            self.status_cb(msg)

    def start(self):
        if self._running:
            return

        self.paths = build_paths(self.cfg.Output, self.cfg.Prompts)
        out_dir = self.paths["base_dir"]
        base_name = self.paths["base_name"]
        os.makedirs(out_dir, exist_ok=True)

        meta_path = os.path.join(out_dir, f"{base_name}_session.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "prompts": asdict(self.cfg.Prompts),
                    "output": asdict(self.cfg.Output),
                    "audio": asdict(self.cfg.Audio),
                    # original:
                    # "video": {"Enabled": self.cfg.Video.Enabled, "Cams": [asdict(c) for c in self.cfg.Video.Cams]},
                    "video": asdict(self.cfg.Video),
                    "xdf_writer": "Python",
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

        # XDF writer
        xdf_path = self._get_xdf_path()
        self.xdf = XDFWriter(xdf_path)
        self.xdf.start()
        self.log(f"XDF writer started: {xdf_path}")

        # # Audio stream
        if self.cfg.Audio.Enabled:
            aset = self._get_audio_stream_settings()
            self.audio_sid = self.xdf.add_audio_stream(
                name=aset.stream_name,
                samplerate=aset.samplerate,
                channels=aset.channels,
                fmt="float32" if aset.bitdepth == 32 else "int16",
                source_id=aset.source_id,
            )

        #     self.audio = AudioLSLStreamer(
        #         aset,
        #         status_cb=self.log,
        #         # IMPORTANT: this callback must be supported by your audio code
        #         sample_cb=self._on_audio_samples,
        #     )
        #     self.audio.start()
        #     self.log("Audio capture started")

        # # Video streams
        # if self.cfg.Video.Enabled:
        #     for cam in self.cfg.Video.Cams:
        #         if not cam.Enabled:
        #             continue

        #         video_path = os.path.join(
        #             out_dir,
        #             f"{base_name}_cam-{cam.Label}.{self.cfg.Video.Container}",
        #         )

        #         sid = self.xdf.add_video_stream(
        #             name=f"Camera-{cam.Label}",
        #             camera_id=str(cam.DeviceIndex),
        #             video_path=video_path,
        #             width=cam.Width,
        #             height=cam.Height,
        #             fps=cam.FPS,
        #         )
        #         self.video_sids[cam.Label] = sid

        #         vr = VideoRecorder(
        #             cam_cfg=cam,
        #             output_path=video_path,
        #             status_cb=self.log,
        #             # IMPORTANT: this callback must be supported by your video code
        #             frame_cb=lambda ts, idx, label=cam.Label: self._on_video_frame(
        #                 label, ts, idx
        #             ),
        #         )
        #         vr.start()
        #         self.videos.append(vr)

        #         self.log(f"Video capture started: {cam.Label}")

        self._running = True
        self.log(f"Run started in {out_dir}")

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
                self.log(f"Skipped stopping video {vr} due to exception: {exc}")
        self.videos.clear()

        # XDF
        if self.xdf:
            self.xdf.stop()
            self.xdf = None
            self.log("XDF writer stopped")

        self._running = False
        self.log("Run stopped")

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

    # -------------------------
    # Callbacks from recorders
    # -------------------------

    def _on_audio_samples(self, timestamps, samples):
        """
        Called by AudioLSLStreamer
        timestamps: (n,)
        samples: (n, channels)
        """
        if self.xdf and self.audio_sid is not None:
            self.xdf.write_audio(self.audio_sid, timestamps, samples)

    def _on_video_frame(self, label: str, timestamp: float, frame_index: int):
        """
        Called by VideoRecorder per frame
        """
        if not self.xdf:
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


