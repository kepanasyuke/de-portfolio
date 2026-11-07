"""Replay order events into Kafka, with a fixed share of deliberately broken ones.

The producer is a load generator for the streaming path, but it is built like a
real integration: every event goes through the same contract validation as the
consumer, keys are partitioned by ``order_id`` (so one order's lifecycle stays in
one partition and stays ordered), and ``--dirty-ratio`` injects the kind of
breakage that actually happens in production — null business keys, unknown
statuses, negative amounts, timestamps in the future.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events.model import EventError, parse_event, to_json

SOURCE_SYSTEM = "orders-service"


def lifecycle(order_id: str, customer_id: str, amount: Decimal, start: datetime) -> list[dict]:
    """Build a plausible event chain for one order."""
    created = {
        "event_id": f"{order_id}-created",
        "order_id": order_id,
        "customer_id": customer_id,
        "event_type": "order_created",
        "occurred_at": start,
        "amount": amount,
        "currency": "RUB",
        "source_system": SOURCE_SYSTEM,
    }
    paid = {
        **created,
        "event_id": f"{order_id}-paid",
        "event_type": "order_paid",
        "occurred_at": start + timedelta(seconds=45),
    }
    chain = [created, paid]
    if start.timestamp() % 7 < 1:
        chain.append(
            {
                **created,
                "event_id": f"{order_id}-refunded",
                "event_type": "order_refunded",
                "occurred_at": start + timedelta(hours=3),
            }
        )
    return chain


def corrupt(event: dict, rng: random.Random) -> dict:
    """Return a copy of the event with one realistic defect."""
    broken = dict(event)
    defect = rng.choice(
        (
            "null_event_id",
            "unknown_type",
            "negative_amount",
            "future_timestamp",
            "empty_order_id",
        )
    )
    if defect == "null_event_id":
        broken["event_id"] = None
    elif defect == "unknown_type":
        broken["event_type"] = "order_shipped"
    elif defect == "negative_amount":
        broken["amount"] = -abs(Decimal(str(broken.get("amount", "100"))))
    elif defect == "future_timestamp":
        broken["occurred_at"] = datetime.now(timezone.utc) + timedelta(days=2)
    else:
        broken["order_id"] = "  "
    broken["_injected_defect"] = defect
    return broken


def _maybe_corrupt(event: dict, rng: random.Random, dirty_ratio: float) -> dict:
    """Break the event with probability ``dirty_ratio``."""
    return corrupt(event, rng) if rng.random() < dirty_ratio else event


def generate(
    orders: int, dirty_ratio: float, seed: int, start: datetime | None = None
) -> Iterator[dict]:
    """Yield a deterministic stream of valid and broken events."""
    rng = random.Random(seed)
    moment = start or datetime.now(timezone.utc)
    for index in range(orders):
        order_id = f"ORD-{moment:%Y%m%d}-{index:05d}"
        amount = Decimal(rng.randrange(100_00, 500_00)) / Decimal(100)
        for event in lifecycle(order_id, f"CUST-{rng.randrange(1, 400):05d}", amount, moment):
            yield _maybe_corrupt(event, rng, dirty_ratio)


def _json_safe(value: object) -> object:
    """Render datetimes and decimals the same way the happy path does."""
    if isinstance(value, datetime):
        serialised: object = value.isoformat().replace("+00:00", "Z")
    elif isinstance(value, Decimal):
        serialised = format(value, "f")
    else:
        serialised = value
    return serialised


def _passthrough(event: dict) -> tuple[str, str]:
    """Publish an invalid event unchanged: the consumer side decides its fate."""
    payload = {
        key: _json_safe(value)
        for key, value in event.items()
        if not key.startswith("_")
    }
    raw_key = str(event.get("order_id") or "").strip()
    key = raw_key or "unknown"
    return key, json.dumps(payload, ensure_ascii=False, default=str)


def prepare(event: dict) -> tuple[str, str]:
    """Validate an event and return ``(key, payload)`` for the producer."""
    try:
        normalised = parse_event(event)
    except EventError:
        prepared = _passthrough(event)
    else:
        prepared = normalised["order_id"], to_json(normalised)
    return prepared


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", default="kafka:9092")
    parser.add_argument("--topic", default="order.events")
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--dirty-ratio", type=float, default=0.02)
    parser.add_argument("--rate", type=float, default=0.0, help="events per second, 0 = as fast as possible")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true", help="print events instead of publishing")
    return parser.parse_args(argv)


def _print_events(events: Iterator[dict]) -> None:
    for event in events:
        key, payload = prepare(event)
        print(f"{key}\t{payload}")


def _report_send_error(error: object) -> None:
    print(f"send failed: {error}", file=sys.stderr)


def _pace(rate: float) -> None:
    """Throttle the producer when a rate is requested."""
    if rate:
        time.sleep(1 / rate)


def _send(producer: object, topic: str, rate: float, event: dict) -> int:
    """Publish one event and return the increment for the sent counter."""
    key, payload = prepare(event)
    producer.send(topic, key=key, value=payload).add_errback(_report_send_error)
    _pace(rate)
    return 1


def _make_producer(bootstrap: str) -> object:
    from kafka import KafkaProducer

    return KafkaProducer(
        bootstrap_servers=bootstrap.split(","),
        key_serializer=lambda value: value.encode("utf-8"),
        value_serializer=lambda value: value.encode("utf-8"),
        acks="all",
        enable_idempotence=True,
        retries=5,
        linger_ms=20,
    )


def _publish(args: argparse.Namespace, events: Iterator[dict]) -> None:
    """Send every event to Kafka, flushing and closing the producer."""
    producer = _make_producer(args.bootstrap)
    sent = 0
    try:
        for event in events:
            sent += _send(producer, args.topic, args.rate, event)
        producer.flush()
    finally:
        producer.close(timeout=10)
    print(f"published {sent} events to {args.topic} @ {args.bootstrap}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    events = generate(args.orders, args.dirty_ratio, args.seed)
    if args.dry_run:
        _print_events(events)
    else:
        _publish(args, events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
