"""agent002 — offer collector.

Objective        : collect the offers of every enabled source, convert them to one format, merge duplicates across
                   aggregators and drop expired or already-seen offers. Log what was really queried.
Scope            : sources whose terms allow it — France Travail official API, offers you paste (manual inbox),
                   public job-board APIs of target companies (Greenhouse, Lever). Sites with unverified terms stay
                   disabled and are logged as such; LinkedIn / Indeed arrive through mail alerts (agent009).
Out of scope     : NEVER scrapes a site whose terms are unverified or that needs a login, never bypasses a
                   protection, never judges experience (agent003) nor scores (agent004).
Inputs           : search_terms of the current profile version, criteria (zones, target companies), settings.sources.
Outputs          : offers, offer_aliases, source_runs; envelope data {per_source, new, merged, discarded}.
Tools            : HTTPS (urllib, generic User-Agent, delay between requests, per-source request budget).
Data read        : search_terms, offer_inbox, offers, offer_aliases.
Data written     : offers, offer_aliases, source_runs (owner); offer_inbox.imported_run.
Success criteria : every enabled source has a source_runs line; every stored offer has the mandatory fields.
Cost limits      : 0 model call; settings.budgets.http_requests_per_source requests per source.
Time limits      : settings.timeouts_s.http_request per request.
Error policy     : one failing source does not stop the others (status partial); a source without credentials is
                   'not_configured' (no incident); nothing is invented for a missing field (left empty).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.agent.common_http import (SourceError, Http, _urllib_transport, html_to_text, robots_rules, robots_allowed)  # noqa: F401 - shared, see common_http.py
from app.agent.common_text import (LEGAL, GENDER, GENERIC_TITLE_WORDS, domain_tokens)  # noqa: F401 - shared, see common_text.py
from app.agent.source_base import (parse_salary, normalize_contract, make_offer, _title_wanted)  # noqa: F401 - see source_base.py
from app.agent.source_france_travail import (FT_TOKEN_URL, FT_SEARCH_URL, fetch_france_travail, FT_WEB, parse_ft_listing, fetch_france_travail_web, FT_DETAIL, ft_detail_fields)  # noqa: F401 - see source_france_travail.py
from app.agent.source_ats import (fetch_greenhouse, fetch_lever)  # noqa: F401 - see source_ats.py
from app.agent.source_arbeitnow import (fetch_arbeitnow)  # noqa: F401 - see source_arbeitnow.py
from app.agent import NotFound, BaseAgent, db_connect, RunContext, envelope, fold, sha, upsert, utc_now

SKILLS = [
    {"skill_id": "SK_SOURCE_FETCH", "version": "1", "status": "proposed",
     "objective": "Interroger chaque source active et journaliser ce qui a vraiment été interrogé.",
     "summary": "Pages publiques France Travail (tous les départements, texte complet de chaque offre, robots.txt "
                "respecté), API publique Arbeitnow, boîte de dépôt manuelle, API publiques Greenhouse et Lever des "
                "entreprises suivies. Une source désactivée ou non configurée est journalisée, jamais aspirée. "
                "Journal sur la page Supervision (sources interrogées)."},
    {"skill_id": "SK_NORMALIZE", "version": "1", "status": "proposed",
     "objective": "Convertir chaque offre dans un format commun.",
     "summary": "Intitulé, entreprise, lieu, contrat, salaire (min et max calculés à partir du texte), description en "
                "texte brut, dates, lien. Contrôle OFFER_MANDATORY_FIELDS : une offre sans intitulé, entreprise, "
                "description ou lien est écartée et comptée."},
    {"skill_id": "SK_DEDUP", "version": "1", "status": "proposed",
     "objective": "Fusionner les doublons entre sources, écarter les offres expirées ou déjà traitées.",
     "summary": "Clé de doublon = entreprise + intitulé + ville, sans accents, formes juridiques ni (H/F). Les autres "
                "sources sont gardées comme alias (page de l'offre, « Vue sur »). Une version au texte complet "
                "remplace un extrait. Les offres expirées sont écartées."},
]

AGENT_ID = "agent002"
CHECKS = [
    ("OFFER_MANDATORY_FIELDS", "une offre sans intitulé, entreprise, description ou lien est écartée et comptée",
     "rule of the method: an offer that cannot be judged is never stored half-empty"),
    ("ROBOTS_ALLOWED", "une adresse interdite par le robots.txt du site n'est jamais demandée",
     "2026-11-21: France Travail forbids keyword search (*motsCles=*) to robots; HelloWork, Meteojob, WTTJ forbid scraping"),
    ("SOURCE_INTERNAL_DUPLICATES", "une seule offre par identifiant externe dans une même source",
     "2026-11-21 test: France Travail returns the same offer for several title queries"),
    ("TITLE_SHORT_SKILL", "une compétence de moins de 3 lettres ne retient jamais un intitulé à elle seule",
     "2026-11-23, Arbeitnow: your skill 'c' kept 'Product Manager Insurance P&C' and C#/.NET jobs"),
    ("FT_FULL_TEXT", "une offre France Travail est jugée sur son texte complet (lu dans le JSON que charge sa page "
     "publique)",
     "2026-11-23: 944 decisions 'à revoir' because the métier pages only give an excerpt of each offer"),
    ("FT_ALL_DEPARTMENTS", "avec mobility_france, la page de chaque département de tes métiers est lue (tes zones d'abord)",
     "2026-11-23: 90 % of the offers came from Île-de-France and Haut-Rhin although you are mobile in all France"),
]


def dedup_key(company: str, title: str, location: str | None) -> str:
    c = re.sub(LEGAL, " ", fold(company))
    t = re.sub(GENDER, " ", fold(title))
    city = re.sub(r"[\d\-_,()]+", " ", fold(location or "")).split()
    parts = [re.sub(r"[^a-z0-9+#]+", " ", x).strip() for x in (c, t, city[0] if city else "")]
    return sha("|".join(re.sub(r"\s+", " ", p) for p in parts), 20)


# =========================================================================== sources
def fetch_manual(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    with ctx.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM offer_inbox WHERE imported_run IS NULL ORDER BY inbox_id")]
    out = []
    for r in rows:
        o = make_offer(ctx.words, source_id="manual", external_id=f"inbox{r['inbox_id']}", title=r["title"],
                       company=r["company"], url=r["url"], description=r["description"], location=r["location"],
                       contract=r["contract"], salary_text=r["salary_text"])
        if o:
            o["_inbox_id"] = r["inbox_id"]
        out.append(o)
    return out, 0


def text_changed(c, offer_id: str) -> None:
    """The text of an offer changed (full text read, pasted or found elsewhere): agent002 only stamps its own table;
    agent003 and agent004 see the stamp and judge the offer again (a decision YOU took is kept)."""
    c.execute("UPDATE offers SET text_updated_at=? WHERE offer_id=?", (utc_now(), offer_id))


def complete_ft_offers(ctx: RunContext, limit: int) -> dict:
    """FT_FULL_TEXT: read the full text of the France Travail offers stored with an excerpt only, newest first."""
    counts = {"completed": 0, "expired": 0, "failed": 0, "left": 0}
    with ctx.connect() as c:
        todo = [(r[0], r[1]) for r in c.execute(
            "SELECT o.offer_id, MIN(a.external_id) FROM offers o JOIN offer_aliases a ON a.offer_id=o.offer_id AND "
            "a.source_id='france_travail_web' WHERE o.description_complete=0 AND o.status='new' "
            "GROUP BY o.offer_id ORDER BY o.first_seen_at DESC", ())]
    counts["left"] = max(0, len(todo) - limit)
    if not todo or limit <= 0:
        return counts
    http = Http(ctx, "france_travail_web")
    http.left = limit + 1
    status, robots = http.request("GET", f"{FT_WEB}/robots.txt", headers={"Accept": "text/plain"})
    if status != 200:
        counts["failed"] = len(todo[:limit])
        return counts
    rules = robots_rules(robots)
    for offer_id, ext in todo[:limit]:
        path = FT_DETAIL + urllib.parse.quote(ext)
        if not re.fullmatch(r"[A-Za-z0-9]{5,12}", ext) or not robots_allowed(rules, path):   # ROBOTS_ALLOWED
            counts["failed"] += 1
            continue
        try:
            st, body = http.request("GET", FT_WEB + path, headers={"Accept": "application/json"})
        except SourceError:
            counts["failed"] += 1
            continue
        with ctx.connect() as c:
            if st in (204, 404):                 # withdrawn by the employer
                c.execute("UPDATE offers SET status='expired' WHERE offer_id=?", (offer_id,))
                counts["expired"] += 1
                continue
            try:
                d = json.loads(body) if st == 200 else None
            except ValueError:
                d = None
            if not d or not d.get("descriptif"):
                counts["failed"] += 1
                continue
            rec = ft_detail_fields(d, ctx.words)
            c.execute(f"UPDATE offers SET {', '.join(k + '=?' for k in rec)} WHERE offer_id=?",
                      [int(v) if isinstance(v, bool) else v for v in rec.values()] + [offer_id])
            text_changed(c, offer_id)
            counts["completed"] += 1
    return counts


# =========================================================================== USER_ACTIONS (called by agent000.act)
def action_add_manual_offer(orch, *, url: str, title: str, company: str, location: str = "", contract: str = "",
                            salary_text: str = "", description: str = "") -> dict:
    """An offer you paste from any site (LinkedIn, Indeed...): kept in the inbox, read at the next collection."""
    f = {k: (v or "").strip() for k, v in dict(url=url, title=title, company=company, location=location, contract=contract,
                                               salary_text=salary_text, description=description).items()}
    u = urllib.parse.urlsplit(f["url"])
    if (u.scheme not in ("http", "https") or not u.netloc or not f["title"] or not f["company"]
            or len(f["description"]) < 40 or any(len(v) > 20000 for v in f.values())):
        raise ValueError("Invalid form.")
    with db_connect(orch.db_path) as c:
        c.execute("INSERT INTO offer_inbox(url, title, company, location, contract, salary_text, description, "
                  "origin_label, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (f["url"], f["title"][:300], f["company"][:200], f["location"][:200] or None, f["contract"][:50] or None,
                   f["salary_text"][:200] or None, f["description"], u.netloc, utc_now()))
    return {"event": "user_manual_offer", "detail": f"{u.netloc}: {f['title'][:80]}"}


def action_set_full_text(orch, *, offer_id: str, text: str) -> dict:
    """ASK_FULL_TEXT: the whole offer you copied from its site replaces the excerpt; the offer is judged again."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not 200 <= len(text) <= 60000:
        raise ValueError("Paste the whole offer (200 to 60,000 characters).")
    with db_connect(orch.db_path) as c:
        if c.execute("UPDATE offers SET description=?, description_complete=1 WHERE offer_id=?", (text, offer_id)).rowcount == 0:
            raise NotFound(offer_id)
        text_changed(c, offer_id)
    return {"event": "user_full_text", "detail": f"{offer_id}: full text pasted ({len(text)} characters)"}


