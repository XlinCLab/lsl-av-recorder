#!/bin/bash
set -euo pipefail

# Install OS-specific camera dependencies
OS="$(uname -s)"

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
elif [[ "${OS}" == "Darwin" ]]; then
  if command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg already installed"
  elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "Homebrew is required to install ffmpeg on macOS. Please install Homebrew, then re-run setup."
    exit 1
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
