from threading import RLock
from typing import Protocol

from limits import parse
from slowapi import Limiter
from slowapi.util import get_remote_address


class RateLimiterPort(Protocol):
    def hit(self, limit: str, key: str) -> bool:
        ...


class SlowAPIRateLimiter(RateLimiterPort):
    def __init__(self) -> None:
        self._limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
        self._lock = RLock()

    def hit(self, limit: str, key: str) -> bool:
        with self._lock:
            return self._limiter.limiter.hit(parse(limit), key)

    def reset(self) -> None:
        self._limiter.reset()


rate_limiter: RateLimiterPort = SlowAPIRateLimiter()
