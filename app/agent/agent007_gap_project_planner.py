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

from app.agent.common_projects import (HW_KEYS, hw_item, image_problem, parse_done_on)  # noqa: F401 - shared, see common_projects.py
from app.agent import NotFound, ROOT, BaseAgent, RunContext, db_connect, envelope, fold, make_llm, sha, upsert, utc_now
from app.agent.common_profile import load_profile, own_skills
from app.agent.common_scoring import (coverage_lines, gain_lines, offer_skills, offers_gain, project_gain,
                                             skill_alternatives, skill_label)
from app.agent.common_llm import ask_with_corrections

SKILLS = [
    {"skill_id": "SK_GAP_AGGREGATE", "version": "3", "status": "proposed",
     "objective": "Choisir les projets à proposer : combler un manque, renforcer une compétence, élargir à une alternative.",
     "summary": "Combler : une offre favorable à laquelle il manque 1 ou 2 compétences (un projet par offre). Renforcer : une "
                "compétence que tu as en « pratique » demandée par au moins reinforce_min_offers offres favorables. "
                "Élargir : une compétence acceptée à la place d'une des tiennes (« VHDL ou Verilog ») par au moins "
                "broaden_min_offers offres. max_briefs_per_run par passage, dont une place réservée à un renforcement ou "
                "un élargissement quand il y en a (contrôle BRIEF_KINDS). Calculé par le programme."},
    {"skill_id": "SK_PROJECT_BRIEF", "version": "3", "status": "proposed",
     "objective": "Concevoir un brief de projet réaliste de 1 à 2 semaines selon son type (combler, renforcer, élargir).",
     "summary": "Objectif, architecture, matériel et logiciels, étapes et jalons, critères de réussite, pièges, comment le "
                "présenter en entretien ; nomme chaque compétence visée et s'appuie sur tes compétences (contrôle "
                "BRIEF_STRUCTURE). Liste les offres que le projet améliore, avec leur gain de score. Signalé par une "
                "alerte ; le brief s'ouvre depuis l'accueil et la page de l'offre."},
    {"skill_id": "SK_PROJECT_GAIN", "version": "1", "status": "proposed",
     "objective": "Chiffrer, pour l'offre visée, le gain entre ton CV optimal sans le projet et avec lui.",
     "summary": "Même calcul que le score, deux fois : ton profil actuel (le meilleur CV possible aujourd'hui), puis ton "
                "profil avec les compétences du projet au niveau « pratique ». Couverture des compétences de l'offre et "
                "score, avant et après, en points (PROJECT_GAIN). Dans le brief, sur la page de l'offre (recalculé à "
                "chaque affichage) et dans le dossier de candidature."},
]

AGENT_ID = "agent007"
BRIEF_PROMPT = """You design a realistic personal project of 1 to 2 weeks for a junior embedded / electronics engineer, so
that he can learn the skills ONE job offer asks for and he does not have yet. Answer in JSON only, exactly:
{"title": "...", "objective": "...", "portfolio_summary": "...", "builds_on": ["skill", ...], "architecture": "...",
 "hardware": [{"name": "...", "role": "...", "specs": "...", "example": "...", "alternative": "...", "optional": false}],
 "software": ["..."], "steps": [{"days": "J1-J2", "milestone": "...", "deliverable": "..."}],
 "success_criteria": ["..."], "pitfalls": ["..."], "interview_pitch": "..."}
Rules: French; every target skill is central and named; the project fits the context of the offer (its domain, its
products) when the offer says it; builds_on lists 2 to 5 skills taken ONLY from the candidate's skills;
cheap and available hardware (or simulation); every hardware item details: role = what it does in THIS project,
specs = the characteristics to check when choosing it (voltage, interface, pins, resolution...), example = one common
reference (a model name, never a price), alternative = a cheaper option or a simulation, optional = true if the
project works without it; 4 to 7 steps covering at most 10 working days (J1 to J10);
measurable success criteria; interview_pitch in 3-4 sentences, without claiming results that do not exist yet;
portfolio_summary: 1-2 sentences for a PUBLIC portfolio card describing what the project builds, never mentioning a
missing skill, learning or results not obtained yet.
"project_kind" says what the project is for: "gap" = learn target skills the candidate does not have (for one offer);
"reinforce" = the candidate already uses the target skill at a working level: take it to an advanced, demonstrable
level (harder topics, measurable results), for the many offers asking it ("offers_asking"); "broaden" = learn the
target skill that many offers accept instead of a skill the candidate knows (given in "known_alternative"): build on
that known skill.
When "accepted_alternatives" gives, for a target skill, other skills the offer accepts instead (the offer says
"VHDL ou Verilog", "VHDL et/ou Verilog"...), the project must be doable with EITHER: write the choice in the title
(e.g. "... en Verilog ou VHDL"), in the objective and in the steps, and say the candidate may use the one he knows."""
PUBLIC_FORBIDDEN = ("manquant", "lacune", "apprendre", "acquerir la competence", "monter en competence", "missing skill")


