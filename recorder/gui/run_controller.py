from __future__ import annotations
import os
import json
from dataclasses import asdict
from typing import Optional, Callable

from ..config import AppConfig
from ..naming import build_paths
from ..audio.lsl_audio import AudioLSLStreamer, AudioLSLSettings
from ..lsl.labrecorder_rcs import LabRecorderRCS

class RunController:
    def __init__(self, cfg: AppConfig, status_cb: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.status_cb = status_cb
        self.audio: Optional[AudioLSLStreamer] = None
        self.labrec: Optional[LabRecorderRCS] = None
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
                    "video": {"Enabled": self.cfg.Video.Enabled, "Cams": [asdict(c) for c in self.cfg.Video.Cams]},
                    "xdf_writer": "LabRecorder (RCS)",
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

        # Audio via LSL (no WAV)
        if self.cfg.Audio.Enabled:
            aset = AudioLSLSettings(
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

            self.audio = AudioLSLStreamer(aset, status_cb=self.log)
            self.audio.start()

        # LabRecorder RCS start (XDF)
        self.labrec = LabRecorderRCS(host="127.0.0.1", port=22345)
        self.labrec.connect()
        self.labrec.select_all()

        root = os.path.abspath(self.cfg.Output.StudyRoot)
        template = self.cfg.Output.PathTemplate + ".xdf"
        p = self.cfg.Prompts
        self.labrec.filename(
            root=root,
            template=template,
            participant=p.Subject,
            session=p.Session,
            task=p.Block,
            run=p.Run,
            acq=p.Acquisition,
        )
        self.labrec.start()
        self.log("LabRecorder: started XDF recording via RCS")

        self._running = True
        self.log(f"Started run in {out_dir} with base {base_name}")

    def stop(self):
        if not self._running:
            return

        if self.audio:
            try:
                self.audio.stop()
            finally:
                self.audio = None

        if self.labrec:
            try:
                self.labrec.stop()
                self.log("LabRecorder: stopped XDF recording via RCS")
            finally:
                self.labrec.close()
                self.labrec = None

        self._running = False
        self.log("Stopped run.")
