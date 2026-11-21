"""agent003 — experience filter on real-looking sentences. Candidate = the example profile: 6 months of
internship + 12 months of apprenticeship (1.5 years), rule max 2 years, stretch 1 year → limit 2."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SCHEMAS, run_main  # noqa: E402

from app.agent.agent003_experience_filter import decide, extract  # noqa: E402

W = SCHEMAS["words"]
MONTHS = {"internship": 6, "apprenticeship": 12, "job": 0, "freelance": 0, "total": 18}
RULE = {"max_years_required": 2, "count_internships": True, "count_apprenticeships": True, "stretch_years": 1}
EXP = SCHEMAS["settings"]["defaults"]["experience"]


def judge(desc, title="Data Engineer", label=None, rule=RULE):
    d = decide(extract(desc, title, label, W), MONTHS, rule, EXP)
    if d["source_sentence"] is not None:      # DECISION_QUOTES_SOURCE
        assert d["source_sentence"] in desc, d["source_sentence"]
    return d["decision"], d["reason_code"], d["years_required"]


def test_years_within_borderline_and_above():
    """Case: 1 year → keep; 3 years ('minimum') → review (just above limit 2); 5 years → reject."""
    assert judge("Une première expérience de 1 an en data est attendue.") == ("keep", "YEARS_WITHIN_LIMIT", 1)
    assert judge("Vous avez 3 ans d'expérience minimum sur Spark.") == ("review", "YEARS_BORDERLINE", 3)
    assert judge("Expérience de 5 ans minimum exigée.") == ("reject", "YEARS_ABOVE_LIMIT", 5)
    assert judge("Au moins deux ans d'expérience en Python.") == ("keep", "YEARS_WITHIN_LIMIT", 2)
    assert judge("You have 5+ years of experience with Kafka.") == ("reject", "YEARS_ABOVE_LIMIT", 5)


def test_range_counts_its_lower_bound():
    """Case: '2 à 4 ans d'expérience' asks for 2 years at least."""
    assert judge("Profil : 2 à 4 ans d'expérience en data engineering.") == ("keep", "YEARS_WITHIN_LIMIT", 2)


def test_hors_stage_et_alternance_lowers_the_countable_years():
    """Case: '2 ans d'expérience hors stage et alternance' — the example profile has 0 countable year left, so
    limit = 1: review with EXCLUSION_REDUCES_YEARS (it would have been kept without the exclusion)."""
    d, code, years = judge("Vous justifiez de 2 ans d'expérience hors stage et alternance.")
    assert (d, code, years) == ("review", "EXCLUSION_REDUCES_YEARS", 2)
    x = extract("Vous justifiez de 2 ans d'expérience hors stage et alternance.", "Data Engineer", None, W)
    assert x["excludes_internship"] and x["excludes_apprenticeship"]
    x = extract("3 ans d'expérience (alternance non comprise).", "Data Engineer", None, W)
    assert x["excludes_apprenticeship"] and not x["excludes_internship"]


def test_user_rule_not_counting_internships():
    """Case: your rule says internships do not count: 1.0 countable year (apprenticeship only)."""
    rule = dict(RULE, count_internships=False)
    d = decide(extract("2 ans d'expérience.", "Data Engineer", None, W), MONTHS, rule, EXP)
    assert d["candidate_years"] == 1.0 and d["decision"] == "keep"


def test_junior_wording_and_contradictions():
    """Case: 'jeune diplômé' → keep; 'jeunes diplômés' + 5 years → review (contradiction); site label 'débutant'
    + text 3 years → review (label vs text)."""
    assert judge("Poste ouvert aux jeunes diplômés passionnés.") == ("keep", "JUNIOR_SIGNAL", None)
    assert judge("Poste ouvert aux jeunes diplômés. Vous avez 5 ans d'expérience.")[:2] == ("review", "CONTRADICTION_JUNIOR_YEARS")
    assert judge("Vous avez 3 ans d'expérience en ETL.", label="D")[:2] == ("review", "CONTRADICTION_LABEL_TEXT")
    assert judge("Missions variées sur la plateforme data.", label="E / 5 An(s)")[:2] == ("review", "CONTRADICTION_LABEL_TEXT")