KIND_FR = {"gap": "combler un manque", "reinforce": "renforcement", "broaden": "élargissement"}

BRIEF_FIELDS = ("title", "objective", "portfolio_summary", "builds_on", "architecture", "hardware", "software", "steps",
                "success_criteria", "pitfalls", "interview_pitch")


def hw_line(h) -> str:
    """One Markdown line of the brief file for a hardware item."""
    d = hw_item(h)
    extra = " ; ".join(x for x in (d["role"], f"à vérifier : {d['specs']}" if d["specs"] else "",
                                   f"exemple : {d['example']}" if d["example"] else "",
                                   f"alternative : {d['alternative']}" if d["alternative"] else "") if x)
    return f"- **{d['name']}**" + (" (facultatif)" if d["optional"] else "") + (f" : {extra}" if extra else "")


def public_summary_problems(text: str) -> list[str]:
    """BRIEF_PUBLIC_SUMMARY: words that must not appear on the public portfolio card."""
    return [w for w in PUBLIC_FORBIDDEN if w in fold(text or "")]


def brief_markdown(offer: dict | None, skills: list[str], ans: dict) -> str:
    """The brief file (also rewritten when you edit the project on its page)."""
    md = [f"# {ans['title']}", ""]
    if offer:
        where = " chez ".join(x for x in (offer.get("title"), offer.get("company")) if x)
        md += [f"Offre visée : **{where}**" + (f" ({offer['location']})" if offer.get("location") else ""), ""]
    score = f" (score de l'offre : {offer['score']:.0f}/100)" if offer and offer.get("score") is not None else ""
    alts = ans.get("alternatives") or {}
    labels = ", ".join(skill_label(x, alts) for x in skills)
    choice = " (l'offre accepte l'une ou l'autre : fais-le dans celle que tu préfères)" if alts else ""
    kind = ans.get("kind", "gap")
    md += [f"Type : **{KIND_FR[kind]}**" + {
        "gap": "",
        "reinforce": f", ta compétence {labels} passe de « pratique » à « maîtrise »",
        "broaden": (f", {labels} est accepté à la place de {ans.get('known_alternative')} par "
                    f"{ans.get('choice_offers', 0)} offre(s) (ton {ans.get('known_alternative')} y suffit déjà) et exigé "
                    f"seul par {ans.get('required_offers', 0)} offre(s)")}[kind], ""]
    md += [f"Compétence(s) visée(s) : **{labels}**{choice}{score}", "",
           *(["## Gain attendu pour cette offre", "", *gain_lines(ans["gain"]), ""] if ans.get("gain") else []),
           *(["## Offres couvertes par ce projet", "", *coverage_lines(ans["coverage"]), ""] if ans.get("coverage") else []),
           "## Objectif", "", ans.get("objective", ""), "", f"S'appuie sur : {', '.join(ans.get('builds_on') or [])}", "",
           "## Architecture", "", ans.get("architecture", ""), "", "## Matériel", ""] + [hw_line(h) for h in ans.get("hardware") or []] + [
           "", "## Logiciels", ""] + [f"- {x}" for x in ans.get("software") or []] + ["", "## Étapes et jalons", ""] + [
           f"- **{st.get('days')}** : {st.get('milestone')}" + (f" (livrable : {st.get('deliverable')})" if st.get("deliverable")
                                                                else "") for st in ans.get("steps") or []] + [
           "", "## Critères de réussite", ""] + [f"- {x}" for x in ans.get("success_criteria") or []] + ["", "## Pièges", ""] + [
           f"- {x}" for x in ans.get("pitfalls") or []] + ["", "## Le présenter en entretien", "", ans.get("interview_pitch", "")]
    if ans.get("portfolio_summary"):
        md += ["", "## Résumé public (carte du portfolio)", "", ans["portfolio_summary"]]
    if offer and offer.get("url"):
        md += ["", f"Offre : {offer['url']}"]
    if ans.get("done_on"):
        md += ["", f"Réalisé en : {ans['done_on']} (date donnée par toi)."]
    if ans.get("edited_at"):
        md += ["", f"Modifié par toi le {ans['edited_at'][:16].replace('T', ' ')}."]
    return "\n".join(md) + "\n"


