"""agent004 — offer scorer.

Objective        : score every offer that went through the experience filter, against your profile and criteria,
                   and list the requirements met and the missing ones.
Scope            : skills (lexicon of words.json + your skills), level (experience decision), title, location,
                   salary, sector; eliminatory criteria derived by agent001 (avoided sectors, excluded keywords,
                   contracts) and salary / location outside your criteria.
Out of scope     : NEVER re-decides experience (agent003), never researches the company (agent005), never changes
                   its own weights (a new version comes from agent011 and is applied only after your approval).
Inputs           : offers + experience_decisions of the current profile version, search_terms, criteria,
                   settings.scoring.
Outputs          : scores rows (score, components, matched, missing, eliminated + reasons); envelope data.
Tools            : arithmetic only; no model. The score is computed by the program, never generated.
Data read        : offers, experience_decisions (your override wins), profile_versions, search_terms, scoring_weights.
Data written     : scores, scoring_weights (first version from settings.json only).
Success criteria : 0 <= score <= 100; components between 0 and 1; check SCORE_IS_WEIGHTED_SUM passes.
Cost limits      : 0 model call.
Time limits      : < 1 s per 100 offers.
Error policy     : a missing value (salary, location) is 'unknown' with its neutral value, never guessed.
"""
from __future__ import annotations

import json
import re
import sys

from app.agent import BaseAgent, RunContext, contains_term, envelope, fold, upsert, utc_now

SKILLS = [
    {"skill_id": "SK_SCORE", "version": "1", "status": "proposed",
     "objective": "Calculer un score à partir des compétences, du niveau, du lieu, du salaire et du secteur.",
     "summary": "Score = 100 x somme pondérée de composantes entre 0 et 1 (poids de settings.json, versionnés). "
                "Éliminations : secteur évité, mot exclu, contrat refusé, salaire sous ton minimum, hors de France, "
                "expérience rejetée, chacune avec sa raison. Une offre prometteuse connue par un extrait t'est "
                "signalée. Contrôle SCORE_IS_WEIGHTED_SUM. Visible sur l'accueil et la page de l'offre."},
    {"skill_id": "SK_GAP_LIST", "version": "1", "status": "proposed",
     "objective": "Lister pour chaque offre les exigences remplies et les manques.",
     "summary": "Compétences repérées dans l'offre (lexique contrôlé), réparties entre remplies (avec ton niveau) et "
                "manquantes. Les manques servent aux projets proposés par agent007."},
]

AGENT_ID = "agent004"
LEVEL_WEIGHT = {"core": 1.0, "working": 0.8, "notions": 0.5}
REMOTE = re.compile(r"full remote|100 ?% (?:remote|teletravail)|teletravail (?:complet|total|integral)|fully remote|remote first")


def offer_skills(text: str, lexicon: dict, profile_skills: dict, aliases: dict) -> set[str]:
    found = set()
    for skill, terms in lexicon.items():
        if any(contains_term(text, t) for t in terms):
            found.add(skill)
    for skill in profile_skills:
        if skill not in lexicon and any(contains_term(text, t) for t in [skill] + aliases.get(skill, [])):
            found.add(skill)
    return found


ALT_JOIN = r"\s*(?:\(?\s*et\s*/?\s*ou\s*\)?|ou bien|ou|or|and/or|/)\s*(?:en\s+|le\s+|la\s+)?"


def skill_alternatives(text: str, skills: set[str], lexicon: dict, groups: list[list[str]] | None = None) -> dict[str, list[str]]:
    """SKILL_ALTERNATIVES (real case 2026-11-23: 14 offers say « VHDL et/ou Verilog »): {skill: [skills the offer accepts
    instead]} for the asked skills written side by side as « X ou Y », « X et/ou Y », « X/Y », « X or Y ».
    'text' is folded."""
    out: dict[str, set[str]] = {}
    names = {s: sorted({fold(t) for t in [s, *lexicon.get(s, [])]}, key=len, reverse=True) for s in skills}
    for a in skills:
        for b in skills:
            if a >= b or (groups is not None and not any(a in g and b in g for g in groups)):
                continue                       # only interchangeable tools (same group of words.json) replace each other
            for ta in names[a]:
                hit = False
                for tb in names[b]:
                    rx = (rf"(?<![a-z0-9]){re.escape(ta)}(?![a-z0-9]){ALT_JOIN}{re.escape(tb)}(?![a-z0-9])|"
                          rf"(?<![a-z0-9]){re.escape(tb)}(?![a-z0-9]){ALT_JOIN}{re.escape(ta)}(?![a-z0-9])")
                    if re.search(rx, text):
                        out.setdefault(a, set()).add(b)
                        out.setdefault(b, set()).add(a)
                        hit = True
                        break
                if hit:
                    break
    return {k: sorted(v) for k, v in out.items()}


