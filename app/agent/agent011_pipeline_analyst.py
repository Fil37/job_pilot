"""agent011 — pipeline analyst (NOT BUILT YET: step H-9 of the plan).

Objective        : compute the monitoring figures (applications, answer rate, average delay, interviews vs rejections, per source and per job type) and propose new scoring weights.
Scope            : advisory agent: its failure never stops anything.
Out of scope     : NEVER changes the weights itself (approval 'apply_scoring_weights').
Inputs           : applications, email_events, scores.
Outputs          : metrics_snapshots, weights proposal (approval).
Tools            : arithmetic only.
Data read        : applications, email_events, scores.
Data written     : metrics_snapshots (table added at step H-9), approvals.
Success criteria : every figure is computed by the program.
Cost limits      : 0 model call.
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-9 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_METRICS", "version": "1", "status": "proposed",
     "objective": "Compute the monitoring figures of the job search.",
     "summary": "Per source and per job type."},
    {"skill_id": "SK_WEIGHT_TUNING", "version": "1", "status": "proposed",
     "objective": "Propose new scoring weights from the results.",
     "summary": "Applied only after your approval."},
]

AGENT_ID = "agent011"


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
