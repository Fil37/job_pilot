"""agent002 — normalisation, salary, duplicates across aggregators, source journal, errors per source."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SCHEMAS, FakeWeb, env_vars, ft_offer, gh_job, make_orch, run_main  # noqa: E402

from app.agent.agent002_offer_collector import dedup_key, html_to_text, make_offer, normalize_contract, parse_salary  # noqa: E402

W = SCHEMAS["words"]
FT_KEYS = {"FT_CLIENT_ID": "id-test", "FT_CLIENT_SECRET": "secret-test"}
TARGETS = [{"name": "ACME", "greenhouse": "acme"}]


def test_salary_is_computed_from_the_text():
    """Case: France Travail / site formats; yearly euros; no figure → nothing invented."""
    assert parse_salary("Annuel de 40000,00 Euros à 45000,00 Euros") == (40000, 45000)
    assert parse_salary("40 - 45 k€ selon profil") == (40000, 45000)
    assert parse_salary("Mensuel de 3000,00 Euros sur 12 mois") == (36000, 36000)
    assert parse_salary("45K€ brut annuel") == (45000, 45000)
    assert parse_salary("Selon profil, 13 mois") == (None, None)
    assert parse_salary(None) == (None, None)


def test_contracts_are_normalised():
    """Case: France Travail codes (MIS = intérim) and free text ('Stage de fin d'études', 'Full-time')."""
    assert normalize_contract("MIS", W) == "INTERIM" and normalize_contract("CDI", W) == "CDI"
    assert normalize_contract("Stage de fin d'études", W) == "STAGE"
    assert normalize_contract("Full-time", W) == "CDI" and normalize_contract("Mission en régie", W) is None


def test_duplicate_key_ignores_case_accents_legal_suffix_and_gender_mark():
    """Case: the same job on France Travail ('Data Engineer (H/F)', 'ACME SAS', '75 - Paris 11e') and on a
    career page ('Data engineer', 'Acme', 'Paris, France')."""
    assert dedup_key("ACME SAS", "Data Engineer (H/F)", "75 - Paris 11e") == dedup_key("Acme", "Data engineer", "Paris, France")
    assert dedup_key("ACME", "Data Engineer", "Paris") != dedup_key("ACME", "Data Scientist", "Paris")
    assert dedup_key("ACME", "Data Engineer", "Paris") != dedup_key("ACME", "Data Engineer", "Lyon")


def test_incomplete_offer_is_dropped_and_html_is_cleaned():
    """Case: an offer without description cannot be judged (OFFER_MANDATORY_FIELDS); HTML becomes plain text."""
    assert make_offer(W, source_id="x", external_id="1", title="T", company="C", url="https://a", description="") is None
    assert html_to_text("&lt;p&gt;Python&lt;/p&gt;&lt;ul&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;") == "Python\n• SQL"


def test_collection_merges_duplicates_and_logs_every_source():
    """Case: the same offer on France Travail and Greenhouse is stored once with 2 aliases; a second run counts it
    'already seen'; disabled sources are logged 'disabled'; no credentials for nothing."""
    web = FakeWeb(ft=[ft_offer(1), ft_offer(2, title="Data Scientist", company="Beta")],
                  gh={"acme": [gh_job(10), gh_job(11, title="Office Manager")]})
    with env_vars(**FT_KEYS):
        orch = make_orch(transport=web, target_companies=TARGETS)
        r1 = orch.run(None, only=["agent001", "agent002"])
        r2 = orch.run(None, only=["agent001", "agent002"])
    c = sqlite3.connect(orch.db_path)
    assert c.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 2          # Office Manager: not a wanted title
    assert c.execute("SELECT COUNT(*) FROM offer_aliases").fetchone()[0] == 3
    d1, d2 = r1["results"]["agent002"], r2["results"]["agent002"]
    # France Travail answers the same 2 offers to each of its title queries: counted once (SOURCE_INTERNAL_DUPLICATES)
    assert (d1["new"], d1["merged"], d1["already_seen"]) == (2, 1, 0) and (d2["new"], d2["already_seen"]) == (0, 3)
    statuses = dict(c.execute("SELECT source_id, status FROM source_runs WHERE run_id=?", (r1["run_id"],)).fetchall())
    assert statuses["france_travail"] == "succeeded" and statuses["greenhouse"] == "succeeded"
    assert statuses["hellowork"] == "disabled" and statuses["manual"] == "valid_empty_result"
    assert len(statuses) == len(SCHEMAS["settings"]["defaults"]["sources"])
    salary = c.execute("SELECT salary_min, salary_max, contract FROM offers WHERE source_id='france_travail' "
                       "AND title LIKE 'Data Eng%'").fetchone()
    assert salary == (42000, 48000, "CDI")
    assert "secret-test" not in str(c.execute("SELECT * FROM events").fetchall())


def test_missing_credentials_is_not_configured_and_a_failing_source_gives_partial():
    """Case: no FT_CLIENT_ID → 'not_configured' (warning, no incident); Greenhouse answering 503 → 'failed',
    the run of agent002 is partial when another source worked, failed when none did."""
    web = FakeWeb(gh={"acme": [gh_job(10)]}, fail=("greenhouse",))
    with env_vars(FT_CLIENT_ID=None, FT_CLIENT_SECRET=None):
        orch = make_orch(transport=web, target_companies=TARGETS)
        with sqlite3.connect(orch.db_path) as c:
            c.execute("INSERT INTO offer_inbox(url,title,company,description,created_at) VALUES "
                      "('https://x.org/1','Data Engineer','Gamma','Python SQL pipelines, junior bienvenu, CDI Paris','now')")
        r = orch.run(None, only=["agent001", "agent002"])
    env = r["results"]["agent002"]
    assert env["new"] == 1
    c = sqlite3.connect(orch.db_path)
    st = dict(c.execute("SELECT source_id, status FROM source_runs").fetchall())
    assert st["france_travail"] == "not_configured" and st["greenhouse"] == "failed" and st["manual"] == "succeeded"
    assert r["status"] == "partial"
    assert c.execute("SELECT imported_run FROM offer_inbox").fetchone()[0] == r["run_id"]
    assert c.execute("SELECT COUNT(*) FROM incidents WHERE error_category='misconfiguration'").fetchone()[0] == 0
    # the manual inbox is a source that works even when empty: disable it to test "no source worked"
    web2 = FakeWeb(fail=("greenhouse",))
    with env_vars(FT_CLIENT_ID=None):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        from conftest import make_instance
        inst = make_instance(tmp, target_companies=TARGETS)
        inst["overrides"] = {"sources": {"manual": {"enabled": False}}}
        orch2 = make_orch(tmp, transport=web2, instance=inst)
        r2 = orch2.run(None, only=["agent001", "agent002"])
    assert r2["status"] == "failed"


def test_title_domain_words_are_enough():
    """Incident 2026-11-21: Alice & Bob 'Electronics Integration Engineer' was skipped (check TITLE_DOMAIN_TOKENS);
    'Office Manager' and 'Software Engineer' stay out for an electronics / embedded search."""
    from app.agent.agent002_offer_collector import _title_wanted
    terms = {"title": ["electronics engineer", "ingenieur systemes embarques", "ingenieur fpga"], "core_skill": ["vhdl"]}
    assert _title_wanted("Electronics Integration Engineer", terms)
    assert _title_wanted("Ingénieur logiciel embarqué (H/F)", terms) and _title_wanted("FPGA Design Engineer", terms)
    assert not _title_wanted("Office Manager", terms) and not _title_wanted("Software Engineer", terms)


if __name__ == "__main__":
    run_main(globals())
