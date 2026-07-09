import re
import sys

from ..utils.constants import _cache_path

# Path to JSON file in project .cache directory where camera device capabilities are saved
CAMERA_CAPS_CACHE = _cache_path() / "camera_capabilities.json"

# Detect operating system
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform.startswith("win")

# Regular expression pattern for video devnodes
DEVNODE_PATTERN = re.compile(r"/dev/video(\d+)$")

# Video display ranges and defaults
WIDTH_RANGE = (1, 7680)
DEFAULT_WIDTH = 1280
HEIGHT_RANGE = (1, 4320)
DEFAULT_HEIGHT = 780
DEFAULT_BRIGHTNESS = 128
DEFAULT_HUE = 0
DEFAULT_SATURATION = 100
BRIGHTNESS_RANGE = (0, 255)
HUE_RANGE = (-180, 180)
SATURATION_RANGE = (0, 200)

# Frame rate (frames per second)
DEFAULT_CAMERA_FPS = 30
DEFAULT_PREVIEW_FPS = 15

# Pixel format
DEFAULT_PIXEL_FORMAT = "YUYV"
# UI -> AVFoundation pixel format mapping for macOS ffmpeg
PIXEL_FORMAT_MAP = {
    "YUYV": "yuyv422",
    "UYVY": "uyvy422",
    "NV12": "nv12",
    "BGRA": "bgra",
    "MJPG": "mjpeg",
}

# UI -> v4l2 control label mapping
V4L2_CONTROL_MAP = {
    "auto_exposure": "exposure_auto",
    "auto_focus": "focus_auto",
}
V4L2_AUTO_EXPOSURE_MODE = 0
V4L2_MANUAL_EXPOSURE_MODE = 1
V4L2_AUTO_FOCUS_MODE = 1
V4L2_MANUAL_FOCUS_MODE = 0

# Other ffmpeg parameters
# Camera controls not supported via ffmpeg
FFMPEG_UNSUPPORTED_CONTROLS = (
    "auto_exposure",
    "auto_focus",
)
FFMPEG_PROBE_DURATION_SEC = 0.1
# Hard cap on how long a single ffmpeg probe subprocess may run before being killed:
# AVFoundation can hang on some device/mode/pixel-format combinations
# instead of erroring out quickly, so probing needs a timeout to avoid stalling indefinitely.
FFMPEG_PROBE_TIMEOUT_SEC = 5
