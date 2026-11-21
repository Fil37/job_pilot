"""agent000 — orchestrator of job_pilot.

Objective        : run a chain of agents (daily, rescore, inbox, weekly, application) or a single agent, in the
                   order and with the dependencies of routes.json, and write the run report.
Scope            : validate parameters and the registry, create the run id, split the chain into tasks, give each
                   specialist its context, check its result envelope, centralise errors and incidents, stop the run
                   after a critical failure (following agents 'not_run'), record skipped agents as 'skipped',
                   register the SKILLS of every agent, update health, write the report and the daily digest.
Out of scope     : NEVER does the specialists' work, never sends anything outside, never imports a module that is
                   not in ALLOWED_MODULES, never runs a command taken from a JSON value.
Inputs           : chain name or agent ids, optional parameters; schemas, job_pilot.json, settings.
Outputs          : sup_runs / sup_tasks / agent_results rows, app/instance/runs/<run_id>/report.json and digest.md.
Tools            : supervision socle (runs, tasks, incidents, health, skills, approvals, backups).
Data read        : everything (read-only for business tables).
Data written     : sup_* tables, agent_results, events, incidents, agent_health, skills, approvals; the run folder.
Success criteria : every agent of the chain has exactly one task with a final status; the report validates
                   against reports.json/run_report.
Cost limits      : the run Budget (settings.budgets) is shared by all tasks; it can only go down.
Time limits      : settings.timeouts_s.agent_default per agent (exceeded → warning + incident 'timeout').
Error policy     : exception or invalid envelope → task failed + incident (software_failure); critical agent
                   failed/blocked → the rest of the run is 'not_run'; advisory agents never stop anything.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):  # launched as a script: make the project root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.agent import (DB_PATH, INSTANCE_DIR, INSTANCE_JSON, RUNS_DIR, SCHEMA_VERSION, ConfigError, RunContext,
                       db_connect, ensure_business_tables, envelope, load_instance, load_schemas, merge_config,
                       secrets_in_env, upsert, utc_now, validate_envelope)
from app.agent import supervision as sv

AGENT_ID = "agent000"
# The ONLY modules the orchestrator may import (routes.json is checked against this list).
ALLOWED_MODULES = {
    "agent001": "app.agent.agent001_profile_builder",
    "agent002": "app.agent.agent002_offer_collector",
    "agent003": "app.agent.agent003_experience_filter",
    "agent004": "app.agent.agent004_offer_scorer",
    "agent005": "app.agent.agent005_company_researcher",
    "agent006": "app.agent.agent006_application_builder",
    "agent007": "app.agent.agent007_gap_project_planner",
    "agent008": "app.agent.agent008_portfolio_publisher",
    "agent009": "app.agent.agent009_inbox_tracker",
    "agent010": "app.agent.agent010_followup_coach",
    "agent011": "app.agent.agent011_pipeline_analyst",
}
SKILL_FIELDS = ("skill_id", "version", "status", "objective", "summary")
FINAL = ("succeeded", "partial", "failed", "skipped", "not_run", "blocked", "cancelled")


class Registry:
    """routes.json checked against ALLOWED_MODULES; modules imported only from the allow-list."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.agents = {a["agent_id"]: a for a in routes["agents"]}
        errs = []
        for aid, a in self.agents.items():
            if ALLOWED_MODULES.get(aid) != a["module"]:
                errs.append(f"routes.json: module of {aid} is not the allowed one")
            if a["entry_point"] not in routes["allowed_entry_points"]:
                errs.append(f"routes.json: entry point of {aid} not allowed")
        if errs:
            raise ConfigError("; ".join(errs))
        self._modules: dict = {}

    def module(self, agent_id: str):
        if agent_id not in ALLOWED_MODULES:
            raise ConfigError(f"agent {agent_id!r} is not allowed")
        if agent_id not in self._modules:
            self._modules[agent_id] = importlib.import_module(ALLOWED_MODULES[agent_id])
        return self._modules[agent_id]

    def instance(self, agent_id: str):
        return self.module(agent_id).Agent()

    def chain(self, name: str) -> list[str]:
        if name not in self.routes["chains"]:
            raise ConfigError(f"unknown chain {name!r} (known: {sorted(self.routes['chains'])})")
        return list(self.routes["chains"][name])


