"""A stand-in for usbvfiod's hotplug socket: it reads the commands the way usbvfiod does (the first
byte with its file descriptor, then the rest) and answers from a handler, so the tests of desk's
client and of the commands built on it never need a controller, a VM or a device."""

import json
import os
import socket
import struct
import threading
from pathlib import Path

FRAMED = {0x10, 0x11, 0x12, 0x13, 0x14, 0x15}


class FakeHotplug:
    """`handler(byte, payload, fds)` -> a reply dict (framed), a response name (a single legacy
    byte is sent for a legacy command), None (the connection is closed unanswered) or "hang".
    `payload` is the decoded JSON, or the two numbers of a legacy command; `fds` are the contents
    (first 64 bytes) of the files that came with the command."""

    def __init__(self, directory, handler):
        self.path = Path(directory) / "h.sock"
        self.handler = handler
        self.seen: list[tuple[int, object, list[bytes]]] = []
        self.raw: list[bytes] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(8)
        self._stop = threading.Event()
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._server.close()
        for c in self._held:
            c.close()

    def _read(self, conn, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                self._one(conn)
            except OSError:
                pass
            finally:
                if conn.fileno() != -1 and conn not in self._held:
                    conn.close()

    def _one(self, conn) -> None:
        conn.settimeout(5)
        first, fds, _, _ = socket.recv_fds(conn, 1, 4)
        if not first:
            return
        byte = first[0]
        if byte in FRAMED:
            head = self._read(conn, 4)
            (length,) = struct.unpack("<I", head)
            body = self._read(conn, length)
            payload, raw = json.loads(body), first + head + body
        else:
            payload = tuple(self._read(conn, 2))
            raw = first + bytes(payload)
        contents = []
        for fd in fds:
            contents.append(os.pread(fd, 64, 0))
            os.close(fd)
        self.seen.append((byte, payload, contents))
        self.raw.append(raw)
        reply = self.handler(byte, payload, contents)
        if reply is None:
            return
        if reply == "hang":
            self._held.append(conn)
            return
        if byte in FRAMED:
            body = json.dumps(reply if isinstance(reply, dict) else {"response": reply}).encode()
            conn.sendall(struct.pack("<I", len(body)) + body)
        else:
            from desk.hotplug import LEGACY

            conn.sendall(bytes([LEGACY.index(reply)]))


class FakeController:
    """usbvfiod's tree and the answers it gives, enough to try desk's commands in sequence:
    a path that exists is occupied, a hub must exist before something is attached below it,
    detaching takes what is below along."""

    def __init__(self, refuse_host: str | None = None, forget_host: bool = False):
        self.tree: list[dict] = []
        self.refuse_host, self.forget_host = refuse_host, forget_host
        self.number = 0

    def paths(self) -> list[str]:
        return [e["path"] for e in self.tree]

    def _add(self, path: str, speed: str, id: str, hub_ports: int | None = None) -> str:
        if path in self.paths():
            return "PathOccupied"
        parent = path.rpartition(".")[0]
        if parent:
            above = {e["path"]: e for e in self.tree}.get(parent)
            if above is None:
                return "NoSuchPath"
            if "hub_ports" not in above:
                return "NotAHub"
        self.tree.append({"path": path, "speed": speed, "id": id, **({"hub_ports": hub_ports} if hub_ports else {})})
        return "SuccessfulOperation"

    def handler(self, byte, payload, fds):
        self.number += 1
        if byte == 0x14:
            return {"response": "SuccessfulOperation", "tree": list(self.tree)}
        if byte == 0x11:
            spec = payload["spec"]
            return self._add(payload["path"], spec["speed"], f"virtual {self.number}", spec["ports"])
        if byte in (0x00, 0x10):
            if self.refuse_host:
                return self.refuse_host
            bus, dev = (payload["bus"], payload["device"]) if byte == 0x10 else payload
            if self.forget_host:
                return "SuccessfulOperation"
            at = payload["path"] if byte == 0x10 else next(p for p in map(str, range(5, 9)) if p not in self.paths())
            return self._add(at, "high", f"host {bus:03}:{dev:03}")
        if byte == 0x13:
            at = payload["path"]
            if at not in self.paths():
                return {"response": "NoSuchDevice", "message": f"there is no device at {at}"}
            self.tree = [e for e in self.tree if e["path"] != at and not e["path"].startswith(at + ".")]
            return "SuccessfulOperation"
        return "InvalidRequest"
