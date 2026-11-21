"""agent010 — follow-up coach.

Objective        : after a positive answer (interview, technical test), prepare an interview sheet; after
                   settings.followup.after_days days without any answer, propose a follow-up draft; propose a
                   calendar event when a date was found in the mail.
Scope            : applications already sent; the offer, the company sheet, the CV sent (cv.txt of the package) and
                   your profile; DeepSeek writes the sheet and the draft, the program checks them.
Out of scope     : NEVER sends a follow-up or any mail, never creates a calendar event without an approved
                   'create_calendar_event' (then only an .ics file you import yourself), never classifies mails
                   (agent009), never invents a fact about you (figures and technologies checked against the profile).
Inputs           : email_events, applications, offers, companies, package folders, profile file.
Outputs          : interview_sheets (+ interview_<application>.md in the run folder), followup_drafts, approvals.
Tools            : DeepSeek (JSON only, 3 corrections at most), the fact checks of agent006.
Data read        : applications, email_events, offers, companies, profile file, package files.
Data written     : interview_sheets, followup_drafts (owner); approvals (requests only).
Success criteria : one sheet per positive answer; one draft per silent application; checks
                   SHEET_FACTS_FROM_PROFILE and FOLLOWUP_FACTS_FROM_PROFILE.
Cost limits      : 1 call per sheet or draft (+ up to 3 corrections), within the run budget.
Time limits      : settings.timeouts_s.agent_default.
Error policy     : model unavailable → the draft uses a fixed polite template, the sheet is written without the
                   model part and says so; nothing is ever sent.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent import ROOT, BaseAgent, RunContext, envelope, fold, make_llm, sha, upsert, utc_now
from app.agent.agent001_profile_builder import load_profile
from app.agent.agent006_application_builder import ask_with_corrections, company_lines, fact_problems, profile_corpus

SKILLS = [
    {"skill_id": "SK_INTERVIEW_SHEET", "version": "1", "status": "proposed",
     "objective": "Prepare an interview sheet: offer and company summary, CV sent, likely questions, weak points, questions to ask.",
     "summary": "Written after an interview or technical-test mail; figures and technologies attributed to you are "
                "checked against your profile (SHEET_FACTS_FROM_PROFILE). A date found in the mail becomes a "
                "calendar proposal (.ics) waiting for your approval. Sheet linked on the offer page."},
    {"skill_id": "SK_FOLLOWUP_DRAFT", "version": "1", "status": "proposed",
     "objective": "Propose a follow-up draft after 7-10 days without an answer.",
     "summary": "Short polite draft in French, no new fact (FOLLOWUP_FACTS_FROM_PROFILE); you copy and send it "
                "yourself. Shown on the offer page and as an approval on the Supervision page."},
]

AGENT_ID = "agent010"
SHEET_PROMPT = """You prepare a French interview sheet for a junior embedded / electronics engineer. Answer in JSON only:
{"offer_summary": "...", "company_summary": "...", "likely_questions": [{"question": "...", "hint": "..."}],
 "weak_points": [{"point": "...", "answer_elements": "..."}], "questions_to_ask": ["..."]}