def skill_label(skill: str, alternatives: dict | None) -> str:
    """'verilog ou vhdl' when the offer accepts either (SKILL_ALTERNATIVES), else the skill."""
    alts = (alternatives or {}).get(skill) or []
    return " ou ".join([skill, *alts])


def in_france(offer: dict, loc: str, criteria: dict, words: dict) -> bool:
    """FRANCE_POSITIVE (real case 2026-11-23: Arbeitnow offers in 'Hamburg' or 'Dortmund' would have counted as France
    because only a list of foreign places was checked): a place counts as France when the source only publishes French
    offers, or the place shows a département number, 'France', one of your zones or a known French city."""
    if offer.get("source_id") in words.get("france_only_sources", []):
        return True
    if re.search(r"(^|[\s,(])(\d{2}|2a|2b|97\d)\s*-\s", loc):
        return True
    places = list(words.get("french_places", [])) + [k for z in criteria.get("zones") or [] for k in z.get("keywords", [])
                                                     if not k.isdigit()]
    return any(contains_term(loc, fold(p)) for p in places)


def score_offer(offer: dict, decision: str, terms: dict, profile: dict, criteria: dict, words: dict, scoring: dict) -> dict:
    text = fold(f"{offer['title']}\n{offer['description']}")
    title = fold(offer["title"])
    skills_p, aliases = dict(profile["skills"]), profile.get("aliases", {})
    for k in profile.get("skills_written", []):            # SKILL_FROM_PROFILE_TEXT (agent001)
        skills_p.setdefault(k, "working")
    req = offer_skills(text, words["skill_lexicon"], skills_p, aliases)
    # SKILL_ALTERNATIVES (real case 2026-11-23: 14 offers say « VHDL et/ou Verilog » and Verilog was counted missing
    # although your VHDL is accepted): a skill the offer accepts INSTEAD of one you have is not a requirement.
    groups = (words.get("skill_alternative_groups") or {}).get("groups", [])
    alts = skill_alternatives(text, req, words["skill_lexicon"], groups) if groups else {}
    instead = {s: next(a for a in alts[s] if a in skills_p) for s in req
               if s not in skills_p and any(a in skills_p for a in alts.get(s, []))}
    req = req - set(instead)
    matched = sorted(s for s in req if s in skills_p)
    missing = sorted(s for s in req if s not in skills_p)
    comp, notes, elim = {}, {}, []
    comp["skills"] = round(sum(LEVEL_WEIGHT[skills_p[s]] for s in matched) / len(req), 4) if req else 0.5
    notes["skills"] = f"{len(matched)} compétence(s) sur {len(req)}" if req else "aucune compétence détectée (neutre)"
    if instead:
        notes["skills"] += " ; " + ", ".join(f"{a} accepté à la place de {s}" for s, a in sorted(instead.items()))
    comp["level"] = {"keep": 1.0, "review": 0.5, "reject": 0.0}[decision]
    notes["level"] = {"keep": "expérience compatible", "review": "expérience à vérifier", "reject": "expérience rejetée"}[decision]
    if decision == "reject":
        elim.append("EXPERIENCE_REJECTED")
    roles = [fold(r) for r in criteria.get("roles") or []]
    if any(contains_term(title, r) for r in roles):
        comp["title"], notes["title"] = 1.0, "un de tes postes visés"
    elif any(contains_term(title, t) for t in terms["title"]):
        comp["title"], notes["title"] = 0.8, "intitulé voisin"
    else:
        comp["title"], notes["title"] = 0.2, "autre intitulé"
    loc = fold(offer.get("location") or "")
    zone = next((z["label"] for z in criteria.get("zones") or []
                 if any(contains_term(loc, k) for k in z.get("keywords", []))), None)
    remote = bool(REMOTE.search(text)) and criteria.get("remote_ok", False)
    foreign = next((f for f in words.get("foreign_locations", []) if contains_term(loc, f)), None)
    if zone:
        comp["location"], notes["location"] = 1.0, zone
    elif remote and scoring.get("remote_counts_as_in_zone", True):
        comp["location"], notes["location"] = 1.0, "télétravail complet"
    elif criteria.get("mobility_france") and loc and not foreign and in_france(offer, loc, criteria, words):
        # MOBILITY_FRANCE (your CV 2026-11-21: "Mobilité France, dont Mulhouse"; your rule 2026-11-23: "je suis mobile
        # dans toute la France, ne restreins pas à Orsay"): anywhere in France scores like your zones; abroad stays out.
        comp["location"], notes["location"] = 1.0, "France (mobile partout)"
    elif not loc or loc in ("france", "fr", "remote"):
        comp["location"], notes["location"] = 0.5, "lieu non précisé"
    else:
        comp["location"], notes["location"] = 0.0, f"hors de tes zones ({offer.get('location')})"
        elim.append("LOCATION_OUTSIDE")
    smin = criteria.get("salary_min_eur")
    top = offer.get("salary_max") or offer.get("salary_min")
    if top is None or not smin:
        comp["salary"], notes["salary"] = scoring.get("unknown_salary_value", 0.5), "salaire non communiqué"
    elif top >= smin:
        comp["salary"], notes["salary"] = 1.0, f"jusqu'à {top:,.0f} € par an".replace(",", " ")
    else:
        comp["salary"], notes["salary"] = 0.0, f"jusqu'à {top:,.0f} € par an, sous ton minimum de {smin:,.0f} €".replace(",", " ")
        elim.append("SALARY_BELOW_MIN")
    comp["sector"], notes["sector"] = 1.0, "aucun secteur évité"
    for ex in terms["exclusion"]:
        kind, _, rest = ex.partition(":")
        if kind == "sector":
            sector, _, kw = rest.partition(":")
            if contains_term(text, kw):
                comp["sector"], notes["sector"] = 0.0, f"secteur évité : {sector} (« {kw} »)"
                if "AVOIDED_SECTOR" not in elim:
                    elim.append("AVOIDED_SECTOR")
        elif kind == "keyword" and contains_term(text, rest) and "EXCLUDED_KEYWORD" not in elim:
            elim.append("EXCLUDED_KEYWORD")
            notes["excluded_keyword"] = rest
        elif kind == "contract" and (offer.get("contract") or "").lower() == rest and "CONTRACT_EXCLUDED" not in elim:
            elim.append("CONTRACT_EXCLUDED")
            notes["contract"] = offer.get("contract")
    weights = scoring["weights"]
    total = round(100 * sum(weights[k] * comp[k] for k in weights), 1)
    return {"score": total, "components": {"values": comp, "notes": notes, "weights": weights, "decision": decision},
            "matched": [{"skill": s, "level": skills_p[s]} for s in matched], "missing": missing,
            "eliminated": bool(elim), "elimination_reasons": elim}


