import json
import unittest
from unittest.mock import patch

from desk import winagent
from desk.config import DeskError


class WindowsVisibility(unittest.TestCase):
    spec = {"name": "O'Brien", "role": "button", "pid": 13, "path": [0, 1]}

    def probe(self, reply):
        with patch.object(winagent, "request", return_value=reply) as request:
            result = winagent.visible_point(None, self.spec, (100, 50))
        args = request.call_args.kwargs
        self.assertEqual(json.loads(args["env"]["DESK_VISIBLE"])["spec"], self.spec)
        self.assertTrue(args["wait"])
        return result

    def test_verified_point(self):
        self.assertEqual(
            self.probe({"code": 0, "stdout": '{"point":[99,49]}'}),
            ((99, 49), None, False),
        )

    def test_covered_and_offscreen(self):
        for cover in ("another button", None):
            output = json.dumps({"point": None, "covered": cover})
            self.assertEqual(
                self.probe({"code": 0, "stdout": output}), (None, cover, False)
            )

    def test_unchecked_never_falls_back_to_rectangle(self):
        for reply in (
            {"ok": True},
            {"timeout": True},
            {"code": 1, "stderr": "no hit test"},
            {"code": 0, "stdout": "not json"},
            {"code": 0, "stdout": '{"showing":true}'},
            {"code": 0, "stdout": '{"point":[100,49]}'},
            {"code": 0, "stdout": '{"point":[true,0]}'},
        ):
            with (
                self.subTest(reply=reply),
                self.assertRaisesRegex(DeskError, "visibility hit test"),
            ):
                self.probe(reply)
