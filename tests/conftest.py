"""Shared pytest fixtures for the unit-test suite."""

import textwrap
from types import SimpleNamespace
from typing import Callable

import pytest


@pytest.fixture
def write_cfg(tmp_path) -> Callable[[str], str]:
    """Return a helper that writes cfg text to a temp file and returns its path.

    Leading indentation is stripped (via textwrap.dedent) so tests can use
    triple-quoted, indented blocks without tripping ConfigParser.
    """
    counter = {"n": 0}

    def _write(text: str) -> str:
        counter["n"] += 1
        path = tmp_path / f"cfg_{counter['n']}.cfg"
        path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
        return str(path)

    return _write


class FakeSoundDevice:
    """A minimal, configurable stand-in for the parts of the `sounddevice`
    module the recorder touches: query_devices(), check_input_settings() and
    default.device.

    A device's supported sample rates / dtypes / channel count are declared up
    front via add_device(), and check_input_settings() raises (like PortAudio)
    for any combination outside that declaration. This lets tests drive the
    device-resolution and capability-probe logic without real audio hardware.
    """

    class PortAudioError(Exception):
        """Stand-in for the error PortAudio raises on an unsupported config."""

    def __init__(self):
        self.devices: list[dict] = []
        # Mirrors sd.default.device == [input_index, output_index].
        self.default = SimpleNamespace(device=[None, None])

    def add_device(
        self,
        name: str,
        *,
        max_input_channels: int = 0,
        hostapi: int = 0,
        default_samplerate: float | None = None,
        supported_rates=(),
        supported_dtypes=("int16", "float32"),
    ) -> int:
        """Register a device and return its index."""
        self.devices.append(
            {
                "name": name,
                "max_input_channels": max_input_channels,
                "max_output_channels": 0,
                "hostapi": hostapi,
                "default_samplerate": default_samplerate,
                "_rates": {int(r) for r in supported_rates},
                "_dtypes": set(supported_dtypes),
            }
        )
        return len(self.devices) - 1

    def set_default_input(self, index):
        self.default.device[0] = index

    # --- sounddevice API surface -------------------------------------------

    def query_devices(self, device=None):
        if device is None:
            return list(self.devices)
        if isinstance(device, str):
            for d in self.devices:
                if d["name"] == device:
                    return d
            raise ValueError(f"no device named {device!r}")
        return self.devices[device]  # may raise IndexError for a bad index

    def check_input_settings(self, device=None, samplerate=None, channels=None, dtype=None):
        resolved = device if device is not None else self.default.device[0]
        d = self.query_devices(resolved)
        if channels is not None and channels > d["max_input_channels"]:
            raise self.PortAudioError("too many input channels")
        if samplerate is not None and int(samplerate) not in d["_rates"]:
            raise self.PortAudioError("unsupported samplerate")
        if dtype is not None and dtype not in d["_dtypes"]:
            raise self.PortAudioError("unsupported dtype")


@pytest.fixture
def fake_sd(monkeypatch) -> FakeSoundDevice:
    """Patch the shared `sounddevice` module with a FakeSoundDevice.

    Both recorder.audio.devices and recorder.audio.lsl_audio do
    `import sounddevice as sd`, so they share one module object; patching its
    attributes here covers both.
    """
    import sounddevice as sd

    fake = FakeSoundDevice()
    monkeypatch.setattr(sd, "query_devices", fake.query_devices)
    monkeypatch.setattr(sd, "check_input_settings", fake.check_input_settings)
    monkeypatch.setattr(sd, "default", fake.default)
    return fake