HARDWARE_PROMPT = """For each hardware item of this junior electronics project, give its details. Answer in JSON only:
{"items": [{"role": "...", "specs": "...", "example": "...", "alternative": "...", "optional": false}]}, one object per
item, SAME order and SAME count as "hardware". French. role = what it does in THIS project; specs = what to check when
choosing it (voltage, interface, pins, resolution...); example = one common reference (a model name, never a price);
alternative = a cheaper option or a simulation; optional = true if the project works without it."""


def detail_hardware(client, ctx, brief: dict) -> list[dict] | None:
    """HARDWARE_DETAIL for an older brief: the model details each item; the NAMES stay exactly yours / the brief's."""
    names = [hw_item(h)["name"] for h in brief.get("hardware") or []]
    if not names:
        return None
    payload = {"title": brief.get("title"), "architecture": brief.get("architecture"), "hardware": names}

    def check(ans):
        items = ans.get("items") if isinstance(ans, dict) else None
        if not isinstance(items, list) or len(items) != len(names):
            return [f"'items' must have exactly {len(names)} objects, in the order of 'hardware'"]
        bad = [i for i, x in enumerate(items) if not isinstance(x, dict) or not all(
            isinstance(x.get(k), str) and x.get(k).strip() for k in ("role", "specs"))]
        return [f"items {bad}: role and specs must be non-empty"] if bad else []
    ans, _probs, _n = ask_with_corrections(client, HARDWARE_PROMPT, payload, check, purpose="hardware_detail", ctx=ctx,
                                           max_corrections=ctx.config["retries"]["llm_correction_max"])
    if not ans:
        return None
    return [hw_item({**x, "name": n}) for n, x in zip(names, ans["items"])]


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


def green(r: dict, cfg: dict) -> bool:
    """A favourable offer: not eliminated, experience keep or review, score >= min_score."""
    return not r["eliminated"] and r["decision"] in ("keep", "review") and r["score"] >= cfg["min_score"]


