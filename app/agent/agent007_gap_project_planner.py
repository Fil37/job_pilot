"""agent007 — gap project planner.

Objective        : aggregate over settings.gaps.window_days the skills missing in otherwise favourable offers and,
                   when the four trigger rules hold, design a realistic 1-2 week project brief from A to Z.
Scope            : trigger computed by the program: (1) the offer is green on everything else (not eliminated,
                   experience keep or review, score >= min_score), (2) only 1 or 2 skills are missing, (3) the skill
                   is missing in at least min_offers such offers, (4) it is not covered by your skills, your
                   projects or an existing brief. At most one new brief per run (cost and attention).
Out of scope     : NEVER adds the project to your CV or profile, never publishes it (agent008, after approval),
                   never decides the trigger with the model.
Inputs           : scores, offers, experience_decisions (window), profile file, project_briefs.
Outputs          : skill_gaps rows; project_briefs row + brief_<skill>.md in the run folder; an alert.
Tools            : arithmetic for the trigger; DeepSeek (JSON only, checked) for the brief.
Data read        : scores, offers, experience_decisions, profile file, project_briefs.
Data written     : skill_gaps, project_briefs (owner).
Success criteria : the brief has every section; 'builds_on' only lists skills of your profile; the steps fit in
                   10 working days (check BRIEF_STRUCTURE).
Cost limits      : 1 call per brief (+ up to 3 corrections).
Time limits      : settings.timeouts_s.agent_default.
Error policy     : no trigger → succeeded with nothing to do; model refused or wrong after 3 corrections → no brief,
                   the gap stays recorded and is retried at the next run.
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
    {"skill_id": "SK_GAP_AGGREGATE", "version": "1", "status": "proposed",
     "objective": "Aggregate missing skills over 30 days and apply the 4 trigger rules.",
     "summary": "Favourable offers only (not eliminated, experience keep/review, score >= min_score, 1-2 missing "
                "skills); a skill triggers when it is missing in >= min_offers of them and is covered neither by your "
                "profile nor by a brief. Computed by the program. Gaps listed on the home page."},
    {"skill_id": "SK_PROJECT_BRIEF", "version": "1", "status": "proposed",
     "objective": "Design a realistic 1-2 week project brief from A to Z.",
     "summary": "Objective, architecture, hardware and software, steps and milestones, success criteria, pitfalls, "
                "how to present it in an interview; builds on your skills (check BRIEF_STRUCTURE). Notified as an "
                "alert; the brief opens from the home page."},
]

AGENT_ID = "agent007"
BRIEF_PROMPT = """You design a realistic personal project of 1 to 2 weeks for a junior embedded / electronics engineer, to
learn ONE missing skill that recruiters ask for. Answer in JSON only, exactly:
{"title": "...", "objective": "...", "portfolio_summary": "...", "builds_on": ["skill", ...], "architecture": "...", "hardware": ["..."],
 "software": ["..."], "steps": [{"days": "J1-J2", "milestone": "...", "deliverable": "..."}],
 "success_criteria": ["..."], "pitfalls": ["..."], "interview_pitch": "..."}
