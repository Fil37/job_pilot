"""job_pilot — pages and web actions (Flask), on the Studio Kit shell.

Language and theme routes, translation, common variables and the CSP header with a nonce come from
studio-kit/demo/app.py. Long operations (runs) go to a background thread through agent000: a page never waits.
Every POST checks a CSRF token. Nothing here sends anything outside.
"""
from __future__ import annotations

import json
import secrets
import threading
from urllib.parse import urlsplit

from flask import abort, g, redirect, render_template, request, session, url_for

from app.agent import db_connect, utc_now
from app.agent import supervision as sv
from app.agent.agent000_job_pilot import Orchestrator, build_digest

WEB_CHAINS = ("daily", "rescore")
_run_lock = threading.Lock()


def register(app, orch: Orchestrator) -> None:
    words = orch.schemas["words"]
    fr = {**words["ui_text"]["fr"], **words["skill_text"]["fr"]}

    def translate(text: str) -> str:
        return fr.get(text, text) if g.get("lang") == "fr" else text

    app.jinja_env.filters["fromjson"] = lambda v: json.loads(v) if v else {}

    def check_csrf():
        if not request.form.get("csrf") or request.form.get("csrf") != session.get("csrf"):
            abort(400)

    @app.before_request
    def prepare():
        g.lang = request.cookies.get("lang") if request.cookies.get("lang") in ("en", "fr") else "en"
        g.csp_nonce = secrets.token_urlsafe(16)
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)

    @app.context_processor
    def inject():
        theme = request.cookies.get("theme")
        return {"labels": {"app_title": "Job Pilot", "version": "v0.1", "css_file": "job_pilot_master.css"},
                "lang": g.lang, "theme": theme if theme in ("light", "dark") else "auto", "_": translate,
                "csrf": session.get("csrf"), "csp_nonce": g.csp_nonce, "error": None,
                "reasons": words["experience_reasons"], "elim": words["elimination_reasons"]}

    @app.after_request
    def security(resp):
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
        example = bool(prof and json.loads(prof[0]).get("example"))
        return render_template("job_pilot.html", stats=stats, digest=digest, last=last, example=example,
                               busy=bool(orch.busy()), msg=request.args.get("msg"))

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
                orch.run(chain, title=f"{chain} (web)")
            finally:
                _run_lock.release()

        threading.Thread(target=work, daemon=True, name=f"run-{chain}").start()
        return redirect(url_for("home", msg="Run started."))

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
        o = dict(o)
        d = decode_row(rows, "experience_decisions", d) if d else None
        s = decode_row(rows, "scores", s) if s else None
        steps = [{"name": "Collect", "state": "succeeded"},
                 {"name": "Experience filter", "state": {"keep": "succeeded", "review": "partial", "reject": "failed"}
                  .get((d or {}).get("user_override") or (d or {}).get("decision"), "pending")},
                 {"name": "Scoring", "state": ("failed" if s and s["eliminated"] else "succeeded") if s else "pending"},
                 {"name": "Application", "state": "pending"}]
        return render_template("job_pilot_page.html", o=o, d=d, s=s, aliases=aliases, steps=steps,
                               run=dict(run) if run else None)

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

    # ------------------------------------------------------------------ supervision
    @app.get("/standup")
    def standup():
        data = orch.store.dashboard(orch.agent_ids)
        with db_connect(orch.db_path) as c:
            last_daily = c.execute("SELECT run_id FROM source_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            sources = [dict(r) for r in c.execute("SELECT * FROM source_runs WHERE run_id=? ORDER BY source_id",
                                                  (last_daily[0],))] if last_daily else []
        backup_dir = orch.db_path.parent / "backups"
        return render_template("job_pilot_standup.html", **data, sources=sources,
                               backups=sv.backups(backup_dir, "job_pilot"),
                               disk=sv.disk_usage({"database": orch.db_path.parent, "runs": orch.runs_dir}))

    @app.post("/approvals/<int:approval_id>")
    def approval_decide(approval_id: int):
        check_csrf()
        try:
            orch.store.decide(approval_id, request.form.get("decision") == "approve", handlers={})
        except (KeyError, ValueError):
            pass
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
