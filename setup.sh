#!/bin/bash
set -euo pipefail

# Install OS-specific camera dependencies
OS="$(uname -s)"

# Linux installation
if [[ "${OS}" == "Linux" ]]; then
  if command -v v4l2-ctl >/dev/null 2>&1; then
    echo "v4l-utils already installed"
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y v4l-utils
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y v4l-utils
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm v4l-utils
  else
    echo "Could not detect a supported Linux package manager. Please install v4l-utils manually."
    exit 1
  fi

  if command -v apt-get >/dev/null 2>&1; then
    # LSL dependencies on Ubuntu
    sudo apt-get update
    sudo apt-get install -y qt6-base-dev freeglut3-dev

    # Install liblsl .deb for supported Ubuntu versions
    if [[ -f /etc/os-release ]]; then
      . /etc/os-release
      case "${VERSION_ID:-}" in
        "24.04")
          LSL_DEB_URL="https://github.com/sccn/liblsl/releases/download/v1.17.4/liblsl-1.17.4-noble_amd64.deb"
          ;;
        "22.04")
          LSL_DEB_URL="https://github.com/sccn/liblsl/releases/download/v1.17.4/liblsl-1.17.4-jammy_amd64.deb"
          ;;
        *)
          LSL_DEB_URL=""
          ;;
      esac
    fi

    if [[ -n "${LSL_DEB_URL:-}" ]]; then
      if ! ldconfig -p 2>/dev/null | grep -q liblsl; then
        if command -v curl >/dev/null 2>&1; then
          curl -L "${LSL_DEB_URL}" -o /tmp/liblsl.deb
          sudo dpkg -i /tmp/liblsl.deb || sudo apt-get -f install -y
        else
          echo "curl is required to download liblsl. Please install curl and re-run setup."
          exit 1
        fi
      else
        echo "liblsl already installed"
      fi
    else
      echo "Unsupported Ubuntu version for automated liblsl install. Please install liblsl manually.\n \
      See: https://github.com/labstreaminglayer/App-LabRecorder?tab=readme-ov-file#linux-ubuntu"
    fi
  else
    echo "Non-Ubuntu Linux detected. Please install liblsl and Qt dependencies manually."
  fi

# MacOS installation
elif [[ "${OS}" == "Darwin" ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required to install dependencies on macOS. Please install Homebrew, then re-run setup."
    exit 1
  fi

  if ! brew list --formula ffmpeg >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "ffmpeg already installed"
  fi

  if ! brew list --formula lsl >/dev/null 2>&1; then
    brew install labstreaminglayer/tap/lsl
  else
    echo "lsl already installed"
  fi

  if ! brew list --formula qt >/dev/null 2>&1; then
    brew install qt
  else
    echo "qt already installed"
  fi

  if ! brew list --formula labrecorder >/dev/null 2>&1; then
    brew install labrecorder
  else
    echo "labrecorder already installed"
  fi
else
  echo "Unsupported OS: ${OS}"
  exit 1
fi

# Create Python3 virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
