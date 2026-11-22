"""agent006 — application builder.

Objective        : build the application package of ONE offer you retained: a one-page LaTeX CV tailored to the
                   offer (PDF), a cover letter when the offer asks for one, the offer link, the points of attention
                   and the company sheet; record the application as 'ready' in the tracking table.
Scope            : order the bullets and projects by relevance to the offer (computed), let the model propose the
                   headline, the tagline, the profile paragraph and the project order (ids only), check every
                   figure and technology against your profile file, compile, fit on one page, check the extracted
                   text (ATS).
Out of scope     : NEVER sends anything (you read, edit and send yourself), never writes a figure, technology or
                   experience that is not in app/instance/profile.yaml, never edits your profile, never scores or
                   filters offers (agents 003/004), never researches the company (agent005).
Inputs           : params.offer_id; profile file; app/assets/cv_template.tex (+ letter_template.tex); offer, score,
                   experience decision.
Outputs          : app/instance/runs/<run_id>/application_<offer_id>/ (cv.tex, cv.pdf, cv.txt, letter.*, package.md,
                   checks.json); applications row (status ready).
Tools            : DeepSeek (settings.llm, JSON only, at most 3 correction re-sends per text); Tectonic --untrusted
                   (no shell escape); pdfinfo / pdftotext.
Data read        : offers, scores, experience_decisions, profile file, templates.
Data written     : applications (owner), the package folder of this run.
Success criteria : CV_FACTS_FROM_PROFILE, CV_ONE_PAGE and CV_ATS_TEXT pass; LETTER_FACTS_FROM_PROFILE when a letter
                   is written.
Cost limits      : the run Budget; 1 call for the CV texts (+ up to 3 corrections), 1 for the letter (+ up to 3).
Time limits      : settings.latex.timeout_s per compilation, settings.latex.max_fit_iterations compilations.
Error policy     : model unavailable or still wrong after 3 corrections → the program uses your own wordings from the
                   profile (logged 'corrected'); compilation failure → failed (software_failure) with the log tail;
                   no page fit → partial with the reason, the PDF is kept for you to decide.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2

from app.agent import (ROOT, BaseAgent, RunContext, contains_term, decode_row, envelope, fold, make_llm, sha, upsert,
                       utc_now)
from app.agent import supervision as sv
from app.agent.agent001_profile_builder import load_profile, numbers
from app.agent.agent004_offer_scorer import offer_skills

SKILLS = [
    {"skill_id": "SK_CV_TAILOR", "version": "1", "status": "proposed",
     "objective": "Choose projects, bullets and skills, adapt the title and profile paragraph, compile the PDF.",
     "summary": "Bullets and projects ordered by the skills they share with the offer (computed). DeepSeek proposes "
                "headline, tagline, profile paragraph and project order (ids); the program checks them. Compiled with "
                "Tectonic --untrusted. Package listed on the offer page."},
    {"skill_id": "SK_CV_FACTCHECK", "version": "1", "status": "proposed",
     "objective": "Check that every figure and technology of the CV exists in the profile file.",
     "summary": "Check CV_FACTS_FROM_PROFILE: numbers and lexicon technologies of the whole CV text must appear in "
                "profile.yaml. A model text that fails is sent back with the defect and the allowed values (3 times "
                "at most), then replaced by your own wording."},
    {"skill_id": "SK_ATS_CHECK", "version": "1", "status": "proposed",
     "objective": "Check the CV fits on one page and its text is readable by screening software.",
     "summary": "Checks CV_ONE_PAGE (pdfinfo; tighter spacing, then fewer projects) and CV_ATS_TEXT (pdftotext finds "
                "your name, e-mail, every section and every bullet, no unreadable character)."},
    {"skill_id": "SK_COVER_LETTER", "version": "1", "status": "proposed",
     "objective": "Write the cover letter on the same principle when the offer asks for it.",
     "summary": "Written only when the offer mentions a cover letter (or on request); 3 short paragraphs from your "
                "profile facts; check LETTER_FACTS_FROM_PROFILE."},
]

AGENT_ID = "agent006"
CHECKS = [
    ("CV_FACTS_FROM_PROFILE", "every figure and technology of the CV is in profile.yaml", "rule of the user (no invented content)"),
    ("CV_ONE_PAGE", "the CV fits on one page", "rule of the user"),
    ("CV_ATS_TEXT", "the PDF text is extractable: name, e-mail, sections, every bullet", "rule of the user (ATS)"),
    ("LETTER_FACTS_FROM_PROFILE", "every figure and technology of the letter is in profile.yaml", "rule of the user"),
    ("SUMMARY_WORDS_FROM_PROFILE", "every meaningful word of the model's headline / tagline / summary is in profile.yaml",
     "2026-11-21 Alice & Bob CV: 'avec un souci de robustesse et de maintenabilité' added by the model"),
]
LABELS_FR = {"headline": "Titre", "tagline": "Accroche", "summary": "Résumé"}
SECTIONS = ("Compétences techniques", "Expériences professionnelles", "Projets", "Formation")
# PROJECT_KIND_LABEL (user request 2026-11-22): section "Projets"; a personal project shows "Personnel" next to its
# title, in the style of its stack; academic projects show nothing.
KIND_LABELS = {"academic": "", "personal": "Personnel"}
LATEX_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
                 "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


# =========================================================================== text helpers
def tex(text: str) -> str:
    """Escape for LaTeX (model and profile texts are data, never commands); **bold** → \\textbf{}."""
    out = "".join(LATEX_SPECIAL.get(ch, ch) for ch in str(text or ""))
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)


def plain(text: str) -> str:
    return str(text or "").replace("**", "")


def profile_corpus(profile: dict) -> str:
    """Every text of the profile: the only allowed source of figures and technologies."""
    parts = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (str, int, float)):
            parts.append(str(v))
    walk({k: v for k, v in profile.items() if k != "identity"})
    ident = profile.get("identity", {})
    parts += ident.get("headline_variants", []) + ident.get("tagline_variants", [])
    return plain(" \n ".join(parts))


def fact_problems(text: str, corpus: str, lexicon: dict) -> list[str]:
    """CV_FACTS_FROM_PROFILE: numbers and lexicon technologies of `text` that are absent from the profile corpus."""
    problems = []
    extra = sorted(numbers(text) - numbers(corpus))
    if extra:
        problems.append(f"figures not in the profile: {extra}")
    ft, fc = fold(plain(text)), fold(corpus)
    techs = sorted(k for k, terms in lexicon.items()
                   if any(contains_term(ft, t) for t in terms) and not any(contains_term(fc, t) for t in terms + [k]))
    if techs:
        problems.append(f"technologies not in the profile: {techs}")
    return problems


def novel_words(text: str, corpus: str) -> list[str]:
    """SUMMARY_WORDS_FROM_PROFILE (real case 2026-11-21, Alice & Bob CV: the model added "avec un souci de robustesse
    et de maintenabilité", a claim absent from your profile): every meaningful word (7+ letters) of a model text must
    exist in the profile (singular / plural ignored)."""
    def bag(t):
        return {w.rstrip("s") for w in re.findall(r"[a-z]{7,}", fold(plain(t)))}
    return sorted(bag(text) - bag(corpus))


def relevance(skills: list[str], req: set[str]) -> int:
    return sum(1 for s in skills if s in req)


# =========================================================================== content selection
def select_content(profile: dict, req: set[str]) -> dict:
    """Experiences keep their order (most recent first), bullets ordered by relevance; projects by relevance."""
    exps = []
    for e in profile.get("experiences") or []:
        bullets = sorted(e.get("bullets") or [], key=lambda b: -relevance(b.get("skills", []), req))
        exps.append({"id": e["id"], "role": e["role"], "company": e["company"], "city": e.get("city", ""),
                     "dates": e.get("dates_text") or f"{e.get('start')} – {e.get('end')}",
                     "bullets": [b["variants"][0] for b in bullets],
                     "score": sum(relevance(b.get("skills", []), req) for b in bullets)})
    projects = []
    for p in profile.get("projects") or []:
        skills = [s for b in p.get("bullets") or [] for s in b.get("skills", [])]
        projects.append({"id": p["id"], "name": p["name"], "tags": p.get("tags", ""),
                         "kind_label": KIND_LABELS.get(p.get("kind", "academic"), ""),
                         "bullets": [b["variants"][0] for b in p.get("bullets") or []], "score": relevance(skills, req),
                         "skills": sorted(set(skills))})
    projects.sort(key=lambda p: -p["score"])
    return {"experiences": exps, "projects": projects}


# =========================================================================== model texts
CV_PROMPT = """You tailor the header of a French engineering CV to a job offer. Answer in JSON only, exactly:
{"headline": "...", "tagline": "...", "summary": "...", "project_order": ["id", ...]}
Rules:
- headline: one line in French, at most 8 words, close to the offer's job title, built ONLY from the candidate's headline
  variants and skills (never claim a title or seniority the candidate does not have).
- tagline: at most 5 items separated by " · ", chosen ONLY among the candidate skills listed.
- summary: French, 45 to 75 words, rewritten from the candidate's summary to match the offer, using ONLY facts present in
  the candidate data. No new number, no technology that is not in the candidate skills, no company name of the offer.
- project_order: ALL the project ids given, most relevant to the offer first.
"""

LETTER_PROMPT = """You write the body of a French cover letter. Answer in JSON only, exactly:
{"paragraphs": ["...", "...", "..."]}
Rules: 3 paragraphs, 220 to 320 words in total, formal French ("vous"). Use ONLY facts present in the candidate data (no
new number, no technology or experience that is not listed). Paragraph 1: why this offer and company (from the offer
text only); 2: matching experiences and projects; 3: availability and wish for an interview. No greeting, no closing.
"""


FRENCH_STYLE = (" Write natural French typography: never use the long dash (—, –) nor English punctuation habits; "
                "use commas, colons or parentheses instead.")


def no_em_dash(value):
    """NO_EM_DASH (user request 2026-11-21: no English-style long dashes): the program fixes model texts itself."""
    if isinstance(value, str):
        v = value.replace(" — ", ", ").replace(" – ", ", ").replace("—", ", ").replace("–", "-")
        return v.replace(", ,", ",").replace(" ,", ",")
    if isinstance(value, list):
        return [no_em_dash(v) for v in value]
    if isinstance(value, dict):
        return {k: no_em_dash(v) for k, v in value.items()}
    return value


def ask_with_corrections(client, system: str, payload: dict, check, *, purpose: str, ctx: RunContext,
                         max_corrections: int) -> tuple[dict | None, list[str], int]:
    """One call + at most `max_corrections` re-sends naming the defect and the expected correction. Returns
    (answer or None, last problems, number of corrections)."""
    messages = [{"role": "system", "content": system + FRENCH_STYLE},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    problems: list[str] = []
    for attempt in range(max_corrections + 1):
        try:
            ans = client.chat_json(messages, purpose=purpose, run_id=ctx.run_id, task_id=ctx.task_id)
        except sv.LLMError as exc:
            return None, [f"model unavailable: {exc.category}"], attempt
        ans = no_em_dash(ans)
        problems = check(ans)
        if not problems:
            return ans, [], attempt
        messages += [{"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)},
                     {"role": "user", "content": "Defects: " + "; ".join(problems) + ". Correct them: remove anything "
                      "absent from the candidate data and keep only the allowed values. Answer the full JSON again."}]
    return None, problems, max_corrections


# =========================================================================== LaTeX
def jinja_env(folder: Path) -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(folder)), block_start_string=r"\BLOCK{",
                              block_end_string="}", variable_start_string=r"\VAR{", variable_end_string="}",
                              comment_start_string="%#{", comment_end_string="}", autoescape=False,
                              undefined=jinja2.StrictUndefined, keep_trailing_newline=True)


def engine(ctx: RunContext) -> str | None:
    p = os.path.expanduser(ctx.config["latex"]["engine_path"])
    return p if os.access(p, os.X_OK) else shutil.which("tectonic")


def compile_tex(ctx: RunContext, folder: Path, name: str) -> tuple[bool, str]:
    exe = engine(ctx)
    if not exe:
        return False, "tectonic not found"
    try:
        r = subprocess.run([exe, "-X", "compile", "--untrusted", f"{name}.tex"], cwd=folder, capture_output=True,
                           text=True, timeout=ctx.config["latex"]["timeout_s"])
    except subprocess.TimeoutExpired:
        return False, "compilation timeout"
    ok = r.returncode == 0 and (folder / f"{name}.pdf").is_file()
    return ok, sv.redact((r.stderr or "")[-1200:])


def pdf_pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else 0


def pdf_text(pdf: Path) -> str:
    return subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, text=True,
                          timeout=30).stdout


def ats_problems(text: str, must: list[str]) -> list[str]:
    """CV_ATS_TEXT: every expected piece is found in the extracted text (accents, spaces and hyphenation ignored)."""
    norm = re.sub(r"[^a-z0-9]+", "", fold(text.replace("-\n", "")))
    missing = [m for m in must if re.sub(r"[^a-z0-9]+", "", fold(plain(m)))[:60] not in norm]
    out = [f"not found in the PDF text: {m[:50]!r}" for m in missing]
    if "\ufffd" in text:
        out.append("unreadable characters in the PDF text")
    return out


def company_lines(ctx: RunContext, name: str) -> list[str]:
    """The company sheet of agent005 (each fact with its kind and source), or 'missing'."""
    from app.agent.agent005_company_researcher import company_key
    with ctx.connect() as c:
        row = c.execute("SELECT facts, match_confidence FROM companies WHERE company_key=?", (company_key(name),)).fetchone()
    if not row:
        return ["- information absente (pas de fiche pour cette entreprise)"]
    out = [] if row[1] >= 0.6 else [f"- Correspondance incertaine avec le registre ({row[1]}) : à vérifier"]
    sources = []
    for f in json.loads(row[0]):
        tag = {"verified_fact": "", "hypothesis": " (hypothèse)", "missing": " (absent)",
               "retrieval_error": " (erreur de récupération)"}.get(f["kind"], "")
        out.append(f"- {f['label']}{tag} : {f['value']}")
        if f["source"].startswith("http") and f["source"] not in sources:
            sources.append(f["source"])
    if sources:
        out.append("- Sources : " + " ; ".join(sources))
    return out


# =========================================================================== agent
class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def _profile_path(self, ctx):
        p = Path(ctx.instance["profile_path"])
        return p if p.is_absolute() else ROOT / p

    def _template(self, ctx, key):
        p = Path(ctx.config["cv"][key])
        return p if p.is_absolute() else ROOT / p

    def validate_input(self, ctx):
        oid = ctx.params.get("offer_id")
        if not oid:
            return ["params.offer_id is required (choose an offer on its page)"]
        with ctx.connect() as c:
            if not c.execute("SELECT 1 FROM offers WHERE offer_id=?", (oid,)).fetchone():
                return [f"unknown offer {oid}"]
        if not self._profile_path(ctx).is_file():
            return ["profile file missing"]
        if not self._template(ctx, "template").is_file():
            return ["CV template missing (app/assets/cv_template.tex)"]
        return []

    def plan(self, ctx):
        return [{"objective": o} for o in ("select content", "header texts (model + fact check)", "render and fit on one page",
                                           "ATS check", "cover letter if asked", "package and tracking row")]

    def healthcheck(self, ctx=None):
        if ctx is None:
            return "healthy", "not checked"
        if not engine(ctx):
            return "critical", "Tectonic not installed (settings.latex.engine_path)"
        if not shutil.which("pdftotext") or not shutil.which("pdfinfo"):
            return "critical", "poppler-utils (pdftotext, pdfinfo) missing"
        if not os.environ.get(ctx.config["llm"]["api_key_env"]):
            return "degraded", "no model key: your own wordings will be used"
        return "healthy", "tectonic, poppler and model key present"

    def work(self, ctx: RunContext, subtasks):
        for cid, name, origin in CHECKS:
            ctx.store.register_check(cid, AGENT_ID, name, origin)
        oid = ctx.params["offer_id"]
        rows, words = ctx.rows, ctx.words
        profile = load_profile(self._profile_path(ctx))
        corpus = profile_corpus(profile)
        lexicon = words["skill_lexicon"]
        with ctx.connect() as c:
            offer = dict(c.execute("SELECT * FROM offers WHERE offer_id=?", (oid,)).fetchone())
            score = c.execute("SELECT * FROM scores WHERE offer_id=? ORDER BY scored_at DESC LIMIT 1", (oid,)).fetchone()
            dec = c.execute("SELECT * FROM experience_decisions WHERE offer_id=? ORDER BY decided_at DESC LIMIT 1",
                            (oid,)).fetchone()
        score = decode_row(rows, "scores", score) if score else None
        dec = decode_row(rows, "experience_decisions", dec) if dec else None
        skills_p = {fold(s["name"]): s["level"] for s in profile.get("skills") or []}
        aliases = {fold(s["name"]): [fold(a) for a in s.get("aliases") or []] for s in profile.get("skills") or []}
        req = offer_skills(fold(offer["title"] + "\n" + offer["description"]), lexicon, skills_p, aliases)
        sel = select_content(profile, req)
        outcomes, warnings = [], []
        ident = profile["identity"]
        max_corr = ctx.config["retries"]["llm_correction_max"]

        # ---- header texts: model proposal, checked by the program
        header = {"headline": ident["headline_variants"][0], "tagline": ident.get("tagline_variants", [""])[0],
                  "summary": profile["summary_variants"][0]}
        project_ids = [p["id"] for p in sel["projects"]]
        client = make_llm(ctx, AGENT_ID)
        payload = {"offer_title": offer["title"],
                   "offer_text": offer["description"][:ctx.config["cv"]["offer_chars_to_model"]],
                   "candidate": {"headline_variants": ident["headline_variants"],
                                 "tagline_variants": ident.get("tagline_variants", []),
                                 "summary_variants": profile["summary_variants"], "skills": sorted(skills_p),
                                 "experiences": [{"role": e["role"], "bullets": [plain(b) for b in e["bullets"]]}
                                                 for e in sel["experiences"]],
                                 "projects": [{"id": p["id"], "name": p["name"], "skills": p["skills"]} for p in sel["projects"]]}}

        def check_header(ans):
            probs = []
            if not isinstance(ans, dict) or not all(isinstance(ans.get(k), str) and ans.get(k).strip()
                                                    for k in ("headline", "tagline", "summary")):
                return ["headline, tagline and summary must be non-empty strings"]
            if len(ans["headline"].split()) > 8:
                probs.append(f"headline has {len(ans['headline'].split())} words, at most 8")
            n = len(ans["summary"].split())
            if not 45 <= n <= 75:
                probs.append(f"summary has {n} words, expected 45 to 75")
            if fold(offer["company"]) and contains_term(fold(ans["summary"]), fold(offer["company"])):
                probs.append("the summary names the offer's company")
            if sorted(ans.get("project_order") or []) != sorted(project_ids):
                probs.append(f"project_order must contain exactly these ids: {project_ids}")
            for k in ("headline", "tagline", "summary"):
                probs += [f"{k}: {p}" for p in fact_problems(ans[k], corpus, lexicon)]
                extra = novel_words(ans[k], corpus)
                if extra:
                    probs.append(f"{k}: words absent from the candidate data {extra}: rephrase with the candidate's own words")
            return probs

        ans, probs, corrections = ask_with_corrections(client, CV_PROMPT, payload, check_header, purpose="cv_header",
                                                       ctx=ctx, max_corrections=max_corr)
        if ans:
            header.update({k: ans[k].strip() for k in ("headline", "tagline", "summary")})
            order = {pid: i for i, pid in enumerate(ans["project_order"])}
            sel["projects"].sort(key=lambda p: order[p["id"]])
            outcomes.append({"check": "CV_FACTS_FROM_PROFILE", "item": oid, "outcome": "corrected" if corrections else "pass",
                             "detail": f"header texts by the model after {corrections} correction(s)"})
        else:
            outcomes.append({"check": "CV_FACTS_FROM_PROFILE", "item": oid, "outcome": "corrected",
                             "detail": "your own wordings used: " + "; ".join(probs)[:300]})
            warnings.append("model header refused or unavailable, your own wordings were used: " + "; ".join(probs)[:200])

        # ---- render, compile, fit on one page
        folder = ctx.run_dir / f"application_{oid}"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        env = jinja_env(self._template(ctx, "template").parent)
        links = ident.get("links") or {}
        n_proj = min(ctx.config["cv"]["max_projects"], len(sel["projects"]))
        spacing = ctx.config["cv"]["spacing"]
        attempts, pages, log, ok = [], 0, "", False
        # CV_FIT_FEW_PROJECTS (test case 2026-11-21: a profile with 1 project and min_projects 3 gave no attempt at all)
        min_proj = min(ctx.config["cv"]["min_projects"], n_proj)
        plan_fit = [(s, n) for n in range(n_proj, min_proj - 1, -1) for s in range(len(spacing))]
        for s_i, n in plan_fit[:ctx.config["latex"]["max_fit_iterations"]]:
            projects = sel["projects"][:n]
            ctx_tex = {
                "name": tex(ident["full_name"]), "headline": tex(header["headline"]), "tagline": tex(header["tagline"]),
                "location_line": tex(" · ".join(x for x in (f"{ident.get('city', '')}, {ident.get('country', '')}".strip(", "),
                                                            ident.get("mobility"), ident.get("availability")) if x)),
                "phone": tex(ident.get("phone", "")), "phone_uri": re.sub(r"[^+0-9]", "", ident.get("phone", "")),
                "email": tex(ident.get("email", "")), "portfolio_url": links.get("portfolio", ""),
                "portfolio_label": tex(re.sub(r"^https?://|/$", "", links.get("portfolio", ""))),
                "github_url": links.get("github", ""), "linkedin_url": links.get("linkedin", ""),
                "summary": tex(header["summary"]),
                "skill_groups": [{"label": tex(g["label"]), "text": tex(g["text"])} for g in profile.get("skill_groups") or []],
                "experiences": [{**e, "role": tex(e["role"]), "company": tex(e["company"]), "city": tex(e["city"]),
                                 "dates": tex(e["dates"]), "bullets": [tex(b) for b in e["bullets"]]} for e in sel["experiences"]],
                "projects": [{"name": tex(p["name"]), "tags": tex(p["tags"]), "kind_label": tex(p["kind_label"]),
                              "bullets": [tex(b) for b in p["bullets"]]}
                             for p in projects],
                "education": [{"degree": tex(d["degree"]), "dates": tex(f"{d.get('start')} à {d.get('end')}"),
                               "school_line": tex(", ".join(x for x in (d.get("school"), d.get("country")) if x) + ". "
                                                  + " ".join(plain(b["variants"][0]) for b in d.get("bullets") or []))}
                              for d in profile.get("education") or []],
                "languages": tex(" ; ".join(f"{lang['name'].lower()} {lang['level']}" for lang in profile.get("languages") or [])),
                "certifications": tex(" ; ".join(profile.get("certifications") or [])),
                **spacing[s_i]}
            (folder / "cv.tex").write_text(env.get_template(self._template(ctx, "template").name).render(**ctx_tex),
                                           encoding="utf-8")
            ok, log = compile_tex(ctx, folder, "cv")
            if not ok:
                return envelope(ctx, AGENT_ID, "failed", errors=[{"category": "software_failure",
                                "message": f"CV compilation failed: {log[-300:]}"}], artifact_refs=[str(folder / "cv.tex")])
            pages = pdf_pages(folder / "cv.pdf")
            attempts.append({"projects": n, "spacing": spacing[s_i], "pages": pages})
            if pages == 1:
                break
        kept_projects = sel["projects"][:attempts[-1]["projects"]]
        outcomes.append({"check": "CV_ONE_PAGE", "item": oid, "outcome": "pass" if pages == 1 and len(attempts) == 1
                         else ("corrected" if pages == 1 else "kept_despite"), "detail": json.dumps(attempts)})

        # ---- ATS text and final fact check of the whole CV
        text = pdf_text(folder / "cv.pdf")
        (folder / "cv.txt").write_text(text, encoding="utf-8")
        must = [ident["full_name"], ident.get("email", ""), *SECTIONS,
                *[b for e in sel["experiences"] for b in e["bullets"]], *[b for p in kept_projects for b in p["bullets"]]]
        ats = ats_problems(text, must)
        outcomes.append({"check": "CV_ATS_TEXT", "item": oid, "outcome": "pass" if not ats else "kept_despite",
                         "detail": "; ".join(ats)[:300]})
        # whole PDF text vs the WHOLE profile (identity, dates and phone included). Test case 2026-11-21: removing an
        # empty phone with str.replace("", " ") split every number of the CV into single digits.
        full_corpus = corpus + " " + json.dumps(profile, ensure_ascii=False, default=str) + " " + " ".join(
            e["dates"] for e in sel["experiences"])
        facts = fact_problems(text, full_corpus, lexicon)
        outcomes.append({"check": "CV_FACTS_FROM_PROFILE", "item": oid, "outcome": "pass" if not facts else "kept_despite",
                         "detail": "whole PDF text: " + "; ".join(facts)[:300]})

        # ---- cover letter when the offer asks for it (or on request)
        letter_ref, letter_note = None, "non demandée par l'offre"
        asked = ctx.params.get("letter") or any(t in fold(offer["description"]) for t in
                                                 (fold(x) for x in ctx.config["cv"]["letter_triggers"]))
        if asked:
            letter_payload = {"offer_title": offer["title"], "company": offer["company"],
                              "offer_text": offer["description"][:ctx.config["cv"]["offer_chars_to_model"]],
                              "candidate": payload["candidate"] | {"summary": header["summary"]}}

            def check_letter(a):
                ps = a.get("paragraphs") if isinstance(a, dict) else None
                if not isinstance(ps, list) or len(ps) != 3 or not all(isinstance(p, str) and p.strip() for p in ps):
                    return ["paragraphs must be a list of 3 non-empty strings"]
                n = sum(len(p.split()) for p in ps)
                probs = [] if 220 <= n <= 320 else [f"{n} words, expected 220 to 320"]
                corpus_letter = corpus + " " + offer["description"]      # the offer may be quoted, nothing else
                return probs + fact_problems(" ".join(ps), corpus_letter, lexicon)

            la, lp, lc = ask_with_corrections(client, LETTER_PROMPT, letter_payload, check_letter, purpose="letter",
                                              ctx=ctx, max_corrections=max_corr)
            if la:
                lenv = jinja_env(self._template(ctx, "letter_template").parent)
                (folder / "letter.tex").write_text(lenv.get_template(self._template(ctx, "letter_template").name).render(
                    name=tex(ident["full_name"]), city=tex(ident.get("city", "")), phone=tex(ident.get("phone", "")),
                    email=tex(ident.get("email", "")), company=tex(offer["company"]), offer_title=tex(offer["title"]),
                    paragraphs=[tex(p) for p in la["paragraphs"]]), encoding="utf-8")
                (folder / "letter.txt").write_text("\n\n".join(la["paragraphs"]), encoding="utf-8")
                lok, llog = compile_tex(ctx, folder, "letter")
                letter_ref = str(folder / ("letter.pdf" if lok else "letter.txt"))
                letter_note = "rédigée" + ("" if lok else f" (échec du PDF : {llog[-120:]})")
                outcomes.append({"check": "LETTER_FACTS_FROM_PROFILE", "item": oid,
                                 "outcome": "corrected" if lc else "pass", "detail": f"{lc} correction(s)"})
            else:
                letter_note = "non rédigée : " + "; ".join(lp)[:200]
                warnings.append("cover letter asked but not written: " + "; ".join(lp)[:150])
                outcomes.append({"check": "LETTER_FACTS_FROM_PROFILE", "item": oid, "outcome": "kept_despite",
                                 "detail": letter_note})

        # ---- package and tracking row
        vigilance = []
        if score:
            if score["missing"]:
                vigilance.append(f"Compétences demandées absentes de ton profil : {', '.join(score['missing'])}.")
            if score["eliminated"]:
                fr = ctx.words["ui_text"]["fr"]
                why = [fr.get(ctx.words["elimination_reasons"].get(e, e), e) for e in score["elimination_reasons"]]
                vigilance.append("Offre éliminée par le score : " + " ".join(why))
            sal = score["components"]["notes"].get("salary")
            if sal == "salaire non communiqué":
                vigilance.append("Salaire non communiqué.")
        if dec:
            d = dec["user_override"] or dec["decision"]
            if d != "keep" or dec["reason_code"] not in ("NO_REQUIREMENT", "JUNIOR_SIGNAL", "YEARS_WITHIN_LIMIT"):
                verdict = {"keep": "compatible", "review": "à vérifier", "reject": "rejetée"}.get(d, d)
                vigilance.append(f"Expérience {verdict} : {dec['reason']}"
                                 + (f" « {dec['source_sentence']} »" if dec["source_sentence"] else ""))
        vigilance += [f"Contrôle {o['check']} non satisfait : {o['detail']}" for o in outcomes if o["outcome"] == "kept_despite"]
        changed = [k for k in ("headline", "tagline", "summary")
                   if header[k] != {"headline": ident["headline_variants"][0], "tagline": ident.get("tagline_variants", [""])[0],
                                    "summary": profile["summary_variants"][0]}[k]]
        md = [f"# {offer['title']} · {offer['company']}", "",
              f"- Offre : {offer['url']}", f"- Lieu : {offer.get('location') or '?'} · contrat : {offer.get('contract') or '?'}",
              f"- Score : {score['score']:.0f} sur 100" if score else "- Score : non calculé",
              f"- CV : {pages} page, texte lisible par les logiciels de tri", f"- Lettre de motivation : {letter_note}",
              "", "## Fiche entreprise", ""] + company_lines(ctx, offer["company"]) + ["",
              "## Points de vigilance", ""] + [f"- {v}" for v in vigilance or ["Aucun."]] + [
              "", "## Ce que le modèle a adapté (à relire)", ""] + (
              [f"- {LABELS_FR[k]} : {header[k]}" for k in changed] or ["- Rien : tes formulations d'origine."]) + [
              "", "## Projets retenus (ordre)", ""] + [f"- {p['name']}" for p in kept_projects] + [
              "", "## Et maintenant", "", "Rien n'a été envoyé. Relis le CV, modifie-le si besoin (fichier cv.tex), envoie ta "
              "candidature toi-même, puis clique sur « Marquer comme envoyée » sur la page de l'offre."]
        (folder / "package.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        (folder / "checks.json").write_text(json.dumps({"outcomes": outcomes, "fit_attempts": attempts}, ensure_ascii=False,
                                                       indent=1), encoding="utf-8")
        cv_sha = hashlib.sha256((folder / "cv.pdf").read_bytes()).hexdigest()
        with ctx.connect() as c:
            # ONE_READY_PACKAGE_PER_OFFER (case 2026-11-21: 5 'ready' rows for one offer after 5 preparations): a new
            # package replaces the one still 'ready'; an application already sent is never touched.
            prev = c.execute("SELECT application_id FROM applications WHERE offer_id=? AND status='ready'", (oid,)).fetchone()
            app_id = prev[0] if prev else "a_" + sha(f"{oid}:{ctx.run_id}", 14)
            upsert(c, rows, "applications", {"application_id": app_id, "offer_id": oid, "status": "ready",
                   "cv_version": ctx.run_id, "cv_sha256": cv_sha, "source_id": offer["source_id"],
                   "package_ref": str(folder.relative_to(ctx.run_dir.parent)), "run_id": ctx.run_id,
                   "created_at": utc_now(), "sent_at": None})
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        bad = [o for o in outcomes if o["outcome"] == "kept_despite"]
        status = "partial" if bad else "succeeded"
        arts = [str(folder / f) for f in ("cv.pdf", "cv.tex", "cv.txt", "package.md")] + ([letter_ref] if letter_ref else [])
        return envelope(ctx, AGENT_ID, status, warnings=warnings + [f"{o['check']}: {o['detail']}"[:200] for o in bad],
                        data={"application_id": app_id, "folder": str(folder), "pages": pages, "letter": letter_note,
                              "model_changed": changed, "projects": [p["id"] for p in kept_projects]},
                        artifact_refs=arts, metrics={"pages": pages, "fit_attempts": len(attempts),
                                                     "corrections": corrections, "kept_despite": len(bad)},
                        sources=[{"kind": "verified_fact", "ref": "profile.yaml"}, {"kind": "verified_fact", "ref": offer["url"]}])


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
