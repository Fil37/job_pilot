"""WORKFLOW_CONTEXTS (2026-11-26, real case): `${{ runner.temp }}` in the job-level `env` made the workflow invalid;
every push failed ("workflow file issue") and, worse, the scheduled chains stopped (no daily collection on
2026-11-26). The job-level env may only use github, inputs, matrix, needs, secrets, strategy and vars."""
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import run_main  # noqa: E402

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/job_pilot.yml"
ALLOWED = {"github", "inputs", "matrix", "needs", "secrets", "strategy", "vars"}


def test_job_level_env_uses_allowed_contexts_only():
    data = yaml.safe_load(WORKFLOW.read_text())
    for name, job in data["jobs"].items():
        for key, value in (job.get("env") or {}).items():
            for context in re.findall(r"\$\{\{\s*([a-z_]+)\.", str(value)):
                assert context in ALLOWED, f"{name}.env.{key} uses '{context}', not available at job level"


def test_schedules_are_still_declared():
    data = yaml.safe_load(WORKFLOW.read_text())
    triggers = data.get("on") or data.get(True)
    assert {s["cron"] for s in triggers["schedule"]} == {"30 5 * * *", "15 * * * *", "0 6 * * 1"}


if __name__ == "__main__":
    run_main(globals())