def project_gain(conn, offer_id: str, extra_skills: list[str], *, criteria: dict, words: dict, scoring: dict,
                 level: str = "working", upgrade: bool = False) -> dict | None:
    """PROJECT_GAIN (request of the user 2026-11-23: "le gain de % entre la version de CV optimale sans le projet et avec
    lui"): the same computation as the score, twice. 'Now' = your current profile, i.e. the best CV possible today (the
    CV check adds any profile skill the CV forgot); 'with' = your profile plus the project's skills at the 'working'
    level, as once the project is done and added to your profile. Estimation, computed by the program."""
    import copy
    prof = conn.execute("SELECT * FROM profile_versions ORDER BY created_at DESC LIMIT 1").fetchone()
    o = conn.execute("SELECT * FROM offers WHERE offer_id=?", (offer_id,)).fetchone()
    if not prof or not o:
        return None
    d = conn.execute("SELECT COALESCE(user_override, decision) FROM experience_decisions WHERE offer_id=? "
                     "ORDER BY profile_hash=? DESC, decided_at DESC LIMIT 1", (offer_id, prof["profile_hash"])).fetchone()
    decision = d[0] if d else "review"
    terms = {k: [] for k in words["term_kinds"]}
    for r in conn.execute("SELECT kind, value FROM search_terms WHERE profile_hash=?", (prof["profile_hash"],)):
        terms[r[0]].append(r[1])
    profile = json.loads(prof["summary"])
    now = score_offer(dict(o), decision, terms, profile, criteria, words, scoring)
    with_p = copy.deepcopy(profile)
    cur = with_p.get("skills", {})
    # a new skill at `level`; with upgrade=True (a reinforcement project) a skill you have is raised to `level`
    with_p["skills"] = {**cur, **{fold(x): level for x in extra_skills
                                  if fold(x) not in cur or (upgrade and LEVEL_WEIGHT[level] > LEVEL_WEIGHT[cur[fold(x)]])}}
    later = score_offer(dict(o), decision, terms, with_p, criteria, words, scoring)

    def cov(sc):
        n = len(sc["matched"]) + len(sc["missing"])
        return round(100 * len(sc["matched"]) / n) if n else 100
    return {"skills": list(extra_skills), "coverage_now": cov(now), "coverage_with": cov(later),
            "coverage_gain": cov(later) - cov(now), "score_now": now["score"], "score_with": later["score"],
            "score_gain": round(later["score"] - now["score"], 1), "still_missing": later["missing"],
            "eliminated": bool(later["eliminated"])}


