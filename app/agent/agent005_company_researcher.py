"""agent005 — company researcher (NOT BUILT YET: step H-6 of the plan).

Objective        : gather sourced facts about the employer of the best offers (activity, size, news, hiring signals, public contacts).
Scope            : top N offers of the day; public pages only; one source URL per fact.
Out of scope     : NEVER contacts anyone, never scores an offer (agent004), never stores a fact without its source.
Inputs           : scores of the day (top N).
Outputs          : companies, company_facts; envelope data {companies, facts}.
Tools            : HTTPS on public pages, configurable model (JSON only) to summarise.
Data read        : offers, scores.
Data written     : companies, company_facts (tables added at step H-6).
Success criteria : every fact has a URL and a fact kind (verified_fact / estimate / missing).
Cost limits      : settings.budgets (LLM), 1 call per company.
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-6 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_COMPANY_BRIEF", "version": "1", "status": "proposed",
     "objective": "Build a sourced company sheet for the best offers.",
     "summary": "Activity, size, news, hiring signals and public contacts, each with its URL and kind (verified fact, estimate, missing). Shown in the application package."},
]

AGENT_ID = "agent005"


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def work(self, ctx, subtasks):
        return envelope(ctx, AGENT_ID, "skipped", warnings=["not built yet (step H-6 of the plan)"])

    def healthcheck(self, ctx=None):
        return "disabled", "not built yet (step H-6)"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
