"""agent008 — portfolio publisher.

Objective        : show a project brief as a card of your real portfolio (local demo, nothing published), then,
                   after your approval, publish it on the date you chose and remove it at the end date if you set one.
Scope            : ONE repository and ONE file (settings.portfolio: Fil37.github.io, index.html); the card is inserted
                   at the top of the project grid between <!-- job_pilot:begin SLUG --> / <!-- job_pilot:end SLUG -->
                   markers, so it can be replaced or removed without touching anything else.
Out of scope     : NEVER pushes without an approved 'publish_portfolio_entry' whose target is the entry and whose
                   repository is the configured one; never changes another file or a line outside the markers
                   (check PORTFOLIO_DIFF_SCOPE); never writes the token in a command line, a URL or a log; never designs
                   the project (agent007).
Inputs           : params {brief_id, action: demo} (web) or nothing (hourly: due publications and removals).
Outputs          : portfolio_entries rows; the demo folder in the run folder; commits on the portfolio repository.
Tools            : git (clone --depth 1, commit, push) through subprocess with argument lists; GIT_ASKPASS reads the
                   token from the environment (GITHUB_TOKEN in .env).
Data read        : project_briefs, portfolio_entries, the public portfolio repository.
Data written     : portfolio_entries (owner); index.html of the portfolio (only after approval, only between markers).
Success criteria : the demo renders your portfolio with the card; a publication changes index.html only, by
                   additions only (removal: by deletions only); commit sha recorded.
Cost limits      : 0 model call.
Time limits      : 120 s per git command.
Error policy     : any git error → entry 'failed' with the cleaned message (token removed), nothing retried blindly;
                   an incident is opened (access_denied / source_unavailable).
"""
from __future__ import annotations

import html
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.agent import BaseAgent, RunContext, db_connect, decode_row, envelope, fold, sha, upsert, utc_now
from app.agent import supervision as sv

SKILLS = [
    {"skill_id": "SK_PORTFOLIO_RENDER", "version": "1", "status": "proposed",
     "objective": "Produire une démo séparée de l'entrée de portfolio, sans rien publier.",
     "summary": "Clone ton portfolio public (lecture seule), insère la carte du projet en tête de la grille entre les "
                "marqueurs job_pilot et sert le résultat en local (« Ouvrir la démo », accueil). Rien n'est envoyé."},
    {"skill_id": "SK_PORTFOLIO_PUBLISH", "version": "1", "status": "proposed",
     "objective": "Publier une entrée approuvée à la date choisie, pour une durée limitée si demandé.",
     "summary": "Seulement après une validation publish_portfolio_entry (dépôt, fichier et dates exacts) ; à "
                "publish_at la carte est envoyée, à expire_at elle est retirée. Contrôle PORTFOLIO_DIFF_SCOPE : "
                "index.html seulement, ajouts seulement (retrait : suppressions seulement). Jeton lu dans .env par "
                "GIT_ASKPASS, jamais journalisé."},
]

AGENT_ID = "agent008"
GRID_CLOSE = "<!-- job_pilot:end"


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", fold(text))).strip("-")[:60].strip("-") or "projet"


def card_html(brief: dict, skill: str, year: int, slug: str) -> str:
    """The card in the markup of your portfolio (compact article). Every text is HTML-escaped."""
    e = html.escape
    tags = "".join(f"<li>{e(t)}</li>" for t in [skill] + list(brief.get("builds_on") or [])[:5])
    steps = " ".join(f"{st.get('days')} : {st.get('milestone')}." for st in brief.get("steps") or [] if isinstance(st, dict))
    return (f"<!-- job_pilot:begin {slug} -->\n"
            f'  <article class="proj compact" data-cat="{e(slugify(skill))}" data-type="perso">\n'
            f'    <div class="side"><strong>{e(skill.upper() if len(skill) <= 6 else skill.capitalize())}</strong>'
            f'Projet personnel<br>{year}<span class="kind">Projet personnel</span></div>\n'
            f"    <div>\n      <h3>{e(brief['title'])}</h3>\n      <p>{e(brief.get('portfolio_summary') or brief['objective'])}</p>\n"
            f"      <details><summary>Architecture</summary><p>{e(brief['architecture'])}</p></details>\n"
            f"      <details><summary>Étapes</summary><p>{e(steps)}</p></details>\n"
            f'      <ul class="tags">{tags}</ul>\n    </div>\n  </article>\n'
            f"<!-- job_pilot:end {slug} -->\n")


def insert_card(page: str, slug: str, card: str, grid_marker: str) -> str:
    """Idempotent: an existing block of the same slug is replaced; otherwise the card goes first in the grid."""
    page = remove_card(page, slug)
    i = page.find(grid_marker)
    if i < 0:
        raise ValueError("project grid not found in the portfolio page")
    j = i + len(grid_marker)
    return page[:j] + "\n" + card + page[j:].lstrip("\n")


