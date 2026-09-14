"""Tests for the pure logic in recorder.gui.validate_capabilities."""
from __future__ import annotations

from recorder.gui.validate_capabilities import (_combinations_for_selection,
                                                _combos_needing_validation,
                                                _effective_result)

# ---------------------------------------------------------------------------
# _combinations_for_selection
# ---------------------------------------------------------------------------

MODES_BY_FORMAT = {
    "MJPG": [
        (1920, 1080, {5, 30, 50}),
        (640, 480, {30, 60, 90}),
    ],
    "YUY2": [
        (1280, 720, {30}),
    ],
}


def test_combinations_for_selection_requires_all_three_axes_checked():
    """A combination is only included if its own pixel format, resolution,
    AND fps are all checked, not merely any one of them."""
    combos = _combinations_for_selection(
        MODES_BY_FORMAT,
        checked_formats={"MJPG"},
        checked_resolutions={(1920, 1080)},
        checked_fps={5, 50},
    )
    assert sorted(combos) == [("MJPG", 1920, 1080, 5), ("MJPG", 1920, 1080, 50)]


def test_combinations_for_selection_excludes_fps_not_declared_for_that_resolution():
    """e.g. 60fps is declared for 640x480 but not for 1920x1080 -- checking both
    the resolution and the fps must not synthesize a combination the device
    never actually declared (no blind cartesian product)."""
    combos = _combinations_for_selection(
        MODES_BY_FORMAT,
        checked_formats={"MJPG"},
        checked_resolutions={(1920, 1080)},
        checked_fps={60},
    )
    assert combos == []


def test_combinations_for_selection_nothing_checked_is_empty():
    combos = _combinations_for_selection(
        MODES_BY_FORMAT,
        checked_formats=set(),
        checked_resolutions=set(),
        checked_fps=set(),
    )
    assert combos == []


def test_combinations_for_selection_spans_multiple_formats():
    combos = _combinations_for_selection(
        MODES_BY_FORMAT,
        checked_formats={"MJPG", "YUY2"},
        checked_resolutions={(1920, 1080), (1280, 720)},
        checked_fps={30},
    )
    assert sorted(combos) == [("MJPG", 1920, 1080, 30), ("YUY2", 1280, 720, 30)]


# ---------------------------------------------------------------------------
# _effective_result
# ---------------------------------------------------------------------------

def test_effective_result_none_when_nothing_recorded():
    assert _effective_result(None, width=1920, height=1080, fps=50, tolerance=0.15) is None


def test_effective_result_unchanged_when_no_measurement_recorded():
    """A result with no measured_fps (e.g. the device couldn't be opened at
    all) has nothing to re-evaluate against a tolerance; returned as-is."""
    result = {"passed": False, "measured_fps": None, "could_open": False}
    assert _effective_result(result, width=1920, height=1080, fps=50, tolerance=0.15) == result


def test_effective_result_keeps_pass_within_tolerance():
    result = {
        "passed": True,
        "measured_fps": 47.0,
        "could_open": True,
        "measured_width": 1920,
        "measured_height": 1080,
    }
    effective = _effective_result(
        result=result,
        width=1920,
        height=1080,
        fps=50,
        tolerance=0.15,
    )
    assert effective["passed"] is True
    # Unchanged object returned when the flag doesn't actually change.
    assert effective is result


def test_effective_result_flips_pass_to_fail_under_stricter_tolerance():
    """e.g. a combination measured at 43fps against a requested
    50fps passed under 15% tolerance but should read as FAIL once the
    tolerance is tightened to 5%, without needing to re-run validation."""
    original_result = {
        "passed": True,
        "measured_fps": 43.0,
        "could_open": True,
        "measured_width": 1920,
        "measured_height": 1080,
    }
    new_result = _effective_result(
        result=original_result,
        width=1920,
        height=1080,
        fps=50,
        tolerance=0.05,
    )
    assert new_result["passed"] is False
    assert new_result != original_result
    assert original_result["passed"] is True


def test_effective_result_flips_fail_to_pass_under_looser_tolerance():
    result = {
        "passed": False,
        "measured_fps": 46.0,
        "could_open": True,
        "measured_width": 1920,
        "measured_height": 1080,
    }
    effective = _effective_result(
        result=result,
        width=1920,
        height=1080,
        fps=50,
        tolerance=0.15,
    )
    assert effective["passed"] is True


