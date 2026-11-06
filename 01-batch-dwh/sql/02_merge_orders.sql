-- Merge staging into the DDS layer (3NF).
--
-- Order of statements is the dependency order: dimensions of the fact grain
-- first, then transactional entities, then their children. Every statement is
-- an idempotent upsert on the business key, so a re-run of the same batch or a
-- late-arriving batch converges to the same state.

INSERT INTO dds.customers (
    customer_id, email, full_name, country_code, segment, created_at, _batch_id
)
SELECT customer_id,
       email,
       full_name,
       country_code,
       segment,
       created_at AT TIME ZONE 'UTC',
       '{{ run_id }}'
FROM stg.customers
WHERE customer_id IS NOT NULL
ON CONFLICT (customer_id) DO UPDATE SET
    email        = excluded.email,
    full_name    = excluded.full_name,
    country_code = excluded.country_code,
    segment      = excluded.segment,
    created_at   = coalesce(dds.customers.created_at, excluded.created_at),
    _loaded_at   = now(),
    _batch_id    = excluded._batch_id;

INSERT INTO dds.products (product_id, sku, name, category, unit_price, is_active, _batch_id)
SELECT product_id, sku, name, category, unit_price, coalesce(is_active, true), '{{ run_id }}'
FROM stg.products
WHERE product_id IS NOT NULL
ON CONFLICT (product_id) DO UPDATE SET
    sku        = excluded.sku,
    name       = excluded.name,
    category   = excluded.category,
    unit_price = excluded.unit_price,
    is_active  = excluded.is_active,
    _loaded_at = now(),
    _batch_id  = excluded._batch_id;

INSERT INTO dds.orders (
    order_id, customer_id, status, currency, total_amount, created_at, updated_at, _batch_id
)
SELECT o.order_id,
       o.customer_id,
       o.status,
       coalesce(o.currency, 'RUB'),
       coalesce(o.total_amount, 0),
       o.created_at AT TIME ZONE 'UTC',
       o.updated_at AT TIME ZONE 'UTC',
       '{{ run_id }}'
FROM stg.orders o
WHERE o.order_id IS NOT NULL
ON CONFLICT (order_id) DO UPDATE SET
    status       = excluded.status,
    currency     = excluded.currency,
    total_amount = excluded.total_amount,
    updated_at   = greatest(coalesce(dds.orders.updated_at, excluded.updated_at), excluded.updated_at),
    _loaded_at   = now(),
    _batch_id    = excluded._batch_id;

INSERT INTO dds.order_items (
    order_item_id, order_id, product_id, quantity, unit_price, discount_amount, _batch_id
)
SELECT order_item_id,
       order_id,
       product_id,
       quantity,
       unit_price,
       coalesce(discount_amount, 0),
       '{{ run_id }}'
FROM stg.order_items
WHERE order_item_id IS NOT NULL
ON CONFLICT (order_item_id) DO UPDATE SET
    quantity        = excluded.quantity,
    unit_price      = excluded.unit_price,
    discount_amount = excluded.discount_amount,
    _loaded_at      = now(),
    _batch_id       = excluded._batch_id;

INSERT INTO dds.payments (payment_id, order_id, amount, method, paid_at, _batch_id)
SELECT payment_id,
       order_id,
       amount,
       method,
       paid_at AT TIME ZONE 'UTC',
       '{{ run_id }}'
FROM stg.payments
WHERE payment_id IS NOT NULL
ON CONFLICT (payment_id) DO UPDATE SET
    amount     = excluded.amount,
    method     = excluded.method,
    paid_at    = excluded.paid_at,
    _loaded_at = now(),
    _batch_id  = excluded._batch_id;

INSERT INTO meta.pipeline_run (run_id, dag_id, load_date, row_counts)
SELECT '{{ run_id }}',
       'orders_batch_dwh',
       '{{ ds }}'::date,
       jsonb_build_object(
           'customers',   (SELECT count(*) FROM stg.customers),
           'products',    (SELECT count(*) FROM stg.products),
           'orders',      (SELECT count(*) FROM stg.orders),
           'order_items', (SELECT count(*) FROM stg.order_items),
           'payments',    (SELECT count(*) FROM stg.payments)
       )
ON CONFLICT (run_id) DO UPDATE SET row_counts = excluded.row_counts;

TRUNCATE stg.customers, stg.products, stg.orders, stg.order_items, stg.payments;
