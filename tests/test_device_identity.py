"""Tests for recorder.audio.device_identity: matching a saved audio device
against a fresh enumeration after PortAudio indices and ALSA card numbers shift."""
from __future__ import annotations

from recorder.audio.device_identity import (MATCH_AMBIGUOUS, MATCH_EXACT,
                                            MATCH_INDEX_TIEBREAK, MATCH_NAME,
                                            MATCH_NAME_HOSTAPI, MATCH_NONE,
                                            AudioDeviceIdentity,
                                            alsa_card_number, device_location,
                                            identity_for,
                                            normalize_device_name,
                                            parse_asound_cards,
                                            resolve_identity)

ASOUND_CARDS = """\
 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xf7340000 irq 136
 1 [BRIO           ]: USB-Audio - Logitech BRIO
                      Logitech BRIO at usb-0000:00:14.0-9, high speed
 3 [BRIO_1         ]: USB-Audio - Logitech BRIO
                      Logitech BRIO at usb-0000:00:14.0-10, high speed
 4 [USB            ]: USB-Audio - Scarlett 2i2 USB
                      Focusrite Scarlett 2i2 USB at usb-0000:07:00.0-2, high speed
"""


def _dev(index, name, hostapi_name="ALSA", location=None):
    d = {"index": index, "name": name, "hostapi_name": hostapi_name}
    if location:
        d["location"] = location
    return d


# --- name handling -----------------------------------------------------------

def test_normalize_strips_alsa_card_suffix():
    assert normalize_device_name("Scarlett 2i2 USB: Audio (hw:4,0)") == "Scarlett 2i2 USB: Audio"
    assert normalize_device_name("Scarlett 2i2 USB: Audio (hw:6,0)") == "Scarlett 2i2 USB: Audio"
    assert normalize_device_name("Mic (plughw:2,1)") == "Mic"


def test_normalize_keeps_other_parentheses_and_handles_none():
    assert normalize_device_name("Microphone (Realtek Audio)") == "Microphone (Realtek Audio)"
    assert normalize_device_name(None) == ""


def test_alsa_card_number():
    assert alsa_card_number("Logitech BRIO: USB Audio (hw:3,0)") == 3
    assert alsa_card_number("MacBook Pro Microphone") is None


# --- location (Linux /proc/asound/cards) -------------------------------------

def test_parse_asound_cards_maps_usb_cards_only():
    assert parse_asound_cards(ASOUND_CARDS) == {
        1: "usb-0000:00:14.0-9",
        3: "usb-0000:00:14.0-10",
        4: "usb-0000:07:00.0-2",
    }


def test_device_location_from_name_and_cards_text():
    assert device_location("Scarlett 2i2 USB: Audio (hw:4,0)", ASOUND_CARDS) == "usb-0000:07:00.0-2"
    assert device_location("HDA Intel PCH: ALC671 Analog (hw:0,0)", ASOUND_CARDS) is None
    assert device_location("sysdefault", ASOUND_CARDS) is None


def test_device_location_none_off_linux(monkeypatch):
    monkeypatch.setattr("recorder.audio.device_identity.sys.platform", "darwin")
    assert device_location("Scarlett 2i2 USB: Audio (hw:4,0)") is None


# --- identity_for ------------------------------------------------------------

def test_identity_for_uses_normalized_name_and_keeps_index_as_hint():
    ident = identity_for(_dev(8, "Scarlett 2i2 USB: Audio (hw:4,0)", location="usb-0000:07:00.0-2"))
    assert ident == AudioDeviceIdentity(
        name="Scarlett 2i2 USB: Audio",
        hostapi_name="ALSA",
        location="usb-0000:07:00.0-2",
        index_hint=8,
    )


# --- resolve_identity --------------------------------------------------------

def test_resolves_device_whose_index_and_card_number_shifted():
    saved = identity_for(_dev(10, "Scarlett 2i2 USB: Audio (hw:6,0)", location="usb-0000:07:00.0-2"))
    now = [
        _dev(7, "Logitech BRIO: USB Audio (hw:3,0)", location="usb-0000:00:14.0-10"),
        _dev(8, "Scarlett 2i2 USB: Audio (hw:4,0)", location="usb-0000:07:00.0-2"),
        _dev(10, "front"),
    ]
    match = resolve_identity(saved, now)
    assert match.found and match.device["index"] == 8
    assert match.level == MATCH_EXACT and not match.degraded


