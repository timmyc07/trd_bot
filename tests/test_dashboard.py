from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from trd_bot.core import Config, Engine
from trd_bot.dashboard import Controller, make_server, tail
from trd_bot.research_engine import ResearchEngine
from trd_bot.research_config import ResearchConfig
from trd_bot.ledger import read_snapshot


class DashboardTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1] / "runtime" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temp.name).resolve()
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        self.controller = Controller(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_start_spawns_one_worker(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        def start():
            try:
                self.controller.action("start")
                return True
            except ValueError:
                return False
        with patch("trd_bot.dashboard.subprocess.Popen", return_value=process) as spawn:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: start(), range(2))), [False, True])
            self.assertEqual(spawn.call_count, 1)
            self.assertFalse(spawn.call_args.kwargs["shell"])
            self.assertIn(str(self.root / "config.json"), spawn.call_args.args[0])

    def test_stop_targets_active_demo(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch("trd_bot.dashboard.subprocess.Popen", return_value=process):
            self.controller.action("demo")
            self.controller.action("stop")
        self.assertTrue((self.controller.directory / "KILL").exists())
        self.assertFalse((self.root / "runtime/dashboard/paper/KILL").exists())
        self.assertEqual(self.controller.snapshot()["status"], "stopping")

    def test_existing_account_needs_explicit_resume(self):
        engine = ResearchEngine(ResearchConfig(), self.controller.directory, "binance_public")
        engine.control("kill_switch")
        (engine.directory / "KILL").touch()
        with self.assertRaises(ValueError):
            self.controller.action("start")
        with patch("trd_bot.dashboard.subprocess.run", return_value=Mock(returncode=0)) as resume, \
                patch("trd_bot.dashboard.subprocess.Popen", return_value=Mock(pid=123)) as spawn:
            self.controller.action("resume")
            self.assertIn("--acknowledge-risk", resume.call_args.args[0])
            self.assertTrue((engine.directory / "KILL").exists())  # Only successful CLI reset removes it.
            self.assertEqual(spawn.call_count, 1)
        saved = read_snapshot(engine.directory)
        self.assertEqual(saved["cash"], "50")
        engine.close()

    def test_historical_health_is_not_running(self):
        engine = Engine(Config(), self.controller.directory, "binance_public")
        engine.health()
        data = self.controller.snapshot()
        self.assertFalse(data["running"])
        self.assertFalse(data["current_report"])
        self.assertEqual(data["status"], "idle")
        (engine.directory / "health.json").write_text("{", encoding="utf-8")
        self.assertIsNotNone(self.controller.snapshot()["error"])

    def test_stopping_takes_priority_over_fresh_health(self):
        engine = Engine(Config(), self.controller.directory, "binance_public")
        engine.last_book_ms = int(time.time()*1000)
        engine.health()
        process = Mock(pid=123)
        process.poll.return_value = None
        self.controller.process = process
        self.controller.stopping = True
        self.assertEqual(self.controller.snapshot()["status"], "stopping")

    def test_resume_review_is_paper_account_even_after_demo(self):
        paper = self.controller.directory
        engine = ResearchEngine(ResearchConfig(), paper, "binance_public")
        engine.control("drawdown")
        self.controller.directory = paper.parent / "demo-test"
        demo = Engine(Config(), self.controller.directory, "synthetic_demo")
        demo.health()
        snapshot = self.controller.snapshot()
        self.assertIsNone(snapshot["report"]["halt_reason"])
        self.assertEqual(snapshot["paper_review"]["halt_reason"], "drawdown")
        engine.close()

    def test_failed_worker_visible_and_lock_not_deleted(self):
        process = Mock(pid=123, returncode=1)
        process.poll.return_value = 1
        self.controller.process = process
        self.assertEqual(self.controller.snapshot()["status"], "failed")
        self.controller.directory.mkdir(parents=True)
        lock = self.controller.directory / "engine.lock"
        lock.write_text("123", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.controller.action("start")
        self.assertTrue(lock.exists())

    def test_shutdown_stops_and_reaps_worker(self):
        self.controller.directory.mkdir(parents=True)
        process = Mock(pid=123)
        process.poll.return_value = None
        self.controller.process = process
        self.controller.shutdown()
        self.assertTrue((self.controller.directory / "KILL").exists())
        process.wait.assert_called_once_with(timeout=40)
        process.terminate.assert_not_called()
        with self.assertRaises(ValueError):
            self.controller.action("demo")

    def test_tail_bounded_for_large_log(self):
        logfile = self.root / "large.log"
        logfile.write_text("x" * 100000 + "\nlast\n", encoding="utf-8")
        self.assertEqual(tail(logfile), ["last"])

    def test_http_origin_token_host_and_routes(self):
        server = make_server(self.controller, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_port
        origin = f"http://127.0.0.1:{port}"
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read().decode()
            finally:
                connection.close()
        try:
            status, html = request("GET", "/")
            self.assertEqual(status, 200)
            token = re.search(r'name="panel-token" content="([^"]+)"', html).group(1)
            self.assertNotEqual(token, "__TOKEN__")
            self.assertEqual(request("GET", "/api/status")[0], 200)
            self.assertEqual(request("GET", "/config.json")[0], 404)
            self.assertEqual(request("GET", "/", headers={"Host": "evil.test"})[0], 403)
            body = json.dumps({"action": "demo"})
            valid = {"Content-Type": "application/json", "Origin": origin, "X-Panel-Token": token}
            with patch.object(self.controller, "action") as action:
                for headers in ({}, {**valid, "Origin": "http://evil.test"},
                                {**valid, "X-Panel-Token": "wrong"}, {**valid, "Origin": "null"}):
                    self.assertEqual(request("POST", "/api/action", body, headers)[0], 403)
                action.assert_not_called()
                self.assertEqual(request("POST", "/api/action", body, valid)[0], 200)
                action.assert_called_once_with("demo")
                self.assertEqual(request("POST", "/api/action", "x"*1025, valid)[0], 409)
                self.assertEqual(request("POST", "/api/action", '{"action":[],"path":"/"}', valid)[0], 409)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
