"""Generate the landing layer: five CSVs the batch pipeline reads.

Deterministic by seed, and deliberately imperfect: about 3% of rows carry a
defect that the staging hop must quarantine (missing keys, unknown statuses,
negative amounts, duplicate lines, future timestamps). A portfolio pipeline that
only ever sees clean data proves nothing.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATEGORIES = ("electronics", "books", "grocery", "fashion", "sport")
SEGMENTS = ("retail", "smb", "enterprise")
COUNTRIES = ("RU", "KZ", "BY", "AM")
STATUSES = ("created", "paid", "paid", "paid", "cancelled")
METHODS = ("card", "sbp", "wallet")
ORDER_HEADER = [
    "order_id",
    "customer_id",
    "status",
    "currency",
    "total_amount",
    "created_at",
    "updated_at",
]
CUSTOMER_HEADER = ["customer_id", "email", "full_name", "country_code", "segment", "created_at"]
PRODUCT_HEADER = ["product_id", "sku", "name", "category", "unit_price", "is_active"]
ITEM_HEADER = ["order_item_id", "order_id", "product_id", "quantity", "unit_price", "discount_amount"]
PAYMENT_HEADER = ["payment_id", "order_id", "amount", "method", "paid_at"]

DEFECT_FIELDS = {
    "orders": ("customer_id", "status", "total_amount", "created_at"),
    "customers": ("customer_id", "email", "country_code"),
    "products": ("product_id", "sku", "unit_price"),
    "order_items": ("order_id", "product_id", "quantity", "unit_price"),
    "payments": ("order_id", "amount"),
}


@dataclass(frozen=True)
class Stats:
    customers: int
    products: int
    orders: int
    items: int
    payments: int
    defects: int


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _money(value: float) -> str:
    return f"{value:.2f}"


def _defect(entity: str, row: list[str], rng: random.Random, index: dict[str, int]) -> list[str]:
    """Break exactly one field of a row, in a way the contract must catch."""
    broken = list(row)
    field = rng.choice(DEFECT_FIELDS[entity])
    position = index[field]
    if field in ("customer_id", "order_id", "product_id", "order_item_id", "payment_id", "sku", "email"):
        broken[position] = ""
    elif field in ("total_amount", "unit_price", "amount"):
        broken[position] = _money(-abs(float(broken[position] or 1)))
    elif field == "quantity":
        broken[position] = "0"
    elif field == "created_at":
        broken[position] = _iso(datetime.now(timezone.utc) + timedelta(days=3))
    elif field == "status":
        broken[position] = "shipped"
    elif field == "country_code":
        broken[position] = "RUS"
    return broken


def _write(path: Path, header: list[str], rows: Iterable[list[str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _entity_rows(
    rows: list[list[str]],
    entity: str,
    header: list[str],
    rng: random.Random,
    defect_ratio: float,
) -> tuple[list[list[str]], int]:
    """Break a share of rows for one entity and report how many were broken."""
    index = {name: position for position, name in enumerate(header)}
    output: list[list[str]] = []
    defects = 0
    for row in rows:
        broken = rng.random() < defect_ratio
        if broken:
            defects += 1
        output.append(_defect(entity, row, rng, index) if broken else row)
    return output, defects


def generate(
    out_dir: Path,
    business_date: str,
    orders: int = 800,
    customers: int = 400,
    products: int = 160,
    defect_ratio: float = 0.03,
    seed: int = 42,
) -> Stats:
    """Write the five landing files and return row counts."""
    rng = random.Random(seed)
    day = datetime.fromisoformat(business_date).replace(tzinfo=timezone.utc)

    customer_rows: list[list[str]] = []
    for number in range(customers):
        customer_id = f"CUST-{number:06d}"
        customer_rows.append(
            [
                customer_id,
                f"user{number}@example.com",
                f"Клиент {number}",
                rng.choice(COUNTRIES),
                rng.choice(SEGMENTS),
                _iso(day - timedelta(days=rng.randrange(30, 900))),
            ]
        )

    product_rows: list[list[str]] = []
    for number in range(products):
        price = rng.randrange(199_00, 49_999_00) / 100
        product_rows.append(
            [
                f"PROD-{number:05d}",
                f"SKU-{number:05d}",
                f"Товар {number}",
                rng.choice(CATEGORIES),
                _money(price),
                "true",
            ]
        )

    order_rows: list[list[str]] = []
    item_rows: list[list[str]] = []
    payment_rows: list[list[str]] = []
    for number in range(orders):
        order_id = f"ORD-{business_date.replace('-', '')}-{number:05d}"
        created = day + timedelta(minutes=rng.randrange(0, 23 * 60), seconds=rng.randrange(0, 60))
        status = rng.choice(STATUSES)
        total = 0.0
        line_count = rng.randrange(1, 5)
        for line in range(line_count):
            product = rng.choice(product_rows)
            quantity = rng.randrange(1, 4)
            unit_price = float(product[4])
            discount = round(unit_price * quantity * rng.choice((0, 0, 0.05, 0.1)), 2)
            total += unit_price * quantity - discount
            item_rows.append(
                [
                    f"{order_id}-{line + 1}",
                    order_id,
                    product[0],
                    str(quantity),
                    _money(unit_price),
                    _money(discount),
                ]
            )
        order_rows.append(
            [
                order_id,
                rng.choice(customer_rows)[0],
                status,
                "RUB",
                _money(round(total, 2)),
                _iso(created),
                _iso(created + timedelta(minutes=rng.randrange(1, 180))),
            ]
        )
        if status == "paid":
            payment_rows.append(
                [
                    f"PAY-{order_id}",
                    order_id,
                    _money(round(total, 2)),
                    rng.choice(METHODS),
                    _iso(created + timedelta(minutes=rng.randrange(1, 60))),
                ]
            )

    entities = {
        "customers": (CUSTOMER_HEADER, customer_rows),
        "products": (PRODUCT_HEADER, product_rows),
        "orders": (ORDER_HEADER, order_rows),
        "order_items": (ITEM_HEADER, item_rows),
        "payments": (PAYMENT_HEADER, payment_rows),
    }

    defects = 0
    written: dict[str, int] = {}
    for entity, (header, rows) in entities.items():
        output, entity_defects = _entity_rows(rows, entity, header, rng, defect_ratio)
        defects += entity_defects
        if entity == "orders" and output:
            output.append(list(output[0]))
            defects += 1
        written[entity] = _write(out_dir / f"{entity}.csv", header, output)

    return Stats(
        customers=written["customers"],
        products=written["products"],
        orders=written["orders"],
        items=written["order_items"],
        payments=written["payments"],
        defects=defects,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/landing", type=Path)
    parser.add_argument("--date", required=True, help="business date, YYYY-MM-DD")
    parser.add_argument("--orders", type=int, default=800)
    parser.add_argument("--customers", type=int, default=400)
    parser.add_argument("--products", type=int, default=160)
    parser.add_argument("--defect-ratio", type=float, default=0.03)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stats = generate(
        out_dir=args.out,
        business_date=args.date,
        orders=args.orders,
        customers=args.customers,
        products=args.products,
        defect_ratio=args.defect_ratio,
        seed=args.seed,
    )
    print(
        f"landing written to {args.out}: customers={stats.customers} products={stats.products} "
        f"orders={stats.orders} order_items={stats.items} payments={stats.payments} "
        f"defects={stats.defects}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
