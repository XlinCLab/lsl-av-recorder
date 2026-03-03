import re
import sys

# Detect operating system
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Regular expression pattern for video devnodes
DEVNODE_PATTERN = re.compile(r"/dev/video(\d+)$")

# Video display ranges and defaults
WIDTH_RANGE = (1, 7680)
DEFAULT_WIDTH = 1280
HEIGHT_RANGE = (1, 4320)
DEFAULT_HEIGHT = 780
BRIGHTNESS_RANGE = (-100, 100)
HUE_RANGE = (-180, 180)
SATURATION_RANGE = (0, 200)

# Frame rate (frames per second)
DEFAULT_CAMERA_FPS = 30
DEFAULT_PREVIEW_FPS = 15

# Pixel format
PIXEL_FORMAT_OPTIONS = ("MJPG", "YUYV")
DEFAULT_PIXEL_FORMAT = "YUYV"
# UI -> AVFoundation pixel format mapping for macOS ffmpeg
MAC_PIXEL_FORMAT_MAP = {
    "YUYV": "yuyv422",
    "MJPG": "mjpeg",
}

# UI -> v4l2 control label mapping
V4L2_CONTROL_MAP = {
    "auto_exposure": "exposure_auto",
    "auto_focus": "focus_auto",
}

# Other ffmpeg parameters
# Camera controls not supported via ffmpeg
FFMPEG_UNSUPPORTED_CONTROLS = (
    "auto_exposure",
    "auto_focus",
)
FFMPEG_PROBE_DURATION_SEC = 0.1
