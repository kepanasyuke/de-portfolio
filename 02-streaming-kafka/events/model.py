"""Order event contract: validation, state transitions, deduplication.

Pure Python on purpose — three consumers share exactly this code:

* the producer, which must not publish events that violate the contract;
* the compaction job, which applies the same rules to what actually landed in
  the lake (the source can be redeployed and start sending garbage);
* the tests, which need no Kafka and no Spark.

Money is handled as ``Decimal`` and never as ``float`` in the domain model:
``0.1 + 0.2`` in binary floating point is the classic way to lose a kopeck in
a revenue report.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

EVENT_TYPES = ("order_created", "order_paid", "order_cancelled", "order_refunded")

EVENT_STATUS = {
    "order_created": "created",
    "order_paid": "paid",
    "order_cancelled": "cancelled",
    "order_refunded": "refunded",
}

TERMINAL_STATUSES = frozenset({"cancelled", "refunded"})

ALLOWED_FROM: dict[str, frozenset[str | None]] = {
    "order_created": frozenset({None}),
    "order_paid": frozenset({"created"}),
    "order_cancelled": frozenset({"created", "paid"}),
    "order_refunded": frozenset({"paid"}),
}

REQUIRED_FIELDS = ("event_id", "order_id", "event_type", "occurred_at", "source_system")


class EventError(ValueError):
    """Raised with the full list of contract violations for one event."""

    def __init__(self, reasons: Sequence[str], payload: Mapping[str, Any] | None = None) -> None:
        self.reasons = tuple(reasons)
        self.payload = dict(payload or {})
        super().__init__("; ".join(self.reasons))


def _as_datetime(value: Any, reasons: list[str], field: str) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            reasons.append(f"{field}: {value!r} is not an ISO-8601 timestamp")
            return None
    else:
        reasons.append(f"{field}: missing timestamp")
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _as_decimal(value: Any, reasons: list[str], field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        reasons.append(f"{field}: {value!r} is not a number")
        return None
    if amount < 0:
        reasons.append(f"{field}: must be non-negative, got {amount}")
    return amount


def _as_text(value: Any, reasons: list[str], field: str) -> str | None:
    if value is None:
        reasons.append(f"{field}: is required")
        return None
    text = str(value).strip()
    if not text:
        reasons.append(f"{field}: must not be empty")
        return None
    return text


def parse_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a raw event and return its normalised form (all timestamps in UTC)."""
    if not isinstance(raw, Mapping):
        raise EventError(["event must be a JSON object"], {})

    reasons: list[str] = []

    event_type = raw.get("event_type")
    if event_type not in EVENT_TYPES:
        reasons.append(f"event_type: {event_type!r} is not one of {EVENT_TYPES}")

    currency = raw.get("currency", "RUB")
    currency = str(currency).strip().upper() if currency not in (None, "") else "RUB"
    if len(currency) != 3 or not currency.isalpha():
        reasons.append(f"currency: {currency!r} must be a 3-letter code")

    normalised: dict[str, Any] = {
        "event_id": _as_text(raw.get("event_id"), reasons, "event_id"),
        "order_id": _as_text(raw.get("order_id"), reasons, "order_id"),
        "event_type": event_type,
        "occurred_at": _as_datetime(raw.get("occurred_at"), reasons, "occurred_at"),
        "source_system": _as_text(raw.get("source_system"), reasons, "source_system"),
        "amount": _as_decimal(raw.get("amount"), reasons, "amount"),
        "currency": currency,
    }

    if raw.get("customer_id") is not None:
        normalised["customer_id"] = str(raw["customer_id"]).strip() or None

    if reasons:
        raise EventError(reasons, raw)
    return normalised


def transition_allowed(current_status: str | None, event_type: str) -> bool:
    """Is ``event_type`` a legal next step from ``current_status``?"""
    allowed = ALLOWED_FROM.get(event_type)
    if allowed is None:
        raise EventError([f"event_type: unknown {event_type!r}"])
    return current_status in allowed


def apply_transition(current_status: str | None, event_type: str) -> str:
    """Return the new order status or raise :class:`EventError`."""
    if not transition_allowed(current_status, event_type):
        raise EventError(
            [f"transition: {event_type!r} is not allowed from status {current_status!r}"]
        )
    return EVENT_STATUS[event_type]


def fold_status(events: Iterable[Mapping[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Replay events in time order and return the final status plus rejected events.

    Used by the compaction job and by the DQ rule that compares the order status
    computed from events with the status stored in ``dds.orders``: a mismatch
    means an event was lost or applied twice.
    """
    parsed = [parse_event(event) for event in events]
    parsed.sort(key=lambda item: (item["occurred_at"], item["event_id"]))

    current: str | None = None
    rejected: list[dict[str, Any]] = []
    for event in parsed:
        if current in TERMINAL_STATUSES:
            rejected.append({**event, "reason": f"order is already {current}"})
            continue
        if not transition_allowed(current, event["event_type"]):
            rejected.append(
                {**event, "reason": f"illegal transition from {current!r}"}
            )
            continue
        current = EVENT_STATUS[event["event_type"]]
    return current, rejected


def deduplicate(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep one event per ``event_id``: the newest by ``occurred_at``.

    Kafka delivers at-least-once, so duplicates are normal. The pipeline relies
    on this function *and* on the primary key in ``dds.order_events``: two
    independent layers, because one of them will eventually be bypassed.
    """
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        parsed = parse_event(event)
        key = parsed["event_id"]
        current = latest.get(key)
        if current is None or (parsed["occurred_at"], parsed["order_id"]) >= (
            current["occurred_at"],
            current["order_id"],
        ):
            latest[key] = parsed
    return sorted(latest.values(), key=lambda item: (item["occurred_at"], item["event_id"]))


def to_json(event: Mapping[str, Any]) -> str:
    """Serialise a normalised event for Kafka (datetimes and decimals are strings)."""
    payload: dict[str, Any] = {}
    for key in (
        "event_id",
        "order_id",
        "customer_id",
        "event_type",
        "occurred_at",
        "source_system",
        "amount",
        "currency",
    ):
        value = event.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            payload[key] = value.isoformat().replace("+00:00", "Z")
        elif isinstance(value, Decimal):
            payload[key] = format(value, "f")
        else:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


def from_json(payload: str | bytes) -> dict[str, Any]:
    """Parse and validate an event read from Kafka."""
    data = json.loads(payload)
    if not isinstance(data, Mapping):
        raise EventError(["event must be a JSON object"])
    return parse_event(data)
