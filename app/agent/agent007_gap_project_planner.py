"""agent007 — gap project planner.

Objective        : for EACH favourable offer that misses 1 or 2 skills, propose a realistic 1-2 week project brief from
                   A to Z that teaches exactly those skills (rule of the user 2026-11-23: "je préfère qu'on me propose
                   à chaque fois que nécessaire le projet à ajouter pour chaque offre"). Also lists, over
                   settings.gaps.window_days, the skills most often missing (information on the home page).
Scope            : trigger computed by the program, per offer: (1) the offer is green on everything else (not
                   eliminated, experience keep or review, score >= min_score), (2) only 1 to max_missing skills are
                   missing and none is already in your profile, (3) the offer has no brief yet (a dismissed one is not
                   proposed again). Best scores first, at most max_briefs_per_run briefs per run (cost and attention);
                   the others come at the next runs.
Out of scope     : NEVER adds the project to your CV or profile, never publishes it (agent008, after approval),
                   never decides the trigger with the model.
Inputs           : scores, offers, experience_decisions (window), profile file, project_briefs.
Outputs          : skill_gaps rows; one project_briefs row (with offer_id) + brief_<offer>.md per offer; an alert each.
Tools            : arithmetic for the trigger; DeepSeek (JSON only, checked) for the brief.
Data read        : scores, offers, experience_decisions, profile file, project_briefs.
Data written     : skill_gaps, project_briefs (owner).
Success criteria : the brief has every section and names every target skill; 'builds_on' only lists skills of your
                   profile; the steps fit in 10 working days (check BRIEF_STRUCTURE).
Cost limits      : 1 call per brief (+ up to 3 corrections), at most max_briefs_per_run briefs.
Time limits      : settings.timeouts_s.agent_default.
Error policy     : nothing to propose → succeeded with nothing to do; model refused or wrong after 3 corrections → no
                   brief for that offer, retried at the next run; the other offers go on.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent import ROOT, BaseAgent, RunContext, envelope, fold, make_llm, sha, upsert, utc_now
from app.agent.agent001_profile_builder import load_profile
from app.agent.agent006_application_builder import ask_with_corrections

SKILLS = [
    {"skill_id": "SK_GAP_AGGREGATE", "version": "2", "status": "proposed",
     "objective": "Choisir, offre par offre, les offres favorables auxquelles il manque 1 ou 2 compétences ; lister les manques les plus fréquents.",
     "summary": "Par offre (règle du 2026-11-23) : non éliminée, expérience gardée ou à revoir, score au moins égal à "
                "min_score, 1 ou 2 compétences absentes de ton profil, pas encore de brief ; meilleurs scores "
                "d'abord, max_briefs_per_run par passage. Calculé par le programme. Les compétences les plus souvent "
                "manquantes sur 30 jours sont affichées sur l'accueil."},
    {"skill_id": "SK_PROJECT_BRIEF", "version": "2", "status": "proposed",
     "objective": "Concevoir, pour une offre, un brief de projet réaliste de 1 à 2 semaines qui enseigne ses compétences manquantes.",
     "summary": "Objectif, architecture, matériel et logiciels, étapes et jalons, critères de réussite, pièges, "
                "comment le présenter en entretien ; nomme chaque compétence manquante de l'offre et s'appuie sur tes "
                "compétences (contrôle BRIEF_STRUCTURE). Signalé par une alerte ; le brief s'ouvre depuis l'accueil "
                "et la page de l'offre."},
]

AGENT_ID = "agent007"
BRIEF_PROMPT = """You design a realistic personal project of 1 to 2 weeks for a junior embedded / electronics engineer, so
that he can learn the skills ONE job offer asks for and he does not have yet. Answer in JSON only, exactly:
{"title": "...", "objective": "...", "portfolio_summary": "...", "builds_on": ["skill", ...], "architecture": "...", "hardware": ["..."],
 "software": ["..."], "steps": [{"days": "J1-J2", "milestone": "...", "deliverable": "..."}],
 "success_criteria": ["..."], "pitfalls": ["..."], "interview_pitch": "..."}
