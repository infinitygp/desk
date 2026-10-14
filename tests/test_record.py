import json
import shutil
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from desk import evidence, record
from desk.config import DeskError


def png(width: int, height: int, shade: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + bytes([shade, shade, shade]) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class FakeRun:
    def __init__(self, d: Path):
        self.id, self.dir, self.created, self.events = "t", d, time.time(), []

    def log_event(self, **event):
        self.events.append(event)

    def live_domain(self):
        return None


@unittest.skipIf(shutil.which("gst-launch-1.0") is None, "GStreamer not installed")
class Encode(unittest.TestCase):
    def test_frames_become_a_webm_and_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = FakeRun(Path(tmp))
            frames = run.dir / "rec"
            frames.mkdir()
            (frames / "meta.json").write_text(json.dumps({"fps": 4, "start": time.time()}))
            (frames / "000000.png").write_bytes(png(64, 48, 40))
            (frames / "000001.png").hardlink_to(frames / "000000.png")
            (frames / "000002.png").write_bytes(png(64, 48, 200))
            video = record.encode(run)
            self.assertEqual(video, run.dir / "videos" / "video-1.webm")
            self.assertEqual(video.read_bytes()[:4], b"\x1a\x45\xdf\xa3")  # EBML (WebM) header
            self.assertFalse(frames.exists())
            self.assertEqual(run.events[-1], {"record": "stop", "video": "video-1.webm", "seconds": 1})

    def test_nothing_to_encode(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(record.encode(FakeRun(Path(tmp))))


class FailedEncode(unittest.TestCase):
    def frames(self, run) -> Path:
        frames = run.dir / "rec"
        frames.mkdir()
        (frames / "meta.json").write_text(json.dumps({"fps": 4, "start": time.time()}))
        (frames / "000000.png").write_bytes(png(8, 8, 40))
        return frames

    def test_frames_are_set_aside_so_the_next_recording_can_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = FakeRun(Path(tmp))
            frames = self.frames(run)
            with mock.patch("desk.record.subprocess.run", side_effect=FileNotFoundError("gst-launch-1.0")):
                with self.assertRaisesRegex(DeskError, "not installed.*rec-failed-"):
                    record.encode(run)
            self.assertFalse(frames.exists())
            self.assertEqual(len(list(run.dir.glob("rec-failed-*/000000.png"))), 1)
            self.assertEqual(list((run.dir / "videos").glob("*")), [])
            self.assertIsNone(record.encode(run))

    def test_the_run_goes_down_without_its_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = FakeRun(Path(tmp))
            self.frames(run)
            with mock.patch("desk.record.subprocess.run", side_effect=FileNotFoundError("gst-launch-1.0")), \
                    mock.patch("desk.runs.down") as down, mock.patch("sys.stderr"):
                report, videos = evidence.finish(run)
            down.assert_called_once_with(run, keep=False)
            self.assertEqual((report, videos), (None, []))


if __name__ == "__main__":
    unittest.main()
