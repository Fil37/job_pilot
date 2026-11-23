"""France Travail: the official API (closed to new applications, kept off) and the public pages allowed by its
robots.txt (métier × département pages, then the full text of each offer). Split out of agent002 on 2026-11-23."""
from __future__ import annotations

import html
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from app.agent import RunContext, fold
from app.agent.common_http import Http, SourceError, robots_allowed, robots_rules
from app.agent.common_text import domain_tokens
from app.agent.source_base import _title_wanted, make_offer, normalize_contract, parse_salary


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


# --------------------------------------------------------------------------- France Travail public pages
FT_WEB = "https://candidat.francetravail.fr"


def parse_ft_listing(page: str, words: dict, now=None) -> list[dict]:
    """Offers of a France Travail métier page: id, title, company, location, excerpt, contract, date, origin."""
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
