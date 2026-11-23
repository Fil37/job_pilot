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

import json
import re
import sys
from pathlib import Path


from app.agent.common_profile import (NUMBER, numbers, load_profile)  # noqa: F401 - shared, see common_profile.py
from app.agent import (ROOT, BaseAgent, RunContext, make_llm, contains_term, envelope, fold, month_index, sha, upsert,
                       utc_now)

SKILLS = [
    {"skill_id": "SK_PROFILE_PARSE", "version": "1", "status": "proposed",
     "objective": "Valider le fichier profil et calculer son empreinte.",
     "summary": "Vérifie les sections obligatoires, les identifiants uniques, les dates AAAA-MM, "
                "PROFILE_FIGURES_DECLARED (chaque nombre d'une formulation est un chiffre déclaré) et "
                "PROFILE_SKILL_DECLARED (chaque compétence d'une ligne figure dans skills). Compte les mois de stage, "
                "d'alternance et d'emploi. Visible sur la page Supervision (contrôles qualité)."},
    {"skill_id": "SK_SEARCH_TERMS", "version": "1", "status": "proposed",
     "objective": "Déduire du profil les mots-clés, les intitulés voisins, les compétences clés et les critères éliminatoires.",
     "summary": "Intitulés = tes postes visés + les familles d'intitulés de words.json ; mots-clés = tes compétences "
                "et leurs synonymes ; compétences clés = niveau « core » ou prouvées par au moins 2 lignes ; "
                "exclusions = secteurs évités, mots exclus et contrats refusés. Calcul déterministe, sans modèle."},
]

