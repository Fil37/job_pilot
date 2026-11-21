"""job_pilot — shared helpers of the agents (no business logic here).

- paths of the project (everything lives under app/);
- loading and validation of the five schemas of app/schemas/ and of the instance file;
- creation of the business tables from rows.json (one owner agent per table);
- the run context given to every agent, the result envelope (reports.json/result_envelope)
  and the base class of the specialised agents (validate_input / plan / run / validate_output / healthcheck);
- text helpers shared by several agents (accent-free matching).

The supervision base of the Studio Kit lives next to this file (supervision.py) and is imported here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.agent import supervision as sv

APP_DIR = Path(__file__).resolve().parents[1]
ROOT = APP_DIR.parent
SCHEMAS_DIR = APP_DIR / "schemas"
INSTANCE_DIR = APP_DIR / "instance"
RUNS_DIR = INSTANCE_DIR / "runs"
DB_PATH = INSTANCE_DIR / "job_pilot.db"
INSTANCE_JSON = INSTANCE_DIR / "job_pilot.json"
SCHEMA_NAMES = ("reports", "routes", "rows", "settings", "words")
SCHEMA_VERSION = "1"
SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "json": "TEXT", "bool": "INTEGER"}

try:  # keys (FT_CLIENT_ID...) come from .env, never from a versioned file
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv missing: environment variables only
    pass

SECRET_ENV = ("FT_CLIENT_ID", "FT_CLIENT_SECRET", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "IMAP_PASSWORD", "FLASK_SECRET_KEY")


def secrets_in_env() -> list[str]:
    """Values to mask in logs. REDACT_MIN_LENGTH (case 2026-11-21: a 1-letter test value turned every 's' of a
    trace into '***'): values shorter than 8 characters are not real secrets and are not masked."""
    return [os.environ[k] for k in SECRET_ENV if len(os.environ.get(k) or "") >= 8]


def utc_now() -> str:
    return sv.now_iso()


# =========================================================================== schemas
class ConfigError(ValueError):
    """A schema or the instance file is invalid: the program refuses to start (misconfiguration)."""


def load_schemas(folder: Path = SCHEMAS_DIR) -> dict:
    out = {}
    for name in SCHEMA_NAMES:
        path = folder / f"{name}.json"
        try:
            out[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{path.name}: {exc}") from exc
    validate_schemas(out)
    return out


def validate_schemas(s: dict) -> None:
    """Checks the contracts at start (skill §9: validate all these files at start)."""
    errs = []
    for name in SCHEMA_NAMES:
        if s.get(name, {}).get("schema_version") != SCHEMA_VERSION:
            errs.append(f"{name}.json: schema_version must be {SCHEMA_VERSION!r}")
    agent_ids = [a.get("agent_id") for a in s["routes"].get("agents", [])]
    if len(agent_ids) != len(set(agent_ids)):
        errs.append("routes.json: duplicate agent_id")
    owners = set(agent_ids) | {"agent000", "web"}
    for tname, t in s["rows"].get("tables", {}).items():
        if t.get("owner") not in owners:
            errs.append(f"rows.json: table {tname} has unknown owner {t.get('owner')!r}")
        for f in t.get("primary_key", []):
            if f not in t.get("fields", {}):
                errs.append(f"rows.json: {tname} primary key {f} is not a field")
        for fname, spec in t.get("fields", {}).items():
            if spec.get("type") not in SQL_TYPES:
                errs.append(f"rows.json: {tname}.{fname} has unknown type {spec.get('type')!r}")
            ref = spec.get("enum_ref")
            if ref and ref.split(".", 1)[1] not in s["words"]:
                errs.append(f"rows.json: {tname}.{fname} enum_ref {ref} missing from words.json")
    for a in s["routes"].get("agents", []):
        if a.get("entry_point") not in s["routes"].get("allowed_entry_points", []):
            errs.append(f"routes.json: {a.get('agent_id')} entry point {a.get('entry_point')!r} not allowed")
        for d in a.get("depends_on", []):
            if d not in agent_ids:
                errs.append(f"routes.json: {a.get('agent_id')} depends on unknown {d}")
    for chain, members in s["routes"].get("chains", {}).items():
        for m in members:
            if m not in agent_ids:
                errs.append(f"routes.json: chain {chain} names unknown agent {m}")
    defaults = s["settings"].get("defaults", {})
    for key in ("agents_enabled", "budgets", "scoring", "sources", "schedule", "digest", "experience"):
        if key not in defaults:
            errs.append(f"settings.json: defaults.{key} missing")
    w = defaults.get("scoring", {}).get("weights", {})
    if w and abs(sum(w.values()) - 1.0) > 1e-6:
        errs.append("settings.json: scoring weights must sum to 1")
    if "fr" not in s["words"].get("ui_text", {}):
        errs.append("words.json: ui_text.fr missing")
    if errs:
        raise ConfigError("; ".join(errs))


def load_instance(path: Path = INSTANCE_JSON) -> dict:
    if not path.is_file():
        raise ConfigError(f"{path.name} missing: copy app/assets/job_pilot.example.json and fill it")
    try:
        inst = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    errs = [f"{path.name}: {k} missing" for k in ("instance_id", "config_version", "criteria", "profile_path")
            if k not in inst]
    text = json.dumps(inst).lower()
    if any(w in text for w in ('"password"', '"api_key"', '"secret"', '"client_secret"')):
        errs.append(f"{path.name}: secrets are not allowed here (use .env)")
    if errs:
        raise ConfigError("; ".join(errs))
    return inst


def merge_config(settings: dict, instance: dict) -> dict:
    """Defaults of settings.json, overridden by instance['overrides'] for the keys listed in 'overridable' only."""
    cfg = json.loads(json.dumps(settings["defaults"]))
    for key, value in (instance.get("overrides") or {}).items():
        if key not in settings.get("overridable", []):
            raise ConfigError(f"job_pilot.json: overrides.{key} is not overridable")
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            for k, v in value.items():
                if isinstance(v, dict) and isinstance(cfg[key].get(k), dict):
                    cfg[key][k] = {**cfg[key][k], **v}
                else:
                    cfg[key][k] = v
        else:
            cfg[key] = value
    w = cfg["scoring"]["weights"]
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ConfigError("scoring weights must sum to 1")
    return cfg


# =========================================================================== database
def db_connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def ensure_business_tables(db_path: str | Path, rows: dict) -> None:
    with db_connect(db_path) as c:
        for tname, t in rows["tables"].items():
            cols = []
            for fname, spec in t["fields"].items():
                col = f"{fname} {SQL_TYPES[spec['type']]}"
                if t.get("autoincrement") and t["primary_key"] == [fname]:
                    col += " PRIMARY KEY AUTOINCREMENT"
                elif spec.get("required"):
                    col += " NOT NULL"
                cols.append(col)
            if not t.get("autoincrement"):
                cols.append(f"PRIMARY KEY ({', '.join(t['primary_key'])})")
            for u in t.get("unique", []):
                cols.append(f"UNIQUE ({', '.join(u)})")
            c.execute(f"CREATE TABLE IF NOT EXISTS {tname} ({', '.join(cols)})")
            # schema evolution: a new OPTIONAL field of rows.json is added to an existing table (never dropped)
            have = {r[1] for r in c.execute(f"PRAGMA table_info({tname})")}
            for fname, spec in t["fields"].items():
                if fname not in have:
                    if spec.get("required"):
                        raise ConfigError(f"rows.json: new required field {tname}.{fname} needs a migration")
                    c.execute(f"ALTER TABLE {tname} ADD COLUMN {fname} {SQL_TYPES[spec['type']]}")
    if str(db_path) != ":memory:" and Path(db_path).exists():
        os.chmod(db_path, 0o600)


def encode_row(rows: dict, table: str, record: dict) -> dict:
    """Checks a record against rows.json (required fields, known fields) and encodes json/bool columns."""
    spec = rows["tables"][table]["fields"]
    unknown = set(record) - set(spec)
    if unknown:
        raise ValueError(f"{table}: unknown fields {sorted(unknown)}")
    out = {}
    for fname, f in spec.items():
        v = record.get(fname)
        if v is None:
            if f.get("required") and not (rows["tables"][table].get("autoincrement")
                                          and rows["tables"][table]["primary_key"] == [fname]):
                raise ValueError(f"{table}.{fname} is required")
            if fname in record:
                out[fname] = None
            continue
        if f["type"] == "json":
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        elif f["type"] == "bool":
            v = int(bool(v))
        out[fname] = v
    return out


def upsert(conn: sqlite3.Connection, rows: dict, table: str, record: dict) -> None:
    enc = encode_row(rows, table, record)
    cols = list(enc)
    pk = rows["tables"][table]["primary_key"]
    updates = [c for c in cols if c not in pk]
    sql = f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    if updates:
        sql += f" ON CONFLICT({', '.join(pk)}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)
    else:
        sql += " ON CONFLICT DO NOTHING"
    conn.execute(sql, [enc[c] for c in cols])


def decode_row(rows: dict, table: str, row: sqlite3.Row | dict) -> dict:
    spec = rows["tables"][table]["fields"]
    out = dict(row)
    for k, v in list(out.items()):
        if k in spec and v is not None:
            if spec[k]["type"] == "json":
                out[k] = json.loads(v)
            elif spec[k]["type"] == "bool":
                out[k] = bool(v)
    return out


# =========================================================================== run context and results
@dataclass
class RunContext:
    run_id: str
    task_id: str
    db_path: Path
    config: dict
    schemas: dict
    instance: dict
    store: "sv.SupervisionStore"
    run_dir: Path
    budget: "sv.Budget | None" = None
    previous: dict = field(default_factory=dict)        # {agent_id: envelope} of this run (run memory)
    params: dict = field(default_factory=dict)          # parameters of this task (e.g. offer_id)
    transport: object = None                            # injectable HTTP transport (tests: no network)
    llm_transport: object = None                        # injectable model transport (tests: no network, no cost)
    llm_clients: list = field(default_factory=list)     # clients created in this task; agent000 records their calls

    @property
    def words(self) -> dict:
        return self.schemas["words"]

    @property
    def rows(self) -> dict:
        return self.schemas["rows"]

    def connect(self) -> sqlite3.Connection:
        return db_connect(self.db_path)


def envelope(ctx: RunContext, agent_id: str, status: str, *, data=None, artifact_refs=(), sources=(), warnings=(),
             errors=(), metrics=None, started_at: str | None = None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "task_id": ctx.task_id, "agent_id": agent_id,
            "status": status, "data": data if data is not None else {}, "artifact_refs": list(artifact_refs),
            "sources": list(sources), "warnings": list(warnings), "errors": list(errors), "metrics": metrics or {},
            "started_at": started_at or utc_now(), "completed_at": utc_now()}


def validate_envelope(env: dict, reports: dict) -> list[str]:
    spec = reports["result_envelope"]
    errs = [f"result: {k} missing" for k in spec["required"] if k not in env]
    if env.get("status") not in spec["status_values"]:
        errs.append(f"result: status {env.get('status')!r} not allowed")
    for e in env.get("errors", []):
        if not isinstance(e, dict) or e.get("category") not in sv.ERROR_CATEGORIES:
            errs.append(f"result: error item without a known category: {e!r}"[:200])
    return errs


class BaseAgent:
    """Contract of every specialised agent. Subclasses set AGENT_ID, SKILLS and implement work()."""

    AGENT_ID = "agentNNN"
    SKILLS: list[dict] = []

    def validate_input(self, ctx: RunContext) -> list[str]:
        return []

    def plan(self, ctx: RunContext) -> list[dict]:
        """Bounded sub-tasks: [{"objective": ...}]. A sub-task is a task (sup_tasks), not a new agent."""
        return [{"objective": f"{self.AGENT_ID}: whole mission"}]

    def work(self, ctx: RunContext, subtasks: list[dict]) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, ctx: RunContext) -> dict:
        started = utc_now()
        problems = self.validate_input(ctx)
        if problems:
            return envelope(ctx, self.AGENT_ID, "blocked", started_at=started,
                            errors=[{"category": "missing_data", "message": p} for p in problems])
        env = self.work(ctx, self.plan(ctx))
        env["started_at"] = started
        return env

    def validate_output(self, ctx: RunContext, env: dict) -> list[str]:
        return validate_envelope(env, ctx.schemas["reports"])

    def healthcheck(self, ctx: RunContext | None = None) -> tuple[str, str]:
        return "healthy", "no external dependency"


# =========================================================================== text helpers
def fold(text: str) -> str:
    """Lower case without accents, typographic apostrophes unified, spaces collapsed (for matching only)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("`", "'").lower()
    return re.sub(r"\s+", " ", text).strip()


