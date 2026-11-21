"""Claude via the official Anthropic SDK.
Structured calls use output_config.format. Discovery uses the server-side web search tool
(https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool):

- tool version `web_search_20260318` (the current one), falling back to `web_search_20260209` and then to the
  basic `web_search_20250305` when a model or account refuses the newer one;
- `max_uses` caps the searches of a call, `allowed_domains` OR `blocked_domains` (never both: the API answers 400)
  keep it on the hosts the engine can read, `user_location` is the United States, with a state when given;
- on the two newer versions the searches run from the code execution sandbox ("dynamic filtering"): only what the
  model's program prints reaches its context. Measured on 2026-11-21: calling the tool directly put about 15,000
  input tokens per search into the context, and a model left to itself wrote several programs per call and ran the
  same query two or three times. The prompt therefore asks for ONE program that runs every query once and prints
  only address and title of each result;
- lead addresses are read from the `web_search_tool_result` blocks of the response, which hold every result whatever
  the program printed (`response_inclusion` stays "full"), and from citations. An address the model merely wrote is
  dropped (`build_candidates`);
- searches are counted from `usage.server_tool_use.web_search_requests` (what is billed: 10 USD per 1,000);
- a search that failed comes back as an error object inside a 200 answer (`too_many_requests`, `max_uses_exceeded`,
  `request_too_large`, ...). A call whose searches all failed is reported as a failure, not as "found nothing";
- `pause_turn` is continued as documented: the paused assistant content is appended unchanged and the container of
  the code execution is named again."""
import json

import anthropic
from anthropic import AsyncAnthropic

from .openai_provider import (ATS_DOMAINS, LEADS_SCHEMA, Leads, ProviderError, build_candidates, page_age_days, parse_leads,
                              searched_sites)

WEB_SEARCH_TOOLS = ("web_search_20260318",       # current: dynamic filtering and response inclusion control
                    "web_search_20260209",       # dynamic filtering (Claude 4.6 and later)
                    "web_search_20250305")       # basic tool: every model, direct calls only
WEB_SEARCH_TOOL, WEB_SEARCH_TOOL_BASIC = WEB_SEARCH_TOOLS[0], WEB_SEARCH_TOOLS[-1]
MAX_CONTINUATIONS = 3                            # pause_turn continuations of one web-search call
SEARCH_MAX_TOKENS = 16000                        # the program the model writes and its list of leads are output tokens
SEARCH_HOW = (
    "\n\nHow to run the searches: write ONE program for the code execution tool that calls the web search function once "
    "for every query (all of them at the same time) and prints, for every result, only its address and its title, "
    "one result per line. The function returns its results as text, so parse it defensively and keep what it returned in "
    "variables: never run the same query a second time, and do not print page content.")
