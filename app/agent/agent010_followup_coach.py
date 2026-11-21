"""agent010 — followup coach (NOT BUILT YET: step H-8 of the plan).

Objective        : prepare an interview sheet after a positive answer and a follow-up draft after 7-10 days without an answer.
Scope            : one application per task.
Out of scope     : NEVER sends a follow-up, never creates a calendar event without an approved approval.
Inputs           : email_events, applications, package, company sheet.
Outputs          : interview_sheets, followup_drafts, approvals (create_calendar_event).
Tools            : configurable model (JSON only).
Data read        : applications, email_events, company_facts.
Data written     : interview_sheets, followup_drafts (tables added at step H-8), approvals.
Success criteria : drafts and sheets are stored and waiting for you.
Cost limits      : settings.budgets (LLM).
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-8 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_INTERVIEW_SHEET", "version": "1", "status": "proposed",
     "objective": "Prepare an interview sheet: offer and company summary, CV sent, likely questions, weak points, questions to ask.",
     "summary": "Calendar event only after your approval."},
    {"skill_id": "SK_FOLLOWUP_DRAFT", "version": "1", "status": "proposed",
     "objective": "Propose a follow-up draft after 7-10 days without an answer.",
     "summary": "You copy and send it yourself."},
]

AGENT_ID = "agent010"


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def work(self, ctx, subtasks):
        return envelope(ctx, AGENT_ID, "skipped", warnings=["not built yet (step H-8 of the plan)"])

    def healthcheck(self, ctx=None):
        return "disabled", "not built yet (step H-8)"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