def latest_rows(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        if r["offer_id"] not in out or r.get("scored_at", "") >= out[r["offer_id"]].get("scored_at", ""):
            out[r["offer_id"]] = r
    return list(out.values())


def reinforce_candidates(rows: list[dict], cfg: dict, done: set[str]) -> list[tuple[str, list[str]]]:
    """[(skill, offer ids)]: skills you have at the 'working' level asked by >= reinforce_min_offers favourable offers,
    most asked first, without a reinforcement brief yet."""
    count: dict[str, list[str]] = {}
    for r in latest_rows(rows):
        if green(r, cfg):
            for m in r.get("matched") or []:
                if m.get("level") == "working":
                    count.setdefault(m["skill"], []).append(r["offer_id"])
    return sorted(((k, v) for k, v in count.items() if len(v) >= cfg.get("reinforce_min_offers", 10) and k not in done),
                  key=lambda kv: -len(kv[1]))


def broaden_candidates(rows: list[dict], cfg: dict, own: set[str], lexicon: dict, groups: list, done: set[str]):
    """[(skill, known alternative, offers accepting it instead, offers requiring it)]: a skill many favourable offers
    accept INSTEAD of one you know (« VHDL ou Verilog »), most accepted first, without a broadening brief yet."""
    choice: dict[str, list[str]] = {}
    known: dict[str, str] = {}
    required: dict[str, list[str]] = {}
    for r in latest_rows(rows):
        if not green(r, cfg):
            continue
        for x in r["missing"]:
            required.setdefault(x, []).append(r["offer_id"])
        text = fold(r.get("text") or "")
        req = offer_skills(text, lexicon, {x: "working" for x in own}, {})
        for x, alts in skill_alternatives(text, req, lexicon, groups).items():
            k = next((a for a in alts if a in own), None)
            if x not in own and k:
                choice.setdefault(x, []).append(r["offer_id"])
                known[x] = k
    return sorted(((x, known[x], v, required.get(x, [])) for x, v in choice.items()
                   if len(v) >= cfg.get("broaden_min_offers", 5) and x not in done), key=lambda t: -len(t[2]))


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

    def _check(self, skills, own, alternatives=None):
        def check(ans):
            need = ("title", "objective", "portfolio_summary", "builds_on", "architecture", "hardware", "software", "steps",
                    "success_criteria", "pitfalls", "interview_pitch")
            if not isinstance(ans, dict) or any(not ans.get(k) for k in need):
                return [f"every key is required: {list(need)}"]
            probs = []
            hw_bad = [h for h in ans["hardware"] if not isinstance(h, dict) or not all(
                isinstance(h.get(k), str) and h.get(k).strip() for k in ("name", "role", "specs"))]
            if hw_bad:
                probs.append("every hardware item is an object with non-empty name, role and specs (plus example, "
                             f"alternative, optional): fix {len(hw_bad)} item(s)")
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
            # SKILL_ALTERNATIVES: the offer accepts either; the title says the choice
            for s, alts in (alternatives or {}).items():
                need = [x for x in [s, *alts] if fold(x) not in fold(ans.get("title", ""))]
                if need:
                    probs.append(f"the offer accepts {' or '.join([s, *alts])}: the title must name the choice "
                                 f"(e.g. '... en {s} ou {alts[0]}'); missing in the title: {need}")
            return probs
        return check

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("BRIEF_STRUCTURE", AGENT_ID, "toutes les sections, chaque compétence visée nommée, builds_on tiré de tes compétences, 10 "
     "jours ouvrés au plus",
                                 "rule of the user: a realistic 1-2 week project built on what you already know")
        cfg = ctx.config["gaps"]
        since = (datetime.now(timezone.utc) - timedelta(days=cfg["window_days"])).isoformat(timespec="seconds")
        today = datetime.now(timezone.utc).date().isoformat()
        profile = self._profile(ctx)
        own = own_skills(profile)
        with ctx.connect() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT s.offer_id, s.score, s.missing, s.matched, s.eliminated, s.scored_at, COALESCE(d.user_override, "
                "d.decision) decision, o.title || char(10) || o.description AS text FROM scores s JOIN experience_decisions d "
                "ON d.offer_id=s.offer_id AND d.profile_hash=s.profile_hash "
                "JOIN offers o ON o.offer_id=s.offer_id WHERE s.scored_at>=? AND o.status!='expired'", (since,))]
            briefed = {r[0] for r in c.execute("SELECT offer_id FROM project_briefs WHERE offer_id IS NOT NULL "
                                               "AND status!='superseded'")}
            done_kind = {(k, sk) for k, sk in c.execute(
                "SELECT json_extract(brief, '$.kind'), skill FROM project_briefs WHERE status!='superseded'")}
        for r in rows:
            r["missing"] = json.loads(r["missing"])
            r["matched"] = json.loads(r["matched"] or "[]")
        todo = offers_to_brief(rows, cfg, own, briefed)
        if ctx.params.get("offer_ids"):              # a regeneration asked for given offers only
            todo = [r for r in todo if r["offer_id"] in set(ctx.params["offer_ids"])]
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
        lexicon, groups = ctx.words["skill_lexicon"], ctx.words["skill_alternative_groups"]["groups"]
        reinf = [] if ctx.params.get("offer_ids") else reinforce_candidates(rows, cfg, {sk for k, sk in done_kind
                                                                                       if k == "reinforce"})
        broad = [] if ctx.params.get("offer_ids") else broaden_candidates(rows, cfg, own, lexicon, groups,
                                                                          {sk for k, sk in done_kind if k == "broaden"})
        # BRIEF_KINDS (request of the user 2026-11-23): 'gap' projects first, but one slot a day goes to a
        # reinforcement or a broadening project when there is one (alternately), so they are not starved by the queue
        n = int(ctx.params.get("max_briefs") or cfg["max_briefs_per_run"])
        extras = [{"kind": "reinforce", "skills": [k], "offers": v} for k, v in reinf] + \
                 [{"kind": "broaden", "skills": [x], "known": kn, "offers": ch, "required": rq} for x, kn, ch, rq in broad]
        if reinf and broad and datetime.now(timezone.utc).toordinal() % 2:
            extras.sort(key=lambda j: j["kind"] != "broaden")
        gap_jobs = [{"kind": "gap", "skills": r["missing"], "row": r} for r in todo]
        jobs = (extras[:1] + gap_jobs[:max(0, n - 1)]) if extras else gap_jobs[:n]
        rest = [j for j in extras[1:] + gap_jobs if j not in jobs]
        jobs += rest[:max(0, n - len(jobs))]
        metrics = {"gaps": len(gaps), "offers_to_brief": len(todo), "reinforce_candidates": len(reinf),
                   "broaden_candidates": len(broad), "briefs": 0}
        if not jobs:
            return envelope(ctx, AGENT_ID, "succeeded", data={**metrics}, metrics=metrics)
        client = make_llm(ctx, AGENT_ID)
        written, alerts, warnings, outcomes = [], [], [], []
        gain_args = {"criteria": ctx.instance["criteria"], "words": ctx.words, "scoring": ctx.config["scoring"]}
        for job in jobs:
            skills, kind, o, alternatives = job["skills"], job["kind"], None, {}
            if kind == "gap":
                r = job["row"]
                with ctx.connect() as c:
                    o = dict(c.execute("SELECT offer_id, title, company, location, url, description FROM offers WHERE "
                                       "offer_id=?", (r["offer_id"],)).fetchone())
                o["score"] = r["score"]
                text = fold(o["title"] + "\n" + (o.get("description") or ""))
                req = offer_skills(text, lexicon, {x: "working" for x in own}, {})
                alt_all = skill_alternatives(text, req | set(skills), lexicon, groups)
                alternatives = {x: alt_all[x] for x in skills if alt_all.get(x)}
                payload = {"project_kind": "gap", "target_skills": skills, "accepted_alternatives": alternatives,
                           "offer": {"title": o["title"], "company": o.get("company"),
                                     "description": (o.get("description") or "")[:2000]}}
                item, brief_id = o["offer_id"], "b_" + sha(f"offer:{o['offer_id']}", 12)
            else:
                with ctx.connect() as c:
                    titles = [t for (t,) in c.execute(
                        f"SELECT title FROM offers WHERE offer_id IN ({','.join('?' * len(job['offers'][:8]))})",
                        job["offers"][:8])]
                payload = {"project_kind": kind, "target_skills": skills, "offers_asking": titles,
                           **({"known_alternative": job["known"]} if kind == "broaden" else {})}
                item, brief_id = f"{kind}:{skills[0]}", "b_" + sha(f"{kind}:{skills[0]}", 12)
            payload.update({"candidate_skills": sorted(own),
                            "candidate_projects": [p["name"] for p in profile.get("projects") or []]})
            ans, probs, nc = ask_with_corrections(client, BRIEF_PROMPT, payload, self._check(skills, own, alternatives),
                                                  purpose="project_brief", ctx=ctx,
                                                  max_corrections=ctx.config["retries"]["llm_correction_max"])
            if not ans:
                outcomes.append({"check": "BRIEF_STRUCTURE", "item": item, "outcome": "kept_despite",
                                 "detail": "; ".join(probs)[:200]})
                warnings.append(f"brief {item} not written: {'; '.join(probs)[:120]}")
                continue
            ans = {**ans, "kind": kind, "hardware": [hw_item(h) for h in ans["hardware"]]}
            with ctx.connect() as c:
                if kind == "gap":
                    gain = project_gain(c, o["offer_id"], skills, **gain_args)
                    if gain:
                        ans["gain"] = gain
                    others = [x["offer_id"] for x in latest_rows(rows) if green(x, cfg) and x["offer_id"] != o["offer_id"]
                              and set(skills) & set(x["missing"])]
                    ans["coverage"] = offers_gain(c, others, skills, **gain_args)
                elif kind == "reinforce":
                    ans["coverage"] = offers_gain(c, job["offers"], skills, level="core", upgrade=True, **gain_args)
                else:
                    ans["coverage"] = offers_gain(c, job["required"], skills, **gain_args)
                    ans.update({"known_alternative": job["known"], "choice_offers": len(job["offers"]),
                                "required_offers": len(job["required"])})
            if alternatives:
                ans["alternatives"] = alternatives
            ctx.run_dir.mkdir(parents=True, exist_ok=True)
            path = ctx.run_dir / f"brief_{o['offer_id'] if o else brief_id}.md"
            path.write_text(brief_markdown(o, skills, ans), encoding="utf-8")
            with ctx.connect() as c:
                upsert(c, ctx.rows, "project_briefs", {"brief_id": brief_id, "skill": ", ".join(skills), "title": ans["title"],
                       "brief": ans, "path": str(path), "status": "proposed", "run_id": ctx.run_id, "created_at": utc_now(),
                       "offer_id": o["offer_id"] if o else None})
            outcomes.append({"check": "BRIEF_STRUCTURE", "item": item, "outcome": "corrected" if nc else "pass"})
            written.append({"brief_id": brief_id, "kind": kind, "offer_id": o["offer_id"] if o else None, "skills": skills,
                            "path": str(path)})
            what = (f"pour « {o['title'][:70]} »" if o else
                    f"({KIND_FR[kind]}, {ans['coverage']['offers']} offre(s) améliorée(s))")
            alerts.append({"kind": "immediate", "category": "project", "ref": brief_id,
                           "message": f"Projet proposé {what} ({', '.join(skill_label(x, alternatives) for x in skills)}) : "
                                      f"{ans['title']}"})
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        metrics["briefs"] = len(written)
        status = "succeeded" if written or not warnings else "partial"
        return envelope(ctx, AGENT_ID, status, artifact_refs=[w["path"] for w in written], warnings=warnings,
                        data={**metrics, "written": written, "alerts": alerts}, metrics=metrics)


