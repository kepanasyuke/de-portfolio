-- Merge compacted events into the DDS and project the order status.
--
-- Transition validation is done here, not in the streaming job: the stream
-- cannot see the full history of an order (it only sees the current micro-batch),
-- while the DWH has every event ever received for that order. An event that is
-- not a legal next step from the order's previous status is stored with
-- is_quarantined = true instead of silently overwriting the truth.

INSERT INTO dds.order_events (
    event_id, order_id, event_type, status, occurred_at,
    amount, currency, source_system, is_quarantined
)
SELECT e.event_id,
       e.order_id,
       e.event_type,
       CASE e.event_type
           WHEN 'order_created'   THEN 'created'
           WHEN 'order_paid'      THEN 'paid'
           WHEN 'order_cancelled' THEN 'cancelled'
           WHEN 'order_refunded'  THEN 'refunded'
       END AS status,
       e.occurred_at AT TIME ZONE 'UTC',
       e.amount,
       upper(e.currency),
       e.source_system,
       NOT (CASE e.event_type
                WHEN 'order_created'   THEN prev.status IS NULL
                WHEN 'order_paid'      THEN prev.status = 'created'
                WHEN 'order_cancelled' THEN prev.status IN ('created', 'paid')
                WHEN 'order_refunded'  THEN prev.status = 'paid'
                ELSE false
            END) AS is_quarantined
FROM stg.order_events e
LEFT JOIN LATERAL (
    SELECT h.status
    FROM dds.order_events h
    WHERE h.order_id = e.order_id
      AND h.occurred_at < (e.occurred_at AT TIME ZONE 'UTC')
      AND NOT h.is_quarantined
    ORDER BY h.occurred_at DESC
    LIMIT 1
) prev ON true
ON CONFLICT (event_id) DO NOTHING;

UPDATE dds.orders o
SET status     = latest.status,
    updated_at = latest.occurred_at,
    _loaded_at = now()
FROM (
    SELECT DISTINCT ON (order_id)
           order_id,
           status,
           occurred_at
    FROM dds.order_events
    WHERE NOT is_quarantined
    ORDER BY order_id, occurred_at DESC, event_id DESC
) latest
WHERE o.order_id = latest.order_id
  AND o.status IS DISTINCT FROM latest.status;

TRUNCATE stg.order_events;
