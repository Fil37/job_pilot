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


def test_mobility_france_accepts_france_and_refuses_abroad():
    """Case: your CV 'Mobilité France, dont Mulhouse' and your rule 2026-11-23 'mobile dans toute la France' (check
    MOBILITY_FRANCE): Toulouse 1.0 like Paris (zone), 'New York, USA' eliminated."""
    crit = dict(CRIT, mobility_france=True)
    assert score_offer(offer(location="31 - Toulouse"), "keep", TERMS, PROFILE, crit, W, SCORING)["components"]["values"]["location"] == 1.0
    assert score_offer(offer(location="Paris 11e"), "keep", TERMS, PROFILE, crit, W, SCORING)["components"]["values"]["location"] == 1.0
    s = score_offer(offer(location="New York, USA"), "keep", TERMS, PROFILE, crit, W, SCORING)
    assert "LOCATION_OUTSIDE" in s["elimination_reasons"]


def test_a_technology_written_in_the_profile_is_known():
    """Incident 2026-11-21 (check SKILL_FROM_PROFILE_TEXT): 'linux' counted missing although the CV says 'Linux
    embarqué'; agent001 now lists the lexicon technologies written in the profile."""
    prof = dict(PROFILE, skills_written=["linux"])
    s = score_offer(offer(description="Python, SQL sous Linux."), "keep", TERMS, prof, CRIT, W, SCORING)
    assert "linux" not in s["missing"] and {"skill": "linux", "level": "working"} in s["matched"]


def test_a_place_counts_as_france_only_when_recognised():
    """Real case 2026-11-23 (check FRANCE_POSITIVE): an Arbeitnow offer in 'Hamburg' scored 1.0 'France' because only
    foreign places were listed. Now: unknown place outside a French-only source → outside; France Travail places,
    département numbers and known French cities → France."""
    crit = dict(CRIT, mobility_france=True)
    loc = lambda **k: score_offer(offer(**k), "keep", TERMS, PROFILE, crit, W, SCORING)
    assert "LOCATION_OUTSIDE" in loc(location="Hamburg", source_id="arbeitnow")["elimination_reasons"]
    assert loc(location="Aubervilliers", source_id="france_travail_web")["components"]["values"]["location"] == 1.0
    assert loc(location="Bordeaux, France", source_id="arbeitnow")["components"]["values"]["location"] == 1.0
    assert loc(location="Grenoble", source_id="lever")["components"]["values"]["location"] == 1.0


def test_a_skill_accepted_instead_of_yours_is_not_missing():
    """Real case 2026-11-23 (check SKILL_ALTERNATIVES): « VHDL et/ou Verilog » with VHDL in your profile: Verilog is
    not missing and the note says why; « VHDL, Verilog » (both asked) keeps Verilog missing."""
    words = dict(W, skill_lexicon={**W["skill_lexicon"], "vhdl": ["vhdl"], "verilog": ["verilog"]},
                 skill_alternative_groups={"groups": [["vhdl", "verilog"]]})
    prof = dict(PROFILE, skills={**PROFILE["skills"], "vhdl": "core"})
    s = score_offer(offer(description="Conception en VHDL et/ou Verilog."), "keep", TERMS, prof, CRIT, words, SCORING)
    assert "verilog" not in s["missing"] and "vhdl accepté à la place de verilog" in s["components"]["notes"]["skills"]
    s = score_offer(offer(description="Conception en VHDL, Verilog."), "keep", TERMS, prof, CRIT, words, SCORING)
    assert "verilog" in s["missing"]


if __name__ == "__main__":
    run_main(globals())
