"""agent009 — inbox tracker (READ-ONLY).

Objective        : read your mailbox in read-only mode every 10-15 minutes, link the answers of recruiters to the
                   right application, classify them (acknowledgement, information request, technical test, interview,
                   rejection, offer) with a confidence, and raise alerts.
Scope            : mails of the last settings.mail.lookback_days days; only the mails linked to an application
                   (sender domain, company or offer title in the subject, thread) or with recruiting wording are kept;
                   headers + class only (the body is read in memory, never stored).
Out of scope     : NEVER writes, flags, moves, copies or deletes a mail (ReadOnlyMailbox refuses those commands);
                   never answers; never sends the full body to a model (a cleaned excerpt of at most
                   settings.mail.excerpt_chars characters, addresses / phones / links masked, only when the rules
                   are not sure); never writes interview sheets or follow-ups (agent010).
Inputs           : IMAP host / user (job_pilot.json external_refs.mailbox), IMAP_PASSWORD (.env), applications.
Outputs          : email_events rows; applications.status advanced forward; alerts (returned to agent000).
Tools            : imaplib over TLS (EXAMINE, UID SEARCH, UID FETCH BODY.PEEK); DeepSeek only for doubtful mails.
Data read        : mailbox headers + excerpt (memory only), applications, offers, email_events.
Data written     : email_events (owner), applications.status / offers.status (forward moves only).
Success criteria : MAIL_READ_ONLY holds; every kept mail has a class of the closed vocabulary and a confidence;
                   below settings.mail.min_confidence or without application → needs_check (flagged to you).
Cost limits      : 1 short model call per doubtful mail, within the run budget.
Time limits      : 60 s connection timeout; settings.mail.max_messages per run.
Error policy     : no password → 'skipped' with the reason (no incident every 15 minutes); login refused →
                   access_denied incident; a mail that cannot be parsed is skipped and counted.
"""
from __future__ import annotations

import email
import email.header
import email.utils
import html
import imaplib
import os
import quopri
import re
import sys
from datetime import datetime, timedelta, timezone

from app.agent import BaseAgent, RunContext, contains_term, envelope, fold, make_llm, sha, upsert, utc_now
from app.agent import supervision as sv

SKILLS = [
    {"skill_id": "SK_MAIL_MATCH", "version": "1", "status": "proposed",
     "objective": "Filtrer les mails et les rattacher à la bonne candidature.",
     "summary": "IMAP en lecture seule (EXAMINE + BODY.PEEK ; contrôle MAIL_READ_ONLY). Un mail n'est gardé que si le "
                "domaine de l'expéditeur, l'objet ou le fil renvoie à une candidature, ou s'il emploie un vocabulaire "
                "de recrutement. Seuls les en-têtes et la catégorie sont conservés. Mails listés sur la page de "
                "l'offre."},
    {"skill_id": "SK_MAIL_CLASSIFY", "version": "1", "status": "proposed",
     "objective": "Classer un mail avec un niveau de confiance ; les cas douteux te sont signalés.",
     "summary": "Règles d'abord (email_rules de words.json) ; DeepSeek seulement quand les règles hésitent, sur un "
                "court extrait masqué. Alerte immédiate pour un entretien, un test, une demande d'information ou une "
                "offre ; regroupée pour les accusés de réception et les refus. Alertes sur l'accueil."},
]

AGENT_ID = "agent009"
PRECEDENCE = ("offer", "interview", "technical_test", "info_request", "rejection", "acknowledgement")
IMMEDIATE = {"offer", "interview", "technical_test", "info_request"}
STATUS_ORDER = ["ready", "sent", "acknowledged", "info_request", "technical_test", "interview", "rejected", "offer"]
CATEGORY_TO_STATUS = {"acknowledgement": "acknowledged", "info_request": "info_request", "technical_test": "technical_test",
                      "interview": "interview", "rejection": "rejected", "offer": "offer"}
FREE_MAIL = {"gmail.com", "outlook.com", "hotmail.com", "hotmail.fr", "yahoo.fr", "yahoo.com", "orange.fr", "free.fr",
             "laposte.net", "icloud.com", "live.fr", "sfr.fr"}
