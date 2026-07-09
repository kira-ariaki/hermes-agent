"""Decodo Scraper API extract provider — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Extract-only.

Why this exists
---------------
Ariaki already pays for the Decodo Scraper API (residential-proxy
"POST URL -> get HTML" endpoint, used by the ``decodo-scraper-api`` skill
to beat DataDome). Rather than sign up for Firecrawl/Tavily/Exa just to
give ``web_extract`` a backend, we reuse that credential: Decodo fetches
the page through a residential IP, returns raw HTML, and this provider
distills it into readable text/markdown locally.

Design notes (carried from the decodo-scraper-api skill)
--------------------------------------------------------
- **plain HTTP, no ``headless``** — adding ``headless="html"`` makes
  DataDome return status 613. SSR pages don't need JS rendering anyway.
- **use the pre-built Basic header verbatim** — rolling our own base64
  from user:key mis-encodes special chars and 401s. The header lives in
  1Password as ``Decodo Scraper API/Config/basic_auth_header`` and is
  surfaced here as ``DECODO_BASIC_AUTH_HEADER``.
- **premium pool + US geo** — datacenter pool gets blocked everywhere.

Content distillation
--------------------
Decodo gives us raw HTML. We strip script/style/nav/footer, prefer the
``<main>``/``<article>`` region when present, and convert to markdown
when ``markdownify`` is importable (falls back to a regex tag-stripper).
The page ``<title>`` becomes the result title. We keep the full cleaned
HTML in ``raw_content`` so the caller can re-process if needed.

Config keys this provider responds to::

    web:
      extract_backend: "decodo"   # explicit per-capability
      backend: "decodo"           # shared fallback

Env var::

    DECODO_BASIC_AUTH_HEADER="Basic <base64>"
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

DECODO_ENDPOINT = "https://scraper-api.decodo.com/v2/scrape"
_PER_URL_TIMEOUT = 60  # seconds


def _get_auth_header() -> str:
    """Return the Decodo Basic auth header, or '' when unset."""
    return os.getenv("DECODO_BASIC_AUTH_HEADER", "").strip()


# ---------------------------------------------------------------------------
# HTML -> readable content
# ---------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg|iframe|template)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n\s*\n+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAIN_RE = re.compile(
    r"<(main|article)\b[^>]*>(?P<body>.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_BODY_RE = re.compile(r"<body\b[^>]*>(?P<body>.*?)</body>", re.IGNORECASE | re.DOTALL)
_CHROME_RE = re.compile(
    r"<(nav|header|footer|aside|form)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)

# Cheap HTML entity decode for the common ones; html.unescape handles the rest.
import html as _html


def _extract_title(raw_html: str) -> str:
    m = _TITLE_RE.search(raw_html)
    if not m:
        return ""
    return _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())


def _select_main_region(raw_html: str) -> str:
    """Return the most content-bearing region of the page.

    Prefer <main>/<article>; fall back to <body>; fall back to the whole
    document. Strip obvious chrome (nav/header/footer/aside/form) first.
    """
    candidates: List[str] = []
    for m in _MAIN_RE.finditer(raw_html):
        candidates.append(m.group("body"))
    if candidates:
        # Pick the longest main/article block (usually the real content).
        region = max(candidates, key=len)
    else:
        bm = _BODY_RE.search(raw_html)
        region = bm.group("body") if bm else raw_html
    region = _CHROME_RE.sub(" ", region)
    return region


def _html_to_markdown(region_html: str) -> str:
    """Convert an HTML fragment to markdown.

    Uses ``markdownify`` when importable for decent markdown; otherwise
    falls back to a tag-stripping plain-text pass. Either way the result
    is whitespace-normalized.
    """
    cleaned = _SCRIPT_STYLE_RE.sub(" ", region_html)
    try:
        from markdownify import markdownify as _md  # type: ignore

        text = _md(cleaned, heading_style="ATX", strip=["a"])
    except Exception:  # noqa: BLE001 — markdownify optional / may choke
        text = _TAG_RE.sub(" ", cleaned)
        text = _html.unescape(text)

    # Normalize whitespace: collapse intra-line runs, cap blank lines at 1.
    lines = [_WS_RE.sub(" ", ln).rstrip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def _distill(raw_html: str) -> Dict[str, str]:
    """Return {title, content, raw_content} distilled from raw HTML."""
    title = _extract_title(raw_html)
    region = _select_main_region(raw_html)
    content = _html_to_markdown(region)
    # raw_content = full body, lightly cleaned, for caller re-processing.
    raw_clean = _html_to_markdown(raw_html)
    return {"title": title, "content": content, "raw_content": raw_clean}


# ---------------------------------------------------------------------------
# Single-URL scrape via Decodo
# ---------------------------------------------------------------------------


def _scrape_one(url: str, auth_header: str) -> Dict[str, Any]:
    """Blocking single-URL scrape. Returns a per-URL result dict.

    Runs inside ``asyncio.to_thread`` from :meth:`DecodoWebExtractProvider.extract`.
    """
    import httpx

    payload = {
        "url": url,
        "proxy_pool": "premium",  # datacenter pool gets blocked
        "geo": "United States",
        "locale": "en-us",
        # deliberately NO "headless" — JS rendering trips DataDome (status 613)
    }
    try:
        resp = httpx.post(
            DECODO_ENDPOINT,
            json=payload,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
            timeout=_PER_URL_TIMEOUT,
        )
    except httpx.RequestError as exc:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"Decodo request failed: {exc}"}

    if resp.status_code == 401:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": "Decodo auth rejected (check DECODO_BASIC_AUTH_HEADER)"}
    if resp.status_code >= 400:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"Decodo HTTP {resp.status_code}: {resp.text[:200]}"}

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": "Decodo response was not JSON"}

    results = body.get("results")
    if not isinstance(results, list) or not results:
        # 613 / failed scrapes land here.
        status = body.get("status_code") or body.get("status")
        msg = body.get("message", "no content")
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"Decodo scrape failed (status={status}): {msg}"}

    first = results[0]
    inner_status = first.get("status_code", 200)
    raw_html = first.get("content") or ""
    if not raw_html:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"Decodo returned empty content (inner status {inner_status})"}

    distilled = _distill(raw_html)
    return {
        "url": url,
        "title": distilled["title"],
        "content": distilled["content"],
        "raw_content": distilled["raw_content"],
        "metadata": {"source": "decodo", "status_code": inner_status},
    }


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class DecodoWebExtractProvider(WebSearchProvider):
    """Extract-only provider backed by the Decodo Scraper API."""

    @property
    def name(self) -> str:
        return "decodo"

    @property
    def display_name(self) -> str:
        return "Decodo Scraper"

    def is_available(self) -> bool:
        """Return True when the Decodo Basic auth header is set."""
        return bool(_get_auth_header())

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from URLs via Decodo.

        Each URL is scraped concurrently (cap 3 — Decodo rate-limits above
        ~5 parallel) in background threads, each with a 60s timeout. The
        website-access policy gate is applied before scraping.

        ``kwargs`` (``format`` etc.) are accepted for forward-compat and
        currently ignored — Decodo always returns HTML which we distill to
        markdown.
        """
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        auth_header = _get_auth_header()
        if not auth_header:
            return [
                {"url": u, "title": "", "content": "", "raw_content": "",
                 "error": "DECODO_BASIC_AUTH_HEADER is not set"}
                for u in urls
            ]

        sem = asyncio.Semaphore(3)  # Decodo rate-limits above ~5 parallel

        async def _one(url: str) -> Dict[str, Any]:
            if is_interrupted():
                return {"url": url, "error": "Interrupted", "title": ""}

            blocked = check_website_access(url)
            if blocked:
                logger.info(
                    "Blocked web_extract for %s by rule %s",
                    blocked["host"], blocked["rule"],
                )
                return {
                    "url": url, "title": "", "content": "",
                    "error": blocked["message"],
                    "blocked_by_policy": {
                        "host": blocked["host"],
                        "rule": blocked["rule"],
                        "source": blocked["source"],
                    },
                }

            async with sem:
                try:
                    logger.info("Decodo scraping: %s", url)
                    return await asyncio.wait_for(
                        asyncio.to_thread(_scrape_one, url, auth_header),
                        timeout=_PER_URL_TIMEOUT + 5,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Decodo scrape timed out for %s", url)
                    return {
                        "url": url, "title": "", "content": "", "raw_content": "",
                        "error": "Decodo scrape timed out after 65s",
                    }

        return list(await asyncio.gather(*[_one(u) for u in urls]))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Decodo Scraper",
            "badge": "paid · extract-only",
            "tag": (
                "Residential-proxy page fetcher (beats DataDome). "
                "Extract-only — pair with SearXNG for search."
            ),
            "env_vars": [
                {
                    "key": "DECODO_BASIC_AUTH_HEADER",
                    "prompt": "Decodo Scraper API Basic auth header (Basic <base64>)",
                    "url": "https://decodo.com/",
                },
            ],
        }
