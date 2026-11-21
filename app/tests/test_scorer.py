"""agent004 — score as a weighted sum, met / missing skills, eliminations with their reasons."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import EXAMPLE_INSTANCE, SCHEMAS, run_main  # noqa: E402

from app.agent import contains_term, fold  # noqa: E402
from app.agent.agent004_offer_scorer import score_offer  # noqa: E402

W = SCHEMAS["words"]
SCORING = SCHEMAS["settings"]["defaults"]["scoring"]
CRIT = EXAMPLE_INSTANCE["criteria"]
PROFILE = {"skills": {"python": "core", "sql": "core", "airflow": "working", "docker": "working", "spark": "notions"},
           "aliases": {}}
TERMS = {"title": ["data engineer", "analytics engineer"], "keyword": [], "core_skill": ["python", "sql"],
         "exclusion": ["sector:defense:armement", "keyword:commercial terrain", "contract:stage"]}


def offer(**kw):
    o = {"title": "Data Engineer", "description": "Python, SQL, Airflow et Kubernetes.", "location": "Paris 11e",
         "contract": "CDI", "salary_min": 42000.0, "salary_max": 48000.0}
    o.update(kw)
    return o


def test_score_is_the_weighted_sum_of_the_components():
    """Case: 3 of 4 skills met (python core 1 + sql core 1 + airflow working 0.8 = 2.8/4 = 0.7), keep, role title,
    Paris, salary ok, no sector → 100 x (0.4x0.7 + 0.2 + 0.15 + 0.1 + 0.1 + 0.05) = 88.0."""
    s = score_offer(offer(), "keep", TERMS, PROFILE, CRIT, W, SCORING)
    assert s["components"]["values"]["skills"] == 0.7
    assert s["score"] == 88.0 and not s["eliminated"]
    assert [m["skill"] for m in s["matched"]] == ["airflow", "python", "sql"] and s["missing"] == ["kubernetes"]


def test_unknown_salary_and_location_are_neutral_not_guessed():
    """Case: no salary, location 'France': both 0.5 (settings.scoring.unknown_salary_value)."""
    s = score_offer(offer(salary_min=None, salary_max=None, location="France"), "keep", TERMS, PROFILE, CRIT, W, SCORING)
    assert s["components"]["values"]["salary"] == 0.5 and s["components"]["values"]["location"] == 0.5
    assert not s["eliminated"]


def test_eliminations_keep_their_reasons():
    """Case: salary 30k < 38k min; Lyon outside the areas; 'armement' (avoided sector); internship contract;
    'commercial terrain'; experience rejected — each one named."""
    s = score_offer(offer(salary_max=30000.0, salary_min=28000.0, location="Lyon", contract="STAGE",
                          description="Python pour l'armement, avec du commercial terrain."),
                    "reject", TERMS, PROFILE, CRIT, W, SCORING)
    assert set(s["elimination_reasons"]) == {"SALARY_BELOW_MIN", "LOCATION_OUTSIDE", "AVOIDED_SECTOR",
                                             "CONTRACT_EXCLUDED", "EXCLUDED_KEYWORD", "EXPERIENCE_REJECTED"}
    assert s["components"]["values"]["sector"] == 0.0


def test_full_remote_counts_as_in_zone():
    """Case: 'Full remote' offer based in Lyon, remote_ok true → location 1, not eliminated."""
    s = score_offer(offer(location="Lyon", description="Python, SQL. Poste en full remote."), "keep", TERMS, PROFILE,
                    CRIT, W, SCORING)
    assert s["components"]["values"]["location"] == 1.0 and not s["eliminated"]


def test_short_skill_names_do_not_match_inside_words():
    """Case: 'c' must not match 'c'est' or 'c++'; 'r' not 'r&d'; 'git' matches 'git'."""
    t = fold("C'est un poste C++ en R&D, Git obligatoire.")
    assert contains_term(t, "c++") and contains_term(t, "git")
    assert not contains_term(t, "c") and not contains_term(t, "langage c") and not contains_term(t, "go")


if __name__ == "__main__":
    run_main(globals())
