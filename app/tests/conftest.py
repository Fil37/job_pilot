"""Shared test helpers of job_pilot (no network, no model, temporary databases only).

Guard (socle busy_runs, auto_clip TESTS_NOT_DURING_A_RUN): the suite refuses to start while a real run of
app/instance/job_pilot.db is in progress.
Every test file also runs without pytest:  .venv/bin/python app/tests/test_<subject>.py  (PASS / FAIL per case).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import DB_PATH, load_schemas  # noqa: E402
from app.agent import supervision as sv  # noqa: E402

_busy = sv.busy_runs(DB_PATH)
if _busy:
    raise SystemExit(f"refusing to run the tests: real run(s) in progress {_busy}")

EXAMPLE_PROFILE = ROOT / "app/assets/profile.example.yaml"
_EXAMPLE_JSON = ROOT / "app/assets/job_pilot.example.json"
# The harness profile (app/harness.yaml) copies only .py files and the five schemas: the example instance is read
# when present, so test_experience.py runs in its container without app/assets/.
EXAMPLE_INSTANCE = json.loads(_EXAMPLE_JSON.read_text(encoding="utf-8")) if _EXAMPLE_JSON.is_file() else None


def make_instance(tmp: Path, **criteria) -> dict:
    inst = copy.deepcopy(EXAMPLE_INSTANCE)
    prof = tmp / "profile.yaml"
    if not prof.exists():
        prof.write_text(EXAMPLE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    inst["profile_path"] = str(prof)
    inst["criteria"].update(criteria)
    return inst


def ft_offer(i, title="Data Engineer (H/F)", company="ACME SAS", city="75 - Paris 11e", desc=None, contract="CDI",
             salary="Annuel de 42000,00 Euros à 48000,00 Euros", label="D"):
    return {"id": f"FT{i}", "intitule": title, "entreprise": {"nom": company}, "lieuTravail": {"libelle": city},
            "typeContrat": contract, "salaire": {"libelle": salary}, "experienceExige": label,
            "description": desc or "Vous construirez des pipelines Python, SQL et Airflow. Une première expérience de 1 an est appréciée.",
            "dateCreation": "2026-11-20T08:00:00Z", "origineOffre": {"urlOrigine": f"https://example.org/ft/{i}"}}


def gh_job(i, title="Data Engineer", location="Paris, France", content=None):
    return {"id": i, "title": title, "absolute_url": f"https://boards.example.org/acme/{i}",
            "location": {"name": location}, "updated_at": "2026-11-20T08:00:00Z",
            "content": content or "&lt;p&gt;Build pipelines in Python and SQL. 2+ years of experience.&lt;/p&gt;"}


class FakeWeb:
    """Injectable transport of agent002: canned answers per URL, every call recorded."""

    def __init__(self, ft=None, gh=None, fail=()):
        self.ft, self.gh, self.fail, self.calls = ft or [], gh or {}, set(fail), []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url))
        if any(f in url for f in self.fail):
            return 503, "unavailable"
        if "access_token" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 1499})
        if "offresdemploi" in url:
            return (206 if self.ft else 204), json.dumps({"resultats": self.ft}) if self.ft else ""
        if "greenhouse" in url:
            token = url.split("/boards/")[1].split("/")[0]
            return 200, json.dumps({"jobs": self.gh.get(token, [])})
        if "lever.co" in url:
            return 200, "[]"
        return 404, "not found"


def make_orch(tmp: Path | None = None, transport=None, instance=None, **criteria):
    from app.agent.agent000_job_pilot import Orchestrator
    tmp = Path(tmp or tempfile.mkdtemp())
    inst = instance or make_instance(tmp, **criteria)
    orch = Orchestrator(db_path=tmp / "t.db", instance=inst, runs_dir=tmp / "runs", transport=transport, backup=False)
    orch.startup()
    return orch


class env_vars:
    """Context manager setting environment variables (works with and without pytest)."""

    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_main(namespace: dict) -> None:
    """PASS / FAIL per case when a test file is run directly."""
    failed = 0
    for name, fn in sorted((k, v) for k, v in namespace.items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)


SCHEMAS = load_schemas()
