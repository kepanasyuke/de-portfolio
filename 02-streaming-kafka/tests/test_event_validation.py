"""Contract validation for order events: every field, every failure mode."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from events.model import (
    ALLOWED_FROM,
    EVENT_STATUS,
    EVENT_TYPES,
    TERMINAL_STATUSES,
    EventError,
    apply_transition,
    deduplicate,
    fold_status,
    from_json,
    parse_event,
    to_json,
    transition_allowed,
)

BASE = {
    "event_id": "EV-1",
    "order_id": "ORD-1",
    "event_type": "order_created",
    "occurred_at": "2026-11-05T10:00:00Z",
    "source_system": "orders-service",
}


def make_event(**overrides):
    payload = {**BASE, **overrides}
    return {key: value for key, value in payload.items() if not key.startswith("_drop_")}


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_event_type_is_accepted(event_type):
    parsed = parse_event(make_event(event_type=event_type))
    assert parsed["event_type"] == event_type
    assert EVENT_STATUS[event_type]


TIMESTAMPS = [
    ("2026-11-05T10:00:00Z", datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)),
    ("2026-11-05T10:00:00+03:00", datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)),
    ("2026-11-05T10:00:00", datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)),
    ("2026-11-05T10:00:00.250Z", datetime(2026, 11, 5, 10, 0, 0, 250000, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize(("raw", "expected"), TIMESTAMPS)
def test_timestamps_are_normalised_to_utc(raw, expected):
    parsed = parse_event(make_event(occurred_at=raw))
    assert parsed["occurred_at"] == expected
    assert parsed["occurred_at"].tzinfo is timezone.utc


def test_datetime_instance_is_accepted():
    moment = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    assert parse_event(make_event(occurred_at=moment))["occurred_at"] == moment


CURRENCIES = [
    ("rub", "RUB"),
    (" USD ", "USD"),
    (None, "RUB"),
    ("", "RUB"),
]


@pytest.mark.parametrize(("raw", "expected"), CURRENCIES)
def test_currency_is_normalised(raw, expected):
    assert parse_event(make_event(currency=raw))["currency"] == expected


BAD_CURRENCIES = ["EURO", "R", "R1B", "12"]


@pytest.mark.parametrize("currency", BAD_CURRENCIES)
def test_bad_currency_is_rejected(currency):
    with pytest.raises(EventError) as error:
        parse_event(make_event(currency=currency))
    assert any("currency" in reason for reason in error.value.reasons)


AMOUNTS = [
    ("199.90", Decimal("199.90")),
    (199.9, Decimal("199.9")),
    (200, Decimal("200")),
    (0, Decimal("0")),
]


@pytest.mark.parametrize(("raw", "expected"), AMOUNTS)
def test_amount_becomes_decimal(raw, expected):
    assert parse_event(make_event(amount=raw))["amount"] == expected


def test_amount_is_optional():
    assert parse_event(make_event())["amount"] is None


def test_amount_from_empty_string_is_none():
    assert parse_event(make_event(amount=""))["amount"] is None


@pytest.mark.parametrize("amount", ["-1", -0.01, "-999999"])
def test_negative_amount_is_rejected(amount):
    with pytest.raises(EventError):
        parse_event(make_event(amount=amount))


@pytest.mark.parametrize("amount", ["двести", "12,5,5", "NaN-ish", "."])
def test_garbage_amount_is_rejected(amount):
    with pytest.raises(EventError):
        parse_event(make_event(amount=amount))


REQUIRED_FIELDS = ["event_id", "order_id", "event_type", "occurred_at", "source_system"]


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_is_rejected(field):
    payload = {key: value for key, value in BASE.items() if key != field}
    with pytest.raises(EventError) as error:
        parse_event(payload)
    assert any(field in reason for reason in error.value.reasons)


@pytest.mark.parametrize("field", ["event_id", "order_id", "source_system"])
def test_blank_text_field_is_rejected(field):
    with pytest.raises(EventError):
        parse_event(make_event(**{field: "   "}))


def test_all_reasons_are_collected_at_once():
    broken = {"event_type": "order_shipped", "currency": "EU", "amount": "-5"}
    with pytest.raises(EventError) as error:
        parse_event(broken)
    assert len(error.value.reasons) >= 4


def test_event_error_keeps_reasons_and_payload():
    payload = make_event(amount="-1", event_type="order_shipped")
    with pytest.raises(EventError) as error:
        parse_event(payload)
    assert error.value.payload["order_id"] == "ORD-1"
    assert len(error.value.reasons) >= 2
    assert "; " in str(error.value)


def test_non_mapping_is_rejected():
    with pytest.raises(EventError):
        parse_event(["not", "a", "mapping"])


def test_customer_id_is_optional():
    assert "customer_id" not in parse_event(make_event())


def test_customer_id_is_trimmed():
    parsed = parse_event(make_event(customer_id="  CUST-9  "))
    assert parsed["customer_id"] == "CUST-9"


def test_unknown_customer_id_field_is_dropped_when_blank():
    assert parse_event(make_event(customer_id="   ")).get("customer_id") is None


def test_extra_fields_are_ignored():
    parsed = parse_event(make_event(debug_hint="ignore me"))
    assert "debug_hint" not in parsed


LEGAL_TRANSITIONS = [
    (None, "order_created"),
    ("created", "order_paid"),
    ("created", "order_cancelled"),
    ("paid", "order_cancelled"),
    ("paid", "order_refunded"),
]


@pytest.mark.parametrize(("current", "event_type"), LEGAL_TRANSITIONS)
def test_legal_transitions_are_allowed(current, event_type):
    assert transition_allowed(current, event_type) is True
    assert apply_transition(current, event_type) == EVENT_STATUS[event_type]


ILLEGAL_TRANSITIONS = [
    (None, "order_paid"),
    (None, "order_refunded"),
    ("created", "order_refunded"),
    ("paid", "order_created"),
    ("paid", "order_paid"),
    ("cancelled", "order_paid"),
    ("cancelled", "order_created"),
    ("refunded", "order_created"),
    ("refunded", "order_paid"),
]


@pytest.mark.parametrize(("current", "event_type"), ILLEGAL_TRANSITIONS)
def test_illegal_transitions_raise(current, event_type):
    assert transition_allowed(current, event_type) is False
    with pytest.raises(EventError):
        apply_transition(current, event_type)


def test_unknown_event_type_in_transition_raises():
    with pytest.raises(EventError):
        transition_allowed("created", "order_shipped")


def test_allowed_table_covers_every_event_type():
    assert set(ALLOWED_FROM) == set(EVENT_TYPES)
    assert {"cancelled", "refunded"} == TERMINAL_STATUSES


def _event(event_id: str, event_type: str, minutes: int) -> dict:
    moment = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return make_event(event_id=event_id, event_type=event_type, occurred_at=moment)


def test_fold_status_walks_the_full_lifecycle():
    status, rejected = fold_status(
        [
            _event("EV-1", "order_created", 0),
            _event("EV-2", "order_paid", 5),
        ]
    )
    assert status == "paid"
    assert rejected == []


def test_fold_status_sorts_events_by_time():
    status, rejected = fold_status(
        [
            _event("EV-2", "order_paid", 5),
            _event("EV-1", "order_created", 0),
        ]
    )
    assert status == "paid"
    assert rejected == []


def test_fold_status_rejects_events_after_a_terminal_state():
    status, rejected = fold_status(
        [
            _event("EV-1", "order_created", 0),
            _event("EV-2", "order_cancelled", 5),
            _event("EV-3", "order_paid", 9),
            _event("EV-4", "order_refunded", 12),
        ]
    )
    assert status == "cancelled"
    assert [event["event_id"] for event in rejected] == ["EV-3", "EV-4"]
    assert "already cancelled" in rejected[0]["reason"]


def test_fold_status_rejects_an_illegal_first_event():
    status, rejected = fold_status([_event("EV-1", "order_paid", 0)])
    assert status is None
    assert len(rejected) == 1
    assert "illegal transition" in rejected[0]["reason"]


def test_fold_status_of_nothing_is_empty():
    assert fold_status([]) == (None, [])


def test_deduplicate_keeps_one_event_per_id():
    events = [
        _event("EV-1", "order_created", 0),
        _event("EV-1", "order_created", 5),
        _event("EV-2", "order_paid", 7),
    ]
    result = deduplicate(events)
    assert len(result) == 2


def test_deduplicate_keeps_the_newest_by_time():
    events = [
        _event("EV-1", "order_created", 0),
        {**_event("EV-1", "order_created", 5), "amount": "250.00"},
    ]
    newest = deduplicate(events)[0]
    assert newest["amount"] == Decimal("250.00")


def test_deduplicate_returns_time_ordered_events():
    events = [
        _event("EV-2", "order_paid", 5),
        _event("EV-1", "order_created", 0),
    ]
    assert [event["event_id"] for event in deduplicate(events)] == ["EV-1", "EV-2"]


def test_deduplicate_rejects_invalid_input():
    with pytest.raises(EventError):
        deduplicate([make_event(event_type="order_shipped")])


def test_deduplicate_of_empty_input_is_empty():
    assert deduplicate([]) == []


def test_to_json_omits_absent_optional_fields():
    payload = json.loads(to_json(parse_event(make_event())))
    assert "amount" not in payload
    assert "customer_id" not in payload


def test_to_json_formats_timestamps_with_z():
    payload = json.loads(to_json(parse_event(make_event())))
    assert payload["occurred_at"] == "2026-11-05T10:00:00Z"


def test_to_json_keeps_decimal_digits():
    payload = json.loads(to_json(parse_event(make_event(amount="199.90"))))
    assert payload["amount"] == "199.90"


def test_to_json_keeps_cyrillic_readable():
    parsed = parse_event(make_event(source_system="сервис заказов"))
    assert "сервис заказов" in to_json(parsed)


def test_json_round_trip():
    original = parse_event(make_event(amount="10.50", customer_id="CUST-1"))
    restored = from_json(to_json(original))
    assert restored["amount"] == original["amount"]
    assert restored["customer_id"] == "CUST-1"
    assert restored["occurred_at"] == original["occurred_at"]


def test_from_json_accepts_bytes():
    payload = to_json(parse_event(make_event())).encode("utf-8")
    assert from_json(payload)["order_id"] == "ORD-1"


def test_from_json_rejects_broken_json():
    with pytest.raises(json.JSONDecodeError):
        from_json("{not json")


def test_from_json_rejects_a_non_object():
    with pytest.raises(EventError):
        from_json("[1, 2, 3]")
