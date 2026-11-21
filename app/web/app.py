"""job_pilot — entry point of the web application.

    .venv/bin/python -m app.web.app        then http://127.0.0.1:8791/

Loads and validates the configuration, starts agent000 (skills, health checks, interrupted runs, daily backup),
creates the Flask application and, when settings.schedule.scheduler_enabled is true, a small in-process scheduler
that launches the daily chain once a day at settings.schedule.daily_at (local time) while the application runs.
Listens on 127.0.0.1 only. No business logic here.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from flask import Flask

from app.agent import APP_DIR, ConfigError
from app.agent.agent000_job_pilot import Orchestrator
from app.web import page


def create_app(orch: Orchestrator | None = None, *, start: bool = True) -> Flask:
    orch = orch or Orchestrator()
    if start:
        orch.startup()
    app = Flask(__name__, template_folder=str(APP_DIR / "web" / "templates"), static_folder=str(APP_DIR / "static"),
                static_url_path="/static")
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=256 * 1024)
    app.extensions["job_pilot"] = orch
    page.register(app, orch)
    return app


def scheduler(orch: Orchestrator, stop: threading.Event) -> None:
    """Daily chain once a day at schedule.daily_at, only while the application runs (no system timer)."""
    at = orch.config["schedule"]["daily_at"]
    done_day = None
    while not stop.wait(30):
        now = time.localtime()
        if time.strftime("%H:%M", now) >= at and done_day != now.tm_yday and not orch.busy():
            done_day = now.tm_yday
            try:
                orch.run("daily", title="daily (scheduler)")
            except Exception as exc:  # noqa: BLE001 - logged; the scheduler keeps running
                orch.store.log_event("error", "scheduler_failed", f"{type(exc).__name__}: {exc}", agent_id="agent000")


def main() -> int:
    try:
        orch = Orchestrator()
        app = create_app(orch)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if orch.config["schedule"].get("scheduler_enabled"):
        threading.Thread(target=scheduler, args=(orch, threading.Event()), daemon=True, name="scheduler").start()
    web = orch.config["web"]
    app.run(host="127.0.0.1", port=int(web.get("port", 8791)), debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
