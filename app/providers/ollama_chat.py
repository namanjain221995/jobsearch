"""An Ollama cloud model as one more worker of the search (https://docs.ollama.com/cloud).

`POST https://ollama.com/api/chat` with `Authorization: Bearer <OLLAMA_API_KEY>`, `{"model", "messages", "stream": false}`;
the answer is in `message.content`. Ollama's documentation says its cloud models do not support structured outputs,
so the schema goes into the instructions and the first JSON object of the answer is taken; anything that does not
parse is a failure, never a guess. Used for small text tasks only (title variants, extra search-engine queries):
every value it returns is checked by the caller like the answer of any other model.

A refusal that will not go away (HTTP 401 / 402 / 403: wrong key, no credits) marks the provider unusable for the rest of
the process for ten minutes, so searches do not ask it again and again."""
import json
import time

import httpx

from .openai_provider import ProviderError

ENDPOINT = "https://ollama.com/api/chat"


class OllamaChat:
    name = "Ollama"

    def __init__(self, api_key: str, model: str, timeout: float = 60.0, client: httpx.AsyncClient | None = None):
        self._key = (api_key or "").strip()
        self.model = model
        self.timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self._refused_until = 0.0
        self.refusal = ""

    @property
    def usable(self) -> bool:
        return bool(self._key) and time.monotonic() >= self._refused_until

    async def aclose(self) -> None:
        await self._client.aclose()

    async def json_call(self, system: str, user: str, schema: dict, schema_name: str = "result") -> dict:
        if not self.usable:
            raise ProviderError(self.refusal or "Ollama is not configured")
        instructions = (f"{system}\n\nAnswer with ONE JSON object and nothing else. It must follow this JSON schema:\n"
                        f"{json.dumps(schema, separators=(',', ':'))}")
        body = {"model": self.model, "stream": False, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": user}]}
        try:
            r = await self._client.post(ENDPOINT, json=body, timeout=self.timeout,
                                        headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        except httpx.TimeoutException:
            raise ProviderError("Ollama: request timed out") from None
        except httpx.HTTPError:
            raise ProviderError("Ollama: connection error") from None
        if r.status_code in (401, 402, 403):
            self.refusal = {402: "Ollama: the account has no usage credits"}.get(r.status_code, "Ollama: the API key was refused")
            self._refused_until = time.monotonic() + 600
            error = ProviderError(self.refusal)
            error.fatal = True
            raise error
        if r.status_code != 200:
            raise ProviderError(f"Ollama: HTTP {r.status_code}")
        try:
            data = r.json()
            text = str((data.get("message") or {}).get("content") or "")
            self.usage["calls"] += 1
            self.usage["input_tokens"] += int(data.get("prompt_eval_count") or 0)
            self.usage["output_tokens"] += int(data.get("eval_count") or 0)
            start, end = text.find("{"), text.rfind("}")
            parsed = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except (ValueError, AttributeError, TypeError):
            parsed = None
        if not isinstance(parsed, dict):
            raise ProviderError("Ollama: the model returned no valid JSON")
        return parsed
