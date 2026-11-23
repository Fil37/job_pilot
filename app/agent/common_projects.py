"""Rules shared by the project ideas (agent007) and the portfolio (agent008): your real date, the image of a card,
the cards of your portfolio grid, the hardware items of a brief. Moved on 2026-11-23 (modularity)."""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path


def parse_done_on(text: str | None, today=None) -> str:
    """REAL_PROJECT_DATE (rule of the user 2026-11-23: "des projets que j'ai déjà faits mais pas encore versionnés ne
    doivent pas apparaître avec la date d'aujourd'hui"): the date YOU give for when the project was really done,
    AAAA, AAAA-MM or AAAA-MM-JJ, never in the future. Raises ValueError otherwise."""
    t = (text or "").strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", t)
    if not m:
        raise ValueError("format AAAA, AAAA-MM ou AAAA-MM-JJ")
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    when = datetime(y, mo, d)                     # ValueError for an impossible month or day
    today = today or datetime.now()
    if when > today or y < 1990:
        raise ValueError("date dans le futur ou trop ancienne")
    return t


IMAGE_TYPES = {".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8", ".webp": b"RIFF", ".svg": None}


def image_problem(name: str, data: bytes, max_bytes: int) -> str | None:
    """IMAGE_SAFE: an image you add to your PUBLIC site: known type, real content, size limit, no script in an SVG."""
    ext = Path(name or "").suffix.lower()
    if ext not in IMAGE_TYPES:
        return "type d'image refusé (png, jpg, webp ou svg)"
    if not data or len(data) > max_bytes:
        return f"image vide ou trop lourde (au plus {max_bytes // 1000} ko)"
    magic = IMAGE_TYPES[ext]
    if magic and not data.startswith(magic):
        return "le contenu ne correspond pas à l'extension"
    if ext == ".svg":
        t = data.decode("utf-8", "replace").lower()
        if "<svg" not in t or re.search(r"<script|<foreignobject|\son\w+\s*=|javascript:|href\s*=\s*[\"']https?:", t):
            return "SVG refusé : script, gestionnaire d'événement ou lien externe"
    return None


def grid_span(page: str, grid_marker: str) -> tuple[int, int, list[tuple[str, int]]]:
    """(start after the marker, end at the grid's closing </div>, [(card title, end of its </article>)...])."""
    i = page.find(grid_marker)
    if i < 0:
        raise ValueError("project grid not found in the portfolio page")
    j = i + len(grid_marker)
    depth, cards, title = 1, [], None
    for m in re.finditer(r"<div\b|</div>|<h3>(.*?)</h3>|</article>", page[j:], re.S):
        tok = m.group(0)
        if tok.startswith("<div"):
            depth += 1
        elif tok == "</div>":
            depth -= 1
            if depth == 0:
                return j, j + m.start(), cards
        elif tok.startswith("<h3>"):
            title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        elif tok == "</article>" and depth == 1:
            cards.append((title or "", j + m.end()))
            title = None
    raise ValueError("project grid not closed in the portfolio page")


def grid_titles(page: str, grid_marker: str) -> list[str]:
    """Titles of the cards of the grid, in order (to choose where your card goes)."""
    return [t for t, _ in grid_span(page, grid_marker)[2]]


HW_KEYS = ("name", "role", "specs", "example", "alternative")


def hw_item(h) -> dict:
    """HARDWARE_DETAIL: a hardware item as a dict (older briefs have plain strings)."""
    if isinstance(h, dict):
        return {**{k: str(h.get(k) or "").strip() for k in HW_KEYS}, "optional": bool(h.get("optional"))}
    return {"name": str(h), "role": "", "specs": "", "example": "", "alternative": "", "optional": False}


def portfolio_token(cfg: dict) -> str:
    """The portfolio token: settings.portfolio.token_env (GITHUB_TOKEN locally) or PORTFOLIO_TOKEN (online: GitHub
    refuses secrets named GITHUB_*). Empty when none."""
    import os
    return os.environ.get(cfg.get("token_env") or "GITHUB_TOKEN") or os.environ.get("PORTFOLIO_TOKEN") or ""

