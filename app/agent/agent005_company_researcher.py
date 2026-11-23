"""agent005 — company researcher.

Objective        : gather sourced facts about the employers of your best offers: activity, size, category, age,
                   headquarters, public directors, a short public description, and a hiring signal.
Scope            : the companies of the top offers of the day (or of one offer you prepare an application for);
                   the official French company registry API (recherche-entreprises.api.gouv.fr, open data) and the
                   Wikipedia API; the hiring signal is computed from the offers job_pilot already saw.
Out of scope     : NEVER contacts anyone, never scrapes a company or news site, never scores an offer (agent004),
                   never stores a fact without its source and its kind; news are reported 'missing' (no news source
                   whose terms allow automated reading).
Inputs           : params.offer_id (application chain) or the top offers (settings.company.top_n).
Outputs          : companies rows (facts: label, value, kind, source); envelope data {companies, matched}.
Tools            : HTTPS (generic User-Agent, delay, request budget); no model.
Data read        : offers, scores, companies.
Data written     : companies (owner).
Success criteria : every fact has a source URL or the 'computed' / 'missing' kind; a doubtful registry match is
                   a 'hypothesis', never a verified fact (check COMPANY_MATCH_CONFIDENCE).
Cost limits      : 0 model call; 3 requests per company at most.
Time limits      : settings.timeouts_s.http_request per request.
Error policy     : a company not found stays with 'missing' facts; a source down → partial, never invented.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

from app.agent import BaseAgent, RunContext, envelope, fold, upsert, utc_now
from app.agent.agent002_offer_collector import LEGAL, Http, SourceError

SKILLS = [
    {"skill_id": "SK_COMPANY_BRIEF", "version": "1", "status": "proposed",
     "objective": "Établir une fiche entreprise sourcée pour les meilleures offres.",
     "summary": "Registre (activité, taille, catégorie, création, siège, dirigeants publics), résumé Wikipédia, "
                "signal de recrutement (offres vues sur 30 jours). Chaque fait a sa source et sa nature ; une "
                "correspondance de nom douteuse reste une hypothèse (contrôle COMPANY_MATCH_CONFIDENCE). Visible sur "
                "la page de l'offre et dans le dossier."},
]

AGENT_ID = "agent005"
UNKNOWN = {"entreprise non communiquee", ""}


def fr_date(iso: str | None) -> str:
    """'2020-01-21' → '21/01/2020' (French reading); unknown stays '?'."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (iso or "?")


def company_key(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]+", " ", re.sub(LEGAL, " ", fold(name)))).strip()


def name_similarity(a: str, b: str) -> float:
    """Token overlap of two company names (accents, case and legal suffixes ignored)."""
    ta, tb = set(company_key(a).split()), set(company_key(b).split())
    if not ta or not tb:
        return 0.0
    if company_key(a) == company_key(b):
        return 1.0
    return round(len(ta & tb) / len(ta | tb), 2)


