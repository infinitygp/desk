"""The hotplug socket of usbvfiod: hubs and devices attached to and detached from the guest's
controller at port paths, and its tree read back.

One connection per command, as usbvfiod serves one client at a time and gives it 5 s for the whole
command and for taking the reply. A command is its byte, the length of its JSON payload (u32 LE)
and the payload, sent in one sendmsg together with the file descriptor of a device that goes along
(SCM_RIGHTS). The reply of the framed commands is a u32 LE length and a JSON object with the
`response` (a name), an optional `message` and, for the tree, the `tree`; the legacy attach
command answers with one byte."""

import json
import socket
import struct
import time
from pathlib import Path

from .config import DeskError

# The command bytes. ATTACH is the old one: the device takes the first root port that fits its speed.
ATTACH = 0x00
ATTACH_AT = 0x10
ATTACH_HUB = 0x11
DETACH_AT = 0x13
TREE = 0x14
# usbvfiod's side takes at most 5 s to read a command or to write its reply; a reply that has not
# come in twice that is not coming.
TIMEOUT = 10.0
MAX_REPLY = 1 << 20

OK = "SuccessfulOperation"
# The legacy reply byte is the index in usbvfiod's `Response`.
LEGACY = ("SuccessfulOperation", "ListFollowing", "NoFreePort", "CouldNotDetermineSpeed", "FailedToOpenFd",
          "NoSuchDevice", "Invalid", "PathOccupied", "NoSuchPath", "NotAHub", "SpeedNotSupported", "InvalidRequest")
WHY = {
    "PathOccupied": "something is attached at that port path already (desk usb ls)",
    "NoSuchPath": "the root port, a hub on the way or its port does not exist",
    "NotAHub": "the device on the way is not a hub",
    "SpeedNotSupported": "the port does not take a device of that speed: a super speed device needs a USB 3 root "
                         "port (1 to 4), and a high speed one high speed hubs on its way (--hub PORTPATH=/hs)",
    "NoFreePort": "no free root port fits the device's speed",
    "FailedToOpenFd": "usbvfiod could not use the file descriptor it was given",
    "NoSuchDevice": "there is no such device",
    "InvalidRequest": "usbvfiod did not accept the request",
    "CouldNotDetermineSpeed": "usbvfiod could not tell the device's speed",
}


class Refused(DeskError):
    """usbvfiod answered, and said no. `response` is the name it gave."""

    def __init__(self, what: str, response: str, message: str | None):
        self.response = response
        super().__init__(f"usbvfiod refused to {what}: {response}"
                         + (f" ({message})" if message else f" ({WHY[response]})" if response in WHY else ""))


def _read(sock: socket.socket, count: int, deadline: float) -> bytes:
    data = b""
    while len(data) < count:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        sock.settimeout(left)
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def _exchange(path: Path | str, message: bytes, fd: int | None, legacy: bool, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sent = socket.send_fds(sock, [message], [fd] if fd is not None else [])
            if sent < len(message):
                sock.sendall(message[sent:])
            if legacy:
                code = _read(sock, 1, deadline)[0]
                return {"response": LEGACY[code] if code < len(LEGACY) else "Invalid"}
            (length,) = struct.unpack("<I", _read(sock, 4, deadline))
            if length > MAX_REPLY:
                raise DeskError(f"usbvfiod's reply announces {length} bytes, which is no reply")
            return json.loads(_read(sock, length, deadline))
        except TimeoutError:
            raise DeskError(f"usbvfiod did not answer on {path} within {timeout:.0f} s") from None
        except EOFError:
            raise DeskError(f"usbvfiod closed {path} without a reply (its log says why)") from None
        except (FileNotFoundError, ConnectionRefusedError):
            raise DeskError(f"nobody listens on {path}: usbvfiod of the run has ended (its log says why)") from None
        except (OSError, ValueError) as e:
            raise DeskError(f"talking to usbvfiod on {path} failed: {e}") from None


def command(path: Path | str, byte: int, payload: dict, what: str, fd: int | None = None,
            timeout: float = TIMEOUT) -> dict:
    """One framed command; the reply when usbvfiod accepted it, `Refused` when it did not."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    reply = _exchange(path, bytes([byte]) + struct.pack("<I", len(body)) + body, fd, False, timeout)
    if reply.get("response") != OK:
        raise Refused(what, str(reply.get("response")), reply.get("message"))
    return reply


def tree(path: Path | str, timeout: float = TIMEOUT) -> list[dict]:
    """The controller's tree: `path`, `speed` (low, full, high, ...), `id` (`host 005:012`,
    `virtual 1`, `external NAME`) and for a hub its `hub_ports`."""
    return command(path, TREE, {}, "list the tree", timeout=timeout).get("tree") or []


def attach_hub(path: Path | str, hub, timeout: float = TIMEOUT) -> None:
    command(path, ATTACH_HUB, hub.payload(), f"attach a hub at {hub.payload()['path']}", timeout=timeout)


def attach_host(path: Path | str, bus: int, device: int, fd: int, at: str | None = None,
                timeout: float = TIMEOUT) -> None:
    """Hand the host device `bus`.`device`, opened by the caller as `fd`, to the controller: at a
    port path, or (`at` None) on the first free root port that fits its speed."""
    if not (0 < bus < 256 and 0 < device < 256):
        raise DeskError(f"usbvfiod takes bus and device numbers below 256 (got {bus}.{device})")
    if at is None:
        reply = _exchange(path, bytes([ATTACH, bus, device]), fd, True, timeout)
        if reply["response"] != OK:
            raise Refused(f"attach {bus}.{device}", reply["response"], None)
        return
    command(path, ATTACH_AT, {"path": at, "bus": bus, "device": device}, f"attach {bus}.{device} at {at}", fd=fd,
            timeout=timeout)


def detach_at(path: Path | str, at: str, timeout: float = TIMEOUT) -> None:
    """Detach what is at a port path, with everything below it if it is a hub."""
    command(path, DETACH_AT, {"path": at}, f"detach {at}", timeout=timeout)
