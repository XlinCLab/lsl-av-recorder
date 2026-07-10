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
  - Windows: `pygrabber` (DirectShow via COM; installed automatically via pip)

## Installation
Use the setup script for your platform:

Linux and MacOS:
```bash
./setup.sh
```

Windows:
```powershell
.\setup.ps1
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

`setup.ps1` does the following:
- downloads the latest LabRecorder Windows release
- creates `.venv`
- installs package with:
  - `pip install -U pip`
  - `pip install -e .`

If PowerShell blocks script execution, run once as Administrator:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
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
- Live preview panel
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
- Device selection by name
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

### Windows
Talks to DirectShow directly via COM (through `pygrabber`), for both capability discovery
and actual frame capture. Devices are addressed by their DirectShow enumeration index:
- Device list from `ICreateDevEnum`/`IEnumMoniker` (`FilterGraph.get_input_devices()`)
- Supported pixel formats, resolutions, and FPS ranges from `IAMStreamConfig::GetStreamCaps`
  (`VideoInput.get_formats()`), per (pixel format, resolution) combination. The declared
  maximum FPS for each combination is not trusted as-is: a brief real capture is opened at
  that rate and the actual delivered frame rate is measured, clamping the reported maximum
  down if the driver's declaration turns out to be optimistic (e.g. a declared 60fps mode
  that only sustains ~30fps in practice) — mirroring how macOS probing doesn't trust
  AVFoundation's self-reported modes without confirming each one actually opens. This makes
  capability refresh noticeably slower (comparable to macOS's own probing cost) but keeps the
  GUI from ever offering a rate the camera can't really deliver.
- Selecting a specific FPS overwrites the matched stream-caps entry's embedded frame interval
  (`avg_time_per_frame`) before applying it — `IAMStreamConfig::GetStreamCaps` returns a media
  type whose embedded rate is that entry's own nominal default (observed to be its declared
  maximum), so without this every FPS within an entry's declared range would otherwise end up
  recording at that same default instead of the one actually selected.
- **Frame capture** (`WindowsDShowVideoCapture` in `recorder/video/dshow_capture.py`) builds
  one persistent DirectShow filter graph per camera: the video source (format selected via
  `IAMStreamConfig::SetFormat`) feeds a `SampleGrabber` filter (requesting RGB24, with
  DirectShow auto-inserting a decoder such as its built-in MJPEG decoder as needed) into a
  null renderer. This replaces `cv2.VideoCapture(..., cv2.CAP_DSHOW)`, because configuring
  the format on one filter graph and then opening a *separate* graph via `cv2.VideoCapture`
  does not reliably carry the format over on all drivers — capture stayed at the device's
  low-fps uncompressed default regardless of what was requested. Capturing frames through
  the same graph that has the format applied avoids that handoff.
- There is no separate "Apply settings" pre-flight step for resolution/FPS/pixel format on
  Windows (the button and the settings pass that runs automatically on **Start** are no-ops
  that report success without touching the device) — these are applied once, for real, when
  the DirectShow graph is built at recording/preview start.
- Brightness/hue/saturation and auto-exposure/auto-focus are **not yet supported** on Windows
  at all (would require wrapping `IAMVideoProcAmp`/`IAMCameraControl`, which `pygrabber` does
  not expose); those controls are disabled in the GUI (capability probing reports them
  unsupported).

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
- Windows support covers device enumeration, capability probing, and recording. 
  There is no pre-flight settings validation (resolution/FPS/pixel format are trusted as-is and applied by
  OpenCV at recording start), and brightness/hue/saturation/auto-exposure/auto-focus controls
  are not yet supported at all — see [Windows](#windows) above.
- Preview/live reconfiguration is intentionally conservative while recording is active.
