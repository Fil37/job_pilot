"""agent001 — profile checks, countable experience and derived search terms."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import EXAMPLE_INSTANCE, EXAMPLE_PROFILE, SCHEMAS, run_main  # noqa: E402

from app.agent.agent001_profile_builder import candidate_months, check_profile, derive_terms, load_profile  # noqa: E402


def profile():
    return load_profile(EXAMPLE_PROFILE)


def test_example_profile_passes_the_checks():
    """Case: the shipped example satisfies PROFILE_FIGURES_DECLARED and PROFILE_SKILL_DECLARED."""
    problems, outcomes = check_profile(profile())
    assert problems == []
    assert outcomes and all(o["outcome"] == "pass" for o in outcomes)


def test_undeclared_figure_is_detected():
    """Case: 'Réduction de 45 %' while figures says '30 %' — a CV must never quote a number you did not declare."""
    p = profile()
    p["experiences"][0]["bullets"][1]["variants"].append("Réduction de 45 % du temps de calcul")
    problems, outcomes = check_profile(p)
    assert any("['45']" in x for x in problems)
    assert any(o["check"] == "PROFILE_FIGURES_DECLARED" and o["outcome"] == "kept_despite" for o in outcomes)


def test_unknown_skill_and_bad_date_and_duplicate_id_are_detected():
    """Case: a bullet proving 'kubernetes' absent from skills; a date '2024/03'; two bullets with the same id."""
    p = profile()
    p["projects"][0]["bullets"][0]["skills"].append("kubernetes")
    p["experiences"][0]["start"] = "2024/03"
    p["experiences"][1]["bullets"][0]["id"] = "exp_stage_fin_b1"
    problems, _ = check_profile(p)
    text = " | ".join(problems)
    assert "kubernetes" in text and "YYYY-MM" in text and "duplicate ids" in text


def test_countable_months_per_kind():
    """Case: example internship 2024-03..2024-08 = 6 months, apprenticeship 2022-09..2023-08 = 12 months."""
    m = candidate_months(profile())
    assert m["internship"] == 6 and m["apprenticeship"] == 12 and m["job"] == 0 and m["total"] == 18


def test_overlapping_experiences_are_not_counted_twice():
    """Case: two internships over the same months count once in the total."""
    p = profile()
    p["experiences"].append(dict(p["experiences"][0], id="dup", bullets=[]))
    assert candidate_months(p)["internship"] == 6


def test_terms_titles_neighbours_core_skills_exclusions():
    """Case: role 'data engineer' brings its family (analytics engineer...); python/sql are core; avoided sectors,
    excluded keywords and contracts other than CDI/CDD are eliminatory terms."""
    terms = derive_terms(profile(), EXAMPLE_INSTANCE["criteria"], SCHEMAS["words"])
    by = {}
    for t in terms:
        by.setdefault(t["kind"], set()).add(t["value"])
    assert {"data engineer", "analytics engineer", "ml engineer"} <= by["title"]
    # core = level 'core' (python, sql, git) or proven by 2+ bullets (postgresql); airflow (1 bullet) is not core
    assert by["core_skill"] == {"python", "sql", "git", "postgresql"}
    assert "keyword:commercial terrain" in by["exclusion"] and "sector:defense:armement" in by["exclusion"]
    assert "contract:stage" in by["exclusion"] and "contract:cdi" not in by["exclusion"]


if __name__ == "__main__":
    run_main(globals())
