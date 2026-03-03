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
DEFAULT_FPS_CANDIDATES = (10, 15, 24, 25, 30, 50, 60, 120)

# Pixel format
PIXEL_FORMAT_OPTIONS = ("MJPG", "YUYV")
DEFAULT_PIXEL_FORMAT = "YUYV"
FFMPEG_PIXEL_FORMATS = (
    "monob",
    "rgb555be",
    "rgb555le",
    "rgb565be",
    "rgb565le",
    "rgb24",
    "bgr24",
    "0rgb",
    "bgr0",
    "0bgr",
    "rgb0",
    "bgr48be",
    "uyvy422",
    "yuva444p",
    "yuva444p16le",
    "yuv444p",
    "yuv422p16",
    "yuv422p10",
    "yuv444p10",
    "yuv420p",
    "nv12",
    "yuyv422",
    "gray",
)
# UI -> AVFoundation pixel format mapping for macOS ffmpeg
MAC_PIXEL_FORMAT_MAP = {
    "YUYV": "yuyv422",
    "MJPG": "mjpeg",
}
INV_MAC_PIXEL_FORMAT_MAP = {
    "yuyv422": "YUYV",
    "mjpeg": "MJPG",
    "mjpg": "MJPG",
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
