"""The report's Observation section and observe.json: every session of the run with its state,
its files (copied next to the report, as the run directory goes with `desk down`), what was
checked of them, and its marks lined up with the run's timeline, screenshots and videos."""

import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .session import Store


def _stamp(created: float, t: float | None) -> str:
    if not t:
        return "-"
    offset = t - created
    return f"+{int(offset // 60):02d}:{int(offset % 60):02d}"


def _mib(n) -> str:
    return f"{(n or 0) / 2**20:.1f} MiB"


def _keep(src: Path, dest: Path) -> None:
    """A hard link where the report is on the same file system (traces are large), else a copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def videos(events: list[dict]) -> list[dict]:
    """Recordings as host time windows: a start, and the stop that names its video."""
    out, started = [], None
    for ev in events:
        if ev.get("record") == "start":
            started = ev["t"]
        elif ev.get("record") == "stop" and started is not None:
            if ev.get("video"):
                out.append({"video": ev["video"], "start": started, "end": started + (ev.get("seconds") or 0)})
            started = None
    return out


def where(t: float, events: list[dict]) -> dict:
    """What else shows the moment `t` (host time): the video and the second in it, the screenshots
    taken last before and first after it."""
    out = {}
    for v in videos(events):
        if v["start"] <= t <= v["end"] + 1:
            out["video"] = v["video"]
            out["video_at"] = round(t - v["start"], 1)
    shots = [ev for ev in events if "shot" in ev]
    before = [ev for ev in shots if ev["t"] <= t]
    after = [ev for ev in shots if ev["t"] > t]
    if before:
        out["shot_before"] = Path(before[-1]["shot"]).name
    if after:
        out["shot_after"] = Path(after[0]["shot"]).name
    return out


def section(run, out: Path, events: list[dict], tz=None) -> list[str]:
    from . import PROVIDERS

    sessions = Store(run).all()
    if not sessions:
        return []
    md = ["## Observation", ""]
    data = {"schema": 1, "run": run.id, "image": run.image, "created": run.created, "sessions": []}
    for s in sessions:
        p = PROVIDERS.get(s.provider)
        record = asdict(s)
        state = s.state + (", complete" if s.complete else ", NOT complete" if s.complete is False else "")
        md.append(f"### {s.name()}: {s.provider} ({p.title if p else 'unknown provider'})")
        md.append("")
        md.append(f"- state: {state}" + (f": {s.error}" if s.error else ""))
        md.append(f"- from {_stamp(run.created, s.started or s.created)} to {_stamp(run.created, s.stopped)}"
                  + (f", limits {s.limits['duration']:.0f} s" if s.limits.get("duration") else "")
                  + (f" / {_mib(s.limits['max_bytes'])}" if s.limits.get("max_bytes") else "")
                  + (f", started by {s.owner}" if s.owner else ""))
        changed = {k: v for k, v in s.options.items() if p is None or v != p.OPTIONS[k].default}
        if changed:
            md.append("- options: " + ", ".join(f"{k} = {v!r}" for k, v in changed.items()))
        if s.clock:
            c = s.clock[0]
            md.append(f"- guest clock {c['guest'] - c['host']:+.3f} s from the host's (±{c['uncertainty']:.3f} s, "
                      f"{c.get('via')}); guest times below are moved onto the host's")
        if s.target:
            procs = list(s.processes.values())
            md.append(f"- processes followed ({s.target.get('image') or 'pid ' + str(s.target.get('pid'))} and "
                      f"descendants): " + (", ".join(f"{x['name']} pid {x['pid']}" for x in procs[:20]) or
                                          "none seen while desk looked (start, marks, stop)"))
        for e in s.events:
            if e["event"] in ("ended", "interrupted", "snapshot restored", "restored", "stop failed", "cleanup failed"):
                md.append(f"- {_stamp(run.created, e['t'])} {e['event']}: {e.get('detail', '')}")
        marks = []
        for m in s.markers:
            seen = where(m["host"], events)
            marks.append({**m, **seen})
            also = []
            if seen.get("video"):
                also.append(f"[{seen['video']}]({seen['video']}) at {int(seen['video_at'] // 60):02d}:"
                            f"{seen['video_at'] % 60:04.1f}")
            if seen.get("shot_before"):
                also.append(f"after shots/{seen['shot_before']}")
            took = (m.get("host_window") or [0, 0])[1] - (m.get("host_window") or [0, 0])[0]
            if took > 2:
                # The guest wrote the marker at its guest time; around it desk waited this long.
                also.append(f"desk's mark took {took:.0f} s")
            guest = datetime.fromtimestamp(m["guest"], tz).strftime("%H:%M:%S.%f")[:-3]
            md.append(f"- {_stamp(run.created, m['host'])} mark {m['seq']} {m['text']!r}: guest {guest}"
                      + (f"; {', '.join(also)}" if also else "")
                      + ("" if m.get("event") else "; no ETW marker event"))
        record["markers"] = marks
        record["artifacts"] = []
        for a in s.artifacts:
            rel = None
            if a.get("path") and Path(a["path"]).exists():
                rel = Path("observe") / s.id / a["name"]
                _keep(Path(a["path"]), out / rel)
            record["artifacts"].append({**a, "report_path": str(rel) if rel else None})
            state_ = "verified" if a.get("verified") else "NOT verified" if a.get("transferred") else "NOT copied"
            name = f"[{a['name']}]({rel})" if rel else a["name"]
            md.append(f"- {name}: {state_}, {_mib(a.get('bytes') or a.get('guest_bytes'))}"
                      + (f", sha256 {a['sha256'][:16]}…" if a.get("sha256") else "")
                      + (f": {a['detail']}" if a.get("detail") else ""))
        if p:
            for line in p.summary(s):
                md.append(f"- {line}")
        for n in s.notes:
            md.append(f"- note: {n}")
        md.append("")
        data["sessions"].append(record)
    (out / "observe.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    md.append("Every session, its clock samples and checks: observe.json.")
    md.append("")
    return md


def timeline(ev: dict) -> str | None:
    """A timeline entry for an observation event, None for others."""
    what = ev.get("observe")
    if not what:
        return None
    if what == "mark":
        return f"mark {ev.get('seq')}: {ev.get('text')}"
    if what == "start":
        return f"observation {ev['session']} started ({ev.get('provider')})"
    if what == "stop":
        return (f"observation {ev['session']} stopped ({ev.get('provider')}): "
                + ("complete" if ev.get("complete") else "NOT complete"))
    if what == "restore":
        return f"snapshot {ev.get('name')}: {ev.get('detail')}"
    return f"observation {ev.get('session')} {what}" + (f": {ev['detail']}" if ev.get("detail") else "")
