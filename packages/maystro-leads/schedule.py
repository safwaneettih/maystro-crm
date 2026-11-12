import fcntl
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from zoneinfo import ZoneInfo

from leads_daily import BASE, load_config, log


class Collector:
    def __init__(self):
        self.config = load_config()
        self.state_dir = Path(os.environ["LEADS_DATA_DIR"])
        self.ready = bool(os.environ.get("LEADS_TWENTY_API_KEY", "").strip())
        self.zone = ZoneInfo("Africa/Tunis")
        self.lock = Lock()
        self.wake = Event()
        self.running = False
        self.requested = False
        self.settings = {"enabled": True, "keywords": self.config["keywords"], "minScore": self.config["min_score"]}
        self.settings.update(self.read("settings.json") or {})

    def read(self, name):
        path = self.state_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write(self, name, data):
        temporary = self.state_dir / (name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_dir / name)

    def status(self):
        database = self.state_dir / "leads.db"
        imported, pending = 0, 0
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                imported = connection.execute("SELECT COUNT(*) FROM pushes WHERE adapter='twenty'").fetchone()[0]
                pending = connection.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0]
        with self.lock:
            last_run = self.read("run.json")
            if last_run and last_run["status"] == "running" and not self.running:
                last_run["status"] = "interrupted"
            return {"ready": self.ready, "busy": self.running or self.requested, "settings": self.settings.copy(),
                    "lastRun": last_run, "imported": imported, "pending": pending}

    def save_settings(self, settings):
        if (not isinstance(settings, dict) or type(settings.get("enabled")) is not bool
                or type(settings.get("minScore")) is not int or not 1 <= settings["minScore"] <= 5):
            raise ValueError("Invalid settings")
        keywords = settings.get("keywords")
        if (not isinstance(keywords, list) or not 1 <= len(keywords) <= 10
                or any(not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 100 for keyword in keywords)):
            raise ValueError("Invalid keywords")
        with self.lock:
            updated = {"enabled": settings["enabled"], "minScore": settings["minScore"],
                       "keywords": list(dict.fromkeys(keyword.strip() for keyword in keywords))}
            self.write("settings.json", updated)
            self.settings = updated
        self.wake.set()

    def request_run(self):
        with self.lock:
            if not self.ready or self.running or self.requested:
                return False
            self.requested = True
        self.wake.set()
        return True

    def schedule(self):
        marker = self.state_dir / "last-scheduled-run"
        while True:
            self.wake.clear()
            now = datetime.now(self.zone)
            today = now.date().isoformat()
            with self.lock:
                due = self.settings["enabled"] and now.hour >= 8 and (
                    not marker.exists() or marker.read_text(encoding="utf-8") != today)
                settings = self.settings.copy() if self.ready and (due or self.requested) else None
                if settings:
                    self.running = True
                    self.requested = False
                    if due:
                        marker.write_text(today, encoding="utf-8")
                    run = {"status": "running", "startedAt": now.isoformat(), "finishedAt": None}
                    self.write("run.json", run)
            if settings:
                exit_code = 1
                try:
                    with (self.state_dir / "schedule.lock").open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        env = {**os.environ, "LEADS_CONFIG_JSON": json.dumps({
                            **json.loads(os.environ.get("LEADS_CONFIG_JSON") or "{}"),
                            "keywords": settings["keywords"], "min_score": settings["minScore"],
                        })}
                        exit_code = subprocess.run([sys.executable, str(BASE / "leads_daily.py"), "run"],
                                                   env=env, check=False).returncode
                finally:
                    with self.lock:
                        self.running = False
                        self.write("run.json", {**run, "status": "failed" if exit_code else "completed",
                                                "finishedAt": datetime.now(self.zone).isoformat()})
            self.wake.wait(30)


def main():
    collector = Collector()
    secret = os.environ.get("LEADS_CONTROL_SECRET", "")
    log("Collecte planifiée chaque jour à 08:00 (Africa/Tunis)", collector.config)
    if not secret:
        collector.schedule()
        return

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            if hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {secret}".encode()):
                return True
            self.respond(401, {"error": "Unauthorized"})
            return False

        def do_GET(self):
            if self.authorized():
                if self.path == "/status":
                    self.respond(200, collector.status())
                else:
                    self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path == "/run":
                if not collector.request_run():
                    self.respond(409, {"error": "Collector unavailable or already running"})
                    return
            elif self.path == "/settings":
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    if not 1 <= size <= 8192:
                        raise ValueError("Invalid request size")
                    collector.save_settings(json.loads(self.rfile.read(size)))
                except (ValueError, UnicodeError):
                    self.respond(400, {"error": "Invalid collector settings"})
                    return
            else:
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, collector.status())

    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    collector.schedule()


if __name__ == "__main__":
    main()
