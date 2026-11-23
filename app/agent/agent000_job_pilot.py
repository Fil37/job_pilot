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
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):  # launched as a script: make the project root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.agent import (DB_PATH, INSTANCE_DIR, INSTANCE_JSON, RUNS_DIR, SCHEMA_VERSION, ConfigError, RunContext,
                       db_connect, ensure_business_tables, envelope, load_instance, load_schemas, merge_config,
                       secrets_in_env, upsert, utc_now)
from app.agent import supervision as sv

AGENT_ID = "agent000"
# The ONLY modules the orchestrator may import (routes.json is checked against this list).
CHAIN_FR = {"daily": "quotidienne", "rescore": "re-classement", "inbox": "boîte mail", "weekly": "hebdomadaire",
            "application": "candidature", "portfolio": "portfolio"}


def chain_label(name: str) -> str:
    """French label of a chain name ('only:agent007' → 'seul : agent007')."""
    return "seul : " + name[5:] if name.startswith("only:") else CHAIN_FR.get(name, name)


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
# Sensitive actions applied only after YOUR approval: action → (agent module, handler function). routes.json lists them.
APPROVAL_HANDLERS = {
    "create_calendar_event": ("agent010", "approve_calendar"),
    "send_followup": ("agent010", "approve_followup"),
    "publish_portfolio_entry": ("agent008", "approve_publish"),
    "apply_scoring_weights": ("agent011", "approve_weights"),
}
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
        problems += self.check_actions()             # USER_ACTIONS declared = implemented
        checks = {}
        for aid in self.registry.agents:
            agent = self.registry.instance(aid)
            enabled = self.config["agents_enabled"].get(aid, False)
            checks[aid] = (lambda a=agent, e=enabled: a.healthcheck(self._ctx("startup", "startup", {})) if e
                           else ("disabled", "disabled in settings.agents_enabled"))
        # AGENT000_HEALTH (user question 2026-11-22: "pourquoi agent000 est inconnu"): the orchestrator checks itself
        # too (registry, database writable, runs folder writable); it was never in any health check.
        checks[AGENT_ID] = self._self_check
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

    def _self_check(self) -> tuple[str, str]:
        with db_connect(self.db_path) as c:
            c.execute("SELECT COUNT(*) FROM sup_runs").fetchone()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(self.runs_dir, os.W_OK):
            return "critical", "dossier des runs non accessible en écriture"
        return "healthy", f"registre de {len(self.registry.agents)} agents valide, base et dossier des runs accessibles en écriture"

    # ------------------------------------------------------------------ helpers
    def _ctx(self, run_id: str, task_id: str, previous: dict, params: dict | None = None,
             budget: sv.Budget | None = None) -> RunContext:
        return RunContext(run_id=run_id, task_id=task_id, db_path=self.db_path, config=self.config, schemas=self.schemas,
                          instance=self.instance, store=self.store, run_dir=self.runs_dir / run_id, budget=budget,
                          previous=previous, params=params or {}, transport=self.transport)

    def decide(self, approval_id: int, approve: bool) -> dict:
        """Your decision on a pending approval; the handler of the action is the only code that applies it."""
        handlers = {a: (lambda target, params, m=m, f=f: getattr(self.registry.module(m), f)(self, target, params))
                    for a, (m, f) in APPROVAL_HANDLERS.items()}
        return self.store.decide(approval_id, approve, handlers)

    def _write_alerts(self, env: dict) -> None:
        """Alerts returned by an agent (data.alerts) are written by agent000 only; immediate ones also go to the
        desktop notification when notify-send exists (local, nothing leaves the machine)."""
        alerts = (env.get("data") or {}).get("alerts") or []
        if not alerts:
            return
        with db_connect(self.db_path) as c:
            for a in alerts:
                upsert(c, self.schemas["rows"], "alerts", {"kind": a["kind"], "category": a["category"],
                       "application_id": a.get("application_id"), "ref": a.get("ref"), "message": a["message"][:400],
                       "created_at": utc_now(), "seen_at": None})
        exe = shutil.which("notify-send")
        for a in alerts:
            if exe and a["kind"] == "immediate":
                try:
                    subprocess.run([exe, "job_pilot", a["message"][:200]], timeout=5, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass

    # ------------------------------------------------------------------ USER_ACTIONS
    def act(self, agent_id: str, action: str, **params) -> dict:
        """USER_ACTIONS (modularity, 2026-11-23): what you do in the interface (your decision on an offer, a pasted full
        text, an edited project...) is done by the agent that OWNS the data, never by the web pages. Only actions
        declared in routes.json 'user_actions' AND in the module's ACTIONS run; each one is traced as an event.
        Raises ValueError (refused, message for you) or NotFound (unknown target)."""
        if action not in (self.schemas["routes"].get("user_actions") or {}).get(agent_id, []):
            raise ValueError(f"action {agent_id}.{action} not declared in routes.json")
        fn = (USER_ACTIONS if agent_id == AGENT_ID else getattr(self.registry.module(agent_id), "ACTIONS", {})).get(action)
        if fn is None:
            raise ValueError(f"action {agent_id}.{action} not implemented")
        result = fn(self, **params) or {}
        self.store.log_event("info", result.pop("event", f"user_{action}"), result.pop("detail", f"{agent_id}.{action}"),
                             agent_id="user")
        return result

    def check_actions(self) -> list[str]:
        """Startup check: every declared action exists, every implemented action is declared."""
        declared = self.schemas["routes"].get("user_actions") or {}
        problems = []
        for aid in [AGENT_ID, *self.registry.agents]:
            impl = set(USER_ACTIONS if aid == AGENT_ID else getattr(self.registry.module(aid), "ACTIONS", {}))
            decl = set(declared.get(aid, []))
            problems += [f"{aid}.{a}: declared in routes.json but not implemented" for a in sorted(decl - impl)]
            problems += [f"{aid}.{a}: implemented but not declared in routes.json" for a in sorted(impl - decl)]
        return problems

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
        calls = [rec for client in ctx.llm_clients for rec in client.calls]
        if calls:                                   # agent000 files the model calls of the task (socle rule)
            self.store.record_llm_calls(calls)
            env["metrics"]["llm_calls"] = len(calls)
        limit = self.config["timeouts_s"].get(aid, self.config["timeouts_s"]["agent_default"])
        if took > limit:
            env["warnings"].append(f"took {took:.0f} s (limit {limit} s)")
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
        run_id = self.store.new_run(title or f"{chain_label(chain_name)} {time.strftime('%Y-%m-%d %H:%M')}",
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
            self._write_alerts(env)
            previous[aid] = env
            statuses[aid] = env["status"]
            detail = "; ".join(e["message"] for e in env["errors"])[:400] or ", ".join(
                f"{k}={v}" for k, v in env["metrics"].items() if isinstance(v, (int, float)))[:400]
            self.store.set_task(task_id, env["status"] if env["status"] in sv.TASK_STATUSES else "failed", detail)
            if env["status"] in ("failed", "blocked") and route.get("critical"):
                halted_by = aid
        results = {a: st for a, st in statuses.items() if st not in ("skipped", "not_run")}
        results[AGENT_ID] = "succeeded"           # the orchestrator went through the whole run and wrote its report
        self.store.update_health_after_run(run_id, results)
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
                limits.append(f"{a} non exécuté (sauté) : ses contrôles n'ont pas été faits")
        for p in (previous.get("agent002") or {}).get("data", {}).get("per_source", []):
            if p["status"] in ("disabled", "not_configured"):
                limits.append(f"source {p['source_id']} non interrogée ({ {'disabled': 'désactivée', 'not_configured': 'non configurée'}.get(p['status'], p['status']) })")
        with db_connect(self.db_path) as c:
            q = [dict(r) for r in c.execute("SELECT check_id, outcome, COUNT(*) n FROM check_outcomes WHERE run_id=? "
                                            "GROUP BY check_id, outcome", (run_id,))]
        counts = {a: e["metrics"] for a, e in previous.items()}
        summary = (f"{chain_label(chain_name)} : {len(ran)} agent(s) exécuté(s), {len(failed)} en échec, "
                   f"{sum(1 for s in statuses.values() if s == 'skipped')} sauté(s), "
                   f"{sum(1 for s in statuses.values() if s == 'not_run')} non lancé(s).")
        if "agent002" in previous:
            d = previous["agent002"]["data"]
            summary += (f" Offres : {d.get('new', 0)} nouvelle(s), {d.get('merged', 0)} doublon(s) fusionné(s), "
                        f"{d.get('already_seen', 0)} déjà vue(s), {d.get('full_text', 0)} complétée(s).")
        if "agent003" in previous:
            d = previous["agent003"]["data"]
            summary += f" Expérience : {d.get('keep', 0)} gardée(s), {d.get('review', 0)} à revoir, {d.get('reject', 0)} rejetée(s)."
        next_steps = []
        if any("EXAMPLE" in w for w in warnings):
            next_steps.append("Remplace app/instance/profile.yaml par ton vrai profil.")
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
    lines = ["# Offres du jour", ""]
    for i, r in enumerate(d["top"], 1):
        lines.append(f"{i}. **{r['score']:.0f}** · {r['title']} · {r['company']} · {r['location'] or '?'} ({r['url']})")
        lines.append(f"   - pourquoi : {'; '.join(r['why'])}")
        if r["missing"]:
            lines.append(f"   - manques : {', '.join(r['missing'])}")
    lines += ["", "## À vérifier", ""]
    for r in d["review"]:
        lines.append(f"- {r['title']} · {r['company']} : {r['reason']}"
                     + (f" (« {r['source_sentence']} »)" if r["source_sentence"] else ""))
    return "\n".join(lines) + "\n"


# =========================================================================== command line
def application_params(offer_id: str, letter: bool = False) -> dict:
    """APPLICATION_PARAMS (case 2026-11-21: only agent006 received the offer, agent005 researched the top of the
    day instead of the offer's company): every agent of the application chain gets the offer."""
    return {"agent005": {"offer_id": offer_id}, "agent006": {"offer_id": offer_id, "letter": letter}}


def action_mark_alerts_seen(orch, *, alert_id: int | None = None, category: str | None = None, ref: str | None = None):
    """agent000 owns the alerts: one alert, or every unseen alert of a category and a target."""
    with db_connect(orch.db_path) as c:
        if alert_id is not None:
            c.execute("UPDATE alerts SET seen_at=? WHERE alert_id=? AND seen_at IS NULL", (utc_now(), int(alert_id)))
        elif category and ref:
            c.execute("UPDATE alerts SET seen_at=? WHERE category=? AND ref=? AND seen_at IS NULL", (utc_now(), category, ref))
        else:
            raise ValueError("an alert id, or a category and a target, is required")
    return {"event": "alert_seen", "detail": f"{alert_id or f'{category}:{ref}'}"}


USER_ACTIONS = {"mark_alerts_seen": action_mark_alerts_seen}


def run_standalone(agent_id: str) -> int:
    """`python -m app.agent.agentNNN_x [--offer ID]` : run one agent alone (its run is recorded like any other)."""
    return main(["--only", agent_id] + sys.argv[1:])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="job_pilot orchestrator")
    p.add_argument("--chain", help="daily | rescore | inbox | weekly | application")
    p.add_argument("--only", action="append", default=[], help="run only this agent (repeatable)")
    p.add_argument("--skip", action="append", default=[], help="skip this agent (recorded 'skipped')")
    p.add_argument("--list", action="store_true", help="list agents, chains and skills")
    p.add_argument("--offer", help="offer id for the application chain (agent006)")
    p.add_argument("--letter", action="store_true", help="write a cover letter even if the offer does not ask")
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
        params = application_params(a.offer, a.letter) if a.offer else None
        report = orch.run(a.chain, only=a.only, skip=a.skip, params=params)
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