def test_effective_result_never_passes_when_device_could_not_open():
    """A measured_fps landing within tolerance is irrelevant if the device
    itself couldn't be opened for this combination."""
    result = {
        "passed": False,
        "measured_fps": 50.0,
        "could_open": False,
        "measured_width": 1920,
        "measured_height": 1080,
    }
    effective = _effective_result(
        result=result,
        width=1920,
        height=1080,
        fps=50,
        tolerance=0.15,
    )
    assert effective["passed"] is False


def test_effective_result_boundary_is_inclusive():
    """Exactly at the tolerance boundary still counts as passed."""
    result = {
        "passed": True,
        "measured_fps": 42.5,
        "could_open": True,
        "measured_width": 1920,
        "measured_height": 1080,
    }
    effective = _effective_result(
        result=result,
        width=1920,
        height=1080,
        fps=50,
        tolerance=0.15,
    )
    assert effective["passed"] is True


def test_effective_result_never_passes_when_resolution_mismatched():
    """A device can silently negotiate a different resolution than requested
    the same way it can silently drop fps; a measured_fps within tolerance
    is irrelevant if the delivered resolution doesn't match."""
    result = {
        "passed": True,
        "measured_fps": 50.0,
        "could_open": True,
        "measured_width": 1280,
        "measured_height": 720,
    }
    effective = _effective_result(
        result=result,
        width=1920,
        height=1080,
        fps=50,
        tolerance=0.15,
    )
    assert effective["passed"] is False


# ---------------------------------------------------------------------------
# _combos_needing_validation
# ---------------------------------------------------------------------------

def _cached(
        pf,
        w,
        h,
        fps,
        passed,
        measured_fps,
        could_open=True,
        measured_width=None,
        measured_height=None,
    ):
    from recorder.video.camera_settings import combination_key
    return combination_key(pf, w, h, fps), {
        "passed": passed,
        "measured_fps": measured_fps,
        "could_open": could_open,
        "measured_width": w if measured_width is None else measured_width,
        "measured_height": h if measured_height is None else measured_height,
    }


def test_combos_needing_validation_skips_already_passing():
    key, result = _cached("MJPG", 1920, 1080, 30, passed=True, measured_fps=29.9)
    cached = {key: result}
    combos = [("MJPG", 1920, 1080, 30)]

    to_test, skipped = _combos_needing_validation(combos, cached, tolerance=0.15)

    assert to_test == []
    assert skipped == 1


def test_combos_needing_validation_retests_failed_and_unvalidated():
    passed_key, passed_result = _cached("MJPG", 1920, 1080, 30, passed=True, measured_fps=29.9)
    failed_key, failed_result = _cached("MJPG", 1920, 1080, 50, passed=False, measured_fps=58.0)
    cached = {passed_key: passed_result, failed_key: failed_result}
    combos = [
        ("MJPG", 1920, 1080, 30),  # already passing: skip
        ("MJPG", 1920, 1080, 50),  # previously failed: retest
        ("MJPG", 1920, 1080, 5),   # never validated: test
    ]

    to_test, skipped = _combos_needing_validation(combos, cached, tolerance=0.15)

    assert sorted(to_test) == [("MJPG", 1920, 1080, 5), ("MJPG", 1920, 1080, 50)]
    assert skipped == 1


def test_combos_needing_validation_retests_when_tolerance_tightened():
    """A combination that passed under a looser tolerance previously is
    re-tested (not skipped) once the requested tolerance is stricter than
    what it was actually measured against."""
    key, result = _cached("MJPG", 1920, 1080, 50, passed=True, measured_fps=43.0)
    cached = {key: result}
    combos = [("MJPG", 1920, 1080, 50)]

    to_test, skipped = _combos_needing_validation(combos, cached, tolerance=0.05)

    assert to_test == [("MJPG", 1920, 1080, 50)]
    assert skipped == 0


def test_combos_needing_validation_empty_input():
    assert _combos_needing_validation([], {}, tolerance=0.15) == ([], 0)


def test_combos_needing_validation_retests_when_resolution_mismatched():
    """A previously 'passed' result whose measured resolution doesn't match
    the requested one must be retested, not skipped; a passing frame rate
    alone does not suffice."""
    key, result = _cached(
        "MJPG", 1920, 1080, 30,
        passed=True,
        measured_fps=29.9,
        measured_width=1280,
        measured_height=720,
    )
    cached = {key: result}
    combos = [("MJPG", 1920, 1080, 30)]

    to_test, skipped = _combos_needing_validation(combos, cached, tolerance=0.15)

    assert to_test == [("MJPG", 1920, 1080, 30)]
    assert skipped == 0
