"""Decodo Scraper API extract plugin — bundled.

Extract-only provider backed by Decodo's residential-proxy scraper API
(``https://scraper-api.decodo.com/v2/scrape``). Reuses Ariaki's existing
paid Decodo Scraper API credential — no new vendor, no new API key.

Search-only providers (e.g. SearXNG) pair with this for ``web_extract``
calls. Configure via::

    web:
      search_backend: "searxng"
      extract_backend: "decodo"

Env var (resolved from 1Password by the gateway env loader, or set
directly)::

    DECODO_BASIC_AUTH_HEADER="Basic <base64>"
"""

from __future__ import annotations

from plugins.web.decodo.provider import DecodoWebExtractProvider


def register(ctx) -> None:
    """Register the Decodo extract provider with the plugin context."""
    ctx.register_web_search_provider(DecodoWebExtractProvider())
