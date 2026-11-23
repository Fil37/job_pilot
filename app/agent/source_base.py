"""What every source connector shares: a common offer format, salary and contract reading, the title filter.
Split out of agent002 on 2026-11-23 (modularity: one file per source; adding a source touches no other)."""
from __future__ import annotations

import re

from app.agent import fold
from app.agent.common_text import domain_tokens


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
