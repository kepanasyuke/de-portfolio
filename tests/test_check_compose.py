"""Static compose checks: structure rules and the ``--file`` contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    """Import scripts/check_compose.py by path — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "compose_check", ROOT / "scripts" / "check_compose.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_checker()
SHIPPED = ROOT / "docker-compose.yml"


def test_shipped_compose_has_no_problems(capsys):
    assert check.main([]) == 0
    assert "0 problem(s)" in capsys.readouterr().out


def test_file_argument_is_honoured(tmp_path, capsys):
    """Regression: the variable check used to read the root file regardless of --file."""
    original = SHIPPED.read_text(encoding="utf-8")
    target = tmp_path / "compose.yml"
    target.write_text(original.replace("${S3_BUCKET:-lake}", "${MYSTERY_VAR:-lake}"), encoding="utf-8")

    code = check.main(["--file", str(target)])
    output = capsys.readouterr().out
    assert code == 1
    assert "MYSTERY_VAR" in output


def test_unreadable_file_is_reported(tmp_path, capsys):
    code = check.main(["--file", str(tmp_path / "absent.yml")])
    assert code == 1
    assert "cannot read" in capsys.readouterr().out


def test_unknown_dependency_is_reported():
    services = {"a": {"image": "a", "depends_on": ["b"]}}
    problems = check.check_dependencies(services)
    assert problems == ["a: depends_on unknown service 'b'"]


def test_known_dependency_is_accepted():
    services = {"a": {"image": "a", "depends_on": {"b": {"condition": "service_started"}}}, "b": {"image": "b"}}
    assert check.check_dependencies(services) == []


def test_duplicate_host_port_is_reported():
    services = {
        "a": {"image": "a", "ports": ["8080:80"]},
        "b": {"image": "b", "ports": ["8080:81"]},
    }
    problems = check.check_ports_are_unique(services)
    assert len(problems) == 1
    assert "8080" in problems[0]


def test_distinct_host_ports_are_accepted():
    services = {
        "a": {"image": "a", "ports": ["8080:80"]},
        "b": {"image": "b", "ports": ["8081:80"]},
    }
    assert check.check_ports_are_unique(services) == []


def test_undeclared_named_volume_is_reported():
    problems = check.check_service("a", {"image": "a", "volumes": ["data:/data"]}, set())
    assert problems == ["a: named volume data is not declared in top-level volumes"]


def test_declared_named_volume_is_accepted():
    definition = {"image": "a", "volumes": ["data:/data"]}
    assert check.check_service("a", definition, {"data"}) == []


def test_missing_bind_mount_is_reported():
    problems = check.check_service("a", {"image": "a", "volumes": ["./nope:/x"]}, set())
    assert problems and "does not exist" in problems[0]


def test_existing_bind_mount_is_accepted():
    definition = {"image": "a", "volumes": ["./docker/airflow/Dockerfile:/x"]}
    assert check.check_service("a", definition, set()) == []


def test_missing_env_file_is_reported():
    problems = check.check_service("a", {"image": "a", "env_file": ".env.absent"}, set())
    assert problems == ["a: env_file .env.absent does not exist"]


def test_bad_port_mapping_is_reported():
    problems = check.check_service("a", {"image": "a", "ports": ["http:80"]}, set())
    assert problems and "malformed" in problems[0]


def test_service_without_image_or_build_is_reported():
    assert check.check_service("a", {}, set()) == ["a: needs 'image' or 'build'"]


def test_missing_build_context_is_reported():
    problems = check.check_service("a", {"build": {"context": "./nowhere"}}, set())
    assert problems and "build context" in problems[0]


def test_documented_variables_pass(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  a:\n    image: ${POSTGRES_USER:-dwh}\n", encoding="utf-8")
    assert check.check_variables(compose) == []


def test_undocumented_variable_is_reported(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  a:\n    image: ${UNDOCUMENTED_VAR}\n", encoding="utf-8")
    problems = check.check_variables(compose)
    assert problems == ["variable UNDOCUMENTED_VAR is used but not documented in .env.example"]


def test_report_returns_the_service_count():
    document = check.load(SHIPPED)
    problems, total = check._report(document, SHIPPED)
    assert problems == []
    assert total == 11
