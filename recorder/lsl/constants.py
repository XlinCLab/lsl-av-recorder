from ..utils.constants import _cache_path

STATUS_COLORS = {
    "GREEN": "#1e7d1e",
    "YELLOW": "#b8860b",
    "RED": "#c62828",
}

# Path to JSON file in the project .cache directory
# where per-device Ref/GND baselines are saved
REFGND_BASELINE_CACHE = _cache_path() / "refgnd_baselines.json"

# Mains frequency:
# 50 Hz (Europe/most of the world) vs. 60 Hz (North America)
DEFAULT_LINE_FREQ = 50.0

# Analysis window: 1.0s puts the line frequency exactly on an FFT bin
DEFAULT_WINDOW_SEC = 1.0
# How often the live display updates
DEFAULT_HOP_SEC = 0.25
# Number of trailing windows the displayed A50/CMI are smoothed (median) over
DEFAULT_SMOOTH_WINDOWS = 8

# |sample| at or above this counts as front-end saturation ("rail")
# NB: Highly amplifier-specific; requires adjustment per device
DEFAULT_SAT_LEVEL = 170000.0

# Channel-name substrings (case-insensitive) used to auto-detect channel roles when no manual override is given
COUNTER_PATTERNS = ("packet", "counter", "sample_id", "seq")
NONEEG_PATTERNS = ("acc", "gyro", "mag", "trigger", "batt", "impedance", "aux", "event")

# Default verdict thresholds, used when no baseline has been recorded yet.
DEFAULT_A50_WARN = 5.0
DEFAULT_CMI_WARN = 0.35
DEFAULT_CMI_BAD = 0.60
# Once a baseline exists, its measured noise floor is scaled by this factor to set the A50 threshold instead
DEFAULT_A50_FACTOR = 3.0

DEFAULT_BASELINE_DURATION_S = 60