# =========================================================================== USER_ACTIONS (called by agent000.act)
LIST_FIELDS = ("software", "success_criteria", "pitfalls")


def _load_brief(orch, brief_id: str):
    """(row, offer or None, file path) of a brief; the file must be inside the runs folder."""
    with db_connect(orch.db_path) as c:
        row = c.execute("SELECT * FROM project_briefs WHERE brief_id=?", (brief_id,)).fetchone()
        if not row:
            raise NotFound(brief_id)
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
    if Path(orch.runs_dir).resolve() not in path.parents:
        path = None                                  # a file outside the runs folder is never written
    return b, offer, path


def _save_brief(orch, b: dict, offer, path: Path | None, ans: dict, title: str | None = None) -> None:
    if path:
        path.write_text(brief_markdown(offer, [x.strip() for x in b["skill"].split(",")], ans), encoding="utf-8")
    with db_connect(orch.db_path) as c:
        c.execute("UPDATE project_briefs SET title=?, brief=? WHERE brief_id=?",
                  (title or b["title"], json.dumps(ans, ensure_ascii=False), b["brief_id"]))


def hw_from_line(line: str) -> dict:
    """'name | role | specs | example | alternative | facultatif' (edit form) → a hardware item."""
    parts = [x.strip() for x in line.split("|")] + [""] * 6
    return {"name": parts[0], "role": parts[1], "specs": parts[2], "example": parts[3], "alternative": parts[4],
            "optional": fold(parts[5]).startswith("facultatif")}


