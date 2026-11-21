"""agent006 — no invented content, correction loop of the model, LaTeX escaping, ATS check, full package (fake model,
real Tectonic when installed), web actions (nothing is sent)."""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import EXAMPLE_PROFILE, SCHEMAS, FakeWeb, env_vars, ft_offer, make_orch, run_main  # noqa: E402

from app.agent.agent001_profile_builder import load_profile  # noqa: E402
from app.agent.agent006_application_builder import (ats_problems, ask_with_corrections, engine, fact_problems,  # noqa: E402
                                                     novel_words, profile_corpus, tex)

LEX = SCHEMAS["words"]["skill_lexicon"]
CORPUS = profile_corpus(load_profile(EXAMPLE_PROFILE))


def test_invented_figure_or_technology_is_detected():
    """Case CV_FACTS_FROM_PROFILE: '45 %' and 'Kubernetes' are not in the example profile; '30 %' and Airflow are."""
    assert fact_problems("Réduction de 30 % avec Airflow et Python.", CORPUS, LEX) == []
    probs = " | ".join(fact_problems("Réduction de 45 % du coût sur Kubernetes.", CORPUS, LEX))
    assert "['45']" in probs and "kubernetes" in probs


def test_claims_absent_from_the_profile_are_detected():
    """Incident 2026-11-21 (Alice & Bob CV, check SUMMARY_WORDS_FROM_PROFILE): 'avec un souci de robustesse et de
    maintenabilité' was added by the model."""
    assert novel_words("Pipeline d'ingestion quotidien en Python", CORPUS) == []
    assert novel_words("avec un souci de robustesse et de maintenabilité", CORPUS) == ["maintenabilite", "robustesse"]


def test_model_text_is_escaped_never_executed():
    """Case: a model answer containing LaTeX commands stays text (\\input, \\write18 are escaped); **bold** works."""
    out = tex(r"Projet \input{/etc/passwd} \write18{ls} 100% & **FPGA** #1 _x_")
    assert r"\input" not in out and r"\write18" not in out and r"\textbackslash{}input" in out
    assert r"100\%" in out and r"\&" in out and r"\textbf{FPGA}" in out and r"\#1" in out


class FakeClient:
    def __init__(self, answers):
        self.answers, self.messages = list(answers), []

    def chat_json(self, messages, **kw):
        self.messages.append(list(messages))
        return self.answers.pop(0)


def test_correction_loop_names_the_defect_and_stops_after_three():
    """Case (skill rule): at most 3 re-sends, each one naming the defect; then None (the program uses your wording)."""
    class Ctx:
        run_id = task_id = "t"
    bad = {"x": 1}
    client = FakeClient([bad] * 4)
    ans, probs, n = ask_with_corrections(client, "sys", {}, lambda a: ["figures not in the profile: ['45']"],
                                         purpose="t", ctx=Ctx, max_corrections=3)
    assert ans is None and n == 3 and len(client.messages) == 4
    assert "figures not in the profile: ['45']" in client.messages[-1][-1]["content"]
    client = FakeClient([bad, {"x": 2}])
    ans, probs, n = ask_with_corrections(client, "sys", {}, lambda a: [] if a["x"] == 2 else ["defect"],
                                         purpose="t", ctx=Ctx, max_corrections=3)
    assert ans == {"x": 2} and n == 1


def test_ats_check_ignores_accents_and_hyphenation_but_finds_missing_bullets():
    """Case CV_ATS_TEXT: 'Dévelop-\\npement' in the PDF text matches the bullet; a missing bullet is reported."""
    text = "Franck Essome\nDévelop-\npement d'un firmware bare-metal en C\n"
    assert ats_problems(text, ["Développement d'un **firmware bare-metal en C**"]) == []
    assert ats_problems(text, ["Accéléromètre MEMS capacitif"])


