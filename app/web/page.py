"""job_pilot — pages and web actions (Flask), on the Studio Kit shell.

Language and theme routes, translation, common variables and the CSP header with a nonce come from
studio-kit/demo/app.py. Long operations (runs) go to a background thread through agent000: a page never waits.
Every POST checks a CSRF token. Nothing here sends anything outside.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import threading
from pathlib import Path

from markupsafe import Markup
from urllib.parse import urlsplit

from flask import abort, g, redirect, render_template, request, send_file, session, url_for

from app.agent import db_connect, utc_now
from app.agent.agent000_job_pilot import chain_label
from app.agent import supervision as sv
from app.agent.agent000_job_pilot import Orchestrator, application_params, build_digest

WEB_CHAINS = ("daily", "rescore", "inbox", "weekly", "portfolio")
LINK_RX = re.compile(r"(https?://[^\s<>()]+[^\s<>().,;:!?»])")


def inline_md(text: str) -> str:
    """Escape FIRST (contents of offers, mails and model answers are data), then **bold** and links."""
    out = html.escape(text, quote=True)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return LINK_RX.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', out)


def md_sections(text: str) -> tuple[str, list[dict]]:
    """A job_pilot Markdown file → (title, [{"title", "html"}]): '## ' opens a panel, '- ' lists, blank lines split
    paragraphs. Only the few forms job_pilot writes; everything else is plain escaped text."""
    title, sections, cur, buf, items = "", [], {"title": "", "lines": []}, [], []

    def flush_par():
        if buf:
            cur["lines"].append("<p>" + " ".join(inline_md(x) for x in buf) + "</p>")
            buf.clear()

    def flush_list():
        if items:
            cur["lines"].append('<ul class="doc-list">' + "".join(f"<li>{inline_md(x)}</li>" for x in items) + "</ul>")
            items.clear()
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            flush_par()
            flush_list()
            if cur["lines"] or cur["title"]:
                sections.append(cur)
            cur = {"title": line[3:].strip(), "lines": []}
        elif line.startswith("- "):
            flush_par()
            items.append(line[2:])
        elif not line.strip():
            flush_par()
            flush_list()
        else:
            flush_list()
            buf.append(line.strip())
    flush_par()
    flush_list()
    if cur["lines"] or cur["title"]:
        sections.append(cur)
    return title, [{"title": c["title"], "html": Markup("".join(c["lines"]))} for c in sections]
_run_lock = threading.Lock()


def register(app, orch: Orchestrator) -> None:
    words = orch.schemas["words"]
    fr = {**words["ui_text"]["fr"], **words["skill_text"]["fr"]}
    # Sentences generated with figures inside (run summaries, run titles of older runs, socle tiles): translated by
    # the patterns of words.json, applied only when no exact translation exists.
    patterns = [(re.compile(a), b) for a, b in words.get("ui_patterns", {}).get("fr", [])]

    def translate(text) -> str:
        if g.get("lang") != "fr" or not isinstance(text, str) or not text:
            return text
        if text in fr:
            return fr[text]
        for rx, rep in patterns:
            text = rx.sub(rep, text)
        return text

    app.jinja_env.filters["fromjson"] = lambda v: json.loads(v) if v else {}

    def check_csrf():
        if not request.form.get("csrf") or request.form.get("csrf") != session.get("csrf"):
            abort(400)

    @app.before_request
    def prepare():
        g.lang = request.cookies.get("lang") if request.cookies.get("lang") in ("en", "fr") else "fr"   # French by default
        g.csp_nonce = secrets.token_urlsafe(16)
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)

    @app.context_processor
    def inject():
        theme = request.cookies.get("theme")
        return {"labels": {"app_title": "Job Pilot", "version": "v0.4", "css_file": "job_pilot_master.css"},
                "lang": g.lang, "theme": theme if theme in ("light", "dark") else "auto", "_": translate,
                "csrf": session.get("csrf"), "csp_nonce": g.csp_nonce, "error": None,
                "reasons": words["experience_reasons"], "elim": words["elimination_reasons"]}

    @app.after_request
    def security(resp):
        if g.get("demo_page"):
            resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: https:; "
                                                       "style-src 'self' 'unsafe-inline' https:; font-src 'self' data: https:; "
                                                       "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                                   f"script-src 'self' 'nonce-{g.get('csp_nonce', '')}'; "
                                                   "form-action 'self'; frame-ancestors 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def back(default: str):
        nxt = request.args.get("next") or default
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default)

    @app.get("/lang/<code>")
    def set_language(code: str):
        resp = back(url_for("home"))
        if code in ("en", "fr"):
            resp.set_cookie("lang", code, max_age=365 * 86400, samesite="Lax")
        return resp

    @app.get("/theme/<code>")
    def set_theme(code: str):
        resp = back(url_for("home"))
        if code in ("light", "dark"):
            resp.set_cookie("theme", code, max_age=365 * 86400, samesite="Lax")
        else:
            resp.delete_cookie("theme")
        return resp

    # ------------------------------------------------------------------ home
    @app.get("/")
    def home():
        digest = build_digest(orch.db_path, orch.config["digest"], orch.config["scoring"]["weights_version"])
        with db_connect(orch.db_path) as c:
            n = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
            stats = [{"value": n("SELECT COUNT(*) FROM offers"), "label": "offers seen"},
                     {"value": len(digest["top"]), "label": "kept"},
                     {"value": len(digest["review"]), "label": "to review"},
                     {"value": n("SELECT COUNT(*) FROM applications"), "label": "applications"}]
            last = c.execute("SELECT * FROM sup_runs ORDER BY created_at DESC LIMIT 1").fetchone()
            last = dict(last) if last else None
            prof = c.execute("SELECT summary FROM profile_versions ORDER BY created_at DESC LIMIT 1").fetchone()
            alerts = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE seen_at IS NULL ORDER BY alert_id DESC LIMIT 30")]
            apps = [dict(r) for r in c.execute(
                "SELECT a.*, o.title, o.company, o.url, (SELECT category FROM email_events e WHERE e.application_id="
                "a.application_id ORDER BY received_at DESC LIMIT 1) AS last_mail FROM applications a JOIN offers o "
                "USING(offer_id) ORDER BY a.created_at DESC LIMIT 50")]
            briefs = [dict(r) for r in c.execute(
                "SELECT b.brief_id, b.skill, b.title, b.status, b.created_at, json_extract(b.brief, '$.done_on') AS done_on, "
                "p.entry_id, p.status AS entry_status, "
                "p.publish_at, p.expire_at, p.error FROM project_briefs b LEFT JOIN portfolio_entries p USING(brief_id) "
                "ORDER BY b.created_at DESC LIMIT 10")]
            gaps = [dict(r) for r in c.execute("SELECT skill, offers_count, covered, triggered FROM skill_gaps WHERE window_end="
                                               "(SELECT MAX(window_end) FROM skill_gaps) ORDER BY offers_count DESC LIMIT 8")]
        example = bool(prof and json.loads(prof[0]).get("example"))
        return render_template("job_pilot.html", stats=stats, digest=digest, last=last, example=example, alerts=alerts,
                               apps=apps, briefs=briefs, gaps=gaps, busy=bool(orch.busy()), msg=request.args.get("msg"))

    @app.post("/alerts/<int:alert_id>/seen")
    def alert_seen(alert_id: int):
        check_csrf()
        with db_connect(orch.db_path) as c:
            c.execute("UPDATE alerts SET seen_at=? WHERE alert_id=? AND seen_at IS NULL", (utc_now(), alert_id))
        return redirect(url_for("home") + "#alerts")

    # ------------------------------------------------------------------ project ideas (agent007) and portfolio (agent008)
    @app.get("/briefs/<brief_id>")
    def brief_file(brief_id: str):
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT path FROM project_briefs WHERE brief_id=?", (brief_id,)).fetchone()
        path = Path(row[0]).resolve() if row else None
        if not path or orch.runs_dir.resolve() not in path.parents or not path.is_file():
            abort(404)
        return send_file(path, mimetype="text/plain; charset=utf-8")

    @app.post("/briefs/<brief_id>/demo")
    def brief_demo(brief_id: str):
        check_csrf()
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT json_extract(brief, '$.card.title') FROM project_briefs WHERE brief_id=?", (brief_id,)).fetchone()
        if not row:
            abort(404)
        if not row[0]:
            return redirect(url_for("brief_done", brief_id=brief_id,
                                    error="Fill in the 'Project done' sheet first: the card is made from it."))
        if orch.busy() or not _run_lock.acquire(blocking=False):
            return redirect(url_for("home", msg="A run is already in progress."))

        def work():
            try:
                orch.run("portfolio", params={"agent008": {"brief_id": brief_id}}, title=f"démo portfolio {brief_id} (interface)")
            finally:
                _run_lock.release()
        threading.Thread(target=work, daemon=True, name=f"demo-{brief_id}").start()
        return redirect(url_for("home", msg="Run started.") + "#projects")

    @app.get("/portfolio/<entry_id>/<path:name>")
    def portfolio_demo(entry_id: str, name: str):
        """Local preview of your portfolio with the card (files of the demo folder only)."""
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT demo_path FROM portfolio_entries WHERE entry_id=?", (entry_id,)).fetchone()
        if not row:
            abort(404)
        base = Path(row[0]).resolve().parent
        path = (base / name).resolve()
        if orch.runs_dir.resolve() not in base.parents or (path != base and base not in path.parents) or not path.is_file():
            abort(404)
        resp = send_file(path)
        g.demo_page = True                     # your own site: its inline styles / scripts are allowed on this route
        return resp

    @app.post("/entries/<entry_id>/publish")
    def entry_publish(entry_id: str):
        check_csrf()
        from datetime import datetime as _dt
        try:
            pub = _dt.fromisoformat(request.form.get("publish_at", ""))
            exp = _dt.fromisoformat(request.form["expire_at"]) if request.form.get("expire_at") else None
        except ValueError:
            return redirect(url_for("home", msg="Invalid date.") + "#projects")
        if exp and exp <= pub:
            return redirect(url_for("home", msg="Invalid date.") + "#projects")
        from app.agent.agent008_portfolio_publisher import parse_done_on
        try:
            done_on = parse_done_on(request.form.get("done_on"))
        except ValueError:
            return redirect(url_for("home", msg="Give the real date of the project (not in the future).") + "#projects")
        cfg = orch.config["portfolio"]
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT p.slug, b.brief_id, b.brief FROM portfolio_entries p JOIN project_briefs b "
                            "USING(brief_id) WHERE p.entry_id=?", (entry_id,)).fetchone()
            if not row:
                abort(404)
            brief = {**json.loads(row[2]), "done_on": done_on}
            c.execute("UPDATE project_briefs SET brief=? WHERE brief_id=?", (json.dumps(brief, ensure_ascii=False), row[1]))
        orch.store.request_approval("publish_portfolio_entry", entry_id, {
            "repo": cfg["repo"], "file": cfg["file"], "slug": row[0], "publish_at": pub.isoformat(timespec="minutes"),
            "expire_at": exp.isoformat(timespec="minutes") if exp else None, "done_on": done_on})
        return redirect(url_for("home", msg="Publication requested: approve it on the Supervision page.") + "#projects")

    @app.post("/runs")
    def start_run():
        check_csrf()
        chain = request.form.get("chain", "daily")
        if chain not in WEB_CHAINS:
            abort(400)
        if orch.busy() or not _run_lock.acquire(blocking=False):
            return redirect(url_for("home", msg="A run is already in progress."))

        def work():
            try:
                orch.run(chain, title=f"{chain_label(chain)} (interface)")
            finally:
                _run_lock.release()

        threading.Thread(target=work, daemon=True, name=f"run-{chain}").start()
        return redirect(url_for("home", msg={"inbox": "Inbox check started.", "weekly": "Weekly analysis started."}
                                .get(chain, "Run started.")))

    @app.post("/inbox")
    def add_offer():
        check_csrf()
        f = {k: (request.form.get(k) or "").strip() for k in
             ("url", "title", "company", "location", "contract", "salary_text", "description")}
        u = urlsplit(f["url"])
        if (u.scheme not in ("http", "https") or not u.netloc or not f["title"] or not f["company"]
                or len(f["description"]) < 40 or any(len(v) > 20000 for v in f.values())):
            return redirect(url_for("home", msg="Invalid form."))
        with db_connect(orch.db_path) as c:
            c.execute("INSERT INTO offer_inbox(url, title, company, location, contract, salary_text, description, "
                      "origin_label, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (f["url"], f["title"][:300], f["company"][:200], f["location"][:200] or None,
                       f["contract"][:50] or None, f["salary_text"][:200] or None, f["description"], u.netloc, utc_now()))
        return redirect(url_for("home", msg="Offer added to the inbox."))

    # ------------------------------------------------------------------ offer detail
    @app.get("/offers/<offer_id>")
    def offer(offer_id: str):
        rows = orch.schemas["rows"]
        from app.agent import decode_row
        with db_connect(orch.db_path) as c:
            o = c.execute("SELECT * FROM offers WHERE offer_id=?", (offer_id,)).fetchone()
            if not o:
                abort(404)
            d = c.execute("SELECT * FROM experience_decisions WHERE offer_id=? ORDER BY decided_at DESC LIMIT 1",
                          (offer_id,)).fetchone()
            s = c.execute("SELECT * FROM scores WHERE offer_id=? ORDER BY scored_at DESC LIMIT 1", (offer_id,)).fetchone()
            aliases = [dict(r) for r in c.execute("SELECT * FROM offer_aliases WHERE offer_id=?", (offer_id,))]
            run = c.execute("SELECT run_id, title, status, created_at FROM sup_runs WHERE run_id=?",
                            (o["first_seen_run"],)).fetchone()
            apps = [dict(r) for r in c.execute("SELECT * FROM applications WHERE offer_id=? ORDER BY created_at DESC",
                                               (offer_id,))]
        from app.agent.agent005_company_researcher import company_key
        with db_connect(orch.db_path) as c:
            comp = c.execute("SELECT facts, match_confidence, updated_at FROM companies WHERE company_key=?",
                             (company_key(o["company"]),)).fetchone()
            ids = [a["application_id"] for a in apps] or [""]
            q = ",".join("?" * len(ids))
            mails = [dict(r) for r in c.execute(f"SELECT * FROM email_events WHERE application_id IN ({q}) ORDER BY received_at DESC", ids)]
            sheets = [dict(r) for r in c.execute(f"SELECT * FROM interview_sheets WHERE application_id IN ({q})", ids)]
            drafts = [dict(r) for r in c.execute(f"SELECT * FROM followup_drafts WHERE application_id IN ({q})", ids)]
            briefs = [dict(r) for r in c.execute("SELECT brief_id, skill, title, status FROM project_briefs WHERE offer_id=?",
                                                 (offer_id,))]
            from app.agent.agent004_offer_scorer import project_gain
            for br in briefs:                       # PROJECT_GAIN, computed now (your profile may have changed)
                br["gain"] = project_gain(c, offer_id, [x.strip() for x in br["skill"].split(",")],
                                          criteria=orch.instance["criteria"], words=orch.schemas["words"],
                                          scoring=orch.config["scoring"])
        company = {"facts": json.loads(comp[0]), "confidence": comp[1], "updated_at": comp[2]} if comp else None
        for sh in sheets:
            pth = Path(sh["path"])
            sh["text"] = pth.read_text(encoding="utf-8") if pth.is_file() else ""
        for a in apps:
            folder = orch.runs_dir / a["package_ref"]
            a["files"] = [f for f in ("cv.pdf", "letter.pdf", "package.md") if (folder / f).is_file()]
            a["package"] = (folder / "package.md").read_text(encoding="utf-8") if (folder / "package.md").is_file() else ""
        o = dict(o)
        d = decode_row(rows, "experience_decisions", d) if d else None
        s = decode_row(rows, "scores", s) if s else None
        steps = [{"name": "Collect", "state": "succeeded"},
                 {"name": "Experience filter", "state": {"keep": "succeeded", "review": "partial", "reject": "failed"}
                  .get((d or {}).get("user_override") or (d or {}).get("decision"), "pending")},
                 {"name": "Scoring", "state": ("failed" if s and s["eliminated"] else "succeeded") if s else "pending"},
                 {"name": "Application", "state": ("succeeded" if any(a["status"] != "ready" for a in apps)
                                                   else "partial") if apps else "pending"}]
        return render_template("job_pilot_page.html", o=o, d=d, s=s, aliases=aliases, steps=steps, apps=apps, company=company,
                               mails=mails, sheets=sheets, drafts=drafts, briefs=briefs,
                               run=dict(run) if run else None, busy=bool(orch.busy()), msg=request.args.get("msg"))

    @app.post("/offers/<offer_id>/decision")
    def offer_decision(offer_id: str):
        check_csrf()
        choice = request.form.get("choice")
        with db_connect(orch.db_path) as c:
            if choice in ("keep", "reject"):
                cur = c.execute("UPDATE experience_decisions SET user_override=? WHERE offer_id=?", (choice, offer_id))
            elif choice == "dismiss":
                cur = c.execute("UPDATE offers SET status='dismissed' WHERE offer_id=? AND status='new'", (offer_id,))
            else:
                abort(400)
            if cur.rowcount == 0:
                abort(404)
        orch.store.log_event("info", "user_decision", f"{offer_id}: {choice}", agent_id="user")
        return redirect(url_for("offer", offer_id=offer_id))

    @app.post("/offers/<offer_id>/full_text")
    def offer_full_text(offer_id: str):
        """ASK_FULL_TEXT: you paste the whole offer copied from its site; it replaces the excerpt (traced as an event)
        and the offer is judged again (experience, score); a decision you took on it is kept."""
        check_csrf()
        text = (request.form.get("text") or "").replace("\r\n", "\n").strip()
        if not 200 <= len(text) <= 60000:
            abort(400)
        from app.agent.agent002_offer_collector import reset_judgement
        with db_connect(orch.db_path) as c:
            cur = c.execute("UPDATE offers SET description=?, description_complete=1 WHERE offer_id=?", (text, offer_id))
            if cur.rowcount == 0:
                abort(404)
            reset_judgement(c, offer_id)
            c.execute("UPDATE alerts SET seen_at=? WHERE category='full_text' AND ref=? AND seen_at IS NULL",
                      (utc_now(), offer_id))
        orch.store.log_event("info", "user_full_text", f"{offer_id}: full text pasted ({len(text)} characters)",
                             agent_id="user")
        if not orch.busy() and _run_lock.acquire(blocking=False):
            def work():
                try:
                    orch.run("rescore", title=f"re-classement {offer_id} (texte complet collé)")
                finally:
                    _run_lock.release()
            threading.Thread(target=work, daemon=True, name=f"rescore-{offer_id}").start()
        return redirect(url_for("offer", offer_id=offer_id, msg="Full text saved: the offer is judged again."))

    # ------------------------------------------------------------------ applications (agent006): nothing is sent
    @app.post("/offers/<offer_id>/application")
    def start_application(offer_id: str):
        check_csrf()
        with db_connect(orch.db_path) as c:
            if not c.execute("SELECT 1 FROM offers WHERE offer_id=?", (offer_id,)).fetchone():
                abort(404)
        if orch.busy() or not _run_lock.acquire(blocking=False):
            return redirect(url_for("offer", offer_id=offer_id, msg="A run is already in progress."))
        letter = request.form.get("letter") == "1"

        def work():
            try:
                orch.run("application", params=application_params(offer_id, letter),
                         title=f"candidature {offer_id} (interface)")
            finally:
                _run_lock.release()

        threading.Thread(target=work, daemon=True, name=f"application-{offer_id}").start()
        return redirect(url_for("offer", offer_id=offer_id, msg="Application started."))

    @app.post("/applications/<application_id>/sent")
    def application_sent(application_id: str):
        check_csrf()
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT offer_id FROM applications WHERE application_id=? AND status='ready'",
                            (application_id,)).fetchone()
            if not row:
                abort(404)
            c.execute("UPDATE applications SET status='sent', sent_at=? WHERE application_id=?", (utc_now(), application_id))
            c.execute("UPDATE offers SET status='applied' WHERE offer_id=?", (row[0],))
        orch.store.log_event("info", "application_sent", application_id, agent_id="user")
        return redirect(url_for("offer", offer_id=row[0], msg="Application marked as sent."))

    @app.get("/applications/<application_id>/<name>")
    def application_file(application_id: str, name: str):
        """Only the package files of a recorded application, only from inside app/instance/runs/."""
        if name not in ("cv.pdf", "letter.pdf", "package.md", "cv.tex", "letter.txt"):
            abort(404)
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT package_ref FROM applications WHERE application_id=?", (application_id,)).fetchone()
        if not row:
            abort(404)
        base = orch.runs_dir.resolve()
        path = (base / row[0] / name).resolve()
        if base not in path.parents or not path.is_file():
            abort(404)
        mime = {"pdf": "application/pdf"}.get(name.rsplit(".", 1)[1], "text/plain; charset=utf-8")
        return send_file(path, mimetype=mime)

    # ------------------------------------------------------------------ edit a project idea (agent007 brief)
    def _brief(brief_id):
        with db_connect(orch.db_path) as c:
            row = c.execute("SELECT * FROM project_briefs WHERE brief_id=?", (brief_id,)).fetchone()
            if not row:
                abort(404)
            b = dict(row)
            offer = None
            if b.get("offer_id"):
                o = c.execute("SELECT offer_id, title, company, location, url FROM offers WHERE offer_id=?",
                              (b["offer_id"],)).fetchone()
                sc = c.execute("SELECT score FROM scores WHERE offer_id=? ORDER BY scored_at DESC LIMIT 1",
                               (b["offer_id"],)).fetchone()
                offer = {**dict(o), "score": sc[0] if sc else None} if o else None
        b["brief"] = json.loads(b["brief"])
        path = Path(b["path"]).resolve()
        if orch.runs_dir.resolve() not in path.parents:
            abort(404)
        return b, offer, path

    LIST_FIELDS = ("hardware", "software", "success_criteria", "pitfalls")

    @app.get("/docs/brief/<brief_id>/edit")
    def brief_edit(brief_id: str):
        b, offer, _path = _brief(brief_id)
        ans = b["brief"]
        form = {k: ans.get(k, "") for k in ("title", "objective", "portfolio_summary", "architecture", "interview_pitch")}
        form["builds_on"] = ", ".join(ans.get("builds_on") or [])
        form["done_on"] = ans.get("done_on", "")
        for k in LIST_FIELDS:
            form[k] = "\n".join(ans.get(k) or [])
        form["steps"] = "\n".join(" | ".join(str(st.get(x, "")) for x in ("days", "milestone", "deliverable"))
                                   for st in ans.get("steps") or [] if isinstance(st, dict))
        doc = {"kind": "brief", "ref": brief_id, "title": ans.get("title", ""), "sections": [], "files": [],
               "offer_id": b.get("offer_id"), "edit": form, "error": request.args.get("error")}
        return render_template("job_pilot_page.html", doc=doc)

    @app.post("/docs/brief/<brief_id>/edit")
    def brief_save(brief_id: str):
        """You edit the project: every field is yours; only the public summary keeps its rule (BRIEF_PUBLIC_SUMMARY,
        it goes on your public portfolio). The brief file is rewritten and the edit is traced."""
        check_csrf()
        from app.agent.agent007_gap_project_planner import brief_markdown, public_summary_problems
        b, offer, path = _brief(brief_id)
        f = {k: (request.form.get(k) or "").replace("\r\n", "\n").strip() for k in
             ("title", "objective", "portfolio_summary", "architecture", "interview_pitch", "builds_on", "steps", *LIST_FIELDS)}
        if not f["title"] or not f["objective"] or any(len(v) > 20000 for v in f.values()):
            return redirect(url_for("brief_edit", brief_id=brief_id, error="A title and an objective are required."))
        from app.agent.agent008_portfolio_publisher import parse_done_on
        done_on = (request.form.get("done_on") or "").strip()
        if done_on:
            try:
                done_on = parse_done_on(done_on)
            except ValueError:
                return redirect(url_for("brief_edit", brief_id=brief_id,
                                        error="Give the real date of the project (not in the future)."))
        bad = public_summary_problems(f["portfolio_summary"])
        if bad:
            return redirect(url_for("brief_edit", brief_id=brief_id,
                                    error="The public summary must describe what is built: remove " + ", ".join(bad)))
        steps = []
        for line in f["steps"].split("\n"):
            if line.strip():
                parts = [x.strip() for x in line.split("|")] + ["", ""]
                steps.append({"days": parts[0], "milestone": parts[1], "deliverable": parts[2]})
        ans = {**b["brief"], "title": f["title"], "objective": f["objective"], "portfolio_summary": f["portfolio_summary"],
               "architecture": f["architecture"], "interview_pitch": f["interview_pitch"],
               "builds_on": [x.strip() for x in f["builds_on"].split(",") if x.strip()], "steps": steps,
               **{k: [x.strip() for x in f[k].split("\n") if x.strip()] for k in LIST_FIELDS}, "edited_at": utc_now(),
               "done_on": done_on or None}
        path.write_text(brief_markdown(offer, [x.strip() for x in b["skill"].split(",")], ans), encoding="utf-8")
        with db_connect(orch.db_path) as c:
            c.execute("UPDATE project_briefs SET title=?, brief=? WHERE brief_id=?",
                      (ans["title"], json.dumps(ans, ensure_ascii=False), brief_id))
        orch.store.log_event("info", "user_brief_edit", f"{brief_id}: edited by you", agent_id="user")
        return redirect(url_for("document", kind="brief", ref=brief_id))

    # ------------------------------------------------------------------ "Projet réalisé" sheet → portfolio card (agent008)
    def _grid_titles():
        """Cards of your portfolio grid, read from the latest demo (a local copy of your public page)."""
        from app.agent.agent008_portfolio_publisher import grid_titles
        demos = sorted(orch.runs_dir.glob("*/portfolio_demo/" + orch.config["portfolio"]["file"]),
                       key=lambda x: x.stat().st_mtime)
        if not demos:
            return []
        try:
            return grid_titles(demos[-1].read_text(encoding="utf-8"), orch.config["portfolio"]["grid_marker"])
        except ValueError:
            return []

    @app.get("/docs/brief/<brief_id>/done")
    def brief_done(brief_id: str):
        b, _offer, _path = _brief(brief_id)
        ans = b["brief"]
        card = ans.get("card") or {}
        skill = b["skill"].split(",")[0].strip()
        form = {"short_name": card.get("short_name", skill.upper() if len(skill) <= 6 else skill.capitalize()),
                "domain": card.get("domain", ""), "context": card.get("context", "Seul"),
                "title": card.get("title", ans.get("title", "")), "summary": card.get("summary", ans.get("portfolio_summary", "")),
                "details_title": card.get("details_title", "Architecture"), "details": card.get("details", ans.get("architecture", "")),
                "tags": ", ".join(card.get("tags") or [x if len(x) <= 4 else x.capitalize() for x in
                                                         [skill, *(ans.get("builds_on") or [])]]),
                "link_url": card.get("link_url", ""), "link_label": card.get("link_label", "Code sur GitHub"),
                "image_alt": card.get("image_alt", ""), "categories": card.get("categories") or [],
                "position": card.get("position", "first"), "done_on": ans.get("done_on") or "",
                "figures": (card.get("figures") or []) + [{"value": "", "label": ""}] * 3, "image_file": card.get("image_file")}
        doc = {"kind": "brief", "ref": brief_id, "title": form["title"], "sections": [], "files": [],
               "offer_id": b.get("offer_id"), "done": form, "error": request.args.get("error"),
               "categories": orch.config["portfolio"].get("categories", {}), "grid": _grid_titles()}
        return render_template("job_pilot_page.html", doc=doc)

    @app.post("/docs/brief/<brief_id>/done")
    def brief_done_save(brief_id: str):
        """CARD_LIKE_YOURS: the card is made only from this sheet YOU fill (what you really built, your figures, your
        link, your image); REAL_PROJECT_DATE and IMAGE_SAFE apply. Traced; nothing is published here."""
        check_csrf()
        from app.agent.agent008_portfolio_publisher import image_problem, parse_done_on
        b, _offer, _path = _brief(brief_id)
        f = {k: (request.form.get(k) or "").replace("\r\n", "\n").strip() for k in
             ("short_name", "domain", "context", "title", "summary", "details_title", "details", "tags", "link_url",
              "link_label", "image_alt", "position", "done_on")}

        def back(msg):
            return redirect(url_for("brief_done", brief_id=brief_id, error=msg))
        if not (f["short_name"] and f["title"] and f["summary"]) or any(len(v) > 6000 for v in f.values()):
            return back("A short name, a title and a summary are required.")
        try:
            done_on = parse_done_on(f["done_on"])
        except ValueError:
            return back("Give the real date of the project (not in the future).")
        if f["link_url"] and not re.fullmatch(r"https://[^\s\"'<>]+", f["link_url"]):
            return back("The link must start with https://")
        cats = [x for x in request.form.getlist("categories") if x in orch.config["portfolio"].get("categories", {})]
        if not cats:
            return back("Choose at least one category of your portfolio.")
        if not (f["position"] in ("first", "last") or f["position"].startswith("after:")):
            f["position"] = "first"
        figures = []
        for i in range(3):
            v, lab = (request.form.get(f"fig_value_{i}") or "").strip(), (request.form.get(f"fig_label_{i}") or "").strip()
            if v:
                figures.append({"value": v[:24], "label": lab[:100]})
        card = {**{k: f[k] for k in ("short_name", "domain", "context", "title", "summary", "details_title", "details",
                                     "link_url", "link_label", "image_alt", "position")},
                "tags": [x.strip() for x in f["tags"].split(",") if x.strip()][:14], "categories": cats, "figures": figures,
                "image_file": (b["brief"].get("card") or {}).get("image_file")}
        up = request.files.get("image")
        if up and up.filename:
            data = up.read()
            bad = image_problem(up.filename, data, orch.config["portfolio"].get("image_max_bytes", 2_000_000))
            if bad:
                return back(bad)
            if not f["image_alt"]:
                return back("Describe the image (alternative text) for people who cannot see it.")
            folder = orch.runs_dir / "portfolio_assets"
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{brief_id}{Path(up.filename).suffix.lower()}"
            for old in folder.glob(f"{brief_id}.*"):
                old.unlink()
            dest.write_bytes(data)
            card["image_file"] = str(dest)
        if request.form.get("remove_image"):
            card["image_file"] = None
        ans = {**b["brief"], "card": card, "done_on": done_on}
        with db_connect(orch.db_path) as c:
            c.execute("UPDATE project_briefs SET brief=? WHERE brief_id=?", (json.dumps(ans, ensure_ascii=False), brief_id))
        orch.store.log_event("info", "user_project_done", f"{brief_id}: 'Projet réalisé' sheet saved", agent_id="user")
        return redirect(url_for("home", msg="Project sheet saved: make the demo to see the card.") + "#projects")

    # ------------------------------------------------------------------ documents in the application's look
    @app.get("/docs/<kind>/<ref>")
    def document(kind: str, ref: str):
        """Package, project brief or interview sheet rendered in the job_pilot page (instead of raw text)."""
        queries = {"package": "SELECT package_ref || '/package.md', offer_id FROM applications WHERE application_id=?",
                   "brief": "SELECT path, offer_id FROM project_briefs WHERE brief_id=?",
                   "sheet": "SELECT s.path, a.offer_id FROM interview_sheets s JOIN applications a USING(application_id) "
                            "WHERE s.sheet_id=?"}
        if kind not in queries:
            abort(404)
        with db_connect(orch.db_path) as c:
            row = c.execute(queries[kind], (ref,)).fetchone()
        if not row or not row[0]:
            abort(404)
        base = orch.runs_dir.resolve()
        path = (Path(row[0]) if kind != "package" else base / row[0]).resolve()
        if base not in path.parents or not path.is_file():
            abort(404)
        title, sections = md_sections(path.read_text(encoding="utf-8"))
        files = []
        if kind == "package":
            files = [f for f in ("cv.pdf", "letter.pdf") if (path.parent / f).is_file()]
        offer = None
        if row[1]:
            with db_connect(orch.db_path) as c:
                o = c.execute("SELECT offer_id, title, company, location, url FROM offers WHERE offer_id=?", (row[1],)).fetchone()
            if o:
                offer = dict(o)
                if not re.match(r"https?://", offer.get("url") or ""):     # a link to the offer's site: http(s) only
                    offer["url"] = None
        doc = {"kind": kind, "ref": ref, "title": title or path.name, "sections": sections, "files": files,
               "offer_id": row[1], "offer": offer, "when": path.stat().st_mtime}
        return render_template("job_pilot_page.html", doc=doc)

    # ------------------------------------------------------------------ supervision
    @app.get("/standup")
    def standup():
        data = orch.store.dashboard(orch.agent_ids)
        with db_connect(orch.db_path) as c:
            last_daily = c.execute("SELECT run_id FROM source_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            sources = [dict(r) for r in c.execute("SELECT * FROM source_runs WHERE run_id=? ORDER BY source_id",
                                                  (last_daily[0],))] if last_daily else []
            snap = c.execute("SELECT metrics FROM metrics_snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
        backup_dir = orch.db_path.parent / "backups"
        return render_template("job_pilot_standup.html", **data, sources=sources, stats=json.loads(snap[0]) if snap else None,
                               backups=sv.backups(backup_dir, "job_pilot"),
                               disk=sv.disk_usage({"database": orch.db_path.parent, "runs": orch.runs_dir}))

    @app.post("/approvals/<int:approval_id>")
    def approval_decide(approval_id: int):
        check_csrf()
        try:
            orch.decide(approval_id, request.form.get("decision") == "approve")
        except (KeyError, ValueError) as exc:
            orch.store.log_event("warning", "approval_refused", f"{approval_id}: {exc}", agent_id="user")
        return redirect(url_for("standup") + "#approvals")

    @app.post("/skills/propose")
    def propose_skills():
        check_csrf()
        orch.store.propose_skill_activations()
        return redirect(url_for("standup") + "#approvals")

    @app.post("/incidents/<int:incident_id>/close")
    def incident_close(incident_id: int):
        check_csrf()
        try:
            orch.store.close_incident(incident_id, request.form.get("status", ""), request.form.get("resolution", ""))
        except (ValueError, KeyError):
            pass
        return redirect(url_for("standup"))