def check_skills(agent_id: str, skills) -> list[str]:
    errs = []
    if not isinstance(skills, list) or not skills:
        return [f"{agent_id}: SKILLS must be a non-empty list"]
    for sk in skills:
        missing = [f for f in SKILL_FIELDS if not sk.get(f)]
        if missing:
            errs.append(f"{agent_id}: skill {sk.get('skill_id')} misses {missing}")
        if sk.get("status") not in sv.SKILL_STATUSES:
            errs.append(f"{agent_id}: skill {sk.get('skill_id')} has an unknown status")
    return errs


class Orchestrator:
    def __init__(self, *, db_path: Path = DB_PATH, schemas: dict | None = None, instance: dict | None = None,
                 runs_dir: Path = RUNS_DIR, transport=None, backup: bool = True):
        self.schemas = schemas or load_schemas()
        self.instance = instance if instance is not None else load_instance(INSTANCE_JSON)
        self.config = merge_config(self.schemas["settings"], self.instance)
        self.registry = Registry(self.schemas["routes"])
        self.db_path, self.runs_dir, self.transport = Path(db_path), Path(runs_dir), transport
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = sv.SupervisionStore(self.db_path, secrets=secrets_in_env())
        ensure_business_tables(self.db_path, self.schemas["rows"])
        self.agent_ids = [AGENT_ID] + list(self.registry.agents)
        self.backup = backup

    # ------------------------------------------------------------------ start
    def startup(self) -> dict:
        """Register every agent's SKILLS, health checks, close runs of dead processes, daily backup."""
        problems = []
        for aid in self.registry.agents:
            mod = self.registry.module(aid)
            errs = check_skills(aid, getattr(mod, "SKILLS", None))
            if errs:
                problems += errs
                continue
            self.store.register_skills(aid, [{k: sk[k] for k in SKILL_FIELDS} for sk in mod.SKILLS])
        self.store.propose_skill_activations(only_new=True)
        checks = {}
        for aid in self.registry.agents:
            agent = self.registry.instance(aid)
            enabled = self.config["agents_enabled"].get(aid, False)
            checks[aid] = (lambda a=agent, e=enabled: a.healthcheck(self._ctx("startup", "startup", {})) if e
                           else ("disabled", "disabled in settings.agents_enabled"))
        states = self.store.startup_check(checks)
        interrupted = self.store.mark_interrupted_runs()
        if self.backup:
            try:
                self.store.backup_database(INSTANCE_DIR / "backups", "job_pilot", extra_files=[INSTANCE_JSON])
            except (OSError, RuntimeError) as exc:
                self.store.log_event("error", "backup_failed", str(exc))
        if problems:
            raise ConfigError("; ".join(problems))
        return {"health": states, "interrupted": interrupted}

    # ------------------------------------------------------------------ helpers
    def _ctx(self, run_id: str, task_id: str, previous: dict, params: dict | None = None,
             budget: sv.Budget | None = None) -> RunContext:
        return RunContext(run_id=run_id, task_id=task_id, db_path=self.db_path, config=self.config, schemas=self.schemas,
                          instance=self.instance, store=self.store, run_dir=self.runs_dir / run_id, budget=budget,
                          previous=previous, params=params or {}, transport=self.transport)

    def busy(self) -> list[str]:
        return sv.busy_runs(self.db_path)

    def _save_result(self, env: dict) -> None:
        with db_connect(self.db_path) as c:
            upsert(c, self.schemas["rows"], "agent_results", {"task_id": env["task_id"], "run_id": env["run_id"],
                   "agent_id": env["agent_id"], "status": env["status"], "result": env, "created_at": utc_now()})

    def _run_agent(self, aid: str, ctx: RunContext) -> dict:
        agent = self.registry.instance(aid)
        t0 = time.monotonic()
        try:
            env = agent.run(ctx)
            problems = agent.validate_output(ctx, env)
            if problems:
                env = envelope(ctx, aid, "failed", errors=[{"category": "software_failure", "message": p} for p in problems])
        except Exception as exc:  # noqa: BLE001 - an agent crash is recorded, never kills the orchestrator
            tb = traceback.format_exc(limit=4)
            env = envelope(ctx, aid, "failed",
                           errors=[{"category": "software_failure", "message": f"{type(exc).__name__}: {exc}"}],
                           data={"trace": sv.redact(tb, secrets_in_env())[-1500:]})
        took = time.monotonic() - t0
        env["metrics"]["duration_s"] = round(took, 3)
        if took > self.config["timeouts_s"]["agent_default"]:
            env["warnings"].append(f"took {took:.0f} s (limit {self.config['timeouts_s']['agent_default']} s)")
            self.store.open_incident(agent_id=aid, category="timeout", errors=[f"took {took:.0f} s"], run_id=ctx.run_id,
                                     task_id=ctx.task_id)
        for e in env["errors"]:
            self.store.log_event("error", f"{aid}_{e['category']}", e["message"], run_id=ctx.run_id, agent_id=aid)
            self.store.open_incident(agent_id=aid, category=e["category"], errors=[e["message"]], run_id=ctx.run_id,
                                     task_id=ctx.task_id)
        for w in env["warnings"]:
            self.store.log_event("warning", f"{aid}_warning", w, run_id=ctx.run_id, agent_id=aid)
        return env

    # ------------------------------------------------------------------ run
    def run(self, chain: str | None = None, *, only: list[str] | None = None, skip: list[str] | None = None,
            params: dict | None = None, title: str | None = None) -> dict:
        skip, only = list(skip or []), list(only or [])
        for a in skip + only:
            if a not in self.registry.agents:
                raise ConfigError(f"unknown agent {a!r}")
        members = self.registry.chain(chain) if chain else only
        if not members:
            raise ConfigError("give a chain or at least one agent")
        chain_name = chain or "only:" + ",".join(only)
        run_id = self.store.new_run(title or f"{chain_name} {time.strftime('%Y-%m-%d %H:%M')}",
                                    {"chain": chain_name, "only": only, "skip": skip, "params": params or {}})
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(run_dir, 0o700)
        budget = sv.Budget(self.config["budgets"])
        previous, statuses, halted_by = {}, {}, None
        self.store.log_event("info", "run_started", chain_name, run_id=run_id, agent_id=AGENT_ID)
        for aid in members:
            route = self.registry.agents[aid]
            reason = None
            if halted_by:
                status, reason = "not_run", f"{halted_by} (critical) failed"
            elif aid in skip:
                status, reason = "skipped", "skipped on request"
            elif only and chain and aid not in only:
                status, reason = "skipped", "not selected (--only)"
            elif not self.config["agents_enabled"].get(aid, False):
                status, reason = "skipped", "disabled in settings.agents_enabled"
            else:
                status = None
            task_id = self.store.new_task(run_id, aid, route["role"])
            if status:
                self.store.set_task(task_id, status, reason)
                statuses[aid] = status
                continue
            self.store.set_task(task_id, "running")
            ctx = self._ctx(run_id, task_id, previous, (params or {}).get(aid) or (params or {}).get("*"), budget)
            env = self._run_agent(aid, ctx)
            self._save_result(env)
            previous[aid] = env
            statuses[aid] = env["status"]
            detail = "; ".join(e["message"] for e in env["errors"])[:400] or ", ".join(
                f"{k}={v}" for k, v in env["metrics"].items() if isinstance(v, (int, float)))[:400]
            self.store.set_task(task_id, env["status"] if env["status"] in sv.TASK_STATUSES else "failed", detail)
            if env["status"] in ("failed", "blocked") and route.get("critical"):
                halted_by = aid
        self.store.update_health_after_run(run_id, {a: s for a, s in statuses.items() if s not in ("skipped", "not_run")})
        report = self.build_report(run_id, chain_name, members, statuses, previous, halted_by)
        (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        if report.get("digest"):
            (run_dir / "digest.md").write_text(digest_markdown(report["digest"]), encoding="utf-8")
        self.store.finish_run(run_id, report["status"], {"summary": report["summary"], "report": f"runs/{run_id}/report.json"})
        self.store.log_event("info", "run_finished", f"{chain_name}: {report['status']}", run_id=run_id, agent_id=AGENT_ID)
        return report

    # ------------------------------------------------------------------ report
    def build_report(self, run_id, chain_name, members, statuses, previous, halted_by) -> dict:
        ran = [a for a in members if statuses.get(a) not in ("skipped", "not_run")]
        failed = [a for a in members if statuses.get(a) in ("failed", "blocked")]
        partial = [a for a in members if statuses.get(a) == "partial"]
        if halted_by or (ran and len(failed) == len(ran)):
            status = "failed"
        elif failed or partial:
            status = "partial"
        else:
            status = "succeeded"
        warnings = [f"{a}: {w}" for a, e in previous.items() for w in e["warnings"]]
        limits = []
        for a in members:
            if statuses.get(a) == "skipped":
                limits.append(f"{a} not run (skipped): its checks were not performed")
        for p in (previous.get("agent002") or {}).get("data", {}).get("per_source", []):
            if p["status"] in ("disabled", "not_configured"):
                limits.append(f"source {p['source_id']} not queried ({p['status']})")
        with db_connect(self.db_path) as c:
            q = [dict(r) for r in c.execute("SELECT check_id, outcome, COUNT(*) n FROM check_outcomes WHERE run_id=? "
                                            "GROUP BY check_id, outcome", (run_id,))]
        counts = {a: e["metrics"] for a, e in previous.items()}
        summary = (f"{chain_name}: {len(ran)} agent(s) ran, {len(failed)} failed, "
                   f"{sum(1 for s in statuses.values() if s == 'skipped')} skipped, "
                   f"{sum(1 for s in statuses.values() if s == 'not_run')} not run.")
        if "agent002" in previous:
            d = previous["agent002"]["data"]
            summary += f" Offers: {d.get('new', 0)} new, {d.get('merged', 0)} merged duplicates, {d.get('already_seen', 0)} already seen."
        if "agent003" in previous:
            d = previous["agent003"]["data"]
            summary += f" Experience: {d.get('keep', 0)} kept, {d.get('review', 0)} to review, {d.get('reject', 0)} rejected."
        next_steps = []
        if any("EXAMPLE" in w for w in warnings):
            next_steps.append("Replace app/instance/profile.yaml with your real profile.")
        if any("france_travail" in l for l in limits):
            next_steps.append("Add FT_CLIENT_ID / FT_CLIENT_SECRET (francetravail.io) to .env to query France Travail.")
        report = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "chain": chain_name, "status": status,
                  "summary": summary,
                  "tasks_succeeded": [{"agent_id": a, "task_id": previous[a]["task_id"]} for a in members
                                      if statuses.get(a) == "succeeded"],
                  "tasks_failed": [{"agent_id": a, "task_id": previous[a]["task_id"], "errors": previous[a]["errors"]}
                                   for a in failed if a in previous],
                  "tasks_other": [{"agent_id": a, "status": statuses.get(a)} for a in members
                                  if statuses.get(a) not in ("succeeded", "failed", "blocked")],
                  "results": counts, "warnings": warnings, "costs": self.store.run_cost(run_id), "quality": q,
                  "limits": limits, "next_steps": next_steps, "created_at": utc_now()}
        if "agent004" in previous and previous["agent004"]["status"] == "succeeded":
            report["digest"] = build_digest(self.db_path, self.config["digest"], self.config["scoring"]["weights_version"])
        missing = [k for k in self.schemas["reports"]["run_report"]["required"] if k not in report]
        if missing:
            raise RuntimeError(f"report misses {missing}")
        return report


