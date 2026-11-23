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
from app.agent.agent006_application_builder import (ats_problems, ask_with_corrections, cv_offer_match, engine,  # noqa: E402
                                                     fact_problems, novel_words, profile_corpus, tex)

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
        desc = ("Vous construirez des pipelines Python, SQL et Airflow, calculés avec Spark, déployés sur Kubernetes. "
                "Une première expérience de 1 an est appréciée.")
        orch = make_orch(transport=FakeWeb(ft=[ft_offer(1, desc=desc)]))
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
    # CV_OFFER_MATCH (request of the user 2026-11-23): 'spark' is in the profile but in no CV text → added by the
    # program in "Également"; 'kubernetes' is not in the profile → never added, only reported
    match = json.loads((folder / "checks.json").read_text())["offer_match"]
    assert match["added"] == ["spark"] and match["lacking"] == ["kubernetes"] and not match["omitted"], match
    assert {"python", "sql", "airflow", "spark"} <= set(match["present"]) and "Également" in (folder / "cv.txt").read_text()
    assert match["coverage"] == 80 and "Kubernetes" not in (folder / "cv.txt").read_text()
    assert "Correspondance du CV avec l'offre" in (folder / "package.md").read_text()
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


def test_model_texts_have_no_english_long_dash():
    """User request 2026-11-21 (check NO_EM_DASH): 'les tirets anglais là non'. The model answer is cleaned by the
    program in every string of the JSON, lists included."""
    from app.agent.agent006_application_builder import no_em_dash
    ans = no_em_dash({"summary": "Ingénieur FPGA — VHDL et C", "steps": ["J1–J2", "Banc — mesures"]})
    assert ans == {"summary": "Ingénieur FPGA, VHDL et C", "steps": ["J1-J2", "Banc, mesures"]}
    assert "—" not in json.dumps(ans, ensure_ascii=False)


def test_section_is_projets_and_personal_projects_are_labelled():
    """User request 2026-11-22 (check PROJECT_KIND_LABEL): section 'Projets' (not 'Projets académiques
    sélectionnés'); a personal project shows 'Personnel' right after its title, an academic one shows nothing."""
    import tempfile
    from conftest import make_instance
    tmp = Path(tempfile.mkdtemp())
    inst = make_instance(tmp)
    prof = Path(inst["profile_path"])
    prof.write_text(prof.read_text().replace("    name: \"Tableau de bord de qualité de l'air\"",
                                             "    name: \"Tableau de bord de qualité de l'air\"\n    kind: personal"))

    def answer(body):
        ids = re.findall(r'"id": "(prj_[a-z_]+)"', body["messages"][1]["content"])
        return {"headline": "Ingénieur data junior", "tagline": "Python · SQL", "project_order": ids,
                "summary": "Ingénieur diplômé spécialisé en données, à l'aise sur Python et SQL, cherchant un premier "
                           "poste en CDI. Conception d'un pipeline d'ingestion Python / Airflow traitant 2 millions de "
                           "lignes par jour et réduction de 30 % du temps de calcul d'un rapport en optimisant les "
                           "requêtes SQL."}
    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="secret-long-enough", DEEPSEEK_API_KEY="test-key-long"):
        orch = make_orch(tmp, transport=FakeWeb(ft=[ft_offer(1)]), instance=inst)
        orch.run("daily")
        oid = sqlite3.connect(orch.db_path).execute("SELECT offer_id FROM offers").fetchone()[0]
        from app.agent import agent000_job_pilot as a0
        original = a0.Orchestrator._ctx

        def ctx_with_model(self, *a, **k):
            c = original(self, *a, **k)
            c.llm_transport = fake_model(answer)
            return c
        a0.Orchestrator._ctx = ctx_with_model
        try:
            r = orch.run("application", params={"agent006": {"offer_id": oid}, "agent005": {"offer_id": oid}})
        finally:
            a0.Orchestrator._ctx = original
    if not engine(type("C", (), {"config": orch.config})()):
        return
    tex_src = (orch.runs_dir / r["run_id"] / f"application_{oid}" / "cv.tex").read_text()
    assert r"\section{Projets}" in tex_src and "académiques" not in tex_src
    assert r"\project{Tableau de bord de qualité de l'air}{}{Personnel}" in tex_src
    text = (orch.runs_dir / r["run_id"] / f"application_{oid}" / "cv.txt").read_text()
    assert re.search(r"Tableau de bord de qualité de l.air\s+Personnel", text)


def test_cv_offer_match_is_computed_on_the_cv_text():
    """Case (check CV_OFFER_MATCH): coverage over the asked skills, omitted = in the profile but not in the CV text,
    lacking = not in the profile; aliases count; the offer title's domain word is looked for in the headline."""
    lexicon = {"python": ["python"], "vhdl": ["vhdl"], "verilog": ["verilog"], "git": ["git"]}
    m = cv_offer_match("Ingénieur FPGA junior\nVHDL, Python", {"python", "vhdl", "verilog", "git"}, {"python", "vhdl", "git"},
                       lexicon, {}, "Ingénieur FPGA (H/F)", "Ingénieur FPGA junior")
    assert (m["present"], m["omitted"], m["lacking"], m["coverage"]) == (["python", "vhdl"], ["git"], ["verilog"], 50)
    assert m["title_in_headline"] == ["fpga"] and m["coverage_reachable"] == 67
    m = cv_offer_match("", set(), set(), {}, {}, "Electronics Integration Engineer", "Ingénieur électronique et systèmes embarqués")
    assert m["title_in_headline"] == ["electronic"] and m["coverage"] == 100


if __name__ == "__main__":
    run_main(globals())
