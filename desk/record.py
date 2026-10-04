"""Video of a run: screenshots at a steady rate from a background process, encoded to WebM (VP9)
with GStreamer when the recording stops. Frames that did not change are hard links, so a still
screen costs no space; the wall-clock time is drawn in a bar below the picture, so the video lines
up with the report's timeline."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import PROJECT, DeskError

DEFAULT_FPS = 4
# A forgotten recording stops itself.
MAX_SECONDS = 30 * 60
MAX_BYTES = 4 << 30


def _frames(run) -> Path:
    return run.dir / "rec"


def _videos(run) -> Path:
    return run.dir / "videos"


def _pid(run) -> int | None:
    try:
        pid = int((_frames(run) / "pid").read_text())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    return pid if b"desk.record" in cmdline else None


def recording(run) -> bool:
    return _pid(run) is not None


def start(run, fps: int = DEFAULT_FPS, limit: float = MAX_SECONDS) -> None:
    if recording(run):
        raise DeskError(f"run {run.id} is already being recorded (desk record stop)")
    if not 1 <= fps <= 10:
        raise DeskError("--fps must be between 1 and 10")
    run.dom()
    d = _frames(run)
    if d.exists():
        encode(run)  # frames of a recorder that died: keep what it captured
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"fps": fps, "start": time.time()}))
    with open(d / "recorder.log", "w") as log:
        p = subprocess.Popen([sys.executable, "-m", "desk.record", run.id, str(fps), str(limit)],
                             cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    (d / "pid").write_text(str(p.pid))
    run.log_event(record="start", fps=fps)


def stop(run) -> Path | None:
    """Stop the recorder and encode what it captured; returns the video, if any."""
    pid = _pid(run)
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while _pid(run) is not None and time.monotonic() < deadline:
            time.sleep(0.2)
    return encode(run)


def kill(run) -> None:
    """For runs destroyed without `desk down` (gc): no video, just no stray recorder."""
    pid = _pid(run)
    if pid is not None:
        os.kill(pid, signal.SIGKILL)


def encode(run) -> Path | None:
    d = _frames(run)
    if not d.exists():
        return None
    frames = sorted(d.glob("*.png"))
    meta = json.loads((d / "meta.json").read_text())
    video = None
    if frames:
        _videos(run).mkdir(exist_ok=True)
        video = _videos(run) / f"video-{len(list(_videos(run).glob('*.webm'))) + 1}.webm"
        epoch = datetime.fromtimestamp(meta["start"]).astimezone().isoformat(timespec="milliseconds")
        pipeline = [
            "gst-launch-1.0", "-q",
            "multifilesrc", f"location={d}/%06d.png", "index=0", f"stop-index={len(frames) - 1}",
            f"caps=image/png,framerate={meta['fps']}/1",
            "!", "pngdec", "!", "videoconvert",
            # The clock goes in a bar of its own, so it covers nothing (Windows' tray clock).
            "!", "videobox", "bottom=-32", "fill=black",
            "!", "timeoverlay", "show-times-as-dates=true", f"datetime-epoch={epoch}", "datetime-format=%H:%M:%S",
            "halignment=right", "valignment=bottom", "ypad=6", "xpad=12", "font-desc=Sans 11",
            "!", "videoconvert", "!", "vp9enc", "deadline=1", "cpu-used=8", "row-mt=true", "threads=4",
            "!", "webmmux", "!", "filesink", f"location={video}",
        ]
        r = subprocess.run(pipeline, capture_output=True, text=True)
        if r.returncode != 0:
            raise DeskError(f"encoding the video failed (frames kept in {d}): {r.stderr.strip()[-500:]}")
    run.log_event(record="stop", video=video.name if video else None,
                  seconds=round(len(frames) / meta["fps"]))
    shutil.rmtree(d)
    return video


def save_videos(run, dest: Path) -> list[Path]:
    """Copy the run's videos next to its report, which outlives the run."""
    saved = []
    for v in sorted(_videos(run).glob("*.webm")) if _videos(run).exists() else []:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(v, dest / v.name)
        saved.append(dest / v.name)
    return saved


def _record(run_id: str, fps: int, limit: float) -> None:
    from . import display, runs

    stopping = False

    def on_term(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    run = runs.load(run_id)
    d = _frames(run)
    dom = run.live_domain()
    period, t0 = 1 / fps, time.monotonic()
    k, prev, prev_digest, written = 0, None, None, 0
    while dom is not None and not stopping and k < limit * fps and written < MAX_BYTES:
        wait = t0 + k * period - time.monotonic()
        if wait > 0:
            time.sleep(wait)
            continue
        try:
            png = display.screenshot(dom)
        except Exception:  # noqa: BLE001 - the domain went away, or a reboot hiccup
            dom = run.live_domain()
            continue
        path = d / f"{k:06d}.png"
        digest = hashlib.sha256(png).digest()
        if digest == prev_digest:
            os.link(prev, path)
        else:
            path.write_bytes(png)
            written += len(png)
            prev, prev_digest = path, digest
        k += 1
        # Ticks missed while a screenshot was slow repeat the last frame, so time stays real.
        while prev is not None and time.monotonic() > t0 + (k + 1) * period and k < limit * fps:
            os.link(prev, d / f"{k:06d}.png")
            k += 1


if __name__ == "__main__":
    _record(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]))
