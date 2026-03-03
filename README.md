# lsl-av-recorder

Desktop GUI recorder for synchronized audio/video stream recording and XDF writing.

## Table of Contents
- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the App](#running-the-app)
- [GUI Walkthrough](#gui-walkthrough)
- [Camera Controls and Capability Detection](#camera-controls-and-capability-detection)
- [Configuration Files (.cfg)](#configuration-files-cfg)
- [Output Files and Naming](#output-files-and-naming)
- [Recording/Data Flow](#recordingdata-flow)
- [Notes and Current Limitations](#notes-and-current-limitations)

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

## Installation
Use the setup script:

```bash
./setup.sh
```

`setup.sh` does the following:
- Linux:
  - checks/installs `v4l-utils` via `apt-get`, `dnf`, or `pacman`
- macOS:
  - checks/installs `ffmpeg` via Homebrew
- Creates `.venv`
- Installs package with:
  - `pip install -U pip`
  - `pip install -e .`

## Running the App
Start the GUI:

```bash
python main.py
```

Startup behavior:
Unless `main.py` is run with a path to a config `.cfg` file, the default `example.cfg` config is loaded into the GUI upon startup.

To load a different config in normal GUI use:
- Click **Load config** in the main window.

## GUI Walkthrough
Main window includes:
- Session metadata fields:
  - `ExperimentName`
  - `Subject (%p)`
  - `Session (%s)`
  - `Block/Task (%b)`
  - `Acquisition (%a)`
  - `Run (%r)`
- Audio tab
- 4 camera tabs (Camera 1..4)
- Live preview panel (2x2)
- Log panel
- Run controls: `Load config`, `Start`, `Stop`

### Audio Tab
Fields:
- Enable audio
- Input device (enumerated from `sounddevice`)
- Sample rate
- Bit depth (16/32/64)
- Channels
- LSL stream name field (currently metadata only for audio stream settings)

### Camera Tabs
Each camera tab includes:
- Enable toggle
- Device selection by name (plus `DeviceIndex` and `DevNode` fields)
- Label
- FPS dropdown
- Resolution dropdown
- Pixel format dropdown (`MJPG`, `YUYV`) with unsupported options disabled
- Brightness / Hue / Saturation (enabled when supported)
- Auto-exposure / Auto-focus toggles (enabled when supported)
- Buttons:
  - `Refresh video devices`
  - `Refresh device capabilities`
  - `Apply settings`

Preview behavior:
- Changing camera selection/settings triggers preview rebind when idle.
- During active run/recording, preview workers are not hot-swapped.

## Camera Controls and Capability Detection
Capability discovery is platform-specific and depending on the platform, some camera controls may not be supported.

### Linux
Uses `v4l2-ctl` to control camera settings:
- Supported pixel formats
- Supported FPS values
- Supported `(resolution, fps)` modes
- Control ranges for brightness/hue/saturation
- Toggle enable/disable of `exposure_auto` and `focus_auto`

### macOS
Uses `ffmpeg` AVFoundation probing:
- Device list
- Supported modes parsed by forcing unsupported framerate probe
- Pixel formats are probed against valid mode/FPS combinations
- Brightness/saturation/hue are applied via `ffmpeg` filter chain
- Auto-exposure / auto-focus are treated as unsupported

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
- Session metadata JSON:
  - `<base_name>_session.json`
- XDF file:
  - rendered from output template
- Video files:
  - preview worker recording path: `<base_name>_cam-XX_role-<label>.<container>`
  - run-controller recorder path: `<base_name>_cam-<label>.<container>`

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

## Notes and Current Limitations
- The app is currently implemented for Linux/macOS camera tooling paths.
- Preview/live reconfiguration is intentionally conservative while recording is active.
