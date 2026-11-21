"""agent009 (read-only mailbox, matching, classification, alerts) and agent010 (interview sheet, calendar proposal,
follow-up draft). Fake mailbox and fake model: no network, no cost, nothing sent."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SCHEMAS, FakeWeb, env_vars, ft_offer, make_orch, run_main  # noqa: E402

from app.agent.agent009_inbox_tracker import ReadOnlyMailbox, classify_rules, mask, proposed_datetime  # noqa: E402

RULES = SCHEMAS["words"]["email_rules"]


class FakeIMAP:
    """Records every command; serves canned messages."""

    def __init__(self, messages):
        self.messages, self.calls = messages, []

    def login(self, u, p):
        self.calls.append(("login",))
        return "OK", [b""]

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + args)
        if cmd == "SEARCH":
            return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]
        if cmd == "FETCH":
            assert "BODY.PEEK" in args[1] and "STORE" not in args[1]
            m = self.messages[int(args[0]) - 1]
            return "OK", [(b"1 (BODY[HEADER.FIELDS (FROM SUBJECT)]", m["headers"].encode()),
                          (b" BODY[TEXT]<0>", m["body"].encode()), b")"]
        raise AssertionError(cmd)

    def logout(self):
        self.calls.append(("logout",))


def mail(frm, subject, body, mid, date="Mon, 17 Nov 2026 10:00:00 +0100", reply_to=""):
    h = f"From: {frm}\r\nSubject: {subject}\r\nDate: {date}\r\nMessage-ID: <{mid}>\r\n"
    if reply_to:
        h += f"In-Reply-To: <{reply_to}>\r\n"
    return {"headers": h + "\r\n", "body": body}


def test_mailbox_is_read_only():
    """Case MAIL_READ_ONLY (rule of the user): EXAMINE only; store / expunge / move / copy / append refused before
    reaching the server."""
    fake = FakeIMAP([])
    box = ReadOnlyMailbox(fake)
    box.examine("INBOX")
    assert fake.calls[-1] == ("select", "INBOX", True)
    for forbidden in ("store", "expunge", "move", "copy", "append", "uid", "select", "delete"):
        try:
            getattr(box, forbidden)
            raise AssertionError(f"{forbidden} allowed")
        except PermissionError:
            pass
    try:
        box.fetch_peek("1:*", 100)
        raise AssertionError("range accepted")
    except ValueError:
        pass


def test_rules_classify_and_detect_doubt():
    """Case: acknowledgement / interview / rejection-with-thanks; two strong categories → low confidence."""
    cat, conf, _ = classify_rules("Votre candidature", "Nous avons bien reçu votre candidature.", RULES)
    assert cat == "acknowledgement" and conf >= 0.8
    assert classify_rules("Entretien", "Seriez-vous disponible pour un entretien en visio ?", RULES)[0] == "interview"
    assert classify_rules("Candidature", "Merci pour votre candidature. Malheureusement, nous ne pourrons pas y donner "
                          "suite.", RULES)[:2] == ("rejection", 0.85)
    cat, conf, _ = classify_rules("Test", "Avant l'entretien, merci de faire ce test technique.", RULES)
    assert conf < 0.7
    assert classify_rules("Newsletter", "Les soldes commencent", RULES)[0] == "other"


def test_dates_and_masking():
    """Case: '12/12 à 14h30' becomes a proposed date; addresses, links and phones are masked for the model."""
    rec = datetime(2026, 11, 17, tzinfo=timezone.utc)
    assert proposed_datetime("Entretien le 12/12 à 14h30", rec) == "2026-12-12T14:30"
    assert proposed_datetime("Aucune date ici", rec) is None
    assert mask("Écrire à rh@acme.fr ou au 06 12 34 56 78, voir https://acme.fr/x") == \
        "Écrire à [email] ou au [téléphone], voir [lien]"


def run_inbox(messages, model_answer=None):
    with env_vars(FT_CLIENT_ID="id", FT_CLIENT_SECRET="s", IMAP_PASSWORD="app-pass", DEEPSEEK_API_KEY="k"):
        orch = make_orch(transport=FakeWeb(ft=[ft_offer(1, company="ACME SAS")]))
        orch.instance["external_refs"]["mailbox"] = {"kind": "imap", "host": "imap.example.org", "user": "me@example.org",
                                                     "password_env": "IMAP_PASSWORD"}
        orch.run("daily")
        c = sqlite3.connect(orch.db_path)
        oid = c.execute("SELECT offer_id FROM offers").fetchone()[0]
        sent = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
        with c:
            c.execute("INSERT INTO applications VALUES ('a_1', ?, 'sent', 'r0', 'x', 'france_travail', NULL, 'r0', ?, ?)",
                      (oid, sent, sent))
        fake = FakeIMAP(messages)
        from app.agent import agent009_inbox_tracker as a9, agent000_job_pilot as a0
        orig_connect, orig_ctx = a9.Agent.connect, a0.Orchestrator._ctx

        def ctx_model(self, *a, **k):
            ctx = orig_ctx(self, *a, **k)
            if model_answer:
                ctx.llm_transport = lambda url, h, body, t: (200, {"choices": [{"message": {"content": json.dumps(model_answer(body))}}],
                                                                   "usage": {"prompt_tokens": 50, "completion_tokens": 20}})
            return ctx
        a9.Agent.connect = lambda self, ctx: fake
        a0.Orchestrator._ctx = ctx_model
        try:
            r = orch.run("inbox")
        finally:
            a9.Agent.connect, a0.Orchestrator._ctx = orig_connect, orig_ctx
    return orch, r, fake, c


def test_inbox_links_classifies_alerts_and_stores_no_body():
    """Case: an interview mail from acme.fr is linked to the ACME application, its date proposed; a newsletter is
    ignored; the body is never stored; the application moves to 'interview'; an immediate alert is written by
    agent000; agent010 writes the interview sheet and asks approval for the calendar event."""
    msgs = [mail("RH ACME <rh@acme.fr>", "Votre candidature Data Engineer", "Nous souhaitons vous rencontrer en "
                 "entretien le 12/12 à 14h30 en visio.", "m1@acme.fr"),
            mail("Promo <news@shop.example>", "Soldes", "Les soldes commencent demain", "m2@shop")]

    def model(body):
        content = body["messages"][1]["content"]
        return {"offer_summary": "Poste data.", "company_summary": "information absente",
                "likely_questions": [{"question": "Pipeline Airflow ?", "hint": "ton pipeline de 2 millions de lignes"}],
                "weak_points": [{"point": "Kafka", "answer_elements": "réduction de 30 % du temps de calcul"}],
                "questions_to_ask": ["Taille de l'équipe ?"]} if "cv_sent" in content else {"category": "interview", "confidence": 0.9}
    orch, r, fake, c = run_inbox(msgs, model)
    assert r["status"] == "succeeded", r
    ev = c.execute("SELECT application_id, category, needs_check, proposed_datetime, subject FROM email_events").fetchall()
    assert ev == [("a_1", "interview", 0, "2026-12-12T14:30", "Votre candidature Data Engineer")]
    cols = {row[1] for row in c.execute("PRAGMA table_info(email_events)")}
    assert not cols & {"body", "text", "excerpt"}
    assert c.execute("SELECT status FROM applications").fetchone()[0] == "interview"
    assert c.execute("SELECT kind, category FROM alerts").fetchall()[:1] == [("immediate", "interview")]
    assert all(call[0] != "uid" or call[1] in ("SEARCH", "FETCH") for call in fake.calls)
    assert ("select", "INBOX", True) in fake.calls
    sheet = c.execute("SELECT path FROM interview_sheets").fetchone()[0]
    assert "Pipeline Airflow" in Path(sheet).read_text()
    appr = [a for a in orch.store.approvals() if a["action"] == "create_calendar_event"]
    assert len(appr) == 1 and appr[0]["parameters"]["start"] == "2026-12-12T14:30"
    out = orch.decide(appr[0]["approval_id"], True)
    assert Path(out["file"]).read_text().count("DTSTART:20261212T143000") == 1


def test_silent_application_gets_a_followup_draft_never_sent():
    """Case: sent 8 days ago, no mail → one follow-up draft (model text checked) and an approval that only marks it
    validated; the model inventing '45 %' is corrected."""
    answers = iter([{"text": "Bonjour, je reviens vers vous au sujet du poste Data Engineer. " * 2 + "J'ai réduit de 45 % un temps de calcul. " + "Merci. " * 20},
                    {"text": "Bonjour, je me permets de revenir vers vous au sujet de ma candidature au poste de Data "
                             "Engineer, envoyée récemment. Mon expérience en Python et SQL, dont un pipeline d'ingestion "
                             "Airflow, correspond à vos besoins. Je reste à votre disposition pour tout complément "
                             "d'information et serais ravi d'en discuter lors d'un échange. Bien cordialement."}])
    orch, r, fake, c = run_inbox([], lambda body: next(answers))
    d = c.execute("SELECT text, status FROM followup_drafts").fetchone()
    assert d[1] == "proposed" and "45 %" not in d[0] and "Airflow" in d[0]
    appr = [a for a in orch.store.approvals() if a["action"] == "send_followup"]
    assert len(appr) == 1
    orch.decide(appr[0]["approval_id"], True)
    assert c.execute("SELECT status FROM followup_drafts").fetchone()[0] == "validated"


def test_inbox_without_password_is_skipped_not_an_incident():
    """Case: IMAP_PASSWORD not set yet → agent009 'skipped' with the reason, no incident every 15 minutes."""
    with env_vars(IMAP_PASSWORD=None):
        orch = make_orch()
        r = orch.run("inbox")
    assert r["status"] == "succeeded"
    assert any("IMAP_PASSWORD" in w for w in r["warnings"])
    assert sqlite3.connect(orch.db_path).execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


if __name__ == "__main__":
    run_main(globals())