def remove_card(page: str, slug: str) -> str:
    return re.sub(rf"<!-- job_pilot:begin {re.escape(slug)} -->.*?<!-- job_pilot:end {re.escape(slug)} -->\n?", "",
                  page, flags=re.S)


class Git:
    """git through argument lists only; the token never appears in argv, URLs or logs (GIT_ASKPASS)."""

    def __init__(self, token: str | None, timeout: int = 120):
        self.token, self.timeout = token or "", timeout
        self.tmp = Path(tempfile.mkdtemp(prefix="jp_git_"))
        os.chmod(self.tmp, 0o700)
        self.askpass = self.tmp / "askpass.sh"
        self.askpass.write_text('#!/bin/sh\ncase "$1" in *sername*) echo x-access-token ;; *) echo "$JP_GIT_TOKEN" ;; esac\n')
        os.chmod(self.askpass, stat.S_IRWXU)

    def run(self, *args, cwd=None) -> str:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), "GIT_TERMINAL_PROMPT": "0",
               "GIT_ASKPASS": str(self.askpass), "JP_GIT_TOKEN": self.token, "LC_ALL": "C"}
        r = subprocess.run(["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(sv.redact(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-300:]}", [self.token]))
        return r.stdout

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def approve_publish(orch, target: str, params: dict) -> dict:
    """Handler of an APPROVED 'publish_portfolio_entry': schedules the entry with its exact dates. The push itself
    happens in the next portfolio run when publish_at is reached."""
    cfg = orch.config["portfolio"]
    if params.get("repo") != cfg["repo"] or params.get("file") != cfg["file"]:
        raise ValueError("approval target differs from the configured portfolio")
    pub = datetime.fromisoformat(params["publish_at"])
    exp = datetime.fromisoformat(params["expire_at"]) if params.get("expire_at") else None
    if exp and exp <= pub:
        raise ValueError("end date before publication date")
    with db_connect(orch.db_path) as c:
        cur = c.execute("UPDATE portfolio_entries SET status='scheduled', publish_at=?, expire_at=?, updated_at=? "
                        "WHERE entry_id=? AND status IN ('demo','failed','expired')",
                        (pub.isoformat(timespec="minutes"), exp.isoformat(timespec="minutes") if exp else None, utc_now(), target))
        if cur.rowcount == 0:
            raise ValueError("entry not found or already scheduled / published")
    return {"applied": True, "scheduled": params["publish_at"], "until": params.get("expire_at")}


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def plan(self, ctx: RunContext):
        if ctx.params.get("brief_id"):
            return [{"objective": "render the demo", "kind": "demo", "brief_id": ctx.params["brief_id"]}]
        now = datetime.now().isoformat(timespec="minutes")
        with ctx.connect() as c:
            pub = [r[0] for r in c.execute("SELECT entry_id FROM portfolio_entries WHERE status='scheduled' AND publish_at<=?", (now,))]
            exp = [r[0] for r in c.execute("SELECT entry_id FROM portfolio_entries WHERE status='published' AND expire_at IS NOT NULL "
                                           "AND expire_at<=?", (now,))]
        return ([{"objective": "publish", "kind": "publish", "entry_id": e} for e in pub] +
                [{"objective": "remove", "kind": "remove", "entry_id": e} for e in exp]) or [{"objective": "nothing due", "kind": None}]

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("PORTFOLIO_DIFF_SCOPE", AGENT_ID, "index.html seulement, et seulement entre les marqueurs job_pilot",
                                 "rule of the user: what goes outside is approved, nothing else changes")
        cfg = ctx.config["portfolio"]
        done, errors, outcomes = [], [], []
        for sub in subtasks:
            if not sub.get("kind"):
                continue
            git = Git(os.environ.get(cfg["token_env"]) if sub["kind"] != "demo" else None)
            try:
                if sub["kind"] == "demo":
                    done.append(self._demo(ctx, git, cfg, sub["brief_id"]))
                else:
                    done.append(self._push(ctx, git, cfg, sub["entry_id"], sub["kind"], outcomes))
            except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
                msg = sv.redact(str(exc), [os.environ.get(cfg["token_env"], "")])
                cat = "access_denied" if re.search(r"403|401|denied|Authentication", msg) else "source_unavailable"
                errors.append({"category": cat, "message": f"{sub['kind']}: {msg[:300]}"})
                if sub.get("entry_id"):
                    with ctx.connect() as c:
                        c.execute("UPDATE portfolio_entries SET status='failed', error=?, updated_at=? WHERE entry_id=?",
                                  (msg[:300], utc_now(), sub["entry_id"]))
            finally:
                git.close()
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        status = "failed" if errors and not done else ("partial" if errors else "succeeded")
        alerts = [{"kind": "immediate", "category": "portfolio", "ref": d["entry_id"], "message": d["message"]}
                  for d in done if d.get("message")]
        return envelope(ctx, AGENT_ID, status, errors=errors, data={"done": done, "alerts": alerts},
                        metrics={"done": len(done)})

    def _brief(self, ctx, brief_id):
        with ctx.connect() as c:
            row = c.execute("SELECT * FROM project_briefs WHERE brief_id=?", (brief_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown brief {brief_id}")
        return decode_row(ctx.rows, "project_briefs", row)

    def _demo(self, ctx, git, cfg, brief_id) -> dict:
        b = self._brief(ctx, brief_id)
        slug = slugify(b["title"])
        folder = ctx.run_dir / "portfolio_demo"
        if folder.exists():
            shutil.rmtree(folder)
        git.run("clone", "--depth", "1", "--branch", cfg["branch"], cfg["repo"], str(folder))
        shutil.rmtree(folder / ".git", ignore_errors=True)               # a demo is not a repository
        page = folder / cfg["file"]
        page.write_text(insert_card(page.read_text(encoding="utf-8"), slug,
                                    card_html(b["brief"], b["skill"], datetime.now().year, slug), cfg["grid_marker"]),
                        encoding="utf-8")
        entry_id = "p_" + sha(brief_id, 12)
        with ctx.connect() as c:
            prev = c.execute("SELECT status FROM portfolio_entries WHERE entry_id=?", (entry_id,)).fetchone()
            status = prev[0] if prev and prev[0] in ("scheduled", "published") else "demo"
            upsert(c, ctx.rows, "portfolio_entries", {"entry_id": entry_id, "brief_id": brief_id, "slug": slug,
                   "demo_path": str(page), "status": status, "run_id": ctx.run_id, "updated_at": utc_now()})
        return {"entry_id": entry_id, "demo": str(page), "message": None}

    def _push(self, ctx, git, cfg, entry_id, kind, outcomes) -> dict:
        with ctx.connect() as c:
            e = dict(c.execute("SELECT * FROM portfolio_entries WHERE entry_id=?", (entry_id,)).fetchone())
            ok = c.execute("SELECT 1 FROM approvals WHERE action='publish_portfolio_entry' AND target=? AND status='approved'",
                           (entry_id,)).fetchone()
        if not ok:
            raise ValueError("no approved publish_portfolio_entry for this entry")   # never push without approval
        b = self._brief(ctx, e["brief_id"])
        work = git.tmp / "repo"
        git.run("clone", "--depth", "1", "--branch", cfg["branch"], cfg["repo"], str(work))
        page = work / cfg["file"]
        text = page.read_text(encoding="utf-8")
        if kind == "publish":
            new = insert_card(text, e["slug"], card_html(b["brief"], b["skill"], datetime.now().year, e["slug"]), cfg["grid_marker"])
            message = f"Add project {b['title']} (job_pilot, approved)"
        else:
            new = remove_card(text, e["slug"])
            message = f"Remove project {b['title']} (job_pilot, end date reached)"
        if new == text:
            raise ValueError("nothing to change in the portfolio page")
        page.write_text(new, encoding="utf-8")
        names = git.run("diff", "--name-only", cwd=work).split()
        added, deleted = (int(x) for x in git.run("diff", "--numstat", cwd=work).split()[:2])
        scope_ok = names == [cfg["file"]] and (deleted == 0 if kind == "publish" else added == 0)
        outcomes.append({"check": "PORTFOLIO_DIFF_SCOPE", "item": entry_id, "outcome": "pass" if scope_ok else "kept_despite",
                         "detail": f"{names} +{added} -{deleted}"})
        if not scope_ok:
            raise ValueError(f"diff outside the allowed scope: {names} +{added} -{deleted}")
        git.run("-c", f"user.name={cfg['author_name']}", "-c", "user.email=job_pilot@localhost", "commit", "-am", message, cwd=work)
        git.run("push", "origin", f"HEAD:{cfg['branch']}", cwd=work)
        commit = git.run("rev-parse", "HEAD", cwd=work).strip()
        with ctx.connect() as c:
            if kind == "publish":
                c.execute("UPDATE portfolio_entries SET status='published', commit_sha=?, error=NULL, updated_at=? WHERE entry_id=?",
                          (commit, utc_now(), entry_id))
            else:
                c.execute("UPDATE portfolio_entries SET status='expired', removed_sha=?, updated_at=? WHERE entry_id=?",
                          (commit, utc_now(), entry_id))
        verb = "publié" if kind == "publish" else "retiré"
        return {"entry_id": entry_id, "commit": commit, "message": f"Portfolio : projet « {b['title']} » {verb} ({commit[:7]})"}

    def healthcheck(self, ctx=None):
        if not shutil.which("git"):
            return "critical", "git absent"
        if ctx and not os.environ.get(ctx.config["portfolio"]["token_env"]):
            return "degraded", "GITHUB_TOKEN absent du .env : les démos marchent, pas la publication"
        return "healthy", "git présent, jeton configuré"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