def contains_term(folded_text: str, term: str) -> bool:
    """Whole-word match of a term in a folded text. "c" does not match "c++", "c#" or "c'est"."""
    t = fold(term).strip(" ,")
    if not t:
        return False
    return re.search(rf"(?<![a-z0-9+#]){re.escape(t)}(?![a-z0-9+#'])", folded_text) is not None


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+|\s[•\-–·]\s", text or "")
    return [p.strip(" •-–·\t") for p in parts if p and p.strip(" •-–·\t")]


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def month_index(value: str) -> int:
    """'2024-09' → months since year 0 (for durations); 'present' → current month."""
    if value in ("present", "now", "aujourd'hui", None, ""):
        d = datetime.now(timezone.utc)
        return d.year * 12 + d.month - 1
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(value))
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"date {value!r} must be YYYY-MM or 'present'")
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


# =========================================================================== model client (DeepSeek, socle LLMClient)
def make_llm(ctx: "RunContext", agent_id: str, transport=None) -> "sv.LLMClient":
    """The run's model client: ONE provider and model from settings.llm, the run Budget, no automatic fallback.
    Raises LLMError('misconfiguration') at the first call when the key is missing (never invented)."""
    llm = ctx.config["llm"]
    pricing = llm.get("pricing_usd_per_million") or {}
    provider = {"name": llm["provider"], "base_url": llm["base_url"], "chat_path": llm.get("chat_path", "/chat/completions"),
                "api_key": os.environ.get(llm["api_key_env"], ""), "pricing": pricing}
    model = {"model": llm["model"], "max_tokens": llm.get("max_tokens", 2000), "temperature": llm.get("temperature", 0.0),
             "thinking": llm.get("thinking")}
    client = sv.LLMClient(provider, model, agent_id, timeout=llm.get("timeout_s", 120), budget=ctx.budget,
                          transport=transport or ctx.llm_transport)
    ctx.llm_clients.append(client)
    return client
