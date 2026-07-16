# lsl-av-recorder

Desktop GUI recorder for synchronized audio/video stream recording and XDF writing.

## Table of Contents
- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the App](#running-the-app)
- [Camera Controls and Capability Detection](#camera-controls-and-capability-detection)
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
  - macOS: `ffmpeg` (AVFoundation input)
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
Capability discovery is platform-specific and depending on the platform, some camera controls may not be supported.

### Linux
Uses `v4l2-ctl` to control camera settings:
- Supported pixel formats
- Supported FPS values
- Control ranges for brightness/hue/saturation
- Toggle enable/disable of `exposure_auto` and `focus_auto`

### macOS
Uses `ffmpeg` AVFoundation probing:
- Supported modes parsed by forcing unsupported framerate probe
- Pixel formats are probed against valid mode/FPS combinations
- Brightness/saturation/hue are applied via `ffmpeg` filter chain
- Auto-exposure / auto-focus are treated as unsupported

### Windows
Interfaces with `DirectShow` via `COM` (through `pygrabber`) for both capability discovery and frame capture:
- Supported pixel formats, resolutions, and FPS are queried from the device, and the declared max FPS per mode is empirically verified (and corrected down if the declared maximum frame rate cannot be verified)
- Brightness/hue/saturation and auto-exposure/auto-focus are applied via `COM` camera-control interfaces
- Resolution/FPS/pixel format have no separate "Apply settings" pre-flight step as in Linux and MacOS; instead, they are applied when the capture opens at preview/recording start

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

