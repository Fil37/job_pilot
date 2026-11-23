"""Public job board APIs of the companies you follow (Greenhouse, Lever). Split out of agent002 on 2026-11-23."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.agent import RunContext
from app.agent.common_http import Http, SourceError, html_to_text
from app.agent.source_base import _title_wanted, make_offer


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
