"""Tests for the pure logic in recorder.gui.camera_panel that does not require running QApplication."""
from __future__ import annotations

from recorder.gui import camera_panel as camera_panel_module
from recorder.gui.camera_panel import CameraPanel


def test_device_key_builds_identity_tuple():
    """A device dict maps to a (name, index, devnode) identity tuple."""
    key = CameraPanel._device_key(
        {
            "name": "USB Cam",
            "index": 1,
            "devnode": "1",
        }
    )
    assert key == ("USB Cam", 1, "1")


def test_device_key_non_dict_returns_none():
    """A non-dict entry (e.g. the transient empty selection during a refresh) has no identity."""
    assert CameraPanel._device_key("not-a-device") is None
    assert CameraPanel._device_key(None) is None


def test_device_key_missing_fields_use_defaults():
    """Missing fields fall back to neutral defaults ('' name/devnode, -1 index)."""
    assert CameraPanel._device_key({}) == ("", -1, "")


def test_device_key_distinguishes_and_matches_devices():
    """Different cameras produce different keys; the same camera produces the
    same key regardless of dict identity."""
    a = CameraPanel._device_key(
        {
            "name": "Cam",
            "index": 0,
            "devnode": "0",
        }
    )
    b = CameraPanel._device_key(
        {
            "name": "Cam",
            "index": 1,
            "devnode": "1",
        }
    )
    same = CameraPanel._device_key(
        {
            "name": "Cam",
            "index": 0,
            "devnode": "0",
        }
    )
    assert a != b
    assert a == same


# ---------------------------------------------------------------------------
# _resolve_device_identity_by_name
# ---------------------------------------------------------------------------

FAKE_DEVICES = [
    {"index": 0, "name": "FaceTime HD Camera", "devnode": "0"},
    {"index": 1, "name": "External Webcam", "devnode": "1"},
]


class _FakePanel:
    """Minimal stand-in exposing just the instance state
    _resolve_device_identity_by_name reads (self._video_devices), so it can
    be exercised without instantiating a real CameraPanel/QApplication."""

    def __init__(self, devices):
        self._video_devices = devices


