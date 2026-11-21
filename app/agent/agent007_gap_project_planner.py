"""agent007 — gap project planner (NOT BUILT YET: step H-9 of the plan).

Objective        : aggregate over 30 days the skills missing in otherwise favourable offers and, when the 4 trigger rules hold, design a 1-2 week project brief.
Scope            : trigger: offer green on everything else, 1 or 2 missing skills, skill missing in several target offers, not covered by an existing project.
Out of scope     : NEVER publishes anything (agent008), never adds the project to your CV before it exists.
Inputs           : scores of the last 30 days, existing project briefs, profile.
Outputs          : skill_gaps, project_briefs.
Tools            : arithmetic for the trigger; configurable model (JSON only) for the brief.
Data read        : scores, offers, profile.
Data written     : skill_gaps, project_briefs (tables added at step H-9).
Success criteria : the trigger rules are computed by the program; the brief has every section (objective, architecture, tools, milestones, success criteria, pitfalls, interview pitch).
Cost limits      : settings.budgets (LLM).
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-9 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_GAP_AGGREGATE", "version": "1", "status": "proposed",
     "objective": "Aggregate missing skills over 30 days and apply the 4 trigger rules.",
     "summary": "Computed by the program, never by the model."},
    {"skill_id": "SK_PROJECT_BRIEF", "version": "1", "status": "proposed",
     "objective": "Design a realistic 1-2 week project brief from A to Z.",
     "summary": "Builds on what you already know; notified to you."},
]

AGENT_ID = "agent007"


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def work(self, ctx, subtasks):
        return envelope(ctx, AGENT_ID, "skipped", warnings=["not built yet (step H-9 of the plan)"])

    def healthcheck(self, ctx=None):
        return "disabled", "not built yet (step H-9)"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
