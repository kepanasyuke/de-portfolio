-- Streaming side of the DDS: raw event log plus the status projection on orders.

CREATE TABLE IF NOT EXISTS dds.order_events (
    event_id       text PRIMARY KEY,
    order_id       text NOT NULL,
    event_type     text NOT NULL,
    status         text NOT NULL,
    occurred_at    timestamptz NOT NULL,
    amount         numeric(18, 2),
    currency       char(3),
    source_system  text NOT NULL,
    is_quarantined boolean NOT NULL DEFAULT false,
    _ingested_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS order_events_order_idx
    ON dds.order_events (order_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS order_events_occurred_idx
    ON dds.order_events (occurred_at DESC);

CREATE INDEX IF NOT EXISTS order_events_quarantine_idx
    ON dds.order_events (is_quarantined) WHERE is_quarantined;

CREATE TABLE IF NOT EXISTS stg.order_events (
    event_id      text,
    order_id      text,
    event_type    text,
    status        text,
    occurred_at   timestamp,
    amount        numeric(18, 2),
    currency      text,
    source_system text,
    _ingest_ts    timestamp
);