def best_registry_match(name: str, results: list[dict]) -> tuple[dict | None, float]:
    best, score = None, 0.0
    for r in results:
        cands = [r.get("nom_complet") or "", r.get("nom_raison_sociale") or "", r.get("sigle") or ""]
        s = max(name_similarity(name, c) for c in cands if c) if any(cands) else 0.0
        if r.get("etat_administratif") != "A":
            s -= 0.2
        if s > score:
            best, score = r, s
    return best, round(max(score, 0.0), 2)


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def plan(self, ctx: RunContext):
        cfg = ctx.config["company"]
        fresh = (datetime.now(timezone.utc) - timedelta(days=cfg["refresh_days"])).isoformat(timespec="seconds")
        with ctx.connect() as c:
            if ctx.params.get("offer_id"):
                names = [r[0] for r in c.execute("SELECT company FROM offers WHERE offer_id=?", (ctx.params["offer_id"],))]
                force = True
            else:
                names = [r[0] for r in c.execute(
                    "SELECT o.company FROM offers o JOIN scores s USING(offer_id) WHERE o.status='new' AND s.eliminated=0 "
                    "ORDER BY s.score DESC LIMIT ?", (cfg["top_n"] * 3,))]
                force = False
            done = {r[0]: r[1] for r in c.execute("SELECT company_key, updated_at FROM companies")}
        subs, seen = [], set()
        for n in names:
            k = company_key(n)
            if k in seen or fold(n) in UNKNOWN or not k:
                continue
            seen.add(k)
            if not force and done.get(k, "") >= fresh:
                continue
            subs.append({"objective": f"company sheet {n}", "name": n, "key": k})
            if len(subs) >= (1 if force else cfg["top_n"]):
                break
        return subs or [{"objective": "nothing to research", "name": None}]

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("COMPANY_MATCH_CONFIDENCE", AGENT_ID, "une correspondance au registre sous 0,6 reste une hypothèse",
                                 "rule of the method: never present a doubtful match as a verified fact")
        cfg = ctx.config["company"]
        http = Http(ctx, "company_registry")
        done, errors, warnings, outcomes = [], [], [], []
        for sub in subtasks:
            if not sub.get("name"):
                continue
            name, key = sub["name"], sub["key"]
            facts, row = [], {"company_key": key, "name": name, "match_confidence": 0.0, "run_id": ctx.run_id,
                              "updated_at": utc_now()}
            q = urllib.parse.urlencode({"q": name, "per_page": 5})
            src = f"{cfg['registry_api']}?{q}"
            try:
                data = http.json("GET", src) or {}
                best, conf = best_registry_match(name, data.get("results", []))
            except SourceError as exc:
                best, conf = None, 0.0
                errors.append({"category": exc.category, "message": f"registry {name}: {exc}"})
                # RETRIEVAL_ERROR_NOT_MISSING (real case 2026-11-21: a network error on Serma Ingénierie was written
                # "entreprise non trouvée"): an unreachable registry is a retrieval error, retried at the next run.
                facts.append({"label": "Registre", "value": "indisponible lors de ce passage, nouvel essai au prochain",
                              "kind": "retrieval_error", "source": src})
                row["updated_at"] = "1970-01-01T00:00:00+00:00"
            if best:
                kind = "verified_fact" if conf >= 0.6 else "hypothesis"
                outcomes.append({"check": "COMPANY_MATCH_CONFIDENCE", "item": key,
                                 "outcome": "pass" if conf >= 0.6 else "corrected",
                                 "detail": f"{best.get('nom_complet')} ({conf})"})
                fiche = f"https://annuaire-entreprises.data.gouv.fr/entreprise/{best.get('siren')}"
                size = best.get("tranche_effectif_salarie") or "NN"
                section = best.get("section_activite_principale")
                siege = best.get("siege") or {}
                row.update(siren=best.get("siren"), legal_name=best.get("nom_complet"), naf=best.get("activite_principale"),
                           size_band=ctx.words["size_bands"].get(size, size), size_year=best.get("annee_tranche_effectif_salarie"),
                           category=best.get("categorie_entreprise"), created_on=best.get("date_creation"),
                           establishments_open=best.get("nombre_etablissements_ouverts"), hq_city=siege.get("libelle_commune"),
                           match_confidence=conf)
                facts += [
                    {"label": "Raison sociale", "value": f"{best.get('nom_complet')} (SIREN {best.get('siren')})", "kind": kind, "source": fiche},
                    {"label": "Activité", "value": f"{ctx.words['naf_sections'].get(section, section)} (NAF {best.get('activite_principale')})", "kind": kind, "source": fiche},
                    {"label": "Effectif", "value": f"{ctx.words['size_bands'].get(size, size)} salariés ({best.get('annee_tranche_effectif_salarie') or '?'})", "kind": kind, "source": fiche},
                    {"label": "Catégorie", "value": best.get("categorie_entreprise") or "?", "kind": kind, "source": fiche},
                    {"label": "Création", "value": fr_date(best.get("date_creation")), "kind": kind, "source": fiche},
                    {"label": "Siège", "value": siege.get("adresse") or "?", "kind": kind, "source": fiche},
                    {"label": "Établissements ouverts", "value": str(best.get("nombre_etablissements_ouverts") or "?"), "kind": kind, "source": fiche}]
                people = [f"{(d.get('prenoms') or '').split(' ')[0].title()} {(d.get('nom') or '').title()}, {d.get('qualite')}"
                          for d in best.get("dirigeants") or [] if d.get("type_dirigeant") == "personne physique" and d.get("nom")][:3]
                if people:
                    facts.append({"label": "Dirigeants (registre public)", "value": " ; ".join(people), "kind": kind, "source": fiche})
            elif not any(f["kind"] == "retrieval_error" for f in facts):
                facts.append({"label": "Registre", "value": "entreprise non trouvée", "kind": "missing", "source": src})
            # Wikipedia: only an article whose title matches the company name
            try:
                sq = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": name, "srlimit": 3,
                                             "format": "json"})
                res = (http.json("GET", f"{cfg['wikipedia_api']}?{sq}") or {}).get("query", {}).get("search", [])
                hit = next((r for r in res if name_similarity(name, r["title"]) >= 0.6), None)
                if hit:
                    eq = urllib.parse.urlencode({"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                                                 "exsentences": 3, "titles": hit["title"], "format": "json"})
                    pages = (http.json("GET", f"{cfg['wikipedia_api']}?{eq}") or {}).get("query", {}).get("pages", {})
                    extract = next(iter(pages.values()), {}).get("extract", "").strip()
                    url = "https://fr.wikipedia.org/wiki/" + urllib.parse.quote(hit["title"].replace(" ", "_"))
                    if extract:
                        row.update(wikipedia_url=url, wikipedia_extract=extract[:800])
                        facts.append({"label": "Présentation (Wikipédia)", "value": extract[:800], "kind": "verified_fact", "source": url})
            except SourceError as exc:
                warnings.append(f"wikipedia {name}: {exc}")
            with ctx.connect() as c:
                since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
                n = sum(1 for (comp,) in c.execute("SELECT company FROM offers WHERE first_seen_at>=?", (since,))
                        if company_key(comp) == key)
            row["offers_seen_30d"] = n
            facts.append({"label": "Signal de recrutement", "value": f"{n} offre(s) vue(s) par job_pilot en 30 jours",
                          "kind": "verified_fact", "source": "computed"})
            facts.append({"label": "Actualité", "value": "non collectée (aucune source d'actualités autorisant une lecture automatisée)",
                          "kind": "missing", "source": "none"})
            row["facts"] = facts
            with ctx.connect() as c:
                upsert(c, ctx.rows, "companies", row)
            done.append({"key": key, "name": name, "confidence": row["match_confidence"]})
        ctx.store.record_outcomes(ctx.run_id, outcomes)
        status = "partial" if errors and done else ("failed" if errors else "succeeded")
        return envelope(ctx, AGENT_ID, status, data={"companies": done, "matched": sum(1 for d in done if d["confidence"] >= 0.6)},
                        errors=errors, warnings=warnings, metrics={"companies": len(done), "requests": http.count},
                        sources=[{"kind": "verified_fact", "ref": "recherche-entreprises.api.gouv.fr"},
                                 {"kind": "verified_fact", "ref": "fr.wikipedia.org"}])

    def healthcheck(self, ctx=None):
        return "healthy", "API publiques du registre et de Wikipédia (sans clé)"


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
