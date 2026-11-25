"""agent007 (gap trigger rules, brief), agent008 (demo, approval, publish / remove on a LOCAL bare repository, diff
scope, token never in argv), agent011 (metrics, weights proposal), agent005 (registry matching)."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SCHEMAS, env_vars, make_instance, make_orch, run_main  # noqa: E402

from app.agent.agent005_company_researcher import best_registry_match, company_key  # noqa: E402
from app.agent import upsert  # noqa: E402
from app.agent.agent007_gap_project_planner import offers_to_brief, window_gaps  # noqa: E402
from app.agent.agent008_portfolio_publisher import (Git, card_html, card_year, grid_titles, image_problem,  # noqa: E402
                                                    insert_card, parse_done_on, remove_card)
from app.agent.agent011_pipeline_analyst import propose_weights  # noqa: E402

GAPS = {"window_days": 30, "min_score": 55, "max_missing": 2, "max_briefs_per_run": 3}
BRIEF = {"title": "Bus CAN sur STM32 avec <script>", "objective": "Apprendre Kafka & co.",
         "portfolio_summary": "Chaîne de messages Kafka & co. entre un PC et une carte.", "builds_on": ["python", "sql"],
         "architecture": "Producteur Python → Kafka → consommateur",
         "hardware": [{"name": "PC", "role": "héberge Kafka", "specs": "8 Go de RAM", "example": "portable", "alternative": "VM",
                       "optional": False}], "software": ["Kafka"],
         "steps": [{"days": f"J{i}", "milestone": f"étape {i}", "deliverable": "d"} for i in range(1, 6)],
         "success_criteria": ["1000 messages/s"], "pitfalls": ["partitions"], "interview_pitch": "Projet d'apprentissage.",
         "done_on": "2025-06",
         "card": {"short_name": "Bus CAN", "domain": "Firmware <b>", "context": "Seul", "title": "Bus CAN sur STM32 avec <script>",
                  "summary": "Une chaîne Kafka & co. entre un PC et une carte.", "details_title": "Architecture",
                  "details": "Producteur → consommateur", "tags": ["STM32", "CAN"], "link_url": "https://github.com/x/y",
                  "link_label": "Code sur GitHub", "image_alt": "", "categories": ["firmware", "capteurs"], "position": "first",
                  "figures": [{"value": "1 000", "label": "messages par seconde"}], "image_file": None}}


def test_trigger_rules_are_computed_by_the_program():
    """Case (rules of the user): only favourable offers (not eliminated, keep/review, score >= 55) with 1-2 missing
    skills count."""
    rows = [{"offer_id": "a", "score": 80, "missing": ["kafka"], "eliminated": 0, "decision": "keep"},
            {"offer_id": "b", "score": 70, "missing": ["kafka", "rust"], "eliminated": 0, "decision": "review"},
            {"offer_id": "c", "score": 90, "missing": ["kafka"], "eliminated": 1, "decision": "keep"},
            {"offer_id": "d", "score": 40, "missing": ["kafka"], "eliminated": 0, "decision": "keep"},
            {"offer_id": "e", "score": 80, "missing": ["kafka", "rust", "go"], "eliminated": 0, "decision": "keep"},
            {"offer_id": "f", "score": 80, "missing": ["kafka"], "eliminated": 0, "decision": "reject"}]
    assert window_gaps(rows, GAPS) == {"kafka": ["a", "b"], "rust": ["b"]}



def test_one_project_per_favourable_offer():
    """Rule of the user 2026-11-23 ("le projet à ajouter pour chaque offre"): a single favourable offer is enough, each
    offer gets its own brief (best score first), an offer already briefed (even dismissed) is not proposed again, an
    offer whose missing skill is in the profile is skipped, only the latest score of an offer counts."""
    rows = [{"offer_id": "a", "score": 70, "missing": ["kafka"], "eliminated": 0, "decision": "keep", "scored_at": "1"},
            {"offer_id": "b", "score": 90, "missing": ["kafka", "rust"], "eliminated": 0, "decision": "review", "scored_at": "1"},
            {"offer_id": "c", "score": 80, "missing": ["verilog"], "eliminated": 0, "decision": "keep", "scored_at": "1"},
            {"offer_id": "d", "score": 85, "missing": ["python"], "eliminated": 0, "decision": "keep", "scored_at": "1"},
            {"offer_id": "e", "score": 95, "missing": ["kafka"], "eliminated": 0, "decision": "keep", "scored_at": "1"},
            {"offer_id": "e", "score": 40, "missing": ["kafka"], "eliminated": 0, "decision": "keep", "scored_at": "2"}]
    todo = offers_to_brief(rows, GAPS, own={"python"}, briefed_offers={"c"})
    assert [r["offer_id"] for r in todo] == ["b", "a"]


def seed_offer(c, rows, oid, score, missing):
    now = "2099-01-01T00:00:00+00:00"
    upsert(c, rows, "offers", {"offer_id": oid, "dedup_key": oid, "title": f"Ingénieur FPGA {oid}", "company": "ACME",
           "location": "31 - Toulouse", "description": "Conception FPGA en VHDL et Verilog.", "url": f"https://x/{oid}",
           "source_id": "manual", "external_id": oid, "status": "new", "first_seen_run": "r0", "first_seen_at": now,
           "last_seen_at": now})
    upsert(c, rows, "experience_decisions", {"offer_id": oid, "profile_hash": "h", "run_id": "r0", "decision": "keep",
           "reason_code": "OK", "reason": "ok", "candidate_years": 1, "excludes_internship": False,
           "excludes_apprenticeship": False, "confidence": 1, "decided_at": now})
    upsert(c, rows, "scores", {"offer_id": oid, "profile_hash": "h", "weights_version": "1", "run_id": "r0", "score": score,
           "components": {}, "matched": [], "missing": missing, "eliminated": False, "elimination_reasons": [],
           "scored_at": now})


def test_agent007_writes_one_brief_per_offer_linked_to_it():
    """Case: two favourable offers missing 'verilog' → two briefs, each with its offer_id and an alert; a second run
    proposes nothing more."""
    tmp = Path(tempfile.mkdtemp())
    orch = make_orch(tmp)
    with sqlite3.connect(orch.db_path) as c:
        for oid, sc in (("o1", 80), ("o2", 70)):
            seed_offer(c, orch.schemas["rows"], oid, sc, ["verilog"])
    brief = dict(BRIEF, title="Filtre FIR en Verilog", objective="Un filtre FIR en Verilog simulé.",
                 portfolio_summary="Filtre FIR en Verilog simulé avec Icarus.", software=["Icarus Verilog"])
    from app.agent import agent000_job_pilot as a0
    original = a0.Orchestrator._ctx
    asked = []

    def ctx_with_model(self, *a, **k):
        ctx = original(self, *a, **k)

        def transport(url, headers, body, timeout):
            asked.append(json.loads(body["messages"][-1]["content"]))
            return 200, {"choices": [{"message": {"content": json.dumps(brief)}}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        ctx.llm_transport = transport
        return ctx
    a0.Orchestrator._ctx = ctx_with_model
    try:
        with env_vars(DEEPSEEK_API_KEY="test-key-long"):
            r = orch.run(None, only=["agent007"])
            r2 = orch.run(None, only=["agent007"])
    finally:
        a0.Orchestrator._ctx = original
    assert r["results"]["agent007"]["briefs"] == 2, r["results"]["agent007"]
    assert r2["results"]["agent007"]["briefs"] == 0
    c = sqlite3.connect(orch.db_path)
    got = c.execute("SELECT offer_id, skill, path FROM project_briefs ORDER BY offer_id").fetchall()
    assert [(o, s) for o, s, _ in got] == [("o1", "verilog"), ("o2", "verilog")]
    assert "Offre visée : **Ingénieur FPGA o1 chez ACME** (31 - Toulouse)" in Path(got[0][2]).read_text()
    assert asked[0]["offer"]["title"] == "Ingénieur FPGA o1" and asked[0]["target_skills"] == ["verilog"]
    assert c.execute("SELECT COUNT(*) FROM alerts WHERE category='project'").fetchone()[0] == 2


def test_card_is_like_yours_escaped_and_placed_where_you_chose():
    """Remark of the user 2026-11-23 (check CARD_LIKE_YOURS): the card follows the markup of his 'Autres projets'
    cards (side: short name, domain, context and REAL year, green 'Projet personnel'; his categories; figures; link),
    is made from his sheet only, escapes every text, and goes first, after a chosen card or last; removing gives the
    page back."""
    card = card_html(BRIEF["card"], "bus-can", 2025)
    assert '<article class="proj compact" data-cat="firmware capteurs" data-type="perso">' in card
    assert '<div class="side"><strong>Bus CAN</strong>Firmware &lt;b&gt;<br>Seul, 2025<span class="kind perso">Projet personnel</span></div>' in card
    assert "<script>" not in card and "&lt;script&gt;" in card and "Kafka &amp; co." in card
    assert '<div><b>1 000</b><span>messages par seconde</span></div>' in card and 'href="https://github.com/x/y"' in card
    assert "Apprendre" not in card and "J1" not in card          # nothing of the plan: what you built
    page = ('<main><div class="proj-grid">\n  <article class="proj"><div><h3>Old one</h3></div></article>\n'
            '  <article class="proj"><div><h3>Second</h3></div></article>\n</div></main>\n')
    grid = '<div class="proj-grid">'
    assert grid_titles(page, grid) == ["Old one", "Second"]
    for pos, before, after in (("first", "job_pilot:begin", "Old one"), ("after:Old one", "Old one", "job_pilot:begin"),
                               ("last", "Second", "job_pilot:begin")):
        once = insert_card(page, "bus-can", card, grid, pos)
        assert insert_card(once, "bus-can", card, grid, pos) == once
        assert once.index(before) < once.index(after) and once.index("job_pilot:end") < once.index("</main>")
        assert remove_card(once, "bus-can") == page, pos
    assert "after" not in insert_card(page, "s", card, grid, "after:unknown")[:30]


def test_images_for_your_public_site_are_checked():
    """Check IMAGE_SAFE: known type with real content, size limit, no script / handler / external link in an SVG."""
    assert image_problem("a.png", b"\x89PNG....", 1000) is None
    assert image_problem("a.png", b"GIF89a", 1000) and image_problem("a.gif", b"GIF89a", 1000)
    assert image_problem("a.png", b"\x89PNG" + b"0" * 2000, 1000)
    assert image_problem("a.svg", b'<svg><rect width="2"/></svg>', 1000) is None
    for bad in (b"<svg><script>alert(1)</script></svg>", b'<svg onload="x()"></svg>', b'<svg><a href="https://x.y">x</a></svg>'):
        assert image_problem("a.svg", bad, 1000), bad


def make_remote(tmp: Path) -> Path:
    bare, work = tmp / "remote.git", tmp / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "index.html").write_text('<html><div class="proj-grid">\n<article>old</article>\n</div></html>\n')
    (work / "other.html").write_text("keep me\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["add", "."], ["commit", "-qm", "seed"], ["push", "-q", str(bare), "main"]):
        subprocess.run(["git", *cmd], cwd=work, check=True, env=env)
    return bare


def portfolio_orch(tmp: Path):
    bare = make_remote(tmp)
    inst = make_instance(tmp)
    inst["overrides"]["portfolio"] = {"repo": str(bare)}
    orch = make_orch(tmp, instance=inst)
    img = orch.runs_dir / "portfolio_assets" / "b_1.png"     # where the 'Projet réalisé' sheet stores images
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG fake image")
    brief = {**BRIEF, "card": {**BRIEF["card"], "image_file": str(img), "image_alt": "Banc CAN"}}
    with sqlite3.connect(orch.db_path) as c:
        c.execute("INSERT INTO project_briefs (brief_id, skill, title, brief, path, status, run_id, created_at) "
                  "VALUES ('b_1','kafka',?,?,'x','proposed','r0','now')",
                  (BRIEF["title"], json.dumps(brief)))
    return orch, bare


def remote_index(bare: Path) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "show", "main:index.html"], capture_output=True, text=True).stdout


def test_portfolio_demo_then_approved_publication_then_removal():
    """Case: the demo changes nothing remote; without approval nothing is pushed; after approval with a past date the
    card is pushed (index.html only, additions only); at the end date it is removed."""
    tmp = Path(tempfile.mkdtemp())
    orch, bare = portfolio_orch(tmp)
    before = remote_index(bare)
    r = orch.run("portfolio", params={"agent008": {"brief_id": "b_1"}})
    assert r["status"] == "succeeded", r
    c = sqlite3.connect(orch.db_path)
    entry, status, demo = c.execute("SELECT entry_id, status, demo_path FROM portfolio_entries").fetchone()
    assert status == "demo" and "job_pilot:begin" in Path(demo).read_text() and remote_index(bare) == before
    # a scheduled entry without approval is never pushed
    with c:
        c.execute("UPDATE portfolio_entries SET status='scheduled', publish_at='2000-01-01T00:00'")
    r = orch.run("portfolio")
    assert remote_index(bare) == before and c.execute("SELECT status FROM portfolio_entries").fetchone()[0] == "failed"
    # approved: exact target, past date → published at the next run
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="minutes")
    end = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="minutes")
    aid = orch.store.request_approval("publish_portfolio_entry", entry, {"repo": str(bare), "file": "index.html",
                                      "publish_at": past, "expire_at": end})
    orch.decide(aid, True)
    orch.run("portfolio")
    after = remote_index(bare)
    assert "Seul, 2025" in after                # REAL_PROJECT_DATE: your date, not today's
    assert "job_pilot:begin" in after and after.count("<article>old</article>") == 1
    st, sha_ = c.execute("SELECT status, commit_sha FROM portfolio_entries").fetchone()
    assert st == "published" and len(sha_) == 40
    names = subprocess.run(["git", "--git-dir", str(bare), "show", "--name-only", "--format=", "main"], capture_output=True,
                           text=True).stdout.split()
    assets = [n for n in names if n.startswith("portfolio-assets/")]
    assert sorted(names) == ["index.html", *assets] and len(assets) == 1 and assets[0].endswith(".png")
    assert f'src="{assets[0]}" alt="Banc CAN"' in after     # your image, added with the card (PORTFOLIO_DIFF_SCOPE)
    orch.run("portfolio")                       # end date reached → removed
    assert "job_pilot:begin" not in remote_index(bare)
    gone = subprocess.run(["git", "--git-dir", str(bare), "show", f"main:{assets[0]}"], capture_output=True)
    assert gone.returncode != 0                 # the image leaves with the card
    assert c.execute("SELECT status FROM portfolio_entries").fetchone()[0] == "expired"


def test_approval_for_another_repository_is_refused_and_token_never_in_argv():
    """Case: an approval naming another repository is refused by the handler; the token goes through GIT_ASKPASS
    (environment), never in the command line."""
    tmp = Path(tempfile.mkdtemp())
    orch, bare = portfolio_orch(tmp)
    orch.run("portfolio", params={"agent008": {"brief_id": "b_1"}})
    entry = sqlite3.connect(orch.db_path).execute("SELECT entry_id FROM portfolio_entries").fetchone()[0]
    aid = orch.store.request_approval("publish_portfolio_entry", entry, {"repo": "https://github.com/evil/x.git",
                                      "file": "index.html", "publish_at": "2026-01-01T00:00"})
    try:
        orch.decide(aid, True)
        raise AssertionError("accepted")
    except ValueError:
        pass
    seen = []
    real = subprocess.run

    def spy(args, **kw):
        seen.append((args, kw.get("env", {})))
        return real(args, **kw)
    g = Git("SECRET-TOKEN")
    subprocess.run = spy
    try:
        g.run("--version")
    finally:
        subprocess.run = real
        g.close()
    assert "SECRET-TOKEN" not in " ".join(seen[0][0]) and seen[0][1]["JP_GIT_TOKEN"] == "SECRET-TOKEN"


def test_weights_proposal_moves_by_at_most_the_step_and_sums_to_one():
    """Case: interviews come with high skills scores, rejections with high salary scores → skills up, salary down."""
    w = SCHEMAS["settings"]["defaults"]["scoring"]["weights"]
    pos = [{"skills": 0.9, "level": 1, "title": 1, "location": 1, "salary": 0.5, "sector": 1}] * 6
    neg = [{"skills": 0.3, "level": 1, "title": 1, "location": 1, "salary": 1.0, "sector": 1}] * 6
    new = propose_weights(w, pos, neg, 0.05)
    assert abs(sum(new.values()) - 1) < 1e-9 and new["skills"] > w["skills"] and new["salary"] < w["salary"]
    assert all(abs(new[k] - w[k]) <= 0.06 for k in w)
    assert propose_weights(w, pos, pos, 0.05) is None


def test_metrics_and_no_proposal_below_the_threshold():
    """Case: 2 sent applications, 1 answered after 3 days → answer rate 0.5, delay 3; too few outcomes → no
    weights approval, the reason is written."""
    orch = make_orch()
    now = datetime.now().astimezone()
    with sqlite3.connect(orch.db_path) as c:
        for i in (1, 2):
            c.execute("INSERT INTO offers(offer_id,dedup_key,title,company,description,url,source_id,external_id,status,"
                      "first_seen_run,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,'applied','r','t','t')",
                      (f"o{i}", f"k{i}", "Ingénieur FPGA", "ACME", "d", "https://x", "france_travail_web", f"e{i}"))
            c.execute("INSERT INTO applications VALUES (?,?,'sent','r','x','france_travail_web',NULL,'r','t',?)",
                      (f"a{i}", f"o{i}", (now - timedelta(days=10)).isoformat(timespec="seconds")))
        c.execute("INSERT INTO email_events VALUES ('e1',NULL,'rh@acme.fr','acme.fr','Entretien',?,'a1','domain','interview',"
                  "0.9,0,NULL,'rules','r','t')", ((now - timedelta(days=7)).isoformat(timespec="seconds"),))
    r = orch.run("weekly")
    m = r["results"]["agent011"]
    snap = json.loads(sqlite3.connect(orch.db_path).execute("SELECT metrics FROM metrics_snapshots").fetchone()[0])
    assert snap["applications_sent"] == 2 and snap["answer_rate"] == 0.5 and snap["avg_days_to_first_answer"] == 3
    assert snap["by_family"]["fpga"]["positive"] == 1 and "nécessaires" in snap["weights_note"]
    assert not [a for a in orch.store.approvals() if a["action"] == "apply_scoring_weights"] and m["sent"] == 2


def test_registry_match_confidence():
    """Case COMPANY_MATCH_CONFIDENCE: 'ELSYS Design' ↔ 'ELSYS DESIGN' is certain; a closed company loses 0.2; an
    unrelated name stays a hypothesis."""
    res = [{"nom_complet": "ELSYS DESIGN", "etat_administratif": "A"}, {"nom_complet": "ELSYS", "etat_administratif": "C"}]
    best, conf = best_registry_match("ELSYS Design", res)
    assert best["nom_complet"] == "ELSYS DESIGN" and conf == 1.0
    assert best_registry_match("Thales Alenia Space", [{"nom_complet": "ALENIA SPA", "etat_administratif": "A"}])[1] < 0.6
    assert company_key("Groupe SII SAS") == "sii"


def test_unreachable_registry_is_a_retrieval_error_retried_later():
    """Incident 2026-11-21 (check RETRIEVAL_ERROR_NOT_MISSING): a network error on Serma Ingénierie was written
    'entreprise non trouvée'. Expected: kind retrieval_error, and the sheet is retried at the next run."""
    from conftest import FakeWeb, env_vars, ft_offer
    web = FakeWeb(ft=[ft_offer(1, company="Serma Ingénierie")], fail=("recherche-entreprises",))
    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="secret-long-enough"):
        orch = make_orch(transport=web)
        orch.run("daily")
    c = sqlite3.connect(orch.db_path)
    facts = json.loads(c.execute("SELECT facts FROM companies").fetchone()[0])
    assert any(f["kind"] == "retrieval_error" for f in facts) and not any("non trouvée" in f["value"] for f in facts)
    assert c.execute("SELECT updated_at FROM companies").fetchone()[0].startswith("1970")


def test_the_card_shows_your_real_date_never_today():
    """Rule of the user 2026-11-23 (check REAL_PROJECT_DATE): a project already done but not versioned yet must not show
    today's date: the card shows the year YOU gave, no year at all without it; a future date is refused."""
    from datetime import datetime as _dt
    assert card_year(BRIEF) == 2025 and "Seul, 2025" in card_html(BRIEF["card"], "s", card_year(BRIEF))
    no_date = {k: v for k, v in BRIEF.items() if k != "done_on"}
    assert card_year(no_date) is None and str(_dt.now().year) not in card_html(no_date["card"], "s", None)
    for bad in ("2999-01", "2025-13", "juin 2025", ""):
        try:
            parse_done_on(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert parse_done_on("2024") == "2024" and parse_done_on("2024-02-29") == "2024-02-29"


def test_project_gain_between_optimal_cv_without_and_with_the_project():
    """Request of the user 2026-11-23 (PROJECT_GAIN): for the offer of a proposed project, the coverage and the score of
    the best CV possible today vs the same profile plus the project's skills at the working level."""
    from conftest import FakeWeb, env_vars, ft_offer
    from app.agent.agent004_offer_scorer import gain_lines, project_gain
    desc = "Pipelines Python, SQL et Airflow, avec Kafka et Rust. Débutant accepté."
    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="secret-long-enough"):
        orch = make_orch(transport=FakeWeb(ft=[ft_offer(1, desc=desc)]))
        orch.run("daily")
    c = sqlite3.connect(orch.db_path)
    c.row_factory = sqlite3.Row
    oid = c.execute("SELECT offer_id FROM offers").fetchone()[0]
    g = project_gain(c, oid, ["kafka"], criteria=orch.instance["criteria"], words=orch.schemas["words"],
                     scoring=orch.config["scoring"])
    assert g["coverage_now"] == 60 and g["coverage_with"] == 80 and g["coverage_gain"] == 20, g
    assert g["score_with"] > g["score_now"] and g["still_missing"] == ["rust"]
    assert "**60 %** sans le projet, **80 %** avec lui (**+20 points**)" in gain_lines(g)[0]


