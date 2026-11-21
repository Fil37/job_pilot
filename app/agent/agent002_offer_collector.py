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

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.agent import BaseAgent, RunContext, envelope, fold, sha, upsert, utc_now

SKILLS = [
    {"skill_id": "SK_SOURCE_FETCH", "version": "1", "status": "proposed",
     "objective": "Query each enabled source and log what was really queried.",
     "summary": "France Travail API (OAuth client credentials from .env), manual inbox, Greenhouse and Lever public "
                "boards of your target companies. Disabled or unconfigured sources are logged, never scraped. "
                "Journal on the Supervision page (Sources queried)."},
    {"skill_id": "SK_NORMALIZE", "version": "1", "status": "proposed",
     "objective": "Convert every offer to one common format.",
     "summary": "Title, company, location, contract, salary (min / max computed from the text), description as "
                "plain text, dates, link. Check OFFER_MANDATORY_FIELDS: an offer without title, company, "
                "description or link is dropped and counted."},
    {"skill_id": "SK_DEDUP", "version": "1", "status": "proposed",
     "objective": "Merge duplicates across aggregators, drop expired and already-processed offers.",
     "summary": "Duplicate key = company + title + city without accents, legal suffixes and (H/F). The other "
                "sources are kept as aliases (shown on the offer page, 'Seen on'). Expired offers are discarded."},
]

AGENT_ID = "agent002"
CHECKS = [
    ("OFFER_MANDATORY_FIELDS", "an offer without title, company, description or link is dropped and counted",
     "rule of the method: an offer that cannot be judged is never stored half-empty"),
    ("SOURCE_INTERNAL_DUPLICATES", "one offer per external id inside a source",
     "2026-11-21 test: France Travail returns the same offer for several title queries"),
]
LEGAL = r"\b(sas|sasu|sa|sarl|eurl|sca|gmbh|ltd|inc|group|groupe|france)\b"
GENDER = r"\(?\b[hf]\s*/\s*[fh]\b\)?|\(?\bf\s*/\s*h\b\)?|\(?\bm\s*/\s*f\b\)?|\(?\bw\s*/\s*m\b\)?|\bh/f/x\b"


class SourceError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


# =========================================================================== HTTP
class Http:
    """Bounded HTTP client of one source: generic User-Agent, delay between requests, request budget."""

    def __init__(self, ctx: RunContext, source_id: str):
        self.cfg, self.source_id = ctx.config, source_id
        self.left = ctx.config["budgets"]["http_requests_per_source"]
        self.transport = ctx.transport or _urllib_transport
        self.last = 0.0
        self.count = 0

    def request(self, method: str, url: str, *, headers=None, data: dict | None = None) -> tuple[int, str]:
        if self.left <= 0:
            raise SourceError("budget_exhausted", f"request budget of {self.source_id} exhausted")
        wait = self.cfg["http"]["min_delay_s"] - (time.monotonic() - self.last)
        if wait > 0 and self.transport is _urllib_transport:
            time.sleep(wait)
        self.left -= 1
        self.count += 1
        self.last = time.monotonic()
        h = {"User-Agent": self.cfg["http"]["user_agent"], "Accept": "application/json", **(headers or {})}
        return self.transport(method, url, h, data, self.cfg["timeouts_s"]["http_request"])

    def json(self, method: str, url: str, **kw):
        status, body = self.request(method, url, **kw)
        if status == 204:
            return None
        if status in (401, 403):
            raise SourceError("access_denied", f"HTTP {status} on {urllib.parse.urlsplit(url).netloc}")
        if status == 404:
            raise SourceError("missing_data", f"HTTP 404 on {urllib.parse.urlsplit(url).path}")
        if status not in (200, 206):
            raise SourceError("source_unavailable", f"HTTP {status} on {urllib.parse.urlsplit(url).netloc}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError("software_failure", f"invalid JSON from {self.source_id}") from exc


def _urllib_transport(method, url, headers, data, timeout):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    if body is not None:
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:500]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError("source_unavailable", f"network: {type(exc).__name__}") from exc


