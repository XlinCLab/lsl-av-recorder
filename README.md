# lsl-av-recorder v0.3.2 (Linux Mint / Ubuntu)

- **Video**: up to 4 USB cameras, always-on preview wall (2×2) in the main window
- **Video sync over LSL**: per camera stream `VideoFrames_cam-XX_role-LABEL` (frame index + monotonic time)
- **Audio over LSL (NO WAV)**: audio samples are streamed via LSL; LabRecorder writes them into XDF
- **XDF**: written by **LabRecorder** controlled via **RCS**

## Requirements
- `sudo apt install v4l-utils` (installed as part of `setup.sh` script)
- LabRecorder running with RCS enabled (port 22345)
- Python 3.10+

## Install
```bash
./setup.sh
```

## Run
```bash
python main.py
```
