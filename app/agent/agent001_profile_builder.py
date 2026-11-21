"""agent001 — profile builder.

Objective        : turn your profile source file and your criteria into a fingerprinted profile version and the
                   search terms used by the other agents (keywords, neighbour job titles, core skills, eliminatory
                   criteria).
Scope            : read app/instance/profile.yaml and the criteria of job_pilot.json; validate them; compute the
                   countable experience (months per kind); derive the terms.
Out of scope     : NEVER queries a job site (agent002), never decides on an offer (agent003/004), never writes a CV
                   (agent006), never edits your profile file (only you do).
Inputs           : profile file (YAML), criteria (job_pilot.json), words.json (title families, skill lexicon).
Outputs          : profile_versions row, search_terms rows; envelope data {profile_hash, counts, candidate_months}.
Tools            : PyYAML; no network; no model.
Data read        : profile file, job_pilot.json, words.json.
Data written     : tables profile_versions, search_terms (owner).
Success criteria : the profile passes PROFILE_* checks; at least one title and one keyword are derived.
Cost limits      : 0 model call.
Time limits      : < 2 s.
Error policy     : an invalid profile blocks the run (critical agent) with the list of problems; nothing is guessed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from app.agent import (ROOT, BaseAgent, RunContext, contains_term, envelope, fold, month_index, sha, upsert,
                       utc_now)

SKILLS = [
    {"skill_id": "SK_PROFILE_PARSE", "version": "1", "status": "proposed",
     "objective": "Validate the profile source file and fingerprint it.",
     "summary": "Checks required sections, unique ids, YYYY-MM dates, PROFILE_FIGURES_DECLARED (every number of a "
                "wording is a declared figure) and PROFILE_SKILL_DECLARED (every skill of a bullet is in skills). "
                "Counts months of internship / apprenticeship / job. Visible on the Supervision page (quality checks)."},
    {"skill_id": "SK_SEARCH_TERMS", "version": "1", "status": "proposed",
     "objective": "Derive keywords, neighbour job titles, core skills and eliminatory criteria from the profile.",
     "summary": "Titles = your roles + the title families of words.json; keywords = your skills and their aliases; "
                "core skills = level 'core' or proven by 2+ bullets; exclusions = avoided sectors, excluded "
                "keywords and contracts. Deterministic, no model. Terms are listed on the home page."},
]

AGENT_ID = "agent001"
KINDS = ("internship", "apprenticeship", "job", "freelance")
LEVELS = ("core", "working", "notions")
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def load_profile(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"profile file unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("profile file must be a mapping")
    return data


def all_bullets(profile: dict):
    for section in ("education", "experiences", "projects"):
        for item in profile.get(section) or []:
            for b in item.get("bullets") or []:
                yield section, item, b


def check_profile(profile: dict) -> tuple[list[str], list[dict]]:
    """Returns (blocking problems, named check outcomes)."""
    problems, outcomes = [], []
    for key in ("identity", "experiences", "skills"):
        if not profile.get(key):
            problems.append(f"section '{key}' missing or empty")
    skills = {fold(s.get("name", "")) for s in profile.get("skills") or [] if isinstance(s, dict)}
    for s in profile.get("skills") or []:
        if not isinstance(s, dict) or not s.get("name") or s.get("level") not in LEVELS:
            problems.append(f"skill {s!r}: needs a name and a level in {LEVELS}")
    ids = []
    for section in ("education", "experiences", "projects"):
        for item in profile.get(section) or []:
            ids.append(item.get("id"))
            for d in ("start", "end"):
                if d in item:
                    try:
                        month_index(str(item[d]))
                    except ValueError as exc:
                        problems.append(f"{item.get('id')}: {exc}")
            if section == "experiences" and item.get("kind") not in KINDS:
                problems.append(f"{item.get('id')}: kind must be one of {KINDS}")
    for _section, item, b in all_bullets(profile):
        ids.append(b.get("id"))
        if not b.get("variants"):
            problems.append(f"{b.get('id')}: at least one wording (variants) is required")
        declared = " ".join(str(f) for f in b.get("figures") or [])
        declared_numbers = set(NUMBER.findall(declared))
        for v in b.get("variants") or []:
            missing = [n for n in NUMBER.findall(v) if n not in declared_numbers]
            if missing:
                problems.append(f"{b.get('id')}: numbers {missing} are not declared in figures")
                outcomes.append({"check": "PROFILE_FIGURES_DECLARED", "outcome": "kept_despite", "item": b.get("id"),
                                 "detail": f"undeclared numbers {missing}"})
            else:
                outcomes.append({"check": "PROFILE_FIGURES_DECLARED", "outcome": "pass", "item": b.get("id")})
        unknown = [s for s in b.get("skills") or [] if fold(s) not in skills]
        if unknown:
            problems.append(f"{b.get('id')}: skills {unknown} are not in the skills section")
            outcomes.append({"check": "PROFILE_SKILL_DECLARED", "outcome": "kept_despite", "item": b.get("id"),
                             "detail": f"unknown skills {unknown}"})
        else:
            outcomes.append({"check": "PROFILE_SKILL_DECLARED", "outcome": "pass", "item": b.get("id")})
    dupes = sorted({i for i in ids if i and ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate ids: {dupes}")
    if any(i is None for i in ids):
        problems.append("every education / experience / project / bullet needs an id")
    return problems, outcomes


def candidate_months(profile: dict) -> dict:
    """Months of experience per kind (overlaps inside a kind counted once) and in total."""
    sets: dict[str, set[int]] = {k: set() for k in KINDS}
    for e in profile.get("experiences") or []:
        a, b = month_index(str(e["start"])), month_index(str(e.get("end", "present")))
        sets[e["kind"]].update(range(a, b + 1))
    out = {k: len(v) for k, v in sets.items()}
    out["total"] = len(set().union(*sets.values()))
    return out


def derive_terms(profile: dict, criteria: dict, words: dict) -> list[dict]:
    terms: dict[tuple[str, str], str] = {}

    def add(kind, value, origin):
        v = fold(value)
        if v:
            terms.setdefault((kind, v), origin)

    for r in criteria.get("roles") or []:
        add("title", r, "criteria.roles")
    families = words.get("title_families", {})
    for fam in criteria.get("title_families") or []:
        for t in families.get(fam, []):
            add("title", t, f"words.title_families.{fam}")
    for r in criteria.get("roles") or []:            # a role that belongs to a family brings its neighbours
        for fam, titles in families.items():
            if any(contains_term(fold(r), t) or contains_term(fold(t), fold(r)) for t in titles):
                for t in titles:
                    add("title", t, f"neighbour of '{r}' ({fam})")
    lexicon = words.get("skill_lexicon", {})
    proof_count: dict[str, int] = {}
    for _s, _i, b in all_bullets(profile):
        for s in b.get("skills") or []:
            proof_count[fold(s)] = proof_count.get(fold(s), 0) + 1
    for s in profile.get("skills") or []:
        name = fold(s["name"])
        add("keyword", name, "profile.skills")
        for alias in (s.get("aliases") or []) + lexicon.get(name, []):
            add("keyword", alias, f"alias of {name}")
        if s["level"] == "core" or proof_count.get(name, 0) >= 2:
            add("core_skill", name, "level core" if s["level"] == "core" else f"proven by {proof_count[name]} bullets")
    for sector, kws in (criteria.get("avoid_sectors") or {}).items():
        for k in kws:
            add("exclusion", f"sector:{sector}:{fold(k)}", "criteria.avoid_sectors")
    for k in criteria.get("excluded_keywords") or []:
        add("exclusion", f"keyword:{fold(k)}", "criteria.excluded_keywords")
    accepted = set(criteria.get("contracts") or [])
    for c in words.get("contract_labels", {}):
        if accepted and c not in accepted:
            add("exclusion", f"contract:{c.lower()}", "criteria.contracts")
    return [{"kind": k, "value": v, "origin": o} for (k, v), o in sorted(terms.items())]


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def profile_path(self, ctx: RunContext) -> Path:
        p = Path(ctx.params.get("profile_path") or ctx.instance["profile_path"])
        return p if p.is_absolute() else ROOT / p

    def validate_input(self, ctx: RunContext) -> list[str]:
        p = self.profile_path(ctx)
        if not p.is_file():
            return [f"profile file {p.name} missing: copy app/assets/profile.example.yaml to app/instance/profile.yaml"]
        if not ctx.instance.get("criteria", {}).get("roles"):
            return ["criteria.roles is empty in job_pilot.json"]
        return []

    def plan(self, ctx):
        return [{"objective": "validate and fingerprint the profile"}, {"objective": "derive the search terms"}]

    def work(self, ctx: RunContext, subtasks):
        path = self.profile_path(ctx)
        raw = path.read_text(encoding="utf-8")
        try:
            profile = load_profile(path)
        except ValueError as exc:
            return envelope(ctx, AGENT_ID, "failed", errors=[{"category": "missing_data", "message": str(exc)}])
        problems, outcomes = check_profile(profile)
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        if problems:
            return envelope(ctx, AGENT_ID, "failed", data={"problems": problems},
                            errors=[{"category": "missing_data", "message": p} for p in problems[:20]])
        warnings = []
        if profile.get("example_profile"):
            warnings.append("the profile is still the EXAMPLE file: results are illustrative only")
        months = candidate_months(profile)
        profile_hash = sha(raw)
        criteria = ctx.instance["criteria"]
        terms = derive_terms(profile, criteria, ctx.words)
        summary = {"experiences": len(profile.get("experiences") or []), "projects": len(profile.get("projects") or []),
                   "skills": {fold(sk["name"]): sk["level"] for sk in profile.get("skills") or []},
                   "aliases": {fold(sk["name"]): [fold(a) for a in sk.get("aliases") or []] for sk in profile.get("skills") or []},
                   "city": (profile.get("identity") or {}).get("city"), "example": bool(profile.get("example_profile")),
                   "criteria_hash": sha(repr(sorted(criteria.items())))}
        with ctx.connect() as c:
            upsert(c, ctx.rows, "profile_versions", {"profile_hash": profile_hash, "source_path": path.name,
                   "summary": summary, "candidate_months": months, "run_id": ctx.run_id, "created_at": utc_now()})
            c.execute("DELETE FROM search_terms WHERE profile_hash=?", (profile_hash,))
            for t in terms:
                upsert(c, ctx.rows, "search_terms", {"profile_hash": profile_hash, **t})
        counts = {k: sum(1 for t in terms if t["kind"] == k) for k in ctx.words["term_kinds"]}
        status = "succeeded" if counts["title"] and counts["keyword"] else "failed"
        errors = [] if status == "succeeded" else [{"category": "missing_data", "message": "no title or keyword derived"}]
        return envelope(ctx, AGENT_ID, status, warnings=warnings, errors=errors,
                        data={"profile_hash": profile_hash, "candidate_months": months, "term_counts": counts},
                        sources=[{"kind": "verified_fact", "ref": path.name}], metrics=counts)


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
