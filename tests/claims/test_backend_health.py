from __future__ import annotations

from fin_analyse.claims.backend_health import BackendCircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_backend_circuit_breaker_opens_after_consecutive_failures() -> None:
    clock = FakeClock()
    breaker = BackendCircuitBreaker(
        failure_threshold=3,
        cooldown_seconds=60,
        clock=clock,
    )

    breaker.record_failure("gpt5", {"error_type": "InternalServerError", "http_status": 500})
    breaker.record_failure("gpt5", {"error_type": "InternalServerError", "http_status": 500})
    assert breaker.can_try("gpt5") is True

    breaker.record_failure("gpt5", {"error_type": "InternalServerError", "http_status": 500})

    assert breaker.can_try("gpt5") is False
    assert breaker.health_status("gpt5").startswith("cooldown:")


def test_backend_circuit_breaker_recovers_after_cooldown() -> None:
    clock = FakeClock()
    breaker = BackendCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=60,
        clock=clock,
    )
    breaker.record_failure("gpt5", "empty_response")

    assert breaker.can_try("gpt5") is False

    clock.advance(61)

    assert breaker.can_try("gpt5") is True
    assert breaker.health_status("gpt5") == "available"


def test_success_resets_failure_count() -> None:
    clock = FakeClock()
    breaker = BackendCircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=60,
        clock=clock,
    )

    breaker.record_failure("deepseek", "timeout")
    breaker.record_success("deepseek")
    breaker.record_failure("deepseek", "timeout")

    assert breaker.can_try("deepseek") is True