def fake_model(answer_for):
    def transport(url, headers, body, timeout):
        content = answer_for(body)
        return 200, {"choices": [{"message": {"content": json.dumps(content)}}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    return transport


def test_full_package_with_a_model_that_invents_then_corrects():
    """Case: the model first adds '45 %' to the summary, is corrected once, the CV compiles on one page, the package
    and the 'ready' row exist, the calls are filed with their cost. Needs Tectonic (installed in ~/.local/bin)."""
    calls = []
    summary_ok = ("Ingénieur diplômé spécialisé en données, à l'aise sur Python et SQL, cherchant un premier poste en "
                  "CDI. Conception d'un pipeline d'ingestion Python / Airflow traitant 2 millions de lignes par jour "
                  "et réduction de 30 % du temps de calcul d'un rapport en optimisant les requêtes SQL.")

    def answer(body):
        calls.append(body)
        ids = re.findall(r'"id": "(prj_[a-z_]+)"', body["messages"][1]["content"])
        summary = summary_ok if len(calls) > 1 else summary_ok.replace("30 %", "45 %")
        return {"headline": "Ingénieur data junior", "tagline": "Python · SQL · Airflow", "summary": summary,
                "project_order": ids}

    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="s", DEEPSEEK_API_KEY="test-key"):
        orch = make_orch(transport=FakeWeb(ft=[ft_offer(1)]))
        orch.run("daily")
        oid = sqlite3.connect(orch.db_path).execute("SELECT offer_id FROM offers").fetchone()[0]
        orch_ctx_transport = fake_model(answer)
        from app.agent import agent000_job_pilot as a0
        original = a0.Orchestrator._ctx

        def ctx_with_model(self, *a, **k):
            c = original(self, *a, **k)
            c.llm_transport = orch_ctx_transport
            return c
        a0.Orchestrator._ctx = ctx_with_model
        try:
            r = orch.run("application", params={"agent006": {"offer_id": oid}})
        finally:
            a0.Orchestrator._ctx = original
    if not engine(type("C", (), {"config": orch.config})()):
        assert r["status"] == "failed"          # no Tectonic: the failure is reported, never hidden
        return
    assert r["status"] == "succeeded", r["tasks_failed"]
    assert len(calls) == 2 and "45" in calls[1]["messages"][-1]["content"]
    folder = orch.runs_dir / r["run_id"] / f"application_{oid}"
    assert (folder / "cv.pdf").is_file() and (folder / "package.md").is_file()
    assert "45 %" not in (folder / "cv.txt").read_text() and "30 %" in (folder / "cv.txt").read_text().replace(" ", " ")
    c = sqlite3.connect(orch.db_path)
    assert c.execute("SELECT status, cv_version FROM applications").fetchone() == ("ready", r["run_id"])
    calls.clear()
    a0.Orchestrator._ctx = ctx_with_model
    try:
        with env_vars(DEEPSEEK_API_KEY="test-key"):
            r2 = orch.run("application", params={"agent006": {"offer_id": oid}})
    finally:
        a0.Orchestrator._ctx = original
    # ONE_READY_PACKAGE_PER_OFFER: the second preparation replaces the first one still 'ready'
    assert c.execute("SELECT COUNT(*), MAX(cv_version) FROM applications").fetchone() == (1, r2["run_id"])
    assert c.execute("SELECT COUNT(*), SUM(estimated_cost IS NULL) FROM llm_calls WHERE run_id=?", (r["run_id"],)).fetchone()[0] == 2
    assert "test-key" not in str(c.execute("SELECT * FROM llm_calls").fetchall())


def test_web_application_file_route_is_confined_and_mark_sent_needs_csrf():
    """Case: only the package files of a recorded application are served; 'mark as sent' needs the token and changes
    the offer to 'applied' (nothing is sent by the program)."""
    from app.web.app import create_app
    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="s"):
        orch = make_orch(transport=FakeWeb(ft=[ft_offer(1)]))
        orch.run("daily")
    c = sqlite3.connect(orch.db_path)
    oid = c.execute("SELECT offer_id FROM offers").fetchone()[0]
    pkg = orch.runs_dir / "r1" / f"application_{oid}"
    pkg.mkdir(parents=True)
    (pkg / "package.md").write_text("# test")
    with c:
        c.execute("INSERT INTO applications VALUES ('a_1', ?, 'ready', 'r1', 'x', 'france_travail', ?, 'r1', 'now', NULL)",
                  (oid, f"r1/application_{oid}"))
    app = create_app(orch, start=False)
    cl = app.test_client()
    assert cl.get("/applications/a_1/package.md").data == b"# test"
    assert cl.get("/applications/a_1/..%2F..%2Fjob_pilot.json").status_code == 404
    assert cl.get("/applications/a_2/package.md").status_code == 404
    assert cl.post("/applications/a_1/sent").status_code == 400
    tok = re.search(r'name="csrf" value="([^"]+)"', cl.get("/").data.decode()).group(1)
    cl.post("/applications/a_1/sent", data={"csrf": tok})
    assert c.execute("SELECT status FROM applications").fetchone()[0] == "sent"
    assert c.execute("SELECT status FROM offers WHERE offer_id=?", (oid,)).fetchone()[0] == "applied"


if __name__ == "__main__":
    run_main(globals())
