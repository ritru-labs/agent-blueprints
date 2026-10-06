"""Run all local controls against disposable PostgreSQL and an immutable compiler image."""

import argparse
import os
import subprocess
import sys
import time
from uuid import uuid4

import psycopg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--compiler-image", required=True)
parser.add_argument("--postgres-image", required=True)
args = parser.parse_args()
if not args.compiler_image.startswith("sha256:") or not args.postgres_image.startswith("sha256:"):
    parser.error("Use local immutable image IDs from docker image inspect")
name = "infra-migration-pg-" + uuid4().hex[:12]
password = uuid4().hex
environment = dict(os.environ, POSTGRES_PASSWORD=password)
subprocess.run(
    [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        name,
        "--env",
        "POSTGRES_PASSWORD",
        "-p",
        "127.0.0.1::5432",
        args.postgres_image,
    ],
    env=environment,
    check=True,
    stdout=subprocess.DEVNULL,
)
try:
    port = (
        subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True)
        .strip()
        .rsplit(":", 1)[1]
    )
    dsn = (
        f"host=127.0.0.1 port={port} dbname=postgres user=postgres "
        f"password={password} connect_timeout=1"
    )
    for _ in range(50):
        try:
            with psycopg.connect(dsn):
                pass
            break
        except psycopg.OperationalError:
            time.sleep(0.2)
    else:
        raise RuntimeError("Disposable PostgreSQL did not become ready")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        env=dict(os.environ, INFRA_TEST_POSTGRES_DSN=dsn, INFRA_RUNNER_IMAGE=args.compiler_image),
        timeout=120,
    )
    raise SystemExit(result.returncode)
finally:
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
