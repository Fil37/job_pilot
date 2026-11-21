"""Contracts: the five schemas, the registry allow-list, the SKILLS declarations, the result envelope, the
instance file (no secrets) and the overrides."""
import copy
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import EXAMPLE_INSTANCE, SCHEMAS, make_orch, run_main  # noqa: E402

from app.agent import ConfigError, load_instance, merge_config, validate_envelope, validate_schemas  # noqa: E402
from app.agent.agent000_job_pilot import ALLOWED_MODULES, Registry, check_skills  # noqa: E402


def test_the_five_schemas_load_and_validate():
    """Case: start-up validation of app/schemas (skill §9)."""
    assert set(SCHEMAS) == {"reports", "routes", "rows", "settings", "words"}
    validate_schemas(SCHEMAS)


def test_schema_with_unknown_owner_or_type_is_refused():
    """Case: a table owned by an agent that does not exist, a field of unknown type."""
    bad = copy.deepcopy(SCHEMAS)
    bad["rows"]["tables"]["offers"]["owner"] = "agent999"
    bad["rows"]["tables"]["scores"]["fields"]["score"]["type"] = "float"
    try:
        validate_schemas(bad)
        raise AssertionError("accepted")
    except ConfigError as exc:
        assert "agent999" in str(exc) and "float" in str(exc)


def test_weights_must_sum_to_one():
    """Case: scoring weights 0.4+0.2+0.15+0.1+0.1+0.05 = 1; a change that breaks the sum is refused."""
    bad = copy.deepcopy(SCHEMAS)
    bad["settings"]["defaults"]["scoring"]["weights"]["skills"] = 0.9
    try:
        validate_schemas(bad)
        raise AssertionError("accepted")
    except ConfigError as exc:
        assert "sum to 1" in str(exc)


def test_registry_refuses_a_module_outside_the_allow_list():
    """Case: routes.json is data; a JSON value pointing at another module (e.g. 'os') is never imported."""
    routes = copy.deepcopy(SCHEMAS["routes"])
    routes["agents"][0]["module"] = "os"
    try:
        Registry(routes)
        raise AssertionError("accepted")
    except ConfigError as exc:
        assert "not the allowed one" in str(exc)
    reg = Registry(SCHEMAS["routes"])
    try:
        reg.module("agent999")
        raise AssertionError("accepted")
    except ConfigError:
        pass


def test_every_specialist_declares_valid_unique_skills_and_agent000_registers_them():
    """Case: rule of the user 2026-11-21 — SKILLS non-empty, unique skill_id, required fields; registered at start
    as 'proposed' with an activation waiting for approval."""
    seen = []
    for aid, mod in ALLOWED_MODULES.items():
        m = importlib.import_module(mod)
        assert check_skills(aid, m.SKILLS) == [], aid
        seen += [s["skill_id"] for s in m.SKILLS]
    assert len(seen) == len(set(seen)), "duplicate skill_id"
    assert check_skills("x", []) and check_skills("x", [{"skill_id": "A"}])
    orch = make_orch()
    skills = orch.store.skills()
    assert {s["skill_id"] for s in skills} == set(seen)
    assert all(s["status"] == "proposed" for s in skills)
    assert len([a for a in orch.store.approvals() if a["action"] == "activate_skill"]) == len(seen)
    orch.startup()   # a second start does not duplicate the pending activations
    assert len([a for a in orch.store.approvals() if a["action"] == "activate_skill"]) == len(seen)


def test_every_agent_module_has_the_contract():
    """Case: validate_input / plan / run / validate_output / healthcheck on every agent; AGENT_ID matches."""
    for aid, mod in ALLOWED_MODULES.items():
        agent = importlib.import_module(mod).Agent()
        assert agent.AGENT_ID == aid
        for fn in ("validate_input", "plan", "run", "validate_output", "healthcheck"):
            assert callable(getattr(agent, fn)), (aid, fn)
        assert importlib.import_module(mod).__doc__.count("Out of scope") == 1, aid


def test_envelope_validation():
    """Case: unknown status and error item without a known category are refused; a correct one passes."""
    good = {k: [] for k in SCHEMAS["reports"]["result_envelope"]["required"]}
    good.update(status="succeeded", data={}, metrics={}, errors=[{"category": "timeout", "message": "x"}])
    assert validate_envelope(good, SCHEMAS["reports"]) == []
    bad = dict(good, status="done", errors=[{"message": "x"}])
    assert len(validate_envelope(bad, SCHEMAS["reports"])) == 2


def test_instance_file_refuses_secrets_and_missing_keys(tmp_path=None):
    """Case: keys are read from .env only; a 'password' in job_pilot.json blocks the start."""
    import json
    import tempfile
    tmp = Path(tmp_path or tempfile.mkdtemp())
    inst = copy.deepcopy(EXAMPLE_INSTANCE)
    inst["external_refs"]["mailbox"]["password"] = "x"
    (tmp / "i.json").write_text(json.dumps(inst))
    try:
        load_instance(tmp / "i.json")
        raise AssertionError("accepted")
    except ConfigError as exc:
        assert "secrets" in str(exc)
    del inst["external_refs"]["mailbox"]["password"]
    del inst["criteria"]
    (tmp / "i.json").write_text(json.dumps(inst))
    try:
        load_instance(tmp / "i.json")
        raise AssertionError("accepted")
    except ConfigError as exc:
        assert "criteria" in str(exc)


def test_overrides_only_on_overridable_keys():
    """Case: job_pilot.json may change the digest size, never the agents' permissions or enablement."""
    inst = dict(EXAMPLE_INSTANCE, overrides={"digest": {"top_n": 5}})
    assert merge_config(SCHEMAS["settings"], inst)["digest"] == {"top_n": 5, "review_max": 30}
    try:
        merge_config(SCHEMAS["settings"], dict(EXAMPLE_INSTANCE, overrides={"agents_enabled": {"agent009": True}}))
        raise AssertionError("accepted")
    except ConfigError:
        pass


def test_new_optional_field_is_added_to_an_existing_database():
    """Case 2026-11-21: offers.description_complete added to rows.json while job_pilot.db already existed."""
    import sqlite3
    import tempfile
    from app.agent import ensure_business_tables
    db = Path(tempfile.mkdtemp()) / "old.db"
    old = copy.deepcopy(SCHEMAS["rows"])
    del old["tables"]["offers"]["fields"]["description_complete"]
    ensure_business_tables(db, old)
    ensure_business_tables(db, SCHEMAS["rows"])
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(offers)")}
    assert "description_complete" in cols
    bad = copy.deepcopy(SCHEMAS["rows"])
    bad["tables"]["offers"]["fields"]["new_required"] = {"type": "text", "required": True}
    try:
        ensure_business_tables(db, bad)
        raise AssertionError("accepted")
    except ConfigError:
        pass


if __name__ == "__main__":
    run_main(globals())
