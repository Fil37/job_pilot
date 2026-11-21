"""agent003 — experience filter.

Objective        : decide, for every new offer, keep / reject / review on the experience criterion by reading the
                   WHOLE description (not the site's label), with the reason and the source sentence.
Scope            : extract the years required, detect "hors stage" / "hors alternance", junior / beginner wording,
                   senior wording, contradictions (text vs text, label vs text); compare with your countable
                   experience and your rule.
Out of scope     : NEVER scores skills, salary or location (agent004), never deletes an offer (a rejected offer
                   stays in the database with its reason), never overrides a decision you took yourself.
Inputs           : offers (status new) without a decision for the current profile version; profile_versions
                   (candidate months); criteria.experience_rule; settings.experience.
Outputs          : experience_decisions rows; envelope data {keep, reject, review}.
Tools            : rules (regular expressions over the accent-free text); no model in version 1.
Data read        : offers, profile_versions, experience_decisions (user overrides).
Data written     : experience_decisions (owner).
Success criteria : every decision has a reason code of words.json; every 'years' decision quotes a sentence that
                   really is in the description (check DECISION_QUOTES_SOURCE).
Cost limits      : 0 model call.
Time limits      : < 1 s per 100 offers.
Error policy     : an unclear case goes to review, never to the bin; an offer that cannot be parsed goes to review.
"""
from __future__ import annotations

import re
import sys

from app.agent import BaseAgent, RunContext, envelope, fold, sentences, upsert, utc_now

SKILLS = [
    {"skill_id": "SK_EXP_EXTRACT", "version": "1", "status": "proposed",
     "objective": "Extract experience requirements from the full description, with the source sentence.",
     "summary": "Years (\"3 ans\", \"2 à 4 ans\", \"au moins deux ans\", \"5+ years\") only in sentences about "
                "experience; \"hors stage\" / \"hors alternance\"; junior and senior wording; the site's label. "
                "Shown on the offer page (Experience filter)."},
    {"skill_id": "SK_EXP_DECIDE", "version": "1", "status": "proposed",
     "objective": "Decide keep / reject / review with a traceable reason; unclear cases go to review.",
     "summary": "Limit = min(your max years, your countable years + stretch). Within → keep; just above → review; "
                "clearly above → reject. Contradictions (junior + many years, label vs text, several numbers) → "
                "review. Check DECISION_QUOTES_SOURCE. Review list on the home page."},
]

AGENT_ID = "agent003"
CHECKS = [  # named checks: (id, what it guarantees, real case that created it)
    ("DECISION_QUOTES_SOURCE", "the quoted sentence is verbatim in the description", "rule of the method (traceability)"),
    ("EXP_IGNORE_COMPANY_HORIZON", "company horizons are not experience requirements",
     "2026-11-21 Datadog 'Staff Software Engineer - Frontend': 'at least our next five years of growth' read as 5 years"),
    ("EXP_CUMULATIVE_REQUIREMENTS", "several required figures add up: the highest binds",
     "2026-11-21 same offer: '10+ years' and '4+ years of leadership' sent to review instead of rejected"),
    ("EXP_JUNIOR_STRICT", "bare 'junior'/'graduate' count in the title only",
     "2026-11-21 same offer: 'from new university graduates to senior engineers' read as open to beginners"),
]
NUM = r"(?<![a-z0-9+])(\d{1,2}|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|one|two|three|four|five|seven|eight|ten)"
UNIT = r"(?:ans?|annees?|years?|yrs?)(?![a-z])"
EXP_CONTEXT = re.compile(r"experience|exp\b|exp\.|years of|pratique|background|minimum|au moins|at least")
NOT_EXP = re.compile(r"\b(fondee|creee|cree|existe|depuis|il y a|anniversaire|histoire|founded|since|ago|age de|ans d'age|"
                     r"garantie|contrat de \d|cdd de|duree|mission de|next|prochaines?|growth|croissance|horizon)\b")
