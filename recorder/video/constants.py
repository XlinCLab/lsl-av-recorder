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
# Common, human-recognizable frame rates to snap noisy measurements to, 
# so that repeated probes of the same device converge on a stable, 
# reproducible value instead of e.g. 29.1 vs 30.6 depending on measurement jitter.
COMMON_FPS_VALUES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 24, 25, 30, 50, 60, 90, 120]

# Pixel format
DEFAULT_PIXEL_FORMAT = "YUYV"

# UI -> v4l2 control label mapping
V4L2_CONTROL_MAP = {
    "auto_exposure": "exposure_auto",
    "auto_focus": "focus_auto",
}
V4L2_AUTO_EXPOSURE_MODE = 0
V4L2_MANUAL_EXPOSURE_MODE = 1
V4L2_AUTO_FOCUS_MODE = 1
V4L2_MANUAL_FOCUS_MODE = 0
AUTO_VALUE_BY_CONTROL = {
    "auto_exposure": V4L2_AUTO_EXPOSURE_MODE,
    "auto_focus": V4L2_AUTO_FOCUS_MODE,
}

# DirectShow (Windows) VideoProcAmp/CameraControl property IDs, from the Windows SDK's strmif.h. 
# Passed as the `Property` argument to IAMVideoProcAmp::GetRange/Set/Get (brightness/hue/saturation)
# and IAMCameraControl::GetRange/Set/Get (exposure/focus).
# Values are non-contiguous because they're fixed IDs from each interface's full property enum 
# (which also covers e.g. Contrast, Iris, Zoom), not local indices.
VIDEO_PROC_AMP_BRIGHTNESS = 0
VIDEO_PROC_AMP_HUE = 2
VIDEO_PROC_AMP_SATURATION = 3
CAMERA_CONTROL_EXPOSURE = 4
CAMERA_CONTROL_FOCUS = 6

# DirectShow (Windows) VideoProcAmp/CameraControl flag values, from the Windows
# SDK's strmif.h, passed to IAMVideoProcAmp::Set (brightness/hue/saturation)
# and IAMCameraControl::Set/GetRange (exposure/focus) to say whether a value 
# is being driven manually or automatically.
# The two "Manual" values happen to share the same number (0x0002) but come from separate SDK enums.
VIDEO_PROC_AMP_FLAGS_MANUAL = 0x0002
CAMERA_CONTROL_FLAGS_AUTO = 0x0001
CAMERA_CONTROL_FLAGS_MANUAL = 0x0002

# Camera controls AVFoundation doesn't expose a standard toggle for on macOS
MAC_UNSUPPORTED_CONTROLS = (
    "auto_exposure",
    "auto_focus",
)
