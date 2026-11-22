"""Ollama's web search API as one more search engine (https://docs.ollama.com/capabilities/web-search).

`POST https://ollama.com/api/web_search` with `Authorization: Bearer <OLLAMA_API_KEY>` and `{"query", "max_results"}`
(default 5, at most 10) answers `{"results": [{"title", "url", "content"}]}`. It is a plain search API: no model is
called and no tokens are spent, and every address comes from the API's own `url` field, never from text a model
wrote. Only the addresses are used here; titles and snippets are dropped like those of every other engine, and each
address is then checked by the engine's normal lead check (board API or the posting's own page).

The documentation states no rate limit and no price for this endpoint; failures are therefore counted and an engine
that keeps failing is switched off for the rest of a search by the caller."""
import asyncio

import httpx

ENDPOINT = "https://ollama.com/api/web_search"
MAX_RESULTS = 10                     # the documented ceiling


class OllamaSearch:
    def __init__(self, api_key: str, client: httpx.AsyncClient, timeout: float = 20.0, concurrency: int = 4):
        self._key = (api_key or "").strip()
        self.client = client
        self.timeout = timeout
        self._gate = asyncio.Semaphore(max(1, concurrency))
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> tuple[list[str], str]:
        """(result addresses, problem). Never raises; the key is never part of a message."""
        if not self.enabled:
            return [], "not configured"
        body = {"query": " ".join(str(query or "").split())[:400], "max_results": max(1, min(MAX_RESULTS, int(max_results)))}
        try:
            async with self._gate:
                self.calls += 1
                r = await self.client.post(ENDPOINT, json=body, timeout=self.timeout, follow_redirects=False,
                                           headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        except httpx.TimeoutException:
            return [], "timed out"
        except httpx.HTTPError:
            return [], "connection error"
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        try:
            rows = r.json().get("results") or []
        except (ValueError, AttributeError):
            return [], "invalid answer"
        urls = [str(row.get("url")).strip() for row in rows if isinstance(row, dict) and isinstance(row.get("url"), str)]
        return list(dict.fromkeys(u for u in urls if u.startswith(("https://", "http://")) and len(u) <= 600)), ""