def action_edit_brief(orch, *, brief_id: str, form: dict) -> dict:
    """You edit the project: every field is yours; only the public summary keeps its rule (BRIEF_PUBLIC_SUMMARY, it goes
    on your public portfolio). The brief file is rewritten."""
    b, offer, path = _load_brief(orch, brief_id)
    keys = ("title", "objective", "portfolio_summary", "architecture", "interview_pitch", "builds_on", "steps", "hardware",
            *LIST_FIELDS, "done_on")
    f = {k: (form.get(k) or "").replace("\r\n", "\n").strip() for k in keys}
    if not f["title"] or not f["objective"] or any(len(v) > 20000 for v in f.values()):
        raise ValueError("A title and an objective are required.")
    try:
        done_on = parse_done_on(f["done_on"]) if f["done_on"] else None
    except ValueError:
        raise ValueError("Give the real date of the project (not in the future).") from None
    bad = public_summary_problems(f["portfolio_summary"])
    if bad:
        raise ValueError("The public summary must describe what is built: remove " + ", ".join(bad))
    steps = []
    for line in f["steps"].split("\n"):
        if line.strip():
            parts = [x.strip() for x in line.split("|")] + ["", ""]
            steps.append({"days": parts[0], "milestone": parts[1], "deliverable": parts[2]})
    ans = {**b["brief"], "title": f["title"], "objective": f["objective"], "portfolio_summary": f["portfolio_summary"],
           "architecture": f["architecture"], "interview_pitch": f["interview_pitch"],
           "builds_on": [x.strip() for x in f["builds_on"].split(",") if x.strip()], "steps": steps,
           **{k: [x.strip() for x in f[k].split("\n") if x.strip()] for k in LIST_FIELDS},
           "hardware": [hw_from_line(x) for x in f["hardware"].split("\n") if x.strip()], "edited_at": utc_now(),
           "done_on": done_on}
    _save_brief(orch, b, offer, path, ans, f["title"])
    return {"event": "user_brief_edit", "detail": f"{brief_id}: edited by you"}


