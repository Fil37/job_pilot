"""Modularity guards (request of the user 2026-11-23: "est-ce qu'on est modulaire ?"): an agent never imports another
agent (shared tools live in app/agent/common_*.py); the web pages only talk to the orchestrator and read through the
common modules; every business table is written only by its owner (rows.json), or by a declared co-writer on the
declared fields; the user actions declared in routes.json are exactly the ones implemented."""
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import ROOT, make_orch, run_main  # noqa: E402

AGENT_DIR = ROOT / "app/agent"
AGENT_MOD = re.compile(r"^app\.agent\.(agent\d{3})_\w+$")


def imports(path: Path) -> list[tuple[str, list[str], int]]:
    out = []
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.append((n.module, [a.name for a in n.names], n.lineno))
        elif isinstance(n, ast.Import):
            out += [(a.name, [], n.lineno) for a in n.names]
    return out


def test_an_agent_never_imports_another_agent():
    """Shared tools are in common_*.py; the only exception is agent000's run_standalone, used to run an agent alone."""
    bad = []
    for f in sorted(AGENT_DIR.glob("agent0*.py")):
        me = f.name[:8]
        for mod, names, line in imports(f):
            m = AGENT_MOD.match(mod)
            if m and m.group(1) != me and not (me != "agent000" and m.group(1) == "agent000" and names == ["run_standalone"]):
                if me == "agent000":          # the orchestrator imports agents only through its registry / approvals
                    continue
                bad.append(f"{f.name}:{line} imports {mod} {names}")
    for f in sorted([*AGENT_DIR.glob("common_*.py"), *AGENT_DIR.glob("source_*.py")]):
        bad += [f"{f.name}:{line} imports {mod}" for mod, _n, line in imports(f) if AGENT_MOD.match(mod)]
    assert not bad, bad


def test_web_pages_only_use_the_orchestrator_and_common_modules():
    bad = []
    for f in sorted((ROOT / "app/web").glob("*.py")):
        for mod, names, line in imports(f):
            m = AGENT_MOD.match(mod)
            if m and m.group(1) != "agent000":
                bad.append(f"{f.name}:{line} imports {mod} {names}")
    assert not bad, bad


WRITE = re.compile(r'(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)(?:\s+SET\s+([a-z_]+))?'
                   r'|upsert\(\s*\w+\s*,\s*[\w.]+\s*,\s*"([a-z_]+)"')


def test_every_business_table_is_written_only_by_its_owner():
    """Owner = rows.json 'owner'; a co-writer only on its declared fields (UPDATE ... SET field); the web writes none."""
    tables = json.loads((ROOT / "app/schemas/rows.json").read_text())["tables"]
    bad = []
    files = list(AGENT_DIR.glob("*.py")) + list((ROOT / "app/web").glob("*.py"))
    for f in sorted(files):
        if f.name == "supervision.py":            # the socle's own tables (runs, tasks, incidents...)
            continue
        m = re.match(r"(agent\d{3})", f.name)
        who = m.group(1) if m else f"{f.parent.name}/{f.name}"
        for w in WRITE.finditer(f.read_text()):
            table, field = w.group(1) or w.group(3), w.group(2)
            if table not in tables or tables[table]["owner"] == who:
                continue
            allowed = tables[table].get("co_writers", {}).get(who, [])
            if not (field and field in allowed):
                bad.append(f"{f.name} writes {table}.{field or '*'} (owner {tables[table]['owner']})")
    assert not bad, bad


def test_user_actions_declared_are_exactly_the_implemented_ones():
    orch = make_orch()
    assert orch.check_actions() == []
    try:
        orch.act("agent003", "drop_everything")
        raise AssertionError("an undeclared action ran")
    except ValueError:
        pass


if __name__ == "__main__":
    run_main(globals())
