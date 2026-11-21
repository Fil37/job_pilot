"""job_pilot — entry point of the web application.

    .venv/bin/python -m app.web.app        then http://127.0.0.1:8795/

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
    """While the application runs (no system timer): daily chain at schedule.daily_at, inbox every
    schedule.inbox_every_min minutes, weekly analysis on schedule.weekly_day, due portfolio publications every
    schedule.portfolio_every_min minutes. One run at a time; a busy period is simply retried at the next tick."""
    sch = orch.config["schedule"]
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    last = {"daily": None, "weekly": None, "inbox": 0.0, "portfolio": 0.0}
    while not stop.wait(30):
        now, mono = time.localtime(), time.monotonic()
        due = []
        if time.strftime("%H:%M", now) >= sch["daily_at"] and last["daily"] != now.tm_yday:
            due.append("daily")
        if days[now.tm_wday] == sch.get("weekly_day", "monday") and time.strftime("%H:%M", now) >= sch["daily_at"] \
                and last["weekly"] != now.tm_yday:
            due.append("weekly")
        if mono - last["inbox"] >= sch.get("inbox_every_min", 15) * 60:
            due.append("inbox")
        if mono - last["portfolio"] >= sch.get("portfolio_every_min", 60) * 60:
            due.append("portfolio")
        for chain in due:
            if orch.busy():
                break
            try:
                orch.run(chain, title=f"{chain} (scheduler)")
            except Exception as exc:  # noqa: BLE001 - logged; the scheduler keeps running
                orch.store.log_event("error", "scheduler_failed", f"{chain}: {type(exc).__name__}: {exc}", agent_id="agent000")
            last[chain] = now.tm_yday if chain in ("daily", "weekly") else time.monotonic()


def main() -> int:
    try:
        orch = Orchestrator()
        app = create_app(orch)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if orch.config["schedule"].get("scheduler_enabled"):
        threading.Thread(target=scheduler, args=(orch, threading.Event()), daemon=True, name="scheduler").start()
    port = int(orch.config["web"].get("port", 8795))
    # PORT_IN_USE (real case 2026-11-21: the harness web app was already on 8791, job_pilot could not start and the
    # address showed the harness): say it clearly instead of failing silently.
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"PORT {port} ALREADY IN USE (another application?). Change settings.web.port or stop it.",
                  file=sys.stderr)
            return 3
    print(f"job_pilot: http://127.0.0.1:{port}/")
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
