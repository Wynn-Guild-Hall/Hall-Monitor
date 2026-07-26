"""Shared HTTP client for every ``external/`` API module.

Every third-party call funnels through a :class:`Requester` so we keep one
consistent policy for timeouts, retries, and rate-limit handling. Each
API module owns its own instance (one base URL, optional default headers).

Per-bucket serialisation with priority jumping
----------------------------------------------
Some APIs (notably Wynncraft v3) split their rate limit across multiple
buckets keyed by route family. Callers pass a ``bucket`` string; requests
sharing a bucket are serialised through one priority queue so we don't
accidentally hammer a single limiter. Requests to different buckets run
concurrently.

Set ``urgent=True`` to jump the bucket queue. User-facing lookups
(``/api/join/lookup``, ``/api/verify``) use this so they aren't stuck
behind a bulk background refresh.

Retry and timeout policy
------------------------
- Timeout: 10s per attempt.
- 5xx: retried once after 500 ms.
- 429: bucket pauses future requests for ``Retry-After`` /
  ``RateLimit-Reset`` seconds (default 1 s if the header is absent /
  malformed), then the 429 is raised as :class:`httpx.HTTPStatusError` so
  the caller can decide whether to fall back to another provider or bubble.
- Any other non-2xx: raised as :class:`httpx.HTTPStatusError` for the
  caller to translate.
"""

import asyncio
import itertools
from dataclasses import dataclass, field

import httpx

_TIMEOUT_S = 10.0
_RETRY_5XX_BACKOFF_S = 0.5
_DEFAULT_429_BACKOFF_S = 1.0


@dataclass
class _Bucket:
    """One serial queue with priority + 429 pause bookkeeping."""

    queue: asyncio.PriorityQueue = field(default_factory=asyncio.PriorityQueue)
    seq: "itertools.count[int]" = field(default_factory=itertools.count)
    pause_until_loop_time: float = 0.0
    worker: asyncio.Task | None = None
    active_release: asyncio.Event | None = None

    def _ensure_worker(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())

    async def acquire(self, urgent: bool) -> "_Release":
        loop = asyncio.get_running_loop()
        turn: asyncio.Future[None] = loop.create_future()
        # Priority 0 jumps the queue; seq breaks ties FIFO.
        await self.queue.put((0 if urgent else 1, next(self.seq), turn))
        self._ensure_worker()
        await turn
        return _Release(self)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                _, _, turn = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            delay = self.pause_until_loop_time - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self.active_release = asyncio.Event()
            turn.set_result(None)
            await self.active_release.wait()

    def pause(self, seconds: float) -> None:
        loop = asyncio.get_event_loop()
        self.pause_until_loop_time = max(
            self.pause_until_loop_time, loop.time() + seconds
        )


class _Release:
    """Handle returned by :meth:`_Bucket.acquire`; released by the caller."""

    def __init__(self, bucket: _Bucket):
        self._bucket = bucket
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        event = self._bucket.active_release
        if event is not None:
            event.set()


def _retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After") or response.headers.get(
        "RateLimit-Reset"
    )
    if header is None:
        return _DEFAULT_429_BACKOFF_S
    try:
        return max(0.0, float(header))
    except ValueError:
        return _DEFAULT_429_BACKOFF_S


class Requester:
    """Per-API HTTP wrapper — one instance per base URL.

    Callers use :meth:`get` (the only verb we need today). Add other verbs
    when a real caller appears.
    """

    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=_TIMEOUT_S,
            headers=headers or {},
        )
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, key: str) -> _Bucket:
        if key not in self._buckets:
            self._buckets[key] = _Bucket()
        return self._buckets[key]

    async def get(
        self,
        path: str,
        *,
        bucket: str = "default",
        urgent: bool = False,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Serialised GET against ``bucket``. Retries 5xx once, waits on 429.

        Non-2xx (other than 429 during the wait) raises
        :class:`httpx.HTTPStatusError`. Network errors propagate.
        """
        b = self._bucket(bucket)
        attempts_5xx = 0
        while True:
            release = await b.acquire(urgent)
            try:
                response = await self._client.get(path, params=params, headers=headers)
            finally:
                release.release()

            if response.status_code == 429:
                b.pause(_retry_after_seconds(response))
                response.raise_for_status()

            if 500 <= response.status_code < 600 and attempts_5xx == 0:
                attempts_5xx += 1
                await asyncio.sleep(_RETRY_5XX_BACKOFF_S)
                continue

            response.raise_for_status()
            return response

    async def aclose(self) -> None:
        await self._client.aclose()