Rules: French; the target skill is central; builds_on lists 2 to 5 skills taken ONLY from the candidate's skills;
cheap and available hardware (or simulation); 4 to 7 steps covering at most 10 working days (J1 to J10);
measurable success criteria; interview_pitch in 3-4 sentences, without claiming results that do not exist yet;
portfolio_summary: 1-2 sentences for a PUBLIC portfolio card describing what the project builds, never mentioning a
missing skill, learning or results not obtained yet."""
PUBLIC_FORBIDDEN = ("manquant", "lacune", "apprendre", "acquerir la competence", "monter en competence", "missing skill")


def window_gaps(rows: list[dict], cfg: dict) -> dict[str, list[str]]:
    """{skill: [offer ids]} over favourable offers with 1..max_missing missing skills (rules 1 to 3 inputs)."""
    out: dict[str, list[str]] = {}
    for r in rows:
        missing = r["missing"]
        if r["eliminated"] or r["decision"] not in ("keep", "review") or r["score"] < cfg["min_score"]:
            continue
        if not 1 <= len(missing) <= cfg["max_missing"]:
            continue
        for s in missing:
            out.setdefault(s, []).append(r["offer_id"])
    return out


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def _profile(self, ctx):
        p = Path(ctx.instance["profile_path"])
        return load_profile(p if p.is_absolute() else ROOT / p)

    def plan(self, ctx: RunContext):
        return [{"objective": "aggregate gaps over the window"}, {"objective": "brief for the first triggered skill"}]

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("BRIEF_STRUCTURE", AGENT_ID, "every section, builds_on from your skills, <= 10 working days",
                                 "rule of the user: a realistic 1-2 week project built on what you already know")
        cfg = ctx.config["gaps"]
        since = (datetime.now(timezone.utc) - timedelta(days=cfg["window_days"])).isoformat(timespec="seconds")
        today = datetime.now(timezone.utc).date().isoformat()
        profile = self._profile(ctx)
        own = {fold(s["name"]) for s in profile.get("skills") or []}
        own |= {fold(s) for sec in ("projects", "experiences", "education") for it in profile.get(sec) or []
                for b in it.get("bullets") or [] for s in b.get("skills") or []}
        with ctx.connect() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT s.offer_id, s.score, s.missing, s.eliminated, COALESCE(d.user_override, d.decision) decision "
                "FROM scores s JOIN experience_decisions d ON d.offer_id=s.offer_id AND d.profile_hash=s.profile_hash "
                "WHERE s.scored_at>=?", (since,))]
            briefed = {r[0] for r in c.execute("SELECT skill FROM project_briefs WHERE status!='dismissed'")}
        for r in rows:
            r["missing"] = json.loads(r["missing"])
        gaps = window_gaps(rows, cfg)
        triggered = []
        with ctx.connect() as c:
            # GAPS_SNAPSHOT (real case 2026-11-21: 'linux' stayed listed after it became known): today's rows are
            # rewritten from scratch at each analysis.
            c.execute("DELETE FROM skill_gaps WHERE window_end=?", (today,))
            for skill, offers in sorted(gaps.items(), key=lambda kv: -len(kv[1])):
                covered = skill in own or skill in briefed
                trig = len(offers) >= cfg["min_offers"] and not covered
                upsert(c, ctx.rows, "skill_gaps", {"skill": skill, "window_end": today, "offers_count": len(offers),
                       "offer_ids": offers[:50], "covered": covered, "triggered": trig, "run_id": ctx.run_id})
                if trig:
                    triggered.append((skill, offers))
        if not triggered:
            return envelope(ctx, AGENT_ID, "succeeded", data={"gaps": len(gaps), "triggered": 0},
                            metrics={"gaps": len(gaps), "triggered": 0})
        skill, offers = triggered[0]
        with ctx.connect() as c:
            titles = [r[0] for r in c.execute(f"SELECT title FROM offers WHERE offer_id IN ({','.join('?' * len(offers[:8]))})",
                                              offers[:8])]
        payload = {"target_skill": skill, "offers_asking_it": titles, "candidate_skills": sorted(own),
                   "candidate_projects": [p["name"] for p in profile.get("projects") or []]}

        def check(ans):
            need = ("title", "objective", "portfolio_summary", "builds_on", "architecture", "hardware", "software", "steps", "success_criteria",
                    "pitfalls", "interview_pitch")
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
            if fold(skill) not in fold(json.dumps(ans, ensure_ascii=False)):
                probs.append(f"the target skill '{skill}' must be central and named")
            return probs

        client = make_llm(ctx, AGENT_ID)
        ans, probs, n = ask_with_corrections(client, BRIEF_PROMPT, payload, check, purpose="project_brief", ctx=ctx,
                                             max_corrections=ctx.config["retries"]["llm_correction_max"])
        if not ans:
            ctx.store.record_outcomes(ctx.run_id, [{"check": "BRIEF_STRUCTURE", "item": skill, "outcome": "kept_despite",
                                                    "detail": "; ".join(probs)[:200]}])
            return envelope(ctx, AGENT_ID, "partial", warnings=[f"brief for {skill} not written: {'; '.join(probs)[:150]}"],
                            data={"gaps": len(gaps), "triggered": len(triggered)})
        brief_id = "b_" + sha(f"{skill}:{today}", 12)
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.run_dir / f"brief_{re.sub(r'[^a-z0-9]+', '_', fold(skill))}.md"
        md = [f"# {ans['title']}", "", f"Compétence visée : **{skill}**, demandée dans {len(offers)} offre(s) favorable(s) sur les "
              f"{cfg['window_days']} derniers jours", "", "## Objectif", "", ans["objective"], "",
              f"S'appuie sur : {', '.join(ans['builds_on'])}", "", "## Architecture", "", ans["architecture"], "",
              "## Matériel", ""] + [f"- {h}" for h in ans["hardware"]] + ["", "## Logiciels", ""] + [
              f"- {s}" for s in ans["software"]] + ["", "## Étapes et jalons", ""] + [
              f"- **{st.get('days')}** : {st.get('milestone')} (livrable : {st.get('deliverable')})" for st in ans["steps"]] + [
              "", "## Critères de réussite", ""] + [f"- {x}" for x in ans["success_criteria"]] + ["", "## Pièges", ""] + [
              f"- {x}" for x in ans["pitfalls"]] + ["", "## Le présenter en entretien", "", ans["interview_pitch"], "",
              "Offres concernées :"] + [f"- {t}" for t in titles]
        path.write_text("\n".join(md) + "\n", encoding="utf-8")
        with ctx.connect() as c:
            upsert(c, ctx.rows, "project_briefs", {"brief_id": brief_id, "skill": skill, "title": ans["title"], "brief": ans,
                   "path": str(path), "status": "proposed", "run_id": ctx.run_id, "created_at": utc_now()})
        ctx.store.record_outcomes(ctx.run_id, [{"check": "BRIEF_STRUCTURE", "item": skill,
                                                "outcome": "corrected" if n else "pass"}])
        return envelope(ctx, AGENT_ID, "succeeded", artifact_refs=[str(path)],
                        data={"gaps": len(gaps), "triggered": len(triggered), "brief_id": brief_id, "skill": skill,
                              "alerts": [{"kind": "immediate", "category": "project", "ref": brief_id,
                                          "message": f"Nouvelle idée de projet ({skill}) : {ans['title']}"}]},
                        metrics={"gaps": len(gaps), "triggered": len(triggered), "briefs": 1})


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