Rules: French; every target skill is central and named; the project fits the context of the offer (its domain, its
products) when the offer says it; builds_on lists 2 to 5 skills taken ONLY from the candidate's skills;
cheap and available hardware (or simulation); 4 to 7 steps covering at most 10 working days (J1 to J10);
measurable success criteria; interview_pitch in 3-4 sentences, without claiming results that do not exist yet;
portfolio_summary: 1-2 sentences for a PUBLIC portfolio card describing what the project builds, never mentioning a
missing skill, learning or results not obtained yet."""
PUBLIC_FORBIDDEN = ("manquant", "lacune", "apprendre", "acquerir la competence", "monter en competence", "missing skill")


def favourable(r: dict, cfg: dict) -> bool:
    """Rules (1) and (2) of the trigger: green on everything else, 1..max_missing missing skills."""
    return (not r["eliminated"] and r["decision"] in ("keep", "review") and r["score"] >= cfg["min_score"]
            and 1 <= len(r["missing"]) <= cfg["max_missing"])


def window_gaps(rows: list[dict], cfg: dict) -> dict[str, list[str]]:
    """{skill: [offer ids]} over favourable offers (information: the skills most often missing)."""
    out: dict[str, list[str]] = {}
    for r in rows:
        if favourable(r, cfg):
            for s in r["missing"]:
                out.setdefault(s, []).append(r["offer_id"])
    return out


def offers_to_brief(rows: list[dict], cfg: dict, own: set[str], briefed_offers: set[str]) -> list[dict]:
    """The offers that get their own project (rules 1 to 3), best score first; one row per offer (its latest score)."""
    latest: dict[str, dict] = {}
    for r in rows:
        if r["offer_id"] not in latest or r.get("scored_at", "") >= latest[r["offer_id"]].get("scored_at", ""):
            latest[r["offer_id"]] = r
    out = [r for r in latest.values() if favourable(r, cfg) and r["offer_id"] not in briefed_offers
           and not any(fold(s) in own for s in r["missing"])]
    return sorted(out, key=lambda r: -r["score"])


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def _profile(self, ctx):
        p = Path(ctx.instance["profile_path"])
        return load_profile(p if p.is_absolute() else ROOT / p)

    def plan(self, ctx: RunContext):
        return [{"objective": "list the missing skills over the window"},
                {"objective": "one brief per favourable offer missing 1-2 skills"}]

    def _check(self, skills, own):
        def check(ans):
            need = ("title", "objective", "portfolio_summary", "builds_on", "architecture", "hardware", "software", "steps",
                    "success_criteria", "pitfalls", "interview_pitch")
            if not isinstance(ans, dict) or any(not ans.get(k) for k in need):
                return [f"every key is required: {list(need)}"]
            probs = []
            foreign = [b for b in ans["builds_on"] if fold(b) not in own]
            if foreign:
                probs.append(f"builds_on must only list candidate skills; remove {foreign}")
            days = [int(x) for st in ans["steps"] if isinstance(st, dict) for x in re.findall(r"\d+", str(st.get("days", "")))]
            if not days or max(days) > 10 or not 4 <= len(ans["steps"]) <= 7:
                probs.append("4 to 7 steps within J1..J10")
            # BRIEF_PUBLIC_SUMMARY (real case 2026-11-21: the public card said "Acquérir la compétence manquante...")
            bad = [w for w in PUBLIC_FORBIDDEN if w in fold(ans["portfolio_summary"])]
            if bad:
                probs.append(f"portfolio_summary is public: remove {bad} (describe what is built)")
            text = fold(json.dumps(ans, ensure_ascii=False))
            absent = [s for s in skills if fold(s) not in text]
            if absent:
                probs.append(f"every target skill must be central and named; missing: {absent}")
            return probs
        return check

    def _write(self, ctx, offer, skills, ans, window_days):
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.run_dir / f"brief_{offer['offer_id']}.md"
        where = " chez ".join(x for x in (offer["title"], offer.get("company")) if x)
        md = [f"# {ans['title']}", "", f"Offre visée : **{where}**" + (f" ({offer['location']})" if offer.get("location") else ""),
              "", f"Compétence(s) à acquérir : **{', '.join(skills)}** (score de l'offre : {offer['score']:.0f}/100)", "",
              "## Objectif", "", ans["objective"], "", f"S'appuie sur : {', '.join(ans['builds_on'])}", "",
              "## Architecture", "", ans["architecture"], "", "## Matériel", ""] + [f"- {h}" for h in ans["hardware"]] + [
              "", "## Logiciels", ""] + [f"- {x}" for x in ans["software"]] + ["", "## Étapes et jalons", ""] + [
              f"- **{st.get('days')}** : {st.get('milestone')} (livrable : {st.get('deliverable')})" for st in ans["steps"]] + [
              "", "## Critères de réussite", ""] + [f"- {x}" for x in ans["success_criteria"]] + ["", "## Pièges", ""] + [
              f"- {x}" for x in ans["pitfalls"]] + ["", "## Le présenter en entretien", "", ans["interview_pitch"]]
        if offer.get("url"):
            md += ["", f"Offre : {offer['url']}"]
        path.write_text("\n".join(md) + "\n", encoding="utf-8")
        return path

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("BRIEF_STRUCTURE", AGENT_ID, "toutes les sections, chaque compétence visée nommée, builds_on tiré de tes compétences, 10 "
     "jours ouvrés au plus",
                                 "rule of the user: a realistic 1-2 week project built on what you already know")
        cfg = ctx.config["gaps"]
        since = (datetime.now(timezone.utc) - timedelta(days=cfg["window_days"])).isoformat(timespec="seconds")
        today = datetime.now(timezone.utc).date().isoformat()
        profile = self._profile(ctx)
        own = {fold(s["name"]) for s in profile.get("skills") or []}
        own |= {fold(s) for sec in ("projects", "experiences", "education") for it in profile.get(sec) or []
                for b in it.get("bullets") or [] for s in b.get("skills") or []}
        own |= {fold(s) for s in profile.get("skills_written") or []}
        with ctx.connect() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT s.offer_id, s.score, s.missing, s.eliminated, s.scored_at, COALESCE(d.user_override, d.decision) "
                "decision FROM scores s JOIN experience_decisions d ON d.offer_id=s.offer_id AND d.profile_hash=s.profile_hash "
                "JOIN offers o ON o.offer_id=s.offer_id WHERE s.scored_at>=? AND o.status!='expired'", (since,))]
            briefed = {r[0] for r in c.execute("SELECT offer_id FROM project_briefs WHERE offer_id IS NOT NULL")}
        for r in rows:
            r["missing"] = json.loads(r["missing"])
        todo = offers_to_brief(rows, cfg, own, briefed)
        gaps = window_gaps(rows, cfg)
        wanted = {s for r in todo for s in r["missing"]}
        with ctx.connect() as c:
            # GAPS_SNAPSHOT (real case 2026-11-21: 'linux' stayed listed after it became known): today's rows are
            # rewritten from scratch at each analysis.
            c.execute("DELETE FROM skill_gaps WHERE window_end=?", (today,))
            for skill, offers in gaps.items():
                upsert(c, ctx.rows, "skill_gaps", {"skill": skill, "window_end": today, "offers_count": len(set(offers)),
                       "offer_ids": sorted(set(offers))[:50], "covered": fold(skill) in own, "triggered": skill in wanted,
                       "run_id": ctx.run_id})
        metrics = {"gaps": len(gaps), "offers_to_brief": len(todo), "briefs": 0}
        if not todo:
            return envelope(ctx, AGENT_ID, "succeeded", data={**metrics}, metrics=metrics)
        client = make_llm(ctx, AGENT_ID)
        written, alerts, warnings, outcomes = [], [], [], []
        for r in todo[:cfg["max_briefs_per_run"]]:
            with ctx.connect() as c:
                o = dict(c.execute("SELECT offer_id, title, company, location, url, description FROM offers WHERE offer_id=?",
                                   (r["offer_id"],)).fetchone())
            o["score"] = r["score"]
            skills = r["missing"]
            payload = {"target_skills": skills, "offer": {"title": o["title"], "company": o.get("company"),
                       "description": (o.get("description") or "")[:2000]},
                       "candidate_skills": sorted(own), "candidate_projects": [p["name"] for p in profile.get("projects") or []]}
            ans, probs, n = ask_with_corrections(client, BRIEF_PROMPT, payload, self._check(skills, own),
                                                 purpose="project_brief", ctx=ctx,
                                                 max_corrections=ctx.config["retries"]["llm_correction_max"])
            if not ans:
                outcomes.append({"check": "BRIEF_STRUCTURE", "item": o["offer_id"], "outcome": "kept_despite",
                                 "detail": "; ".join(probs)[:200]})
                warnings.append(f"brief for {o['title'][:60]} not written: {'; '.join(probs)[:120]}")
                continue
            brief_id = "b_" + sha(f"offer:{o['offer_id']}", 12)
            path = self._write(ctx, o, skills, ans, cfg["window_days"])
            with ctx.connect() as c:
                upsert(c, ctx.rows, "project_briefs", {"brief_id": brief_id, "skill": ", ".join(skills), "title": ans["title"],
                       "brief": ans, "path": str(path), "status": "proposed", "run_id": ctx.run_id, "created_at": utc_now(),
                       "offer_id": o["offer_id"]})
            outcomes.append({"check": "BRIEF_STRUCTURE", "item": o["offer_id"], "outcome": "corrected" if n else "pass"})
            written.append({"brief_id": brief_id, "offer_id": o["offer_id"], "skills": skills, "path": str(path)})
            alerts.append({"kind": "immediate", "category": "project", "ref": brief_id,
                           "message": f"Projet proposé pour « {o['title'][:70]} » ({', '.join(skills)}) : {ans['title']}"})
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        metrics["briefs"] = len(written)
        status = "succeeded" if written or not warnings else "partial"
        return envelope(ctx, AGENT_ID, status, artifact_refs=[w["path"] for w in written], warnings=warnings,
                        data={**metrics, "written": written, "alerts": alerts}, metrics=metrics)


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
