"""Tests for recorder.audio.channel_labels: naming the channels of an audio stream."""
from __future__ import annotations

from recorder.audio.channel_labels import (audio_channel_descriptors,
                                           default_channel_label,
                                           label_problems,
                                           resolve_channel_labels,
                                           trim_channel_labels)
from recorder.audio.settings import build_audio_stream_settings
from recorder.config import AudioConfig


def test_default_channels():
    assert default_channel_label(1) == "Channel 1"
    assert default_channel_label(5) == "Channel 5"
    assert default_channel_label(20) == "Channel 20"


def test_trim_strips_whitespace_and_trailing_blanks():
    assert trim_channel_labels([" Participant A ", "B", "  ", ""]) == ["Participant A", "B"]
    assert trim_channel_labels(None) == []
    assert trim_channel_labels(["", " "]) == []


def test_trim_keeps_the_position_of_an_empty_entry():
    assert trim_channel_labels(["A", "", "C"]) == ["A", "", "C"]


def test_trim_cuts_to_the_channel_count():
    assert trim_channel_labels(["A", "B", "C"], channels=2) == ["A", "B"]
    assert trim_channel_labels(["A", "", "C"], channels=2) == ["A"]


def test_resolve_fills_missing_labels_with_defaults():
    assert resolve_channel_labels(["Mic"], 3) == ["Mic", "Channel 2", "Channel 3"]
    assert resolve_channel_labels(["A", "", "C"], 3) == ["A", "Channel 2", "C"]
    assert resolve_channel_labels([], 2) == [default_channel_label(1), default_channel_label(2)]
    assert resolve_channel_labels(None, 1) == ["Channel 1"]

def test_resolve_ignores_labels_beyond_the_channel_count():
    assert resolve_channel_labels(["A", "B", "C"], 2) == ["A", "B"]


def test_channel_descriptors_for_the_xdf_header():
    assert audio_channel_descriptors(["A", "B"]) == [
        {"label": "A", "type": "Audio"},
        {"label": "B", "type": "Audio"},
    ]


def test_label_problems_reports_duplicates_among_recorded_channels_only():
    assert label_problems(["A", "B"], 2) == []
    assert label_problems([], 2) == []
    duplicate = label_problems(["A", "A"], 2)
    assert len(duplicate) == 1 and "'A'" in duplicate[0]
    # a repeat on a channel that is not recorded does not matter
    assert label_problems(["A", "B", "A"], 2) == []


def test_audio_stream_settings_carry_one_label_per_channel():
    cfg = AudioConfig(Channels=3, ChannelLabels=["Participant A", "Participant B"])
    settings = build_audio_stream_settings(cfg)
    assert settings.channel_labels == ["Participant A", "Participant B", "Channel 3"]
