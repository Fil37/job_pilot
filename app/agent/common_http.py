"""Bounded HTTP client of a source and robots.txt rules (shared by agent002 and agent005, moved out of agent002 on
2026-11-23)."""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app.agent import RunContext


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
