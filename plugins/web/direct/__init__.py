"""Direct web extract plugin — bundled.

Extract-only provider with a two-stage strategy:

1. Free direct fetch (``httpx`` + ``trafilatura``) for the common case —
   normal pages with no anti-bot.
2. Decodo residential-proxy fallback, reusing the ``decodo`` provider's
   single-URL scraper, only when the direct fetch hits an anti-bot wall or
   otherwise fails recoverably.

Pair with a search-only provider (e.g. SearXNG). Configure via::

    web:
      search_backend: "searxng"
      extract_backend: "direct"

The Decodo fallback is best-effort; it activates only when
``DECODO_BASIC_AUTH_HEADER`` is present (resolved from 1Password by the
gateway env loader, or set directly).
"""

from __future__ import annotations

from plugins.web.direct.provider import DirectWebExtractProvider


def register(ctx) -> None:
    """Register the direct+fallback extract provider with the plugin context."""
    ctx.register_web_search_provider(DirectWebExtractProvider())