def offers_gain(conn, offer_ids: list[str], skills: list[str], *, criteria: dict, words: dict, scoring: dict,
                level: str = "working", upgrade: bool = False) -> dict:
    """PROJECT_GAIN over several offers: how many offers the project improves and by how much (score points)."""
    per = []
    for oid in offer_ids:
        g = project_gain(conn, oid, skills, criteria=criteria, words=words, scoring=scoring, level=level, upgrade=upgrade)
        if g and g["score_gain"] > 0:
            t = conn.execute("SELECT title, company FROM offers WHERE offer_id=?", (oid,)).fetchone()
            per.append({"offer_id": oid, "title": t[0], "company": t[1], "score_now": g["score_now"],
                        "score_with": g["score_with"], "score_gain": g["score_gain"], "coverage_gain": g["coverage_gain"]})
    per.sort(key=lambda x: (-x["score_gain"], -x["score_with"]))
    return {"offers": len(per), "avg_score_gain": round(sum(x["score_gain"] for x in per) / len(per), 1) if per else 0.0,
            "max_score_gain": max((x["score_gain"] for x in per), default=0.0), "top": per[:10]}


def coverage_lines(cov: dict) -> list[str]:
    """French lines: the offers a project improves."""
    if not cov.get("offers"):
        return ["- Aucune autre offre favorable n'en profiterait pour l'instant."]
    out = [f"- Ce projet améliore **{cov['offers']} offre(s) favorable(s)** : +{cov['avg_score_gain']:.1f} points de score "
           f"en moyenne, jusqu'à +{cov['max_score_gain']:.0f}"]
    out += [f"- {x['title'][:70]}" + (f" · {x['company']}" if x.get("company") else "") +
            f" : {x['score_now']:.0f} → {x['score_with']:.0f}" for x in cov["top"]]
    if cov["offers"] > len(cov["top"]):
        out.append(f"- … et {cov['offers'] - len(cov['top'])} autre(s)")
    return out


