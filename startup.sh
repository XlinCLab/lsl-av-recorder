#!/bin/bash
# Launch the app from anywhere: the repo location is derived from this script's path (including following symlinks)
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE}" ]]; do
  DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
  SOURCE="$(readlink "${SOURCE}")"
  [[ "${SOURCE}" != /* ]] && SOURCE="${DIR}/${SOURCE}"
done
REPO_DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"

# Find virtual environment's python
PYTHON="${REPO_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "No virtual environment found at ${REPO_DIR}/.venv. Run ${REPO_DIR}/setup.sh first." >&2
  exit 1
fi

# Relative paths in the app (e.g. the default ./recordings output folder) resolve
# against the working directory, so always run from the repo root.
cd "${REPO_DIR}"
exec "${PYTHON}" main.py "$@"
