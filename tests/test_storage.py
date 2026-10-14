import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trd_bot.core import atomic_json


class StorageTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1] / "runtime" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.path = Path(self.temp.name) / "state.json"
        self.path.write_text('{"old":true}', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_transient_windows_reader_then_complete_replace(self):
        original = Path.replace
        attempts = []
        def replace(source, target):
            attempts.append(source.read_text(encoding="utf-8"))
            if len(attempts) < 3:
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"old": True})
                raise PermissionError("reader busy")
            return original(source, target)
        with patch("trd_bot.core.WINDOWS", True), patch.object(Path, "replace", replace), \
                patch("trd_bot.core.time.sleep") as sleep:
            atomic_json(self.path, {"new": "complete"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"new": "complete"})
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.01, .02])

    def test_permanent_denial_preserves_previous_snapshot(self):
        with patch("trd_bot.core.WINDOWS", True), \
                patch.object(Path, "replace", side_effect=PermissionError("denied")) as replace, \
                patch("trd_bot.core.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {"new": True})
        self.assertEqual(replace.call_count, 6)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), .31)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})

    def test_posix_permission_failure_not_retried(self):
        with patch("trd_bot.core.WINDOWS", False), \
                patch.object(Path, "replace", side_effect=PermissionError("denied")), \
                patch("trd_bot.core.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {})
            sleep.assert_not_called()
