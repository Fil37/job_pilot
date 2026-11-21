"""agent006 — application builder (NOT BUILT YET: step H-7 of the plan).

Objective        : build the application package of an offer you retained: tailored LaTeX CV (PDF), cover letter if needed, offer link, points of attention, company sheet.
Scope            : one offer per task, chosen by you in the interface.
Out of scope     : NEVER sends anything, never writes a figure or a technology that is not in your profile file, never edits your profile.
Inputs           : offer_id, profile file, LaTeX template (app/assets/cv_template.tex).
Outputs          : package folder in app/instance/runs/<run_id>/, applications row (status ready).
Tools            : configurable model (JSON only) to choose and adapt wordings; latexmk; pdftotext (ATS check).
Data read        : offers, scores, company_facts, profile file.
Data written     : applications, run folder.
Success criteria : CV_FACTS_FROM_PROFILE, CV_ONE_PAGE, CV_ATS_TEXT pass.
Cost limits      : settings.budgets (LLM).
Time limits      : settings.timeouts_s.agent_default.
Error policy     : until step H-7 is built, the agent is disabled (settings.agents_enabled) and returns
                   'skipped' with the reason if it is called anyway; it never pretends to have worked.
"""
from __future__ import annotations

import sys

from app.agent import BaseAgent, envelope

SKILLS = [
    {"skill_id": "SK_CV_TAILOR", "version": "1", "status": "proposed",
     "objective": "Choose projects, bullets and skills, adapt the title and profile paragraph, compile the PDF.",
     "summary": "Only wordings of your profile file are used (variants). Compiled with latexmk."},
    {"skill_id": "SK_CV_FACTCHECK", "version": "1", "status": "proposed",
     "objective": "Check that every figure and technology of the CV exists in the profile file.",
     "summary": "Check CV_FACTS_FROM_PROFILE; a failure blocks the package."},
    {"skill_id": "SK_ATS_CHECK", "version": "1", "status": "proposed",
     "objective": "Check the CV fits on one page and its text is readable by screening software.",
     "summary": "Checks CV_ONE_PAGE and CV_ATS_TEXT (pdftotext)."},
    {"skill_id": "SK_COVER_LETTER", "version": "1", "status": "proposed",
     "objective": "Write the cover letter on the same principle when the offer asks for it.",
     "summary": "Same fact check as the CV."},
]

AGENT_ID = "agent006"


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def work(self, ctx, subtasks):
        return envelope(ctx, AGENT_ID, "skipped", warnings=["not built yet (step H-7 of the plan)"])

    def healthcheck(self, ctx=None):
        return "disabled", "not built yet (step H-7)"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
