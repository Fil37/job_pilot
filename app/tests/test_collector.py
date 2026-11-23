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
        inst["overrides"]["sources"]["manual"] = {"enabled": False}
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


FT_ROBOTS = """User-agent: *
Disallow: /espacepersonnel/
Disallow: *motsCles=*
Disallow: /offres/recherchesenregistrees
Disallow: *JSESSIONID*
"""
FT_ITEM = ('<li data-id-offre="{id}" class="result"><a href="/offres/recherche/detail/{id}"><img alt="Origine de l\'offre : '
           'France Travail"/><h2 class="t4 media-heading"><span class="media-heading-title">{title}</span></h2>'
           '<p translate="no" class="subtext">\n{sub}</p><p class="description">{desc}</p><p class="contrat">CDI'
           '<span class="type-contrat">Temps plein</span></p><p class="date">\nPublié il y a 3 jours\n</p></a></li>')


def ft_page(*items):
    return "<ul>" + "".join(FT_ITEM.format(**i) for i in items) + "</ul>"


def test_robots_rules_of_france_travail_are_respected():
    """Case 2026-11-21 (check ROBOTS_ALLOWED): France Travail forbids keyword search to robots; métier pages allowed."""
    from app.agent.agent002_offer_collector import robots_allowed, robots_rules
    rules = robots_rules(FT_ROBOTS)
    assert robots_allowed(rules, "/offres/emploi/ingenieur-electronicien/s13m6")
    assert robots_allowed(rules, "/rechercheoffre/sitemap/secteurs-metiers.xml")
    assert not robots_allowed(rules, "/offres/recherche?motsCles=fpga&lieux=75")
    assert not robots_allowed(rules, "/espacepersonnel/x") and not robots_allowed(rules, "/a;JSESSIONID=1")
    assert robots_allowed(robots_rules("User-agent: *\nDisallow: /*?\nAllow: /jobs?*"), "/jobs?q=1")


def test_france_travail_listing_is_parsed_including_offers_without_employer():
    """Incident 2026-11-21 (check FT_NO_COMPANY): '92 - Boulogne-Billancourt' without employer gave company '92'."""
    from app.agent.agent002_offer_collector import parse_ft_listing
    page = ft_page({"id": "216HQRB", "title": "Ingénieur FPGA (H/F)", "sub": "ELSYS Design - <span>94 - Cachan</span>",
                    "desc": "Conception FPGA en VHDL..."},
                   {"id": "216AAAA", "title": "Ingénieur électronique (H/F)", "sub": "<span>92 - Boulogne-Billancourt</span>",
                    "desc": "Cartes électroniques..."})
    a, b = parse_ft_listing(page, W)
    assert (a["external_id"], a["company"], a["location"], a["contract"]) == ("216HQRB", "ELSYS Design", "94 - Cachan", "CDI")
    assert a["description_complete"] is False and a["url"].endswith("/offres/recherche/detail/216HQRB")
    assert a["published_at"] is not None
    assert (b["company"], b["location"]) == ("Entreprise non communiquée", "92 - Boulogne-Billancourt")