def gain_lines(g: dict) -> list[str]:
    """French lines of the gain, for the brief, the offer page and the application package."""
    return [f"- Couverture des compétences de l'offre par ton CV optimal : **{g['coverage_now']} %** sans le projet, "
            f"**{g['coverage_with']} %** avec lui (**{g['coverage_gain']:+d} points**)",
            f"- Score de l'offre : {g['score_now']:.0f} sans le projet, {g['score_with']:.0f} avec lui "
            f"({g['score_gain']:+.0f} points)",
            "- Compétences encore manquantes après le projet : " + (", ".join(g["still_missing"]) or "aucune"),
            "- Estimation calculée par le programme : les compétences du projet comptées au niveau « pratique », "
            "une fois le projet réalisé et ajouté à ton profil."]


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def _profile(self, ctx):
        h = (ctx.previous.get("agent001") or {}).get("data", {}).get("profile_hash")
        with ctx.connect() as c:
            return (c.execute("SELECT * FROM profile_versions WHERE profile_hash=?", (h,)).fetchone() if h else
                    c.execute("SELECT * FROM profile_versions ORDER BY created_at DESC LIMIT 1").fetchone())

    def validate_input(self, ctx):
        return [] if self._profile(ctx) else ["no profile version: run agent001 first"]

    def plan(self, ctx):
        prof = self._profile(ctx)
        wv = ctx.config["scoring"]["weights_version"]
        with ctx.connect() as c:
            ids = [r[0] for r in c.execute(
                # not scored yet, or scored with another experience decision than the current one (your override)
                "SELECT d.offer_id FROM experience_decisions d JOIN offers o USING(offer_id) WHERE d.profile_hash=? "
                "AND o.status='new' AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.offer_id=d.offer_id AND "
                "s.profile_hash=d.profile_hash AND s.weights_version=? AND json_extract(s.components, '$.decision') "
                "= COALESCE(d.user_override, d.decision))", (prof["profile_hash"], wv))]
        return [{"objective": f"score {len(ids[i:i + 100])} offers", "offer_ids": ids[i:i + 100]}
                for i in range(0, len(ids), 100)] or [{"objective": "nothing new to score", "offer_ids": []}]

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("SCORE_IS_WEIGHTED_SUM", AGENT_ID, "score = 100 x somme pondérée des composantes",
                                 "rule of the method: every displayed figure is computed by the program")
        ctx.store.register_check("FRANCE_POSITIVE", AGENT_ID, "un lieu compte comme la France seulement s'il est reconnu (source uniquement française, numéro "
     "de département, « France », tes zones, grande ville française)",
                                 "2026-11-23: Arbeitnow offers in Hamburg or Dortmund would have scored 'France'")
        prof = self._profile(ctx)
        profile = json.loads(prof["summary"])
        scoring = ctx.config["scoring"]
        with ctx.connect() as c:
            if not c.execute("SELECT 1 FROM scoring_weights WHERE version=?", (scoring["weights_version"],)).fetchone():
                upsert(c, ctx.rows, "scoring_weights", {"version": scoring["weights_version"], "weights": scoring["weights"],
                                                        "origin": "settings.json", "created_at": utc_now()})
            stored = json.loads(c.execute("SELECT weights FROM scoring_weights WHERE version=?",
                                          (scoring["weights_version"],)).fetchone()[0])
            if stored != scoring["weights"]:
                return envelope(ctx, AGENT_ID, "failed", errors=[{"category": "misconfiguration", "message":
                                "weights changed without a new weights_version (settings.scoring)"}])
            terms = {k: [] for k in ctx.words["term_kinds"]}
            for r in c.execute("SELECT kind, value FROM search_terms WHERE profile_hash=?", (prof["profile_hash"],)):
                terms[r["kind"]].append(r["value"])
            counts = {"scored": 0, "eliminated": 0}
            outcomes, alerts = [], []
            for sub in subtasks:
                for oid in sub["offer_ids"]:
                    o = dict(c.execute("SELECT * FROM offers WHERE offer_id=?", (oid,)).fetchone())
                    d = c.execute("SELECT COALESCE(user_override, decision) FROM experience_decisions WHERE offer_id=? "
                                  "AND profile_hash=?",
                                  (oid, prof["profile_hash"])).fetchone()
                    decision = d[0]
                    s = score_offer(o, decision, terms, profile, ctx.instance["criteria"], ctx.words, scoring)
                    vals, w = s["components"]["values"], s["components"]["weights"]
                    ok = 0 <= s["score"] <= 100 and abs(s["score"] - round(100 * sum(w[k] * vals[k] for k in w), 1)) < 1e-9
                    outcomes.append({"check": "SCORE_IS_WEIGHTED_SUM", "outcome": "pass" if ok else "kept_despite", "item": oid})
                    upsert(c, ctx.rows, "scores", {"offer_id": oid, "profile_hash": prof["profile_hash"],
                           "weights_version": scoring["weights_version"], "run_id": ctx.run_id, "scored_at": utc_now(), **s})
                    counts["scored"] += 1
                    counts["eliminated"] += int(s["eliminated"])
                    # ASK_FULL_TEXT (your rule 2026-11-23: "si il y a une offre partielle j'irai moi-même chercher les
                    # détails ... donc venir me voir"): a promising offer known by an excerpt only is brought to you once.
                    if (o.get("description_complete") == 0 and not s["eliminated"] and decision in ("keep", "review")
                            and s["score"] >= ctx.config["gaps"]["min_score"] and not c.execute(
                                "SELECT 1 FROM alerts WHERE category='full_text' AND ref=?", (oid,)).fetchone()):
                        alerts.append({"kind": "immediate", "category": "full_text", "ref": oid,
                                       "message": f"Offre prometteuse ({s['score']:.0f}/100) mais incomplète : « {o['title'][:70]} »"
                                                  f"{' chez ' + o['company'] if o.get('company') else ''}. Colle son texte "
                                                  "complet sur sa page."})
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        counts["ask_full_text"] = len(alerts)
        return envelope(ctx, AGENT_ID, "succeeded", data={**counts, "alerts": alerts}, metrics=counts)


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