def test_senior_title_without_years_goes_to_review_not_to_the_bin():
    """Case: Datadog 'Senior Software Engineer - Distributed Systems' (Paris, 2026-11-21): no number in the text."""
    assert judge("You have significant backend programming experience.", title="Senior Software Engineer")[:2] == \
        ("review", "SENIOR_TITLE_NO_YEARS")


def test_company_facts_are_not_requirements():
    """Case: 'entreprise fondée il y a 20 ans', 'CDD de 2 ans' are not experience requirements."""
    assert judge("Notre entreprise, fondée il y a 20 ans, a de l'expérience. CDD de 2 ans.")[:2] == ("keep", "NO_REQUIREMENT")


def test_real_case_datadog_staff_engineer():
    """Incident 2026-11-21 (Datadog 'Staff Software Engineer - Frontend'): before the fix the offer went to review
    (MULTIPLE_YEARS) because 'at least our next five years of growth' was read as a requirement, '10+' and '4+'
    were seen as conflicting and 'new university graduates' as a junior signal. Expected: reject, 10 years, no junior
    signal (checks EXP_IGNORE_COMPANY_HORIZON, EXP_CUMULATIVE_REQUIREMENTS, EXP_JUNIOR_STRICT)."""
    desc = ("Datadog’s Staff Engineers are our technical leaders, building solutions that take us through at least our "
            "next five years of growth.\n• Coach and develop engineers at various levels, from new university graduates "
            "to senior engineers with years of industry experience\n• 10+ years of relevant engineering experience\n"
            "• 4+ years of technical leadership experience, including both effective communication")
    x = extract(desc, "Staff Software Engineer - Frontend", None, W)
    assert x["junior"] is None and x["senior"] == "staff"
    assert sorted(m[0] for m in x["mentions"]) == [4, 10]
    assert judge(desc, title="Staff Software Engineer - Frontend") == ("reject", "YEARS_ABOVE_LIMIT", 10)


def test_preferred_years_and_two_different_requirements():
    """Case: '3 ans, idéalement 5' → the requirement is 3 (review, borderline); '2 ans en Python' + '5 ans en gestion
    de projet' → the decision depends on which one binds → review MULTIPLE_YEARS."""
    assert judge("3 ans d'expérience, idéalement 5 ans.")[:3] == ("review", "YEARS_BORDERLINE", 3)
    assert judge("2 ans d'expérience en Python. 5 ans d'expérience en gestion de projet.")[:2] == ("review", "MULTIPLE_YEARS")


def test_excerpt_without_requirement_goes_to_review():
    """Case 2026-11-21 (France Travail listing pages, check PARTIAL_DESCRIPTION): an excerpt without years is not a
    'no requirement'; a senior title is reported as such; years or junior wording in the excerpt still decide."""
    def partial(desc, title="Ingénieur FPGA (H/F)"):
        x = extract(desc, title, None, W)
        x["partial"] = True
        d = decide(x, MONTHS, RULE, EXP)
        return d["decision"], d["reason_code"]
    assert partial("Conception FPGA en VHDL pour l'aéronautique...") == ("review", "PARTIAL_DESCRIPTION")
    assert partial("Conception FPGA...", title="Ingénieur Fpga Expérimenté H/F") == ("review", "SENIOR_TITLE_NO_YEARS")
    assert partial("Poste ouvert aux jeunes diplômés...") == ("keep", "JUNIOR_SIGNAL")
    assert partial("Vous avez 8 ans d'expérience en FPGA...") == ("reject", "YEARS_ABOVE_LIMIT")


if __name__ == "__main__":
    run_main(globals())
