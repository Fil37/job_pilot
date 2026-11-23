"""Text helpers on job titles and company names (shared by agent002, agent005, agent006; moved out of agent002 on
2026-11-23)."""
from __future__ import annotations

import re

from app.agent import fold


LEGAL = r"\b(sas|sasu|sa|sarl|eurl|sca|gmbh|ltd|inc|group|groupe|france)\b"


GENDER = r"\(?\b[hf]\s*/\s*[fh]\b\)?|\(?\bf\s*/\s*h\b\)?|\(?\bm\s*/\s*f\b\)?|\(?\bw\s*/\s*m\b\)?|\bh/f/x\b"


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
