"""Web pages and actions: rendering, CSP without inline code, CSRF on every POST, manual offer, your decision."""
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import ROOT, FakeWeb, env_vars, ft_offer, make_orch, run_main  # noqa: E402

from app.web.app import create_app  # noqa: E402

TEMPLATES = ROOT / "app/web/templates"


def client():
    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="s"):
        orch = make_orch(transport=FakeWeb(ft=[ft_offer(1)]))
        orch.run("daily")
    app = create_app(orch, start=False)
    app.config["TESTING"] = True
    return orch, app.test_client()


def token(cl):
    html = cl.get("/").data.decode()
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_pages_render_in_both_languages_with_csp():
    """Case: home, offer page and supervision answer 200 in EN and FR; CSP with a nonce, no inline style."""
    orch, cl = client()
    oid = sqlite3.connect(orch.db_path).execute("SELECT offer_id FROM offers").fetchone()[0]
    for lang in ("en", "fr"):
        cl.set_cookie("lang", lang)
        for url in ("/", f"/offers/{oid}", "/standup"):
            r = cl.get(url)
            assert r.status_code == 200, (url, r.status_code)
            assert "script-src 'self' 'nonce-" in r.headers["Content-Security-Policy"]
    cl.set_cookie("lang", "fr")
    assert "Sources interrogées" in cl.get("/standup").data.decode()
    assert "Meilleures offres du jour" in cl.get("/").data.decode()
    assert cl.get("/offers/o_missing").status_code == 404


def test_templates_have_no_inline_style_or_handler():
    """Case: kit rule — no style attribute, no on*= attribute, scripts only with the nonce."""
    for f in TEMPLATES.glob("*.html"):
        t = f.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle=", t), f.name
        assert not re.search(r"\son[a-z]+=", t), f.name
        assert all("nonce" in s for s in re.findall(r"<script[^>]*>", t)), f.name


def test_every_post_needs_the_csrf_token():
    """Case: run launch, manual offer, decision, approvals, incidents without token → 400."""
    orch, cl = client()
    for url in ("/runs", "/inbox", "/offers/x/decision", "/offers/x/full_text", "/approvals/1", "/skills/propose", "/incidents/1/close"):
        assert cl.post(url, data={}).status_code == 400, url


def test_manual_offer_is_validated_then_stored_in_the_inbox():
    """Case: a javascript: link or a 10-character description is refused; a real paste is stored for agent002."""
    orch, cl = client()
    t = token(cl)
    bad = {"csrf": t, "url": "javascript:alert(1)", "title": "Data Engineer", "company": "X", "description": "x" * 50}
    cl.post("/inbox", data=bad)
    assert sqlite3.connect(orch.db_path).execute("SELECT COUNT(*) FROM offer_inbox").fetchone()[0] == 0
    good = dict(bad, url="https://www.linkedin.com/jobs/view/1", description="Data engineer junior, Python SQL, CDI à Paris. " * 2)
    cl.post("/inbox", data=good)
    rows = sqlite3.connect(orch.db_path).execute("SELECT url, origin_label FROM offer_inbox").fetchall()
    assert rows == [("https://www.linkedin.com/jobs/view/1", "www.linkedin.com")]


def test_your_decision_is_recorded_as_override():
    """Case: 'Reject' on the offer page sets user_override, keeps the agent's decision and logs the event."""
    orch, cl = client()
    oid = sqlite3.connect(orch.db_path).execute("SELECT offer_id FROM offers").fetchone()[0]
    cl.post(f"/offers/{oid}/decision", data={"csrf": token(cl), "choice": "reject"})
    c = sqlite3.connect(orch.db_path)
    assert c.execute("SELECT decision, user_override FROM experience_decisions WHERE offer_id=?", (oid,)).fetchone() == ("keep", "reject")
    assert c.execute("SELECT COUNT(*) FROM events WHERE code='user_decision'").fetchone()[0] == 1
    assert cl.post(f"/offers/{oid}/decision", data={"csrf": token(cl), "choice": "send"}).status_code == 400


