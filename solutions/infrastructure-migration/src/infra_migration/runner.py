"""Disposable Docker runner with immutable images, no-network compilation and bounded output."""

import hashlib
import re
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from .generation import ProjectBundle, verify_bundle_directory
from .tools import AccessDenied


class DockerRunner:
    def __init__(self, image: str, *, timeout=60, output_limit=4_000_000, cloud_network=None):
        if not re.fullmatch(r"(?:[\w./:-]+@sha256:|sha256:)[0-9a-f]{64}", image):
            raise AccessDenied("Runner requires an immutable image digest")
        if not 1 <= timeout <= 600 or not 1 <= output_limit <= 10_000_000:
            raise ValueError("Runner budgets outside policy")
        self.image, self.timeout, self.output_limit = image, timeout, output_limit
        if cloud_network is not None and not re.fullmatch(
            r"infra-migration-egress-[a-z0-9-]+", cloud_network
        ):
            raise AccessDenied("Cloud runner requires a designated egress network")
        self.cloud_network = cloud_network

    def run(
        self,
        bundle: ProjectBundle,
        directory: Path,
        args: list[str],
        *,
        state_directory: Path | None = None,
        credentials: dict | None = None,
    ):
        verify_bundle_directory(bundle, directory)
        if not args or args[0] not in {"node", "pulumi"}:
            raise AccessDenied("Runner command is not allowlisted")
        if args[0] == "pulumi" and (len(args) < 2 or args[1] not in {"preview", "import", "stack"}):
            raise AccessDenied("Pulumi action is not allowlisted")
        if args[0] == "pulumi" and args[1] == "stack" and (len(args) < 3 or args[2] != "export"):
            raise AccessDenied("Only read-only stack export is allowed")
        if args[0] == "pulumi" and (not state_directory or not credentials):
            raise AccessDenied("Pulumi requires explicit state and credential leases")
        import os

        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"]}
        name = "infra-migration-" + uuid4().hex
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=1g",
            "--cpus=2",
            "--user=65532:65532",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=128m",
            "--env=HOME=/tmp",
            "--env=NODE_PATH=/opt/deps/node_modules",
            "--workdir=/project",
            "--mount",
            f"type=bind,src={directory.resolve()},dst=/project,readonly",
        ]
        if args[0] == "node":
            command += ["--network=none"]
        else:
            if self.cloud_network is None:
                raise AccessDenied("Qualified cloud egress network is not configured")
            command += ["--network=" + self.cloud_network]
            if state_directory.is_symlink() or not state_directory.is_dir():
                raise AccessDenied("Invalid state directory")
            # Scope read credentials to a lease; never inherit the host's AWS profile or secrets.
            allowed = {
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_REGION",
                "PULUMI_CONFIG_PASSPHRASE",
            }
            if set(credentials) - allowed or not {
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_REGION",
                "PULUMI_CONFIG_PASSPHRASE",
            } <= set(credentials):
                raise AccessDenied("Credential lease is incomplete or contains forbidden keys")
            environment.update(credentials)
            command += [
                "--mount",
                f"type=bind,src={state_directory.resolve()},dst=/state",
                "--env=PULUMI_BACKEND_URL=file:///state",
                "--env=PULUMI_HOME=/opt/pulumi-home",
            ]
            command += ["--env=" + key for key in sorted(credentials)]
        command += [self.image, *args]
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, env=environment, stdout=output, stderr=output)
            try:
                while process.poll() is None:
                    if (
                        time.monotonic() - started > self.timeout
                        or output.tell() > self.output_limit
                    ):
                        raise AccessDenied("Runner time or output budget exhausted")
                    time.sleep(0.05)
                if output.tell() > self.output_limit:
                    raise AccessDenied("Runner output exceeds budget")
                output.seek(0)
                payload = output.read()
                if process.returncode != 0:
                    # Do not surface raw provider output containing potential secrets.
                    raise AccessDenied(
                        f"Runner failed with exit status {process.returncode}; "
                        f"output digest {hashlib.sha256(payload).hexdigest()}"
                    )
                return payload.decode("utf-8")
            finally:
                if process.poll() is None:
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                    process.kill()
                    process.wait(timeout=10)

    def compile(self, bundle: ProjectBundle, directory: Path):
        self.run(
            bundle,
            directory,
            [
                "node",
                "/opt/deps/node_modules/typescript/bin/tsc",
                "--project",
                "/project/tsconfig.json",
                "--baseUrl",
                "/opt/deps/node_modules",
                "--typeRoots",
                "/opt/deps/node_modules/@types",
            ],
        )
        return {
            "artifact_digest": bundle.artifact_digest,
            "runner_image": self.image,
            "check": "typescript_compile",
            "passed": True,
        }
