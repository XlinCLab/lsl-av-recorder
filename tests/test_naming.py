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
    """Alphanumerics and the allowed punctuation (-, _, .) pass through unchanged."""
    assert _safe_name("abc123") == "abc123"
    assert _safe_name("a-b_c.d") == "a-b_c.d"


def test_safe_replaces_disallowed_chars_with_underscore():
    """Any character outside the allowed set becomes a single underscore."""
    assert _safe_name("a b/c") == "a_b_c"
    assert _safe_name("na:me*?") == "na_me__"


def test_safe_empty_string():
    """An empty string sanitizes to an empty string (no crash, no padding)."""
    assert _safe_name("") == ""


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------

def _prompts(**kw) -> AppPrompts:
    """Build an AppPrompts with sensible defaults, overridable per test."""
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
    """The %p/%s/%b/%a/%r shortcuts expand to Subject/Session/Block/Acquisition/Run."""
    p = _prompts(
        Subject="S01",
        Session="2",
        Block="b",
        Acquisition="a",
        Run="3",
    )
    out = render_template(template="sub-%p/ses-%s/%b_%a_%r", p=p)
    assert out == "sub-S01/ses-2/b_a_3"


def test_render_template_named_fields():
    """{FieldName} placeholders expand too, covering fields with no %-shortcut
    (e.g. ExperimentName)."""
    p = _prompts(ExperimentName="MyExp", Subject="S01")
    assert render_template(template="{ExperimentName}_{Subject}", p=p) == "MyExp_S01"


def test_render_template_sanitizes_substituted_values():
    """Substituted values are sanitized as they are inserted, so unsafe field
    contents can't inject path separators or illegal characters."""
    p = _prompts(Subject="S 01", Block="a/b")
    out = render_template(template="sub-%p/task-%b", p=p)
    assert out == "sub-S_01/task-a_b"


# ---------------------------------------------------------------------------
# build_paths
# ---------------------------------------------------------------------------

def test_build_paths_splits_dir_and_basename():
    """build_paths renders the template, then splits it into StudyRoot-joined
    base_dir, base_name, and the raw relative path."""
    out = OutputConfig(StudyRoot="/data", PathTemplate="sub-%p/ses-%s/rec_%r")
    p = _prompts(Subject="S01", Session="1", Run="2")
    paths = build_paths(out=out, p=p)
    assert paths["rel"] == "sub-S01/ses-1/rec_2"
    assert paths["base_name"] == "rec_2"
    assert paths["base_dir"] == os.path.join("/data", "sub-S01/ses-1")


def test_build_paths_flat_template_has_studyroot_as_base_dir():
    """With a flat (no-directory) template, base_dir is just StudyRoot since the
    relative path's dirname is empty."""
    out = OutputConfig(StudyRoot="/data", PathTemplate="rec_%r")
    paths = build_paths(out=out, p=_prompts(Run="7"))
    assert paths["base_name"] == "rec_7"
    assert paths["base_dir"] == os.path.join("/data", "")


# ---------------------------------------------------------------------------
# video_filename
# ---------------------------------------------------------------------------

def test_video_filename_zero_pads_camera_index():
    """The camera index is zero-padded to two digits so filenames sort correctly."""
    assert video_filename(base_name="rec", cam_idx=0, label="Cam") == "rec_cam-00_role-Cam.mp4"
    assert video_filename(base_name="rec", cam_idx=12, label="Cam") == "rec_cam-12_role-Cam.mp4"


def test_video_filename_sanitizes_label_and_respects_container():
    """The role label is sanitized and the container argument sets the extension."""
    result = video_filename(
        base_name="rec",
        cam_idx=1,
        label="Left Eye",
        container="mkv",
    )
    assert result == "rec_cam-01_role-Left_Eye.mkv"
