"""Remembered model judgements for the ranking stage of a search.

A judgement depends on exactly two texts: the candidate side (what the model is told about the candidate, together
with the model's name and its instructions) and the posting text the model was shown. Both are hashed into the key.
A changed resume, profile, title list, model, prompt or posting can therefore never be answered from memory: it is
a different key. Entries also expire after `ttl_seconds`, because a model behind an unchanged name may be updated.

What is remembered: the company reranker's raw relevance of a posting ("rerank"), an OpenAI / Claude judgement of a
posting ("judge"), a written explanation of the company chat model ("explain") and the audit of a posting's
relevance and eligibility statements ("audit"). Only model output is kept; the
resume and the posting text are never stored here, only their hashes. The profile a model extracted from a resume
is kept in the process only (get_local / put_local), never in the index.

The memory lives in the process and, when the job index offers its key/value store (index_meta), there too: one
row per candidate side and kind. It then survives a restart of the worker. Every failure of that store is ignored:
losing an entry costs one model call, nothing else."""
import hashlib
import json
import time
from collections import OrderedDict

PREFIX = "rankcache:"
PRUNE_EVERY_SECONDS = 6 * 3600.0


def scope_key(*parts: str) -> str:
    """Key of a candidate side: every text that is the same for all postings of one model call series."""
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:24]


def item_key(text: str) -> str:
    """Key of one posting as the model sees it."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


class RankCache:
    """One per engine process, shared by every search it runs."""

    def __init__(self, index=None, ttl_seconds: float = 7 * 86400.0, max_items: int = 6000, max_scopes: int = 128,
                 clock=time.time):
        self.index = index
        self.ttl = max(0.0, float(ttl_seconds))
        self.max_items = max(1, max_items)            # entries kept per candidate side and kind
        self.max_scopes = max(1, max_scopes)          # candidate sides held in the process
        self._clock = clock
        self._memory: OrderedDict[str, dict[str, list]] = OrderedDict()      # row key -> item key -> [value, written]
        self._local: OrderedDict[str, list] = OrderedDict()                  # key -> [value, written]; this process only
        self._last_prune = 0.0
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    @staticmethod
    def _row(kind: str, scope: str) -> str:
        return f"{PREFIX}{kind}:{scope}"

    def _fresh(self, items: dict[str, list]) -> dict[str, list]:
        cutoff = self._clock() - self.ttl
        return {k: v for k, v in items.items() if isinstance(v, list) and len(v) == 2 and float(v[1]) >= cutoff}

    async def _load(self, row: str) -> dict[str, list]:
        items = self._memory.get(row)
        if items is None:
            items = {}
            if self.index is not None:
                try:
                    raw = (await self.index.meta_prefix(row)).get("")       # meta_prefix answers with the prefix cut off
                    data = json.loads(raw) if raw else None
                    if isinstance(data, dict) and isinstance(data.get("items"), dict):
                        items = data["items"]
                except Exception:
                    items = {}
            self._memory[row] = items
            while len(self._memory) > self.max_scopes:
                self._memory.popitem(last=False)
        else:
            self._memory.move_to_end(row)
        return items

    def get_local(self, key: str):
        """A value kept in this process only (never written to the index): used for the extracted profile, which is
        a structured view of a resume and therefore stays out of the index's key/value store."""
        if not self.enabled:
            return None
        entry = self._local.get(key)
        if entry is None or float(entry[1]) < self._clock() - self.ttl:
            self._local.pop(key, None)
            return None
        self._local.move_to_end(key)
        return entry[0]

    def put_local(self, key: str, value) -> None:
        if not self.enabled:
            return
        self._local[key] = [value, self._clock()]
        self._local.move_to_end(key)
        while len(self._local) > self.max_scopes:
            self._local.popitem(last=False)

    async def get_many(self, kind: str, scope: str, wanted: dict[str, str]) -> dict:
        """`wanted`: caller's id -> item key. Returns caller's id -> remembered value, for fresh entries only."""
        if not self.enabled or not wanted:
            return {}
        items = self._fresh(await self._load(self._row(kind, scope)))
        out = {caller: items[key][0] for caller, key in wanted.items() if key in items}
        self.hits += len(out)
        self.misses += len(wanted) - len(out)
        return out

    async def put_many(self, kind: str, scope: str, values: dict[str, object]) -> None:
        """`values`: item key -> model output (anything JSON can hold)."""
        if not self.enabled or not values:
            return
        row, now = self._row(kind, scope), self._clock()
        items = self._fresh(await self._load(row))
        for key, value in values.items():
            items[key] = [value, round(now)]
        if len(items) > self.max_items:                       # keep the newest
            newest = sorted(items.items(), key=lambda kv: kv[1][1], reverse=True)[: self.max_items]
            items = dict(newest)
        self._memory[row] = items
        self._memory.move_to_end(row)
        if self.index is not None:
            try:
                await self.index.meta_set(row, json.dumps({"t": round(now), "items": items}, separators=(",", ":")))
            except Exception:
                pass
            if now - self._last_prune > PRUNE_EVERY_SECONDS:
                self._last_prune = now
                await self.prune()

    async def prune(self) -> None:
        """Drop rows nobody wrote to for a whole lifetime (PostgreSQL index only; elsewhere rows just stop being read)."""
        pool = getattr(self.index, "pool", None)
        if pool is None or not hasattr(pool, "execute"):
            return
        try:
            await pool.execute("DELETE FROM index_meta WHERE key LIKE $1 AND coalesce((value::jsonb->>'t')::double precision, 0) < $2",
                               PREFIX.replace("_", r"\_") + "%", self._clock() - self.ttl)
        except Exception:
            pass