# =========================================================================== normalisation
def html_to_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def parse_salary(text: str | None) -> tuple[float | None, float | None]:
    """Yearly min / max in euros computed from a salary text; (None, None) when no figure (never guessed)."""
    if not text:
        return None, None
    t = fold(text).replace(" ", " ")
    found = []
    for m in re.finditer(r"(\d{1,3}(?:[ .]\d{3})+|\d+)(?:[.,](\d+))?\s*(k€|keur|k)?(?![a-z])", t):
        whole = float(m.group(1).replace(" ", "").replace(".", ""))
        if m.group(2) and len(m.group(2)) <= 2:
            whole += float("0." + m.group(2))
        found.append((whole, bool(m.group(3))))
    any_k = any(k for _, k in found)               # "40 - 45 k€": the k applies to both bounds
    nums = [v * 1000 if (k or (any_k and v < 1000)) else v for v, k in found]
    nums = [n for n in nums if n >= 300]          # drop "13 mois", "35 h"...
    if not nums:
        return None, None
    monthly = bool(re.search(r"mensuel|par mois|/ ?mois|per month|monthly", t))
    hourly = bool(re.search(r"horaire|par heure|/ ?h\b|per hour", t))
    factor = 12 if monthly else (1820 if hourly else 1)
    if not monthly and not hourly and max(nums) < 10000:     # "3 000 €" without unit: monthly in France
        factor = 12
    vals = [round(n * factor, 2) for n in nums[:2]]
    return min(vals), max(vals)


def normalize_contract(raw: str | None, words: dict) -> str | None:
    if not raw:
        return None
    code = {"MIS": "INTERIM", "LIB": "FREELANCE", "SAI": "CDD", "DIN": "CDI"}.get(raw.strip().upper())
    if code:
        return code
    if raw.strip().upper() in words["contract_labels"]:
        return raw.strip().upper()
    f = fold(raw)
    for code, labels in words["contract_labels"].items():
        if any(re.search(rf"(?<![a-z]){re.escape(fold(lab))}(?![a-z])", f) for lab in labels):
            return code
    return None


def dedup_key(company: str, title: str, location: str | None) -> str:
    c = re.sub(LEGAL, " ", fold(company))
    t = re.sub(GENDER, " ", fold(title))
    city = re.sub(r"[\d\-_,()]+", " ", fold(location or "")).split()
    parts = [re.sub(r"[^a-z0-9+#]+", " ", x).strip() for x in (c, t, city[0] if city else "")]
    return sha("|".join(re.sub(r"\s+", " ", p) for p in parts), 20)


def make_offer(words: dict, *, source_id, external_id, title, company, url, description, location=None,
               contract=None, salary_text=None, published_at=None, expires_at=None, label_experience=None) -> dict | None:
    rec = {"source_id": source_id, "external_id": str(external_id), "title": (title or "").strip(),
           "company": (company or "").strip(), "url": (url or "").strip(), "description": (description or "").strip(),
           "location": (location or "").strip() or None, "contract": normalize_contract(contract, words),
           "salary_text": (salary_text or "").strip() or None, "published_at": published_at, "expires_at": expires_at,
           "label_experience": label_experience}
    if not all(rec[k] for k in ("title", "company", "url", "description", "external_id")):
        return None
    if rec["contract"] is None:
        rec["contract"] = normalize_contract(rec["title"], words)
    rec["salary_min"], rec["salary_max"] = parse_salary(rec["salary_text"])
    return rec


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


FT_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
FT_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"


