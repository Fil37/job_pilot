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
     "objective": "Compute a score from skills, level, location, salary and sector.",
     "summary": "Score = 100 x weighted sum of components in [0, 1] (weights of settings.json, versioned). "
                "Eliminations: avoided sector, excluded keyword, contract not accepted, salary below your minimum, "
                "outside your areas, experience rejected — each with its reason. Check SCORE_IS_WEIGHTED_SUM. "
                "Shown on the home page and the offer page (Scoring)."},
    {"skill_id": "SK_GAP_LIST", "version": "1", "status": "proposed",
     "objective": "List the requirements met and the missing ones for each offer.",
     "summary": "Skills detected in the offer (controlled lexicon) split into met (with your level) and missing. "
                "The missing lists feed the 30-day gap analysis of agent007."},
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


def score_offer(offer: dict, decision: str, terms: dict, profile: dict, criteria: dict, words: dict, scoring: dict) -> dict:
    text = fold(f"{offer['title']}\n{offer['description']}")
    title = fold(offer["title"])
    skills_p, aliases = profile["skills"], profile.get("aliases", {})
    req = offer_skills(text, words["skill_lexicon"], skills_p, aliases)
    matched = sorted(s for s in req if s in skills_p)
    missing = sorted(s for s in req if s not in skills_p)
    comp, notes, elim = {}, {}, []
    comp["skills"] = round(sum(LEVEL_WEIGHT[skills_p[s]] for s in matched) / len(req), 4) if req else 0.5
    notes["skills"] = f"{len(matched)}/{len(req)} skills" if req else "no skill detected (neutral)"
    comp["level"] = {"keep": 1.0, "review": 0.5, "reject": 0.0}[decision]
    notes["level"] = f"experience: {decision}"
    if decision == "reject":
        elim.append("EXPERIENCE_REJECTED")
    roles = [fold(r) for r in criteria.get("roles") or []]
    if any(contains_term(title, r) for r in roles):
        comp["title"], notes["title"] = 1.0, "one of your roles"
    elif any(contains_term(title, t) for t in terms["title"]):
        comp["title"], notes["title"] = 0.8, "neighbour title"
    else:
        comp["title"], notes["title"] = 0.2, "other title"
    loc = fold(offer.get("location") or "")
    zone = next((z["label"] for z in criteria.get("zones") or []
                 if any(contains_term(loc, k) for k in z.get("keywords", []))), None)
    remote = bool(REMOTE.search(text)) and criteria.get("remote_ok", False)
    foreign = next((f for f in words.get("foreign_locations", []) if contains_term(loc, f)), None)
    if zone:
        comp["location"], notes["location"] = 1.0, zone
    elif remote and scoring.get("remote_counts_as_in_zone", True):
        comp["location"], notes["location"] = 1.0, "full remote"
    elif criteria.get("mobility_france") and loc and not foreign:
        # MOBILITY_FRANCE (your CV 2026-11-21: "Mobilité France, dont Mulhouse"): anywhere in France is accepted,
        # your preferred areas score higher; a foreign location stays outside.
        comp["location"], notes["location"] = 0.8, "France (mobility)"
    elif not loc or loc in ("france", "fr", "remote"):
        comp["location"], notes["location"] = 0.5, "location unknown"
    else:
        comp["location"], notes["location"] = 0.0, f"outside your areas ({offer.get('location')})"
        elim.append("LOCATION_OUTSIDE")
    smin = criteria.get("salary_min_eur")
    top = offer.get("salary_max") or offer.get("salary_min")
    if top is None or not smin:
        comp["salary"], notes["salary"] = scoring.get("unknown_salary_value", 0.5), "salary unknown"
    elif top >= smin:
        comp["salary"], notes["salary"] = 1.0, f"up to {top:,.0f} EUR/year"
    else:
        comp["salary"], notes["salary"] = 0.0, f"up to {top:,.0f} EUR/year < {smin:,.0f}"
        elim.append("SALARY_BELOW_MIN")
    comp["sector"], notes["sector"] = 1.0, "no avoided sector"
    for ex in terms["exclusion"]:
        kind, _, rest = ex.partition(":")
        if kind == "sector":
            sector, _, kw = rest.partition(":")
            if contains_term(text, kw):
                comp["sector"], notes["sector"] = 0.0, f"avoided sector {sector} ('{kw}')"
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
    return {"score": total, "components": {"values": comp, "notes": notes, "weights": weights},
            "matched": [{"skill": s, "level": skills_p[s]} for s in matched], "missing": missing,
            "eliminated": bool(elim), "elimination_reasons": elim}


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
                "s.profile_hash=d.profile_hash AND s.weights_version=? AND json_extract(s.components, '$.notes.level') "
                "= 'experience: ' || COALESCE(d.user_override, d.decision))", (prof["profile_hash"], wv))]
        return [{"objective": f"score {len(ids[i:i + 100])} offers", "offer_ids": ids[i:i + 100]}
                for i in range(0, len(ids), 100)] or [{"objective": "nothing new to score", "offer_ids": []}]

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("SCORE_IS_WEIGHTED_SUM", AGENT_ID, "score = 100 x weighted sum of the components",
                                 "rule of the method: every displayed figure is computed by the program")
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
            outcomes = []
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
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        return envelope(ctx, AGENT_ID, "succeeded", data=counts, metrics=counts)


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