def test_every_reason_and_elimination_has_a_french_label():
    """Case: 2026-11-21 screenshot review — reason texts stayed in English on the French interface."""
    from conftest import SCHEMAS
    w = SCHEMAS["words"]
    texts = list(w["experience_reasons"].values()) + list(w["elimination_reasons"].values())
    assert [t for t in texts if t not in w["ui_text"]["fr"]] == []


def test_alerts_projects_and_publication_request():
    """Case: an alert is shown then marked seen; a project idea with a demo can be asked for publication, which only
    creates an approval (nothing is published); the demo route serves only files of its own folder."""
    orch, cl = client()
    c = sqlite3.connect(orch.db_path)
    demo = orch.runs_dir / "r9" / "portfolio_demo"
    demo.mkdir(parents=True)
    (demo / "index.html").write_text("<html>demo</html>")
    with c:
        c.execute("INSERT INTO alerts(kind,category,message,created_at) VALUES ('immediate','interview','Entretien ACME','t')")
        c.execute("INSERT INTO project_briefs (brief_id, skill, title, brief, path, status, run_id, created_at) "
                  "VALUES ('b_1','kafka','Projet Kafka','{}','x','proposed','r9','2026-11-21')")
        c.execute("INSERT INTO portfolio_entries(entry_id,brief_id,slug,demo_path,status,run_id,updated_at) VALUES "
                  "('p_1','b_1','projet-kafka',?,'demo','r9','t')", (str(demo / "index.html"),))
    html = cl.get("/").data.decode()
    assert "Entretien ACME" in html and "Projet Kafka" in html
    tok = token(cl)
    cl.post("/alerts/1/seen", data={"csrf": tok})
    assert c.execute("SELECT seen_at IS NOT NULL FROM alerts").fetchone()[0] == 1
    assert cl.get("/portfolio/p_1/index.html").data == b"<html>demo</html>"
    assert cl.get("/portfolio/p_1/..%2F..%2F..%2Fjob_pilot.json").status_code == 404
    cl.post("/entries/p_1/publish", data={"csrf": tok, "publish_at": "2026-12-01T09:00", "expire_at": "2026-11-01T09:00"})
    assert not orch.store.approvals()[-1]["action"] == "publish_portfolio_entry"
    cl.post("/entries/p_1/publish", data={"csrf": tok, "publish_at": "2026-12-01T09:00"})
    appr = [a for a in orch.store.approvals() if a["action"] == "publish_portfolio_entry"]
    assert len(appr) == 1 and appr[0]["parameters"]["publish_at"] == "2026-12-01T09:00"
    assert c.execute("SELECT status FROM portfolio_entries").fetchone()[0] == "demo"


def test_documents_are_rendered_in_the_app_look_and_escaped():
    """User request 2026-11-21 ('la page n'a pas de charte graphique dans les onglets'): a package opens in the
    job_pilot page (panels, title, icon), not as raw text; offer text stays text (no <script>), links are clickable."""
    from app.web.page import md_sections
    title, secs = md_sections("# Poste FPGA · ACME\n\n- Offre : https://ex.org/a?b=1\n\n## Points de vigilance\n\n"
                              "- <script>alert(1)</script> **important**\n")
    assert title == "Poste FPGA · ACME" and secs[1]["title"] == "Points de vigilance"
    h = str(secs[1]["html"])
    assert "<script>" not in h and "&lt;script&gt;" in h and "<strong>important</strong>" in h
    assert '<a href="https://ex.org/a?b=1" target="_blank" rel="noopener noreferrer">' in str(secs[0]["html"])
    orch, cl = client()
    c = sqlite3.connect(orch.db_path)
    oid = c.execute("SELECT offer_id FROM offers").fetchone()[0]
    pkg = orch.runs_dir / "r1" / f"application_{oid}"
    pkg.mkdir(parents=True)
    (pkg / "package.md").write_text("# Poste · ACME\n\n## Fiche entreprise\n\n- Effectif : 100\n")
    with c:
        c.execute("INSERT INTO applications VALUES ('a_1', ?, 'ready', 'r1', 'x', 'france_travail', ?, 'r1', 'now', NULL)",
                  (oid, f"r1/application_{oid}"))
    page = cl.get("/docs/package/a_1").data.decode()
    assert 'class="panel doc"' in page and "<title>Poste · ACME · Job Pilot</title>" in page and 'rel="icon"' in page
    assert cl.get("/docs/package/a_2").status_code == 404 and cl.get("/docs/other/a_1").status_code == 404
    for f in TEMPLATES.glob("*.html"):
        visible = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
        assert "—" not in visible, f.name


