"""Tests for output-path / filename templating."""
from __future__ import annotations

import os

from recorder.config import AppPrompts, OutputConfig
from recorder.naming import (_safe_name, build_paths, render_template,
                             video_filename)

# ---------------------------------------------------------------------------
# _safe_name
# ---------------------------------------------------------------------------

def test_safe_keeps_alnum_and_allowed_punctuation():
    assert _safe_name("abc123") == "abc123"
    assert _safe_name("a-b_c.d") == "a-b_c.d"


def test_safe_replaces_disallowed_chars_with_underscore():
    assert _safe_name("a b/c") == "a_b_c"
    assert _safe_name("na:me*?") == "na_me__"


def test_safe_empty_string():
    assert _safe_name("") == ""


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------

def _prompts(**kw) -> AppPrompts:
    base = dict(
        ExperimentName="Exp",
        Subject="S01",
        Session="1",
        Block="task",
        Acquisition="acq",
        Run="1",
    )
    base.update(kw)
    return AppPrompts(**base)


def test_render_template_percent_shortcuts():
    p = _prompts(Subject="S01", Session="2", Block="b", Acquisition="a", Run="3")
    out = render_template("sub-%p/ses-%s/%b_%a_%r", p)
    assert out == "sub-S01/ses-2/b_a_3"


def test_render_template_named_fields():
    p = _prompts(ExperimentName="MyExp", Subject="S01")
    # ExperimentName has no %-shortcut but is available as a {named} field
    assert render_template("{ExperimentName}_{Subject}", p) == "MyExp_S01"


def test_render_template_sanitizes_substituted_values():
    p = _prompts(Subject="S 01", Block="a/b")
    out = render_template("sub-%p/task-%b", p)
    # Values are sanitized as they are substituted in
    assert out == "sub-S_01/task-a_b"


# ---------------------------------------------------------------------------
# build_paths
# ---------------------------------------------------------------------------

def test_build_paths_splits_dir_and_basename():
    out = OutputConfig(StudyRoot="/data", PathTemplate="sub-%p/ses-%s/rec_%r")
    p = _prompts(Subject="S01", Session="1", Run="2")
    paths = build_paths(out, p)
    assert paths["rel"] == "sub-S01/ses-1/rec_2"
    assert paths["base_name"] == "rec_2"
    assert paths["base_dir"] == os.path.join("/data", "sub-S01/ses-1")


def test_build_paths_flat_template_has_studyroot_as_base_dir():
    out = OutputConfig(StudyRoot="/data", PathTemplate="rec_%r")
    paths = build_paths(out, _prompts(Run="7"))
    assert paths["base_name"] == "rec_7"
    # dirname of a bare filename is empty -> base_dir is just StudyRoot
    assert paths["base_dir"] == os.path.join("/data", "")


# ---------------------------------------------------------------------------
# video_filename
# ---------------------------------------------------------------------------

def test_video_filename_zero_pads_camera_index():
    assert video_filename("rec", 0, "Cam") == "rec_cam-00_role-Cam.mp4"
    assert video_filename("rec", 12, "Cam") == "rec_cam-12_role-Cam.mp4"


def test_video_filename_sanitizes_label_and_respects_container():
    assert (
        video_filename("rec", 1, "Left Eye", container="mkv")
        == "rec_cam-01_role-Left_Eye.mkv"
    )
