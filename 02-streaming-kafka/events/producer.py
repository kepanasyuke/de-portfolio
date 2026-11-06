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
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events.model import EVENT_STATUS, EventError, parse_event, to_json

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
            if rng.random() < dirty_ratio:
                yield corrupt(event, rng)
            else:
                yield event


def prepare(event: dict) -> tuple[str, str]:
    """Validate an event and return ``(key, payload)`` for the producer."""
    try:
        normalised = parse_event(event)
    except EventError as error:
        payload = {key: value for key, value in event.items() if not key.startswith("_")}
        for key, value in list(payload.items()):
            if isinstance(value, datetime):
                payload[key] = value.isoformat().replace("+00:00", "Z")
            elif isinstance(value, Decimal):
                payload[key] = format(value, "f")
        import json

        return str(event.get("order_id") or "unknown"), json.dumps(
            payload, ensure_ascii=False, default=str
        )
    return normalised["order_id"], to_json(normalised)


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    events = generate(args.orders, args.dirty_ratio, args.seed)

    if args.dry_run:
        for event in events:
            key, payload = prepare(event)
            print(f"{key}\t{payload}")
        return 0

    from kafka import KafkaProducer

    producer = KafkaProducer(
        bootstrap_servers=args.bootstrap.split(","),
        key_serializer=lambda value: value.encode("utf-8"),
        value_serializer=lambda value: value.encode("utf-8"),
        acks="all",
        enable_idempotence=True,
        retries=5,
        linger_ms=20,
    )

    sent = 0
    try:
        for event in events:
            key, payload = prepare(event)
            producer.send(args.topic, key=key, value=payload).add_errback(
                lambda error: print(f"send failed: {error}", file=sys.stderr)
            )
            sent += 1
            if args.rate:
                time.sleep(1 / args.rate)
        producer.flush()
    finally:
        producer.close(timeout=10)
    print(f"published {sent} events to {args.topic} @ {args.bootstrap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
