import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk import scenario


class StepOutput(unittest.TestCase):
    def test_report_keeps_each_streams_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = SimpleNamespace(dir=Path(tmp))
            limit = scenario.STEP_STREAM_LIMIT
            result = subprocess.CompletedProcess(
                [], 1, b"cut" + b"x" * limit, b"!" + b"y" * limit
            )
            path = run.dir / scenario._save_output(run, 1, result)
            self.assertEqual(
                path.read_bytes(),
                b"stdout:\n[truncated 3 leading bytes]\n"
                + b"x" * limit
                + b"\nstderr:\n[truncated 1 leading bytes]\n"
                + b"y" * limit
                + b"\n",
            )

    def test_until_captures_only_final_attempt_even_when_empty(self):
        early = subprocess.CompletedProcess([], 1, b"not ready", b"warning")
        final = subprocess.CompletedProcess([], 0, b"", b"")
        once = mock.Mock(side_effect=[(early, False), (final, False)])
        capture = mock.Mock()
        with mock.patch("desk.scenario.time.sleep"):
            scenario._checked_command({"until": True}, once, None, capture)
        capture.assert_called_once_with(final)
        with tempfile.TemporaryDirectory() as tmp:
            run = SimpleNamespace(dir=Path(tmp))
            self.assertIsNone(scenario._save_output(run, 1, final))
            self.assertEqual(list(run.dir.iterdir()), [])