def action_set_offer_status(orch, *, offer_id: str, status: str) -> dict:
    """'dismissed' (you set an offer aside, only a new one) or 'applied' (you sent your application)."""
    if status not in ("dismissed", "applied"):
        raise ValueError(f"unknown status {status}")
    with db_connect(orch.db_path) as c:
        cond = " AND status='new'" if status == "dismissed" else ""
        if c.execute(f"UPDATE offers SET status=? WHERE offer_id=?{cond}", (status, offer_id)).rowcount == 0:
            raise NotFound(offer_id)
    return {"event": "user_decision" if status == "dismissed" else "offer_applied", "detail": f"{offer_id}: {status}"}


ACTIONS = {"add_manual_offer": action_add_manual_offer, "set_full_text": action_set_full_text,
           "set_offer_status": action_set_offer_status}


FETCHERS = {"manual": fetch_manual, "france_travail": fetch_france_travail, "france_travail_web": fetch_france_travail_web,
            "greenhouse": fetch_greenhouse, "lever": fetch_lever, "arbeitnow": fetch_arbeitnow}


# =========================================================================== agent
class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def terms(self, ctx: RunContext) -> dict:
        h = (ctx.previous.get("agent001") or {}).get("data", {}).get("profile_hash") or ctx.params.get("profile_hash")
        with ctx.connect() as c:
            if not h:
                row = c.execute("SELECT profile_hash FROM profile_versions ORDER BY created_at DESC LIMIT 1").fetchone()
                h = row and row[0]
            rows = c.execute("SELECT kind, value FROM search_terms WHERE profile_hash=?", (h,)).fetchall() if h else []
        out = {k: [] for k in ctx.words["term_kinds"]}
        for r in rows:
            out[r["kind"]].append(r["value"])
        return out

    def validate_input(self, ctx):
        return [] if self.terms(ctx)["title"] else ["no search terms: run agent001 first"]

    def plan(self, ctx):
        return [{"objective": f"collect {sid}", "source_id": sid, "enabled": bool(spec.get("enabled"))}
                for sid, spec in ctx.config["sources"].items()]

    def _fetch(self, ctx, terms, sub):
        sid = sub["source_id"]
        line = {"source_id": sid, "started_at": utc_now(), "queried": 0, "offers": [], "error": None, "category": None}
        if not sub["enabled"]:
            line["status"] = "disabled"
            return line
        fn = FETCHERS.get(sid)
        if fn is None:
            line.update(status="not_configured", error="pas encore de connecteur pour cette source (conditions d'utilisation à vérifier d'abord)")
            return line
        http = Http(ctx, sid)
        try:
            offers, queries = fn(ctx, http, terms)
            # SOURCE_INTERNAL_DUPLICATES (test case 2026-11-21: France Travail returns the same offer for several
            # title queries; it was counted 'already seen' once per query): one offer per external id and source.
            unique, seen = [], set()
            for o in offers:
                key = o["external_id"] if o else None
                if key is not None and key in seen:
                    continue
                seen.add(key)
                unique.append(o)
            line.update(status="succeeded", offers=unique, queried=http.count)
        except SourceError as exc:
            not_cfg = exc.category == "misconfiguration"
            line.update(status="not_configured" if not_cfg else "failed", error=str(exc), category=exc.category,
                        queried=http.count)
        return line

    def work(self, ctx: RunContext, subtasks):
        for cid, name, origin in CHECKS:
            ctx.store.register_check(cid, AGENT_ID, name, origin)
        terms = self.terms(ctx)
        workers = max(1, ctx.config["concurrency"]["sources_in_parallel"])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            lines = list(pool.map(lambda s: self._fetch(ctx, terms, s), subtasks))
        now = utc_now()
        totals = {"new": 0, "merged": 0, "already_seen": 0, "discarded": 0, "dropped_incomplete": 0}
        per_source, warnings, errors, outcomes = [], [], [], []
        with ctx.connect() as c:
            for line in lines:
                counts = {"new": 0, "merged": 0, "already_seen": 0, "discarded": 0}
                for o in line["offers"]:
                    if o is None:
                        totals["dropped_incomplete"] += 1
                        counts["discarded"] += 1
                        outcomes.append({"check": "OFFER_MANDATORY_FIELDS", "outcome": "kept_despite",
                                         "detail": f"{line['source_id']}: offer without title/company/description/link dropped"})
                        continue
                    kind = self._store(ctx, c, o, now)
                    counts[kind] += 1
                    totals[kind] += 1
                    if o.get("_inbox_id"):
                        c.execute("UPDATE offer_inbox SET imported_run=? WHERE inbox_id=?", (ctx.run_id, o["_inbox_id"]))
                status = line["status"]
                if status == "succeeded" and not line["offers"]:
                    status = "valid_empty_result"
                upsert(c, ctx.rows, "source_runs", {
                    "run_id": ctx.run_id, "source_id": line["source_id"], "status": status, "queried": line["queried"],
                    "fetched": len(line["offers"]), "new_offers": counts["new"], "merged": counts["merged"],
                    "discarded": counts["discarded"], "error": line["error"], "started_at": line["started_at"],
                    "completed_at": utc_now()})
                per_source.append({"source_id": line["source_id"], "status": status, "fetched": len(line["offers"]),
                                   **counts, "error": line["error"]})
                if status == "failed":
                    errors.append({"category": line["category"] or "source_unavailable",
                                   "message": f"{line['source_id']}: {line['error']}"})
                elif status == "not_configured":
                    warnings.append(f"{line['source_id']} not configured: {line['error']}")
        ft = ctx.config["sources"].get("france_travail_web", {})
        if ft.get("enabled") and ft.get("read_full_text", True):
            full = complete_ft_offers(ctx, int(ctx.params.get("detail_max") or ft.get("detail_max_per_run", 250)))
            totals["full_text"] = full["completed"]
            outcomes.append({"check": "FT_FULL_TEXT", "outcome": "pass" if not full["failed"] else "kept_despite",
                             "detail": f"{full['completed']} completed, {full['expired']} withdrawn, {full['failed']} "
                                       f"not read, {full['left']} left for the next runs"})
            if full["left"]:
                warnings.append(f"france_travail_web : {full['left']} offre(s) encore connue(s) par un extrait (prochains passages)")
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        active = [p for p in per_source if p["status"] not in ("disabled", "not_configured")]
        failed = [p for p in active if p["status"] == "failed"]
        if not active:
            status = "failed"
            errors.append({"category": "misconfiguration", "message": "no source could be queried"})
        elif failed:
            status = "partial" if len(failed) < len(active) else "failed"
        else:
            status = "succeeded"
        return envelope(ctx, AGENT_ID, status, data={"per_source": per_source, **totals}, warnings=warnings,
                        errors=errors, metrics=totals,
                        sources=[{"kind": "verified_fact", "ref": p["source_id"]} for p in active])

    def _store(self, ctx, c, o: dict, now: str) -> str:
        """new | merged | already_seen | discarded."""
        alias = c.execute("SELECT offer_id FROM offer_aliases WHERE source_id=? AND external_id=?",
                          (o["source_id"], o["external_id"])).fetchone()
        if alias:
            c.execute("UPDATE offers SET last_seen_at=? WHERE offer_id=?", (now, alias[0]))
            return "already_seen"
        if o.get("expires_at"):
            try:
                if datetime.fromisoformat(o["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
                    return "discarded"
            except ValueError:
                pass
        key = dedup_key(o["company"], o["title"], o["location"])
        existing = c.execute("SELECT offer_id, description, description_complete FROM offers WHERE dedup_key=?",
                             (key,)).fetchone()
        if existing:
            if existing[2] == 0 and o.get("description_complete", True) is not False:
                # FT_FULL_TEXT: the same job seen elsewhere with its full text: the full text replaces the excerpt
                c.execute("UPDATE offers SET description=?, description_complete=1 WHERE offer_id=?",
                          (o["description"], existing[0]))
                text_changed(c, existing[0])
            upsert(c, ctx.rows, "offer_aliases", {"source_id": o["source_id"], "external_id": o["external_id"],
                                                  "offer_id": existing[0], "url": o["url"], "seen_at": now})
            c.execute("UPDATE offers SET last_seen_at=? WHERE offer_id=?", (now, existing[0]))
            return "merged"
        offer_id = "o_" + sha(f"{o['source_id']}:{o['external_id']}", 14)
        rec = {k: v for k, v in o.items() if not k.startswith("_")}
        upsert(c, ctx.rows, "offers", {**rec, "offer_id": offer_id, "dedup_key": key, "status": "new",
                                       "first_seen_run": ctx.run_id, "first_seen_at": now, "last_seen_at": now})
        upsert(c, ctx.rows, "offer_aliases", {"source_id": o["source_id"], "external_id": o["external_id"],
                                              "offer_id": offer_id, "url": o["url"], "seen_at": now})
        return "new"

    def healthcheck(self, ctx=None):
        if ctx and ctx.config["sources"].get("france_travail", {}).get("enabled") and not os.environ.get("FT_CLIENT_ID"):
            return "healthy", "API France Travail non configurée (FT_CLIENT_ID absent) : les autres sources fonctionnent"
        return "healthy", "sources configurées"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
