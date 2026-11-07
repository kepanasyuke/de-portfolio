"""Landing-zone generation and upload: determinism, defects, CLI paths."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from ingestion import generate_landing, upload_landing

DATE = "2026-11-05"
FILES = ("customers.csv", "products.csv", "orders.csv", "order_items.csv", "payments.csv")


def generate(tmp_path: Path, **overrides):
    options = {
        "out_dir": tmp_path,
        "business_date": DATE,
        "orders": 20,
        "customers": 10,
        "products": 8,
        "defect_ratio": 0.0,
        "seed": 1,
    }
    options.update(overrides)
    return generate_landing.generate(**options)


def read_rows(tmp_path: Path, name: str) -> list[dict]:
    with (tmp_path / name).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows


def test_generate_writes_every_file(tmp_path):
    generate(tmp_path)
    assert all((tmp_path / name).exists() for name in FILES)


def test_generate_returns_consistent_counts(tmp_path):
    stats = generate(tmp_path)
    assert stats.customers == len(read_rows(tmp_path, "customers.csv"))
    assert stats.products == len(read_rows(tmp_path, "products.csv"))
    assert stats.items == len(read_rows(tmp_path, "order_items.csv"))


def test_headers_match_the_contract(tmp_path):
    generate(tmp_path)
    assert list(read_rows(tmp_path, "orders.csv")[0]) == [
        "order_id",
        "customer_id",
        "status",
        "currency",
        "total_amount",
        "created_at",
        "updated_at",
    ]


def test_order_ids_carry_the_business_date(tmp_path):
    generate(tmp_path)
    assert all(DATE.replace("-", "") in row["order_id"] for row in read_rows(tmp_path, "orders.csv"))


def test_orders_reference_existing_customers(tmp_path):
    generate(tmp_path)
    customers = {row["customer_id"] for row in read_rows(tmp_path, "customers.csv")}
    assert all(row["customer_id"] in customers for row in read_rows(tmp_path, "orders.csv"))


def test_order_items_reference_existing_orders_and_products(tmp_path):
    generate(tmp_path)
    orders = {row["order_id"] for row in read_rows(tmp_path, "orders.csv")}
    products = {row["product_id"] for row in read_rows(tmp_path, "products.csv")}
    items = read_rows(tmp_path, "order_items.csv")
    assert all(row["order_id"] in orders for row in items)
    assert all(row["product_id"] in products for row in items)


def test_payments_only_belong_to_paid_orders(tmp_path):
    generate(tmp_path)
    paid = {row["order_id"] for row in read_rows(tmp_path, "orders.csv") if row["status"] == "paid"}
    assert all(row["order_id"] in paid for row in read_rows(tmp_path, "payments.csv"))


def test_generation_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate(first, seed=5)
    generate(second, seed=5)
    assert (first / "orders.csv").read_text(encoding="utf-8") == (
        second / "orders.csv"
    ).read_text(encoding="utf-8")


def test_another_seed_changes_the_data(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate(first, seed=5)
    generate(second, seed=6)
    assert (first / "orders.csv").read_text(encoding="utf-8") != (
        second / "orders.csv"
    ).read_text(encoding="utf-8")


def test_clean_run_contains_no_defects(tmp_path):
    stats = generate(tmp_path, defect_ratio=0.0)
    assert stats.defects == 1


def test_defect_ratio_breaks_a_share_of_rows(tmp_path):
    stats = generate(tmp_path, defect_ratio=0.5)
    assert stats.defects > 1


def test_order_duplicate_is_always_injected(tmp_path):
    generate(tmp_path, defect_ratio=0.0)
    orders = read_rows(tmp_path, "orders.csv")
    assert len(orders) == 21
    assert orders[0]["order_id"] == orders[-1]["order_id"]


def test_output_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "landing"
    generate(target)
    assert target.is_dir()


def test_parse_args_defaults():
    args = generate_landing.parse_args(["--date", DATE])
    assert args.orders == 800
    assert args.defect_ratio == pytest.approx(0.03)


def test_main_reports_counts(capsys, tmp_path):
    code = generate_landing.main(
        ["--out", str(tmp_path), "--date", DATE, "--orders", "3", "--customers", "2", "--products", "2"]
    )
    assert code == 0
    assert "landing written to" in capsys.readouterr().out


def test_print_plan_lists_every_key(capsys, tmp_path):
    files = [tmp_path / name for name in FILES]
    keys = [f"landing/load_date={DATE}/{name}" for name in FILES]
    upload_landing._print_plan(files, keys, "lake")
    out = capsys.readouterr().out
    assert out.count("s3://lake/landing/load_date=") == len(FILES)


def test_upload_main_fails_without_files(capsys, tmp_path):
    code = upload_landing.main(["--dir", str(tmp_path), "--date", DATE])
    assert code == 2
    assert "missing landing files" in capsys.readouterr().err


def test_upload_main_dry_run_lists_targets(capsys, tmp_path):
    generate(tmp_path, orders=1, customers=1, products=1)
    code = upload_landing.main(["--dir", str(tmp_path), "--date", DATE, "--dry-run"])
    assert code == 0
    assert capsys.readouterr().out.count("-> s3://") == len(FILES)


def test_upload_main_pushes_through_the_helper(tmp_path, monkeypatch):
    generate(tmp_path, orders=1, customers=1, products=1)
    calls: list[list] = []

    def fake_upload(files, keys, bucket, endpoint):
        calls.append([len(files), bucket])

    monkeypatch.setattr(upload_landing, "_upload", fake_upload)
    code = upload_landing.main(["--dir", str(tmp_path), "--date", DATE, "--bucket", "lake"])
    assert code == 0
    assert calls == [[len(FILES), "lake"]]


def test_parse_upload_args_defaults():
    args = upload_landing.parse_args(["--date", DATE])
    assert args.bucket == "lake"
    assert args.prefix == "landing"
    assert args.dry_run is False


def test_stats_dataclass_is_frozen():
    stats = generate_landing.Stats(1, 2, 3, 4, 5, 6)
    with pytest.raises(AttributeError):
        stats.orders = 9


def test_defect_fields_cover_every_entity():
    assert set(generate_landing.DEFECT_FIELDS) == {
        "orders",
        "customers",
        "products",
        "order_items",
        "payments",
    }


def test_json_spec_of_the_generator_is_stable(tmp_path):
    generate(tmp_path, orders=5)
    payload = json.loads(json.dumps({"orders": len(read_rows(tmp_path, "orders.csv"))}))
    assert payload["orders"] == 6
