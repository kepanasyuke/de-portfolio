-- Analytical queries used by the marts and by bench/benchmark.py.
-- Each pair (slow / fast) isolates one optimisation so the effect is measurable
-- instead of folklore.

-- 1. Daily revenue from the pre-aggregated mart: merge partial states.
--    Reads one row per (day, category) instead of every order line.
SELECT
    order_date,
    category,
    uniqExactMerge(orders)    AS orders,
    sumMerge(items)           AS items,
    sumMerge(revenue)         AS revenue,
    uniqExactMerge(customers) AS customers
FROM marts.daily_revenue
WHERE order_date >= today() - 30
GROUP BY order_date, category
ORDER BY order_date, revenue DESC;

-- 2. Same question straight from the fact (baseline for the comparison).
SELECT
    order_date,
    category,
    uniqExact(order_id)   AS orders,
    sum(quantity)         AS items,
    sum(net_amount)       AS revenue
FROM marts.fact_sales
WHERE order_date >= today() - 30
  AND order_status <> 'cancelled'
GROUP BY order_date, category
ORDER BY order_date, revenue DESC;

-- 3. PREWHERE: the filter column is read and applied before the other columns
--    are materialised. On a wide fact this is the cheapest single win.
SELECT category, sum(net_amount) AS revenue
FROM marts.fact_sales
PREWHERE order_date BETWEEN '2026-10-01' AND '2026-10-31'
WHERE order_status = 'paid'
GROUP BY category
ORDER BY revenue DESC
LIMIT 10;

-- 4. Approximate distinct: uniqCombined uses ~1.6 KB of state per group at a
--    bounded error, uniqExact keeps every id. For dashboards the approximation
--    is the right default; for invoicing it is not.
SELECT
    toStartOfMonth(order_date)     AS month,
    uniqCombined(12)(customer_id)  AS customers_approx,
    uniqExact(customer_id)         AS customers_exact
FROM marts.fact_sales
WHERE order_date >= today() - 180
GROUP BY month
ORDER BY month;

-- 5. ReplacingMergeTree: FINAL forces deduplication at read time and is
--    correct but expensive. The cheap alternative is argMax over the version.
SELECT
    order_item_id,
    argMax(net_amount, _loaded_at) AS net_amount,
    argMax(order_status, _loaded_at) AS order_status
FROM marts.fact_sales
WHERE order_date = today() - 1
GROUP BY order_item_id;

-- 6. LTV per customer with window functions.
SELECT
    customer_id,
    first_order,
    last_order,
    orders,
    revenue,
    revenue / orders                        AS avg_order_value,
    dateDiff('day', first_order, last_order) AS lifetime_days
FROM marts.customer_lifetime
ORDER BY revenue DESC
LIMIT 100;
