"""Arbeitnow free public API (full text, every country). Split out of agent002 on 2026-11-23."""
from __future__ import annotations

from datetime import datetime, timezone

from app.agent import RunContext
from app.agent.common_http import Http, html_to_text
from app.agent.source_base import _title_wanted, make_offer


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