# =========================================================================== digest
def build_digest(db_path: Path, cfg: dict, weights_version: str) -> dict:
    """Best offers (kept, not eliminated, never applied/dismissed) and offers to review, for the latest profile."""
    with db_connect(db_path) as c:
        row = c.execute("SELECT profile_hash FROM profile_versions ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return {"top": [], "review": []}
        h = row[0]
        base = ("SELECT o.offer_id, o.title, o.company, o.location, o.contract, o.url, o.source_id, s.score, s.matched, "
                "s.missing, s.components, s.elimination_reasons, d.decision, d.reason_code, d.reason, d.source_sentence "
                "FROM offers o JOIN experience_decisions d ON d.offer_id=o.offer_id AND d.profile_hash=? "
                "JOIN scores s ON s.offer_id=o.offer_id AND s.profile_hash=d.profile_hash AND s.weights_version=? "
                "WHERE o.status='new' ")
        top = [dict(r) for r in c.execute(base + "AND s.eliminated=0 AND COALESCE(d.user_override, d.decision)='keep' "
                                          "ORDER BY s.score DESC LIMIT ?", (h, weights_version, cfg["top_n"]))]
        review = [dict(r) for r in c.execute(base + "AND s.eliminated=0 AND d.user_override IS NULL AND d.decision='review' "
                                             "ORDER BY s.score DESC LIMIT ?", (h, weights_version, cfg["review_max"]))]
    for r in top + review:
        for k in ("matched", "missing", "components", "elimination_reasons"):
            r[k] = json.loads(r[k])
        notes = r["components"]["notes"]
        r["why"] = [notes[k] for k in ("title", "skills", "location", "salary") if k in notes]
    return {"top": top, "review": review}


def digest_markdown(d: dict) -> str:
    lines = ["# job_pilot — offres du jour", ""]
    for i, r in enumerate(d["top"], 1):
        lines.append(f"{i}. **{r['score']:.0f}** — {r['title']} · {r['company']} · {r['location'] or '?'} — {r['url']}")
        lines.append(f"   - pourquoi : {'; '.join(r['why'])}")
        if r["missing"]:
            lines.append(f"   - manques : {', '.join(r['missing'])}")
    lines += ["", "## À vérifier", ""]
    for r in d["review"]:
        lines.append(f"- {r['title']} · {r['company']} — {r['reason_code']} : {r['reason']}"
                     + (f" — « {r['source_sentence']} »" if r["source_sentence"] else ""))
    return "\n".join(lines) + "\n"


# =========================================================================== command line
def run_standalone(agent_id: str) -> int:
    """`python -m app.agent.agentNNN_x` : run one agent alone (its run is recorded like any other)."""
    return main(["--only", agent_id])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="job_pilot orchestrator")
    p.add_argument("--chain", help="daily | rescore | inbox | weekly | application")
    p.add_argument("--only", action="append", default=[], help="run only this agent (repeatable)")
    p.add_argument("--skip", action="append", default=[], help="skip this agent (recorded 'skipped')")
    p.add_argument("--list", action="store_true", help="list agents, chains and skills")
    a = p.parse_args(argv)
    try:
        orch = Orchestrator()
        orch.startup()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if a.list:
        for aid, r in orch.registry.agents.items():
            print(f"{aid}  {r['role']:<22} enabled={orch.config['agents_enabled'].get(aid)}  critical={r['critical']}")
        print("chains:", json.dumps(orch.schemas["routes"]["chains"]))
        return 0
    if orch.busy():
        print("A run is already in progress.", file=sys.stderr)
        return 3
    try:
        report = orch.run(a.chain, only=a.only, skip=a.skip)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"{report['run_id']}  {report['status'].upper()}\n{report['summary']}")
    for t in report["tasks_failed"]:
        for e in t["errors"]:
            print(f"  FAILED {t['agent_id']}: [{e['category']}] {e['message']}")
    for l in report["limits"]:
        print(f"  limit: {l}")
    for w in report["warnings"]:
        print(f"  warning: {w}")
    if report.get("digest"):
        print(f"  digest: {len(report['digest']['top'])} top offers, {len(report['digest']['review'])} to review "
              f"→ app/instance/runs/{report['run_id']}/digest.md")
    return 0 if report["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