def test_a_promising_partial_offer_comes_to_you_and_your_full_text_is_judged():
    """Rule of the user 2026-11-23 (ASK_FULL_TEXT): a promising offer known by an excerpt only raises one alert
    linking to its page; the pasted full text replaces the excerpt, is traced, and the offer is judged again (your own
    decision kept); a too short paste is refused."""
    import time
    orch, cl = client()
    c = sqlite3.connect(orch.db_path)
    oid, score = c.execute("SELECT offer_id, score FROM scores ORDER BY score DESC").fetchone()
    with c:
        c.execute("UPDATE offers SET description_complete=0 WHERE offer_id=?", (oid,))
        c.execute("DELETE FROM scores WHERE offer_id=?", (oid,))
    orch.run("rescore")
    orch.run("rescore")                                     # once only
    assert score >= 55 and c.execute("SELECT COUNT(*) FROM alerts WHERE category='full_text' AND ref=?",
                                     (oid,)).fetchone()[0] == 1
    assert f"/offers/{oid}#full-text" in cl.get("/").data.decode()
    assert 'id="full-text"' in cl.get(f"/offers/{oid}").data.decode()
    tok = token(cl)
    assert cl.post(f"/offers/{oid}/full_text", data={"csrf": tok, "text": "trop court"}).status_code == 400
    text = "Ingénieur systèmes embarqués junior. " * 10 + "Débutant accepté. Python, SQL."
    assert cl.post(f"/offers/{oid}/full_text", data={"csrf": tok, "text": text}).status_code == 302
    for _ in range(100):                                    # the rescore runs in the background
        if not orch.busy() and c.execute("SELECT 1 FROM scores WHERE offer_id=?", (oid,)).fetchone():
            break
        time.sleep(0.1)
    desc, complete = c.execute("SELECT description, description_complete FROM offers WHERE offer_id=?", (oid,)).fetchone()
    assert desc == text.strip() and complete == 1
    assert c.execute("SELECT 1 FROM experience_decisions WHERE offer_id=?", (oid,)).fetchone()
    assert c.execute("SELECT COUNT(*) FROM events WHERE code='user_full_text'").fetchone()[0] == 1
    assert 'id="full-text"' not in cl.get(f"/offers/{oid}").data.decode()


def test_the_interface_is_really_french():
    """Remark of the user 2026-11-23 ("la partie du site FR n'est pas vraiment FR"): French by default; skills, check
    names, sources, agents, incident categories and approval actions have French texts; generated English sentences of
    older runs are translated on display."""
    import importlib
    import json as _json
    import re as _re
    from app.agent.agent000_job_pilot import ALLOWED_MODULES
    words = _json.loads((ROOT / "app/schemas/words.json").read_text())
    fr = words["ui_text"]["fr"]
    english = _re.compile(r"\b(the|and|of|with|every|only|your|is|are)\b")
    for mod in ALLOWED_MODULES.values():
        m = importlib.import_module(mod)
        for k in m.SKILLS:
            assert not english.search(k["objective"]) and not english.search(_re.sub(r"[A-Z_]{4,}|\S+\.\w+", "", k["summary"])), k
        for check in getattr(m, "CHECKS", []):
            assert not english.search(check[1]), check
    settings = _json.loads((ROOT / "app/schemas/settings.json").read_text())
    for key in list(settings["defaults"]["sources"]) + list(ALLOWED_MODULES) + ["agent000", "timeout", "software_failure",
                                                                              "activate_skill", "publish_portfolio_entry"]:
        assert key in fr, key
    orch, cl = client()
    home = cl.get("/").data.decode()
    assert 'lang="fr"' in home or "Accueil" in home
    with sqlite3.connect(orch.db_path) as c:
        c.execute("UPDATE sup_runs SET title='daily (scheduler)'")
    page = cl.get("/standup").data.decode()
    assert "quotidienne (planificateur)" in page and "(scheduler)" not in page