def test_france_travail_pages_source_reads_only_allowed_pages(mobility=False):
    """Case: the connector reads robots.txt first, then the sitemaps and the métier pages of your titles (national +
    your départements); keyword search is never requested; titles outside your search are not kept."""
    import tempfile
    from conftest import make_instance
    sm = ("<urlset><loc>https://candidat.francetravail.fr/offres/emploi/ingenieur-electronicien/s13m6</loc>"
          "<loc>https://candidat.francetravail.fr/offres/emploi/boulanger/s1m1</loc>"
          "<loc>https://candidat.francetravail.fr/offres/emploi/ingenieur-technico-commercial-en-electronique/s18m10</loc></urlset>")
    cross = ("<urlset><loc>https://candidat.francetravail.fr/offres/emploi/ingenieur-electronicien/essonne/s13m6d91</loc>"
             "<loc>https://candidat.francetravail.fr/offres/emploi/ingenieur-electronicien/rhone/s13m6d69</loc></urlset>")
    calls = []

    def web(method, url, headers, data, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return 200, FT_ROBOTS
        if url.endswith("secteurs-metiers.xml"):
            return 200, sm
        if url.endswith("croisements-metiers-departements.xml"):
            return 200, cross
        if url.endswith("/s13m6") or url.endswith("/s13m6d91"):
            return 200, ft_page({"id": "216" + url[-4:].upper().replace("/", "X"), "title": "Ingénieur électronicien (H/F)",
                                 "sub": "ACME - <span>91 - Orsay</span>", "desc": "Électronique embarquée..."},
                                {"id": "217" + url[-4:].upper().replace("/", "X"), "title": "Office Manager",
                                 "sub": "ACME - <span>91 - Orsay</span>", "desc": "Gestion..."})
        return 404, ""
    tmp = Path(tempfile.mkdtemp())
    inst = make_instance(tmp, roles=["ingénieur électronique"], title_families=["electronics"],
                         zones=[{"label": "Essonne", "keywords": ["essonne", "91"]}], mobility_france=mobility)
    inst["overrides"] = {"sources": {"manual": {"enabled": False}, "greenhouse": {"enabled": False},
                                     "lever": {"enabled": False}, "france_travail": {"enabled": False},
                                     "france_travail_web": {"enabled": True}}}
    orch = make_orch(tmp, transport=web, instance=inst)
    r = orch.run(None, only=["agent001", "agent002"])
    # the same job on the national page and on the Essonne page: stored once, the second one merged as an alias
    assert (r["results"]["agent002"]["new"], r["results"]["agent002"]["merged"]) == (1, 1), r["results"]["agent002"]
    assert calls[0].endswith("/robots.txt") and not any("motsCles" in u for u in calls)
    assert not any("boulanger" in u or "technico" in u for u in calls)
    titles = [t for (t,) in sqlite3.connect(orch.db_path).execute("SELECT title FROM offers")]
    assert titles == ["Ingénieur électronicien (H/F)"]
    rhone = [i for i, u in enumerate(calls) if "rhone" in u]
    if mobility:                                      # FT_ALL_DEPARTMENTS: your zones first, then every other one
        assert rhone and rhone[0] > next(i for i, u in enumerate(calls) if u.endswith("d91"))
    else:
        assert not rhone


def test_mobility_france_reads_every_departement():
    """Real case 2026-11-23 (check FT_ALL_DEPARTMENTS): 90 % of the offers came from Île-de-France and Haut-Rhin
    although you are mobile in all France; with mobility_france the Rhône page is read too, after your zones."""
    test_france_travail_pages_source_reads_only_allowed_pages(mobility=True)

def test_arbeitnow_keeps_every_country_with_full_text():
    """Choice of the user 2026-11-23 ('garde tout'): the Arbeitnow offers matching your titles are kept whatever the
    country, with their full description; pagination stops when there is no next page; other titles are dropped."""
    import json as _json
    import tempfile
    from conftest import make_instance
    page = {"data": [
        {"slug": "fpga-munich-1", "company_name": "Chip GmbH", "title": "FPGA Engineer (m/w/d)", "remote": False,
         "description": "<p>Design in <b>VHDL</b>, 0-2 years</p>", "url": "https://www.arbeitnow.com/jobs/x/fpga-munich-1",
         "job_types": ["Unbefristeter arbeitsvertrag"], "location": "Munich", "created_at": 1795411828},
        {"slug": "fpga-paris-2", "company_name": "Puce SAS", "title": "Ingénieur FPGA", "remote": False,
         "description": "<p>VHDL</p>", "url": "https://www.arbeitnow.com/jobs/y/fpga-paris-2", "job_types": [],
         "location": "Paris", "created_at": 1795411828},
        {"slug": "designer-3", "company_name": "Agentur", "title": "Digital Designer", "remote": False,
         "description": "x", "url": "https://www.arbeitnow.com/jobs/z/designer-3", "job_types": [], "location": "Hamburg",
         "created_at": 1795411828}], "links": {"next": None}}
    calls = []

    def web(method, url, headers, data, timeout):
        calls.append(url)
        return (200, _json.dumps(page)) if "arbeitnow.com/api/job-board-api" in url else (404, "")
    tmp = Path(tempfile.mkdtemp())
    inst = make_instance(tmp, roles=["ingénieur FPGA"], title_families=["fpga"], mobility_france=True)
    inst["overrides"] = {"sources": {k: {"enabled": k == "arbeitnow"} for k in
                                     ("manual", "greenhouse", "lever", "france_travail", "france_travail_web", "arbeitnow")}}
    orch = make_orch(tmp, transport=web, instance=inst)
    r = orch.run(None, only=["agent001", "agent002", "agent003", "agent004"])
    assert r["results"]["agent002"]["new"] == 2 and calls == ["https://www.arbeitnow.com/api/job-board-api?page=1"]
    c = sqlite3.connect(orch.db_path)
    got = dict(c.execute("SELECT location, description FROM offers").fetchall())
    assert "VHDL" in got["Munich"] and "<" not in got["Munich"]
    assert c.execute("SELECT contract FROM offers WHERE location='Munich'").fetchone()[0] == "CDI"
    # FRANCE_POSITIVE: Munich stays outside France (traceable reason), Paris counts as one of your places
    rows = dict(c.execute("SELECT o.location, s.eliminated FROM scores s JOIN offers o USING(offer_id)").fetchall())
    assert rows == {"Munich": 1, "Paris": 0}


if __name__ == "__main__":
    run_main(globals())
