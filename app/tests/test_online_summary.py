"""ONLINE_SUMMARY (2026-11-26): the summary harness reads of the online copy holds numbers only (the repository is
public): open incidents per agent and category, AI costs, the last run; never a message."""
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import run_main  # noqa: E402

from app.agent.agent000_job_pilot import online_summary  # noqa: E402


def test_numbers_only():
    folder = Path(tempfile.mkdtemp())
    assert online_summary(folder)["data"] is False
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(folder / "job_pilot.db")) as db:
        db.execute("CREATE TABLE incidents (agent_id TEXT, error_category TEXT, diagnostic TEXT, status TEXT)")
        db.execute("CREATE TABLE llm_calls (created_at TEXT, estimated_cost REAL)")
        db.execute("CREATE TABLE sup_runs (status TEXT, completed_at TEXT)")
        db.executemany("INSERT INTO incidents VALUES (?, ?, ?, ?)", [
            ("agent009", "timeout", '{"errors": ["Entretien chez Acme : rappel"]}', "open"),
            ("agent009", "timeout", "{}", "open"), ("agent006", "software_failure", "{}", "resolved")])
        db.executemany("INSERT INTO llm_calls VALUES (?, ?)", [(now, 0.25), (now, None)])
        db.execute("INSERT INTO sup_runs VALUES ('succeeded', ?)", (now,))
        db.commit()
    summary = online_summary(folder)
    assert summary["open_incidents"] == 2 and summary["incidents_by_agent"] == {"agent009:timeout": 2}
    assert summary["costs"]["today"] == 0.25 and summary["costs"]["calls_month"] == 2
    assert summary["costs"]["unpriced_month"] == 1 and summary["last_run"]["status"] == "succeeded"
    assert "Acme" not in json.dumps(summary)


if __name__ == "__main__":
    run_main(globals())