def test_you_can_edit_a_project_idea():
    """Request of the user 2026-11-23 ("pour le projet j'aimerais pouvoir l'éditer"): the brief page has an edit form;
    the public summary keeps its rule (BRIEF_PUBLIC_SUMMARY); a valid edit rewrites the file and the row, traced."""
    import json as _json
    orch, cl = client()
    folder = orch.runs_dir / "r_test"
    folder.mkdir(parents=True)
    path = folder / "brief_x.md"
    path.write_text("# Ancien titre\n")
    brief = {"title": "Ancien titre", "objective": "Un filtre FIR.", "portfolio_summary": "Filtre FIR simulé.",
             "builds_on": ["vhdl"], "architecture": "a", "hardware": ["PC"], "software": ["GHDL"],
             "steps": [{"days": "J1", "milestone": "m", "deliverable": "d"}], "success_criteria": ["c"],
             "pitfalls": ["p"], "interview_pitch": "i"}
    with sqlite3.connect(orch.db_path) as c:
        c.execute("INSERT INTO project_briefs (brief_id, skill, title, brief, path, status, run_id, created_at) "
                  "VALUES ('b_e','verilog','Ancien titre',?,?,'proposed','r_test','2026-11-23')", (_json.dumps(brief), str(path)))
    assert "/docs/brief/b_e/edit" in cl.get("/docs/brief/b_e").data.decode()
    page = cl.get("/docs/brief/b_e/edit").data.decode()
    assert 'name="steps"' in page and "J1 | m | d" in page
    tok = token(cl)
    form = {"csrf": tok, "title": "Filtre FIR en Verilog sur FPGA", "objective": "Mon objectif à moi.",
            "builds_on": "vhdl, python", "architecture": "Chaîne ADC puis FIR", "hardware": "DE0-Nano\nCâbles",
            "software": "Icarus Verilog", "steps": "J1-J2 | Banc de test | tb.v\nJ3 | Synthèse", "success_criteria": "0 erreur",
            "pitfalls": "timing", "interview_pitch": "Je l'ai conçu.", "portfolio_summary": "Pour apprendre Verilog."}
    r = cl.post("/docs/brief/b_e/edit", data=form)
    assert r.status_code == 302 and "error=" in r.headers["Location"]          # 'apprendre' refused on the public card
    form["portfolio_summary"] = "Filtre FIR en Verilog sur une carte DE0-Nano."
    assert cl.post("/docs/brief/b_e/edit", data=form).status_code == 302
    c = sqlite3.connect(orch.db_path)
    title, saved = c.execute("SELECT title, brief FROM project_briefs WHERE brief_id='b_e'").fetchone()
    saved = _json.loads(saved)
    assert title == form["title"] and saved["hardware"] == ["DE0-Nano", "Câbles"] and saved["edited_at"]
    assert saved["steps"][1] == {"days": "J3", "milestone": "Synthèse", "deliverable": ""}
    text = path.read_text()
    assert text.startswith("# Filtre FIR en Verilog sur FPGA") and "Modifié par toi le" in text
    assert c.execute("SELECT COUNT(*) FROM events WHERE code='user_brief_edit'").fetchone()[0] == 1
    assert cl.post("/docs/brief/b_e/edit", data={**form, "csrf": "bad"}).status_code == 400


if __name__ == "__main__":
    run_main(globals())
