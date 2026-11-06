-- Rebuild the fact table for a rolling window.
--
-- Why a window instead of INSERT-only: orders change status and amounts after
-- they are created (paid, cancelled, refunded), and corrections arrive with
-- later batches. Delete+insert over a bounded window makes the load idempotent
-- and lets late corrections land without a full rebuild. In a large DWH this
-- is the same pattern with partitioned tables and partition swaps.

DELETE FROM dm.fact_sales
WHERE date_key BETWEEN to_char(('{{ macros.ds_add(ds, -3) }}')::date, 'YYYYMMDD')::integer
                   AND to_char(('{{ ds }}')::date, 'YYYYMMDD')::integer;

INSERT INTO dm.fact_sales (
    order_item_id, order_id, date_key, customer_key, product_key,
    quantity, gross_amount, discount_amount, net_amount, order_status
)
SELECT i.order_item_id,
       o.order_id,
       to_char(o.created_at AT TIME ZONE 'UTC', 'YYYYMMDD')::integer AS date_key,
       dc.customer_key,
       dp.product_key,
       i.quantity,
       round(i.quantity * i.unit_price, 2)                      AS gross_amount,
       round(i.discount_amount, 2)                              AS discount_amount,
       round(i.quantity * i.unit_price - i.discount_amount, 2)  AS net_amount,
       o.status
FROM dds.order_items i
JOIN dds.orders o
  ON o.order_id = i.order_id
 AND o.status <> 'cancelled'
JOIN dm.dim_product dp
  ON dp.product_id = i.product_id
JOIN dm.dim_customer dc
  ON dc.customer_id = o.customer_id
 AND o.created_at >= dc.valid_from
 AND (dc.valid_to IS NULL OR o.created_at < dc.valid_to)
WHERE o.created_at >= '{{ macros.ds_add(ds, -3) }}'::date
  AND o.created_at <  '{{ ds }}'::date + interval '1 day'
ON CONFLICT (order_item_id) DO NOTHING;
