"""Table contracts and dialect-aware SQL for legacy-to-DWH reconciliation.

The idea is the same one that makes ``pt-table-checksum`` usable on huge tables:
never compare every row. Split the key space into buckets, compare a cheap
fingerprint per bucket, and only read the rows of buckets that disagree.

Everything here is pure: it builds SQL strings and validates identifiers. The
execution lives in ``parity.adapters`` and the orchestration in
``parity.compare``, which is what makes this testable without a database.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
FORBIDDEN = (";", "--", "/*", "*/", "'")

DIALECTS = ("postgres", "clickhouse")
NULL_TOKEN = "\u2205"


class SpecError(ValueError):
    """Raised when a table contract cannot be turned into safe SQL."""


def identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.match(value.strip()):
        raise SpecError(f"{field_name}: {value!r} is not a table or column identifier")
    return value.strip()


def _filter(value: str | None, field_name: str) -> str | None:
    """Validate the optional ``where`` fragment: no statement separators, no quotes."""
    if value is None:
        fragment = None
    else:
        stripped = value.strip()
        forbidden = next((token for token in FORBIDDEN if token in stripped), None)
        if forbidden is not None:
            raise SpecError(f"{field_name}: {forbidden!r} is not allowed in a filter")
        fragment = stripped or None
    return fragment


@dataclass(frozen=True)
class TableSpec:
    """What to compare, by which key, and which columns are allowed to differ."""

    name: str
    key: tuple[str, ...]
    columns: tuple[str, ...]
    where: str | None = None
    ignore: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name, "name"))
        if not self.key:
            raise SpecError(f"{self.name}: at least one key column is required")
        if not self.columns:
            raise SpecError(f"{self.name}: at least one column is required")
        object.__setattr__(self, "key", tuple(identifier(c, "key") for c in self.key))
        object.__setattr__(
            self, "columns", tuple(identifier(c, "columns") for c in self.columns)
        )
        unknown = set(self.ignore) - set(self.columns) - set(self.key)
        if unknown:
            raise SpecError(f"{self.name}: ignored columns are not part of the table: {sorted(unknown)}")
        object.__setattr__(self, "ignore", tuple(self.ignore))
        object.__setattr__(self, "where", _filter(self.where, "where"))
        if not set(self.key).issubset(set(self.columns) | set(self.key)):
            raise SpecError(f"{self.name}: key columns must be part of the table")

    @property
    def compared_columns(self) -> tuple[str, ...]:
        """Columns that participate in the fingerprint (volatile ones excluded)."""
        return tuple(column for column in self.columns if column not in self.ignore)

    @classmethod
    def from_mapping(cls, mapping: dict) -> TableSpec:
        missing = [name for name in ("name", "key", "columns") if name not in mapping]
        if missing:
            raise SpecError(f"table spec is missing {missing[0]!r} from {sorted(mapping)}")
        return cls(
            name=mapping["name"],
            key=tuple(mapping["key"]),
            columns=tuple(mapping["columns"]),
            where=mapping.get("where"),
            ignore=tuple(mapping.get("ignore", ())),
        )


CAST_TEMPLATES: dict[str, str] = {
    "postgres": "coalesce(cast({column} as text), '{null}')",
    "clickhouse": "coalesce(toString({column}), '{null}')",
}

CONCAT_SEPARATOR: dict[str, tuple[str, str]] = {
    "postgres": ("concat_ws('|', ", ")"),
    "clickhouse": ("concat(", ")"),
}

FINGERPRINT_TEMPLATES: dict[str, str] = {
    "postgres": "md5({inner})",
    "clickhouse": "lower(hex(MD5({inner})))",
}

BUCKET_TEMPLATES: dict[str, str] = {
    "postgres": "abs(hashtext({key})) % {buckets}",
    "clickhouse": "cityHash64({key}) % {buckets}",
}


def _template(templates: dict[str, str], dialect: str) -> str:
    template = templates.get(dialect)
    if template is None:
        raise SpecError(f"unknown dialect {dialect!r}")
    return template


def _cast_text(column: str, dialect: str) -> str:
    """Per-dialect cast to text with an explicit NULL sentinel."""
    return _template(CAST_TEMPLATES, dialect).format(column=column, null=NULL_TOKEN)


def _concat(parts: Sequence[str], dialect: str) -> str:
    """Join parts with a separator that never appears inside a value."""
    opening, closing = CONCAT_SEPARATOR.get(dialect, ("", ""))
    if not opening:
        raise SpecError(f"unknown dialect {dialect!r}")
    separator = ", '|', " if dialect == "clickhouse" else ", "
    return opening + separator.join(parts) + closing


def key_expression(spec: TableSpec, dialect: str) -> str:
    """Stable textual key used for ordering rows during a drill-down."""
    return _concat([_cast_text(column, dialect) for column in spec.key], dialect)


def fingerprint_expression(spec: TableSpec, dialect: str) -> str:
    """MD5 over the canonical concatenation of every compared column."""
    inner = _concat([_cast_text(column, dialect) for column in spec.compared_columns], dialect)
    return _template(FINGERPRINT_TEMPLATES, dialect).format(inner=inner)


def bucket_expression(spec: TableSpec, dialect: str, buckets: int) -> str:
    """Deterministic, engine-side hash bucket — the same key lands in the same bucket."""
    if buckets < 1:
        raise SpecError("buckets must be positive")
    key = key_expression(spec, dialect)
    return _template(BUCKET_TEMPLATES, dialect).format(key=key, buckets=buckets)


def _where_sql(spec: TableSpec, extra: str | None = None) -> str:
    clauses = [clause for clause in (spec.where, extra) if clause]
    return f" WHERE {' AND '.join(clauses)}" if clauses else ""


def signature_sql(spec: TableSpec, dialect: str, buckets: int) -> str:
    """Per-bucket fingerprint summary: cheap to compute, hard to collide."""
    fingerprint = fingerprint_expression(spec, dialect)
    bucket = bucket_expression(spec, dialect, buckets)
    return (
        f"SELECT {bucket} AS bucket,\n"
        f"       count(*) AS row_count,\n"
        f"       count(DISTINCT {fingerprint}) AS distinct_fingerprints,\n"
        f"       min({fingerprint}) AS min_fingerprint,\n"
        f"       max({fingerprint}) AS max_fingerprint\n"
        f"FROM {spec.name}{_where_sql(spec)}\n"
        f"GROUP BY 1\n"
        f"ORDER BY 1"
    )


def bucket_rows_sql(spec: TableSpec, dialect: str, buckets: int, bucket: int) -> str:
    """Key + fingerprint of every row in one bucket, ordered for a streaming diff."""
    if bucket < 0 or bucket >= buckets:
        raise SpecError(f"bucket {bucket} is out of range 0..{buckets - 1}")
    fingerprint = fingerprint_expression(spec, dialect)
    key = key_expression(spec, dialect)
    bucket_filter = f"{bucket_expression(spec, dialect, buckets)} = {bucket}"
    return (
        f"SELECT {key} AS k, {fingerprint} AS fp\n"
        f"FROM {spec.name}{_where_sql(spec, bucket_filter)}\n"
        f"ORDER BY k"
    )


def row_values_sql(spec: TableSpec, dialect: str, keys: Sequence[str]) -> str:
    """Full row for the columns of interest, for column-level diffing."""
    if not keys:
        raise SpecError("row_values_sql requires at least one key")
    columns = ", ".join(spec.compared_columns)
    key = key_expression(spec, dialect)
    quoted = ", ".join("'" + _escape_literal(value) for value in keys) + "'"
    return (
        f"SELECT {key} AS k, {columns}\n"
        f"FROM {spec.name}{_where_sql(spec, f'{key} IN ({quoted})')}\n"
        f"ORDER BY k"
    )


def _escape_literal(value: str) -> str:
    return str(value).replace("'", "''")
