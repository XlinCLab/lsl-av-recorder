"""Live proxy for EEG reference/ground (Ref/GND) contact quality over LSL.

This does NOT measure impedance in ohms as that would require injecting
a known current and reading the voltage response, which only the amplifier's
own firmware can do. Instead it measures a consequence of a bad Ref/GND contact:
the front-end's ability to reject common-mode interference (mains hum).

Three indicators per analysis window:
  - A50: mains-hum amplitude (median across channels).
  - CMI: Common-Mode Index in [0, 1]. The complex Fourier coefficient at the
    mains frequency, per channel; CMI = |mean_k c_k| / mean_k |c_k|.
    Hum that is the same amplitude AND phase on every channel (CMI -> 1) is
    the signature of an unrejected common-mode signal, i.e. Ref/GND,
    as a single bad channel raises A50 but leaves CMI low, since that
    single channel's noise does not correlate with others.
  - RAIL: fraction of samples near the front-end's saturation limit.

Thresholds are highly device/room-specific.
Record a baseline against a setup already known to be good;
live values are then compared against that baseline.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .constants import REFGND_BASELINE_CACHE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def line_coefficients(X: np.ndarray, fs: float, f0: float) -> np.ndarray:
    """Complex Fourier coefficient at f0, per channel. X: (n_ch, n_samples)."""
    n = X.shape[1]
    t = np.arange(n) / fs
    phasor = np.exp(-2j * np.pi * f0 * t)
    Xc = X - X.mean(axis=1, keepdims=True)
    return 2.0 * (Xc @ phasor) / n


def common_mode_index(coeffs: np.ndarray) -> float:
    """Bias-corrected: ~0 = no common mode, ~1 = fully shared.

    The raw ratio has, under the null hypothesis (independent phases), an
    expected value of 0.886/sqrt(N) : 0.40 at 5 channels, 0.08 at 128.
    Since E[raw^2] = 1/N, that bias can be subtracted out; only then are
    baselines comparable across montages with different channel counts.
    """
    n = coeffs.size
    if n < 2:
        return 0.0
    denom = float(np.mean(np.abs(coeffs)))
    if denom <= 0.0:
        return 0.0
    raw = float(np.abs(np.mean(coeffs)) / denom)
    corr = (raw ** 2 - 1.0 / n) / (1.0 - 1.0 / n)
    return float(np.sqrt(max(0.0, corr)))


def counter_gaps(counter: np.ndarray, wrap: Optional[int]) -> int:
    """Number of missing packets within the window, from a packet counter
    channel that may wrap around (e.g. at 256 or 65536)."""
    if counter.size < 2:
        return 0
    d = np.diff(counter.astype(np.int64))
    if wrap is None:
        span = counter.max() - counter.min()
        wrap = 256 if span < 256 else 65536
    d = np.mod(d, wrap)
    return int(np.sum(d[d > 1] - 1))


def analyze_window(
    X: np.ndarray,
    fs: float,
    counter: Optional[np.ndarray] = None,
    wrap: Optional[int] = None,
    f0: float = 50.0,
    sat: float = 170000.0,
) -> Dict[str, Any]:
    """Metrics for one window. X: (n_ch, n_samples), EEG channels only."""
    coeffs = line_coefficients(X, fs, f0)
    amps = np.abs(coeffs)
    rail = float(np.mean(np.abs(X) > sat))
    gaps = counter_gaps(counter, wrap) if counter is not None else 0
    median_amp = float(np.median(amps))
    return {
        "a50_med": median_amp,
        "a50_max": float(np.max(amps)),
        "cmi": common_mode_index(coeffs),
        "rail": rail,
        "gaps": gaps,
        "n_hot": int(np.sum(amps > 3.0 * median_amp)) if median_amp > 0 else 0,
    }


def verdict(m: Dict[str, Any], thr: Dict[str, float]) -> tuple[str, str]:
    """Traffic light + short reason."""
    if m["gaps"] > 0:
        return "RED", "Packet loss -- re-prep Ref/GND immediately"
    if m["rail"] > 0.001:
        return "RED", "Front-end saturated"
    if m["cmi"] >= thr["cmi_bad"] and m["a50_med"] >= thr["a50_warn"]:
        return "RED", "Hum is in-phase across all channels -> Ref/GND"
    if m["cmi"] >= thr["cmi_warn"] and m["a50_med"] >= thr["a50_warn"]:
        return "YELLOW", "Common mode rising -- watch Ref/GND"
    if m["a50_med"] >= thr["a50_warn"]:
        return "YELLOW", f"Hum elevated, but channel-local ({m['n_hot']} channel(s))"
    return "GREEN", "Common mode is being rejected"


def smoothed(hist: Sequence[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    """Median over the trailing windows; rail and gaps stay as the current
    (unsmoothed) value since those are meant to catch a fault immediately."""
    out = dict(current)
    out["a50_med"] = float(np.median([h["a50_med"] for h in hist]))
    out["cmi"] = float(np.median([h["cmi"] for h in hist]))
    return out


# ---------------------------------------------------------------------------
# Channel-role detection
# ---------------------------------------------------------------------------


def match_pattern(label: str, patterns: Sequence[str]) -> bool:
    low = label.lower()
    return any(p in low for p in patterns)


def detect_counter_index(labels: Sequence[str], patterns: Sequence[str]) -> Optional[int]:
    """First channel whose label matches a counter-like pattern, or None."""
    for i, label in enumerate(labels):
        if match_pattern(label, patterns):
            return i
    return None


def detect_eeg_indices(
    labels: Sequence[str],
    counter_idx: Optional[int],
    noneeg_patterns: Sequence[str],
) -> List[int]:
    """Every channel except the counter and other non-EEG extras
    (motion, battery, trigger, etc.), by label pattern."""
    return [
        i for i, label in enumerate(labels)
        if i != counter_idx and not match_pattern(label, noneeg_patterns)
    ]


# ---------------------------------------------------------------------------
# Per-device baseline cache
# ---------------------------------------------------------------------------


def _baseline_cache_key(device_name: str) -> str:
    return device_name or "unknown"


def _load_baseline_cache() -> Dict[str, Any]:
    if not REFGND_BASELINE_CACHE.exists():
        return {}
    try:
        data = json.loads(REFGND_BASELINE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_baseline(device_name: str) -> Optional[Dict[str, Any]]:
    """Previously saved baseline summary for this device, if any."""
    data = _load_baseline_cache()
    entry = data.get(_baseline_cache_key(device_name))
    return entry if isinstance(entry, dict) else None


def save_baseline(
        device_name: str,
        a50_values: Sequence[float],
        cmi_values: Sequence[float],
        line_freq: float,
        label: Optional[str] = None,
    ) -> Dict[str, Any]:
    """Summarize a baseline recording's per-window A50/CMI values and save
    them for this device, keyed by device name.
    `label` is an optional free-text nickname (e.g. "Room 3, fresh gel")
    to help recall what/where/when a given baseline was recorded."""
    a50 = np.asarray(a50_values, dtype=np.float64)
    cmi = np.asarray(cmi_values, dtype=np.float64)
    summary = {
        "n_windows": int(a50.size),
        "a50_med": float(np.median(a50)),
        "a50_p95": float(np.percentile(a50, 95)),
        "cmi_med": float(np.median(cmi)),
        "cmi_p95": float(np.percentile(cmi, 95)),
        "line_freq": float(line_freq),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label": (label or "").strip(),
    }
    data = _load_baseline_cache()
    data[_baseline_cache_key(device_name)] = summary
    try:
        REFGND_BASELINE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        REFGND_BASELINE_CACHE.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"Failed to save Ref/GND baseline for device '{device_name}': {exc}")
    return summary


def thresholds_for_baseline(
    baseline: Optional[Dict[str, Any]],
    a50_warn_default: float,
    cmi_warn_default: float,
    cmi_bad_default: float,
    a50_factor: float,
) -> Dict[str, Any]:
    """Verdict thresholds: derived from a saved baseline if one exists,
    otherwise the given defaults."""
    if baseline is None:
        return {
            "a50_warn": a50_warn_default,
            "cmi_warn": cmi_warn_default,
            "cmi_bad": cmi_bad_default,
            "source": "Default (no baseline recorded)",
        }
    return {
        "a50_warn": baseline["a50_p95"] * a50_factor,
        "cmi_warn": max(cmi_warn_default, baseline["cmi_p95"] + 0.10),
        "cmi_bad": max(cmi_bad_default, baseline["cmi_p95"] + 0.20),
        "source": f"Baseline ({baseline.get('timestamp', '?')})",
    }
