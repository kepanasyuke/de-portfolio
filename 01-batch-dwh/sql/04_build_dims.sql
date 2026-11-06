-- Build the DM dimensions.
--
-- SCD2 for customers in two statements:
--   1. close the current version when any tracked attribute changed;
--   2. open a new version for every customer without a current row.
-- The partial unique index dm.dim_customer_current_uidx turns a broken SCD2
-- (two current rows for one customer) into a hard failure instead of a silent
-- double count in the fact table.

UPDATE dm.dim_customer d
SET valid_to   = now(),
    is_current = false
FROM dds.customers c
WHERE d.is_current
  AND d.customer_id = c.customer_id
  AND (d.email, d.full_name, d.country_code, d.segment)
      IS DISTINCT FROM (c.email, c.full_name, c.country_code, c.segment);

INSERT INTO dm.dim_customer (
    customer_id, email, full_name, country_code, segment,
    version, valid_from, valid_to, is_current
)
SELECT c.customer_id,
       c.email,
       c.full_name,
       c.country_code,
       c.segment,
       coalesce(v.max_version, 0) + 1,
       coalesce(c.created_at, now()),
       NULL,
       true
FROM dds.customers c
LEFT JOIN (
    SELECT customer_id,
           max(version)         AS max_version,
           bool_or(is_current)  AS has_current
    FROM dm.dim_customer
    GROUP BY customer_id
) v ON v.customer_id = c.customer_id
WHERE coalesce(v.has_current, false) = false;

INSERT INTO dm.dim_product (product_id, sku, name, category, unit_price, is_active)
SELECT product_id, sku, name, category, unit_price, coalesce(is_active, true)
FROM dds.products
ON CONFLICT (product_id) DO UPDATE SET
    sku        = excluded.sku,
    name       = excluded.name,
    category   = excluded.category,
    unit_price = excluded.unit_price,
    is_active  = excluded.is_active,
    _loaded_at = now();
