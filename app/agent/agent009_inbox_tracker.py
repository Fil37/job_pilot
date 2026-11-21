"""agent009 — inbox tracker (NOT BUILT YET: step H-8 of the plan).

Objective        : read your mailbox READ-ONLY every 10-15 minutes, link the mails to the right application and classify them with a confidence.
Scope            : mails filtered by sender, keywords and thread; classes: acknowledgement, information request, technical test, interview, rejection; job alert mails (LinkedIn, Indeed) are handed to agent002.
Out of scope     : NEVER writes, moves, flags or deletes a mail; never stores the body of a mail; sends the model only a short cleaned excerpt.
Inputs           : mailbox (IMAP EXAMINE or gmail.readonly), applications.
Outputs          : email_events, alerts.
Tools            : IMAP read-only; configurable model (JSON only, minimal content).
Data read        : mailbox metadata, applications.
Data written     : email_events, alerts (tables added at step H-8).
Success criteria : every event has a class of the closed vocabulary and a confidence; low confidence is flagged to you.
Cost limits      : settings.budgets (LLM), 1 short call per new mail.
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-8 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_MAIL_MATCH", "version": "1", "status": "proposed",
     "objective": "Filter mails and link them to the right application.",
     "summary": "Sender, keywords and thread; read-only."},
    {"skill_id": "SK_MAIL_CLASSIFY", "version": "1", "status": "proposed",
     "objective": "Classify a mail with a confidence; doubtful cases are flagged to you.",
     "summary": "Closed vocabulary; immediate alert for interviews and action requests, grouped for the rest."},
]

AGENT_ID = "agent009"


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