# EXP_IGNORE_COMPANY_HORIZON (real case 2026-11-21, Datadog "Staff Software Engineer - Frontend":
# "building solutions that take us through at least our next five years of growth" was read as 5 years required)
PREFERRED = re.compile(r"ideal|ideally|preferabl|souhait|un plus|bonus|nice to have|apprecie|would be a plus|is a plus")
RANGE = re.compile(rf"{NUM}\s*(?:a|-|–|to|ou)\s*{NUM}\s*{UNIT}")
PLAIN = re.compile(rf"(?:\+\s*de\s*|plus de\s*|minimum\s*(?:de\s*)?|au moins\s*|at least\s*|min\.?\s*)?{NUM}\s*\+?\s*{UNIT}(?!\s*d'age)")
EXCL = re.compile(r"(?:hors|en dehors des?|excluding|excluant|sans compter les?)\s+(?:les\s+)?((?:stages?|alternances?|internships?|apprenticeships?)"
                  r"(?:\s*(?:et|/|,|ou|and|or)\s*(?:les\s+)?(?:stages?|alternances?|internships?|apprenticeships?))?)"
                  r"|((?:stages?|alternances?)(?:\s*(?:et|/|ou)\s*(?:stages?|alternances?))?)\s+(?:non compris|non inclus|exclus|ne comptent pas)")
LABEL_YEARS = re.compile(r"(\d{1,2})\s*an")


def to_int(token: str, words: dict) -> int | None:
    if token.isdigit():
        return int(token)
    return words.get("number_words", {}).get(token)


def extract(description: str, title: str, label: str | None, words: dict) -> dict:
    """Everything the decision needs, from the full text. Pure function (tested sentence by sentence)."""
    mentions, excl_i, excl_a, excl_sentence = [], False, False, None
    for s in sentences(description):
        f = fold(s)
        m = EXCL.search(f)
        if m:
            what = m.group(1) or m.group(2) or ""
            excl_i = excl_i or bool(re.search(r"stage|internship", what))
            excl_a = excl_a or bool(re.search(r"alternance|apprenticeship", what))
            excl_sentence = excl_sentence or s
        if not EXP_CONTEXT.search(f) or NOT_EXP.search(f):
            continue
        found = []
        for r in RANGE.finditer(f):
            a, b = to_int(r.group(1), words), to_int(r.group(2), words)
            if a is not None and b is not None and a <= b <= 20:
                found.append(a)
        f_wo_ranges = RANGE.sub(" ", f)
        for p in PLAIN.finditer(f_wo_ranges):
            n = to_int(p.group(1), words)
            if n is not None and n <= 20:
                found.append(n)
        preferred = bool(PREFERRED.search(f))
        for n in found:
            mentions.append((n, s, preferred))
    ft, fd = fold(title), fold(description)
    # EXP_JUNIOR_STRICT (real case 2026-11-21, same offer: "from new university graduates to senior engineers" was
    # read as "open to beginners"): bare words (junior, graduate) count in the TITLE only; the text needs a phrase.
    sig = words["experience_signals"]

    def first(terms, text):
        return next((w for w in terms if re.search(rf"(?<![a-z]){re.escape(fold(w).strip())}(?![a-z])", text)), None)

    junior = first(sig["junior_title"], ft) or first(sig["junior_text"], fd)
    senior = first(sig["senior"], ft)
    label_years, label_beginner = None, False
    if label:
        fl = fold(label)
        label_beginner = "debutant" in fl or fl.strip().startswith("d /") or fl.strip() == "d"
        m = LABEL_YEARS.search(fl)
        label_years = int(m.group(1)) if m else None
    return {"mentions": mentions, "excludes_internship": excl_i, "excludes_apprenticeship": excl_a,
            "exclusion_sentence": excl_sentence, "junior": junior, "senior": senior,
            "label_years": label_years, "label_beginner": label_beginner}


def countable_years(months: dict, rule: dict, excl_i: bool, excl_a: bool) -> float:
    m = months.get("job", 0) + months.get("freelance", 0)
    if rule.get("count_internships", True) and not excl_i:
        m += months.get("internship", 0)
    if rule.get("count_apprenticeships", True) and not excl_a:
        m += months.get("apprenticeship", 0)
    return round(min(m, months.get("total", m)) / 12, 1)


