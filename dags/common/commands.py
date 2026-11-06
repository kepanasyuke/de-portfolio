"""Command builders shared by the DAGs.

Airflow orchestrates here; the cluster does the work. Keeping the command in one
place means the same jar set, memory limits and conf are used by every DAG, and
a change of the cluster address is a one-line edit.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Iterable

DEFAULT_PACKAGES = ",".join(
    [
        "org.apache.hadoop:hadoop-aws:3.3.4",
        "com.amazonaws:aws-java-sdk-bundle:1.12.262",
        "org.postgresql:postgresql:42.7.3",
        "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1",
    ]
)


def spark_submit(job: Path | str, *args: str) -> str:
    """Build a spark-submit command line for a BashOperator."""
    master = os.getenv("SPARK_MASTER", "spark://spark-master:7077")
    packages = os.getenv("SPARK_PACKAGES", DEFAULT_PACKAGES)
    java_home = os.getenv("JAVA_HOME", "")
    parts = [
        "spark-submit",
        f"--master {shlex.quote(master)}",
        "--deploy-mode client",
        f"--packages {shlex.quote(packages)}",
        f"--conf spark.executor.memory={os.getenv('SPARK_EXECUTOR_MEMORY', '1g')}",
        f"--conf spark.cores.max={os.getenv('SPARK_CORES_MAX', '2')}",
        f"--conf spark.driver.memory={os.getenv('SPARK_DRIVER_MEMORY', '1g')}",
        "--conf spark.sql.shuffle.partitions=8",
    ]
    if java_home:
        parts.append(f"--conf spark.executorEnv.JAVA_HOME={shlex.quote(java_home)}")
    parts.append(str(job))
    parts.extend(shlex.quote(argument) for argument in args)
    return " ".join(parts)


def dq_command(
    rules: Path | str,
    *,
    fail_on: str = "error",
    json_out: str | None = None,
    extra: Iterable[str] = (),
) -> str:
    """Build the data-quality gate command executed inside the Airflow worker."""
    parts = [
        "python -m dq.run",
        f"--rules {shlex.quote(str(rules))}",
        f"--fail-on {fail_on}",
    ]
    if json_out:
        parts.append(f"--json-out {shlex.quote(json_out)}")
    parts.extend(shlex.quote(argument) for argument in extra)
    return (
        f"PYTHONPATH={os.getenv('PORTFOLIO_ROOT', '/opt/project')}/03-dq-framework "
        + " ".join(parts)
    )
