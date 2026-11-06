"""The budget's reservations, shared between threads."""

import threading

import pytest

from pr_review.arms import Arm
from pr_review.llm import (
    REPORTED_COST_MARGIN,
    Budget,
    BudgetExceeded,
    Cancelled,
    worst_case_cost,
)


def test_reservation_waits_for_a_call_in_flight_to_settle() -> None:
    budget = Budget(1.0)
    first = budget.reserve(0.6)
    got: list[float] = []
    waiter = threading.Thread(target=lambda: got.append(budget.reserve(0.6)))
    waiter.start()
    waiter.join(0.2)
    assert waiter.is_alive()  # 0.6 + 0.6 > 1.0 while the first is in flight
    budget.settle(first, 0.1)
    waiter.join(2)
    assert got == [0.6]
    assert budget.spent_usd == pytest.approx(0.1)
    assert budget.reserved_usd == pytest.approx(0.6)


def test_reservation_is_refused_once_nothing_in_flight_can_free_room() -> None:
    budget = Budget(1.0)
    first = budget.reserve(0.6)
    errors: list[Exception] = []

    def reserve() -> None:
        try:
            budget.reserve(0.6)
        except BudgetExceeded as e:
            errors.append(e)

    waiter = threading.Thread(target=reserve)
    waiter.start()
    budget.settle(first, 0.6)
    waiter.join(2)
    assert not waiter.is_alive()
    assert len(errors) == 1
    assert budget.spent_usd == pytest.approx(0.6)


def test_unpriced_reservations_take_all_that_is_left() -> None:
    budget = Budget(1.0)
    held = budget.reserve(None)
    assert held == pytest.approx(1.0)
    budget.settle(held, 0.4)
    assert budget.reserve(None) == pytest.approx(0.6)


def test_estimate_larger_than_the_budget_is_refused_at_once() -> None:
    with pytest.raises(BudgetExceeded):
        Budget(0.1).reserve(0.2)


def test_a_failed_call_cancels_waiting_reservations() -> None:
    budget = Budget(1.0)
    first = budget.reserve(0.6)
    errors: list[Exception] = []

    def reserve() -> None:
        try:
            budget.reserve(0.6)
        except Cancelled as e:
            errors.append(e)

    waiter = threading.Thread(target=reserve)
    waiter.start()
    budget.settle(first, 0.0, failed=True)  # frees room, but the run is over
    waiter.join(2)
    assert len(errors) == 1
    with pytest.raises(Cancelled):
        budget.reserve(0.1)


def test_settling_above_the_reservation_warns() -> None:
    budget = Budget(1.0)
    held = budget.reserve(0.1)
    with pytest.warns(UserWarning, match="above its"):
        budget.settle(held, 0.2)


def arm(**overrides: object) -> Arm:
    return Arm.model_validate(
        {
            "name": "a",
            "base_url": "http://x",
            "api_key_env": "K",
            "model": "m",
            "price": {"input_per_mtok": 1.0, "output_per_mtok": 10.0},
            "max_tokens": 100,
            **overrides,
        }
    )


def test_worst_case_counts_bytes_overhead_and_max_tokens() -> None:
    # "é" is 2 bytes: 5 chars, 10 bytes, so 10 + 16 overhead prompt tokens.
    messages = [{"role": "user", "content": "ééééé"}]
    expected = ((10 + 16) * 1.0 + 100 * 10.0) / 1_000_000
    assert worst_case_cost(arm(), messages) == pytest.approx(expected)  # type: ignore[arg-type]
    reported = arm(reports_cost=True)
    assert worst_case_cost(reported, messages) == pytest.approx(  # type: ignore[arg-type]
        expected * REPORTED_COST_MARGIN
    )
    unpriced = arm(price=None, reports_cost=True)
    assert worst_case_cost(unpriced, messages) is None  # type: ignore[arg-type]