Rules: French; 6 to 10 likely technical questions tied to the offer; 2 to 4 weak points taken from the missing skills
and the experience gap; answer elements and hints may ONLY use the candidate's real experiences, projects and figures
given (no new number, no technology the candidate does not have); company_summary only from the company facts given
(say "information absente" when there is none); 4 to 6 questions to ask."""
FOLLOWUP_PROMPT = """You write a short French follow-up e-mail body for a job application without answer. Answer in JSON
only: {"text": "..."}. 60 to 110 words, "vous", polite, mention the job title and the application date, restate in one
sentence the candidate's fit using ONLY the facts given, propose to send more information. No greeting line with a
name, no signature block (the candidate adds them)."""
TEMPLATE = ("Bonjour,\n\nJe me permets de revenir vers vous au sujet de ma candidature au poste de {title}, envoyée le "
            "{date}. Ce poste correspond pleinement à mon parcours en systèmes embarqués et électronique, et ma motivation "
            "pour en discuter avec vous reste entière. Je peux vous transmettre tout complément d'information utile.\n\nBien cordialement,")


def ics_event(uid: str, start: str, minutes: int, summary: str, description: str) -> str:
    """A minimal RFC 5545 event (local time, floating), escaped."""
    def esc(t):
        return t.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")
    st = datetime.fromisoformat(start)
    en = st + timedelta(minutes=minutes)
    fmt = "%Y%m%dT%H%M%S"
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//job_pilot//FR", "BEGIN:VEVENT", f"UID:{uid}@job_pilot",
                        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}Z", f"DTSTART:{st.strftime(fmt)}",
                        f"DTEND:{en.strftime(fmt)}", f"SUMMARY:{esc(summary)}", f"DESCRIPTION:{esc(description)}",
                        "END:VEVENT", "END:VCALENDAR", ""])


def approve_calendar(orch, target: str, params: dict) -> dict:
    """Handler of an APPROVED 'create_calendar_event': writes the .ics next to the interview sheet. Nothing else."""
    folder = Path(params["folder"]).resolve()
    if orch.runs_dir.resolve() not in folder.parents:
        raise ValueError("calendar file outside the runs folder")
    path = folder / f"entretien_{target}.ics"
    path.write_text(ics_event(target, params["start"], int(params.get("minutes", 60)), params["summary"],
                              params.get("description", "")), encoding="utf-8")
    return {"applied": True, "file": str(path), "note": "import this .ics into your calendar yourself"}


def approve_followup(orch, target: str, params: dict) -> dict:
    """Handler of an APPROVED 'send_followup': the draft becomes 'validated'. job_pilot never sends it."""
    from app.agent import db_connect
    with db_connect(orch.db_path) as c:
        c.execute("UPDATE followup_drafts SET status='validated' WHERE draft_id=?", (target,))
    return {"applied": True, "note": "validated: copy the text and send it yourself"}


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def plan(self, ctx: RunContext):
        cfg = ctx.config["followup"]
        now = datetime.now(timezone.utc)
        with ctx.connect() as c:
            sheets = [dict(r) for r in c.execute(
                "SELECT e.event_id, e.application_id, e.proposed_datetime, e.category FROM email_events e "
                "WHERE e.category IN ('interview','technical_test') AND e.application_id IS NOT NULL AND e.needs_check=0 "
                "AND NOT EXISTS (SELECT 1 FROM interview_sheets s WHERE s.event_id=e.event_id)")]
            silent = [dict(r) for r in c.execute(
                "SELECT a.application_id, a.sent_at FROM applications a WHERE a.status='sent' AND a.sent_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM email_events e WHERE e.application_id=a.application_id) "
                "AND NOT EXISTS (SELECT 1 FROM followup_drafts d WHERE d.application_id=a.application_id)")]
        due = [a for a in silent if now - datetime.fromisoformat(a["sent_at"]) >= timedelta(days=cfg["after_days"])]
        return ([{"objective": "interview sheet", "kind": "sheet", **s} for s in sheets] +
                [{"objective": "follow-up draft", "kind": "followup", **a} for a in due]) or [{"objective": "nothing to do", "kind": None}]

    def _context(self, ctx, application_id):
        with ctx.connect() as c:
            a = dict(c.execute("SELECT * FROM applications WHERE application_id=?", (application_id,)).fetchone())
            o = dict(c.execute("SELECT * FROM offers WHERE offer_id=?", (a["offer_id"],)).fetchone())
            s = c.execute("SELECT missing FROM scores WHERE offer_id=? ORDER BY scored_at DESC LIMIT 1", (o["offer_id"],)).fetchone()
            d = c.execute("SELECT reason FROM experience_decisions WHERE offer_id=? ORDER BY decided_at DESC LIMIT 1",
                          (o["offer_id"],)).fetchone()
        # SHEET_FOLDER (test case 2026-11-21: an application without package pointed at the runs folder itself)
        folder = ctx.run_dir.parent / a["package_ref"] if a["package_ref"] else ctx.run_dir
        cv = (folder / "cv.txt").read_text(encoding="utf-8")[:3500] if (folder / "cv.txt").is_file() else ""
        return a, o, json.loads(s[0]) if s else [], d[0] if d else "", cv, folder

    def work(self, ctx: RunContext, subtasks):
        for cid, name in (("SHEET_FACTS_FROM_PROFILE", "figures / technologies attributed to you exist in profile.yaml"),
                          ("FOLLOWUP_FACTS_FROM_PROFILE", "the follow-up draft adds no fact about you")):
            ctx.store.register_check(cid, AGENT_ID, name, "rule of the user (no invented content)")
        p = Path(ctx.instance["profile_path"])
        profile = load_profile(p if p.is_absolute() else ROOT / p)
        corpus, lexicon = profile_corpus(profile), ctx.words["skill_lexicon"]
        max_corr = ctx.config["retries"]["llm_correction_max"]
        client, outcomes, alerts, warnings = None, [], [], []
        made = {"sheets": 0, "drafts": 0, "calendar_requests": 0}
        for sub in subtasks:
            if not sub.get("kind"):
                continue
            a, o, missing, exp_reason, cv, folder = self._context(ctx, sub["application_id"])
            client = client or make_llm(ctx, AGENT_ID)
            if sub["kind"] == "sheet":
                payload = {"offer": {"title": o["title"], "company": o["company"], "text": o["description"][:2500]},
                           "company_facts": company_lines(ctx, o["company"]), "cv_sent": cv,
                           "missing_skills": missing, "experience_note": exp_reason}

                def check(ans):
                    if not isinstance(ans, dict) or not isinstance(ans.get("likely_questions"), list):
                        return ["the JSON template was not followed"]
                    own = " ".join(f"{w.get('answer_elements', '')}" for w in ans.get("weak_points") or [] if isinstance(w, dict))
                    own += " " + " ".join(f"{q.get('hint', '')}" for q in ans["likely_questions"] if isinstance(q, dict))
                    return fact_problems(own, corpus + " " + o["description"], lexicon)

                ans, probs, n = ask_with_corrections(client, SHEET_PROMPT, payload, check, purpose="interview_sheet",
                                                     ctx=ctx, max_corrections=max_corr)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"interview_{a['application_id']}.md"
                md = [f"# {o['title']} · {o['company']}", "", f"- Offre : {o['url']}",
                      f"- CV envoyé : {folder / 'cv.pdf'} (version {a['cv_version']})",
                      f"- Type de retour : {ctx.words['ui_text']['fr'].get(sub['category'], sub['category'])}"
                      + (f", date proposée le {sub['proposed_datetime'].replace('T', ' à ')}" if sub["proposed_datetime"] else ""),
                      "", "## Entreprise", ""] + company_lines(ctx, o["company"])
                if ans:
                    md += ["", "## Résumé de l'offre", "", ans.get("offer_summary", ""), "", "## Questions techniques probables", ""]
                    md += [f"- **{q.get('question', '')}** Piste : {q.get('hint', '')}" for q in ans["likely_questions"] if isinstance(q, dict)]
                    md += ["", "## Tes points faibles et éléments de réponse", ""]
                    md += [f"- **{w.get('point', '')}** : {w.get('answer_elements', '')}" for w in ans.get("weak_points") or [] if isinstance(w, dict)]
                    md += ["", "## Questions à poser", ""] + [f"- {q}" for q in ans.get("questions_to_ask") or []]
                    outcomes.append({"check": "SHEET_FACTS_FROM_PROFILE", "item": a["application_id"],
                                     "outcome": "corrected" if n else "pass"})
                else:
                    md += ["", "## Préparation", "", f"Non générée ({'; '.join(probs)[:200]}). Compétences manquantes : "
                           f"{', '.join(missing) or 'aucune'}."]
                    outcomes.append({"check": "SHEET_FACTS_FROM_PROFILE", "item": a["application_id"], "outcome": "kept_despite",
                                     "detail": "; ".join(probs)[:200]})
                path.write_text("\n".join(md) + "\n", encoding="utf-8")
                with ctx.connect() as c:
                    upsert(c, ctx.rows, "interview_sheets", {"sheet_id": "s_" + sha(sub["event_id"], 12),
                           "application_id": a["application_id"], "event_id": sub["event_id"], "path": str(path),
                           "run_id": ctx.run_id, "created_at": utc_now()})
                made["sheets"] += 1
                alerts.append({"kind": "immediate", "category": "interview_sheet", "application_id": a["application_id"],
                               "ref": str(path), "message": f"Fiche d'entretien prête : {o['title']} · {o['company']}"})
                if sub["proposed_datetime"]:
                    if ctx.store.request_approval("create_calendar_event", a["application_id"], {
                            "start": sub["proposed_datetime"], "minutes": 60, "summary": f"Entretien {o['company']} : {o['title']}",
                            "description": f"Offre : {o['url']}", "folder": str(folder)}):
                        made["calendar_requests"] += 1
            else:
                sent_day = a["sent_at"][:10]
                facts = {"title": o["title"], "company": o["company"], "application_date": sent_day,
                         "candidate_summary": profile.get("summary_variants", [""])[0]}

                def check_f(ans):
                    t = ans.get("text") if isinstance(ans, dict) else None
                    if not isinstance(t, str) or not 40 <= len(t.split()) <= 130:
                        return ["text must be 60 to 110 words"]
                    return fact_problems(t, corpus + " " + o["title"] + " " + sent_day, lexicon)

                ans, probs, n = ask_with_corrections(client, FOLLOWUP_PROMPT, facts, check_f, purpose="followup",
                                                     ctx=ctx, max_corrections=max_corr)
                text = ans["text"].strip() if ans else TEMPLATE.format(title=o["title"], date=sent_day)
                if not ans:
                    warnings.append(f"follow-up {a['application_id']}: fixed template used ({'; '.join(probs)[:100]})")
                draft_id = "f_" + sha(a["application_id"], 12)
                with ctx.connect() as c:
                    upsert(c, ctx.rows, "followup_drafts", {"draft_id": draft_id, "application_id": a["application_id"],
                           "text": text, "status": "proposed", "run_id": ctx.run_id, "created_at": utc_now()})
                ctx.store.request_approval("send_followup", draft_id, {"application": a["application_id"], "offer": o["title"],
                                           "company": o["company"], "text": text,
                                           "note": "job_pilot never sends it: approving marks it validated, you send it yourself"})
                outcomes.append({"check": "FOLLOWUP_FACTS_FROM_PROFILE", "item": draft_id,
                                 "outcome": "pass" if ans and not n else "corrected"})
                made["drafts"] += 1
                alerts.append({"kind": "grouped", "category": "followup", "application_id": a["application_id"],
                               "ref": draft_id, "message": f"Relance proposée : {o['title']} · {o['company']} (envoyée le {sent_day})"})
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        return envelope(ctx, AGENT_ID, "succeeded", warnings=warnings, data={**made, "alerts": alerts}, metrics=made)


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
