"""Names for the channels of an audio stream (e.g. which speaker is on which input)."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

CHANNEL_LABEL_REGEX = re.compile(r"^channellabel(\d+)$", re.IGNORECASE)


def default_channel_label(channel: int) -> str:
    """Label of a channel that has none assigned (`channel` is 1-based)."""
    return f"Channel {channel}"


def trim_channel_labels(labels: Optional[Sequence[str]], channels: Optional[int] = None) -> List[str]:
    """Labels stripped of surrounding whitespace, cut to `channels` entries if given and
    without trailing blanks (an empty entry elsewhere keeps its position)."""
    trimmed = [str(label or "").strip() for label in (labels or [])]
    if channels is not None:
        trimmed = trimmed[: max(int(channels), 0)]
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return trimmed


def resolve_channel_labels(labels: Optional[Sequence[str]], channels: int) -> List[str]:
    """Exactly `channels` labels: configured ones in order, 'Channel N' for the rest.
    Labels beyond the channel count are ignored."""
    given = [str(label or "").strip() for label in (labels or [])]
    return [
        (given[i] if i < len(given) and given[i] else default_channel_label(i + 1))
        for i in range(max(int(channels), 0))
    ]


def audio_channel_descriptors(labels: Sequence[str]) -> List[Dict[str, str]]:
    """Per-channel entries for the XDF stream header."""
    return [{"label": label, "type": "Audio"} for label in labels]


def label_problems(labels: Optional[Sequence[str]], channels: int) -> List[str]:
    """What is inconsistent about the labels of the recorded channels, if anything."""
    given = [label for label in trim_channel_labels(labels, channels) if label]
    return [
        f"Channel label '{dup}' is used for more than one channel."
        for dup in sorted({g for g in given if given.count(g) > 1})
    ]
