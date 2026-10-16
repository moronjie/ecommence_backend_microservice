import time
from collections.abc import Callable


class CircuitBreaker:
    """Opens after N consecutive failures; lets one trial call through after a cool-down."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self.recovery_seconds:
            # Half-open: allow a trial call; restart the timer so only one slips through
            self._opened_at = self._clock()
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._clock()


class BreakerRegistry:
    def __init__(self, failure_threshold: int, recovery_seconds: float) -> None:
        self._args = (failure_threshold, recovery_seconds)
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(*self._args)
        return self._breakers[name]