def test_index_alone_never_selects_a_different_device():
    """The saved index now belongs to a different device, which must not be chosen."""
    saved = identity_for(_dev(10, "Scarlett 2i2 USB: Audio (hw:6,0)"))
    match = resolve_identity(saved, [_dev(10, "Logitech BRIO: USB Audio (hw:6,0)")])
    assert not match.found and match.level == MATCH_NONE


def test_missing_device_is_reported_not_guessed():
    saved = identity_for(_dev(8, "Scarlett 2i2 USB: Audio (hw:4,0)"))
    match = resolve_identity(saved, [_dev(0, "HDA Intel PCH: ALC671 Analog (hw:0,0)")])
    assert match.device is None and match.level == MATCH_NONE
    assert "Scarlett 2i2 USB: Audio" in match.detail


def test_same_name_under_another_host_api_is_a_degraded_match():
    saved = AudioDeviceIdentity(name="Focusrite", hostapi_name="Windows WASAPI")
    match = resolve_identity(saved, [_dev(3, "Focusrite", hostapi_name="MME")])
    assert match.found and match.level == MATCH_NAME and match.degraded


def test_host_api_picks_between_same_named_entries():
    """On Windows the same physical device is listed once per host API."""
    saved = AudioDeviceIdentity(name="Focusrite", hostapi_name="Windows WASAPI")
    devices = [
        _dev(1, "Focusrite", hostapi_name="MME"),
        _dev(2, "Focusrite", hostapi_name="Windows WASAPI"),
    ]
    match = resolve_identity(saved, devices)
    assert match.device["index"] == 2 and match.level == MATCH_NAME_HOSTAPI


def test_location_distinguishes_identical_devices():
    saved = AudioDeviceIdentity(
        name="Logitech BRIO: USB Audio",
        hostapi_name="ALSA",
        location="usb-0000:00:14.0-10",
    )
    devices = [
        _dev(2, "Logitech BRIO: USB Audio (hw:1,0)", location="usb-0000:00:14.0-9"),
        _dev(7, "Logitech BRIO: USB Audio (hw:3,0)", location="usb-0000:00:14.0-10"),
    ]
    match = resolve_identity(saved, devices)
    assert match.device["index"] == 7 and match.level == MATCH_EXACT


def test_same_name_at_a_different_port_is_not_the_same_device():
    saved = AudioDeviceIdentity(
        name="Logitech BRIO: USB Audio",
        hostapi_name="ALSA",
        location="usb-0000:00:14.0-10",
    )
    devices = [_dev(2, "Logitech BRIO: USB Audio (hw:1,0)", location="usb-0000:00:14.0-9")]
    match = resolve_identity(saved, devices)
    assert not match.found and match.level == MATCH_NONE


def test_identical_devices_without_location_are_ambiguous():
    saved = AudioDeviceIdentity(name="USB Mic", hostapi_name="Core Audio")
    devices = [_dev(1, "USB Mic", "Core Audio"), _dev(2, "USB Mic", "Core Audio")]
    match = resolve_identity(saved, devices)
    assert not match.found and match.level == MATCH_AMBIGUOUS


def test_saved_index_breaks_a_tie_between_identical_devices():
    saved = AudioDeviceIdentity(name="USB Mic", hostapi_name="Core Audio", index_hint=2)
    devices = [_dev(1, "USB Mic", "Core Audio"), _dev(2, "USB Mic", "Core Audio")]
    match = resolve_identity(saved, devices)
    assert match.device["index"] == 2 and match.level == MATCH_INDEX_TIEBREAK and match.degraded


def test_saved_index_that_matches_none_of_the_tied_devices_stays_ambiguous():
    saved = AudioDeviceIdentity(name="USB Mic", hostapi_name="Core Audio", index_hint=9)
    devices = [_dev(1, "USB Mic", "Core Audio"), _dev(2, "USB Mic", "Core Audio")]
    assert resolve_identity(saved, devices).level == MATCH_AMBIGUOUS
