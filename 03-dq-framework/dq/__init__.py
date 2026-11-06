"""Declarative data-quality engine used by every project in this portfolio."""

from .checks import (
    Rule,
    RuleError,
    Result,
    build_sql,
    evaluate,
    gate,
    load_rules,
    rule_from_mapping,
    summarize,
)

__all__ = [
    "Rule",
    "RuleError",
    "Result",
    "build_sql",
    "evaluate",
    "gate",
    "load_rules",
    "rule_from_mapping",
    "summarize",
]
