"""Tests for recorder.lsl.labrecorder_rcs.LabRecorderRCS.

LabRecorderRCS speaks LabRecorder's Remote Control Server (RCS) text protocol
over a TCP socket. These tests drive it with a fake socket so no real
LabRecorder / network is involved, pinning down the wire format of each command
(newline framing, the filename field syntax, optional acquisition) and the
not-connected / close behaviors.
"""
from __future__ import annotations

import pytest

from recorder.lsl.labrecorder_rcs import LabRecorderRCS


class FakeSocket:
    """Minimal socket stand-in that records what was sent and whether it closed."""

    def __init__(self):
        self.sent = bytearray()
        self.closed = False

    def sendall(self, data: bytes):
        self.sent += data

    def close(self):
        self.closed = True

    def text(self) -> str:
        """The full byte stream sent so far, decoded as UTF-8."""
        return self.sent.decode("utf-8")


@pytest.fixture
def rcs():
    """An RCS client pre-wired to a FakeSocket (as if connect() had run)."""
    client = LabRecorderRCS()
    client.sock = FakeSocket()
    return client


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def test_send_appends_newline_when_missing(rcs):
    """A command without a trailing newline gets exactly one appended."""
    rcs.send("start")
    assert rcs.sock.text() == "start\n"


def test_send_does_not_double_newline(rcs):
    """A command that already ends in a newline is sent unchanged."""
    rcs.send("start\n")
    assert rcs.sock.text() == "start\n"


def test_send_encodes_utf8(rcs):
    """The command is transmitted as UTF-8 bytes."""
    rcs.send("stop")
    assert rcs.sock.sent == b"stop\n"


def test_send_without_connection_raises():
    """Sending before connect() (no socket) raises RuntimeError rather than silently doing nothing."""
    client = LabRecorderRCS()
    with pytest.raises(RuntimeError, match="not connected"):
        client.send("start")


# ---------------------------------------------------------------------------
# convenience commands
# ---------------------------------------------------------------------------

def test_select_all_command(rcs):
    """select_all() sends the 'select all' command."""
    rcs.select_all()
    assert rcs.sock.text() == "select all\n"


def test_start_and_stop_commands(rcs):
    """start()/stop() send their respective one-word commands, newline-framed."""
    rcs.start()
    rcs.stop()
    assert rcs.sock.text() == "start\nstop\n"


# ---------------------------------------------------------------------------
# filename
# ---------------------------------------------------------------------------

def test_filename_formats_fields_without_acquisition(rcs):
    """filename() renders each field in {key:value} form; acquisition is omitted
    when not supplied."""
    rcs.filename(
        root="/data",
        template="sub-%p",
        participant="S01",
        session="1",
        task="reading",
        run="2",
    )
    assert rcs.sock.text() == (
        "filename {root:/data} {template:sub-%p} {participant:S01} "
        "{session:1} {task:reading} {run:2}\n"
    )


def test_filename_includes_acquisition_when_given(rcs):
    """A non-empty acquisition adds a trailing {acquisition:...} field."""
    rcs.filename(
        root="/data",
        template="sub-%p",
        participant="S01",
        session="1",
        task="reading",
        run="2",
        acq="highres",
    )
    assert rcs.sock.text().endswith(" {run:2} {acquisition:highres}\n")


def test_filename_empty_acquisition_is_omitted(rcs):
    """An empty acquisition string adds no acquisition field."""
    rcs.filename(
        root="/data",
        template="sub-%p",
        participant="S01",
        session="1",
        task="reading",
        run="2",
        acq="",
    )
    assert "acquisition" not in rcs.sock.text()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def test_close_closes_socket_and_clears_reference(rcs):
    """close() closes the underlying socket and drops the reference."""
    sock = rcs.sock
    rcs.close()
    assert sock.closed is True
    assert rcs.sock is None


def test_close_without_socket_is_noop():
    """close() on a never-connected client does nothing and doesn't raise."""
    client = LabRecorderRCS()
    client.close()  # must not raise
    assert client.sock is None