_RATE_LIMITED = "too_many_requests"
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
    site_list_needed = False          # this tool holds a `site:` query to its site (365 of 365 addresses of 60 such queries on 2026-11-21)

    def __init__(self, api_key: str, model: str, timeout: float = 180.0, search_model: str = ""):
        self.model = model
        self.search_model = search_model or model         # web-search discovery may run on another model (ANTHROPIC_SEARCH_MODEL)
        self.client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._fallbacks = {m: m in _FALLBACK_MODELS for m in (self.model, self.search_model)}     # refusal fallback, per model
        self._web_tool = 0                # index into WEB_SEARCH_TOOLS: steps down when the newer version is refused
        self._lead_schema = True          # False once the JSON-schema answer was refused together with web search
        # Tokens and web searches of this search, for the cost report (counts only).
        self.usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "ungrounded_addresses": 0}

    def _count(self, message) -> None:
        usage = getattr(message, "usage", None)
        for name, fields in (("input_tokens", ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                             ("output_tokens", ("output_tokens",))):
            for f in fields:
                value = getattr(usage, f, 0)
                if isinstance(value, int):
                    self.usage[name] += value

    async def _create(self, client=None, **kwargs):
        """messages.create with the refusal fallback enabled where the model supports it."""
        client = client or self.client
        model = kwargs.setdefault("model", self.model)
        if self._fallbacks.get(model):
            try:
                return await client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
            except anthropic.BadRequestError:
                self._fallbacks[model] = False   # not available for this account/model: retry without it
        return await client.messages.create(**kwargs)

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
            self._count(message)
            if message.stop_reason == "max_tokens":
                raise ProviderError("response truncated")
            return json.loads(self._text(message))
        except ProviderError:
            raise
        except ValueError as exc:
            raise ProviderError("model returned invalid JSON") from exc
        except Exception as exc:
            raise ProviderError(sanitize_error(exc)) from None

    async def web_discover(self, system: str, prompt: str, max_searches: int, restrict_to_ats: bool, *,
                           allowed_domains: list[str] | None = None, blocked_domains: list[str] | None = None,
                           region: str = "", own_searches: bool = False, timeout: float | None = None) -> tuple[Leads, int]:
        """Run live web search. Returns (leads, web searches actually run and billed); the leads are every address the
        search tool returned, the model's own picks first (`Leads.picks` of them). `own_searches` (the call composes
        its searches itself) changes nothing here: this model ran such searches one by one at its low effort.
        Either `allowed_domains` (the hosts of the call) or `blocked_domains` (for a search that is not limited to
        hosts: job boards, and the ATS hosts other calls search) is sent, never both: the API rejects the pair.
        `timeout` bounds every request of this call (one retry at most)."""
        def tool(kind: str) -> dict:
            location = {"type": "approximate", "country": "US"}
            if region:
                location["region"] = region
            t = {"type": kind, "name": "web_search", "max_uses": max_searches, "user_location": location}
            if allowed_domains:
                t["allowed_domains"] = list(allowed_domains)
            elif restrict_to_ats:
                t["allowed_domains"] = ATS_DOMAINS
            elif blocked_domains:
                t["blocked_domains"] = list(blocked_domains)
            return t

        client = self.client.with_options(timeout=timeout, max_retries=1) if timeout else self.client
        history: list = []                # paused assistant turns of this call, sent back unchanged

        async def ask(container: str | None):
            """One request, stepping down on HTTP 400: first without the JSON-schema answer, then to an older tool.
            A request that is refused in every form was wrong for another reason: the settings are put back."""
            before = (self._lead_schema, self._web_tool)
            while True:
                kind = WEB_SEARCH_TOOLS[self._web_tool]
                config: dict = {"effort": "low"}
                if self._lead_schema:
                    config["format"] = {"type": "json_schema", "schema": LEADS_SCHEMA}
                # The basic tool has no code execution: its searches are called directly, nothing to explain.
                user = {"role": "user", "content": prompt + (SEARCH_HOW if kind != WEB_SEARCH_TOOL_BASIC else "")}
                extra = {"container": container} if container else {}
                try:
                    return await self._create(client, model=self.search_model, max_tokens=SEARCH_MAX_TOKENS, system=system,
                                              messages=[user, *history], tools=[tool(kind)], output_config=config, **extra)
                except anthropic.BadRequestError:
                    if self._lead_schema:
                        self._lead_schema = False
                    elif self._web_tool < len(WEB_SEARCH_TOOLS) - 1:
                        self._web_tool += 1
                    else:
                        self._lead_schema, self._web_tool = before
                        raise

        sourced: dict[str, str] = {}
        ages: dict[str, int] = {}             # address -> days since the page was last updated ("page_age" of a result)
        queries: list[str] = []
        billed = blocks = failed = 0
        errors: dict[str, int] = {}
        text_parts: list[str] = []
        container: str | None = None
        try:
            for _ in range(1 + MAX_CONTINUATIONS):
                message = await ask(container)
                self._count(message)
                if message.stop_reason == "refusal":
                    raise ProviderError("request declined by the model")
                requests = getattr(getattr(getattr(message, "usage", None), "server_tool_use", None), "web_search_requests", None)
                billed += requests if isinstance(requests, int) else 0
                for block in message.content:
                    if block.type == "server_tool_use" and getattr(block, "name", "") == "web_search":
                        blocks += 1
                        query = (getattr(block, "input", None) or {}).get("query") if isinstance(getattr(block, "input", None), dict) else None
                        if query:
                            queries.append(str(query))
                    elif block.type == "web_search_tool_result":
                        content = block.content
                        if isinstance(content, list):          # success: list of results (empty = no match); error: one object
                            for result in content:
                                url = getattr(result, "url", None)
                                if url:
                                    sourced.setdefault(url, getattr(result, "title", "") or "")
                                    age = page_age_days(getattr(result, "page_age", None))
                                    if age is not None:
                                        ages.setdefault(url, age)
                        else:
                            failed += 1
                            code = str(getattr(content, "error_code", "") or "error")
                            errors[code] = errors.get(code, 0) + 1
                    elif block.type == "text":
                        text_parts.append(block.text)
                        for cite in getattr(block, "citations", None) or []:
                            url = getattr(cite, "url", None)
                            if url:
                                sourced.setdefault(url, getattr(cite, "title", "") or "")
                if message.stop_reason != "pause_turn":
                    break
                # Continue the paused turn: the assistant content goes back unchanged, and the container of the code
                # execution is named again so the program's variables are still there.
                history.append({"role": "assistant", "content": message.content})
                container = getattr(getattr(message, "container", None), "id", None) or container
        except ProviderError:
            raise
        except Exception as exc:
            error = ProviderError(sanitize_error(exc))
            error.fatal = isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError))
            raise error from None
        searches = billed or max(0, blocks - failed)
        self.usage["web_searches"] += searches
        if failed and not sourced and failed >= blocks:
            # Every search of the call came back as an error object: that is a failed call, not an empty result.
            code = max(errors, key=errors.get)
            raise ProviderError("web search rate limited" if code == _RATE_LIMITED else f"web search failed ({code})")
        if searches == 0:
            return Leads(), 0
        # The model's list is its last text block (the schema holds it to JSON); notes between programs are not it.
        rows = parse_leads(text_parts[-1] if text_parts else "") or parse_leads("\n".join(text_parts))
        leads, ungrounded = build_candidates(self.name, rows, sourced)
        leads.sites = searched_sites(queries)
        leads.ages = ages
        self.usage["ungrounded_addresses"] += ungrounded
        return leads, searches
