from __future__ import annotations
import os
from dataclasses import asdict
from .config import AppPrompts, OutputConfig

def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)

def render_template(template: str, p: AppPrompts) -> str:
    s = template
    s = s.replace("%p", _safe(p.Subject))
    s = s.replace("%s", _safe(p.Session))
    s = s.replace("%b", _safe(p.Block))
    s = s.replace("%a", _safe(p.Acquisition))
    s = s.replace("%r", _safe(p.Run))
    for k, v in asdict(p).items():
        s = s.replace("{" + k + "}", _safe(v))
    return s

def build_paths(out: OutputConfig, p: AppPrompts) -> dict:
    rel = render_template(out.PathTemplate, p)
    base_dir = os.path.join(out.StudyRoot, os.path.dirname(rel))
    base_name = os.path.basename(rel)
    return {"base_dir": base_dir, "base_name": base_name, "rel": rel}

def video_filename(base_name: str, cam_idx: int, label: str, container: str="mp4") -> str:
    return f"{base_name}_cam-{cam_idx:02d}_role-{_safe(label)}.{container}"
