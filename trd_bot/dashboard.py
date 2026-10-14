"""Loopback-only control panel. Reuses the paper CLI and its risk boundaries."""
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser

from .research_config import ResearchConfig as Config
from .ledger import read_snapshot

LOG = logging.getLogger("trd_bot")
ASSETS = Path(__file__).with_name("web")


def tail(path, lines=40):
    if not path.exists():
        return []
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - 32768))
        data = handle.read().decode("utf-8", errors="replace").splitlines()
    if size > 32768:
        data = data[1:]
    return data[-lines:]


class Controller:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config = self.root / "config.json"
        self.cfg = Config.load(self.config)
        self.directory = self.root / "runtime" / "dashboard" / "v2-paper"
        self.process = None
        self.started_at = 0
        self.stopping = False
        self.lock = threading.RLock()
        self.closed = False

    def running(self):
        return self.process is not None and self.process.poll() is None

    def argv(self, command, directory):
        return [sys.executable, "-m", "trd_bot", command, "--config", str(self.config),
                "--runtime", str(directory)]

    def spawn(self, command, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.started_at = time.time()
        self.stopping = False
        args = self.argv(command, directory)
        args += ["--seconds", str(self.cfg.duration_seconds)] if command == "paper" else ["--steps", "600"]
        with (directory / "panel.log").open("ab") as logfile:
            self.process = subprocess.Popen(args, cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=logfile, stderr=subprocess.STDOUT, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        LOG.info("Panel started %s pid=%s", command, self.process.pid)

    def action(self, action):
        with self.lock:
            if self.closed:
                raise ValueError("面板正在關閉")
            if action == "stop":
                if not self.running():
                    raise ValueError("目前沒有執行中的 bot")
                (self.directory / "KILL").touch()
                self.stopping = True
                LOG.warning("Panel requested stop pid=%s", self.process.pid)
                return
            if action not in ("demo", "start", "resume"):
                raise ValueError("不支援的操作")
            if self.running():
                raise ValueError("請先停止目前的 bot")
            directory = self.root / "runtime" / "dashboard" / "v2-paper"
            if action == "demo":
                directory = directory.parent / ("demo-" + uuid.uuid4().hex[:12])
                self.spawn("demo", directory)
                return
            if (directory / "engine.lock").exists():
                raise ValueError("帳戶仍有程序鎖；請先確認舊程序已停止，面板不會自動刪鎖")
            if action == "start" and ((directory / "ledger.sqlite3").exists() or (directory / "state.json").exists() or (directory / "KILL").exists()):
                raise ValueError("此帳戶已有執行紀錄，請使用「審查後恢復」保留資產與風控基準")
            if action == "resume":
                if not (directory / "ledger.sqlite3").exists():
                    raise ValueError("尚無公開行情模擬帳戶，請先開始模擬")
                with (directory / "panel.log").open("ab") as logfile:
                    result = subprocess.run(self.argv("resume", directory) + ["--acknowledge-risk"],
                        cwd=self.root, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                        shell=False, timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                if result.returncode:
                    self.directory = directory
                    raise ValueError("恢復檢查失敗，請查看日誌；未啟動交易")
                LOG.warning("Panel user approved resume; risk baselines retained")
            self.spawn("paper", directory)

    def snapshot(self):
        with self.lock:
            running = self.running()
            report, error = {}, None
            path = self.directory / "health.json"
            try:
                if path.exists():
                    report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                error = "報表暫時無法讀取，請查看日誌"
            current = bool(report) and path.stat().st_mtime >= self.started_at if path.exists() else False
            age = max(0, (time.time() * 1000 - report["last_book_ms"]) / 1000) if report.get("last_book_ms") else None
            demo = self.directory.name.startswith("demo-")
            status = "idle"
            if running:
                status = "stopping" if self.stopping else "demo" if demo else "connecting"
                if not self.stopping and not demo and current and report.get("halt_reason"):
                    status = "halting"
                elif not self.stopping and not demo and current and age is not None and report.get("data_fresh", True):
                    status = "running" if age <= self.cfg.max_data_age_ms / 1000 else "stale"
            elif self.process is not None and self.process.returncode:
                status = "failed"
            elif report.get("halt_reason"):
                status = "halted"
            elif self.process is not None:
                status = "failed" if self.process.returncode else "finished"
            fills = []
            for line in tail(self.directory / "fills.jsonl", 12):
                try:
                    fills.append(json.loads(line))
                except ValueError:
                    continue
            paper = self.root / "runtime" / "dashboard" / "v2-paper"
            paper_review = {}
            if (paper / "ledger.sqlite3").exists():
                try:
                    saved = read_snapshot(paper)
                    paper_review = {key: saved.get(key) for key in ("halt_reason", "cash", "base")}
                except (OSError, ValueError):
                    paper_review = {"error": "公開行情帳戶狀態無法讀取，請檢查原始狀態檔"}
            return {"status": status, "running": running, "report": report, "error": error,
                "current_report": current and running, "book_age_seconds": age,
                "runtime": str(self.directory.relative_to(self.root)),
                "exit_code": self.process.poll() if self.process else None,
                "config": asdict(self.cfg), "fills": list(reversed(fills)),
                "logs": tail(self.directory / "panel.log"),
                "paper_exists": (paper / "ledger.sqlite3").exists(),
                "paper_review": paper_review, "paper_logs": tail(paper / "panel.log", 12),
                "paper_locked": (paper / "engine.lock").exists()}

    def shutdown(self):
        with self.lock:
            self.closed = True
            if not self.running():
                return
            (self.directory / "KILL").touch()
            process = self.process
        LOG.warning("Panel closing; waiting for paper engine cleanup")
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            LOG.error("Worker did not stop in 40s; terminating. Retain state and inspect engine.lock before resume")
            process.terminate()
            process.wait(timeout=10)


def make_server(controller, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            LOG.debug("HTTP " + fmt, *args)

        def reply(self, value, code=200, mime="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.trusted_host():
                self.reply({"error": "不允許的 Host"}, 403)
                return
            routes = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            try:
                if self.path == "/api/status":
                    self.reply(controller.snapshot())
                elif self.path in routes:
                    filename, mime = routes[self.path]
                    body = (ASSETS / filename).read_bytes()
                    if filename == "index.html":
                        body = body.replace(b"__TOKEN__", token.encode())
                    self.reply(body, mime=mime)
                else:
                    self.reply({"error": "找不到頁面"}, 404)
            except Exception:
                LOG.exception("Panel read failed")
                self.reply({"error": "讀取失敗，請查看面板服務日誌"}, 500)

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.trusted_host() or self.headers.get("Origin") != origin or
                    not secrets.compare_digest(self.headers.get("X-Panel-Token", ""), token)):
                self.reply({"error": "操作來源驗證失敗，請重新整理本機面板"}, 403)
                return
            if self.path != "/api/action":
                self.reply({"error": "找不到操作"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 or self.headers.get_content_type() != "application/json":
                    raise ValueError("操作格式不正確")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) != {"action"} or not isinstance(data["action"], str):
                    raise ValueError("操作格式不正確")
                controller.action(data["action"])
                self.reply({"ok": True})
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                LOG.warning("Panel action rejected: %s", exc)
                self.reply({"error": str(exc)}, 409)
            except Exception:
                LOG.exception("Panel action failed")
                self.reply({"error": "操作失敗，請查看服務日誌"}, 500)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port=8765, open_browser=False):
    controller = Controller(Path.cwd())
    server = make_server(controller, port)
    url = f"http://127.0.0.1:{server.server_port}"
    LOG.info("Trading panel: %s (Ctrl+C to stop panel and its bot)", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        LOG.info("Panel interrupted")
    finally:
        server.server_close()
        controller.shutdown()
