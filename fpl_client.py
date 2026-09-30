"""Async FPL API client: browser User-Agent, retries with backoff, bootstrap cache."""
import asyncio
import logging
import time

import httpx

import config

log = logging.getLogger(__name__)


class FPLError(Exception):
    """Raised when the FPL API can't be reached or returns an unusable response."""


class FPLNotFound(FPLError):
    pass


class FPLClient:
    def __init__(self, retries: int = 4, backoff: float = 1.0, client: httpx.AsyncClient | None = None):
        self.retries = retries
        self.backoff = backoff
        self._client = client or httpx.AsyncClient(
            base_url=config.FPL_BASE_URL,
            headers={"User-Agent": config.FPL_USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
        self._cache: dict[str, tuple[float, object]] = {}
        self._standings_cache: dict[tuple[int, int], tuple[float, dict]] = {}
        self._bootstrap: dict | None = None
        self._bootstrap_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str):
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self._sem:
                    resp = await self._client.get(path)
                if resp.status_code == 404:
                    raise FPLNotFound(path)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise FPLError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except FPLNotFound:
                raise
            except (httpx.HTTPError, FPLError, ValueError) as exc:
                last_exc = exc
                log.warning("FPL GET %s failed (attempt %d/%d): %s", path, attempt + 1, self.retries, exc)
                if attempt < self.retries - 1:
                    await asyncio.sleep(self.backoff * (2 ** attempt))
        raise FPLError(f"FPL API unavailable for {path}: {last_exc}")

    async def bootstrap(self) -> dict:
        if self._bootstrap and time.time() - self._bootstrap_at < config.BOOTSTRAP_TTL_SECONDS:
            return self._bootstrap
        try:
            self._bootstrap = await self._get("bootstrap-static/")
            self._bootstrap_at = time.time()
        except FPLError:
            if self._bootstrap:  # serve stale data rather than fail
                log.warning("Serving stale bootstrap-static data")
                return self._bootstrap
            raise
        return self._bootstrap

    async def current_gameweek(self) -> int:
        events = (await self.bootstrap())["events"]
        for e in events:
            if e.get("is_current"):
                return e["id"]
        for e in events:
            if e.get("is_next"):
                return e["id"]
        finished = [e["id"] for e in events if e.get("finished")]
        return max(finished) if finished else 1

    async def entry(self, team_id: int) -> dict:
        return await self._get(f"entry/{team_id}/")

    async def history(self, team_id: int) -> dict:
        return await self._get(f"entry/{team_id}/history/")

    async def picks(self, team_id: int, gw: int) -> dict:
        return await self._get(f"entry/{team_id}/event/{gw}/picks/")

    async def standings(self, league_id: int, page: int = 1) -> dict:
        """One page (50 managers) of a classic league. Cached briefly."""
        key = (league_id, page)
        hit = self._standings_cache.get(key)
        if hit and time.time() - hit[0] < config.STANDINGS_TTL_SECONDS:
            return hit[1]
        data = await self._get(f"leagues-classic/{league_id}/standings/?page_standings={page}")
        self._standings_cache[key] = (time.time(), data)
        return data

    async def _cached(self, path: str, ttl: float):
        hit = self._cache.get(path)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            data = await self._get(path)
        except FPLError:
            if hit:  # serve stale rather than fail mid-gameweek
                log.warning("Serving stale %s", path)
                return hit[1]
            raise
        self._cache[path] = (time.time(), data)
        return data

    async def live(self, gw: int) -> dict:
        """In-match points for every player this gameweek (cached 2 minutes to respect rate limits)."""
        return await self._cached(f"event/{gw}/live/", config.LIVE_TTL_SECONDS)

    async def fixtures(self, event: int | None = None) -> list:
        """Fixtures for one gameweek (short cache, includes live minutes) or the whole season (1h cache)."""
        if event is None:
            return await self._cached("fixtures/", config.BOOTSTRAP_TTL_SECONDS)
        return await self._cached(f"fixtures/?event={event}", config.LIVE_TTL_SECONDS)

    async def transfers(self, team_id: int) -> list:
        return await self._get(f"entry/{team_id}/transfers/")
