"""agent000 — chains, skipped / not_run agents, critical stop, crashes as incidents, report and digest, writes."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import FakeWeb, env_vars, ft_offer, gh_job, make_instance, make_orch, run_main  # noqa: E402

FT_KEYS = {"FT_CLIENT_ID": "id-test", "FT_CLIENT_SECRET": "secret-test"}


def daily_web():
    # label "E" (experience required) for Beta: with the default label "D" (beginner accepted) the filter rightly
    # sends "7 years" to review as CONTRADICTION_LABEL_TEXT (expectation corrected 2026-11-21)
    return FakeWeb(ft=[ft_offer(1), ft_offer(2, title="Senior Data Engineer", company="Beta", label="E",
                                         desc="Vous avez 7 ans d'expérience en data engineering."),
                       ft_offer(3, title="Data Engineer", company="Gamma", city="69 - Lyon",
                                desc="Jeune diplômé accepté. Python, SQL.")],
                   gh={"acme": [gh_job(10)]})


def tasks(orch, run_id):
    with sqlite3.connect(orch.db_path) as c:
        return dict(c.execute("SELECT agent_id, status FROM sup_tasks WHERE run_id=?", (run_id,)).fetchall())


def test_daily_chain_end_to_end():
    """Case: the daily chain with fake sources: the 6 agents ran (agent007 daily since 2026-11-23: a project per offer) (agent005 enabled since 2026-11-21: its company
    sheets say 'not found' with the fake registry); the
    report has every field of reports.json, the digest lists the kept offer and its file exists."""
    with env_vars(**FT_KEYS):
        orch = make_orch(transport=daily_web(), target_companies=[{"name": "ACME", "greenhouse": "acme"}])
        r = orch.run("daily")
    assert r["status"] == "succeeded", r
    t = tasks(orch, r["run_id"])
    assert t == {"agent001": "succeeded", "agent002": "succeeded", "agent003": "succeeded", "agent004": "succeeded",
                 "agent005": "succeeded", "agent007": "succeeded"}
    assert r["results"]["agent003"] == {**r["results"]["agent003"], "keep": 2, "reject": 1}
    top = r["digest"]["top"]
    assert [o["company"] for o in top] == ["ACME SAS"]        # Gamma: Lyon is outside the areas → eliminated
    assert (orch.runs_dir / r["run_id"] / "digest.md").read_text().count("ACME") == 1
    saved = json.loads((orch.runs_dir / r["run_id"] / "report.json").read_text())
    assert saved["run_id"] == r["run_id"] and saved["costs"]["calls"] == 0
    with sqlite3.connect(orch.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM agent_results WHERE run_id=?", (r["run_id"],)).fetchone()[0] == 6
        assert c.execute("SELECT status FROM sup_runs WHERE run_id=?", (r["run_id"],)).fetchone()[0] == "succeeded"
    assert orch.store.health()["agent003"] == "healthy"


def test_skip_and_only_are_recorded():
    """Case: --skip agent002 → recorded 'skipped' (never absent: it must not look like a failure)."""
    with env_vars(**FT_KEYS):
        orch = make_orch(transport=daily_web())
        r = orch.run("daily", skip=["agent002"])
    t = tasks(orch, r["run_id"])
    assert t["agent002"] == "skipped" and t["agent003"] == "succeeded"
    assert any("agent002 non exécuté (sauté)" in lim for lim in r["limits"])


def test_critical_failure_stops_the_run():
    """Case: profile file missing → agent001 'blocked' → agents 002-004 'not_run', run 'failed'."""
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    inst = make_instance(tmp)
    inst["profile_path"] = str(tmp / "missing.yaml")
    orch = make_orch(tmp, instance=inst)
    r = orch.run("daily")
    t = tasks(orch, r["run_id"])
    assert t["agent001"] == "blocked" and t["agent002"] == t["agent003"] == t["agent004"] == "not_run"
    assert r["status"] == "failed"


def test_an_agent_crash_becomes_an_incident_and_degraded_health():
    """Case: an exception inside agent003 → task failed (software_failure), incident opened, agent 'degraded';
    the orchestrator itself does not crash and the rest of the run is 'not_run'."""
    from app.agent import agent003_experience_filter as a3
    with env_vars(**FT_KEYS):
        orch = make_orch(transport=daily_web())
        original = a3.Agent.work
        a3.Agent.work = lambda self, ctx, sub: 1 / 0
        try:
            r = orch.run("daily")
        finally:
            a3.Agent.work = original
    t = tasks(orch, r["run_id"])
    assert t["agent003"] == "failed" and t["agent004"] == "not_run"
    with sqlite3.connect(orch.db_path) as c:
        cat = c.execute("SELECT error_category FROM incidents WHERE agent_id='agent003'").fetchone()[0]
    assert cat == "software_failure" and orch.store.health()["agent003"] == "degraded"


def test_invalid_envelope_is_refused():
    """Case: an agent returning a status outside the contract ('done') is recorded failed, not trusted."""
    from app.agent import agent004_offer_scorer as a4
    with env_vars(**FT_KEYS):
        orch = make_orch(transport=daily_web())
        original = a4.Agent.work
        a4.Agent.work = lambda self, ctx, sub: {"status": "done"}
        try:
            r = orch.run("daily")
        finally:
            a4.Agent.work = original
    assert tasks(orch, r["run_id"])["agent004"] == "failed"


def test_user_override_is_kept_and_rescore_does_not_redecide():
    """Case: you keep an offer the filter rejected (7 years asked); a rescore run does not overwrite your decision,
    agent004 scores it again with 'keep' and the offer enters the digest."""
    with env_vars(**FT_KEYS):
        orch = make_orch(transport=daily_web())
        orch.run("daily")
        with sqlite3.connect(orch.db_path) as c:
            oid = c.execute("SELECT offer_id FROM offers WHERE company='Beta'").fetchone()[0]
            c.execute("UPDATE experience_decisions SET user_override='keep' WHERE offer_id=?", (oid,))
        r = orch.run("rescore")
    with sqlite3.connect(orch.db_path) as c:
        assert c.execute("SELECT user_override FROM experience_decisions WHERE offer_id=?", (oid,)).fetchone()[0] == "keep"
    assert r["results"]["agent003"]["decided"] == 0
    assert oid in [o["offer_id"] for o in r["digest"]["top"]]


def test_agent000_has_a_health_state():
    """User question 2026-11-22 ('pourquoi agent000 est inconnu', check AGENT000_HEALTH): the orchestrator gets a
    health state at start and stays healthy after a run (it was 'unknown' on the Supervision page)."""
    orch = make_orch()
    assert orch.store.health()["agent000"] == "healthy"
    orch.run(None, only=["agent001"])
    assert orch.store.health()["agent000"] == "healthy"
    assert "agent000" in [a["id"] for a in orch.store.dashboard(orch.agent_ids)["agents"] if a["state"] == "healthy"]


if __name__ == "__main__":
    run_main(globals())