GENERIC_SENDERS = {"greenhouse.io", "lever.co", "workday.com", "myworkday.com", "smartrecruiters.com", "teamtailor.com",
                   "teamtailor-mail.com", "welcomekit.co", "jobteaser.com", "francetravail.fr", "taleo.net",
                   "successfactors.com", "icims.com", "recruitee.com", "flatchr.io", "digitalrecruiters.com",
                   "jobaffinity.fr", "hellowork.com", "dayforce.com", "beetween-software.com", "mydjob.fr", "talentsoft.com",
                   "cornerstoneondemand.com", "softy.pro", "werecruit.io", "jobvite.com", "breezy.hr", "personio.de"}
# MAIL_UNLINKED_EVIDENCE (real case 2026-11-21: school newsletters "Simulation Entretien", "rendez-vous jeudi" were
# kept as interviews): a mail linked to no tracked application needs a recruiting sender or these subject words.
APPLICATION_WORDS = ("candidature", "application", "postul", "votre profil", "recrutement", "your application",
                     "entretien d'embauche", "offre d'emploi", "job offer", "suite a votre")


def is_recruiting_sender(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in GENERIC_SENDERS)


class ReadOnlyMailbox:
    """MAIL_READ_ONLY: the only door to the mailbox. Allowed: login, EXAMINE (select readonly), UID SEARCH,
    UID FETCH with BODY.PEEK / headers, logout. Anything else raises PermissionError before reaching the server."""

    def __init__(self, conn):
        self._c = conn

    def login(self, user, password):
        return self._c.login(user, password)

    def examine(self, mailbox="INBOX"):
        return self._c.select(mailbox, readonly=True)

    def search_since(self, day: str):
        if not re.fullmatch(r"\d{2}-[A-Z][a-z]{2}-\d{4}", day):
            raise ValueError("bad IMAP date")
        return self._c.uid("SEARCH", None, f"SINCE {day}")

    def search_uids_from(self, first: int):
        """MAIL_CURSOR: the UIDs from `first` on (only the mails not examined yet); a search, read-only."""
        return self._c.uid("SEARCH", None, f"UID {int(first)}:*")

    def uidvalidity(self) -> str:
        """The mailbox's UIDVALIDITY given by EXAMINE (a change means the UIDs were renumbered)."""
        try:
            typ, data = self._c.response("UIDVALIDITY")
        except (AttributeError, imaplib.IMAP4.error):     # no UIDVALIDITY: the cursor is simply not used
            return ""
        return (data[-1].decode() if data and data[-1] else "") if isinstance(data, list) else ""

    def fetch_peek(self, uid: bytes | str, excerpt: int):
        uid = uid.decode() if isinstance(uid, bytes) else uid
        if not re.fullmatch(r"\d+", uid):
            raise ValueError("bad uid")
        return self._c.uid("FETCH", uid, f"(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)] "
                                         f"BODY.PEEK[TEXT]<0.{int(excerpt)}>)")

    def logout(self):
        try:
            return self._c.logout()
        except Exception:  # noqa: BLE001 - closing only
            return None

    def __getattr__(self, name):            # store, copy, move, expunge, append, uid('STORE'...)...
        raise PermissionError(f"MAIL_READ_ONLY: '{name}' is not allowed")


def decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    # RAW_8BIT_HEADER (test case 2026-11-21: a UTF-8 subject not encoded per RFC 2047 gave 'unknown-8bit' and stopped
    # the whole inbox run): unknown encodings fall back to UTF-8 with replacement.
    for text, enc in email.header.decode_header(value):
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", errors="replace")
            except LookupError:
                text = text.decode("utf-8", errors="replace")
        parts.append(text)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def body_excerpt(raw: bytes, limit: int) -> str:
    """Readable text of a partial body (MIME boundaries, headers, quoted-printable, HTML removed)."""
    try:
        text = quopri.decodestring(raw).decode("utf-8", errors="replace")
    except ValueError:
        text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?im)^--[\w'()+,./:=?-]+-*$|^content-[a-z-]+:.*$|^mime-version:.*$", " ", text)
    text = re.sub(r"<style.*?</style>|<script.*?</script>", " ", text, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def mask(text: str) -> str:
    """Minimum content to the model: addresses, phone numbers and links masked."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text)
    text = re.sub(r"https?://\S+", "[lien]", text)
    return re.sub(r"(?:\+33|0)\s*[1-9](?:[\s.-]*\d{2}){4}", "[téléphone]", text)


def classify_rules(subject: str, excerpt: str, rules: dict) -> tuple[str, float, dict]:
    text = fold(subject + " \n " + excerpt)
    hits = {cat: [t for t in terms if contains_term(text, t)] for cat, terms in rules.items()}
    found = [c for c in PRECEDENCE if hits.get(c)]
    if not found:
        return "other", 0.3, hits
    best = found[0]
    if len(found) == 1:
        return best, 0.9 if len(hits[best]) > 1 else 0.8, hits
    # rejection words often come with an acknowledgement ("merci pour votre candidature, malheureusement..."):
    if set(found) == {"rejection", "acknowledgement"}:
        return "rejection", 0.85, hits
    return best, 0.55, hits


def fr_datetime(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return d.strftime("%d/%m/%Y à %Hh%M")


DATE_RX = re.compile(r"(?:le\s+)?(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\s*(?:a|à|-)?\s*(\d{1,2})\s*[h:]\s*(\d{2})?")


def proposed_datetime(text: str, received: datetime) -> str | None:
    """A date and time written as 12/12 à 14h30 in the mail (rules only; never guessed)."""
    m = DATE_RX.search(fold(text))
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    year = int(y) + (2000 if y and len(y) == 2 else 0) if y else received.year
    try:
        dt = datetime(year, int(mo), int(d), int(h), int(mi or 0))
    except ValueError:
        return None
    if dt.date() < received.date() - timedelta(days=1) and not y:
        dt = dt.replace(year=dt.year + 1)
    return dt.isoformat(timespec="minutes")


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def _mailbox_cfg(self, ctx: RunContext) -> dict:
        return (ctx.instance.get("external_refs") or {}).get("mailbox") or {}

    def connect(self, ctx: RunContext):
        """Overridable in tests (fake mailbox)."""
        cfg = self._mailbox_cfg(ctx)
        return imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)), timeout=60)

    def plan(self, ctx):
        return [{"objective": o} for o in ("read new mails (read-only)", "match to applications", "classify", "alerts")]

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("MAIL_READ_ONLY", AGENT_ID, "aucune commande autre que EXAMINE, SEARCH et FETCH BODY.PEEK",
                                 "rule of the user: the mailbox stays read-only")
        mcfg, cfg = self._mailbox_cfg(ctx), ctx.config["mail"]
        # APP_PASSWORD_SPACES (real case 2026-11-21): Google shows "abcd efgh ijkl mnop"; the spaces are not part of it
        password = os.environ.get(mcfg.get("password_env", "IMAP_PASSWORD"), "").replace(" ", "")
        if not mcfg.get("host") or not mcfg.get("user") or not password:
            return envelope(ctx, AGENT_ID, "skipped", warnings=["mailbox not configured: IMAP_PASSWORD missing in .env "
                                                                 "(Google app password), nothing read"])
        with ctx.connect() as c:
            apps = [dict(r) for r in c.execute(
                "SELECT a.application_id, a.status, a.sent_at, o.company, o.title, o.url FROM applications a "
                "JOIN offers o USING(offer_id) WHERE a.status != 'ready'")]
            known = {r[0] for r in c.execute("SELECT event_id FROM email_events")}
            threads = {r[0]: r[1] for r in c.execute("SELECT thread_key, application_id FROM email_events "
                                                     "WHERE thread_key IS NOT NULL AND application_id IS NOT NULL")}
        box = ReadOnlyMailbox(self.connect(ctx))
        events, alerts, warnings, skipped = [], [], [], 0
        client = None
        try:
            try:
                box.login(mcfg["user"], password)
            except imaplib.IMAP4.error as exc:
                return envelope(ctx, AGENT_ID, "failed", errors=[{"category": "access_denied",
                                "message": f"IMAP login refused ({sv.redact(str(exc), [password])[:120]})"}])
            mailbox = cfg.get("mailbox", "INBOX")
            box.examine(mailbox)
            # MAIL_CURSOR (real case 2026-11-25: every hourly run fetched up to 300 mails again, 5-7 min, timeouts): only
            # the mails after the last one examined are fetched, as long as the mailbox keeps its UIDVALIDITY
            uv = box.uidvalidity()
            with ctx.connect() as c:
                cur = c.execute("SELECT uidvalidity, last_uid FROM mail_cursor WHERE mailbox=?", (mailbox,)).fetchone()
            if cur and uv and cur[0] == uv:
                typ, data = box.search_uids_from(int(cur[1]) + 1)
                uids = [u for u in (data[0] or b"").split() if int(u) > int(cur[1])][-cfg["max_messages"]:]
            else:
                since = (datetime.now(timezone.utc) - timedelta(days=cfg["lookback_days"])).strftime("%d-%b-%Y")
                typ, data = box.search_since(since)
                uids = (data[0] or b"").split()[-cfg["max_messages"]:]
            for uid in uids:
                typ, parts = box.fetch_peek(uid, cfg["excerpt_chars"] * 4)
                header_raw = b"".join(p[1] for p in parts if isinstance(p, tuple) and b"HEADER" in p[0])
                body_raw = b"".join(p[1] for p in parts if isinstance(p, tuple) and b"TEXT" in p[0])
                msg = email.message_from_bytes(header_raw)
                mid = (msg.get("Message-ID") or f"uid-{uid.decode()}").strip()
                event_id = "e_" + sha(mid, 16)
                if event_id in known:
                    continue
                try:
                    ev = self._one(ctx, msg, body_raw, apps, threads, cfg, event_id)
                except (ValueError, TypeError, UnicodeError, LookupError) as exc:   # one bad mail never stops the run
                    skipped += 1
                    warnings.append(f"mail skipped: {type(exc).__name__}")
                    continue
                if ev is None:
                    continue
                if ev["needs_llm"]:
                    client = client or make_llm(ctx, AGENT_ID)
                    self._ask_model(ctx, client, ev)
                events.append(ev)
            cursor = {"mailbox": mailbox, "uidvalidity": uv, "last_uid": max(int(u) for u in uids),
                      "updated_at": utc_now()} if uids and uv else None
        finally:
            box.logout()
        with ctx.connect() as c:
            for ev in events:
                ev["needs_check"] = ev["confidence"] < cfg["min_confidence"]
                upsert(c, ctx.rows, "email_events", {k: ev[k] for k in ctx.rows["tables"]["email_events"]["fields"] if k in ev})
            if cursor:                               # MAIL_CURSOR moves only once the mails are stored
                upsert(c, ctx.rows, "mail_cursor", cursor)
                if ev["application_id"] and ev["category"] in CATEGORY_TO_STATUS and not ev["needs_check"]:
                    cur = c.execute("SELECT status FROM applications WHERE application_id=?", (ev["application_id"],)).fetchone()[0]
                    new = CATEGORY_TO_STATUS[ev["category"]]
                    if cur in STATUS_ORDER and STATUS_ORDER.index(new) > STATUS_ORDER.index(cur):
                        c.execute("UPDATE applications SET status=? WHERE application_id=?", (new, ev["application_id"]))
                label = f"« {ev['subject'][:80]} » ({ev['from_domain']})"     # the category is the badge
                untracked = not ev["application_id"] and ev["confidence"] >= cfg["min_confidence"]
                if untracked:
                    # UNTRACKED_ANSWER: a clear answer to an application sent outside job_pilot
                    alerts.append({"kind": "immediate" if ev["category"] in IMMEDIATE else "grouped",
                                   "category": ev["category"], "application_id": None, "ref": ev["event_id"],
                                   "message": f"Candidature non suivie · {label}"})
                elif ev["needs_check"]:
                    alerts.append({"kind": "immediate", "category": "to_check", "application_id": ev["application_id"],
                                   "ref": ev["event_id"], "message": f"À vérifier · {label}"})
                elif ev["category"] in IMMEDIATE:
                    when = f", date proposée le {fr_datetime(ev['proposed_datetime'])}" if ev.get("proposed_datetime") else ""
                    alerts.append({"kind": "immediate", "category": ev["category"], "application_id": ev["application_id"],
                                   "ref": ev["event_id"], "message": f"{label}{when}"})
                elif ev["category"] in ("acknowledgement", "rejection"):
                    alerts.append({"kind": "grouped", "category": ev["category"], "application_id": ev["application_id"],
                                   "ref": ev["event_id"], "message": label})
        ctx.store.record_outcomes(ctx.run_id, [{"check": "MAIL_READ_ONLY", "outcome": "pass",
                                                "detail": f"{len(uids)} mail(s) examined, read-only"}])
        counts = {c: sum(1 for e in events if e["category"] == c) for c in ctx.words["email_categories"]}
        return envelope(ctx, AGENT_ID, "succeeded", warnings=warnings,
                        data={"kept": len(events), "examined": len(uids), "alerts": alerts, "by_category": counts},
                        metrics={"examined": len(uids), "kept": len(events), "skipped": skipped,
                                 "to_check": sum(1 for e in events if e["needs_check"])})

    def _one(self, ctx, msg, body_raw, apps, threads, cfg, event_id) -> dict | None:
        subject = decode_header(msg.get("Subject"))
        name, address = email.utils.parseaddr(decode_header(msg.get("From")))
        domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
        try:
            received = email.utils.parsedate_to_datetime(msg.get("Date")) or datetime.now(timezone.utc)
        except (TypeError, ValueError):
            received = datetime.now(timezone.utc)
        refs = (msg.get("In-Reply-To") or "") + " " + (msg.get("References") or "")
        thread_ids = re.findall(r"<[^>]+>", refs) + [(msg.get("Message-ID") or "").strip()]
        excerpt = body_excerpt(body_raw, cfg["excerpt_chars"])
        # ---- link to an application
        app_id, reason = None, None
        for t in thread_ids:
            key = sha(t, 16) if t else None
            if key and key in threads:
                app_id, reason = threads[key], "thread"
                break
        if not app_id:
            text = fold(f"{name} {subject} {domain}")
            for a in apps:
                company = fold(a["company"])
                core = re.sub(r"[^a-z0-9]", "", company.split()[0]) if company.split() else ""
                dom_ok = core and len(core) >= 3 and core in domain.replace("-", "") and domain not in FREE_MAIL
                if dom_ok or (len(company) >= 3 and contains_term(text, company)) or (
                        len(a["title"]) > 12 and contains_term(fold(subject), fold(a["title"])[:40])):
                    app_id, reason = a["application_id"], "domain" if dom_ok else "company/title"
                    break
        category, conf, hits = classify_rules(subject, excerpt, ctx.words["email_rules"])
        if not app_id:
            evidence = is_recruiting_sender(domain) or any(w in fold(subject) for w in APPLICATION_WORDS)
            if category == "other" or not evidence:
                return None                   # not about your job search: nothing stored
        return {"event_id": event_id, "thread_key": sha(thread_ids[0], 16) if thread_ids and thread_ids[0] else None,
                "from_address": address, "from_domain": domain, "subject": subject[:300],
                "received_at": received.astimezone(timezone.utc).isoformat(timespec="seconds"), "application_id": app_id,
                "match_reason": reason, "category": category, "confidence": conf,
                "proposed_datetime": proposed_datetime(subject + " " + excerpt, received) if category in ("interview", "technical_test") else None,
                "classified_by": "rules", "run_id": ctx.run_id, "created_at": utc_now(),
                "needs_llm": conf < ctx.config["mail"]["min_confidence"], "_excerpt": excerpt, "_subject": subject}

    def _ask_model(self, ctx, client, ev):
        cats = ctx.words["email_categories"]
        prompt = ("Classify this mail received during a job search. Answer in JSON only, exactly: "
                  '{"category": "<one of ' + ", ".join(cats) + '>", "confidence": <0 to 1>}. '
                  "acknowledgement = application received; info_request = the recruiter asks for something; "
                  "technical_test = a test to do; interview = meeting proposed; rejection = negative answer; "
                  "offer = job offer; other = anything else.")
        content = mask(f"Subject: {ev['_subject']}\n\n{ev['_excerpt']}")[:ctx.config["mail"]["excerpt_chars"]]
        try:
            ans = client.chat_json([{"role": "system", "content": prompt}, {"role": "user", "content": content}],
                                   purpose="mail_class", run_id=ctx.run_id, task_id=ctx.task_id)
        except sv.LLMError:
            return
        cat, conf = ans.get("category"), ans.get("confidence")
        if cat in cats and isinstance(conf, (int, float)) and 0 <= conf <= 1:
            ev.update(category=cat, confidence=round(float(conf), 2), classified_by="model")

    def healthcheck(self, ctx=None):
        if ctx and not os.environ.get(self._mailbox_cfg(ctx).get("password_env", "IMAP_PASSWORD")):
            return "degraded", "IMAP_PASSWORD absent du .env : la boîte mail n'est pas encore lue"
        return "healthy", "boîte mail configurée (lecture seule)"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