AGENT_ID = "agent001"
KINDS = ("internship", "apprenticeship", "job", "freelance")
LEVELS = ("core", "working", "notions")


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
                    value = str(item[d])
                    if section != "experiences" and re.fullmatch(r"\d{4}", value):
                        continue          # a year alone is enough outside experiences (no month invented)
                    try:
                        month_index(value)
                    except ValueError as exc:
                        problems.append(f"{item.get('id')}: {exc}")
            if section == "experiences" and item.get("kind") not in KINDS:
                problems.append(f"{item.get('id')}: kind must be one of {KINDS}")
            if section == "projects" and item.get("kind", "academic") not in ("academic", "personal"):
                problems.append(f"{item.get('id')}: project kind must be academic or personal")
    for _section, item, b in all_bullets(profile):
        ids.append(b.get("id"))
        if not b.get("variants"):
            problems.append(f"{b.get('id')}: at least one wording (variants) is required")
        declared_numbers = numbers(" ; ".join(str(f) for f in b.get("figures") or []))
        for v in b.get("variants") or []:
            missing = sorted(n for n in numbers(v) if n not in declared_numbers)
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
    # NEIGHBOURS_EXACT_ROLE (your criteria 2026-11-21: "embedded software engineer" contains "software engineer" and
    # brought the whole web-developer family): a role brings its neighbours only when it IS a title of the family.
    for r in criteria.get("roles") or []:
        for fam, titles in families.items():
            if fold(r) in {fold(t) for t in titles}:
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
        # SKILL_FROM_PROFILE_TEXT (real case 2026-11-21: "linux" counted missing although the CV says "Linux embarqué"
        # and "i.MX93 sous Linux"): a lexicon technology written in the profile counts as known (level 'working').
        text = fold(json.dumps({k: v for k, v in profile.items() if k != "identity"}, ensure_ascii=False))
        written = sorted(k for k, terms in ctx.words["skill_lexicon"].items() if any(contains_term(text, t) for t in terms))
        summary = {"experiences": len(profile.get("experiences") or []), "projects": len(profile.get("projects") or []),
                   "skills_written": written,
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


# =========================================================================== USER_ACTIONS (called by agent000.act)
IMPORT_PROMPT = """You read the text of a CV and put it, WORD FOR WORD, into this JSON structure (JSON only):
{"identity": {"full_name": "...", "city": "...", "country": "...", "mobility": "...", "availability": "...",
  "links": {"portfolio": "...", "github": "...", "linkedin": "..."}, "headline_variants": ["..."], "tagline_variants": ["..."]},
 "summary_variants": ["..."], "skill_groups": [{"label": "...", "text": "..."}],
 "education": [{"degree": "...", "school": "...", "country": "...", "start": "YYYY or YYYY-MM", "end": "...",
   "bullets": [{"text": "...", "skills": ["..."]}]}],
 "experiences": [{"kind": "internship|apprenticeship|job|freelance", "role": "...", "company": "...", "city": "...",
   "start": "YYYY-MM", "end": "YYYY-MM or present", "bullets": [{"text": "...", "skills": ["..."]}]}],
 "projects": [{"name": "...", "kind": "academic|personal", "start": "YYYY or YYYY-MM", "end": "...", "url": "...",
   "bullets": [{"text": "...", "skills": ["..."]}]}],
 "skills": [{"name": "...", "level": "core|working|notions"}], "languages": [{"name": "...", "level": "..."}],
 "certifications": ["..."]}
Rules: every text (headline, summary, bullets, skill groups) is COPIED from the CV, never rephrased, never invented;
leave a field "" or [] when the CV does not say it; dates as written in the CV converted to YYYY-MM (YYYY alone when the
month is not given); "present" for an ongoing experience; skills = the technologies and methods named in the CV, in
lower case; level "core" only for the skills the headline or the summary puts forward, "working" otherwise. The e-mail
and the phone are masked ([email], [phone]): do not output them."""
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RX = re.compile(r"(?:\+\d{1,3}[\s.]?)?(?:\(?0?\d\)?[\s.-]?)(?:\d{2}[\s.-]?){4}")


def _texts(ans: dict):
    """(where, sentence) of every text that must be written in the CV."""
    ident = ans.get("identity") or {}
    for t in ident.get("headline_variants") or []:
        yield "identity.headline", t
    for t in ans.get("summary_variants") or []:
        yield "summary", t
    for sec in ("education", "experiences", "projects"):
        for i, item in enumerate(ans.get(sec) or []):
            for j, b in enumerate(item.get("bullets") or []):
                yield f"{sec}[{i}].bullets[{j}]", b.get("text", "") if isinstance(b, dict) else str(b)


def draft_profile(ans: dict, contact: dict) -> dict:
    """The profile.yaml structure from the model's reading: ids, wordings (variants), figures and skills declared by
    the program so that the draft passes PROFILE_FIGURES_DECLARED and PROFILE_SKILL_DECLARED."""
    skills = {fold(s["name"]): s for s in ans.get("skills") or [] if isinstance(s, dict) and s.get("name")}
    out = {"identity": {**{k: v for k, v in (ans.get("identity") or {}).items() if v}, **{k: v for k, v in contact.items() if v}},
           "summary_variants": [t for t in ans.get("summary_variants") or [] if t]}
    if ans.get("skill_groups"):
        out["skill_groups"] = ans["skill_groups"]
    prefix = {"education": "edu", "experiences": "exp", "projects": "prj"}
    for sec in ("education", "experiences", "projects"):
        items = []
        for i, item in enumerate(ans.get(sec) or [], 1):
            it = {k: v for k, v in item.items() if k != "bullets" and v not in ("", None, [])}
            it["id"] = f"{prefix[sec]}_{i}"
            it["bullets"] = []
            for j, b in enumerate(item.get("bullets") or [], 1):
                text = (b.get("text") if isinstance(b, dict) else str(b)).strip()
                if not text:
                    continue
                bs = [fold(x) for x in (b.get("skills") if isinstance(b, dict) else []) or [] if x]
                for x in bs:
                    skills.setdefault(x, {"name": x, "level": "working"})
                it["bullets"].append({"id": f"{it['id']}_b{j}", "variants": [text], "skills": bs,
                                      "figures": sorted(numbers(text))})
            items.append(it)
        if items:
            out[sec] = items
    out["skills"] = [{"name": s["name"], "level": s.get("level") if s.get("level") in LEVELS else "working"}
                     for s in skills.values()]
    for k in ("languages", "certifications"):
        if ans.get(k):
            out[k] = ans[k]
    return out


def action_import_cv(orch, *, pdf: bytes) -> dict:
    """CV_IMPORT (first steps of a new user, 2026-11-23): a DRAFT profile from your CV PDF. Your e-mail and phone are
    masked before the model sees the text and put back by the program; every sentence must be written word for word
    in the CV (CV_IMPORT_VERBATIM: sent back to the model, then dropped). Nothing is used before you validate it."""
    import yaml
    from app.agent.common_llm import ask_with_corrections
    from app.agent.common_profile import pdf_bytes_to_text, squash, verbatim_in
    text = pdf_bytes_to_text(pdf)
    emails, phones = EMAIL_RX.findall(text), PHONE_RX.findall(text)
    masked = PHONE_RX.sub("[phone]", EMAIL_RX.sub("[email]", text))
    source = squash(text)
    run_id = orch.store.new_run("import du CV (brouillon de profil)", {"action": "import_cv"})
    ctx = orch._ctx(run_id, "t_import_cv", {})
    ctx.config = {**ctx.config, "llm": {**ctx.config["llm"], "max_tokens": 8000}}
    client = make_llm(ctx, AGENT_ID)

    def check(ans):
        if not isinstance(ans, dict) or not (ans.get("identity") or {}).get("full_name") or not ans.get("experiences") \
                and not ans.get("education"):
            return ["identity.full_name and at least experiences or education are required"]
        bad = [f"{where}: « {t[:80]} »" for where, t in _texts(ans) if t and not verbatim_in(t, source)]
        return [f"these texts are not written word for word in the CV, copy them exactly or remove them: {bad[:8]}"] \
            if bad else []
    try:
        ans, probs, _n = ask_with_corrections(client, IMPORT_PROMPT, {"cv_text": masked[:20000]}, check,
                                              purpose="cv_import", ctx=ctx,
                                              max_corrections=ctx.config["retries"]["llm_correction_max"])
    finally:
        orch.store.record_llm_calls([r for c in ctx.llm_clients for r in c.calls])
    if not ans and probs and probs[0].startswith("model unavailable"):
        orch.store.finish_run(run_id, "failed", {"summary": probs[0]})
        raise ValueError("The model is unavailable: check DEEPSEEK_API_KEY in your secrets.")
    dropped = []
    if not ans:                                   # last answer kept: the sentences not found in the CV are dropped
        raise ValueError("The CV could not be read reliably: " + "; ".join(probs)[:300])
    for sec in ("education", "experiences", "projects"):
        for item in ans.get(sec) or []:
            keep = [b for b in item.get("bullets") or [] if verbatim_in(b.get("text", "") if isinstance(b, dict) else str(b), source)]
            dropped += [b for b in item.get("bullets") or [] if b not in keep]
            item["bullets"] = keep
    contact = {"email": emails[0] if emails else "", "phone": phones[0].strip() if phones else ""}
    draft = draft_profile(ans, contact)
    problems, _o = check_profile(draft)
    header = ("# job_pilot - DRAFT of your profile, read from your CV on " + utc_now()[:10] + ".\n"
              "# Check every line (dates, kind of each experience, skill levels: core / working / notions),\n"
              "# then validate: only then is it used. Every sentence below is written word for word in your CV.\n")
    yaml_text = header + yaml.safe_dump(draft, allow_unicode=True, sort_keys=False, width=110)
    orch.instance_dir.mkdir(parents=True, exist_ok=True)
    (orch.instance_dir / "profile.draft.yaml").write_text(yaml_text, encoding="utf-8")
    orch.store.finish_run(run_id, "succeeded", {"summary": f"brouillon de profil : {len(draft.get('experiences') or [])} "
                                                           f"expérience(s), {len(draft['skills'])} compétence(s)"})
    return {"event": "user_cv_import", "detail": f"draft profile from the CV ({len(text)} characters)",
            "yaml": yaml_text, "problems": problems, "dropped": len(dropped)}


def action_save_profile(orch, *, yaml_text: str) -> dict:
    """You validate your profile: it must pass the same checks as always (sections, ids, dates, figures, skills).
    The previous profile is archived, never deleted. The example flag is refused."""
    import yaml
    try:
        prof = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML error: {str(exc)[:200]}") from None
    if not isinstance(prof, dict):
        raise ValueError("The profile must be a YAML mapping.")
    if prof.get("example_profile"):
        raise ValueError("Remove 'example_profile: true': the profile must hold your own data.")
    problems, _o = check_profile(prof)
    if problems:
        raise ValueError("Profile not valid: " + " ; ".join(problems[:6]))
    path = Path(orch.instance["profile_path"])
    path = path if path.is_absolute() else ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        archive = path.parent / "profile_archive"
        archive.mkdir(exist_ok=True)
        path.rename(archive / f"profile_{utc_now().replace(':', '')}.yaml")
    path.write_text(yaml_text, encoding="utf-8")
    return {"event": "user_profile_saved", "detail": f"profile saved ({len(prof.get('experiences') or [])} experience(s))"}


ACTIONS = {"import_cv": action_import_cv, "save_profile": action_save_profile}


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
