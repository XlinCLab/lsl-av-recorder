from __future__ import annotations

import socket
from typing import Optional


class LabRecorderRCS:
    def __init__(self, host: str = "127.0.0.1", port: int = 22345):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=2)

    def send(self, cmd: str):
        if not self.sock:
            raise RuntimeError("RCS socket not connected")
        if not cmd.endswith("\n"):
            cmd += "\n"
        self.sock.sendall(cmd.encode("utf-8"))

    def select_all(self):
        self.send("select all")

    def filename(self, root: str, template: str, participant: str, session: str, task: str, run: str, acq: str = ""):
        cmd = (
            f"filename {{root:{root}}} "
            f"{{template:{template}}} "
            f"{{participant:{participant}}} "
            f"{{session:{session}}} "
            f"{{task:{task}}} "
            f"{{run:{run}}}"
        )
        if acq:
            cmd += f" {{acquisition:{acq}}}"
        self.send(cmd)

    def start(self):
        self.send("start")

    def stop(self):
        self.send("stop")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None
