"""MarketData wrapper that records failed calls."""
from __future__ import annotations

import asyncio


class TrackingSource:
    """Wraps a MarketData source and records every failed call.

    Scanners skip symbols whose data fails, so without this an outage would look like
    "no opportunities" instead of "no data".
    """

    def __init__(self, inner):
        self._inner = inner
        self.exchanges = inner.exchanges
        self.calls = 0
        self.failures: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name == "close" or not asyncio.iscoroutinefunction(attr):
            return attr

        async def tracked(*args, **kwargs):
            self.calls += 1
            try:
                return await attr(*args, **kwargs)
            except Exception as e:
                self.failures.append(f"{name}{args[:2]}: {type(e).__name__}: {str(e)[:200]}")
                raise
        return tracked
