"""Schemas and column contracts for the batch pipeline.

Bronze keeps everything as text: the landing zone is an immutable copy of what
the source produced, and typing happens on the bronze -> staging hop. This is
what makes replaying a broken batch cheap: no data is lost to a bad cast at
ingest time.
"""

from __future__ import annotations

from pyspark.sql.types import StringType, StructField, StructType

LANDING_SCHEMA: dict[str, StructType] = {
    "customers": StructType(
        [
            StructField("customer_id", StringType()),
            StructField("email", StringType()),
            StructField("full_name", StringType()),
            StructField("country_code", StringType()),
            StructField("segment", StringType()),
            StructField("created_at", StringType()),
        ]
    ),
    "products": StructType(
        [
            StructField("product_id", StringType()),
            StructField("sku", StringType()),
            StructField("name", StringType()),
            StructField("category", StringType()),
            StructField("unit_price", StringType()),
            StructField("is_active", StringType()),
        ]
    ),
    "orders": StructType(
        [
            StructField("order_id", StringType()),
            StructField("customer_id", StringType()),
            StructField("status", StringType()),
            StructField("currency", StringType()),
            StructField("total_amount", StringType()),
            StructField("created_at", StringType()),
            StructField("updated_at", StringType()),
        ]
    ),
    "order_items": StructType(
        [
            StructField("order_item_id", StringType()),
            StructField("order_id", StringType()),
            StructField("product_id", StringType()),
            StructField("quantity", StringType()),
            StructField("unit_price", StringType()),
            StructField("discount_amount", StringType()),
        ]
    ),
    "payments": StructType(
        [
            StructField("payment_id", StringType()),
            StructField("order_id", StringType()),
            StructField("amount", StringType()),
            StructField("method", StringType()),
            StructField("paid_at", StringType()),
        ]
    ),
}

STAGING_COLUMNS: dict[str, list[str]] = {
    "customers": [
        "customer_id",
        "email",
        "full_name",
        "country_code",
        "segment",
        "created_at",
        "_ingest_ts",
    ],
    "products": [
        "product_id",
        "sku",
        "name",
        "category",
        "unit_price",
        "is_active",
        "_ingest_ts",
    ],
    "orders": [
        "order_id",
        "customer_id",
        "status",
        "currency",
        "total_amount",
        "created_at",
        "updated_at",
        "_ingest_ts",
    ],
    "order_items": [
        "order_item_id",
        "order_id",
        "product_id",
        "quantity",
        "unit_price",
        "discount_amount",
        "_ingest_ts",
    ],
    "payments": ["payment_id", "order_id", "amount", "method", "paid_at", "_ingest_ts"],
}

ORDER_STATUSES = ("created", "paid", "cancelled", "refunded")
ENTITIES = tuple(LANDING_SCHEMA)
