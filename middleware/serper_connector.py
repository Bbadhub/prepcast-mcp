"""
Serper Web Enrichment Middleware — MCP SaaS Template

Silently fetches web context (snippets, links) for tool results.
Results are memory-cached with a 24-hour TTL.
Completely fail-safe: if Serper is unavailable or unconfigured, returns
an empty dict without raising or logging at warning level.

Environment Variables:
    SERPER_API_KEY  - API key for google.serper.dev (required to enrich)
    SERPER_TOOLS    - Comma-separated list of tool names to enrich, or "all"
                      (default: "all")
"""

import os
import time
import hashlib
import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SERPER_URL = "https://google.serper.dev/search"
_CACHE_TTL_SECONDS = 86_400  # 24 hours
_REQUEST_TIMEOUT = 5.0       # seconds


def _parse_tool_set(raw: str) -> Optional[Set[str]]:
    """Return None (meaning 'all') or a set of lowercase tool names."""
    raw = raw.strip().lower()
    if raw in ("all", "*", ""):
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


# ---------------------------------------------------------------------------
# SerperConnector
# ---------------------------------------------------------------------------

class SerperConnector:
    """
    Cached, fail-safe Serper enrichment connector.

    Usage:
        serper = get_serper()
        if serper.should_enrich("search_inventory"):
            ctx = await serper.enrich("vintage levi jeans 501", context_hint="price comps")
    """

    def __init__(self, api_key: str = "", tools_config: str = "all"):
        self._api_key = api_key
        self._tool_set: Optional[Set[str]] = _parse_tool_set(tools_config)
        # Cache: query_hash -> (timestamp, result_dict)
        self._cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_enrich(self, tool_name: str) -> bool:
        """Return True if this tool should receive Serper enrichment."""
        if not self._api_key:
            return False
        if self._tool_set is None:
            return True
        return tool_name.lower() in self._tool_set

    async def enrich(self, query: str, context_hint: str = "") -> Dict[str, Any]:
        """
        Fetch web context for a query.

        Returns:
            {
                "snippets": ["..."],
                "links": [{"title": "...", "url": "..."}],
                "enriched": True/False,
            }

        On any failure (network, auth, parse) returns {"enriched": False}.
        """
        if not self._api_key or not query:
            return {"enriched": False}

        # Build cache key from query + optional hint
        cache_key = self._cache_key(query + context_hint)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        result = await self._fetch(query, context_hint)
        if result.get("enriched"):
            self._set_cached(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch(self, query: str, context_hint: str) -> Dict[str, Any]:
        """Call Serper API and parse the response."""
        try:
            import httpx
        except ImportError:
            logger.debug("serper_connector: httpx not available")
            return {"enriched": False}

        payload = {"q": query, "num": 5}
        if context_hint:
            # Append hint to query for better signal without a separate field
            payload["q"] = f"{query} {context_hint}".strip()

        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(_SERPER_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.debug("serper_connector: API call failed", exc_info=True)
            return {"enriched": False}

        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract snippets and links from the Serper JSON response."""
        snippets: List[str] = []
        links: List[Dict[str, str]] = []

        # Organic results
        for item in data.get("organic", []):
            snippet = item.get("snippet", "").strip()
            if snippet:
                snippets.append(snippet)
            title = item.get("title", "").strip()
            url = item.get("link", "").strip()
            if title and url:
                links.append({"title": title, "url": url})

        # Answer box (when present — high-value snippet)
        answer_box = data.get("answerBox", {})
        if answer_box:
            ab_snippet = answer_box.get("snippet") or answer_box.get("answer", "")
            if ab_snippet and ab_snippet not in snippets:
                snippets.insert(0, ab_snippet.strip())

        return {
            "snippets": snippets,
            "links": links,
            "enriched": bool(snippets or links),
        }

    @staticmethod
    def _cache_key(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _get_cached(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > _CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        return value

    def _set_cached(self, key: str, value: Dict[str, Any]) -> None:
        self._cache[key] = (time.time(), value)

    def cache_size(self) -> int:
        """Return the number of cached entries (for diagnostics)."""
        return len(self._cache)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[SerperConnector] = None


def get_serper() -> SerperConnector:
    """Return the global SerperConnector singleton."""
    global _instance
    if _instance is None:
        _instance = SerperConnector(
            api_key=os.environ.get("SERPER_API_KEY", ""),
            tools_config=os.environ.get("SERPER_TOOLS", "all"),
        )
    return _instance
