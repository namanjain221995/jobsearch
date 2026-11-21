"""Which boards a search reads: those whose last read listed the requested titles first, whatever their platform;
fresh boards outside the budget; few units of one-request-a-second sources. No network."""
import time

from app.providers.registry import Unit
from app.search_engine import PACED_UNITS_PER_SEARCH, RELEVANT_READ_SECONDS, paced_unit
from app.search_scope import NO_SCOPE, Scope
from tests.test_wider_search import WideAI, run, wide_engine, wide_settings


class PlanIndex:
    """The two questions the reading plan asks the job index."""
    is_async = True

    def __init__(self, rows=(), fresh=(), fail=False):
        self.rows, self.fail, self.asked = list(rows), fail, None
        self.states = {(u.src, u.key, u.scope): (time.time() - 60, time.time() + 600, 1) for u in fresh}

    async def unit_states(self):
        return self.states

    async def relevant_units(self, tokens, max_age, limit=0):
        self.asked = (tokens, max_age)
        if self.fail:
            raise RuntimeError("index down")
        return self.rows


def board(src: str, key: str) -> Unit:
    return Unit(src, key, "")


def plan(units, index, limit=1500, scope=NO_SCOPE, titles=("AI Engineer",), related=()):
    engine = wide_engine(WideAI(available=False), wide_settings(searxng_url=""), sources=("greenhouse", "lever", "ashby"))
    engine.index = index

    async def go():
        try:
            return await engine._reading_plan(list(units), list(titles), list(related), limit, scope)
        finally:
            await engine.client.aclose()

    return run(go())


GREENHOUSE = [board("greenhouse", f"g{i}") for i in range(1, 6)]
LEVER = [board("lever", f"l{i}") for i in range(1, 4)]
ASHBY = [board("ashby", "a1"), board("ashby", "a2")]
ROWS = [("lever", "l2", "AI Engineer"), ("lever", "l2", "Senior AI Engineer"), ("lever", "l2", "AI Engineer, Platform"),
        ("greenhouse", "g4", "AI Engineer"), ("greenhouse", "g4", "Staff AI Engineer"),
        ("ashby", "a1", "AI Engineer"),
        ("greenhouse", "g1", "Sales Manager"), ("greenhouse", "g1", "Account Executive")]      # found by a token, not a fitting title


def test_boards_that_listed_the_requested_titles_are_read_first_whatever_their_platform():
    units = [*GREENHOUSE, *LEVER, *ASHBY]
    index = PlanIndex(ROWS)
    ordered = plan(units, index)
    assert ordered[:3] == [board("lever", "l2"), board("greenhouse", "g4"), board("ashby", "a1")]
    assert ordered[3:] == [u for u in units if u not in ordered[:3]]                # the others keep their order
    assert index.asked[1] == RELEVANT_READ_SECONDS and "ai" in index.asked[0]
    # A budget of three boards: the three that listed the titles, not the first three Greenhouse boards.
    assert plan(units, PlanIndex(ROWS), limit=3) == [board("lever", "l2"), board("greenhouse", "g4"), board("ashby", "a1")]
    # Nothing known about any board (a new installation, or the index cannot say): the order is as it was.
    assert plan(units, PlanIndex()) == units and plan(units, PlanIndex(ROWS, fail=True)) == units


def test_fresh_units_cost_nothing_and_query_units_are_always_kept():
    search, feed = Unit("workable", "*", "ai engineer"), Unit("remoteok", "*", "")
    units = [*GREENHOUSE, *LEVER, search, feed]
    fresh = [board("greenhouse", "g4"), board("lever", "l1")]
    ordered = plan(units, PlanIndex(ROWS, fresh=fresh), limit=4)
    # The budget of four goes to stale units: the two query units first, then the best two stale boards. The fresh ones follow.
    assert ordered[:2] == [search, feed] and ordered[2] == board("lever", "l2") and len(ordered) == 4 + len(fresh)
    assert ordered[4:] == fresh and board("greenhouse", "g5") not in ordered


def test_one_request_a_second_sources_get_few_units_per_search_their_best_matches_first():
    tenants = [Unit("workday", f"t{i}|wd5|Careers", "ai engineer") for i in range(70)]
    accounts = [Unit("workable", f"acct{i}", "") for i in range(50)]
    assert all(paced_unit(u) for u in [*tenants, *accounts]) and not paced_unit(Unit("workable", "*", "ai")) and not paced_unit(GREENHOUSE[0])
    rows = [("workday", "t66|wd5|Careers", "AI Engineer"), ("workable", "acct49", "AI Engineer")]
    fresh = tenants[:5]
    ordered = plan([*GREENHOUSE, *tenants, *accounts], PlanIndex(rows, fresh=fresh))
    stale_tenants = [u for u in ordered if u.src == "workday" and u not in fresh]
    assert len(stale_tenants) == PACED_UNITS_PER_SEARCH and stale_tenants[0].key == "t66|wd5|Careers"
    assert len([u for u in ordered if u.src == "workable"]) == PACED_UNITS_PER_SEARCH
    assert ordered[:2] == [stale_tenants[0], Unit("workable", "acct49", "")]        # the two that listed the title lead the plan
    assert all(u in ordered for u in [*GREENHOUSE, *fresh])                        # fast boards and fresh units are not capped


def test_startup_focus_still_puts_startup_platforms_first():
    units = [board("workday", "big|wd1|Jobs"), *GREENHOUSE[:2], *LEVER[:2]]
    engine = wide_engine(WideAI(available=False), wide_settings(searxng_url=""), sources=("greenhouse", "lever", "ashby"))
    focus = Scope.of([], True, {**engine.specs, "workday": None})
    run(engine.aclose())
    rows = [("workday", "big|wd1|Jobs", "AI Engineer"), ("lever", "l2", "AI Engineer")]
    ordered = plan(units, PlanIndex(rows), scope=focus)
    assert ordered[0] == board("lever", "l2") and ordered[-1] == board("workday", "big|wd1|Jobs")      # focus first, relevance inside it
