"""Company sheets of agent005: their key and their lines for a package or an interview sheet (shared by agent005,
agent006, agent010 and the web pages; moved on 2026-11-23)."""
from __future__ import annotations

import json
import re

from app.agent import RunContext, fold
from app.agent.common_text import LEGAL


def company_key(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]+", " ", re.sub(LEGAL, " ", fold(name)))).strip()


def company_lines(ctx: RunContext, name: str) -> list[str]:
    """The company sheet of agent005 (each fact with its kind and source), or 'missing'."""
    with ctx.connect() as c:
        row = c.execute("SELECT facts, match_confidence FROM companies WHERE company_key=?", (company_key(name),)).fetchone()
    if not row:
        return ["- information absente (pas de fiche pour cette entreprise)"]
    out = [] if row[1] >= 0.6 else [f"- Correspondance incertaine avec le registre ({row[1]}) : à vérifier"]
    sources = []
    for f in json.loads(row[0]):
        tag = {"verified_fact": "", "hypothesis": " (hypothèse)", "missing": " (absent)",
               "retrieval_error": " (erreur de récupération)"}.get(f["kind"], "")
        out.append(f"- {f['label']}{tag} : {f['value']}")
        if f["source"].startswith("http") and f["source"] not in sources:
            sources.append(f["source"])
    if sources:
        out.append("- Sources : " + " ; ".join(sources))
    return out
