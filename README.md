# lsl-av-recorder

Desktop GUI recorder for synchronized audio/video stream recording and XDF writing.

## Table of Contents
- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the App](#running-the-app)
- [Camera Controls and Capability Detection](#camera-controls-and-capability-detection)
- [LSL Integration](#lsl-integration)
- [Configuration Files (.cfg)](#configuration-files-cfg)
- [Output Files and Naming](#output-files-and-naming)
- [Recording/Data Flow](#recordingdata-flow)

## Overview
- GUI for session metadata, audio settings, and per-camera setup.
- Live preview wall for up to 4 cameras.
- Camera control application (FPS, resolution, pixel format, exposure/focus toggles, and color controls where supported).
- Video stream recording.
- Audio stream capture via `sounddevice`.
- XDF writing through the in-app `XDFWriter`.

## Requirements
- Python 3.10+
- OS-specific camera dependency:
  - Linux: `v4l-utils` (`v4l2-ctl`)
  - macOS: `ffmpeg` (used only to enumerate available cameras via AVFoundation's device list); `pyobjc-framework-AVFoundation` (native camera capability querying)
  - Windows: `pygrabber` (DirectShow via COM; installed automatically via pip)

## Installation
Use the appropriate setup script for your platform. In addition to the platform-specific installation steps documented below, both scripts install a Python virtual environment (`.venv`) for this application.

### Linux and MacOS:
```bash
./setup.sh
```

`setup.sh` does the following:
- Linux:
  - checks/installs `v4l-utils` via `apt-get`, `dnf`, or `pacman`
- macOS:
  - checks/installs `ffmpeg` via Homebrew

After initial installation, only the project's virtual environment needs to be activated before running the application:
```bash
source .venv/bin/activate
```

### Windows
```powershell
.\setup.ps1
```

`setup.ps1` does the following:
- downloads the latest LabRecorder Windows release

If PowerShell blocks script execution, run once as Administrator:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

After initial installation, only the project's virtual environment needs to be activated before running the application:
```powershell
.\.venv\Scripts\Activate.ps1
```

## Running the App
Start the GUI:

```bash
python main.py
```

Startup behavior:
Unless `main.py` is run with a path to a config `.cfg` file, the default `example.cfg` config is loaded into the GUI upon startup.

To load a different config in normal GUI use:
- Click **Load config** in the main window.

## Camera Controls and Capability Detection
Camera capability discovery and configuration is platform-specific. Depending on the platform, some camera controls may not be supported. Furthermore, the same physical camera may report different capabilities (supported pixel formats, resolutions, and FPS) on different operating systems.

### Linux
Uses `v4l2-ctl` to query and control camera settings:
- Supported pixel formats
- Supported FPS values
- Control ranges for brightness/hue/saturation
- Toggle enable/disable of `exposure_auto` and `focus_auto`

### macOS
Uses native AVFoundation (via `pyobjc-framework-AVFoundation`) to query each camera's `AVCaptureDevice.formats()` directly:
- Supported pixel formats, resolutions, and FPS values are read straight from the device's declared capture formats
- Brightness/saturation/hue are applied in software to captured frames, not via a camera-level control
- Auto-exposure / auto-focus are treated as unsupported

### Windows
On Windows, device capabilities are probed and/or tested empirically on first use, which may take up to a few minutes. These device capabilities are then cached and reused for future sessions, such that this capability probing/test step only runs once per device. This probe additionally runs again when the application version has changed from the version in the cache.

Interfaces with `DirectShow` via `COM` (through `pygrabber`) for both capability discovery and frame capture:
- Supported pixel formats, resolutions, and FPS are queried from the device, and the declared max FPS per mode is empirically verified (and corrected down if the declared maximum frame rate cannot be verified)
- Brightness/hue/saturation and auto-exposure/auto-focus are applied via `COM` camera-control interfaces
- Resolution/FPS/pixel format have no separate "Apply settings" pre-flight step as in Linux and MacOS; instead, they are applied when the capture opens at preview/recording start

## LSL Integration
- Every camera's frame index and timestamp are published live as its own LSL outlet (`VideoFrames_cam-XX_role-<label>`, type `VideoFrame`) for the lifetime of the camera worker, independent of the in-app XDF writer, so external LSL clients can also record video timing.
- Audio is not published as an LSL outlet; its samples are timestamped against the LSL clock (`pylsl.local_clock()`) and written directly into the in-app XDF file for synchronization with other streams.
- Additional LSL streams (e.g. EEG, eye tracking) are found on the network and folded into the same XDF file:
  - **Discover streams** (LabRecorder tab) resolves currently broadcasting LSL outlets via `pylsl.resolve_streams()`.
  - Streams checked in the resulting table each get their own `StreamInlet`, pulling samples into the XDF file for the duration of the run.
- **LabRecorder RCS**: the LabRecorder tab's host/port fields and Connect/Disconnect buttons open a socket to a separately running LabRecorder instance's Remote Control Server, independent of this app's own Start/Stop and XDF writing.

## Configuration Files (.cfg)
Configuration files are INI-style and expected to be saved as `.cfg` files.

### Supported Keys

#### `[Session]`
- `ExperimentName`
- `Subject`
- `Session`
- `Block`
- `Acquisition`
- `Run`

#### `[Output]`
- `StudyRoot`
- `PathTemplate`

#### `[Audio]`
- `Enabled`
- `Device`
- `SampleRate`
- `BitDepth`
- `Channels`
- `StreamName`

#### `[Video]`
- `Enabled`
- `MaxCams`
- `Codec`
- `Container`
- `PreviewFPS`

#### `[VideoCamX]`
- `Enabled`
- `DeviceIndex`
- `DevNode`
- `Label`
- `FPS`
- `Width`
- `Height`
- `AutoExposure`
- `AutoFocus`
- `Brightness`
- `Hue`
- `Saturation`
- `PixelFormat`

## Output Files and Naming
Path templating uses tokens:
- `%p` participant (Subject)
- `%s` session
- `%b` block/task
- `%a` acquisition
- `%r` run

- Base directory and base name from `Output.PathTemplate`

Created artifacts include:
- XDF file containing audio and any LSL streams
- Video `.mp4` files per camera
- Session metadata JSON: `<base_name>_session.json`
- Full session log: `run.log`

## Recording/Data Flow
On **Start**:
1. GUI values are pulled into in-memory config.
2. `RunController` initializes audio/video stream components.
3. Streams are started.
4. `XDFWriter` starts and stream headers are added.
5. Preview manager recording requests are issued for enabled cameras.

Audio flow:
- `sounddevice.InputStream` callback captures chunks and timestamps.
- Samples are forwarded to `RunController` callback and written into XDF.

Video flow:
- Preview workers (`CameraWorker`) handle live frames and optional file writes.
- `RunController` also initializes `VideoRecorder` instances for video capture and frame callbacks into XDF.
- Video frame index/timestamp data is written to XDF as video stream samples.

XDF flow:
- `XDFWriter` writes file header, stream headers, boundary/sample chunks, and stream footers.

