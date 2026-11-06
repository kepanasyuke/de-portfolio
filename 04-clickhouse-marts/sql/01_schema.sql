-- ClickHouse serving layer: one wide fact + one aggregate mart.
--
-- Model choices, and the reason for each
-- --------------------------------------
-- * ReplacingMergeTree(_loaded_at) on the fact: repeated loads of the same order
--   line collapse to the newest version instead of doubling revenue. Same idea
--   as the upsert in Postgres, different mechanism.
-- * ORDER BY starts with order_date: almost every analytical query filters or
--   groups by a date range, so the sparse primary index can skip granules.
-- * LowCardinality for category/status: dictionary encoding turns repeated
--   strings into small integers, which is most of the compression win here.
-- * AggregatingMergeTree mart with AggregateFunction states: daily revenue is
--   computed once at insert time, and reads merge partial states instead of
--   scanning the fact.

CREATE DATABASE IF NOT EXISTS marts;

CREATE TABLE IF NOT EXISTS marts.fact_sales
(
    order_id        String,
    order_item_id   String,
    order_date      Date,
    customer_id     LowCardinality(String),
    product_id      LowCardinality(String),
    category        LowCardinality(String),
    quantity        UInt32,
    gross_amount    Decimal(18, 2) CODEC(ZSTD(3)),
    discount_amount Decimal(18, 2) CODEC(ZSTD(3)),
    net_amount      Decimal(18, 2) CODEC(ZSTD(3)),
    order_status    LowCardinality(String),
    _loaded_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_loaded_at)
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, customer_id, product_id, order_item_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS marts.daily_revenue
(
    order_date Date,
    category   LowCardinality(String),
    orders     AggregateFunction(uniqExact, String),
    items      AggregateFunction(sum, UInt64),
    revenue    AggregateFunction(sum, Decimal(18, 2)),
    customers  AggregateFunction(uniqExact, String),
    _loaded_at SimpleAggregateFunction(max, DateTime)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, category);

CREATE MATERIALIZED VIEW IF NOT EXISTS marts.daily_revenue_mv
TO marts.daily_revenue
AS
SELECT
    order_date,
    category,
    uniqExactState(order_id)      AS orders,
    sumState(toUInt64(quantity))  AS items,
    sumState(net_amount)          AS revenue,
    uniqExactState(customer_id)   AS customers,
    max(_loaded_at)               AS _loaded_at
FROM marts.fact_sales
WHERE order_status <> 'cancelled'
GROUP BY order_date, category;

CREATE TABLE IF NOT EXISTS marts.customer_lifetime
(
    customer_id   String,
    first_order   Date,
    last_order    Date,
    orders        UInt64,
    revenue       Decimal(18, 2),
    _loaded_at    SimpleAggregateFunction(max, DateTime)
)
ENGINE = ReplacingMergeTree(_loaded_at)
ORDER BY customer_id;