def fetch_france_travail(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    cid, secret = os.environ.get("FT_CLIENT_ID"), os.environ.get("FT_CLIENT_SECRET")
    if not cid or not secret:
        raise SourceError("misconfiguration", "FT_CLIENT_ID / FT_CLIENT_SECRET missing in .env")
    tok = http.json("POST", FT_TOKEN_URL, data={"grant_type": "client_credentials", "client_id": cid,
                                                "client_secret": secret, "scope": "api_offresdemploiv2 o2dsoffre"})
    token = (tok or {}).get("access_token")
    if not token:
        raise SourceError("access_denied", "France Travail returned no token")
    zones = [z.get("ft_departement") for z in ctx.instance["criteria"].get("zones", []) if z.get("ft_departement")]
    queries = 0
    out = []
    for title in terms["title"][:6]:
        for dep in zones or [None]:
            params = {"motsCles": title, "range": "0-149", "publieeDepuis": "7"}
            if dep:
                params["departement"] = dep
            data = http.json("GET", FT_SEARCH_URL + "?" + urllib.parse.urlencode(params),
                             headers={"Authorization": f"Bearer {token}"})
            queries += 1
            for r in (data or {}).get("resultats", []):
                sal = (r.get("salaire") or {})
                out.append(make_offer(
                    ctx.words, source_id="france_travail", external_id=r.get("id"), title=r.get("intitule"),
                    company=(r.get("entreprise") or {}).get("nom") or "Entreprise non communiquée",
                    url=(r.get("origineOffre") or {}).get("urlOrigine")
                    or f"https://candidat.francetravail.fr/offres/recherche/detail/{r.get('id')}",
                    description=r.get("description"), location=(r.get("lieuTravail") or {}).get("libelle"),
                    contract=r.get("typeContrat"), salary_text=sal.get("libelle") or sal.get("commentaire"),
                    published_at=r.get("dateCreation"),
                    label_experience=" / ".join(x for x in (r.get("experienceExige"), r.get("experienceLibelle")) if x)))
    return out, queries


def _title_wanted(title: str, terms: dict) -> bool:
    f = fold(title)
    return any(t in f for t in terms["title"]) or any(re.search(rf"\b{re.escape(k)}\b", f) for k in terms["core_skill"])


def fetch_greenhouse(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    boards = [(c["name"], c["greenhouse"]) for c in ctx.instance["criteria"].get("target_companies", []) if c.get("greenhouse")]
    if not boards:
        raise SourceError("misconfiguration", "no target company with a greenhouse board in job_pilot.json")
    out, q = [], 0
    for name, token in boards:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", token):
            raise SourceError("misconfiguration", f"invalid greenhouse board token for {name}")
        data = http.json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        q += 1
        for j in (data or {}).get("jobs", []):
            if not _title_wanted(j.get("title", ""), terms):
                continue
            out.append(make_offer(ctx.words, source_id="greenhouse", external_id=f"{token}:{j.get('id')}",
                                  title=j.get("title"), company=name, url=j.get("absolute_url"),
                                  description=html_to_text(j.get("content", "")),
                                  location=(j.get("location") or {}).get("name"), published_at=j.get("updated_at")))
    return out, q


def fetch_lever(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    companies = [(c["name"], c["lever"]) for c in ctx.instance["criteria"].get("target_companies", []) if c.get("lever")]
    if not companies:
        raise SourceError("misconfiguration", "no target company with a lever id in job_pilot.json")
    out, q = [], 0
    for name, cid in companies:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", cid):
            raise SourceError("misconfiguration", f"invalid lever id for {name}")
        data = http.json("GET", f"https://api.lever.co/v0/postings/{cid}?mode=json")
        q += 1
        for j in data or []:
            if not _title_wanted(j.get("text", ""), terms):
                continue
            lists = "\n".join(f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in j.get("lists", []))
            desc = "\n".join(x for x in (j.get("descriptionPlain"), lists, j.get("additionalPlain")) if x)
            cat = j.get("categories") or {}
            created = j.get("createdAt")
            out.append(make_offer(ctx.words, source_id="lever", external_id=f"{cid}:{j.get('id')}", title=j.get("text"),
                                  company=name, url=j.get("hostedUrl"), description=desc, location=cat.get("location"),
                                  contract=cat.get("commitment"),
                                  published_at=datetime.fromtimestamp(created / 1000, timezone.utc).isoformat(timespec="seconds")
                                  if isinstance(created, (int, float)) else None))
    return out, q


FETCHERS = {"manual": fetch_manual, "france_travail": fetch_france_travail, "greenhouse": fetch_greenhouse,
            "lever": fetch_lever}


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
            line.update(status="not_configured", error="no connector for this source yet (terms to check first)")
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
        existing = c.execute("SELECT offer_id, description FROM offers WHERE dedup_key=?", (key,)).fetchone()
        if existing:
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
            return "healthy", "France Travail not configured (FT_CLIENT_ID missing): other sources still work"
        return "healthy", "sources configured"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