def test_a_project_for_an_offer_accepting_either_skill_says_the_choice():
    """Request of the user 2026-11-23 (SKILL_ALTERNATIVES): when the offer says « VHDL et/ou Verilog », the project
    stays proposed but names the choice (doable in VHDL); a title without the choice is sent back to the model."""
    from app.agent import fold
    from app.agent.agent004_offer_scorer import skill_alternatives, skill_label
    lex = {"vhdl": ["vhdl"], "verilog": ["verilog"], "c": ["c"], "python": ["python"]}
    assert skill_alternatives(fold("Maîtrise du VHDL et/ou Verilog, Python"), {"vhdl", "verilog", "python"}, lex) == \
        {"verilog": ["vhdl"], "vhdl": ["verilog"]}
    assert skill_alternatives(fold("VHDL, Verilog et Python"), {"vhdl", "verilog", "python"}, lex) == {}
    assert skill_label("verilog", {"verilog": ["vhdl"]}) == "verilog ou vhdl"
    # real case 2026-11-23 (Astek): « FPGA /VHDL », « FPGA et ou VERILOG »: FPGA is not a language one can pick instead
    groups = [["vhdl", "verilog"]]
    lex2 = {**lex, "fpga": ["fpga"]}
    assert skill_alternatives(fold("FPGA /VHDL ; FPGA et ou VERILOG ; VHDL et ou Verilog"), {"fpga", "vhdl", "verilog"},
                              lex2, groups) == {"verilog": ["vhdl"], "vhdl": ["verilog"]}
    tmp = Path(tempfile.mkdtemp())
    orch = make_orch(tmp)
    with sqlite3.connect(orch.db_path) as c:
        seed_offer(c, orch.schemas["rows"], "o1", 80, ["verilog"])
        c.execute("UPDATE offers SET description='Conception FPGA en VHDL et/ou Verilog.'")
    answers = [dict(BRIEF, title="Filtre FIR en Verilog", objective="Un filtre FIR en Verilog simulé.",
                    portfolio_summary="Filtre FIR simulé.", software=["Icarus Verilog"]),
               dict(BRIEF, title="Filtre FIR en Verilog ou VHDL", objective="Un filtre FIR en Verilog ou VHDL.",
                    portfolio_summary="Filtre FIR simulé.", software=["Icarus Verilog", "GHDL"])]
    from app.agent import agent000_job_pilot as a0
    original = a0.Orchestrator._ctx
    asked = []

    def ctx_with_model(self, *a, **k):
        ctx = original(self, *a, **k)

        def transport(url, headers, body, timeout):
            asked.append(body["messages"])
            return 200, {"choices": [{"message": {"content": json.dumps(answers[min(len(asked), 2) - 1])}}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        ctx.llm_transport = transport
        return ctx
    a0.Orchestrator._ctx = ctx_with_model
    try:
        with env_vars(DEEPSEEK_API_KEY="test-key-long"):
            orch.run(None, only=["agent007"])
    finally:
        a0.Orchestrator._ctx = original
    assert '"accepted_alternatives": {"verilog": ["vhdl"]}' in asked[0][1]["content"]
    assert len(asked) == 2 and "title must name the choice" in asked[1][-1]["content"]
    c = sqlite3.connect(orch.db_path)
    title, brief, path = c.execute("SELECT title, brief, path FROM project_briefs").fetchone()
    assert title == "Filtre FIR en Verilog ou VHDL" and json.loads(brief)["alternatives"] == {"verilog": ["vhdl"]}
    assert "Compétence(s) visée(s) : **verilog ou vhdl** (l'offre accepte l'une ou l'autre" in Path(path).read_text()


def test_reinforce_and_broaden_candidates():
    """Request of the user 2026-11-23 (BRIEF_KINDS): reinforce = a skill you have at the working level asked by many
    favourable offers; broaden = a skill many offers accept instead of one you know; existing briefs are not redone."""
    from app.agent.agent007_gap_project_planner import broaden_candidates, reinforce_candidates
    cfg = {**GAPS, "reinforce_min_offers": 2, "broaden_min_offers": 2}
    row = lambda i, **k: {"offer_id": f"o{i}", "score": 80, "eliminated": 0, "decision": "keep", "scored_at": "1",
                          "missing": [], "matched": [], "text": "", **k}
    rows = [row(1, matched=[{"skill": "altium", "level": "working"}], text="VHDL et/ou Verilog"),
            row(2, matched=[{"skill": "altium", "level": "working"}, {"skill": "vhdl", "level": "core"}], text="VHDL ou Verilog"),
            row(3, matched=[{"skill": "vhdl", "level": "core"}], missing=["verilog"], text="Verilog"),
            row(4, eliminated=1, matched=[{"skill": "python", "level": "working"}] )]
    assert reinforce_candidates(rows, cfg, set()) == [("altium", ["o1", "o2"])]
    assert reinforce_candidates(rows, cfg, {"altium"}) == []
    lex = {"vhdl": ["vhdl"], "verilog": ["verilog"]}
    assert broaden_candidates(rows, cfg, {"vhdl", "altium"}, lex, [["vhdl", "verilog"]], set()) == \
        [("verilog", "vhdl", ["o1", "o2"], ["o3"])]


def test_one_slot_goes_to_a_reinforcement_and_it_lists_the_offers_it_improves():
    """BRIEF_KINDS: with 3 slots, a reinforcement project takes one and the gap projects the others; the
    reinforcement brief lists the offers it improves (the skill raised from working to core)."""
    tmp = Path(tempfile.mkdtemp())
    inst = make_instance(tmp)
    inst["overrides"]["gaps"] = {"reinforce_min_offers": 2, "max_briefs_per_run": 3}
    orch = make_orch(tmp, instance=inst)
    with sqlite3.connect(orch.db_path) as c:
        for i in range(4):
            seed_offer(c, orch.schemas["rows"], f"o{i}", 80 - i, ["kafka"])
        c.execute("UPDATE scores SET matched=?", (json.dumps([{"skill": "postgresql", "level": "working"}]),))
        c.execute("UPDATE offers SET description='Pipelines avec PostgreSQL et Kafka.'")
    orch.run(None, only=["agent001"])
    from app.agent import agent000_job_pilot as a0
    original = a0.Orchestrator._ctx
    kinds = []

    def ctx_with_model(self, *a, **k):
        ctx = original(self, *a, **k)

        def transport(url, headers, body, timeout):
            payload = json.loads(body["messages"][1]["content"])
            kinds.append(payload["project_kind"])
            sk = payload["target_skills"][0]
            ans = dict(BRIEF, title=f"Projet {sk}", objective=f"Un projet {sk}.", portfolio_summary="Un banc.",
                       software=[sk], builds_on=["python", "sql"])
            return 200, {"choices": [{"message": {"content": json.dumps(ans)}}], "usage": {"prompt_tokens": 9, "completion_tokens": 9}}
        ctx.llm_transport = transport
        return ctx
    a0.Orchestrator._ctx = ctx_with_model
    try:
        with env_vars(DEEPSEEK_API_KEY="test-key-long"):
            r = orch.run(None, only=["agent007"])
    finally:
        a0.Orchestrator._ctx = original
    assert kinds == ["reinforce", "gap", "gap"], (kinds, r["results"]["agent007"])
    c = sqlite3.connect(orch.db_path)
    b, path = c.execute("SELECT brief, path FROM project_briefs WHERE json_extract(brief, '$.kind')='reinforce'").fetchone()
    b = json.loads(b)
    assert b["coverage"]["offers"] == 4 and b["coverage"]["avg_score_gain"] > 0
    text = Path(path).read_text()
    assert "Type : **renforcement**" in text and "améliore **4 offre(s) favorable(s)**" in text


def test_hardware_items_are_detailed_and_older_briefs_get_their_details():
    """Request of the user 2026-11-23 (HARDWARE_DETAIL): every hardware item has its role, what to check, an example and
    an alternative (never a price); an older brief with plain names is detailed by the model, names kept as they are."""
    from app.agent.agent007_gap_project_planner import detail_hardware, hw_item, hw_line
    assert hw_item("Oscilloscope") == {"name": "Oscilloscope", "role": "", "specs": "", "example": "", "alternative": "",
                                       "optional": False}
    line = hw_line({"name": "DE0-Nano", "role": "cible FPGA", "specs": "Cyclone IV, 3,3 V", "example": "Terasic DE0-Nano",
                    "alternative": "simulation GHDL", "optional": True})
    assert line == ("- **DE0-Nano** (facultatif) : cible FPGA ; à vérifier : Cyclone IV, 3,3 V ; exemple : Terasic DE0-Nano ; "
                    "alternative : simulation GHDL")
    tmp = Path(tempfile.mkdtemp())
    orch = make_orch(tmp)
    ctx = orch._ctx("r_hw", "t", {})
    sent = []

    class Client:
        def chat_json(self, messages, **k):
            sent.append(messages)
            n = len(json.loads(messages[1]["content"])["hardware"])
            items = [{"role": "r", "specs": "s", "example": "e", "alternative": "a"}] * (n if len(sent) > 1 else n - 1)
            return {"items": items}
    got = detail_hardware(Client(), ctx, {"title": "t", "architecture": "a", "hardware": ["Carte A", "Câble B"]})
    assert len(sent) == 2 and [g["name"] for g in got] == ["Carte A", "Câble B"] and got[0]["specs"] == "s"


if __name__ == "__main__":
    run_main(globals())