def test_resolve_device_identity_by_name_finds_current_index(monkeypatch):
    """Resolves to whatever index/devnode the panel's cached device list
    reports for this name, not the fallback."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)
    panel = _FakePanel(FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name(panel, "External Webcam", 0, "0")
    assert (idx, devnode) == (1, "1")


def test_resolve_device_identity_by_name_falls_back_when_not_found(monkeypatch):
    """A name not present in the cached device list (e.g. unplugged, or
    connected since the list was last cached) falls back to the given
    values rather than raising or silently picking a different device."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)
    panel = _FakePanel(FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name(panel, "Unplugged Cam", 7, "7")
    assert (idx, devnode) == (7, "7")


def test_resolve_device_identity_by_name_falls_back_without_name(monkeypatch):
    """No name to match against (e.g. a brand-new default panel) means no
    lookup is attempted; the fallback is returned as-is."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)
    panel = _FakePanel(FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name(panel, None, 3, "3")
    assert (idx, devnode) == (3, "3")


def test_resolve_device_identity_by_name_skipped_off_mac(monkeypatch):
    """This re-resolution only applies on macOS; elsewhere the fallback is trusted as-is."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", False)
    panel = _FakePanel(FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name(panel, "External Webcam", 5, "/dev/video5")
    assert (idx, devnode) == (5, "/dev/video5")


def test_resolve_device_identity_by_name_does_not_query_live(monkeypatch):
    """This must resolve purely against the panel's cached _video_devices --
    never fall back to a fresh list_video_devices() subprocess call."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)

    def boom():
        raise AssertionError("list_video_devices() must not be called here")

    monkeypatch.setattr(camera_panel_module, "list_video_devices", boom)
    panel = _FakePanel(FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name(panel, "External Webcam", 0, "0")
    assert (idx, devnode) == (1, "1")


# ---------------------------------------------------------------------------
# FPS / resolution / pixel-format cascading selection (_refresh_combo_choices,
# validate_settings) -- exercised against fake combo widgets exposing just the
# QComboBox surface these methods use, per this repo's no-real-widget
# convention.
# ---------------------------------------------------------------------------

class _FakeCombo:
    """Minimal stand-in for QComboBox: an ordered (text, data) item list plus
    a current index, matching just the surface _set_*_choices/_selected_*
    read and write."""

    def __init__(self):
        self._items: list[tuple[str, object]] = []
        self._index = 0
        self._enabled = True

    def blockSignals(self, blocked: bool):
        pass

    def clear(self):
        self._items = []
        self._index = 0

    def addItem(self, text, data=None):
        self._items.append((text, data))

    def setCurrentIndex(self, i):
        self._index = i

    def currentData(self):
        return self._items[self._index][1] if 0 <= self._index < len(self._items) else None

    def currentText(self):
        return self._items[self._index][0] if 0 <= self._index < len(self._items) else ""

    def count(self):
        return len(self._items)

    def itemData(self, i):
        return self._items[i][1]

    def findData(self, value):
        for i, (_, data) in enumerate(self._items):
            if data == value:
                return i
        return -1

    def setEnabled(self, enabled: bool):
        self._enabled = enabled

    def isEnabled(self):
        return self._enabled


class _FakeModePanel:
    """Stand-in exposing just the fps/resolution/pixel_format combos and
    combo-set state that _refresh_combo_choices/validate_settings read and
    write, so the cascading-selection logic can be exercised without a real
    CameraPanel/QApplication. The small helper methods those two rely on
    (_selected_fps, _set_resolution_choices, etc.) are pulled straight from
    CameraPanel itself rather than reimplemented, so tests exercise the real
    production logic end to end."""

    _UNSET_VALUE_LABEL = CameraPanel._UNSET_VALUE_LABEL
    _selected_fps = CameraPanel._selected_fps
    _selected_resolution = CameraPanel._selected_resolution
    _selected_pixel_format = CameraPanel._selected_pixel_format
    _set_fps_choices = CameraPanel._set_fps_choices
    _set_resolution_choices = CameraPanel._set_resolution_choices
    _set_pixel_format_choices = CameraPanel._set_pixel_format_choices
    _combos_matching = CameraPanel._combos_matching
    _sort_resolutions_desc = CameraPanel._sort_resolutions_desc
    _resolution_label = CameraPanel._resolution_label

    def __init__(self, combos: list[tuple[str, int, int, int]]):
        self.fps = _FakeCombo()
        self.resolution = _FakeCombo()
        self.pixel_format = _FakeCombo()
        self._combos = combos
        self._combos_set = set(combos)
        self._modes_by_format: dict = {}


def _select(panel, *, pixel_format=None, resolution=None, fps=None):
    """Seed a fake panel's combos directly to a given selection, bypassing
    _refresh_combo_choices, so tests can set up an arbitrary starting state."""
    panel.pixel_format.clear()
    panel.pixel_format.addItem(str(pixel_format), pixel_format)
    panel.resolution.clear()
    panel.resolution.addItem(str(resolution), resolution)
    panel.fps.clear()
    panel.fps.addItem(str(fps), fps)


# Two combinations that share no resolution and no pixel format at all -- the
# exact "impossible to switch" hypothetical: if 30fps and 60fps supported no
# common resolution, filtering resolution-by-fps-then-fps-by-resolution with
# no escape hatch could deadlock.
DISJOINT_COMBOS = [
    ("YUYV", 1280, 720, 30),
    ("MJPG", 640, 480, 60),
]


def test_refresh_combo_choices_widens_other_controls_after_reset():
    """Changing FPS to a value with no overlap with the current
    resolution/pixel-format must still surface the resolution/format that
    DOES support it as selectable options, not just wipe the selection with
    no way forward. This is the fix for a hypothetical deadlock, whereby
    resetting fps could narrow resolution to nothing without ever revealing
    what resolution the new fps actually needs."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=30)

    panel.fps.clear()
    panel.fps.addItem("60", 60)
    CameraPanel._refresh_combo_choices(panel, changed="fps")

    resolution_options = [data for _text, data in panel.resolution._items]
    pixel_format_options = [data for _text, data in panel.pixel_format._items]
    assert (640, 480) in resolution_options
    assert "MJPG" in pixel_format_options
    # The old (now-incompatible) selections must not be silently kept.
    assert CameraPanel._selected_resolution(panel) != (1280, 720)
    assert CameraPanel._selected_pixel_format(panel) != "YUYV"


def test_refresh_combo_choices_resets_incompatible_selection_to_unset():
    """A selection that is no longer compatible with the just-changed control
    resets to "Not set" rather than jumping to an arbitrary fallback mode --
    the whole point is to make the gap visible, not paper over it."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=30)

    panel.fps.clear()
    panel.fps.addItem("60", 60)
    CameraPanel._refresh_combo_choices(panel, changed="fps")

    assert CameraPanel._selected_resolution(panel) is None
    assert CameraPanel._selected_pixel_format(panel) is None
    # FPS itself (the control the user just set) is left untouched.
    assert CameraPanel._selected_fps(panel) == 60


def test_refresh_combo_choices_keeps_still_compatible_selection():
    """When the current resolution/format selection remains valid for the
    newly-changed control, it is preserved rather than being reset."""
    combos = [("YUYV", 1280, 720, 15), ("YUYV", 1280, 720, 30)]
    panel = _FakeModePanel(combos)
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=15)

    panel.fps.clear()
    panel.fps.addItem("30", 30)
    CameraPanel._refresh_combo_choices(panel, changed="fps")

    assert CameraPanel._selected_resolution(panel) == (1280, 720)
    assert CameraPanel._selected_pixel_format(panel) == "YUYV"


def test_refresh_combo_choices_resetting_first_processed_control_widens_the_next():
    """Resetting pixel format -- the first control resolved in the internal
    ordering (pixel format, then resolution, then FPS) -- to "Not set"
    removes it as a constraint entirely, so resolution (resolved next)
    widens to show every resolution across every format, not just whatever
    the previously-selected format supported. Resolution's own prior
    selection is still individually valid (1280x720 exists under YUYV), so
    it's kept rather than reset."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=30)

    panel.pixel_format.clear()
    panel.pixel_format.addItem(CameraPanel._UNSET_VALUE_LABEL, None)
    CameraPanel._refresh_combo_choices(panel, changed="pixel_format")

    resolution_options = {data for _text, data in panel.resolution._items}
    assert resolution_options == {None, (1280, 720), (640, 480)}
    assert CameraPanel._selected_resolution(panel) == (1280, 720)
    assert CameraPanel._selected_pixel_format(panel) is None


def test_refresh_combo_choices_initial_load_keeps_a_jointly_valid_selection():
    """changed=None (a freshly loaded config, nothing explicitly touched by
    the user yet) keeps a pre-existing selection intact when all three
    values together already form a real verified combination."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=30)

    CameraPanel._refresh_combo_choices(panel, changed=None)

    assert CameraPanel._selected_pixel_format(panel) == "YUYV"
    assert CameraPanel._selected_resolution(panel) == (1280, 720)
    assert CameraPanel._selected_fps(panel) == 30


def test_refresh_combo_choices_initial_load_narrows_an_incompatible_selection():
    """changed=None with a config whose stored resolution doesn't actually
    belong to the stored pixel format progressively narrows rather than
    leaving an invalid triple selected: pixel format is resolved first (so
    it's kept, since YUYV is a real format on its own), but resolution is
    then resolved against that settled pixel format and reset, since
    640x480 only exists under MJPG, not YUYV. FPS, resolved last, is kept
    since it's compatible with the settled pixel format regardless of
    resolution."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    # YUYV is real, but 640x480 only exists under MJPG -- not a real pair.
    _select(panel, pixel_format="YUYV", resolution=(640, 480), fps=30)

    CameraPanel._refresh_combo_choices(panel, changed=None)

    assert CameraPanel._selected_pixel_format(panel) == "YUYV"
    assert CameraPanel._selected_resolution(panel) is None
    assert CameraPanel._selected_fps(panel) == 30


def test_validate_settings_flags_missing_selections():
    """Apply/Start must be blocked with a clear message naming exactly which
    control(s) still need a value, not a generic failure."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format=None, resolution=(1280, 720), fps=30)

    messages = CameraPanel.validate_settings(panel)
    assert len(messages) == 1
    assert "pixel format" in messages[0]


def test_validate_settings_passes_for_a_real_verified_combo():
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=30)
    assert CameraPanel.validate_settings(panel) == []


def test_validate_settings_flags_unverified_combo():
    """Even with all three set, a combination that was never actually
    confirmed by the capability probe is rejected -- defense in depth in
    case something bypasses the cascading combo population."""
    panel = _FakeModePanel(DISJOINT_COMBOS)
    _select(panel, pixel_format="YUYV", resolution=(640, 480), fps=60)
    messages = CameraPanel.validate_settings(panel)
    assert len(messages) == 1
    assert "not a confirmed-supported combination" in messages[0]


def test_validate_settings_flags_missing_even_with_no_combos_loaded():
    """An unselected control is worth flagging on its own, even before
    capabilities have ever been probed (self._combos empty) -- otherwise a
    camera could reach Start/Apply with nothing chosen at all just because
    refresh_capabilities() was never clicked."""
    panel = _FakeModePanel([])
    _select(panel, pixel_format=None, resolution=None, fps=None)
    messages = CameraPanel.validate_settings(panel)
    assert len(messages) == 1
    assert "pixel format" in messages[0] and "resolution" in messages[0] and "FPS" in messages[0]


def test_validate_settings_skips_combo_check_when_no_combos_loaded():
    """With no capability data at all (never refreshed) but a selection
    made anyway (e.g. from a loaded config), there is nothing to
    cross-check that selection against, so no false-positive "not a
    confirmed-supported combination" message is raised."""
    panel = _FakeModePanel([])
    _select(panel, pixel_format="YUYV", resolution=(1280, 720), fps=30)
    assert CameraPanel.validate_settings(panel) == []
