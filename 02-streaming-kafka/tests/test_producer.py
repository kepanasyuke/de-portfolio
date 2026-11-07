"""Producer: lifecycle generation, defect injection and the publishing path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from events import producer
from events.model import EventError, parse_event

MOMENT = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


class FakeRng:
    """Deterministic stand-in for ``random.Random``."""

    def __init__(self, choice: str, random_value: float = 0.0) -> None:
        self._choice = choice
        self._random = random_value

    def choice(self, options):
        return self._choice

    def random(self) -> float:
        return self._random


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.flushed = False
        self.closed = False

    def send(self, topic, key=None, value=None):
        self.sent.append((topic, key, value))
        return self

    def add_errback(self, callback):
        return self

    def flush(self) -> None:
        self.flushed = True

    def close(self, timeout=None) -> None:
        self.closed = True


def _event(**overrides) -> dict:
    payload = {
        "event_id": "EV-1",
        "order_id": "ORD-1",
        "event_type": "order_created",
        "occurred_at": MOMENT,
        "amount": Decimal("100.00"),
        "currency": "RUB",
        "source_system": "orders-service",
    }
    payload.update(overrides)
    return payload


def test_lifecycle_starts_with_created_and_paid():
    chain = producer.lifecycle("ORD-7", "CUST-1", Decimal("50.00"), MOMENT)
    assert [event["event_type"] for event in chain[:2]] == ["order_created", "order_paid"]
    assert [event["event_id"] for event in chain[:2]] == ["ORD-7-created", "ORD-7-paid"]


def test_lifecycle_keeps_the_payment_45_seconds_later():
    created, paid = producer.lifecycle("ORD-7", "CUST-1", Decimal("50.00"), MOMENT)[:2]
    assert paid["occurred_at"] - created["occurred_at"] == timedelta(seconds=45)


def test_lifecycle_propagates_business_fields():
    created = producer.lifecycle("ORD-7", "CUST-1", Decimal("50.00"), MOMENT)[0]
    assert created["order_id"] == "ORD-7"
    assert created["customer_id"] == "CUST-1"
    assert created["amount"] == Decimal("50.00")
    assert created["currency"] == "RUB"
    assert created["source_system"] == "orders-service"


def test_lifecycle_adds_a_refund_for_some_timestamps():
    lengths = {
        len(producer.lifecycle("ORD", "CUST", Decimal("1"), MOMENT + timedelta(hours=hour)))
        for hour in range(8)
    }
    assert lengths == {2, 3}


def test_refund_event_is_a_refund_three_hours_later():
    chains = [
        producer.lifecycle("ORD", "CUST", Decimal("1"), MOMENT + timedelta(hours=hour))
        for hour in range(8)
    ]
    refunds = [chain[-1] for chain in chains if len(chain) == 3]
    assert refunds
    assert all(event["event_type"] == "order_refunded" for event in refunds)
    assert all(event["event_id"].endswith("-refunded") for event in refunds)
    assert all(event["occurred_at"] - MOMENT >= timedelta(hours=3) for event in refunds)


DEFECTS = [
    ("null_event_id", "event_id", lambda value: value is None),
    ("unknown_type", "event_type", lambda value: value == "order_shipped"),
    ("negative_amount", "amount", lambda value: value < 0),
    ("future_timestamp", "occurred_at", lambda value: value > datetime.now(timezone.utc)),
    ("empty_order_id", "order_id", lambda value: value == "  "),
]


@pytest.mark.parametrize(("defect", "field", "check"), DEFECTS)
def test_corrupt_breaks_exactly_one_field(defect, field, check):
    broken = producer.corrupt(_event(), FakeRng(defect))
    assert check(broken[field])


@pytest.mark.parametrize(("defect", "field", "check"), DEFECTS)
def test_corrupt_records_the_injected_defect(defect, field, check):
    broken = producer.corrupt(_event(), FakeRng(defect))
    assert broken["_injected_defect"] == defect


def test_corrupt_does_not_mutate_the_source_event():
    original = _event()
    producer.corrupt(original, FakeRng("null_event_id"))
    assert original["event_id"] == "EV-1"


def test_corrupt_keeps_other_fields_intact():
    broken = producer.corrupt(_event(), FakeRng("unknown_type"))
    assert broken["order_id"] == "ORD-1"
    assert broken["amount"] == Decimal("100.00")


def test_generate_is_deterministic_for_a_seed():
    first = list(producer.generate(orders=5, dirty_ratio=0.0, seed=7, start=MOMENT))
    second = list(producer.generate(orders=5, dirty_ratio=0.0, seed=7, start=MOMENT))
    assert first == second


def test_generate_differs_for_another_seed():
    first = list(producer.generate(orders=5, dirty_ratio=0.3, seed=7, start=MOMENT))
    second = list(producer.generate(orders=5, dirty_ratio=0.3, seed=8, start=MOMENT))
    assert first != second


def test_generate_without_dirt_produces_valid_events():
    events = list(producer.generate(orders=3, dirty_ratio=0.0, seed=1, start=MOMENT))
    assert all(parse_event(event) for event in events)


def test_generate_with_full_dirt_breaks_every_event():
    events = list(producer.generate(orders=2, dirty_ratio=1.0, seed=1, start=MOMENT))
    with pytest.raises(EventError):
        parse_event(events[0])


def test_generate_embeds_the_start_date_in_order_ids():
    events = list(producer.generate(orders=2, dirty_ratio=0.0, seed=1, start=MOMENT))
    assert all(event["order_id"].startswith("ORD-20261105-") for event in events)


def test_generate_emits_at_least_two_events_per_order():
    events = list(producer.generate(orders=4, dirty_ratio=0.0, seed=1, start=MOMENT))
    assert len(events) >= 8


def test_prepare_returns_order_key_and_payload():
    key, payload = producer.prepare(parse_event(_event()))
    assert key == "ORD-1"
    assert json.loads(payload)["event_id"] == "EV-1"


def test_prepare_drops_private_fields():
    key, payload = producer.prepare(_event(_note="internal"))
    assert key == "ORD-1"
    assert "_note" not in json.loads(payload)


def test_prepare_publishes_invalid_events_unchanged():
    key, payload = producer.prepare(_event(event_id=None))
    assert key == "ORD-1"
    assert json.loads(payload)["event_id"] is None


def test_prepare_falls_back_to_unknown_key():
    key, _payload = producer.prepare(_event(order_id="   "))
    assert key == "unknown"


def test_prepare_serialises_datetimes_of_invalid_events():
    key, payload = producer.prepare(_event(event_type="order_shipped"))
    assert key == "ORD-1"
    assert json.loads(payload)["occurred_at"].endswith("Z")


def test_prepare_serialises_decimals_of_invalid_events():
    _key, payload = producer.prepare(_event(event_type="order_shipped"))
    assert json.loads(payload)["amount"] == "100.00"


JSON_SAFE = [
    (datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc), "2026-11-05T10:00:00Z"),
    (Decimal("10.50"), "10.50"),
    ("paid", "paid"),
    (7, 7),
]


@pytest.mark.parametrize(("value", "expected"), JSON_SAFE)
def test_json_safe_renders_each_type(value, expected):
    assert producer._json_safe(value) == expected


def test_pace_does_nothing_without_a_rate(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(producer.time, "sleep", lambda seconds: slept.append(seconds))
    producer._pace(0.0)
    assert slept == []


def test_pace_sleeps_for_the_requested_rate(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(producer.time, "sleep", lambda seconds: slept.append(seconds))
    producer._pace(10.0)
    assert slept == [pytest.approx(0.1)]


def test_send_publishes_and_counts(monkeypatch):
    monkeypatch.setattr(producer, "_pace", lambda rate: None)
    fake = FakeProducer()
    assert producer._send(fake, "order.events", 0.0, _event()) == 1
    assert fake.sent[0][0] == "order.events"
    assert fake.sent[0][1] == "ORD-1"


def test_publish_flushes_and_closes(monkeypatch, capsys):
    fake = FakeProducer()
    monkeypatch.setattr(producer, "_make_producer", lambda bootstrap: fake)
    monkeypatch.setattr(producer, "_pace", lambda rate: None)
    args = producer.parse_args(["--orders", "2", "--dirty-ratio", "0"])
    producer._publish(args, producer.generate(2, 0.0, 1, MOMENT))
    assert fake.flushed is True
    assert fake.closed is True
    assert "published" in capsys.readouterr().out


def test_publish_closes_the_producer_on_failure(monkeypatch):
    class ExplodingProducer(FakeProducer):
        def send(self, topic, key=None, value=None):
            raise OSError("broker is down")

    fake = ExplodingProducer()
    monkeypatch.setattr(producer, "_make_producer", lambda bootstrap: fake)
    args = producer.parse_args(["--orders", "1"])
    with pytest.raises(OSError):
        producer._publish(args, producer.generate(1, 0.0, 1, MOMENT))
    assert fake.closed is True


def test_print_events_writes_one_line_per_event(capsys):
    producer._print_events(producer.generate(1, 0.0, 1, MOMENT))
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) >= 2
    assert "\t" in lines[0]


def test_main_dry_run_prints_and_exits_zero(capsys):
    code = producer.main(["--dry-run", "--orders", "1", "--dirty-ratio", "0"])
    assert code == 0
    assert "ORD-" in capsys.readouterr().out


def test_main_publishes_through_the_helper(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(producer, "_publish", lambda args, events: calls.append("published"))
    assert producer.main(["--orders", "1"]) == 0
    assert calls == ["published"]


def test_report_send_error_writes_to_stderr(capsys):
    producer._report_send_error(RuntimeError("nope"))
    assert "send failed" in capsys.readouterr().err


def test_parse_args_defaults():
    args = producer.parse_args([])
    assert args.bootstrap == "kafka:9092"
    assert args.topic == "order.events"
    assert args.dirty_ratio == pytest.approx(0.02)
    assert args.dry_run is False


def test_parse_args_accepts_custom_values():
    args = producer.parse_args(["--bootstrap", "broker:9093", "--rate", "5", "--seed", "9"])
    assert args.bootstrap == "broker:9093"
    assert args.rate == pytest.approx(5)
    assert args.seed == 9
