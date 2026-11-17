"""OpenAI via the official SDK and the Responses API.
Live discovery uses the hosted `web_search` tool; only URLs returned as tool sources or
url_citation annotations are treated as cited leads."""
import json
import re

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, BadRequestError

from ..schemas import Candidate

ATS_DOMAINS = ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
               "myworkdayjobs.com", "apply.workable.com", "jobs.smartrecruiters.com", "ats.rippling.com", "jobs.gem.com"]
_SECRET = re.compile(r"sk-[A-Za-z0-9_\-*\.]{6,}")


class ProviderError(Exception):
    """Sanitized provider failure (never contains keys or resume text)."""


def sanitize_error(exc: Exception) -> str:
    if isinstance(exc, APIStatusError):
        code = exc.status_code
        hint = {401: "authentication failed (check API key)", 403: "access denied", 404: "model or endpoint not found (check model name)",
                429: "rate limited or quota exceeded"}.get(code, "request failed")
        return f"HTTP {code}: {hint}"
    if isinstance(exc, APITimeoutError):
        return "request timed out"
    if isinstance(exc, APIConnectionError):
        return "connection error"
    return _SECRET.sub("[redacted]", f"{type(exc).__name__}")


def parse_json_array(text: str) -> list[dict]:
    """Leniently pull a JSON array of objects out of model text."""
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


class OpenAIProvider:
    name = "OpenAI"

    def __init__(self, api_key: str, model: str, timeout: float = 180.0, reasoning_effort: str = "low"):
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        # Extraction and scoring do not need deep reasoning; a low effort makes these calls much faster.
        self._reasoning = {"reasoning": {"effort": reasoning_effort}} if reasoning_effort else {}

    async def json_call(self, system: str, user: str, schema: dict, schema_name: str) -> dict:
        request = dict(model=self.model, instructions=system, input=user, store=False,
                       text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}})
        try:
            try:
                resp = await self.client.responses.create(**request, **self._reasoning)
            except BadRequestError:
                if not self._reasoning:
                    raise
                self._reasoning = {}          # this model has no reasoning-effort setting
                resp = await self.client.responses.create(**request)
            return json.loads(resp.output_text)
        except ValueError as exc:
            raise ProviderError("model returned invalid JSON") from exc
        except Exception as exc:
            raise ProviderError(sanitize_error(exc)) from None

    async def web_discover(self, system: str, prompt: str, max_searches: int, restrict_to_ats: bool) -> tuple[list[Candidate], int]:
        """Run live web search. Returns (candidates, number of search calls actually executed)."""
        tool: dict = {"type": "web_search", "user_location": {"type": "approximate", "country": "US"}}
        if restrict_to_ats:
            tool["filters"] = {"allowed_domains": ATS_DOMAINS}
        try:
            resp = await self.client.responses.create(
                model=self.model,
                instructions=system,
                input=prompt,
                tools=[tool],
                tool_choice="auto",
                include=["web_search_call.action.sources"],
                max_tool_calls=max_searches,
                store=False,
            )
        except Exception as exc:
            raise ProviderError(sanitize_error(exc)) from None
        sourced: dict[str, str] = {}
        searches = 0
        text_parts: list[str] = []
        for item in resp.output or []:
            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            if data.get("type") == "web_search_call":
                searches += 1
                for src in ((data.get("action") or {}).get("sources") or []):
                    if isinstance(src, dict) and src.get("url"):
                        sourced.setdefault(src["url"], src.get("title") or "")
            elif data.get("type") == "message":
                for part in data.get("content") or []:
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text") or "")
                        for ann in part.get("annotations") or []:
                            if ann.get("type") == "url_citation" and ann.get("url"):
                                sourced.setdefault(ann["url"], ann.get("title") or "")
        if searches == 0:
            # No live search happened: nothing in this response may be presented as a web result.
            return [], 0
        return build_candidates(self.name, "\n".join(text_parts), sourced), searches


def build_candidates(provider: str, text: str, sourced: dict[str, str]) -> list[Candidate]:
    """Combine the model's listed leads with URLs the search tool actually returned.
    `cited` is True only for URLs present in tool sources / citations."""
    def key(u: str) -> str:
        return re.sub(r"[?#].*$", "", u.strip()).rstrip("/").lower()

    sourced_keys = {key(u) for u in sourced}
    out: dict[str, Candidate] = {}
    for row in parse_json_array(text):
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        out[key(url)] = Candidate(url=url, title=str(row.get("title") or "")[:200], company=str(row.get("company") or "")[:120],
                                  location=str(row.get("location") or "")[:160], provider=provider, cited=key(url) in sourced_keys)
    for url, title in sourced.items():
        if key(url) not in out and url.startswith(("http://", "https://")):
            out[key(url)] = Candidate(url=url, title=(title or "")[:200], provider=provider, cited=True)
    return list(out.values())