def decide(x: dict, months: dict, rule: dict, exp_cfg: dict) -> dict:
    """keep / reject / review + reason code, sentence and confidence (all computed, nothing generated)."""
    cand = countable_years(months, rule, x["excludes_internship"], x["excludes_apprenticeship"])
    cand_all = countable_years(months, rule, False, False)
    max_req, stretch = rule.get("max_years_required", 2), rule.get("stretch_years", 0)
    gap, spread = exp_cfg.get("borderline_gap_years", 1), exp_cfg.get("multiple_years_spread", 2)
    limit = min(max_req, cand + stretch)
    limit_all = min(max_req, cand_all + stretch)
    out = {"candidate_years": cand, "years_required": None, "source_sentence": None}

    def res(decision, code, reason, sentence=None, conf=0.5, years=None):
        out.update(decision=decision, reason_code=code, reason=reason, source_sentence=sentence, confidence=conf,
                   years_required=years)
        return out

    if x["mentions"]:
        # EXP_CUMULATIVE_REQUIREMENTS (real case 2026-11-21, same offer: "10+ years of engineering experience" and
        # "4+ years of technical leadership" went to review): requirements add up, so the binding one is the
        # highest; a figure marked as preferred ("idéalement 5 ans") only counts when nothing else is given.
        required = [m for m in x["mentions"] if not m[2]]
        pool = required or x["mentions"]
        low, low_sentence, _ = min(pool, key=lambda t: t[0])
        req, sentence, _ = max(pool, key=lambda t: t[0]) if required else (low, low_sentence, True)
        if required and low <= limit < req and req - low > spread:
            return res("review", "MULTIPLE_YEARS", f"{low} and {req} years both required", sentence, 0.5, req)
        if x["junior"] and req >= 3:
            return res("review", "CONTRADICTION_JUNIOR_YEARS", f"'{x['junior']}' but {req} years asked", sentence, 0.5, req)
        if x["label_beginner"] and req >= 2 or (x["label_years"] is not None and abs(x["label_years"] - req) >= 2):
            return res("review", "CONTRADICTION_LABEL_TEXT",
                       f"label says {'beginner' if x['label_beginner'] else str(x['label_years']) + ' years'}, text says {req}",
                       sentence, 0.5, req)
        if req <= limit:
            return res("keep", "YEARS_WITHIN_LIMIT", f"{req} years asked, limit {limit:g} (you count {cand:g})",
                       sentence, 0.9, req)
        excluded = x["excludes_internship"] or x["excludes_apprenticeship"]
        if req <= limit + gap:
            code = "EXCLUSION_REDUCES_YEARS" if excluded and req <= limit_all else "YEARS_BORDERLINE"
            return res("review", code, f"{req} years asked, limit {limit:g} (you count {cand:g})", sentence, 0.5, req)
        if excluded and req <= limit_all:
            return res("reject", "EXCLUSION_REDUCES_YEARS",
                       f"{req} years asked without internships/apprenticeships; you count {cand:g}",
                       x["exclusion_sentence"] or sentence, 0.85, req)
        return res("reject", "YEARS_ABOVE_LIMIT", f"{req} years asked, limit {limit:g} (you count {cand:g})",
                   sentence, 0.95 if req >= limit + 3 else 0.85, req)
    if x["label_years"] is not None and x["label_years"] > limit:
        return res("review", "CONTRADICTION_LABEL_TEXT",
                   f"label says {x['label_years']} years, the text gives no number", None, 0.5, None)
    if x["junior"]:
        return res("keep", "JUNIOR_SIGNAL", f"'{x['junior']}' in the text", None, 0.85)
    if x["senior"]:
        return res("review", "SENIOR_TITLE_NO_YEARS", f"'{x['senior'].strip()}' in the title, no number of years", None, 0.5)
    if x.get("partial"):
        # PARTIAL_DESCRIPTION (France Travail listing pages, 2026-11-21): an excerpt without any requirement says
        # nothing about the full text, so the offer goes to review, never to "keep" by default.
        return res("review", "PARTIAL_DESCRIPTION", "excerpt only, no experience information in it", None, 0.4)
    if x["senior"]:
        return res("review", "SENIOR_TITLE_NO_YEARS", f"'{x['senior'].strip()}' in the title, no number of years", None, 0.5)
    return res("keep", "NO_REQUIREMENT", "no experience requirement found in the full text", None, 0.7)


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def profile(self, ctx: RunContext):
        h = (ctx.previous.get("agent001") or {}).get("data", {}).get("profile_hash")
        with ctx.connect() as c:
            row = (c.execute("SELECT * FROM profile_versions WHERE profile_hash=?", (h,)).fetchone() if h else
                   c.execute("SELECT * FROM profile_versions ORDER BY created_at DESC LIMIT 1").fetchone())
        return row

    def validate_input(self, ctx):
        return [] if self.profile(ctx) else ["no profile version: run agent001 first"]

    def plan(self, ctx):
        prof = self.profile(ctx)
        with ctx.connect() as c:
            ids = [r[0] for r in c.execute(
                "SELECT o.offer_id FROM offers o WHERE o.status='new' AND NOT EXISTS (SELECT 1 FROM experience_decisions d "
                "WHERE d.offer_id=o.offer_id AND d.profile_hash=?) ORDER BY o.first_seen_at", (prof["profile_hash"],))]
        return [{"objective": f"decide {len(ids[i:i + 100])} offers", "offer_ids": ids[i:i + 100]}
                for i in range(0, len(ids), 100)] or [{"objective": "nothing new to decide", "offer_ids": []}]

    def work(self, ctx: RunContext, subtasks):
        import json
        prof = self.profile(ctx)
        months = json.loads(prof["candidate_months"])
        rule = ctx.instance["criteria"].get("experience_rule", {})
        counts = {"keep": 0, "reject": 0, "review": 0}
        reasons: dict[str, int] = {}
        outcomes, warnings = [], []
        for cid, name, origin in CHECKS:
            ctx.store.register_check(cid, AGENT_ID, name, origin)
        with ctx.connect() as c:
            for sub in subtasks:
                for oid in sub["offer_ids"]:
                    o = c.execute("SELECT title, description, label_experience, description_complete FROM offers "
                                  "WHERE offer_id=?", (oid,)).fetchone()
                    try:
                        x = extract(o["description"], o["title"], o["label_experience"], ctx.words)
                        x["partial"] = o["description_complete"] == 0
                        d = decide(x, months, rule, ctx.config["experience"])
                    except Exception as exc:  # noqa: BLE001 - an unparsable offer goes to review, never to the bin
                        x = {"excludes_internship": False, "excludes_apprenticeship": False, "junior": None, "senior": None}
                        d = {"decision": "review", "reason_code": "UNREADABLE", "candidate_years": 0,
                             "reason": f"could not be read: {type(exc).__name__}", "source_sentence": None,
                             "confidence": 0.0, "years_required": None}
                        warnings.append(f"{oid}: {type(exc).__name__}")
                    if len({m[0] for m in x.get("mentions", []) if not m[2]}) > 1:
                        outcomes.append({"check": "EXP_CUMULATIVE_REQUIREMENTS", "outcome": "pass", "item": oid,
                                         "detail": d["reason_code"]})
                    if d["source_sentence"] is not None:
                        ok = d["source_sentence"] in o["description"]
                        outcomes.append({"check": "DECISION_QUOTES_SOURCE", "outcome": "pass" if ok else "kept_despite",
                                         "item": oid, "detail": "" if ok else "sentence not found verbatim"})
                    upsert(c, ctx.rows, "experience_decisions", {
                        "offer_id": oid, "profile_hash": prof["profile_hash"], "run_id": ctx.run_id,
                        "decision": d["decision"], "reason_code": d["reason_code"], "reason": d["reason"],
                        "source_sentence": d["source_sentence"], "years_required": d["years_required"],
                        "candidate_years": d["candidate_years"], "excludes_internship": x["excludes_internship"],
                        "excludes_apprenticeship": x["excludes_apprenticeship"], "junior_signal": x["junior"],
                        "senior_signal": (x["senior"] or "").strip() or None, "confidence": d["confidence"],
                        "user_override": None, "decided_at": utc_now()})
                    counts[d["decision"]] += 1
                    reasons[d["reason_code"]] = reasons.get(d["reason_code"], 0) + 1
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        return envelope(ctx, AGENT_ID, "succeeded", data={**counts, "reasons": reasons}, warnings=warnings,
                        metrics={**counts, "decided": sum(counts.values())})


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
