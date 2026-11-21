"""The company's own model service (OpenAI-compatible HTTP API; its Responses endpoint has one tool, web search).

Used for the bulk work of a search, because it has no per-call bill: title expansion, extracting postings
from fetched pages, scoring every remaining candidate against the profile (`/v1/rerank`) and writing short
explanations beyond the slice judged by OpenAI / Claude.

Built for failure: every call has a timeout; 503 (model restarting or at capacity), 429, 500, 504 and
network errors are retried with backoff, honouring `Retry-After`; when the retries are used up the caller
gets a ProviderError and falls back to the path that existed before this service."""
import asyncio
import json
import random
import re

import httpx

from .openai_provider import ProviderError

_RETRY_STATUS = (429, 500, 502, 503, 504)
_HINTS = {400: "request rejected", 401: "authentication failed (check TECHSARA_API_KEY)", 403: "access denied",
          404: "model or endpoint not found", 413: "request too large", 429: "rate limited",
          503: "model unavailable (restarting or at capacity)", 504: "timed out upstream"}
_SECRET = re.compile(r"tsk_[A-Za-z0-9_\-]{4,}")
MAX_RETRY_AFTER = 20.0
WEB_SEARCH_RETRY_SECONDS = 8.0         # longest wait before the one repeat of a web search (a search round does not wait longer)
# A (query, document) pair must fit the reranker's 4,096-token window. Sizes below are characters and leave a
# wide margin (about 4 characters per token for English text; dense text with numbers runs nearer 3).
RERANK_QUERY_CHARS = 2400
RERANK_DOC_CHARS = 5200


_LINK = re.compile(r"https://[^\s)\]>\"'|]{4,600}")


class TechsaraProvider:
    name = "TechSara"

    def __init__(self, api_key: str, base_url: str, model: str, rerank_model: str, timeout: float = 90.0,
                 retries: int = 3, concurrency: int = 4, client: httpx.AsyncClient | None = None,
                 sleep=asyncio.sleep, web_concurrency: int = 2):
        self.model = model
        self.rerank_model = rerank_model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._web_sem = asyncio.Semaphore(max(1, web_concurrency))      # web searches at once: the model's queue is shared
        self._sleep = sleep
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "retries": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        """POST with retries. Raises ProviderError with a short, secret-free message."""
        last = "request failed"
        for attempt in range(self.retries + 1):
            delay = min(MAX_RETRY_AFTER, 1.5 * 2 ** attempt) + random.random() * 0.4
            try:
                async with self._sem:
                    r = await self._client.post(self.base_url + path, json=body, headers=self._headers, timeout=self.timeout)
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        raise ProviderError("invalid JSON from the company model") from None
                    self.usage["calls"] += 1
                    usage = data.get("usage") if isinstance(data, dict) else None
                    if isinstance(usage, dict):
                        self.usage["input_tokens"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                        self.usage["output_tokens"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                    return data
                last = f"HTTP {r.status_code}: {_HINTS.get(r.status_code, 'request failed')}"
                if r.status_code not in _RETRY_STATUS or r.headers.get("x-should-retry", "").lower() == "false":
                    code = ""
                    try:
                        code = str((r.json().get("error") or {}).get("code") or "")
                    except (ValueError, AttributeError):
                        pass
                    raise ProviderError(_SECRET.sub("[redacted]", f"{last}{f' ({code})' if code else ''}"))
                retry_after = r.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = min(float(retry_after), MAX_RETRY_AFTER)
            except httpx.TimeoutException:
                last = "request timed out"
            except httpx.HTTPError:
                last = "connection error"
            if attempt < self.retries:
                self.usage["retries"] += 1
                await self._sleep(delay)
        raise ProviderError(f"{last} after {self.retries + 1} attempts")

    async def json_call(self, system: str, user: str, schema: dict, schema_name: str = "result", max_tokens: int = 6000) -> dict:
        """Chat completion that must answer with one JSON object following `schema`."""
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens, "store": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": schema_name[:64] or "result", "schema": schema}}}
        data = await self._post("/chat/completions", body)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            if choice.get("finish_reason") == "length":
                raise ProviderError("response truncated")
            parsed = json.loads(text[text.find("{"): text.rfind("}") + 1] if not text.lstrip().startswith("{") else text)
        except ProviderError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("model returned invalid JSON") from None
        if not isinstance(parsed, dict):
            raise ProviderError("model returned invalid JSON")
        return parsed

    async def web_search(self, query: str, timeout: float = 60.0) -> tuple[list[str], str]:
        """One search through the model's own `web_search` tool (POST /v1/responses, the tool forced so the model
        cannot answer from memory). Returns (result addresses, problem). The answer carries no structured tool
        output (no `web_search_call` item, no sources, no citations; checked on 2026-11-21), so the addresses are
        read from the model's text: they are untrusted, and the caller keeps only well-formed addresses on the
        configured ATS hosts and checks every one against the employer's live board. A busy or restarting model
        (HTTP 429 / 503) is asked once more after a short wait. Never raises."""
        body = {"model": self.model, "store": False, "max_output_tokens": 2500, "tools": [{"type": "web_search"}],
                "tool_choice": {"type": "web_search"},
                "input": f"Search the web once for: {query[:300]}\nThen list every search result as a markdown link "
                         "[title](url), one per line. Nothing else."}
        r = None
        for attempt in (1, 2):
            try:
                async with self._web_sem:
                    r = await self._client.post(self.base_url + "/responses", json=body, headers=self._headers, timeout=timeout)
            except httpx.TimeoutException:
                return [], "timed out"
            except httpx.HTTPError:
                return [], "connection error"
            if r.status_code not in (429, 503) or attempt == 2 or r.headers.get("x-should-retry", "").lower() == "false":
                break
            retry_after = r.headers.get("retry-after", "")
            self.usage["retries"] += 1
            await self._sleep(min(float(retry_after), WEB_SEARCH_RETRY_SECONDS) if retry_after.isdigit() else 2.0 + random.random())
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        try:
            data = r.json()
            text = "".join(str(part.get("text") or "") for item in data.get("output") or [] if isinstance(item, dict)
                           for part in item.get("content") or [] if isinstance(part, dict))
        except (ValueError, AttributeError):
            return [], "invalid answer"
        self.usage["calls"] += 1
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self.usage["input_tokens"] += int(usage.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(usage.get("output_tokens") or 0)
        urls = [u.rstrip(".,;") for u in _LINK.findall(text)]
        return list(dict.fromkeys(urls))[:40], ""

    async def rerank(self, query: str, documents: list[str], instruction: str = "") -> list[float | None]:
        """Relevance of every document to the query, in input order (None where the service returned no score).
        Scores are whatever the model gives; mapping them onto the 0-100 scale is the caller's job."""
        if not documents:
            return []
        body = {"model": self.rerank_model, "query": query[:RERANK_QUERY_CHARS],
                "documents": [(d or "-")[:RERANK_DOC_CHARS] for d in documents], "return_documents": False}
        if instruction:
            body["instruction"] = instruction[:512]
        data = await self._post("/rerank", body)
        out: list[float | None] = [None] * len(documents)
        for row in data.get("results") or []:
            try:
                index, score = int(row["index"]), float(row["relevance_score"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= index < len(out) and score == score:      # NaN never counts
                out[index] = score
        return out
