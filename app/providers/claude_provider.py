"""Claude via the official Anthropic SDK.
Discovery uses the server-side web search tool; structured calls use output_config.format."""
import json

import anthropic
from anthropic import AsyncAnthropic

from ..schemas import Candidate
from .openai_provider import ATS_DOMAINS, ProviderError, build_candidates

WEB_SEARCH_TOOL = "web_search_20260209"          # Opus/Sonnet 4.6 and later
WEB_SEARCH_TOOL_BASIC = "web_search_20250305"    # older models
# Models that accept the server-side refusal fallback (see Anthropic "refusals and fallback" docs).
_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def sanitize_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "HTTP 401: authentication failed (check API key)"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "HTTP 403: access denied"
    if isinstance(exc, anthropic.NotFoundError):
        return "HTTP 404: model not found (check model name)"
    if isinstance(exc, anthropic.RateLimitError):
        return "HTTP 429: rate limited"
    if isinstance(exc, anthropic.APIStatusError):
        return f"HTTP {exc.status_code}: request failed"
    if isinstance(exc, anthropic.APITimeoutError):
        return "request timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection error"
    return type(exc).__name__


class ClaudeProvider:
    name = "Claude"

    def __init__(self, api_key: str, model: str, timeout: float = 180.0):
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._use_fallbacks = model in _FALLBACK_MODELS
        self._web_tool = WEB_SEARCH_TOOL

    async def _create(self, **kwargs):
        """messages.create with the refusal fallback enabled where the model supports it."""
        kwargs.setdefault("model", self.model)
        if self._use_fallbacks:
            try:
                return await self.client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
            except anthropic.BadRequestError:
                self._use_fallbacks = False   # not available for this account/model: retry without it
        return await self.client.messages.create(**kwargs)

    @staticmethod
    def _text(message) -> str:
        if message.stop_reason == "refusal":
            raise ProviderError("request declined by the model")
        return "\n".join(b.text for b in message.content if b.type == "text")

    async def json_call(self, system: str, user: str, schema: dict, schema_name: str = "") -> dict:
        try:
            message = await self._create(
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            )
            if message.stop_reason == "max_tokens":
                raise ProviderError("response truncated")
            return json.loads(self._text(message))
        except ProviderError:
            raise
        except ValueError as exc:
            raise ProviderError("model returned invalid JSON") from exc
        except Exception as exc:
            raise ProviderError(sanitize_error(exc)) from None

    async def web_discover(self, system: str, prompt: str, max_searches: int, restrict_to_ats: bool) -> tuple[list[Candidate], int]:
        """Run live web search. Returns (candidates, number of searches actually executed)."""
        def tool(kind: str) -> dict:
            t = {"type": kind, "name": "web_search", "max_uses": max_searches,
                 "user_location": {"type": "approximate", "country": "US"}}
            if restrict_to_ats:
                t["allowed_domains"] = ATS_DOMAINS
            return t

        messages = [{"role": "user", "content": prompt}]
        sourced: dict[str, str] = {}
        searches = 0
        text_parts: list[str] = []
        try:
            for _ in range(4):   # bounded pause_turn continuations
                try:
                    message = await self._create(max_tokens=8000, system=system, messages=messages,
                                                 tools=[tool(self._web_tool)], output_config={"effort": "low"})
                except anthropic.BadRequestError:
                    if self._web_tool == WEB_SEARCH_TOOL_BASIC:
                        raise
                    self._web_tool = WEB_SEARCH_TOOL_BASIC
                    message = await self._create(max_tokens=8000, system=system, messages=messages,
                                                 tools=[tool(self._web_tool)], output_config={"effort": "low"})
                if message.stop_reason == "refusal":
                    raise ProviderError("request declined by the model")
                for block in message.content:
                    if block.type == "server_tool_use" and getattr(block, "name", "") == "web_search":
                        searches += 1
                    elif block.type == "web_search_tool_result":
                        content = block.content
                        if isinstance(content, list):          # success: list of results; error: single object
                            for result in content:
                                url = getattr(result, "url", None)
                                if url:
                                    sourced.setdefault(url, getattr(result, "title", "") or "")
                    elif block.type == "text":
                        text_parts.append(block.text)
                        for cite in getattr(block, "citations", None) or []:
                            url = getattr(cite, "url", None)
                            if url:
                                sourced.setdefault(url, getattr(cite, "title", "") or "")
                if message.stop_reason != "pause_turn":
                    break
                messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": message.content}]
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(sanitize_error(exc)) from None
        if searches == 0:
            return [], 0
        return build_candidates(self.name, "\n".join(text_parts), sourced), searches
