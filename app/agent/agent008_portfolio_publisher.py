"""agent008 — portfolio publisher.

Objective        : show a project brief as a card of your real portfolio (local demo, nothing published), then,
                   after your approval, publish it on the date you chose and remove it at the end date if you set one.
Scope            : ONE repository and ONE file (YOUR portfolio, job_pilot.json overrides.portfolio; optional); the card is inserted
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
from datetime import datetime
from pathlib import Path

from app.agent.common_projects import (parse_done_on, IMAGE_TYPES, image_problem, grid_span, grid_titles)  # noqa: F401 - shared, see common_projects.py
from app.agent import NotFound, BaseAgent, RunContext, db_connect, decode_row, envelope, fold, sha, upsert, utc_now
from app.agent import supervision as sv

SKILLS = [
    {"skill_id": "SK_PORTFOLIO_RENDER", "version": "2", "status": "proposed",
     "objective": "Produire une démo de ta carte de portfolio, faite à partir de ta fiche « Projet réalisé », sans rien publier.",
     "summary": "Clone ton portfolio public (lecture seule) et insère la carte au format exact de tes autres cartes (nom "
                "court, domaine, contexte et année réelle, chiffres, volet dépliable, mots-clés, lien, image), dans tes "
                "catégories et à la place choisie (contrôles CARD_LIKE_YOURS, REAL_PROJECT_DATE, IMAGE_SAFE). Rien n'est envoyé."},
    {"skill_id": "SK_PORTFOLIO_PUBLISH", "version": "2", "status": "proposed",
     "objective": "Publier une carte approuvée à la date choisie, pour une durée limitée si demandé.",
     "summary": "Seulement après une validation publish_portfolio_entry ; à publish_at la carte (et ton image) sont envoyées, "
                "à expire_at elles sont retirées. Contrôle PORTFOLIO_DIFF_SCOPE : index.html entre les marqueurs job_pilot "
                "et, au plus, l'image de ta carte. Jeton lu dans .env par GIT_ASKPASS, jamais journalisé."},
]

AGENT_ID = "agent008"
GRID_CLOSE = "<!-- job_pilot:end"


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", fold(text))).strip("-")[:60].strip("-") or "projet"


def card_year(brief: dict) -> int | None:
    """Year shown on the card: the year of YOUR date of realisation, never today's (REAL_PROJECT_DATE)."""
    try:
        return int(parse_done_on(brief.get("done_on"))[:4])
    except ValueError:
        return None


CARD_FIELDS = ("short_name", "domain", "context", "title", "summary", "details_title", "details", "tags", "link_url",
               "link_label", "image_alt", "categories", "position", "figures", "image_file")


def card_html(card: dict, slug: str, year: int | None, image_src: str | None = None) -> str:
    """CARD_LIKE_YOURS (remark of the user 2026-11-23: the generated cards did not look like his): the card in the exact
    markup of the portfolio's 'Autres projets' grid, from the 'Projet réalisé' sheet YOU filled. Every text is escaped."""
    e = html.escape
    ctx_line = ", ".join(x for x in (card.get("context"), str(year) if year else "") if x)
    side = (f'<div class="side"><strong>{e(card["short_name"])}</strong>{e(card.get("domain") or "")}'
            f'{"<br>" + e(ctx_line) if ctx_line else ""}<span class="kind perso">Projet personnel</span></div>')
    parts = [f'  <article class="proj compact" data-cat="{e(" ".join(card.get("categories") or []))}" data-type="perso">',
             f"    {side}", "    <div>"]
    if image_src:
        parts.append(f'      <figure class="proj-visual"><img src="{e(image_src)}" alt="{e(card.get("image_alt") or "")}" '
                     f'loading="lazy" decoding="async"></figure>')
    parts += [f"      <h3>{e(card['title'])}</h3>", f"      <p>{e(card['summary'])}</p>"]
    figs = [f for f in card.get("figures") or [] if f.get("value")]
    if figs:
        parts.append('      <div class="figures">')
        parts += [f'        <div><b>{e(f["value"])}</b><span>{e(f.get("label") or "")}</span></div>' for f in figs[:3]]
        parts.append("      </div>")
    if card.get("details"):
        parts.append(f"      <details><summary>{e(card.get('details_title') or 'Architecture')}</summary>"
                     f"<p>{e(card['details'])}</p></details>")
    tags = card.get("tags") or []
    if tags:
        parts.append('      <ul class="tags">' + "".join(f"<li>{e(t)}</li>" for t in tags) + "</ul>")
    if card.get("link_url"):
        parts.append(f'      <p style="margin-top:14px"><a class="link" href="{e(card["link_url"])}">'
                     f'{e(card.get("link_label") or "Code sur GitHub")}</a></p>')
    elif card.get("link_label"):
        parts.append(f'      <p style="margin-top:14px"><span class="link">{e(card["link_label"])}</span></p>')
    parts += ["    </div>", "  </article>"]
    return f"<!-- job_pilot:begin {slug} -->\n" + "\n".join(parts) + f"\n<!-- job_pilot:end {slug} -->\n"


