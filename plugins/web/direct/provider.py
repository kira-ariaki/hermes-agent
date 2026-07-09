"""Direct web extract provider — free httpx fetch + trafilatura, Decodo fallback.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Extract-only.

Why this exists
---------------
The in-tree ``decodo`` provider routes *every* extract through the Decodo
residential-proxy Scraper API — ~3s/page and it costs money. But the
overwhelming majority of pages Ariaki extracts (blogs, docs, GitHub,
Wikipedia, news) have no anti-bot at all. Paying Decodo for those is
killing a chicken with a cleaver.

This provider is two-stage:

1. **Free direct fetch** — ``httpx.get`` with a real browser User-Agent,
   then ``trafilatura`` distills the main article text to markdown. Zero
   cost, fast. Handles the 95% case.
2. **Decodo fallback** — only when the direct fetch *fails* (timeout,
   403/429/5xx, empty body, or an obvious anti-bot challenge page) do we
   reach for the residential-proxy cleaver. Reuses ``decodo``'s
   ``_scrape_one`` verbatim so the DataDome-beating logic stays in one place.

Config keys this provider responds to::

    web:
      extract_backend: "direct"   # explicit per-capability
      backend: "direct"           # shared fallback

The Decodo fallback is best-effort: if ``DECODO_BASIC_AUTH_HEADER`` is
unset, stage 2 is skipped and the direct-fetch error is returned as-is.
So ``is_available()`` only requires that we can do stage 1 (always true —
httpx + trafilatura are hard deps of this provider).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_PER_URL_TIMEOUT = 25  # seconds for the free direct fetch
_MAX_PARALLEL = 5

# A realistic desktop Chrome UA. Many sites 403 the default httpx UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Signals in a fetched body that mean "this is an anti-bot wall, not content".
_ANTIBOT_RE = re.compile(
    r"(just a moment|checking your browser|cf-browser-verification|"
    r"datadome|px-captcha|please enable javascript and cookies|"
    r"verify you are (a )?human|attention required|access denied)",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Minimum chars of distilled text below which we treat the fetch as "thin"
# and let Decodo retry (e.g. JS-rendered SPA shells).
_MIN_CONTENT_CHARS = 200


def _extract_title(raw_html: str) -> str:
    import html as _html

    m = _TITLE_RE.search(raw_html)
    if not m:
        return ""
    return _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _looks_like_js_shell(raw_html: str) -> bool:
    """Heuristic: True when the page is mostly script with little visible text.

    A real SPA shell ships a big JS bundle and an almost-empty body
    (``<div id="root"></div>``). After stripping <script> blocks and tags,
    if there's very little human-visible text left relative to the raw size,
    treat it as a JS shell worth retrying via Decodo. A genuinely short page
    (e.g. example.com) has little script and so does NOT match.
    """
    scripts = _SCRIPT_BLOCK_RE.findall(raw_html)
    script_chars = sum(len(s) for s in scripts)
    no_scripts = _SCRIPT_BLOCK_RE.sub(" ", raw_html)
    visible = _TAG_STRIP_RE.sub(" ", no_scripts)
    visible_text = re.sub(r"\s+", " ", visible).strip()
    # Mostly-script AND almost no visible text => SPA shell.
    return script_chars > 2000 and len(visible_text) < 150


def _distill_with_trafilatura(raw_html: str, url: str) -> str:
    """Return markdown main-content via trafilatura, or '' on failure."""
    try:
        import trafilatura

        text = trafilatura.extract(
            raw_html,
            url=url,
            output_format="markdown",
            include_links=False,
            include_images=False,
            include_comments=False,
            favor_precision=True,
        )
        return (text or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("trafilatura.extract failed for %s: %s", url, exc)
        return ""


def _direct_fetch_one(url: str) -> Dict[str, Any]:
    """Blocking single-URL free fetch + trafilatura distill.

    Returns a per-URL result dict. On a recoverable failure the dict carries
    a non-empty ``error`` AND ``_fallback`` True so the async wrapper knows
    to try Decodo. On hard success ``error`` is absent.
    """
    import httpx

    try:
        resp = httpx.get(
            url,
            headers=_DEFAULT_HEADERS,
            timeout=_PER_URL_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.RequestError as exc:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"direct fetch failed: {exc}", "_fallback": True}

    status = resp.status_code
    if status in (403, 429) or status >= 500:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"direct fetch HTTP {status}", "_fallback": True}
    if status >= 400:
        # 404/410 etc — page genuinely not there. Don't waste Decodo on it.
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"direct fetch HTTP {status}", "_fallback": False}

    raw_html = resp.text or ""
    if not raw_html.strip():
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": "direct fetch returned empty body", "_fallback": True}

    # Anti-bot wall? Trip the cleaver.
    head = raw_html[:4000]
    if _ANTIBOT_RE.search(head):
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": "direct fetch hit anti-bot challenge", "_fallback": True}

    content = _distill_with_trafilatura(raw_html, url)
    if not content:
        # trafilatura found no main content at all — likely a JS-rendered shell
        # or an unusual layout. Let Decodo try a residential fetch.
        return {"url": url, "title": _extract_title(raw_html),
                "content": "", "raw_content": raw_html[:200_000],
                "error": "direct fetch produced no main content", "_fallback": True}
    if len(content) < _MIN_CONTENT_CHARS and _looks_like_js_shell(raw_html):
        # Thin distilled text AND the page looks like a JS-rendered shell
        # (big <script> payload, tiny visible body). Decodo may get SSR'd HTML.
        return {"url": url, "title": _extract_title(raw_html),
                "content": content, "raw_content": raw_html[:200_000],
                "error": "direct fetch produced thin content (JS shell)",
                "_fallback": True}

    return {
        "url": url,
        "title": _extract_title(raw_html),
        "content": content,
        "raw_content": raw_html[:200_000],
        "metadata": {"source": "direct"},
    }


class DirectWebExtractProvider(WebSearchProvider):
    """Extract-only: free direct fetch first, Decodo residential fallback."""

    @property
    def name(self) -> str:
        return "direct"

    @property
    def display_name(self) -> str:
        return "Direct + Decodo fallback"

    def is_available(self) -> bool:
        """Always available — httpx + trafilatura are hard deps.

        The Decodo fallback is best-effort and gated separately at call time
        on ``DECODO_BASIC_AUTH_HEADER``.
        """
        try:
            import httpx  # noqa: F401
            import trafilatura  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Two-stage extract: free direct fetch, Decodo only on recoverable failure."""
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        # Lazy import of the Decodo single-URL scraper for fallback.
        try:
            from plugins.web.decodo.provider import _scrape_one as _decodo_scrape_one
            from plugins.web.decodo.provider import _get_auth_header as _decodo_auth
        except Exception:  # noqa: BLE001
            _decodo_scrape_one = None
            _decodo_auth = lambda: ""  # noqa: E731

        sem = asyncio.Semaphore(_MAX_PARALLEL)

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
                # Stage 1: free direct fetch.
                logger.info("Direct fetching: %s", url)
                result = await asyncio.wait_for(
                    asyncio.to_thread(_direct_fetch_one, url),
                    timeout=_PER_URL_TIMEOUT + 5,
                )

                if not result.get("error"):
                    result.pop("_fallback", None)
                    return result

                # Stage 2: Decodo fallback (only if recoverable + creds present).
                want_fallback = result.pop("_fallback", False)
                auth = _decodo_auth() if _decodo_auth else ""
                if want_fallback and _decodo_scrape_one and auth:
                    direct_err = result.get("error", "")
                    logger.info(
                        "Direct fetch failed (%s); falling back to Decodo: %s",
                        direct_err, url,
                    )
                    try:
                        decodo_result = await asyncio.wait_for(
                            asyncio.to_thread(_decodo_scrape_one, url, auth),
                            timeout=70,
                        )
                    except asyncio.TimeoutError:
                        return {
                            "url": url, "title": result.get("title", ""),
                            "content": result.get("content", ""),
                            "raw_content": "",
                            "error": f"direct failed ({direct_err}); "
                                     "Decodo fallback timed out",
                        }
                    # If Decodo succeeded, prefer it; else surface both errors.
                    if not decodo_result.get("error"):
                        md = decodo_result.setdefault("metadata", {})
                        md["fallback_from"] = "direct"
                        md["direct_error"] = direct_err
                        return decodo_result
                    return {
                        "url": url, "title": result.get("title", ""),
                        "content": result.get("content", ""),
                        "raw_content": "",
                        "error": f"direct failed ({direct_err}); "
                                 f"Decodo also failed ({decodo_result.get('error')})",
                    }

                # No fallback available/wanted — return the direct result as-is
                # (keep any thin content we did distill).
                result.pop("_fallback", None)
                return result

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[_one(u) for u in urls]),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return [{"url": u, "title": "", "content": "", "raw_content": "",
                     "error": "extract batch timed out"} for u in urls]
        return list(results)

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Direct + Decodo fallback",
            "badge": "free · extract-only",
            "tag": (
                "Free direct fetch (httpx + trafilatura) for normal pages; "
                "auto-falls back to Decodo residential proxy on anti-bot walls. "
                "Pair with SearXNG for search."
            ),
            "env_vars": [
                {
                    "key": "DECODO_BASIC_AUTH_HEADER",
                    "prompt": "Decodo Basic auth header for anti-bot fallback "
                              "(optional — Basic <base64>)",
                    "url": "https://decodo.com/",
                },
            ],
        }
