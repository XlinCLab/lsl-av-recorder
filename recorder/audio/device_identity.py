"""Ascertain stable identity for audio input devices.

PortAudio device indices are only valid for the PortAudio session that listed
them: they shift after a reboot or when a USB device is plugged in or out.
ALSA device names also embed the card number (e.g. "Scarlett 2i2 USB: Audio (hw:4,0)"),
so even the raw name may change. A device is therefore identified by its
normalized name, host API and, where the OS exposes one, its physical
location (the USB port on Linux), and the numeric index is used only as a hint.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Trailing ALSA card/device suffix, e.g. " (hw:4,0)" or " (plughw:1,0)"
_ALSA_SUFFIX = re.compile(r"\s*\((?:plug)?hw:\d+(?:,\d+)*\)\s*$")
_ALSA_CARD = re.compile(r"\((?:plug)?hw:(\d+)(?:,\d+)*\)\s*$")
_ASOUND_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[")
_ASOUND_USB_LOCATION = re.compile(r"\bat\s+(usb-[^\s,]+)")

MATCH_EXACT = "exact"  # name + host API + location
MATCH_NAME_HOSTAPI = "name+hostapi"
MATCH_NAME = "name"  # name only: the host API differs from the saved one
MATCH_INDEX_TIEBREAK = "index-tiebreak"  # identical devices, resolved by the saved index
MATCH_AMBIGUOUS = "ambiguous"
MATCH_NONE = "none"

# Dropdown entry standing in for a saved audio device that is not currently available
_MISSING_AUDIO_DEVICE = "__missing_audio_device__"


@dataclass(frozen=True)
class AudioDeviceIdentity:
    name: str  # normalized (ALSA suffix stripped)
    hostapi_name: Optional[str] = None
    location: Optional[str] = None
    index_hint: Optional[int] = None

    def describe(self) -> str:
        parts = [self.name]
        if self.hostapi_name:
            parts.append(self.hostapi_name)
        if self.location:
            parts.append(self.location)
        return " | ".join(parts)


@dataclass(frozen=True)
class DeviceMatch:
    device: Optional[Dict[str, Any]]
    level: str
    detail: str

    @property
    def found(self) -> bool:
        return self.device is not None

    @property
    def degraded(self) -> bool:
        """True when the match was weaker than name + host API (worth a warning)."""
        return self.level in (MATCH_NAME, MATCH_INDEX_TIEBREAK)


def normalize_device_name(name: Optional[str]) -> str:
    """Name without a trailing ALSA `(hw:N,M)` suffix and surrounding whitespace."""
    return _ALSA_SUFFIX.sub("", str(name or "")).strip()


def alsa_card_number(name: Optional[str]) -> Optional[int]:
    """Card number from a trailing `(hw:N,M)` in a PortAudio ALSA device name."""
    m = _ALSA_CARD.search(str(name or ""))
    return int(m.group(1)) if m else None


def parse_asound_cards(text: str) -> Dict[int, str]:
    """Map ALSA card number -> USB location (e.g. 'usb-0000:00:14.0-9') from the
    text of /proc/asound/cards. Non-USB cards are omitted."""
    locations: Dict[int, str] = {}
    card: Optional[int] = None
    for line in text.splitlines():
        m = _ASOUND_CARD_LINE.match(line)
        if m:
            card = int(m.group(1))
            continue
        if card is not None:
            loc = _ASOUND_USB_LOCATION.search(line)
            if loc:
                locations[card] = loc.group(1)
                card = None
    return locations


def read_asound_cards() -> str:
    try:
        with open("/proc/asound/cards", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def device_location(name: Optional[str], cards_text: Optional[str] = None) -> Optional[str]:
    """Physical location (USB path) of an ALSA device, or None where unavailable
    (non-Linux, non-USB card, or a name without an ALSA `(hw:N,M)` suffix)."""
    if cards_text is None:
        if not sys.platform.startswith("linux"):
            return None
        cards_text = read_asound_cards()
    card = alsa_card_number(name)
    if card is None or not cards_text:
        return None
    return parse_asound_cards(cards_text).get(card)


def identity_for(device: Dict[str, Any]) -> AudioDeviceIdentity:
    """Identity of an entry returned by `list_input_devices()`."""
    index = device.get("index")
    return AudioDeviceIdentity(
        name=normalize_device_name(device.get("name")),
        hostapi_name=device.get("hostapi_name"),
        location=device.get("location"),
        index_hint=int(index) if index is not None else None,
    )


def identity_from_audio_config(audio_cfg: Any) -> Optional[AudioDeviceIdentity]:
    """Saved identity from an AudioConfig, or None when only an index (an old
    config) or nothing is stored. The index, if numeric, is kept as a hint."""
    name = getattr(audio_cfg, "DeviceName", None)
    if not name:
        return None
    hint: Optional[int] = None
    try:
        hint = int(str(getattr(audio_cfg, "Device", "")).strip())
    except ValueError:
        pass
    return AudioDeviceIdentity(
        name=normalize_device_name(name),
        hostapi_name=getattr(audio_cfg, "DeviceHostApi", None) or None,
        location=getattr(audio_cfg, "DeviceLocation", None) or None,
        index_hint=hint,
    )


def resolve_identity(
    identity: AudioDeviceIdentity,
    devices: List[Dict[str, Any]],
) -> DeviceMatch:
    """Find `identity` in a fresh device listing.

    Match order: name + host API + location, then name + host API, then name
    alone. The saved index is never trusted by itself; it only breaks a tie
    between otherwise identical candidates. Returns no device (MATCH_NONE or
    MATCH_AMBIGUOUS) rather than guessing.
    """
    named = [d for d in devices if normalize_device_name(d.get("name")) == identity.name]
    if not named:
        return DeviceMatch(None, MATCH_NONE, f"no input device named '{identity.name}'")

    if identity.hostapi_name:
        same_api = [d for d in named if d.get("hostapi_name") == identity.hostapi_name]
    else:
        same_api = list(named)
    pool, level = (same_api, MATCH_NAME_HOSTAPI) if same_api else (named, MATCH_NAME)

    if identity.location:
        at_location = [d for d in pool if d.get("location") == identity.location]
        if at_location:
            pool = at_location
            if level == MATCH_NAME_HOSTAPI:
                level = MATCH_EXACT
        elif any(d.get("location") for d in pool):
            # The device exists, but not at the saved port: do not treat a
            # same-named device on a different port as the same one when the
            # location is known on both sides.
            return DeviceMatch(
                None, MATCH_NONE,
                f"'{identity.name}' not found at {identity.location} "
                f"(found at {sorted({d.get('location') for d in pool if d.get('location')})})",
            )

    if len(pool) == 1:
        return DeviceMatch(pool[0], level, _detail(level, identity, pool[0]))

    if identity.index_hint is not None:
        by_hint = [d for d in pool if d.get("index") == identity.index_hint]
        if len(by_hint) == 1:
            return DeviceMatch(
                by_hint[0], MATCH_INDEX_TIEBREAK,
                f"{len(pool)} identical devices named '{identity.name}'; "
                f"chose index {identity.index_hint} from the saved index",
            )
    return DeviceMatch(
        None, MATCH_AMBIGUOUS,
        f"{len(pool)} devices match '{identity.name}' and cannot be told apart "
        f"(indices {[d.get('index') for d in pool]})",
    )


def _detail(level: str, identity: AudioDeviceIdentity, device: Dict[str, Any]) -> str:
    if level == MATCH_NAME:
        return (
            f"'{identity.name}' found, but under host API {device.get('hostapi_name')!r} "
            f"instead of the saved {identity.hostapi_name!r}"
        )
    return f"matched '{identity.name}' by {level}"