def insert_card(page: str, slug: str, card: str, grid_marker: str, position: str = "first") -> str:
    """Idempotent: an existing block of the same slug is removed first. position = 'first' (top of the grid), 'last'
    or 'after:<title of a card>' (YOUR choice of place; an unknown title falls back to the top)."""
    page = remove_card(page, slug)
    start, end, cards = grid_span(page, grid_marker)
    at = start
    if position == "last":
        at = cards[-1][1] if cards else start
    elif position.startswith("after:"):
        at = next((p for t, p in cards if t == position[6:]), start)
    return page[:at] + "\n" + card.rstrip("\n") + "\n" + page[at:].lstrip("\n") if at == start else \
        page[:at] + "\n" + card.rstrip("\n") + page[at:]


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
        if not ctx.config["portfolio"].get("repo"):    # PORTFOLIO_OPTIONAL: no portfolio configured, nothing to do
            return [{"objective": "no portfolio configured (optional)", "kind": None}]
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
        ctx.store.register_check("REAL_PROJECT_DATE", AGENT_ID, "la carte affiche l'année de réalisation que tu as donnée, "
                                 "jamais celle du jour ; pas de publication sans elle",
                                 "règle de l'utilisateur 2026-11-23 : des projets déjà faits mais pas encore versionnés")
        ctx.store.register_check("PORTFOLIO_DIFF_SCOPE", AGENT_ID, "index.html (entre les marqueurs job_pilot) et, au plus, "
                                 "l'image de ta carte ; rien d'autre",
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

    def _card(self, b: dict, slug: str, cfg: dict):
        """(card markup, (image source path, path in the portfolio) or None) from YOUR 'Projet réalisé' sheet."""
        card = b["brief"]["card"]
        image = None
        if card.get("image_file") and Path(card["image_file"]).is_file():
            image = (card["image_file"], f"{cfg.get('assets_dir', 'portfolio-assets')}/{slug}{Path(card['image_file']).suffix.lower()}")
        return card_html(card, slug, card_year(b["brief"]), image[1] if image else None), image

    def _brief(self, ctx, brief_id):
        with ctx.connect() as c:
            row = c.execute("SELECT * FROM project_briefs WHERE brief_id=?", (brief_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown brief {brief_id}")
        return decode_row(ctx.rows, "project_briefs", row)

    def _demo(self, ctx, git, cfg, brief_id) -> dict:
        b = self._brief(ctx, brief_id)
        if not (b["brief"].get("card") or {}).get("title"):
            raise ValueError("fiche « Projet réalisé » non remplie : remplis-la avant la démo")
        slug = slugify(b["brief"]["card"]["title"])
        folder = ctx.run_dir / "portfolio_demo"
        if folder.exists():
            shutil.rmtree(folder)
        git.run("clone", "--depth", "1", "--branch", cfg["branch"], cfg["repo"], str(folder))
        shutil.rmtree(folder / ".git", ignore_errors=True)               # a demo is not a repository
        page = folder / cfg["file"]
        card, image = self._card(b, slug, cfg)
        if image:
            (folder / image[1]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image[0], folder / image[1])
        page.write_text(insert_card(page.read_text(encoding="utf-8"), slug, card, cfg["grid_marker"],
                                    b["brief"]["card"].get("position") or "first"), encoding="utf-8")
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
        asset = None
        if kind == "publish":
            if card_year(b["brief"]) is None:      # REAL_PROJECT_DATE: never published with today's date by default
                raise ValueError("date de réalisation du projet absente : renseigne-la avant de publier")
            if not (b["brief"].get("card") or {}).get("title"):
                raise ValueError("fiche « Projet réalisé » non remplie")
            card, image = self._card(b, e["slug"], cfg)
            if image:
                asset = image[1]
                dest = work / asset
                if dest.exists() and dest.read_bytes() != Path(image[0]).read_bytes():
                    raise ValueError(f"{asset} existe déjà dans ton portfolio avec un autre contenu")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(image[0], dest)
            new = insert_card(text, e["slug"], card, cfg["grid_marker"], b["brief"]["card"].get("position") or "first")
            message = f"Add project {b['brief']['card']['title']} (job_pilot, approved)"
        else:
            new = remove_card(text, e["slug"])
            image_file = (b["brief"].get("card") or {}).get("image_file")
            if image_file:
                asset = f"{cfg.get('assets_dir', 'portfolio-assets')}/{e['slug']}{Path(image_file).suffix.lower()}"
                if (work / asset).exists():
                    (work / asset).unlink()
            message = f"Remove project {(b['brief'].get('card') or {}).get('title') or b['title']} (job_pilot, end date reached)"
        if new == text:
            raise ValueError("nothing to change in the portfolio page")
        page.write_text(new, encoding="utf-8")
        git.run("add", "-A", cwd=work)
        status = sorted(tuple(line.split("\t", 1)) for line in git.run("diff", "--cached", "--name-status", cwd=work).splitlines()
                        if line.strip())
        stat = git.run("diff", "--cached", "--numstat", "--", cfg["file"], cwd=work).split()
        added, deleted = (int(stat[0]), int(stat[1])) if len(stat) >= 2 else (0, 0)
        # PORTFOLIO_DIFF_SCOPE: the page (additions only to publish, deletions only to remove) and, at most, YOUR image of
        # this card (added to publish, deleted to remove); nothing else
        expected = sorted([("M", cfg["file"])] + ([("A" if kind == "publish" else "D", asset)] if asset else []))
        scope_ok = (status == expected or (kind == "publish" and asset and status == [("M", cfg["file"])])) and \
            (deleted == 0 if kind == "publish" else added == 0)
        outcomes.append({"check": "PORTFOLIO_DIFF_SCOPE", "item": entry_id, "outcome": "pass" if scope_ok else "kept_despite",
                         "detail": f"{status} +{added} -{deleted}"})
        if not scope_ok:
            raise ValueError(f"diff outside the allowed scope: {status} +{added} -{deleted}")
        git.run("-c", f"user.name={cfg['author_name']}", "-c", "user.email=job_pilot@localhost", "commit", "-m", message, cwd=work)
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
        if ctx and not ctx.config["portfolio"].get("repo"):
            return "healthy", "pas de portfolio configuré (facultatif) : rien à publier"
        if not shutil.which("git"):
            return "critical", "git absent"
        if ctx and not os.environ.get(ctx.config["portfolio"]["token_env"]):
            return "degraded", (f"{ctx.config['portfolio']['token_env']} absent des secrets : les démos marchent, "
                                "pas la publication")
        return "healthy", "git présent, jeton configuré"


# =========================================================================== USER_ACTIONS (called by agent000.act)
def action_request_publication(orch, *, entry_id: str, publish_at: str, expire_at: str | None = None,
                               done_on: str | None = None) -> dict:
    """You ask for a publication: only an approval request is created (approved on the Supervision page)."""
    try:
        pub = datetime.fromisoformat(publish_at or "")
        exp = datetime.fromisoformat(expire_at) if expire_at else None
    except ValueError:
        raise ValueError("Invalid date.") from None
    if exp and exp <= pub:
        raise ValueError("Invalid date.")
    cfg = orch.config["portfolio"]
    with db_connect(orch.db_path) as c:
        row = c.execute("SELECT slug FROM portfolio_entries WHERE entry_id=?", (entry_id,)).fetchone()
    if not row:
        raise NotFound(entry_id)
    orch.store.request_approval("publish_portfolio_entry", entry_id, {
        "repo": cfg["repo"], "file": cfg["file"], "slug": row[0], "publish_at": pub.isoformat(timespec="minutes"),
        "expire_at": exp.isoformat(timespec="minutes") if exp else None, "done_on": done_on})
    return {"event": "user_publication_request", "detail": f"{entry_id}: {pub.isoformat(timespec='minutes')}"}


ACTIONS = {"request_publication": action_request_publication}


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
