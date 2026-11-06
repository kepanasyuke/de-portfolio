-- DWH: staging, DDS (3NF) and audit schemas.
-- Staging tables are pre-created on purpose: Spark appends into a fixed shape
-- and we control every type instead of letting the JDBC dialect guess.

CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS dds;
CREATE SCHEMA IF NOT EXISTS dm;
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS dds.customers (
    customer_id  text PRIMARY KEY,
    email        text,
    full_name    text,
    country_code char(2),
    segment      text,
    created_at   timestamptz,
    _loaded_at   timestamptz NOT NULL DEFAULT now(),
    _batch_id    text
);

CREATE TABLE IF NOT EXISTS dds.products (
    product_id  text PRIMARY KEY,
    sku         text NOT NULL,
    name        text,
    category    text,
    unit_price  numeric(18, 2) NOT NULL CHECK (unit_price >= 0),
    is_active   boolean NOT NULL DEFAULT true,
    _loaded_at  timestamptz NOT NULL DEFAULT now(),
    _batch_id   text
);

CREATE TABLE IF NOT EXISTS dds.orders (
    order_id     text PRIMARY KEY,
    customer_id  text NOT NULL REFERENCES dds.customers (customer_id),
    status       text NOT NULL CHECK (status IN ('created', 'paid', 'cancelled', 'refunded')),
    currency     char(3) NOT NULL DEFAULT 'RUB',
    total_amount numeric(18, 2) NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz,
    _loaded_at   timestamptz NOT NULL DEFAULT now(),
    _batch_id    text
);

CREATE TABLE IF NOT EXISTS dds.order_items (
    order_item_id   text PRIMARY KEY,
    order_id        text NOT NULL REFERENCES dds.orders (order_id) ON DELETE CASCADE,
    product_id      text NOT NULL REFERENCES dds.products (product_id),
    quantity        integer NOT NULL CHECK (quantity > 0),
    unit_price      numeric(18, 2) NOT NULL CHECK (unit_price >= 0),
    discount_amount numeric(18, 2) NOT NULL DEFAULT 0,
    _loaded_at      timestamptz NOT NULL DEFAULT now(),
    _batch_id       text
);

CREATE TABLE IF NOT EXISTS dds.payments (
    payment_id text PRIMARY KEY,
    order_id   text NOT NULL REFERENCES dds.orders (order_id) ON DELETE CASCADE,
    amount     numeric(18, 2) NOT NULL,
    method     text,
    paid_at    timestamptz,
    _loaded_at timestamptz NOT NULL DEFAULT now(),
    _batch_id  text
);

CREATE INDEX IF NOT EXISTS orders_created_at_idx ON dds.orders (created_at);
CREATE INDEX IF NOT EXISTS orders_status_idx ON dds.orders (status);
CREATE INDEX IF NOT EXISTS order_items_order_id_idx ON dds.order_items (order_id);
CREATE INDEX IF NOT EXISTS payments_order_id_idx ON dds.payments (order_id);

CREATE TABLE IF NOT EXISTS stg.customers (
    customer_id  text,
    email        text,
    full_name    text,
    country_code text,
    segment      text,
    created_at   timestamp,
    _ingest_ts   timestamp
);

CREATE TABLE IF NOT EXISTS stg.products (
    product_id text,
    sku        text,
    name       text,
    category   text,
    unit_price numeric(18, 2),
    is_active  boolean,
    _ingest_ts timestamp
);

CREATE TABLE IF NOT EXISTS stg.orders (
    order_id     text,
    customer_id  text,
    status       text,
    currency     text,
    total_amount numeric(18, 2),
    created_at   timestamp,
    updated_at   timestamp,
    _ingest_ts   timestamp
);

CREATE TABLE IF NOT EXISTS stg.order_items (
    order_item_id   text,
    order_id        text,
    product_id      text,
    quantity        integer,
    unit_price      numeric(18, 2),
    discount_amount numeric(18, 2),
    _ingest_ts      timestamp
);

CREATE TABLE IF NOT EXISTS stg.payments (
    payment_id text,
    order_id   text,
    amount     numeric(18, 2),
    method     text,
    paid_at    timestamp,
    _ingest_ts timestamp
);

CREATE TABLE IF NOT EXISTS meta.pipeline_run (
    run_id     text PRIMARY KEY,
    dag_id     text NOT NULL,
    load_date  date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    row_counts jsonb NOT NULL DEFAULT '{}'::jsonb
);