def action_set_done_on(orch, *, brief_id: str, done_on: str) -> dict:
    """REAL_PROJECT_DATE: the date YOU give for when the project was really done."""
    b, offer, path = _load_brief(orch, brief_id)
    try:
        real = parse_done_on(done_on)
    except ValueError:
        raise ValueError("Give the real date of the project (not in the future).") from None
    _save_brief(orch, b, offer, path, {**b["brief"], "done_on": real})
    return {"event": "user_project_date", "detail": f"{brief_id}: done on {done_on}"}


def action_save_project_sheet(orch, *, brief_id: str, form: dict, categories: list[str], figures: list[tuple[str, str]],
                              image: tuple[str, bytes] | None = None, remove_image: bool = False) -> dict:
    """CARD_LIKE_YOURS: the 'Projet réalisé' sheet YOU fill (what you really built, your figures, your link, your image);
    REAL_PROJECT_DATE and IMAGE_SAFE apply. Nothing is published here (agent008, after your approval)."""
    b, offer, path = _load_brief(orch, brief_id)
    keys = ("short_name", "domain", "context", "title", "summary", "details_title", "details", "tags", "link_url",
            "link_label", "image_alt", "position", "done_on")
    f = {k: (form.get(k) or "").replace("\r\n", "\n").strip() for k in keys}
    if not (f["short_name"] and f["title"] and f["summary"]) or any(len(v) > 6000 for v in f.values()):
        raise ValueError("A short name, a title and a summary are required.")
    try:
        done_on = parse_done_on(f["done_on"])
    except ValueError:
        raise ValueError("Give the real date of the project (not in the future).") from None
    if f["link_url"] and not re.fullmatch(r"https://[^\s\"'<>]+", f["link_url"]):
        raise ValueError("The link must start with https://")
    cats = [x for x in categories if x in orch.config["portfolio"].get("categories", {})]
    if not cats:
        raise ValueError("Choose at least one category of your portfolio.")
    if not (f["position"] in ("first", "last") or f["position"].startswith("after:")):
        f["position"] = "first"
    card = {**{k: f[k] for k in ("short_name", "domain", "context", "title", "summary", "details_title", "details",
                                 "link_url", "link_label", "image_alt", "position")},
            "tags": [x.strip() for x in f["tags"].split(",") if x.strip()][:14], "categories": cats,
            "figures": [{"value": v.strip()[:24], "label": (lab or "").strip()[:100]} for v, lab in figures[:3] if (v or "").strip()],
            "image_file": (b["brief"].get("card") or {}).get("image_file")}
    if image and image[0]:
        name, data = image
        bad = image_problem(name, data, orch.config["portfolio"].get("image_max_bytes", 2_000_000))
        if bad:
            raise ValueError(bad)
        if not f["image_alt"]:
            raise ValueError("Describe the image (alternative text) for people who cannot see it.")
        folder = Path(orch.runs_dir) / "portfolio_assets"
        (folder / "archive").mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{brief_id}.*"):              # archived, never deleted
            old.rename(folder / "archive" / f"{old.stem}_{utc_now().replace(':', '')}{old.suffix}")
        dest = folder / f"{brief_id}{Path(name).suffix.lower()}"
        dest.write_bytes(data)
        card["image_file"] = str(dest)
    if remove_image:
        card["image_file"] = None
    _save_brief(orch, b, offer, path, {**b["brief"], "card": card, "done_on": done_on})
    return {"event": "user_project_done", "detail": f"{brief_id}: 'Projet réalisé' sheet saved"}


ACTIONS = {"edit_brief": action_edit_brief, "set_done_on": action_set_done_on,
           "save_project_sheet": action_save_project_sheet}


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
