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
            raise SourceError("budget_exhausted", f"budget de requêtes de {self.source_id} épuisé")
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
            raise SourceError("access_denied", f"HTTP {status} sur {urllib.parse.urlsplit(url).netloc}")
        if status == 404:
            raise SourceError("missing_data", f"HTTP 404 sur {urllib.parse.urlsplit(url).path}")
        if status not in (200, 206):
            raise SourceError("source_unavailable", f"HTTP {status} sur {urllib.parse.urlsplit(url).netloc}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError("software_failure", f"JSON invalide reçu de {self.source_id}") from exc


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
        raise SourceError("source_unavailable", f"réseau : {type(exc).__name__}") from exc


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
        raise SourceError("misconfiguration", "FT_CLIENT_ID / FT_CLIENT_SECRET absents du .env")
    tok = http.json("POST", FT_TOKEN_URL, data={"grant_type": "client_credentials", "client_id": cid,
                                                "client_secret": secret, "scope": "api_offresdemploiv2 o2dsoffre"})
    token = (tok or {}).get("access_token")
    if not token:
        raise SourceError("access_denied", "France Travail n'a renvoyé aucun jeton")
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


GENERIC_TITLE_WORDS = {"ingenieur", "engineer", "developpeur", "developer", "concepteur", "conception", "test", "tests",
                       "et", "de", "des", "en", "logiciel", "software", "systeme", "systemes", "system", "design",
                       "developpement", "validation", "essais", "h/f", "f/h", "senior", "junior", "lead",
                       # TITLE_DOMAIN_TOKENS (real case 2026-11-23, Arbeitnow: "Digital Designer" and "digital marketing"
                       # kept through 'digital design engineer'): words that also name marketing / IT transformation jobs
                       "digital", "numerique"}


def domain_tokens(titles: list[str]) -> set[str]:
    """Distinctive words of your titles: 'electronique', 'embarque', 'fpga', 'firmware', 'hardware'..."""
    words = {w.rstrip("s") for t in titles for w in re.split(r"[^a-z0-9+#]+", fold(t)) if len(w) >= 4}
    return words - {g.rstrip("s") for g in GENERIC_TITLE_WORDS}


def _title_wanted(title: str, terms: dict) -> bool:
    # TITLE_DOMAIN_TOKENS (real case 2026-11-21, Alice & Bob "Electronics Integration Engineer" was skipped because
    # no full title such as "electronics engineer" appears in it): a domain word of your titles is enough.
    f = fold(title)
    # TITLE_SHORT_SKILL (real case 2026-11-23, Arbeitnow: your skill 'c' kept "Product Manager Insurance P&C" and C#/.NET
    # jobs): a skill shorter than 3 letters never selects a title on its own.
    if any(t in f for t in terms["title"]) or any(re.search(rf"\b{re.escape(k)}\b", f) for k in terms["core_skill"]
                                                 if len(k) >= 3):
        return True
    words = {w.rstrip("s") for w in re.split(r"[^a-z0-9+#]+", f)}       # singular / plural
    return bool(words & domain_tokens(terms["title"]))


def fetch_greenhouse(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    boards = [(c["name"], c["greenhouse"]) for c in ctx.instance["criteria"].get("target_companies", []) if c.get("greenhouse")]
    if not boards:
        raise SourceError("misconfiguration", "aucune entreprise suivie avec un tableau Greenhouse dans job_pilot.json")
    out, q = [], 0
    for name, token in boards:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", token):
            raise SourceError("misconfiguration", f"identifiant Greenhouse invalide pour {name}")
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
        raise SourceError("misconfiguration", "aucune entreprise suivie avec un identifiant Lever dans job_pilot.json")
    out, q = [], 0
    for name, cid in companies:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", cid):
            raise SourceError("misconfiguration", f"identifiant Lever invalide pour {name}")
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


# --------------------------------------------------------------------------- France Travail public pages
FT_WEB = "https://candidat.francetravail.fr"


def robots_rules(text: str) -> list[tuple[str, str]]:
    """(allow|disallow, pattern) of the 'User-agent: *' group."""
    rules, active = [], False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            active = value == "*"
        elif active and key in ("allow", "disallow") and value:
            rules.append((key, value))
    return rules


def robots_allowed(rules: list[tuple[str, str]], path: str) -> bool:
    """Longest matching rule wins (RFC 9309), '*' wildcard and '$' end anchor supported."""
    best, verdict = -1, True
    for kind, pattern in rules:
        rx = re.escape(pattern).replace(r"\*", ".*")
        rx = rx[:-2] + "$" if rx.endswith(r"\$") else rx
        if re.match(rx, path) and len(pattern) > best:
            best, verdict = len(pattern), kind == "allow"
    return verdict


def parse_ft_listing(page: str, words: dict, now=None) -> list[dict]:
    """Offers of a France Travail métier page: id, title, company, location, excerpt, contract, date, origin."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    out = []
    for m in re.finditer(r'<li data-id-offre="([A-Z0-9]+)" class="result">(.*?)</li>', page, re.S):
        oid, block = m.group(1), m.group(2)
        def text(rx):
            x = re.search(rx, block, re.S)
            return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x.group(1)))).strip() if x else ""
        title = text(r'class="media-heading-title">(.*?)</span>')
        sub = text(r'<p translate="no" class="subtext">(.*?)</p>')
        # FT_NO_COMPANY (real case 2026-11-21: "92 - Boulogne-Billancourt" without employer gave company "92")
        if re.match(r"^(\d{2,3}|2[AB])\s*-", sub):
            company, location = "", sub
        else:
            company, _, location = sub.partition(" - ")
        excerpt = text(r'<p class="description">(.*?)</p>')
        contract = text(r'<p class="contrat">(.*?)<span')
        origin = text(r'alt="Origine de l\'offre : ([^"]*)"')
        date_txt = fold(text(r'<p class="date">(.*?)</p>'))
        days = 0 if "aujourd" in date_txt else 1 if "hier" in date_txt else None
        dm = re.search(r"il y a (\d+) jour", date_txt)
        if dm:
            days = int(dm.group(1))
        published = (now - timedelta(days=days)).date().isoformat() if days is not None else None
        o = make_offer(words, source_id="france_travail_web", external_id=oid, title=title,
                       company=company.strip() or "Entreprise non communiquée", url=f"{FT_WEB}/offres/recherche/detail/{oid}",
                       description=excerpt, location=location.strip() or None, contract=contract, published_at=published,
                       label_experience=None)
        if o:
            o["description_complete"] = False
            o["_origin"] = origin
        out.append(o)
    return out


def fetch_france_travail_web(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    spec = ctx.config["sources"]["france_travail_web"]
    http.left = spec.get("max_requests") or http.left        # the source's own budget replaces the generic one
    status, robots = http.request("GET", f"{FT_WEB}/robots.txt", headers={"Accept": "text/plain"})
    if status != 200:
        raise SourceError("source_unavailable", f"robots.txt HTTP {status} : rien n'a été lu")
    rules = robots_rules(robots)
    blocked = []

    def get(path):
        if not robots_allowed(rules, path):                      # ROBOTS_ALLOWED: never requested
            blocked.append(path)
            return None
        st, body = http.request("GET", FT_WEB + path, headers={"Accept": "text/html,application/xml"})
        return body if st == 200 else None

    tokens = domain_tokens(terms["title"])
    must, exclude = set(spec.get("metier_must_contain_any", [])), set(spec.get("metier_exclude", []))
    sm = get("/rechercheoffre/sitemap/secteurs-metiers.xml") or ""
    national = {}
    for u in re.findall(r"<loc>([^<]+)</loc>", sm):
        mm = re.match(re.escape(FT_WEB) + r"(/offres/emploi/([a-z0-9-]+)/(s\d+m\d+))$", u)
        if not mm:
            continue
        words = {w.rstrip("s") for w in mm.group(2).split("-")}
        if words & tokens and (not must or words & must) and not words & exclude:
            national.setdefault(mm.group(2), []).append(mm.group(1))
    if not national:
        raise SourceError("missing_data", "aucune page métier France Travail ne correspond à tes intitulés")
    criteria = ctx.instance["criteria"]
    depts = sorted({k for z in criteria.get("zones", []) for k in z.get("keywords", []) if re.fullmatch(r"\d{2}", k)})
    everywhere = bool(criteria.get("mobility_france"))
    paths = [p for ps in national.values() for p in ps]
    if depts or everywhere:
        # FT_ALL_DEPARTMENTS (real case 2026-11-23: 90 % of the offers came from Île-de-France and Haut-Rhin although
        # you are mobile in all France): a national page only shows its latest offers, so with mobility_france every
        # department page is read; your zones first, so a short budget still covers them.
        cross = get("/rechercheoffre/sitemap/croisements-metiers-departements.xml") or ""
        firsts = {slug: ps[0].rsplit("/", 1)[1] for slug, ps in national.items()}
        mine, others = [], []
        for u in re.findall(r"<loc>([^<]+)</loc>", cross):
            mm = re.match(re.escape(FT_WEB) + r"(/offres/emploi/([a-z0-9-]+)/[a-z0-9-]+/(s\d+m\d+)d(\d{2}))$", u)
            if mm and firsts.get(mm.group(2)) == mm.group(3):
                if mm.group(4) in depts:
                    mine.append(mm.group(1))
                elif everywhere:
                    others.append(mm.group(1))
        paths += mine + others
        if everywhere:
            ctx.store.record_outcomes(ctx.run_id, [{"check": "FT_ALL_DEPARTMENTS", "outcome": "pass",
                                                    "detail": f"{len(mine)} page(s) of your zones, {len(others)} elsewhere"}])
    out = []
    for path in paths:
        if http.left <= 0:
            break
        page = get(path)
        if page:
            out += [o for o in parse_ft_listing(page, ctx.words) if o is None or _title_wanted(o["title"], terms)]
    if blocked:
        ctx.store.record_outcomes(ctx.run_id, [{"check": "ROBOTS_ALLOWED", "outcome": "corrected",
                                                "detail": f"{len(blocked)} path(s) not requested: {blocked[:3]}"}])
    else:
        ctx.store.record_outcomes(ctx.run_id, [{"check": "ROBOTS_ALLOWED", "outcome": "pass",
                                                "detail": f"{http.count} request(s), all allowed"}])
    return out, len(paths)


def fetch_arbeitnow(ctx: RunContext, http: Http, terms: dict) -> tuple[list[dict], int]:
    """Arbeitnow free public API: pages of 300 offers, newest first, full description; every country kept."""
    spec = ctx.config["sources"]["arbeitnow"]
    out, pages = [], 0
    for page in range(1, int(spec.get("max_pages", 3)) + 1):
        data = http.json("GET", f"https://www.arbeitnow.com/api/job-board-api?page={page}") or {}
        pages += 1
        for j in data.get("data", []):
            if not _title_wanted(j.get("title", ""), terms):
                continue
            created = j.get("created_at")
            loc = (j.get("location") or "").strip() or ("Remote" if j.get("remote") else None)
            out.append(make_offer(ctx.words, source_id="arbeitnow", external_id=j.get("slug"), title=j.get("title"),
                                  company=j.get("company_name"), url=j.get("url"),
                                  description=html_to_text(j.get("description", "")), location=loc,
                                  contract=", ".join(j.get("job_types") or []) or None,
                                  published_at=datetime.fromtimestamp(int(created), timezone.utc).isoformat(timespec="seconds")
                                  if str(created or "").isdigit() else None))
        if not (data.get("links") or {}).get("next"):
            break
    return out, pages


FT_DETAIL = "/api-descriptifoffre/offre/"


def ft_detail_fields(d: dict, words: dict) -> dict:
    """Fields of a France Travail offer read from the JSON its public offer page loads (full text, experience asked)."""
    exp = (d.get("experienceProfil") or {}).get("experience")
    parts = [d.get("descriptif") or ""]
    if exp:
        parts.append(f"Expérience demandée : {exp}")
    contract = normalize_contract(d.get("syntheseContrat"), words)
    sal = d.get("salaire")
    rec = {"description": "\n".join(p for p in parts if p).strip(), "label_experience": exp, "description_complete": True}
    if contract:
        rec["contract"] = contract
    if sal:
        rec["salary_text"] = sal
        rec["salary_min"], rec["salary_max"] = parse_salary(sal)
    return rec


def reset_judgement(c, offer_id: str) -> None:
    """The text of an offer changed: its automatic experience decision and its scores are recomputed at the next step
    (agent003, agent004); a decision YOU took on the offer is kept."""
    c.execute("DELETE FROM experience_decisions WHERE offer_id=? AND user_override IS NULL", (offer_id,))
    c.execute("DELETE FROM scores WHERE offer_id=?", (offer_id,))


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
            reset_judgement(c, offer_id)
            counts["completed"] += 1
    return counts


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
                reset_judgement(c, existing[0])
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
