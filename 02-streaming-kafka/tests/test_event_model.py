"""Contract tests for the order event model: no Kafka, no Spark, no database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from events.model import (
    EventError,
    apply_transition,
    deduplicate,
    fold_status,
    from_json,
    parse_event,
    to_json,
    transition_allowed,
)


def make_event(**overrides):
    payload = {
        "event_id": "EV-1",
        "order_id": "ORD-1",
        "event_type": "order_created",
        "occurred_at": "2026-11-05T10:00:00Z",
        "source_system": "orders-service",
        "amount": "199.90",
        "currency": "rub",
    }
    payload.update(overrides)
    return payload


def test_parse_normalises_timestamp_and_currency():
    parsed = parse_event(make_event())
    assert parsed["occurred_at"] == datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    assert parsed["currency"] == "RUB"
    assert parsed["amount"] == Decimal("199.90")


def test_naive_timestamp_is_treated_as_utc():
    parsed = parse_event(make_event(occurred_at="2026-11-05T13:00:00"))
    assert parsed["occurred_at"].tzinfo is timezone.utc
    assert parsed["occurred_at"].hour == 13


def test_missing_business_keys_are_reported_together():
    with pytest.raises(EventError) as error:
        parse_event(make_event(event_id=None, order_id="", source_system=None))
    reasons = error.value.reasons
    assert any("event_id" in reason for reason in reasons)
    assert any("order_id" in reason for reason in reasons)
    assert any("source_system" in reason for reason in reasons)


def test_unknown_event_type_is_rejected():
    with pytest.raises(EventError) as error:
        parse_event(make_event(event_type="order_shipped"))
    assert "event_type" in str(error.value)


def test_negative_amount_is_rejected():
    with pytest.raises(EventError) as error:
        parse_event(make_event(amount="-1"))
    assert any("non-negative" in reason for reason in error.value.reasons)


def test_non_numeric_amount_is_rejected():
    with pytest.raises(EventError):
        parse_event(make_event(amount="двести"))


def test_money_keeps_decimal_precision():
    parsed = parse_event(make_event(amount="0.1"))
    assert parsed["amount"] + Decimal("0.2") == Decimal("0.3")


@pytest.mark.parametrize(
    ("current", "event_type", "expected"),
    [
        (None, "order_created", "created"),
        ("created", "order_paid", "paid"),
        ("paid", "order_refunded", "refunded"),
        ("created", "order_cancelled", "cancelled"),
    ],
)
def test_legal_transitions(current, event_type, expected):
    assert transition_allowed(current, event_type)
    assert apply_transition(current, event_type) == expected


@pytest.mark.parametrize(
    ("current", "event_type"),
    [
        ("created", "order_refunded"),
        ("paid", "order_created"),
        ("cancelled", "order_paid"),
        ("refunded", "order_created"),
        (None, "order_paid"),
    ],
)
def test_illegal_transitions(current, event_type):
    assert not transition_allowed(current, event_type)
    with pytest.raises(EventError):
        apply_transition(current, event_type)


def test_fold_status_replays_lifecycle():
    start = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    events = [
        make_event(event_id="EV-1", event_type="order_created", occurred_at=start),
        make_event(
            event_id="EV-2",
            event_type="order_paid",
            occurred_at=(start + timedelta(minutes=1)).isoformat(),
        ),
        make_event(
            event_id="EV-3",
            event_type="order_refunded",
            occurred_at=(start + timedelta(hours=2)).isoformat(),
        ),
    ]
    status, rejected = fold_status(events)
    assert status == "refunded"
    assert rejected == []


def test_fold_status_rejects_event_after_terminal_state():
    start = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    events = [
        make_event(event_id="EV-1", event_type="order_created", occurred_at=start),
        make_event(
            event_id="EV-2",
            event_type="order_cancelled",
            occurred_at=(start + timedelta(minutes=5)).isoformat(),
        ),
        make_event(
            event_id="EV-3",
            event_type="order_paid",
            occurred_at=(start + timedelta(minutes=9)).isoformat(),
        ),
    ]
    status, rejected = fold_status(events)
    assert status == "cancelled"
    assert len(rejected) == 1
    assert rejected[0]["event_id"] == "EV-3"


def test_deduplicate_keeps_newest_occurrence():
    start = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    events = [
        make_event(event_id="EV-1", occurred_at=start),
        make_event(
            event_id="EV-1",
            occurred_at=(start + timedelta(minutes=3)).isoformat(),
            amount="250.00",
        ),
        make_event(event_id="EV-2", occurred_at=start),
    ]
    result = deduplicate(events)
    assert sorted(event["event_id"] for event in result) == ["EV-1", "EV-2"]
    newest = next(event for event in result if event["event_id"] == "EV-1")
    assert newest["amount"] == Decimal("250.00")


def test_json_round_trip_preserves_utc_instant():
    original = parse_event(make_event())
    payload = to_json(original)
    restored = from_json(payload)
    assert '"occurred_at": "2026-11-05T10:00:00Z"' in payload
    assert restored["occurred_at"] == original["occurred_at"]
    assert restored["amount"] == original["amount"]
