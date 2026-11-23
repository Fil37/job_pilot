"""Your profile file: loading, figures, corpus and fact checks, your skills.

Shared by agent001, agent004, agent006, agent007, agent010 (moved out of agent001 and agent006 on 2026-11-23)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.agent import contains_term, fold


# PROFILE_FIGURES_STANDALONE (real case 2026-11-21, your CV): numbers glued to a name are not figures
# ("i.MX93", "LPC55S69", "CRC-8", "INT8", "DE0-Nano") and "1 632 W" / "4 885" are ONE number each.
NUMBER = re.compile(r"(?<![A-Za-z0-9.\-])\d{1,3}(?:[ \u202f\u00a0]\d{3})+(?:[.,]\d+)?(?![A-Za-z0-9])"
                    r"|(?<![A-Za-z0-9.\-])\d+(?:[.,]\d+)?(?![A-Za-z0-9])")


def numbers(text: str) -> set[str]:
    return {re.sub(r"[ \u202f\u00a0]", "", n).replace(",", ".") for n in NUMBER.findall(text.replace("**", ""))}


def load_profile(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"profile file unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("profile file must be a mapping")
    return data


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


def own_skills(profile: dict) -> set[str]:
    """PROFILE_SKILLS_ONE_DEFINITION (2026-11-23: three agents computed it, slightly differently): your skills = the
    'skills' list + every skill proven by a line of an experience, a project or your education (+ the technologies
    written in the profile text, when agent001 computed them). Folded names."""
    own = {fold(s["name"]) for s in profile.get("skills") or []}
    own |= {fold(x) for sec in ("projects", "experiences", "education") for it in profile.get(sec) or []
            for b in it.get("bullets") or [] for x in b.get("skills") or []}
    own |= {fold(x) for x in profile.get("skills_written") or []}
    return own

