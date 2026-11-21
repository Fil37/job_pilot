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
from app.agent.agent007_gap_project_planner import window_gaps  # noqa: E402
from app.agent.agent008_portfolio_publisher import Git, card_html, insert_card, remove_card  # noqa: E402
from app.agent.agent011_pipeline_analyst import propose_weights  # noqa: E402

GAPS = {"window_days": 30, "min_score": 55, "min_offers": 3, "max_missing": 2}
BRIEF = {"title": "Bus CAN sur STM32 avec <script>", "objective": "Apprendre Kafka & co.",
         "portfolio_summary": "Chaîne de messages Kafka & co. entre un PC et une carte.", "builds_on": ["python", "sql"],
         "architecture": "Producteur Python → Kafka → consommateur", "hardware": ["PC"], "software": ["Kafka"],
         "steps": [{"days": f"J{i}", "milestone": f"étape {i}", "deliverable": "d"} for i in range(1, 6)],
         "success_criteria": ["1000 messages/s"], "pitfalls": ["partitions"], "interview_pitch": "Projet d'apprentissage."}


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


def test_card_is_escaped_and_insert_remove_touch_only_the_block():
    """Case: model text is HTML-escaped in the card; inserting twice replaces; removing gives back the page."""
    page = '<main><div class="proj-grid">\n  <article>old</article>\n</div></main>\n'
    card = card_html(BRIEF, "kafka", 2026, "bus-can")
    assert "<script>" not in card and "&lt;script&gt;" in card and "Kafka &amp; co." in card
    assert "Apprendre" not in card            # BRIEF_PUBLIC_SUMMARY: the public card uses portfolio_summary
    once = insert_card(page, "bus-can", card, '<div class="proj-grid">')
    assert insert_card(once, "bus-can", card, '<div class="proj-grid">') == once
    assert once.index("job_pilot:begin bus-can") < once.index("<article>old")
    assert remove_card(once, "bus-can").replace("\n\n", "\n") == page.replace("\n\n", "\n")


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
    with sqlite3.connect(orch.db_path) as c:
        c.execute("INSERT INTO project_briefs VALUES ('b_1','kafka',?,?,'x','proposed','r0','now')",
                  (BRIEF["title"], json.dumps(BRIEF)))
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
    assert "job_pilot:begin" in after and after.count("<article>old</article>") == 1
    st, sha_ = c.execute("SELECT status, commit_sha FROM portfolio_entries").fetchone()
    assert st == "published" and len(sha_) == 40
    names = subprocess.run(["git", "--git-dir", str(bare), "show", "--name-only", "--format=", "main"], capture_output=True,
                           text=True).stdout.split()
    assert names == ["index.html"]
    orch.run("portfolio")                       # end date reached → removed
    assert "job_pilot:begin" not in remote_index(bare)
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
    assert snap["by_family"]["fpga"]["positive"] == 1 and "needed" in snap["weights_note"]
    assert not [a for a in orch.store.approvals() if a["action"] == "apply_scoring_weights"] and m["sent"] == 2


def test_registry_match_confidence():
    """Case COMPANY_MATCH_CONFIDENCE: 'ELSYS Design' ↔ 'ELSYS DESIGN' is certain; a closed company loses 0.2; an
    unrelated name stays a hypothesis."""
    res = [{"nom_complet": "ELSYS DESIGN", "etat_administratif": "A"}, {"nom_complet": "ELSYS", "etat_administratif": "C"}]
    best, conf = best_registry_match("ELSYS Design", res)
    assert best["nom_complet"] == "ELSYS DESIGN" and conf == 1.0
    assert best_registry_match("Thales Alenia Space", [{"nom_complet": "ALENIA SPA", "etat_administratif": "A"}])[1] < 0.6
    assert company_key("Groupe SII SAS") == "sii"


if __name__ == "__main__":
    run_main(globals())
