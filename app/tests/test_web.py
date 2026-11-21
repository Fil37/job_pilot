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
    for url in ("/runs", "/inbox", "/offers/x/decision", "/approvals/1", "/skills/propose", "/incidents/1/close"):
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
        c.execute("INSERT INTO project_briefs VALUES ('b_1','kafka','Projet Kafka','{}','x','proposed','r9','2026-11-21')")
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


if __name__ == "__main__":
    run_main(globals())
