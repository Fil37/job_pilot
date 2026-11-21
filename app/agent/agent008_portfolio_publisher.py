"""agent008 — portfolio publisher (NOT BUILT YET: step H-9 of the plan).

Objective        : render a local demo of a project for the portfolio and, after your approval, publish it on the date you choose (optionally until an end date).
Scope            : one project brief at a time.
Out of scope     : NEVER publishes without an approved 'publish_portfolio_entry' approval with its exact target and dates.
Inputs           : project brief, publication date chosen by you.
Outputs          : portfolio_entries, demo HTML in the run folder.
Tools            : HTML template; portfolio target (to be defined by you).
Data read        : project_briefs.
Data written     : portfolio_entries (table added at step H-9), run folder.
Success criteria : demo rendered; publication only with an approved approval.
Cost limits      : 0 model call.
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-9 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_PORTFOLIO_RENDER", "version": "1", "status": "proposed",
     "objective": "Render a separate demo of the portfolio entry, without publishing anything.",
     "summary": "Local preview only."},
    {"skill_id": "SK_PORTFOLIO_PUBLISH", "version": "1", "status": "proposed",
     "objective": "Publish an approved entry on the chosen date, for a limited time if asked.",
     "summary": "Needs an approved approval."},
]

AGENT_ID = "agent008"


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
